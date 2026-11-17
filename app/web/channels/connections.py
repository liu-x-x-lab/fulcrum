"""账号接入流程：选择渠道、验证连接并完成账号绑定。"""

from urllib.parse import quote
import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.models.entities import Channel
from app.services.account_connections import (
    AccountConnectionError,
    begin_account_connection,
    finalize_account_connection,
    verify_account_connection,
)
from app.services.bot_entitlements import BotEntitlementError, get_bot_quota
from app.services.channel_catalog import require_supported_channel_type
from app.services.crud import get_channel
from app.services.guga_agent.access import (
    account_info as guga_account_info,
    connection_url as guga_connection_url,
    initialize_connection as initialize_guga_connection,
)
from app.services.local_proxy_connector import list_online_proxy_connectors
from app.services.login_proxy_pool import (
    LoginProxyPoolError,
    build_login_proxy_session_summary,
    create_login_proxy_session,
    list_available_proxy_regions,
)
from app.services.outbound_files import build_public_base_url
from app.services.provider_nodes import list_provider_nodes
from app.services.saas_auth import has_tenant_role
from app.services.tenant_admin import TenantPolicyError, require_tenant_mutation_allowed
from app.services.wechat_ipad import build_wechat_ipad_overview
from app.services.wechat_webridge.access import (
    account_info as webridge_account_info,
    connection_url as webridge_connection_url,
    initialize_connection as initialize_webridge_connection,
    websocket_url as webridge_websocket_url,
)
from app.web.auth_common import require_tenant_csrf
from app.web.shared.access import enforce_role, get_session, require_user
from app.web.shared.presentation import render


router = APIRouter()


def _channel_connection_state(channel: Channel) -> dict[str, object]:
    """从服务端运行信息读取统一连接状态，不把协议原始响应交给页面。"""

    try:
        runtime_info = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        runtime_info = {}
    connection = runtime_info.get("account_connection") if isinstance(runtime_info, dict) else None
    if not isinstance(connection, dict):
        connection = {}
    state = str(connection.get("state") or ("active" if channel.is_enabled else channel.status or "draft")).strip()
    labels = {
        "active": "已接入",
        "online": "已接入",
        "verified": "待确认",
        "connecting": "连接中",
        "failed": "连接失败",
        "draft": "待连接",
        "offline": "已停用",
    }
    return {
        "state": state,
        "label": labels.get(state, state or "未知"),
        "message": str(connection.get("message") or "").strip(),
        "display_name": str(connection.get("display_name") or channel.name).strip(),
    }


def _account_connection_error_message(code: str) -> str:
    """把连接和额度错误码转换为不泄露其他租户信息的操作提示。"""

    messages = {
        "bot_quota_exceeded": "机器人额度已用完，请先升级套餐",
        "subscription_inactive": "当前订阅不可用",
        "subscription_expired": "当前订阅已到期",
        "bot_account_already_bound": "该账号已经绑定，请联系平台处理",
        "bot_binding_conflict": "账号绑定冲突，请稍后重试",
        "device_owned_by_other_tenant": "该设备已经绑定，请联系平台处理",
        "device_trial_already_claimed": "该设备已经领取过试用权益",
        "trial_device_already_claimed": "当前试用设备不能更换绑定账号",
        "connection_not_verified": "请先完成账号验证",
        "connection_identity_changed": "微信账号信息已变化，请刷新页面后重新核对",
        "connection_already_active": "该账号已经完成接入",
        "connection_not_found": "连接记录不存在",
        "connection_type_not_supported": "暂不支持该接入方式",
        "account_identity_missing": "客户端没有返回可验证的账号身份",
        "platform_protocol_service_missing": "平台尚未配置该协议服务，请联系平台管理员",
        "platform_protocol_service_disabled": "该协议服务已暂停新账号接入，请联系平台管理员",
        "proxy_mode_invalid": "请选择本次登录使用的网络方式",
        "proxy_province_required": "请选择代理所在省份",
        "proxy_city_required": "请选择代理所在城市",
        "proxy_host_required": "请输入自有代理地址",
        "proxy_port_invalid": "请输入有效的自有代理端口",
        "proxy_region_unavailable": "所选地区暂无可用代理，请选择其他地区或直接登录",
        "proxy_connector_required": "请选择一个在线的本地代理连接器",
        "proxy_connector_unavailable": "本地连接器当前不在线，请在 Windows 连接器中启动后重试",
    }
    normalized = str(code or "").strip()
    return messages.get(normalized, normalized or "账号连接失败")


