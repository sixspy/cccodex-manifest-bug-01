import os
import sqlite3
import threading

from app.db import init_db
from app.services.errors import ServiceError
from app.services.rails import get_current_rail, list_rails, switch_rail


def _db_path() -> str:
    return os.environ["DATABASE_PATH"]


def _first_open_batch_id(authed) -> int:
    items = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    return items[0]["id"]


def test_meta_reports_single_current_rail(authed):
    rail = authed.get("/api/meta").json()["rail"]
    assert rail["rail_code"] == "rail_a"
    assert rail["rule_version"] == "v1"
    assert rail["min_byte_size"] == 0
    # 不再吐双轨字段
    assert "list_rail" not in rail and "register_rail" not in rail


def test_no_dual_credential_left_in_meta(authed):
    conn = sqlite3.connect(_db_path())
    try:
        keys = {r[0] for r in conn.execute("SELECT key FROM meta")}
        assert "rail_current" not in keys
        assert "rail_current_alt" not in keys
        rows = conn.execute("SELECT current_rail FROM rail_pointer").fetchall()
        assert len(rows) == 1 and rows[0][0] == "rail_a"
    finally:
        conn.close()


def test_four_surfaces_read_same_rail(authed):
    bid = _first_open_batch_id(authed)
    rails = {
        authed.get("/api/meta").json()["rail"]["rail_code"],
        authed.get("/api/batches").json()["rail"]["rail_code"],
        authed.get(f"/api/batches/{bid}").json()["rail"]["rail_code"],
        authed.get("/api/rails").json()["current"]["rail_code"],
        authed.get("/api/close-runs").json()["rail"]["rail_code"],
    }
    assert rails == {"rail_a"}


def test_switch_flips_all_surfaces_atomically(authed):
    r = authed.post("/api/rails/switch", json={"to_rail": "rail_b"})
    assert r.status_code == 200
    assert r.json()["rail"]["rail_code"] == "rail_b"
    assert r.json()["rail"]["rule_version"] == "v2"

    bid = _first_open_batch_id(authed)
    rails = {
        authed.get("/api/meta").json()["rail"]["rail_code"],
        authed.get("/api/batches").json()["rail"]["rail_code"],
        authed.get(f"/api/batches/{bid}").json()["rail"]["rail_code"],
        authed.get("/api/rails").json()["current"]["rail_code"],
        authed.get("/api/close-runs").json()["rail"]["rail_code"],
    }
    assert rails == {"rail_b"}

    log = authed.get("/api/rails").json()["log"]
    assert log[0]["from_rail"] == "rail_a"
    assert log[0]["to_rail"] == "rail_b"
    assert log[0]["ok"] == 1
    assert log[0]["operator"] == "admin"


def test_switch_unknown_rail_plain_error_and_logged(authed):
    r = authed.post("/api/rails/switch", json={"to_rail": "rail_z"})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert "不存在" in r.json()["error"]

    data = authed.get("/api/rails").json()
    assert data["current"]["rail_code"] == "rail_a"  # 现行轨未动
    assert data["log"][0]["ok"] == 0
    assert "不存在" in data["log"][0]["reason"]


def test_switch_to_current_rail_rejected(authed):
    r = authed.post("/api/rails/switch", json={"to_rail": "rail_a"})
    assert r.status_code == 409
    assert "已是现行轨" in r.json()["error"]
    log = authed.get("/api/rails").json()["log"]
    assert log[0]["ok"] == 0


def test_threshold_follows_current_rail(authed):
    bid = _first_open_batch_id(authed)
    ok = authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/small.parquet", "content_hash": "h1", "byte_size": 10},
    )
    assert ok.status_code == 200  # rail_a 门槛 ≥0

    authed.post("/api/rails/switch", json={"to_rail": "rail_b"})
    low = authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/tiny.parquet", "content_hash": "h2", "byte_size": 10},
    )
    assert low.status_code == 400
    assert "未过门槛" in low.json()["error"]
    assert "rail_b" in low.json()["error"]

    high = authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/big.parquet", "content_hash": "h3", "byte_size": 150},
    )
    assert high.status_code == 200  # rail_b 门槛 ≥100


