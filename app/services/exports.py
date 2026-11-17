"""闭合清单导出服务。

凭证定边：只启用「物化不可变清单包」(manifest_package)。
清单包在生成瞬间冻结指纹集合，下游解析落库只认包内指纹与包 sha256；
不启用独立快照表（snapshot_table 请求一律拒绝）。
"""
import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import Config
from app.db import get_db
from app.services.objects import ServiceError

ENABLED_CREDENTIAL = "manifest_package"

# 同批导出串行化：配合 BEGIN IMMEDIATE 与部分唯一索引，杜绝并发双写
_batch_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _batch_lock(batch_id: int) -> threading.Lock:
    with _locks_guard:
        lock = _batch_locks.get(batch_id)
        if lock is None:
            lock = threading.Lock()
            _batch_locks[batch_id] = lock
        return lock


def _export_dir(export_dir: str | None) -> Path:
    path = Path(export_dir or Config.export_dir())
    path.mkdir(parents=True, exist_ok=True)
    return path


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_failure(batch_id: int, target: str, error: str, db_path: str | None) -> None:
    try:
        with get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO export_jobs(batch_id, credential, status, target_name, error) "
                "VALUES (?,?, '失败', ?,?)",
                (batch_id, ENABLED_CREDENTIAL, target, error),
            )
    except sqlite3.Error:
        pass


