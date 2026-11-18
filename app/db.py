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
    conn.execute("PRAGMA busy_timeout = 5000")
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
  rail_code TEXT NOT NULL DEFAULT '',
  rule_version TEXT NOT NULL DEFAULT '',
  min_byte_size INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 规则轨目录：轨号与门槛同源，门槛只从这里读
CREATE TABLE IF NOT EXISTS rule_rails (
  rail_code TEXT PRIMARY KEY,
  rule_version TEXT NOT NULL,
  min_byte_size INTEGER NOT NULL DEFAULT 0
);

-- 现行轨唯一凭证：旁路表单行指针（CHECK 强制只有一行），切换即整行换完
CREATE TABLE IF NOT EXISTS rail_pointer (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  current_rail TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_by TEXT NOT NULL DEFAULT 'system',
  FOREIGN KEY(current_rail) REFERENCES rule_rails(rail_code)
);

-- 切换登记：成功与失败都留痕，失败因可回查
CREATE TABLE IF NOT EXISTS rail_switch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_rail TEXT NOT NULL DEFAULT '',
  to_rail TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SEED_BATCHES = [
    ("bronze_orders", "订单铜区", "2026-11-14", "收集中"),
    ("bronze_payments", "支付铜区", "2026-11-14", "收集中"),
    ("bronze_events", "事件铜区", "2026-11-13", "收集中"),
    ("silver_orders", "订单银区", "2026-11-12", "已闭合"),
]

# 规则轨目录种子：轨号 ↔ 规则版本 ↔ 灌库门槛（最小字节数）一一对应
SEED_RAILS = [
    ("rail_a", "v1", 0),
    ("rail_b", "v2", 100),
]

# 旧版双轨凭证（meta 双键并存即“双开现行”的病灶），迁移后必须清除
LEGACY_RAIL_KEYS = ("rail_current", "rail_current_alt")


def _migrate_close_runs_columns(conn) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(close_runs)").fetchall()}
    if "rail_code" not in cols:
        conn.execute("ALTER TABLE close_runs ADD COLUMN rail_code TEXT NOT NULL DEFAULT ''")
    if "rule_version" not in cols:
        conn.execute("ALTER TABLE close_runs ADD COLUMN rule_version TEXT NOT NULL DEFAULT ''")
    if "min_byte_size" not in cols:
        conn.execute(
            "ALTER TABLE close_runs ADD COLUMN min_byte_size INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_rail_pointer(conn) -> None:
    """现行轨凭证全局只留一种：旁路表单行指针。

    - 旧 meta 双键（rail_current / rail_current_alt）无论是否存在，一律作废删除；
    - 指针不存在时落成一次：旧库以 rail_current 为现行并登记，新库默认 rail_a；
    - 幂等，可重复执行。
    """
    legacy = {}
    for key in LEGACY_RAIL_KEYS:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row:
            legacy[key] = row["value"]

    pointer = conn.execute("SELECT current_rail FROM rail_pointer WHERE id=1").fetchone()
    if pointer is None:
        survivor = legacy.get("rail_current") or "rail_a"
        valid = conn.execute(
            "SELECT 1 FROM rule_rails WHERE rail_code=?", (survivor,)
        ).fetchone()
        if not valid:
            survivor = "rail_a"
        conn.execute(
            "INSERT INTO rail_pointer(id, current_rail, updated_by) VALUES (1, ?, 'system')",
            (survivor,),
        )
        if legacy:
            reason = (
                f"双轨凭证合并：meta 双键（{'+'.join(sorted(legacy))}）作废，"
                f"旁路表落成，现行轨登记为 {survivor}"
            )
            from_rail = ",".join(f"{k}={v}" for k, v in sorted(legacy.items()))
        else:
            reason = f"初始化现行轨为 {survivor}"
            from_rail = ""
        conn.execute(
            "INSERT INTO rail_switch_log(from_rail, to_rail, ok, reason, operator) "
            "VALUES (?,?,1,?,'system')",
            (from_rail, survivor, reason),
        )

    # 两种凭证不并存：旁路表已是唯一凭证，meta 旧键一律清除
    for key in LEGACY_RAIL_KEYS:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate_close_runs_columns(conn)
        for rail_code, rule_version, min_byte_size in SEED_RAILS:
            conn.execute(
                "INSERT OR IGNORE INTO rule_rails(rail_code, rule_version, min_byte_size) "
                "VALUES (?,?,?)",
                (rail_code, rule_version, min_byte_size),
            )
        _migrate_rail_pointer(conn)
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
                "INSERT INTO close_runs(batch_id, object_count, ok, detail, rail_code, rule_version, min_byte_size) "
                "VALUES (?,?,1,?,?,?,?)",
                (bid, 2, "种子闭合", "rail_a", "v1", 0),
            )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
