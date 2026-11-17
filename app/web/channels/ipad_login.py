"""iPad 单次代理登录会话和二维码接口，调用现有登录流程服务。"""

from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.models.entities import Channel
from app.services.crud import get_channel
from app.services.login_proxy_pool import (
    LoginProxyPoolError,
    build_login_proxy_session_summary,
    create_login_proxy_session,
    get_active_login_proxy_session,
)
from app.services.saas_auth import has_tenant_role
from app.services.wechat_ipad import build_wechat_ipad_overview, resolve_wechat_ipad_qrcode_image
from app.services.wechat_ipad.login_flow import (
    WechatIpadLoginFlowError,
    cancel_login_with_proxy_session,
    poll_login_with_proxy_session,
    start_login_with_proxy_session,
)
from app.web.auth_common import require_tenant_csrf
from app.web.channels.connections import _account_connection_error_message
from app.web.shared.access import enforce_role, get_session


router = APIRouter()


def build_wechat_ipad_flow_payload(db: Session, channel: Channel) -> dict[str, object]:
    """把协议运行态收口为扫码页面唯一需要的四类业务状态。"""

    overview = build_wechat_ipad_overview(channel)
    login_state = str(overview.get("login_state") or "").strip()
    qrcode_status = str(overview.get("qrcode_status") or "").strip()
    if login_state == "online" or qrcode_status == "success":
        status = "success"
        status_label = "登录成功"
        message = "账号连接成功，正在进入下一步"
        should_poll = False
    elif qrcode_status in {"scanned", "confirmed"}:
        status = "phone_confirm"
        status_label = "请在手机确认"
        message = "已识别扫码，请在手机微信中确认登录"
        should_poll = True
    elif qrcode_status == "waiting_scan" and overview.get("qrcode_image_url"):
        status = "waiting_scan"
        status_label = "等待扫码"
        message = "请使用手机微信扫描二维码"
        should_poll = True
    elif qrcode_status in {"failed", "expired", "unavailable"}:
        status = "failed"
        status_label = "登录未完成"
        message = "本次登录未完成，请重新开始"
        should_poll = False
    else:
        status = "preparing"
        status_label = "准备二维码"
        message = "正在准备安全登录二维码"
        should_poll = True
    network = build_login_proxy_session_summary(db, channel_id=int(channel.id))
    return {
        "ok": True,
        "status": status,
        "status_label": status_label,
        "message": message,
        "qrcode_image_url": str(overview.get("qrcode_image_url") or ""),
        "should_poll": should_poll,
        "network_label": str(network.get("network_label") or ""),
    }


@router.get("/channels/{channel_id}/wechat-ipad/login-session")
async def configure_wechat_ipad_login_session(request: Request, channel_id: int):
    """进入统一登录网络选择页，首次接入草稿和正式账号共用同一页面。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if channel is None or channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=404, detail="Channel not found")
        query = urlencode(
            {
                "channel_id": int(channel.id),
                "network": "1",
                "notice": request.query_params.get("notice", ""),
                "tone": request.query_params.get("tone", ""),
            }
        )
        return RedirectResponse(url=f"/channels/connect?{query}", status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-ipad/login-session")
async def create_wechat_ipad_login_session_action(
    request: Request,
    channel_id: int,
    proxy_mode: str = Form("direct"),
    proxy_province: str = Form(""),
    proxy_city: str = Form(""),
    custom_proxy_host: str = Form(""),
    custom_proxy_port: str = Form(""),
    custom_proxy_username: str = Form(""),
    custom_proxy_password: str = Form(""),
    proxy_connector_id: int = Form(0),
    csrf_token: str = Form(""),
):
    """替换当前扫码会话并跳转到业务化二维码页，旧会话租约同步释放。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        channel = get_channel(db, channel_id)
        if channel is None or channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=404, detail="Channel not found")
        try:
            create_login_proxy_session(
                db,
                channel=channel,
                mode=proxy_mode,
                values={
                    "province": proxy_province,
                    "city": proxy_city,
                    "host": custom_proxy_host,
                    "port": custom_proxy_port,
                    "username": custom_proxy_username,
                    "password": custom_proxy_password,
                    "connector_id": proxy_connector_id,
                },
            )
        except LoginProxyPoolError as exc:
            message = quote(_account_connection_error_message(str(exc)))
            return RedirectResponse(
                url=f"/channels/{channel.id}/wechat-ipad/login-session?notice={message}&tone=danger",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/channels/connect?channel_id={channel.id}&relogin=1",
            status_code=303,
        )
    finally:
        db.close()


@router.get("/channels/{channel_id}/wechat-ipad/qrcode-image")
async def wechat_ipad_qrcode_image(channel_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        image = resolve_wechat_ipad_qrcode_image(channel)
        if not image.get("available"):
            raise HTTPException(status_code=404, detail="QR code image not available")
        if image["kind"] == "url":
            return RedirectResponse(url=image["url"], status_code=307)
        return Response(content=image["content"], media_type=image["media_type"])
    finally:
        db.close()


@router.get("/channels/{channel_id}/wechat-ipad/login-flow")
async def get_wechat_ipad_login_flow(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-ipad/login-flow/start")
async def start_wechat_ipad_login_flow(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, request.headers.get("X-CSRF-Token", ""))
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        active_session = get_active_login_proxy_session(db, channel_id=int(channel.id))
        connector_registry = None
        if active_session is not None and active_session.mode == "local_connector":
            from app.services.local_proxy_connector_ws import local_proxy_connector_ws_registry

            connector_registry = local_proxy_connector_ws_registry
            if not connector_registry.is_online(int(active_session.proxy_connector_id or 0)):
                raise HTTPException(status_code=409, detail="proxy_connector_offline")
        try:
            start_login_with_proxy_session(db, channel)
        except WechatIpadLoginFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        active_session = get_active_login_proxy_session(db, channel_id=int(channel.id))
        if active_session is not None and active_session.mode == "local_connector":
            if connector_registry is None:
                raise HTTPException(status_code=409, detail="proxy_connector_offline")
            await connector_registry.bind_session(
                int(active_session.proxy_connector_id or 0), active_session.session_key
            )
            await connector_registry.send_session_message(
                active_session.session_key,
                {"type": "session_bind", "session_key": active_session.session_key},
            )
        db.refresh(channel)
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-ipad/login-flow/cancel")
async def cancel_wechat_ipad_login_flow(request: Request, channel_id: int):
    """取消当前扫码会话；重新开始必须先创建新的服务端代理会话。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, request.headers.get("X-CSRF-Token", ""))
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        cancel_login_with_proxy_session(db, channel)
        db.refresh(channel)
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-ipad/login-flow/poll")
async def poll_wechat_ipad_login_flow(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, request.headers.get("X-CSRF-Token", ""))
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        try:
            poll_login_with_proxy_session(db, channel)
        except WechatIpadLoginFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.refresh(channel)
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()
