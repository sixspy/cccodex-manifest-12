import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS landing_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_code TEXT NOT NULL,
  zone_name TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('收集中','已闭合','已作废')),
  closed_object_count INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(zone_code, biz_date)
);

CREATE TABLE IF NOT EXISTS landing_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  object_key TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('已登记','已闭合','已作废')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(batch_id, object_key),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS close_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL UNIQUE,
  object_count INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS visibility_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  closed_batch_count INTEGER NOT NULL,
  open_batch_count INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  orphan_object_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 导出队列：一条导出任务一个凭证；成功产物按 (批次,凭证) 唯一，防双写
CREATE TABLE IF NOT EXISTS export_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  credential TEXT NOT NULL CHECK (credential IN ('manifest_package','snapshot_table')),
  status TEXT NOT NULL CHECK (status IN ('已生成','已拦截','失败')),
  target_name TEXT NOT NULL DEFAULT '',
  package_sha256 TEXT NOT NULL DEFAULT '',
  fingerprint_sha256 TEXT NOT NULL DEFAULT '',
  object_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

-- 每批每凭证最多一份成功导出（失败/拦截记录可多条，便于留痕）
CREATE UNIQUE INDEX IF NOT EXISTS idx_export_jobs_one_success
ON export_jobs(batch_id, credential) WHERE status = '已生成';

-- 下游仓本机落库记录与解析出的对象指纹
CREATE TABLE IF NOT EXISTS downstream_landings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  export_job_id INTEGER NOT NULL UNIQUE,
  target_name TEXT NOT NULL,
  package_sha256 TEXT NOT NULL,
  object_count INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(export_job_id) REFERENCES export_jobs(id)
);

CREATE TABLE IF NOT EXISTS downstream_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  landing_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  object_key TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(landing_id, object_key),
  FOREIGN KEY(landing_id) REFERENCES downstream_landings(id)
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_BATCHES = [
    ("bronze_orders", "订单铜区", "2026-11-14", "收集中"),
    ("bronze_payments", "支付铜区", "2026-11-14", "收集中"),
    ("bronze_events", "事件铜区", "2026-11-13", "收集中"),
    ("silver_orders", "订单银区", "2026-11-12", "已闭合"),
]


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            return
        for zone, name, biz, status in SEED_BATCHES:
            conn.execute(
                "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status, closed_object_count) "
                "VALUES (?,?,?,?,?)",
                (zone, name, biz, status, 2 if status == "已闭合" else None),
            )
        closed = conn.execute(
            "SELECT id FROM landing_batches WHERE status='已闭合'"
        ).fetchone()
        if closed:
            bid = closed["id"]
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,?)",
                (bid, "part-000.parquet", "hash-silver-a", 1024, "已闭合"),
            )
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,?)",
                (bid, "part-001.parquet", "hash-silver-b", 2048, "已闭合"),
            )
            conn.execute(
                "INSERT INTO close_runs(batch_id, object_count, ok, detail) VALUES (?,?,1,?)",
                (bid, 2, "种子闭合"),
            )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('seeded','1')"
        )
