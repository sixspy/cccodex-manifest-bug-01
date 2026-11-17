from app.db import get_db


def _row(r) -> dict:
    return dict(r) if r is not None else {}


def list_batches(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM landing_batches WHERE status=? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM landing_batches ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return _row(row) or None


def get_current_rail(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        # silent: two actives; list path prefers rail_current, register prefers alt
        a = conn.execute("SELECT value FROM meta WHERE key='rail_current'").fetchone()
        b = conn.execute("SELECT value FROM meta WHERE key='rail_current_alt'").fetchone()
        return {"list_rail": a["value"] if a else "", "register_rail": b["value"] if b else ""}

