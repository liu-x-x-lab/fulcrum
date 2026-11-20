"""MaiBot 多连接的非敏感配置、稳定聊天标识及唯一归属规则。"""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from typing import Any, Mapping
from urllib.parse import urlsplit


MAX_CONNECTIONS = 20
MAX_TARGETS = 2000
SECRET_FIELD = "connection_tokens"
TRANSPORT_DEFAULTS = {
    "protocol": "maim", "verify_ssl": True,
    "connect_timeout_seconds": 15, "receive_timeout_seconds": 120,
    "heartbeat_interval_seconds": 20, "reconnect_delay_seconds": 5,
    "queue_maxsize": 200, "max_send_retries": 2, "max_message_age_seconds": 120,
}
BEHAVIOR_DEFAULTS = {
    "collect_group_messages": True, "collect_private_messages": True,
    "enable_reply_output": True, "reply_group_messages": True,
    "reply_private_messages": True, "mention_only": False,
}


def parse_config(value: str | None) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def target_key(target: Mapping[str, Any]) -> tuple[int, str, str]:
    return (int(target.get("channel_id") or 0), str(target.get("chat_type") or ""), str(target.get("external_id") or ""))


def valid_ws_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        # 连接令牌有独立的密文字段；禁止通过 URL 的查询参数或 userinfo 夹带凭据。
        return bool(parsed.scheme in {"ws", "wss"} and parsed.hostname and parsed.port != 0
                    and parsed.username is None and parsed.password is None
                    and not parsed.query and not parsed.fragment
                    and not any(char.isspace() or ord(char) < 32 for char in value))
    except (TypeError, ValueError):
        return False


def connection_platform(tenant_id: int, binding_id: int, connection_id: str) -> str:
    """平台标识符合运行时的 64 字符限制，长连接编号也不会被截断为同一值。"""

    value = f"mbs-{tenant_id}-{binding_id}-{connection_id}"
    return value if len(value) <= 64 else "mbs-" + hashlib.sha256(value.encode()).hexdigest()[:48]


def connections_from_config(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """旧单连接按原范围惰性兼容；首次保存时再原子迁移，无需改变数据库结构。"""

    if "connections" in raw:
        values = raw.get("connections")
        allowed = {"id", "name", "enabled", "api_server_ws_url", "platform_name", "scope", "revision", *TRANSPORT_DEFAULTS, *BEHAVIOR_DEFAULTS}
        return [deepcopy({key: value for key, value in item.items() if key in allowed}) for item in values if isinstance(item, dict)] if isinstance(values, list) else []
    if not (raw.get("api_server_ws_url") or raw.get("enabled") or raw.get("api_key")):
        return []
    connection = {
        **{key: raw.get(key, value) for key, value in TRANSPORT_DEFAULTS.items()},
        **{key: raw.get(key, value) for key, value in BEHAVIOR_DEFAULTS.items()},
        # 原有单连接没有协议字段；升级和首次保存都应保留原线协议。
        "protocol": raw.get("protocol", "legacy_api"),
        "id": "default", "name": "默认连接", "enabled": bool(raw.get("enabled", False)),
        "api_server_ws_url": str(raw.get("api_server_ws_url") or ""),
        "platform_name": str(raw.get("platform_name") or "multi-bot-saas"),
        "enable_reply_output": bool(raw.get("enable_reply_output", False)),
        "scope": {"mode": "legacy_all", "targets": [], "exclusions": []},
    }
    material = [connection, raw["maibot_reply_generation"]] if raw.get("maibot_reply_generation") else connection
    connection["revision"] = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:24]
    return [connection]


def invalidate_binding_replies(binding) -> None:
    """绑定停用后再启用，也不能复活停用前的回调或待发回复。"""

    raw = parse_config(binding.config_json)
    if isinstance(raw.get("connections"), list):
        for connection in raw["connections"]:
            if isinstance(connection, dict):
                connection["revision"] = uuid.uuid4().hex
    else:
        raw["maibot_reply_generation"] = uuid.uuid4().hex
    binding.config_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)


