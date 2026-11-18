from pathlib import Path

from itsdangerous import BadSignature, URLSafeSerializer
from litestar import Litestar, Request, Response, get, post
from litestar.exceptions import HTTPException
from litestar.response import Redirect
from litestar.static_files import create_static_files_router
from litestar.status_codes import HTTP_302_FOUND

from app.config import Config
from app.db import init_db
from app.services import (
    ServiceError,
    close_batch,
    get_batch,
    get_current_rail,
    list_batches,
    list_close_runs,
    list_objects,
    list_rails,
    list_reports,
    register_object,
    run_visibility,
    switch_rail,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-manifest-session")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("manifest_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def service_error_handler(_: Request, exc: ServiceError) -> Response:
    # 白话短句统一出口：业务失败都带 ok=False + error
    return Response(
        content={"ok": False, "error": exc.message},
        status_code=exc.status_code,
        media_type="application/json",
    )


def _on_startup() -> None:
    init_db()


@get("/")
async def root(request: Request) -> Redirect:
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return Redirect("/app/", status_code=HTTP_302_FOUND)
    return Redirect("/login", status_code=HTTP_302_FOUND)


@get("/login")
async def login_page() -> Response:
    return Response(
        content=(STATIC / "login.html").read_bytes(),
        media_type="text/html; charset=utf-8",
    )


@get("/app/")
async def spa_page(request: Request) -> Response | Redirect:
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return Redirect("/login", status_code=HTTP_302_FOUND)
    return Response(
        content=(STATIC / "index.html").read_bytes(),
        media_type="text/html; charset=utf-8",
    )


@post("/api/login")
async def api_login(request: Request) -> Response:
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return Response(
            content={"ok": False, "error": "账号或密码错误"},
            status_code=400,
            media_type="application/json",
        )
    resp = Response(
        content={"ok": True, "user": user},
        status_code=200,
        media_type="application/json",
    )
    resp.set_cookie(
        "manifest_session",
        serializer.dumps({"user": user}),
        httponly=True,
        samesite="lax",
    )
    return resp


@post("/api/logout")
async def api_logout() -> Response:
    resp = Response(content={"ok": True}, status_code=200, media_type="application/json")
    resp.delete_cookie("manifest_session")
    return resp


@get("/api/meta")
async def api_meta(request: Request) -> dict:
    require_user(request)
    return {
        "lab_name": Config.LAB_NAME,
        "product_line": "cc-manifest-lab",
        "chain": ["落地批次", "对象登记", "清单闭合", "可见性对账"],
        "rail": get_current_rail(),
    }


@get("/api/rails")
async def api_rails(request: Request) -> dict:
    require_user(request)
    return list_rails()


@post("/api/rails/switch")
async def api_switch_rail(request: Request) -> Response:
    user = require_user(request)
    body = await request.json()
    rail = switch_rail(str(body.get("to_rail", "")), operator=user)
    return Response(
        content={"ok": True, "rail": rail},
        status_code=200,
        media_type="application/json",
    )


@get("/api/batches")
async def api_batches(request: Request, status: str | None = None) -> dict:
    require_user(request)
    return {"items": list_batches(status=status), "rail": get_current_rail()}


@get("/api/batches/{batch_id:int}")
async def api_batch_detail(request: Request, batch_id: int) -> dict:
    require_user(request)
    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return {
        "batch": batch,
        "objects": list_objects(batch_id),
        "rail": get_current_rail(),
    }


@post("/api/batches/{batch_id:int}/objects")
async def api_register(request: Request, batch_id: int) -> Response:
    require_user(request)
    body = await request.json()
    obj = register_object(
        batch_id,
        str(body.get("object_key", "")),
        str(body.get("content_hash", "")),
        int(body.get("byte_size", 0)),
    )
    return Response(
        content={"ok": True, "object": obj},
        status_code=200,
        media_type="application/json",
    )


@post("/api/batches/{batch_id:int}/close")
async def api_close(request: Request, batch_id: int) -> Response:
    require_user(request)
    run = close_batch(batch_id)
    return Response(
        content={"ok": True, "run": run},
        status_code=200,
        media_type="application/json",
    )


@get("/api/close-runs")
async def api_close_runs(request: Request) -> dict:
    require_user(request)
    return {"items": list_close_runs(), "rail": get_current_rail()}


@get("/api/visibility-reports")
async def api_reports(request: Request) -> dict:
    require_user(request)
    return {"items": list_reports()}


@post("/api/visibility-reports")
async def api_run_report(request: Request) -> Response:
    require_user(request)
    body = await request.json()
    report = run_visibility(str(body.get("as_of_date", "2026-11-16")))
    return Response(
        content={"ok": True, "report": report, "rail": get_current_rail()},
        status_code=200,
        media_type="application/json",
    )


app = Litestar(
    route_handlers=[
        root,
        login_page,
        spa_page,
        api_login,
        api_logout,
        api_meta,
        api_rails,
        api_switch_rail,
        api_batches,
        api_batch_detail,
        api_register,
        api_close,
        api_close_runs,
        api_reports,
        api_run_report,
        create_static_files_router(path="/static", directories=[str(STATIC)]),
    ],
    on_startup=[_on_startup],
    exception_handlers={ServiceError: service_error_handler},
)