def test_close_run_stamped_and_replay_matches(authed):
    bid = _first_open_batch_id(authed)
    authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/a.parquet", "content_hash": "ha", "byte_size": 10},
    )
    close = authed.post(f"/api/batches/{bid}/close", json={})
    assert close.status_code == 200
    assert close.json()["run"]["rail_code"] == "rail_a"
    assert close.json()["run"]["rule_version"] == "v1"

    authed.post("/api/rails/switch", json={"to_rail": "rail_b"})
    bid2 = _first_open_batch_id(authed)
    authed.post(
        f"/api/batches/{bid2}/objects",
        json={"object_key": "raw/b.parquet", "content_hash": "hb", "byte_size": 200},
    )
    close2 = authed.post(f"/api/batches/{bid2}/close", json={})
    assert close2.json()["run"]["rail_code"] == "rail_b"

    replay = authed.get("/api/close-runs").json()
    # 回放台读到的现行轨与规则队列一致
    assert replay["rail"]["rail_code"] == authed.get("/api/rails").json()["current"]["rail_code"]
    by_batch = {r["batch_id"]: r for r in replay["items"]}
    assert by_batch[bid]["rail_code"] == "rail_a"
    assert by_batch[bid]["min_byte_size"] == 0
    assert by_batch[bid2]["rail_code"] == "rail_b"
    assert by_batch[bid2]["min_byte_size"] == 100


def test_concurrent_switches_never_double_write(authed):
    db = _db_path()
    errors = []

    def work(target):
        for _ in range(5):
            try:
                switch_rail(target, operator="值班", db_path=db)
            except ServiceError:
                pass  # 业务拒绝（如已是现行轨）不影响一致性
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=work, args=(t,)) for t in ("rail_a", "rail_b")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors
    data = list_rails(db)
    ok_log = [l for l in data["log"] if l["ok"] == 1]
    # 现行轨与最新一条成功登记一致
    assert data["current"]["rail_code"] == ok_log[0]["to_rail"]
    # 成功登记链严格首尾相接：排他生效，无双写撕裂
    chain = list(reversed(ok_log))
    for prev, nxt in zip(chain, chain[1:]):
        assert nxt["from_rail"] == prev["to_rail"]


def test_dual_pointer_stops_reads_and_writes(authed):
    conn = sqlite3.connect(_db_path())
    conn.execute("DELETE FROM rail_pointer")
    conn.commit()
    conn.close()

    r = authed.get("/api/meta")
    assert r.status_code == 409
    assert "现行轨" in r.json()["error"]
    assert authed.get("/api/rails").status_code == 409
    assert authed.get("/api/batches").status_code == 409

    reg = authed.post(
        "/api/batches/1/objects",
        json={"object_key": "raw/x.parquet", "content_hash": "hx", "byte_size": 1},
    )
    assert reg.status_code == 409
    assert "现行轨" in reg.json()["error"]


def test_migration_merges_legacy_dual_keys(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('seeded','1')")
    conn.execute("INSERT INTO meta VALUES ('rail_current','rail_a')")
    conn.execute("INSERT INTO meta VALUES ('rail_current_alt','rail_b')")
    conn.commit()
    conn.close()

    init_db(str(db))

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        keys = {r["key"] for r in conn.execute("SELECT key FROM meta")}
        assert "rail_current" not in keys
        assert "rail_current_alt" not in keys
        rows = conn.execute("SELECT * FROM rail_pointer").fetchall()
        assert len(rows) == 1
        assert rows[0]["current_rail"] == "rail_a"
        logs = conn.execute("SELECT * FROM rail_switch_log").fetchall()
        assert any("双轨凭证合并" in l["reason"] for l in logs)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(close_runs)")}
        assert {"rail_code", "rule_version", "min_byte_size"} <= cols
    finally:
        conn.close()