class MaiBotRouting:
    """一份配置只建一次索引，目录批量渲染不逐行扫描全部聊天选择。"""

    def __init__(self, raw: Mapping[str, Any]):
        self.targets: dict[tuple[int, str, str], list[dict]] = {}
        self.blankets = []
        self.valid = True
        try:
            for connection in connections_from_config(raw):
                scope = connection.get("scope") or {}
                for target in scope.get("targets", []):
                    self.targets.setdefault(target_key(target), []).append(connection)
                if scope.get("mode") == "legacy_all":
                    self.blankets.append((connection, {target_key(item) for item in scope.get("exclusions", [])}))
        except (ValueError, TypeError, AttributeError):
            self.valid = False

    def owner(self, *, channel_id: int, chat_type: str, external_id: str, include_disabled: bool = False) -> dict[str, Any] | None:
        if not self.valid or chat_type not in {"group", "direct"}:
            return None
        key = (int(channel_id), chat_type, external_id)
        candidates = list(self.targets.get(key, []))
        for connection, exclusions in self.blankets:
            if key not in exclusions and connection.get("collect_group_messages" if chat_type == "group" else "collect_private_messages", True):
                if not any(item is connection for item in candidates):
                    candidates.append(connection)
        if len(candidates) != 1:
            return None
        owner = candidates[0]
        return owner if include_disabled or owner.get("enabled") else None


def connection_owner(
    raw: Mapping[str, Any], *, channel_id: int, chat_type: str, external_id: str,
    include_disabled: bool = False,
) -> dict[str, Any] | None:
    """显式范围与旧范围都校验唯一性，遇到损坏或歧义配置直接拒绝路由。"""

    return MaiBotRouting(raw).owner(channel_id=channel_id, chat_type=chat_type, external_id=external_id, include_disabled=include_disabled)


def connection_runtime_config(raw: Mapping[str, Any], connection: Mapping[str, Any], *, tenant_id: int, binding_id: int) -> dict[str, Any]:
    connection_id = str(connection["id"])
    tokens = raw.get(SECRET_FIELD)
    token = tokens.get(connection_id, "") if isinstance(tokens, Mapping) else ""
    if "connections" not in raw and connection_id == "default":
        token = raw.get("api_key") or ""
    return {
        **TRANSPORT_DEFAULTS, **BEHAVIOR_DEFAULTS, **dict(connection),
        "api_key": str(token),
        "platform_name": str(connection.get("platform_name") or connection_platform(tenant_id, binding_id, connection_id)),
    }


def maibot_outbound_allowed(binding, conversation, payload: Mapping[str, Any]) -> bool:
    """发送前核对原连接和配置版本，转交、停用、改令牌后旧排队回复不能复活。"""

    if binding is None or binding.status != "enabled" or not binding.bot or not binding.bot.is_enabled or binding.bot.deleted_at is not None:
        return False
    if (conversation.tenant_id != binding.tenant_id or conversation.bot_id != binding.bot_id):
        return False
    raw = parse_config(binding.config_json)
    owner = connection_owner(raw, channel_id=conversation.channel_id or 0, chat_type=conversation.chat_type, external_id=conversation.external_id)
    if owner is None or not owner.get("enable_reply_output", False):
        return False
    connection_id = str(payload.get("maibot_connection_id") or "default")
    if str(owner["id"]) != connection_id:
        return False
    if payload.get("maibot_binding_id") not in (None, binding.id):
        return False
    revision = payload.get("maibot_connection_revision")
    if (revision is not None and revision != owner.get("revision")) or (revision is None and ("connections" in raw or raw.get("maibot_reply_generation"))):
        return False
    return bool(owner.get("reply_group_messages" if conversation.chat_type == "group" else "reply_private_messages", True))
