import threading

from app.db import get_db
from app.services.errors import ServiceError

# 进程内排他：两侧值班同时点切换时先串行化；
# 库级再由 BEGIN IMMEDIATE 兜底，杜绝双写现行轨。
_SWITCH_LOCK = threading.Lock()


def current_rail_in(conn) -> dict:
    """在已打开的连接上读现行轨（轨号 ↔ 门槛同源）。

    双开即停：指针不是唯一一行、或指向不存在的轨，直接报错停读写。
    """
    n = conn.execute("SELECT COUNT(*) AS c FROM rail_pointer").fetchone()["c"]
    if n != 1:
        raise ServiceError("现行轨指针不是唯一一行，已停止读写", 409)
    row = conn.execute(
        "SELECT p.current_rail AS rail_code, p.updated_at, p.updated_by, "
        "r.rule_version, r.min_byte_size "
        "FROM rail_pointer p JOIN rule_rails r ON r.rail_code = p.current_rail "
        "WHERE p.id = 1"
    ).fetchone()
    if not row:
        raise ServiceError("现行轨指向的规则轨不存在，已停止读写", 409)
    return dict(row)


def get_current_rail(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        return current_rail_in(conn)


def list_rails(db_path: str | None = None) -> dict:
    """规则队列视图：全部轨 + 唯一现行 + 最近切换登记（含失败因）。"""
    with get_db(db_path) as conn:
        current = current_rail_in(conn)
        rows = conn.execute(
            "SELECT * FROM rule_rails ORDER BY rail_code"
        ).fetchall()
        rails = []
        for r in rows:
            d = dict(r)
            d["is_current"] = 1 if d["rail_code"] == current["rail_code"] else 0
            rails.append(d)
        log = [
            dict(x)
            for x in conn.execute(
                "SELECT * FROM rail_switch_log ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
        return {"current": current, "rails": rails, "log": log}


def switch_rail(to_rail: str, operator: str = "", db_path: str | None = None) -> dict:
    """点切换：服务端按旁路表凭证一次事务落成（改指针 + 登记同库同事务）。

    失败也登记失败因并提交，再向调用方吐白话短句。
    """
    target = (to_rail or "").strip()
    operator = (operator or "").strip() or "unknown"
    with _SWITCH_LOCK:
        with get_db(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = current_rail_in(conn)
            fail = None
            status = 400
            if not target:
                fail = "目标轨不能为空"
            elif target == current["rail_code"]:
                fail = f"目标轨 {target} 已是现行轨，无需切换"
                status = 409
            elif not conn.execute(
                "SELECT 1 FROM rule_rails WHERE rail_code=?", (target,)
            ).fetchone():
                fail = f"目标轨 {target} 不存在"
            if fail:
                conn.execute(
                    "INSERT INTO rail_switch_log(from_rail, to_rail, ok, reason, operator) "
                    "VALUES (?,?,0,?,?)",
                    (current["rail_code"], target, fail, operator),
                )
                outcome = (fail, status)
            else:
                conn.execute(
                    "UPDATE rail_pointer SET current_rail=?, "
                    "updated_at=datetime('now'), updated_by=? WHERE id=1",
                    (target, operator),
                )
                conn.execute(
                    "INSERT INTO rail_switch_log(from_rail, to_rail, ok, reason, operator) "
                    "VALUES (?,?,1,?,?)",
                    (
                        current["rail_code"],
                        target,
                        f"现行轨由 {current['rail_code']} 切换为 {target}",
                        operator,
                    ),
                )
                outcome = None
        # with 块正常退出即提交：改指针与登记同事务，失败登记也落库
    if outcome:
        raise ServiceError(outcome[0], outcome[1])
    return get_current_rail(db_path)
