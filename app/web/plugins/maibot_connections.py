"""复用租户插件鉴权的 MaiBot 多连接管理接口。"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from websockets.asyncio.client import connect

from app.services.crud import get_binding
from app.services.maibot_config import connection_platform, connections_from_config, parse_config, valid_ws_url
from app.services.maibot_management import (
    MaiBotConfigConflict, MaiBotConfigError, connection_status_view, connection_tokens, connection_view, save_connections, search_targets,
)
from app.services.ops import record_audit
from app.services.plugin_runtime import execute_binding_action
from app.services.plugin_runtime.maibot_client import maibot_connection_error, stop_maibot_connection
from app.services.plugin_secrets import PluginSecretError
from app.web.auth_common import require_tenant_csrf
from app.web.plugins.routes import _get_tenant_usable_plugin
from app.web.shared.access import enforce_role, get_session


router = APIRouter(prefix="/plugins/{binding_id}/maibot", tags=["maibot-connections"])


def _binding(db, binding_id):
    binding = get_binding(db, binding_id)
    if (binding is None or binding.plugin is None or binding.plugin.code != "maibot_adapter"
            or binding.bot is None or binding.bot.deleted_at is not None):
        raise HTTPException(404, "MaiBot 插件绑定不存在")
    _get_tenant_usable_plugin(db, binding.plugin_id)
    return binding


async def _body(request: Request) -> dict:
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > 1_000_000:
        raise HTTPException(413, "配置内容过大")
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(413, "配置内容过大")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "配置格式无效") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "配置格式无效")
    return value


@router.get("")
def get_connections(request: Request, binding_id: int):
    enforce_role(request, "readonly")
    with get_session() as db:
        return JSONResponse(connection_view(db, _binding(db, binding_id)), headers={"Cache-Control": "no-store"})


@router.get("/targets")
def list_targets(request: Request, binding_id: int, chat_type: str = "group", q: str = "", channel_id: int = 0, page: int = 1):
    enforce_role(request, "readonly")
    with get_session() as db:
        binding = _binding(db, binding_id)
        try:
            return JSONResponse({"ok": True, **search_targets(db, binding, chat_type=chat_type, keyword=q, channel_id=channel_id, page=page)}, headers={"Cache-Control": "no-store"})
        except MaiBotConfigError as error:
            raise HTTPException(400, str(error)) from None


@router.get("/status")
def get_connection_status(request: Request, binding_id: int):
    enforce_role(request, "readonly")
    with get_session() as db:
        return JSONResponse(connection_status_view(db, _binding(db, binding_id)), headers={"Cache-Control": "no-store"})


@router.post("")
async def update_connections(request: Request, binding_id: int):
    enforce_role(request, "ops")
    with get_session() as db:
        require_tenant_csrf(db, request, request.headers.get("x-csrf-token"))
        binding = _binding(db, binding_id)
        data = await _body(request)
        try:
            changed = save_connections(db, binding, data)
        except MaiBotConfigConflict as error:
            raise HTTPException(409, str(error)) from None
        except MaiBotConfigError as error:
            raise HTTPException(400, str(error)) from None
        except PluginSecretError:
            raise HTTPException(400, "连接令牌暂时无法加密保存，请联系管理员检查插件密钥设置") from None
        record_audit(db, "maibot.connections.update", "bot_plugin", binding.id, f"connections={len(data['connections'])}")
        if binding.status == "enabled":
            execute_binding_action(binding, "apply_connections", payload={"connection_ids": changed})
        else:
            stop_maibot_connection(binding.tenant_id, binding.id)
        return {**connection_view(db, binding), "notice": "MaiBot 连接和聊天范围已保存并应用"}


@router.post("/restart")
async def restart_connection(request: Request, binding_id: int):
    enforce_role(request, "ops")
    with get_session() as db:
        require_tenant_csrf(db, request, request.headers.get("x-csrf-token"))
        binding = _binding(db, binding_id)
        data = await _body(request)
        connection_id = str(data.get("connection_id") or "")
        item = next((item for item in connections_from_config(parse_config(binding.config_json)) if item["id"] == connection_id), None)
        if item is None:
            raise HTTPException(404, "连接不存在，请先保存")
        if binding.status != "enabled" or not item.get("enabled"):
            raise HTTPException(400, "请先启用插件绑定和当前连接")
        execute_binding_action(binding, "apply_connections", payload={"connection_ids": [connection_id]})
        record_audit(db, "maibot.connection.restart", "bot_plugin", binding.id, f"connection={connection_id}")
        return {"ok": True, "notice": "当前连接已重新连接，其他连接不受影响"}


@router.post("/test")
async def test_connection(request: Request, binding_id: int):
    enforce_role(request, "ops")
    with get_session() as db:
        require_tenant_csrf(db, request, request.headers.get("x-csrf-token"))
        binding = _binding(db, binding_id)
        data = await _body(request)
        connection_id = str(data.get("id") or "")
        address = str(data.get("api_server_ws_url") or "").strip()
        protocol = data.get("protocol", "maim")
        supplied_token = data.get("token", "")
        clear_token = data.get("clear_token", False)
        if (not isinstance(supplied_token, str) or len(supplied_token) > 4096 or any(char in supplied_token for char in "\r\n\x00")
                or not isinstance(clear_token, bool) or (clear_token and supplied_token.strip())):
            raise HTTPException(400, "连接令牌选项无效")
        token = supplied_token.strip() or ("" if clear_token else connection_tokens(binding).get(connection_id, ""))
        if (not valid_ws_url(address) or len(address) > 2000 or not isinstance(protocol, str) or protocol not in {"maim", "legacy_api"}
                or not isinstance(token, str) or len(token) > 4096 or any(char in token for char in "\r\n\x00")):
            raise HTTPException(400, "连接地址、协议或令牌无效")
        # 测试使用独立平台身份，避免 MaiBot 将正式连接当作同平台旧连接替换掉。
        test_id = uuid.uuid4().hex
        platform = connection_platform(binding.tenant_id, binding.id, "test-" + test_id)
    headers = {"Authorization": token, "platform": platform} if protocol == "maim" else {
        "x-apikey": token, "x-platform": platform, "x-uuid": test_id}
    try:
        async with asyncio.timeout(6):
            async with connect(address, additional_headers=headers, open_timeout=5, close_timeout=1, proxy=None, max_size=1024):
                pass
        return {"ok": True, "notice": "WebSocket 握手成功；尚未发送聊天消息，协议收发需实际消息验证"}
    except Exception as error:
        # 底层异常可能包含 URL、请求头或令牌，禁止透传。
        notices = {
            "auth_rejected": "MaiBot 拒绝连接，请检查令牌和服务端鉴权设置。",
            "path_not_found": "连接路径不存在，请核对 MaiBot 的完整 WebSocket 地址。",
            "tls_failure": "证书校验失败，请检查地址和服务端证书。",
            "connect_timeout": "连接超时，请检查 MaiBot 是否在线。",
            "network_failure": "无法连接 MaiBot，请检查地址、端口和网络。",
        }
        notice = notices.get(maibot_connection_error(error), "连接失败，请检查地址、端口、证书和连接令牌。")
        return JSONResponse({"ok": False, "notice": notice}, status_code=400)
