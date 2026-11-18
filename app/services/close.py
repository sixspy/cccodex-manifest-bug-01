from app.db import get_db
from app.services.errors import ServiceError
from app.services.rails import current_rail_in


def close_batch(batch_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        # 清洗任务按唯一现行轨执行并留痕，回放台据此对账
        rail = current_rail_in(conn)
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已闭合":
            raise ServiceError("批次已闭合", 409)
        if batch["status"] != "收集中":
            raise ServiceError("仅收集中批次可闭合", 409)
        objs = conn.execute(
            "SELECT id FROM landing_objects WHERE batch_id=? AND status='已登记'",
            (batch_id,),
        ).fetchall()
        if not objs:
            raise ServiceError("空批不可闭合", 400)
        n = len(objs)
        conn.execute(
            "UPDATE landing_batches SET status='已闭合', closed_object_count=? WHERE id=?",
            (n, batch_id),
        )
        conn.execute(
            "UPDATE landing_objects SET status='已闭合' WHERE batch_id=? AND status='已登记'",
            (batch_id,),
        )
        conn.execute(
            "INSERT INTO close_runs(batch_id, object_count, ok, detail, rail_code, rule_version, min_byte_size) "
            "VALUES (?,?,1,?,?,?,?)",
            (
                batch_id,
                n,
                f"闭合 {n} 个对象",
                rail["rail_code"],
                rail["rule_version"],
                rail["min_byte_size"],
            ),
        )
        run = conn.execute(
            "SELECT * FROM close_runs WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return dict(run)


def list_close_runs(db_path: str | None = None) -> list[dict]:
    """清洗回放台数据：每次闭合跑在哪条轨上，逐条可回查。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT r.*, b.zone_code, b.zone_name, b.biz_date "
            "FROM close_runs r JOIN landing_batches b ON b.id = r.batch_id "
            "ORDER BY r.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
