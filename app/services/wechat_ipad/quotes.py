"""从本会话归档还原引用上下文，不接受调用方提供的微信作者、原文或服务端编号。"""

import re

from app.models.entities import Channel, Conversation, Message
from app.services.wechat_ipad.common import _parse_json


def server_id(value: object) -> str | None:
    text = str(value)
    return text if re.fullmatch(r"[1-9][0-9]{0,18}", text) and int(text) < 2**63 else None


def reference_fields(channel: Channel, conversation: Conversation, message: Message | None) -> dict:
    if (not message or message.conversation_id != conversation.id or conversation.channel_id != channel.id
            or conversation.bot_id != channel.bot_id or message.tenant_id != channel.tenant_id
            or conversation.tenant_id != channel.tenant_id):
        raise ValueError("引用的消息不属于当前账号会话")
    if message.status in {"failed", "revoked", "queued"} or message.external_status == "pending":
        raise ValueError("原消息未确认发送或已撤回，无法引用")
    snapshot = _parse_json(message.raw_request_json)
    native = snapshot.get("native_ids")
    original = server_id(native.get("server")) if isinstance(native, dict) else None
    author = message.sender_id
    if message.direction == "out":
        author = _parse_json(channel.adapter_config_json).get("wxid")
        # 只有明确的服务端编号可引用；clientMsgId、事件键或本地行号不能代替。
        data = _parse_json(message.raw_response_json).get("Data")
        if not original and isinstance(data, dict):
            rows = data.get("List")
            ack = rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else data
            original = server_id(ack.get("NewMsgId") or ack.get("newMsgId"))
    if not original or not author or (author == conversation.external_id and conversation.chat_type == "group"):
        raise ValueError("缺少原消息的微信服务端编号或作者，无法引用")
    if message.message_type != "text":
        raise ValueError("iPad 当前仅支持引用文本消息")
    return {"ReferSvrID": original, "ReferFromUser": conversation.external_id if conversation.chat_type == "group" else author,
            "ReferChatUser": author, "ReferDisplayName": message.sender_name or author,
            "ReferContent": message.content, "ReferMsgType": 1}
