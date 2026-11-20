from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.services import maibot_wechat
from app.services.account_identity import account_identity
from app.services.maibot_config import (
    connections_from_config, connection_owner, connection_runtime_config,
)
from app.services.plugin_scopes import binding_group_default_enabled, group_plugin_scope_enabled
from app.models.entities import (
    BotPlugin,
    Channel,
    Chatroom,
    Conversation,
    GroupPluginScope,
    Message,
    TaskJob,
)
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.maibot_client import (
    MaiBotConnectionConfig,
    MaiBotConnectionKey,
    MaiBotReplyOutcome,
    SUPPORTED_PROTOCOLS,
    maibot_message_payload,
    maibot_client_registry,
    stop_maibot_connection,
)
from app.services.plugin_runtime.replies import PluginReplyError, text_reply
from app.services.task_queue import OUTBOUND_DISPATCH_JOB, enqueue_job
from app.services.workwechat_cloud_poll import notify_cloud_job_enqueued, resolve_outbound_job_type


PLUGIN_CODE = "maibot_adapter"
MAX_MESSAGE_TEXT_LENGTH = 8_000
MAX_REPLY_SEGMENT_DEPTH = 3
SUPPORTED_MESSAGE_TYPES = {
    "text",
    "image",
    "file",
    "video",
    "voice",
    "location",
    "app",
    "emoji",
    "system",
}
MESSAGE_PLACEHOLDERS = {
    "image": "[图片]",
    "file": "[文件]",
    "video": "[视频]",
    "voice": "[语音]",
    "location": "[位置]",
    "app": "[应用消息]",
    "emoji": "[表情]",
    "system": "[系统消息]",
}


@dataclass(frozen=True)
class MaiBotPluginConfig:
    """保存当前租户绑定的采集、回复和连接配置。"""

    enabled: bool
    collect_group_messages: bool
    collect_private_messages: bool
    enable_reply_output: bool
    reply_group_messages: bool
    reply_private_messages: bool
    api_server_ws_url: str
    api_key: str = field(repr=False)
    platform_name: str
    verify_ssl: bool
    connect_timeout_seconds: int
    receive_timeout_seconds: int
    heartbeat_interval_seconds: int
    reconnect_delay_seconds: int
    queue_maxsize: int
    max_send_retries: int
    protocol: str
    max_message_age_seconds: int

    def connection_config(self) -> MaiBotConnectionConfig:
        """只把传输字段交给当前绑定自己的 WebSocket 连接。"""

        return MaiBotConnectionConfig(
            ws_url=self.api_server_ws_url,
            api_key=self.api_key,
            platform_name=self.platform_name,
            verify_ssl=self.verify_ssl,
            connect_timeout_seconds=self.connect_timeout_seconds,
            receive_timeout_seconds=self.receive_timeout_seconds,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            reconnect_delay_seconds=self.reconnect_delay_seconds,
            queue_maxsize=self.queue_maxsize,
            max_send_retries=self.max_send_retries,
            protocol=self.protocol,
            max_message_age_seconds=self.max_message_age_seconds,
        )