def create_export(
    batch_id: int,
    credential: str = ENABLED_CREDENTIAL,
    db_path: str | None = None,
    export_dir: str | None = None,
) -> dict:
    """按凭证一次生成不可变清单包；任何对不齐都拦截并在队列留痕。"""
    if credential != ENABLED_CREDENTIAL:
        raise ServiceError("仅支持不可变清单包凭证，快照表凭证未启用", 400)

    lock = _batch_lock(batch_id)
    if not lock.acquire(blocking=False):
        raise ServiceError("该批次正在导出，请勿重复点击", 409)

    target_name = ""
    try:
        try:
            with get_db(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                batch = conn.execute(
                    "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
                ).fetchone()
                if not batch:
                    raise ServiceError("批次不存在", 404)
                if batch["status"] != "已闭合":
                    raise ServiceError("仅已闭合批次可导出", 409)

                duplicate = conn.execute(
                    "SELECT id FROM export_jobs WHERE batch_id=? AND credential=? "
                    "AND status='已生成'",
                    (batch_id, ENABLED_CREDENTIAL),
                ).fetchone()
                if duplicate:
                    raise ServiceError("该批次已存在导出产物", 409)

                rows = conn.execute(
                    "SELECT object_key, content_hash, byte_size FROM landing_objects "
                    "WHERE batch_id=? AND status='已闭合' ORDER BY object_key",
                    (batch_id,),
                ).fetchall()
                objects = [
                    {
                        "object_key": r["object_key"],
                        "content_hash": r["content_hash"],
                        "byte_size": r["byte_size"],
                    }
                    for r in rows
                ]

                # 一致性拦截：导出物必须与库内闭合对象完全对齐
                blocked = ""
                if not objects:
                    blocked = "闭合对象为空，拒绝导出"
                elif batch["closed_object_count"] is not None and len(objects) != batch["closed_object_count"]:
                    blocked = "库内闭合对象数与固化数对不齐，已拦截"
                else:
                    stray = conn.execute(
                        "SELECT COUNT(*) AS c FROM landing_objects "
                        "WHERE batch_id=? AND status!='已闭合'",
                        (batch_id,),
                    ).fetchone()["c"]
                    if stray:
                        blocked = "批内存在未闭合对象，指纹集合对不齐，已拦截"
                if blocked:
                    conn.execute(
                        "INSERT INTO export_jobs(batch_id, credential, status, target_name, "
                        "object_count, error) VALUES (?,?, '已拦截', ?,?,?)",
                        (batch_id, ENABLED_CREDENTIAL, "", len(objects), blocked),
                    )
                    # 拦截留痕须先落库；随后抛错触发的 rollback 将是空操作
                    conn.commit()
                    raise ServiceError(blocked, 409)

                fingerprint_sha = _sha256(_canonical(objects))
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                target_name = f"manifest_batch_{batch_id}_{stamp}.json"
                payload = {
                    "manifest_version": 1,
                    "credential": ENABLED_CREDENTIAL,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "fingerprint_sha256": fingerprint_sha,
                    "object_count": len(objects),
                    "batch": {
                        "id": batch["id"],
                        "zone_code": batch["zone_code"],
                        "zone_name": batch["zone_name"],
                        "biz_date": batch["biz_date"],
                        "closed_object_count": batch["closed_object_count"],
                    },
                    "objects": objects,
                }
                blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                package_sha = _sha256(blob)

                package_path = _export_dir(export_dir) / target_name
                try:
                    # O_EXCL：同名包绝不覆盖，防双写/防篡改重放
                    fd = os.open(package_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                except FileExistsError:
                    raise ServiceError("清单包名已被占用，拒绝覆盖", 409)
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(blob)
                except OSError:
                    package_path.unlink(missing_ok=True)
                    raise

                try:
                    cur = conn.execute(
                        "INSERT INTO export_jobs(batch_id, credential, status, target_name, "
                        "package_sha256, fingerprint_sha256, object_count) "
                        "VALUES (?,?, '已生成', ?,?,?,?)",
                        (
                            batch_id,
                            ENABLED_CREDENTIAL,
                            target_name,
                            package_sha,
                            fingerprint_sha,
                            len(objects),
                        ),
                    )
                except sqlite3.IntegrityError:
                    package_path.unlink(missing_ok=True)
                    raise ServiceError("该批次已存在导出产物", 409)

                job = conn.execute(
                    "SELECT * FROM export_jobs WHERE id=?", (cur.lastrowid,)
                ).fetchone()
                return dict(job)
        except ServiceError:
            raise
        except Exception as exc:  # 兜底：事务回滚后再写失败队列，并吐短句
            _record_failure(
                batch_id, target_name, f"导出失败：{type(exc).__name__}", db_path
            )
            raise ServiceError("导出失败，请稍后重试", 500)
    finally:
        lock.release()


def list_exports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM export_jobs ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_export(job_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM export_jobs WHERE id=?", (job_id,)
        ).fetchone()
        return dict(row) if row else None


def get_success_export(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM export_jobs WHERE batch_id=? AND status='已生成' "
            "ORDER BY id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        return dict(row) if row else None


def _read_package(job: dict, export_dir: str | None) -> tuple[dict, bytes]:
    path = _export_dir(export_dir) / job["target_name"]
    if not path.exists():
        raise ServiceError("清单包文件缺失，拒绝读取", 409)
    blob = path.read_bytes()
    if _sha256(blob) != job["package_sha256"]:
        raise ServiceError("清单包指纹与队列记录不一致，已拦截", 409)
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ServiceError("清单包解析失败，已拦截", 409)
    return payload, blob


def get_appendix(
    job_id: int, db_path: str | None = None, export_dir: str | None = None
) -> dict:
    """指纹附页：读不可变包回算指纹，附页与包对不齐即拦。"""
    job = get_export(job_id, db_path)
    if not job:
        raise ServiceError("导出任务不存在", 404)
    if job["status"] != "已生成":
        return {"job": job, "verified": False, "objects": [], "error": job["error"]}
    payload, _ = _read_package(job, export_dir)
    objects = payload.get("objects", [])
    recomputed = _sha256(_canonical(objects))
    if recomputed != job["fingerprint_sha256"] or recomputed != payload.get("fingerprint_sha256"):
        raise ServiceError("附页指纹与清单包对不齐，已拦截", 409)
    return {
        "job": job,
        "verified": True,
        "batch": payload.get("batch", {}),
        "generated_at": payload.get("generated_at", ""),
        "objects": objects,
    }


def land_export(
    job_id: int, db_path: str | None = None, export_dir: str | None = None
) -> dict:
    """下游仓拉取清单包：验包 sha → 解析 → 与库内闭合对象核对 → 落本机库。"""
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT * FROM export_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ServiceError("导出任务不存在", 404)
        if job["status"] != "已生成":
            raise ServiceError("仅已生成的清单可落库", 409)

        landed = conn.execute(
            "SELECT id FROM downstream_landings WHERE export_job_id=?", (job_id,)
        ).fetchone()
        if landed:
            raise ServiceError("该清单已落库，不可重复解析", 409)

        # 读包在事务内完成，包被拧动直接拦，不落一条下游数据
        payload, blob = _read_package(dict(job), export_dir)
        if _sha256(blob) != job["package_sha256"]:
            raise ServiceError("清单包指纹校验失败，拒绝落库", 409)

        objects = payload.get("objects", [])
        if _sha256(_canonical(objects)) != job["fingerprint_sha256"]:
            raise ServiceError("包内指纹集与导出记录对不齐，已拦截", 409)

        live_rows = conn.execute(
            "SELECT object_key, content_hash, byte_size FROM landing_objects "
            "WHERE batch_id=? AND status='已闭合' ORDER BY object_key",
            (job["batch_id"],),
        ).fetchall()
        live = {(r["object_key"], r["content_hash"], r["byte_size"]) for r in live_rows}
        packed = {(o["object_key"], o["content_hash"], o["byte_size"]) for o in objects}
        if live != packed:
            raise ServiceError("导出物与库内闭合对象不一致，已拦截", 409)

        cur = conn.execute(
            "INSERT INTO downstream_landings(export_job_id, target_name, "
            "package_sha256, object_count) VALUES (?,?,?,?)",
            (job_id, job["target_name"], job["package_sha256"], len(objects)),
        )
        landing_id = cur.lastrowid
        for o in objects:
            conn.execute(
                "INSERT INTO downstream_objects(landing_id, batch_id, object_key, "
                "content_hash, byte_size) VALUES (?,?,?,?,?)",
                (
                    landing_id,
                    job["batch_id"],
                    o["object_key"],
                    o["content_hash"],
                    o["byte_size"],
                ),
            )
        landing = conn.execute(
            "SELECT * FROM downstream_landings WHERE id=?", (landing_id,)
        ).fetchone()
        result = dict(landing)
        result["objects"] = [dict(o) for o in objects]
        return result


def list_landings(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT l.*, j.batch_id FROM downstream_landings l "
            "JOIN export_jobs j ON j.id = l.export_job_id ORDER BY l.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
