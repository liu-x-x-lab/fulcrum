"""应用装配入口：注册领域路由、恢复请求身份并管理进程生命周期。

页面业务放在 app.web，各领域不得反向依赖本模块，避免导入时形成环。"""

import time as time_module

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Match

from app.api import (
    channel_inbound_router,
    channel_outbound_router,
    customer_operations_router,
    guga_agent_router,
    open_api_router,
    open_media_router,
    open_messages_router,
    open_resources_router,
    payment_webhook_router,
    provider_router,
    tool_api_router,
    wechat_webridge_router,
    workwechat_provider_cloud_router,
)
from app.api.channel_webhooks import router as channel_webhooks_router
from app.api.local_proxy_connector import router as local_proxy_connector_router
from app.core.application import APP_STARTED_AT
from app.core.db import SessionLocal
from app.core.logging import configure_logging, get_logger
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.services.async_workers import start_background_workers, stop_background_workers
from app.services.init_db import database_readiness, init_db_with_seed
from app.services.installer import is_setup_required
from app.services.local_proxy_relay import local_proxy_relay_server
from app.services.request_metrics import record_request
from app.services.saas_auth import load_platform_principal, load_tenant_principal
from app.services.tenant_admin import TenantPolicyError, require_tenant_mutation_allowed
from app.web import (
    billing_router,
    developer_docs_router,
    group_triggers_router,
    guess_image_router,
    open_api_apps_router,
    platform_admin_router,
    platform_auth_router,
    platform_billing_router,
    platform_operations_router,
    platform_plugins_router,
    provider_nodes_router,
    public_site_router,
    tenant_admin_router,
    tenant_auth_router,
)
from app.web.auth_common import (
    PLATFORM_LOGIN_PATH,
    PLATFORM_SESSION_COOKIE,
    TENANT_LOGIN_PATH,
    TENANT_LOGOUT_PATH,
    TENANT_PASSWORD_PATH,
    TENANT_SESSION_COOKIE,
    platform_must_change_password_redirect,
    tenant_must_change_password_redirect,
)
from app.web.bots import router as bots_router
from app.web.channels.accounts import router as channels_accounts_router
from app.web.channels.actions import router as channels_actions_router
from app.web.channels.connections import router as channels_connections_router
from app.web.channels.ipad_login import router as channels_ipad_login_router
from app.web.channels.webridge import router as channels_webridge_router
from app.web.channels.workwechat_contacts import router as channels_workwechat_contacts_router
from app.web.chatrooms.directory import router as chatrooms_directory_router
from app.web.chatrooms.management import router as chatrooms_management_router
from app.web.chatrooms.plugin_settings import router as chatrooms_plugin_settings_router
from app.web.contact_check import router as contact_check_router
from app.web.dashboard import router as dashboard_router
from app.web.messages.moments import router as messages_moments_router
from app.web.messages.routes import router as messages_routes_router
from app.web.plugins.routes import router as plugins_routes_router
from app.web.plugins.maibot_connections import router as maibot_connections_router
from app.web.points import router as points_router
from app.web.setup import router as setup_router
from app.web.shared.access import get_platform_session


configure_logging()
logger = get_logger(__name__)