@dataclass(frozen=True)
class TrustedMaiBotMessage:
    """保存经过数据库交叉验证的插件绑定和入站消息归属链。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message


def matches_event(context) -> dict[str, object]:
    """按聊天类型和租户采集开关匹配旁路消息，不占用命令入口。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "").strip().lower()
    raw = getattr(context, "binding_config", {}) or {}
    owner = connection_owner(raw, channel_id=getattr(context, "channel_id", 0) or 0,
                             chat_type=chat_type, external_id=getattr(context, "conversation_external_id", "") or "")
    config = _load_config(owner or {}) if "connections" in raw else _load_config(raw)
    collection_enabled = (
        config.collect_group_messages if chat_type == "group" else config.collect_private_messages
    )
    matched = (
        config.enabled
        and chat_type in {"direct", "group"}
        and message_type in SUPPORTED_MESSAGE_TYPES
        and collection_enabled
    )
    return {
        "matched": matched,
        "reason": "maibot_collection_enabled" if matched else "maibot_collection_disabled",
        "debug": {"chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """启动或重载当前绑定的连接。"""

    config = _load_config(request.config)
    context = request.context
    if request.action == "start_connections" or (request.action in {"reload", "apply_connections"} and "connections" in request.config):
        if context is None:
            return _ignored_result("trusted_context_required", "缺少插件绑定上下文。")
        selected_ids = request.payload.get("connection_ids") if request.action == "apply_connections" else None
        if request.action != "start_connections":
            if selected_ids is None:
                stop_maibot_connection(context.tenant_id, context.binding_id)
            else:
                for connection_id in selected_ids:
                    maibot_client_registry.stop(MaiBotConnectionKey(context.tenant_id, context.binding_id, str(connection_id)))
        configured_count = 0
        for item in connections_from_config(request.config):
            if selected_ids is not None and item["id"] not in selected_ids:
                continue
            item_config = _load_config(connection_runtime_config(request.config, item, tenant_id=context.tenant_id, binding_id=context.binding_id))
            if (item_config.enabled and _is_valid_ws_url(item_config.api_server_ws_url)
                    and item_config.protocol in SUPPORTED_PROTOCOLS
                    and ("connections" in request.config or item_config.api_key)):
                maibot_client_registry.start(
                    key=MaiBotConnectionKey(context.tenant_id, context.binding_id, item["id"]),
                    config=replace(item_config.connection_config(), generation=str(item.get("revision") or "")),
                    reply_handler=_reply_handler(str(item.get("revision") or "")),
                )
                configured_count += 1
        return PluginExecutionResult(ok=True, status="running", message="MaiBot 连接配置已应用。", output={"configured_connections": configured_count})
    if request.action == "reload":
        if context is not None:
            stop_maibot_connection(context.tenant_id, context.binding_id)
        if not config.enabled:
            return PluginExecutionResult(
                ok=True,
                status="disabled",
                message="MaiBot 适配器当前未启用。",
                output={"enabled": False, "provider_configured": False},
            )
        configured = bool(
            config.protocol in SUPPORTED_PROTOCOLS
            and _is_valid_ws_url(config.api_server_ws_url)
            and config.api_key
        )
        if configured and context is not None:
            execute_action(replace(request, action="start_connections"))
        return PluginExecutionResult(
            ok=True,
            status="running" if configured else "waiting_config",
            message="MaiBot 配置已加载。" if configured else "请配置 MaiBot 地址和 API Key。",
            output={
                "enabled": True,
                "provider_configured": configured,
                "protocol": config.protocol,
                "collect_group_messages": config.collect_group_messages,
                "collect_private_messages": config.collect_private_messages,
                "enable_reply_output": config.enable_reply_output,
                "reply_group_messages": config.reply_group_messages,
                "reply_private_messages": config.reply_private_messages,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _ignored_result("trusted_context_required", "MaiBot 采集必须来自真实入站消息。")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """把可信入站消息提交给当前租户绑定自己的 MaiBot 连接。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)
    if "connections" not in request.config and not config.enabled:
        if request.context is not None:
            stop_maibot_connection(request.context.tenant_id, request.context.binding_id)
        return _ignored_result("disabled", "MaiBot 适配器当前未启用。")

    trusted, context_error = _require_trusted_message(db, request.context)
    if context_error is not None or trusted is None:
        return context_error or _ignored_result("trusted_context_required", "当前消息上下文无效。")
    chat_type = str(trusted.conversation.chat_type or "direct").strip()
    if "connections" not in request.config and not (config.collect_group_messages if chat_type == "group" else config.collect_private_messages):
        return _ignored_result("collection_disabled", "当前绑定未采集此类型的消息。")
    owner = connection_owner(request.config, channel_id=trusted.channel.id, chat_type=chat_type, external_id=trusted.conversation.external_id)
    if owner is None:
        return _ignored_result("unassigned", "当前聊天未分配到已启用的 MaiBot 连接。")
    config = _load_config(connection_runtime_config(request.config, owner, tenant_id=trusted.execution.tenant_id, binding_id=trusted.binding.id))
    key = MaiBotConnectionKey(trusted.execution.tenant_id, trusted.binding.id, str(owner["id"]))
    revision = str(owner.get("revision") or "")

    def ignored(code: str, message: str) -> PluginExecutionResult:
        maibot_client_registry.record_forward(key, revision, code)
        return _ignored_result(code, message)

    message_text = _normalize_message_text(trusted.message)
    enriched = None
    mentioned_self = bool(request.payload.get("mentioned_self"))
    if config.protocol == "maim" and trusted.channel.channel_type in maibot_wechat.WECHAT_CHANNELS:
        enriched = maibot_wechat.inbound_context(db, trusted.channel, trusted.conversation, trusted.message,
                                               message_text, config.platform_name, mentioned_self)
        if enriched.get("self_sent"):
            return _ignored_result("self_sent", "已忽略机器人自己发出的消息。")
        mentioned_self = enriched["mentioned_self"]
    if owner.get("mention_only") and chat_type == "group" and not mentioned_self:
        return ignored("mention_required", "当前连接只接收 @ 机器人的群消息。")
    if chat_type == "group" and not config.collect_group_messages:
        return _ignored_result("collection_disabled", "当前绑定未采集群消息。")
    if chat_type == "direct" and not config.collect_private_messages:
        return _ignored_result("collection_disabled", "当前绑定未采集私聊消息。")
    if config.protocol not in SUPPORTED_PROTOCOLS:
        return _ignored_result("invalid_protocol", "MaiBot 协议请选择 maim 或 legacy_api。")
    if not _is_valid_ws_url(config.api_server_ws_url) or ("connections" not in request.config and not config.api_key):
        return _ignored_result("not_configured", "请先配置当前租户自己的 MaiBot 地址和 API Key。")

    if (enriched and not enriched["media_ready"] and not request.payload.get("maibot_media_final")
            and maibot_wechat.queue_media_forward(db, trusted, owner, mentioned_self)):
        maibot_client_registry.record_forward(key, revision, "waiting_media")
        return PluginExecutionResult(ok=True, status="completed", message="正在等微信附件。", output={"provider_status": "waiting_media", "replies": []})

    if not (enriched["segments"] if enriched else message_text):
        return _ignored_result("empty_message", "当前消息没有可采集内容。")
    api_message = _build_api_message(
        trusted,
        config=config,
        message_text=message_text,
        mentioned_self=mentioned_self,
    )
    if enriched:
        info = api_message["message_info"]
        info["user_info"].update(user_nickname=enriched["nickname"], user_cardname=enriched["cardname"])
        if enriched["account_id"]:
            api_message["account_id"] = enriched["account_id"]
        if enriched["bot_identity"]:
            info["additional_config"]["bot_identity"] = enriched["bot_identity"]
        segments = enriched["segments"]
        api_message["message_segment"] = segments[0] if len(segments) == 1 else {"type": "seglist", "data": segments}
        info["format_info"]["content_format"] = list(dict.fromkeys(item["type"] for item in segments)) + (["seglist"] if len(segments) > 1 else [])
        info["format_info"]["accept_format"] = maibot_wechat.outbound_formats(trusted.channel, trusted.conversation)
    queued = maibot_client_registry.submit(
        key=key,
        config=replace(config.connection_config(), generation=revision),
        api_message=api_message,
        conversation_id=int(trusted.conversation.id),
        reply_handler=_reply_handler(revision),
    )
    if not queued:
        return ignored("queue_full", "MaiBot 发送队列繁忙，本次消息未采集。")
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="消息已提交给 MaiBot。",
        output={"provider_status": "queued", "replies": []},
    )


