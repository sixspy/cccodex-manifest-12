import sqlite3
import threading

import pytest

from app.config import Config
from app.db import connect
from app.services.exports import create_export, land_export
from app.services.objects import ServiceError


def _closed_batch(authed):
    batches = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    bid = batches[0]["id"]
    authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/a.parquet", "content_hash": "hash-a", "byte_size": 10},
    )
    authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/b.parquet", "content_hash": "hash-b", "byte_size": 20},
    )
    r = authed.post(f"/api/batches/{bid}/close", json={})
    assert r.status_code == 200
    return bid


def test_export_appendix_and_landing_chain(authed):
    bid = _closed_batch(authed)

    r = authed.post(
        f"/api/batches/{bid}/export", json={"credential": "manifest_package"}
    )
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["status"] == "已生成"
    assert job["target_name"].startswith(f"manifest_batch_{bid}_")
    assert job["target_name"].endswith(".json")
    assert len(job["package_sha256"]) == 64
    assert job["object_count"] == 2

    # 导出队列能看见包名
    queue = authed.get("/api/exports").json()["items"]
    assert len(queue) == 1
    assert queue[0]["target_name"] == job["target_name"]

    # 批次详情带导出芯片数据
    detail = authed.get(f"/api/batches/{bid}").json()
    assert detail["export"]["id"] == job["id"]

    # 指纹附页：包内冻结指纹与库内闭合对象一致
    ap = authed.get(f"/api/exports/{job['id']}/appendix")
    assert ap.status_code == 200
    appendix = ap.json()["appendix"]
    assert appendix["verified"] is True
    assert {o["object_key"] for o in appendix["objects"]} == {
        "raw/a.parquet",
        "raw/b.parquet",
    }
    assert {o["content_hash"] for o in appendix["objects"]} == {"hash-a", "hash-b"}

    # 下游解析落库
    land = authed.post(f"/api/exports/{job['id']}/land", json={})
    assert land.status_code == 200
    landing = land.json()["landing"]
    assert landing["object_count"] == 2
    assert landing["target_name"] == job["target_name"]

    # 进本机库，指纹可核
    conn = connect(Config.database())
    rows = conn.execute(
        "SELECT object_key, content_hash FROM downstream_objects"
    ).fetchall()
    conn.close()
    assert {(r[0], r[1]) for r in rows} == {
        ("raw/a.parquet", "hash-a"),
        ("raw/b.parquet", "hash-b"),
    }

    # 重复落库被拒
    again = authed.post(f"/api/exports/{job['id']}/land", json={})
    assert again.status_code == 409


def test_snapshot_table_credential_rejected(authed):
    bid = _closed_batch(authed)
    r = authed.post(
        f"/api/batches/{bid}/export", json={"credential": "snapshot_table"}
    )
    assert r.status_code == 400
    assert "凭证" in r.json()["error"]


def test_only_closed_batch_can_export(authed):
    batches = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    bid = next(b["id"] for b in batches if b["zone_code"] == "bronze_payments")
    r = authed.post(f"/api/batches/{bid}/export", json={})
    assert r.status_code == 409


def test_concurrent_export_single_artifact(authed):
    bid = _closed_batch(authed)
    results = []

    def worker():
        try:
            results.append(create_export(bid))
        except ServiceError as exc:
            results.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    jobs = [x for x in results if not isinstance(x, ServiceError)]
    errors = [x for x in results if isinstance(x, ServiceError)]
    assert len(jobs) == 1
    assert len(errors) == 1

    conn = connect(Config.database())
    success = conn.execute(
        "SELECT COUNT(*) FROM export_jobs WHERE batch_id=? AND status='已生成'", (bid,)
    ).fetchone()[0]
    conn.close()
    assert success == 1

    # 再来一次也不允许第二份产物
    with pytest.raises(ServiceError):
        create_export(bid)
    queue = authed.get("/api/exports").json()["items"]
    assert len([j for j in queue if j["status"] == "已生成"]) == 1


def test_fingerprint_mismatch_blocks_export(authed):
    bid = _closed_batch(authed)
    # 闭合后拧动固化数，导出必须拦截且队列留痕
    conn = connect(Config.database())
    conn.execute(
        "UPDATE landing_batches SET closed_object_count=99 WHERE id=?", (bid,)
    )
    conn.commit()
    conn.close()

    r = authed.post(f"/api/batches/{bid}/export", json={})
    assert r.status_code == 409
    assert "对不齐" in r.json()["error"] or "拦截" in r.json()["error"]

    queue = authed.get("/api/exports").json()["items"]
    blocked = [j for j in queue if j["status"] == "已拦截"]
    assert len(blocked) == 1
    assert blocked[0]["target_name"] == ""

    # 拧回去后可以正常导出
    conn = connect(Config.database())
    conn.execute(
        "UPDATE landing_batches SET closed_object_count=2 WHERE id=?", (bid,)
    )
    conn.commit()
    conn.close()
    r2 = authed.post(f"/api/batches/{bid}/export", json={})
    assert r2.status_code == 200


def test_tampered_package_blocks_landing(authed):
    bid = _closed_batch(authed)
    job = authed.post(f"/api/batches/{bid}/export", json={}).json()["job"]

    from pathlib import Path

    pkg = Path(Config.export_dir()) / job["target_name"]
    pkg.write_text(pkg.read_text(encoding="utf-8").replace("hash-a", "hash-tampered"), encoding="utf-8")

    r = authed.post(f"/api/exports/{job['id']}/land", json={})
    assert r.status_code == 409
    assert "指纹" in r.json()["error"]

    # 附页同样拦截
    ap = authed.get(f"/api/exports/{job['id']}/appendix")
    assert ap.status_code == 409

    # 下游没有写入任何脏数据
    conn = connect(Config.database())
    n = conn.execute("SELECT COUNT(*) FROM downstream_objects").fetchone()[0]
    conn.close()
    assert n == 0


def test_drifted_database_blocks_landing(authed):
    bid = _closed_batch(authed)
    job = authed.post(f"/api/batches/{bid}/export", json={}).json()["job"]

    # 导出后库内闭合对象被拧动（指纹漂移），落库必须拦
    conn = connect(Config.database())
    conn.execute(
        "UPDATE landing_objects SET content_hash='hash-drift' "
        "WHERE batch_id=? AND object_key='raw/a.parquet'",
        (bid,),
    )
    conn.commit()
    conn.close()

    r = authed.post(f"/api/exports/{job['id']}/land", json={})
    assert r.status_code == 409
    assert "不一致" in r.json()["error"]

    conn = connect(Config.database())
    n = conn.execute("SELECT COUNT(*) FROM downstream_landings").fetchone()[0]
    conn.close()
    assert n == 0


def test_appendix_unknown_job_404(authed):
    r = authed.get("/api/exports/9999/appendix")
    assert r.status_code == 404