# 应用元数据统一从 Settings 读取，部署时可以覆盖名称，但默认明确标识 SaaS 服务。
app = FastAPI(title=settings.app_name, version="1.0.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(tool_api_router)
app.include_router(channel_outbound_router)
app.include_router(channel_inbound_router)
app.include_router(customer_operations_router)
app.include_router(open_api_router)
app.include_router(open_resources_router)
app.include_router(open_messages_router)
app.include_router(open_media_router)
app.include_router(payment_webhook_router)
app.include_router(workwechat_provider_cloud_router)
app.include_router(wechat_webridge_router)
app.include_router(contact_check_router)
app.include_router(guga_agent_router)
app.include_router(local_proxy_connector_router)
app.include_router(provider_router)


# 平台和租户认证使用独立 Router 与 Cookie，主应用只负责统一注册和请求身份恢复。
app.include_router(public_site_router)
app.include_router(developer_docs_router)
app.include_router(group_triggers_router)
app.include_router(guess_image_router)
app.include_router(tenant_auth_router)
app.include_router(platform_auth_router)
app.include_router(billing_router)
app.include_router(platform_admin_router)
app.include_router(platform_billing_router)
app.include_router(platform_operations_router)
app.include_router(platform_plugins_router)
app.include_router(tenant_admin_router)
app.include_router(open_api_apps_router)
app.include_router(provider_nodes_router)
logger.info("application module initialized", extra={"app_env": settings.app_env, "log_dir": settings.log_dir})


# 机器接口通过自身签名或一次性令牌完成鉴权，不能被租户页面 Cookie 中间件重定向。
# 将公开前缀集中定义，便于支付回调、开放 API 和 Provider 接口共同维护边界。
PUBLIC_MACHINE_PREFIXES = (
    "/api/open/",
    # 工具接口自行校验全局工具密钥和有效租户；截图等机器请求不能要求网页登录 Cookie。
    "/api/tool/",
    "/api/payment/",
    "/api/workwechat/customer-operations",
    "/api/workwechat/provider",
    "/api/provider",
    "/api/wechat-webridge",
    "/api/guga-agent",
    "/outbound-files/",
    "/inbound-files/",
    "/webhooks/",
    "/static/",
)


def log_startup_banner() -> None:
    # 在 INFO 级别输出显眼的系统启动完成标识，便于从日志中快速定位启动成功时间点。
    banner_lines = [
        " __  __ _   _ _   _____ ___    ____   ___ _____     _    ____  __  __ ___ _   _ ",
        "|  \\/  | | | | | |_   _|_ _|  | __ ) / _ \\_   _|   / \\  |  _ \\|  \\/  |_ _| \\ | |",
        "| |\\/| | | | | |   | |  | |   |  _ \\| | | || |    / _ \\ | | | | |\\/| || ||  \\| |",
        "| |  | | |_| | |___| |  | |   | |_) | |_| || |   / ___ \\| |_| | |  | || || |\\  |",
        "|_|  |_|\\___/|_____|_| |___|  |____/ \\___/ |_|  /_/   \\_\\____/|_|  |_|___|_| \\_|",
    ]
    logger.info("========== MULTI BOT SAAS STARTUP COMPLETE ==========")
    for line in banner_lines:
        logger.info(line)
    logger.info(
        "MULTI BOT SAAS 已启动并完成初始化",
        extra={
            "app_env": settings.app_env,
            "started_at": APP_STARTED_AT.isoformat(),
        },
    )
    logger.info("======================================================")


@app.on_event("startup")
async def startup_event() -> None:
    db = SessionLocal()
    try:
        init_db_with_seed(db)
    except Exception:
        # 数据库未迁移到 head 时应用仍保留 liveness/readiness 探针，但绝不启动 Worker。
        logger.exception("application started in database-not-ready mode")
        return
    finally:
        db.close()
    start_background_workers()
    await local_proxy_relay_server.start()
    log_startup_banner()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await local_proxy_relay_server.stop()
    stop_background_workers()


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    started_at = time_module.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time_module.perf_counter() - started_at) * 1000, 2)
        logger.exception(
            "request failed",
            extra={"method": request.method, "path": request.url.path, "duration_ms": duration_ms},
        )
        raise

    duration_ms = round((time_module.perf_counter() - started_at) * 1000, 2)
    # 平台后台的响应性能统计:用路由模板聚合,静态资源不记。
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if route_path and not request.url.path.startswith("/static/"):
        record_request(f"{request.method} {route_path}", response.status_code, duration_ms)
    logger.info(
        "request completed",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """恢复平台与租户数据库会话，并按 URL 空间选择唯一可信身份。"""

    path = request.url.path
    # 存活探针、就绪探针和静态资源不能依赖业务表查询；数据库落后时运维仍需
    # 能读取 readyz 原因，并正常加载反向代理的静态错误页。
    if path in {"/healthz", "/readyz"} or path.startswith("/static/"):
        return await call_next(request)
    readiness = database_readiness()
    if not readiness["ready"]:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "ready": False,
                "error": readiness["error"],
                "expected_revision": readiness["expected_revision"],
            },
        )

    # 云端企微 Provider 接口是桌面客户端的机器到机器入口，不能依赖后台登录 Cookie。
    # 这些接口内部会分别校验一次性授权码或 client token，因此这里只放行到业务路由。
    # Open API、支付回调、Provider 和受控文件入口均使用自己的鉴权边界，
    # 不能因为未携带后台 Cookie 被重定向到网页登录页。
    public_prefixes = PUBLIC_MACHINE_PREFIXES
    public_paths = {
        "/",
        "/pricing",
        "/register",
        "/healthz",
        "/readyz",
        "/setup",
        "/setup/completed",
        TENANT_LOGIN_PATH,
        PLATFORM_LOGIN_PATH,
        "/auth/captcha",
    }
    # 认证中间件只保护应用真实注册的页面和接口。完全未注册的路径直接交给
    # FastAPI 生成原生 404；仅方法不匹配的路由仍继续处理，以保留标准 405 响应。
    has_registered_route = any(
        route.matches(request.scope)[0] is not Match.NONE
        for route in request.app.routes
    )
    if not has_registered_route:
        return await call_next(request)
    db = get_platform_session()
    try:
        # 两类会话始终分别恢复。租户业务只读取 current_user，平台路径只读取
        # platform_principal，任何一方都不能通过同名账号或另一类 Cookie 自动升级身份。
        request.state.current_user = load_tenant_principal(db, request.cookies.get(TENANT_SESSION_COOKIE))
        request.state.platform_principal = load_platform_principal(
            db,
            request.cookies.get(PLATFORM_SESSION_COOKIE),
        )
        request.state.setup_required = is_setup_required(db)
    finally:
        db.close()
    if request.state.setup_required and path not in {"/setup", "/healthz", "/readyz"}:
        return RedirectResponse(url="/setup", status_code=303)

    if path in public_paths or path.startswith(public_prefixes):
        return await call_next(request)

    if path.startswith("/platform"):
        platform_principal = request.state.platform_principal
        if platform_principal is None:
            return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)
        password_redirect = platform_must_change_password_redirect(platform_principal, path)
        if password_redirect is not None:
            return RedirectResponse(url=password_redirect, status_code=303)
        return await call_next(request)

    tenant_principal = request.state.current_user
    if tenant_principal is None:
        return RedirectResponse(url=TENANT_LOGIN_PATH, status_code=303)
    password_redirect = tenant_must_change_password_redirect(tenant_principal, path)
    if password_redirect is not None:
        return RedirectResponse(url=password_redirect, status_code=303)
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and path not in {
        TENANT_PASSWORD_PATH,
        TENANT_LOGOUT_PATH,
    }:
        try:
            # 所有租户浏览器写请求统一经过订阅策略；路由无需自行读取状态或日期，
            # 新增页面也不会因为遗漏判断而在停用、挂起或到期后继续修改数据。
            require_tenant_mutation_allowed(tenant_principal)
        except TenantPolicyError as exc:
            return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})
    # ContextVar 必须覆盖完整的 ASGI 调用链，页面内部新建的每个业务会话才能从
    # 已验证数据库会话恢复出的 principal 取得 tenant_id。退出请求时上下文会
    # 精确复位，异步线程复用也不会把本次租户泄漏到下一次请求。
    with tenant_scope(tenant_principal.tenant_id):
        return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "fulcrum"}


@app.get("/readyz")
async def readyz():
    """只有数据库 revision 已到 head 且安全种子完成后才返回就绪。"""

    readiness = database_readiness()
    status_code = 200 if readiness["ready"] else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": bool(readiness["ready"]),
            "ready": bool(readiness["ready"]),
            "error": readiness["error"],
            "expected_revision": readiness["expected_revision"],
        },
    )


# 领域路由只依赖 shared 和服务层；固定路径优先于同层动态主键路径。
app.include_router(channels_connections_router)
app.include_router(channels_ipad_login_router)
app.include_router(channels_accounts_router)
app.include_router(channels_actions_router)
app.include_router(channels_webridge_router)
app.include_router(channels_workwechat_contacts_router)
app.include_router(chatrooms_directory_router)
app.include_router(chatrooms_management_router)
app.include_router(chatrooms_plugin_settings_router)
app.include_router(plugins_routes_router)
app.include_router(maibot_connections_router)
app.include_router(messages_routes_router)
app.include_router(messages_moments_router)
app.include_router(dashboard_router)
app.include_router(bots_router)
app.include_router(points_router)
app.include_router(setup_router)
app.include_router(channel_webhooks_router)