def _require_trusted_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedMaiBotMessage | None, PluginExecutionResult | None]:
    """交叉验证租户、绑定、机器人、渠道、会话和入站消息的完整归属链。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type not in {"direct", "group"}
        or context.message_type not in SUPPORTED_MESSAGE_TYPES
    ):
        return None, _ignored_result("trusted_context_required", "请从真实入站消息启动 MaiBot 采集。")

    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    sender_id = str(message.sender_id or "").strip() if message is not None else ""
    if (
        binding is None
        or int(binding.tenant_id) != int(context.tenant_id)
        or int(binding.bot_id) != int(context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
        or binding.bot.deleted_at is not None
        or plugin_code != PLUGIN_CODE
        or channel is None
        or int(channel.tenant_id) != int(context.tenant_id)
        or int(channel.bot_id) != int(context.bot_id)
        or not channel.is_enabled
        or channel.deleted_at is not None
        or str(channel.channel_type or "").strip() != str(context.channel_type or "").strip()
        or conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(channel.id)
        or str(conversation.chat_type or "").strip() != str(context.chat_type or "").strip()
        or not str(conversation.external_id or "").strip()
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "").strip() != "in"
        or str(message.message_type or "").strip() != str(context.message_type or "").strip()
        or not sender_id
        or (context.sender_id is not None and str(context.sender_id).strip() != sender_id)
    ):
        return None, _ignored_result("trusted_context_required", "当前消息上下文无效。")

    return (
        TrustedMaiBotMessage(
            execution=context,
            binding=binding,
            channel=channel,
            conversation=conversation,
            message=message,
        ),
        None,
    )


def _build_api_message(
    trusted: TrustedMaiBotMessage,
    *,
    config: MaiBotPluginConfig,
    message_text: str,
    mentioned_self: bool,
) -> dict[str, Any]:
    """构造 MaiM 或旧 API 消息，继续用内部会话令牌隔离出站目标。"""

    conversation_token = f"mbs-conversation-{int(trusted.conversation.id)}"
    sender_id = str(trusted.message.sender_id or "").strip()
    sender_name = _single_line(trusted.message.sender_name or sender_id, limit=128) or sender_id
    chat_type = str(trusted.conversation.chat_type or "direct").strip()
    if chat_type == "group":
        user_id = f"mbs-user-{int(trusted.channel.id)}-{sender_id}"
        group_info: dict[str, str] | None = {
            "platform": config.platform_name,
            "group_id": conversation_token,
            "group_name": _single_line(
                trusted.conversation.title or trusted.conversation.external_id,
                limit=255,
            ),
        }
    else:
        # 私聊回复也必须落回当前平台会话，因此 user_id 使用内部会话令牌。
        # 原始发送人只作为显示名，不作为 MaiBot 可任意指定的出站目标。
        user_id = conversation_token
        group_info = None
    user_info = {
        "platform": config.platform_name,
        "user_id": user_id,
        "user_nickname": sender_name,
    }
    message_id = str(trusted.message.external_message_id or "").strip() or f"mbs-message-{int(trusted.message.id)}"
    created_at = trusted.message.created_at
    timestamp = float(created_at.timestamp()) if created_at is not None else 0.0
    api_message: dict[str, Any] = {
        "message_info": {
            "platform": config.platform_name,
            "message_id": message_id,
            "time": timestamp,
            "format_info": {
                "content_format": ["text"],
                "accept_format": ["text"],
            },
            "additional_config": {
                "source": "multi_bot_saas_maibot_adapter",
                "is_at": mentioned_self,
                "at_bot": mentioned_self,
                "is_mentioned": mentioned_self,
                "message_type": str(trusted.message.message_type or "text"),
                "collect_only": not config.enable_reply_output,
            },
        },
        "message_segment": {"type": "text", "data": message_text},
    }
    if config.protocol == "legacy_api":
        api_message["message_info"]["sender_info"] = {
            "user_info": user_info,
            "group_info": group_info,
        }
        api_message["message_dim"] = {
            "api_key": config.api_key,
            "platform": config.platform_name,
        }
    else:
        api_message["message_info"].update({"user_info": user_info, "group_info": group_info})
        # 渠道未登记真实账号时采用独立平台标识，不猜测微信身份或复用其他渠道账号。
        api_message["account_id"] = account_identity(trusted.channel)["external_id"] or f"mbs-channel-{int(trusted.channel.id)}"
        api_message["scope"] = None
        api_message["raw_message"] = None
    return api_message


def _reply_handler(revision: str):
    return lambda key, conversation_id, package: _handle_maibot_reply(key, conversation_id, package, revision)


def _handle_maibot_reply(
    key: MaiBotConnectionKey,
    conversation_id: int,
    package: Mapping[str, Any],
    revision: str | None = None,
) -> MaiBotReplyOutcome:
    """重新加载当前租户资源，把客户端验证后的会话回复写入标准出站任务。"""

    with tenant_scope(int(key.tenant_id)), tenant_session(TenantSessionLocal) as db:
        binding = db.get(BotPlugin, int(key.binding_id))
        conversation = db.get(Conversation, int(conversation_id))
        if (
            binding is None
            or int(binding.tenant_id) != int(key.tenant_id)
            or binding.status != "enabled"
            or binding.plugin is None
            or str(binding.plugin.code or "") != PLUGIN_CODE
            or binding.bot is None
            or not binding.bot.is_enabled
            or binding.bot.deleted_at is not None
            or conversation is None
            or int(conversation.tenant_id) != int(key.tenant_id)
            or int(conversation.bot_id) != int(binding.bot_id)
        ):
            return MaiBotReplyOutcome("reply_scope_changed")
        channel = db.get(Channel, int(conversation.channel_id or 0))
        if (
            channel is None
            or int(channel.tenant_id) != int(key.tenant_id)
            or int(channel.bot_id) != int(binding.bot_id)
            or not channel.is_enabled
            or channel.deleted_at is not None
        ):
            return MaiBotReplyOutcome("reply_scope_changed")

        raw_config = _parse_json_object(binding.config_json)
        owner = connection_owner(raw_config, channel_id=channel.id, chat_type=conversation.chat_type, external_id=conversation.external_id)
        if owner is None or str(owner["id"]) != key.connection_id:
            return MaiBotReplyOutcome("reply_scope_changed")
        if revision is not None and revision != owner.get("revision"):
            return MaiBotReplyOutcome("reply_scope_changed")
        config = _load_config(owner)
        chat_type = str(conversation.chat_type or "direct").strip()
        if not config.enabled or not config.enable_reply_output:
            return MaiBotReplyOutcome("reply_disabled")
        if chat_type == "group" and (not config.reply_group_messages or not _group_reply_allowed(db, binding, channel, conversation)):
            return MaiBotReplyOutcome("reply_disabled")
        if chat_type == "direct" and not config.reply_private_messages:
            return MaiBotReplyOutcome("reply_disabled")

        payload = maibot_message_payload(package, protocol=config.protocol)
        if payload is None:
            return MaiBotReplyOutcome("reply_no_text")
        rich_wechat = config.protocol == "maim" and channel.channel_type in maibot_wechat.WECHAT_CHANNELS
        if rich_wechat:
            parts = maibot_wechat.reply_parts(db, channel, conversation, payload.get("message_segment"))
            if not parts:
                return MaiBotReplyOutcome("reply_unsupported")
        else:
            try:
                parts = [text_reply(_extract_reply_text(package, protocol=config.protocol))]
            except PluginReplyError:
                return MaiBotReplyOutcome("reply_no_text")
        message_info = payload.get("message_info")
        if config.protocol == "legacy_api":
            package_id = str(package.get("msg_id") or "").strip()
        else:
            package_id = str(message_info.get("message_id") or "").strip() if isinstance(message_info, Mapping) else ""
        dedupe_material = package_id or json.dumps(package, ensure_ascii=False, sort_keys=True)
        dedupe_key = f"maibot:{int(binding.id)}:{key.connection_id}:{int(conversation.id)}:{hashlib.sha256(dedupe_material.encode('utf-8')).hexdigest()}"
        existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key))
        if existing is not None:
            return MaiBotReplyOutcome("reply_duplicate", existing.id)
        jobs = []
        for index, part in enumerate(parts):
            if isinstance(part, tuple):
                part = maibot_wechat.outbound_media_payload(channel, *part)
            if part.get("message_type") == "text" or "message_type" not in part:
                part = {**part, "content": text_reply(part["content"][:20_000])["content"], "message_type": "text"}
                part.pop("type", None)
            outbound_payload = {
                **part, "conversation_id": int(conversation.id), "external_id": str(conversation.external_id),
                "receiver_id": str(conversation.external_id), "chat_type": chat_type,
            }
            job = enqueue_job(
                db, job_type=OUTBOUND_DISPATCH_JOB if channel.channel_type == "wechat_webridge" else resolve_outbound_job_type(channel),
                payload={"channel_id": int(channel.id), "conversation_id": int(conversation.id), "payload": outbound_payload,
                         "plugin_code": PLUGIN_CODE, "maibot_binding_id": int(binding.id),
                         "maibot_connection_id": key.connection_id, "maibot_connection_revision": owner.get("revision"),
                         **({"maibot_previous_dispatch_id": jobs[-1].id} if jobs else {})},
                priority=90, related_type="conversation", related_id=int(conversation.id),
                dedupe_key=dedupe_key if not index else f"{dedupe_key}:{index}", max_attempts=2 if len(parts) == 1 else 1,
            )
            jobs.append(job)
            notify_cloud_job_enqueued(channel, job)
        db.commit()
        return MaiBotReplyOutcome("reply_queued", jobs[0].id)


def _group_reply_allowed(
    db: Session,
    binding: BotPlugin,
    channel: Channel,
    conversation: Conversation,
) -> bool:
    """异步回包再次检查当前群级强制关闭状态。"""

    chatroom = db.scalar(
        select(Chatroom).where(
            Chatroom.channel_id == int(channel.id),
            Chatroom.external_id == str(conversation.external_id),
        )
    )
    if chatroom is None:
        return binding_group_default_enabled(binding)
    if chatroom.status == "left" or not chatroom.is_active or chatroom.management_status != "enabled":
        return False
    scope = db.scalar(
        select(GroupPluginScope).where(
            GroupPluginScope.channel_id == int(channel.id),
            GroupPluginScope.chatroom_id == int(chatroom.id),
            GroupPluginScope.bot_plugin_id == int(binding.id),
        )
    )
    return group_plugin_scope_enabled(binding, scope)


def _extract_reply_text(package: Mapping[str, Any], *, protocol: str = "maim") -> str:
    payload = maibot_message_payload(package, protocol=protocol)
    if payload is None:
        return ""
    segment = payload.get("message_segment")
    text = _extract_segment_text(segment, depth=0)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()
    return text[:20_000]


def _extract_segment_text(segment: Any, *, depth: int) -> str:
    """只解析官方文本与文本片段列表，忽略外部媒体和任意对象。"""

    if depth > MAX_REPLY_SEGMENT_DEPTH or not isinstance(segment, Mapping):
        return ""
    segment_type = str(segment.get("type") or "").strip().lower()
    data = segment.get("data")
    if segment_type == "text" and isinstance(data, str):
        return data
    if segment_type in {"seglist", "seg"} and isinstance(data, list):
        return "".join(_extract_segment_text(item, depth=depth + 1) for item in data[:20])
    return ""


def _normalize_message_text(message: Message) -> str:
    message_type = str(message.message_type or "text").strip().lower()
    content = re.sub(r"\s+", " ", str(message.content or "")).strip()
    if message_type == "text":
        return content[:MAX_MESSAGE_TEXT_LENGTH]
    placeholder = MESSAGE_PLACEHOLDERS.get(message_type, f"[{message_type or '消息'}]")
    if content and content != placeholder:
        return f"{placeholder} {content}"[:MAX_MESSAGE_TEXT_LENGTH]
    return placeholder


def _load_config(raw_config: Mapping[str, Any]) -> MaiBotPluginConfig:
    raw = dict(raw_config)
    platform_name = _single_line(raw.get("platform_name") or "multi-bot-saas", limit=64) or "multi-bot-saas"
    return MaiBotPluginConfig(
        enabled=_as_bool(raw.get("enabled"), default=False),
        collect_group_messages=_as_bool(raw.get("collect_group_messages"), default=True),
        collect_private_messages=_as_bool(raw.get("collect_private_messages"), default=True),
        enable_reply_output=_as_bool(raw.get("enable_reply_output"), default=False),
        reply_group_messages=_as_bool(raw.get("reply_group_messages"), default=True),
        reply_private_messages=_as_bool(raw.get("reply_private_messages"), default=True),
        api_server_ws_url=str(raw.get("api_server_ws_url") or "").strip(),
        api_key=str(raw.get("api_key") or "").strip(),
        platform_name=platform_name,
        verify_ssl=_as_bool(raw.get("verify_ssl"), default=True),
        connect_timeout_seconds=_bounded_int(raw.get("connect_timeout_seconds"), default=15, minimum=5, maximum=60),
        receive_timeout_seconds=_bounded_int(raw.get("receive_timeout_seconds"), default=120, minimum=30, maximum=600),
        heartbeat_interval_seconds=_bounded_int(raw.get("heartbeat_interval_seconds"), default=20, minimum=10, maximum=120),
        reconnect_delay_seconds=_bounded_int(raw.get("reconnect_delay_seconds"), default=5, minimum=1, maximum=60),
        queue_maxsize=_bounded_int(raw.get("queue_maxsize"), default=200, minimum=20, maximum=2_000),
        max_send_retries=_bounded_int(raw.get("max_send_retries"), default=2, minimum=1, maximum=5),
        protocol=str(raw.get("protocol") or "legacy_api").strip().lower(),
        max_message_age_seconds=_bounded_int(raw.get("max_message_age_seconds"), default=120, minimum=1, maximum=86_400),
    )


def _is_valid_ws_url(value: str) -> bool:
    parsed = urlsplit(str(value or "").strip())
    return (
        parsed.scheme.lower() in {"ws", "wss"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def _parse_json_object(raw_json: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw_json or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _single_line(value: Any, *, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    return normalized[:limit]


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _ignored_result(provider_status: str, message: str) -> PluginExecutionResult:
    """观察者插件不向聊天发送前置错误，只在执行状态中记录原因。"""

    return PluginExecutionResult(
        ok=False,
        status="ignored",
        message=message,
        output={"provider_status": provider_status, "replies": []},
    )