@router.get("/channels/connect", response_class=HTMLResponse)
async def channel_connect_page(request: Request):
    """按选择平台、连接账号和确认启用三个阶段展示统一接入向导。"""

    db = get_session()
    try:
        raw_channel_id = str(request.query_params.get("channel_id") or "").strip()
        channel = None
        if raw_channel_id:
            try:
                channel = get_channel(db, int(raw_channel_id))
            except ValueError:
                channel = None
            if channel is None:
                raise HTTPException(status_code=404, detail="Connection not found")
            if channel.channel_type == "wechat_webridge" and not has_tenant_role(require_user(request), "tenant_admin"):
                raise HTTPException(status_code=403, detail="Permission denied")
        requested_type = str(
            channel.channel_type if channel is not None else request.query_params.get("channel_type") or ""
        ).strip()
        if requested_type:
            try:
                requested_type = require_supported_channel_type(requested_type)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="Connection type not found") from exc
        platform_choice = str(request.query_params.get("platform") or "").strip()
        connection = _channel_connection_state(channel) if channel is not None else {}
        network_setup = bool(channel is not None and request.query_params.get("network") == "1")
        is_relogin = bool(
            channel is not None
            and (network_setup or request.query_params.get("relogin") == "1")
        )
        if channel is not None and channel.channel_type == "wechat_gugatool":
            if not guga_connection_url(channel):
                try:
                    require_tenant_mutation_allowed(require_user(request))
                except TenantPolicyError as exc:
                    raise HTTPException(status_code=403, detail=str(exc)) from exc
                initialize_guga_connection(channel, build_public_base_url(request))
                db.commit()
            stage = "guga"
        elif channel is not None and channel.channel_type == "wechat_webridge":
            if not webridge_connection_url(channel):
                try:
                    require_tenant_mutation_allowed(require_user(request))
                except TenantPolicyError as exc:
                    raise HTTPException(status_code=403, detail=str(exc)) from exc
                initialize_webridge_connection(channel, build_public_base_url(request))
                db.commit()
            stage = "webridge"
        elif network_setup:
            stage = "configure"
        elif channel is not None:
            stage = "confirm" if connection.get("state") == "verified" else "progress"
        elif requested_type:
            stage = "configure"
        elif platform_choice == "wechat":
            stage = "wechat_method"
        else:
            stage = "choose"
        try:
            quota = get_bot_quota(db)
            quota_error = ""
        except BotEntitlementError as exc:
            quota = None
            quota_error = _account_connection_error_message(str(exc))
        response = render(
            request,
            "channel_connect.html",
            page_title="接入账号",
            stage=stage,
            requested_type=requested_type,
            channel=channel,
            connection=connection,
            webridge_url=webridge_connection_url(channel) if channel and channel.channel_type == "wechat_webridge" else "",
            webridge_ws_url=webridge_websocket_url(channel) if channel and channel.channel_type == "wechat_webridge" else "",
            webridge_account=webridge_account_info(channel) if channel and channel.channel_type == "wechat_webridge" else {},
            guga_url=guga_connection_url(channel) if channel and channel.channel_type == "wechat_gugatool" else "",
            guga_account=guga_account_info(channel) if channel and channel.channel_type == "wechat_gugatool" else {},
            quota=quota,
            quota_error=quota_error,
            provider_nodes=list_provider_nodes(db),
            proxy_regions=(list_available_proxy_regions(db) if requested_type == "wechat_ipad" else []),
            local_connectors=(list_online_proxy_connectors(db) if requested_type == "wechat_ipad" else []),
            is_relogin=is_relogin,
            login_network=(
                build_login_proxy_session_summary(db, channel_id=int(channel.id))
                if channel is not None and channel.channel_type == "wechat_ipad"
                else {}
            ),
            wechat_ipad_overview=(build_wechat_ipad_overview(channel) if channel and channel.channel_type == "wechat_ipad" else None),
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        db.close()


@router.post("/channels/connect/start")
async def channel_connect_start_action(
    request: Request,
    channel_type: str = Form(""),
    display_name: str = Form(""),
    api_base: str = Form(""),
    bot_token: str = Form(""),
    adapter_config_json: str = Form(""),
    provider_node_id: int = Form(0),
    provider_account_id: int = Form(0),
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
    """创建连接草稿；可信账号 ID 和设备键绝不从此表单接收。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            channel = begin_account_connection(
                db,
                channel_type=channel_type,
                display_name=display_name,
                connection_values={
                    "api_base": build_public_base_url(request) if channel_type in {"wechat_webridge", "wechat_gugatool"} else api_base,
                    "bot_token": bot_token,
                    "adapter_config_json": adapter_config_json,
                    "provider_node_id": provider_node_id,
                    "provider_account_id": provider_account_id,
                },
            )
            if channel.channel_type == "wechat_ipad":
                # 登录网络只保存为本次扫码会话快照；渠道 adapter_config 始终不接收代理字段。
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
            elif channel.channel_type not in {"wechat_webridge", "wechat_gugatool"}:
                verify_account_connection(db, channel_id=channel.id)
        except (AccountConnectionError, BotEntitlementError, LoginProxyPoolError, ValueError) as exc:
            message = quote(_account_connection_error_message(str(exc)))
            if "channel" in locals() and channel.channel_type == "wechat_ipad":
                return RedirectResponse(
                    url=f"/channels/{channel.id}/wechat-ipad/login-session?notice={message}&tone=danger",
                    status_code=303,
                )
            return RedirectResponse(url=f"/channels/connect?channel_type={quote(channel_type)}&notice={message}&tone=danger", status_code=303)
        return RedirectResponse(url=f"/channels/connect?channel_id={channel.id}", status_code=303)
    finally:
        db.close()


@router.get("/channels/{channel_id}/connect/status")
async def channel_connect_status(request: Request, channel_id: int):
    """只向所属企业管理员返回接入进度，不暴露 Token 或设备会话。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if channel is None or channel.channel_type not in {"wechat_webridge", "wechat_gugatool"}:
            raise HTTPException(status_code=404, detail="Connection not found")
        account = webridge_account_info(channel) if channel.channel_type == "wechat_webridge" else guga_account_info(channel)
        return JSONResponse(
            {"connection": _channel_connection_state(channel), "account": account},
            headers={"Cache-Control": "no-store"},
        )
    finally:
        db.close()


@router.post("/channels/{channel_id}/connect/verify")
async def channel_connect_verify_action(
    request: Request,
    channel_id: int,
    csrf_token: str = Form(""),
):
    """重新检查草稿连接状态并保存可信身份摘要。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            result = verify_account_connection(db, channel_id=channel_id)
        except AccountConnectionError as exc:
            message = quote(_account_connection_error_message(str(exc)))
            return RedirectResponse(url=f"/channels/connect?channel_id={channel_id}&notice={message}&tone=danger", status_code=303)
        tone = "danger" if result.state == "failed" else ""
        return RedirectResponse(
            url=f"/channels/connect?channel_id={channel_id}&notice={quote(result.message)}&tone={tone}",
            status_code=303,
        )
    finally:
        db.close()


@router.post("/channels/{channel_id}/connect/finalize")
async def channel_connect_finalize_action(
    request: Request,
    channel_id: int,
    display_name: str = Form(""),
    expected_account_id: str = Form(""),
    csrf_token: str = Form(""),
):
    """确认显示名称并原子启用机器人、渠道和额度绑定。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            account = finalize_account_connection(db, channel_id=channel_id, display_name=display_name, expected_account_id=expected_account_id)
        except AccountConnectionError as exc:
            message = quote(_account_connection_error_message(str(exc)))
            return RedirectResponse(url=f"/channels/connect?channel_id={channel_id}&notice={message}&tone=danger", status_code=303)
        return RedirectResponse(url=f"/channels/{account.channel.id}?notice={quote('账号已接入')}", status_code=303)
    finally:
        db.close()
