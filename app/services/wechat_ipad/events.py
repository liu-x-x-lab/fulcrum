"""消息事件识别与群同步信号提取，不执行同步或写入。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Channel, Chatroom
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.transport import logger


def _extract_message_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return WechatIpadApiClient.extract_sync_fields(payload)["list"]


def should_sync_wechat_ipad_chatroom_for_event(
    db: Session,
    channel: Channel,
    normalized: dict[str, Any],
    *,
    entity_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    signal = _build_chatroom_sync_signal(db, channel, normalized, entity_state=entity_state)
    if signal:
        logger.info(
            "wechat ipad chatroom sync signal matched",
            extra={
                "channel_id": channel.id,
                "chatroom_id": signal.get("chatroom_id"),
                "chatroom_ids": signal.get("chatroom_ids") or [],
                "reason": signal.get("reason"),
                "event_type": signal.get("event_type"),
                "message_type": signal.get("message_type"),
                "event_key": signal.get("event_key"),
            },
        )
    else:
        conversation_payload = normalized.get("conversation") or {}
        message_payload = normalized.get("message") or {}
        details = normalized.get("details") or {}
        logger.info(
            "wechat ipad chatroom sync signal skipped",
            extra={
                "channel_id": channel.id,
                "chatroom_id": str(conversation_payload.get("external_id") or "").strip(),
                "chat_type": str(conversation_payload.get("chat_type") or ""),
                "event_type": str(normalized.get("event_type") or ""),
                "message_type": str(message_payload.get("message_type") or ""),
                "event_key": str(normalized.get("event_key") or ""),
                "direction": str(details.get("direction") or ""),
                "reason": "no_match",
            },
        )
    return signal


def _build_chatroom_sync_signal(
    db: Session,
    channel: Channel,
    normalized: dict[str, Any],
    *,
    entity_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    conversation_payload = normalized.get("conversation") or {}
    message_payload = normalized.get("message") or {}
    details = normalized.get("details") or {}
    chatroom_ids = _extract_chatroom_sync_target_ids(normalized)
    if not chatroom_ids:
        return None
    chatroom_id = chatroom_ids[0]

    chatroom = db.scalar(
        select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == chatroom_id)
    )
    message_type = str(message_payload.get("message_type") or "").strip().lower()
    content = str(message_payload.get("content") or "").strip()
    event_type = str(normalized.get("event_type") or "").strip()
    state = entity_state if isinstance(entity_state, dict) else {}
    cache_chatroom_known = bool(state.get("cache_chatroom_known"))
    cache_member_known = bool(state.get("cache_member_known"))
    chatroom_created = bool(state.get("chatroom_created"))
    member_created = bool(state.get("member_created"))

    reason = ""
    if _is_wechat_ipad_member_change_event(
        event_type=event_type,
        message_type=message_type,
        content=content,
    ):
        reason = "member_change_event"
    elif not cache_chatroom_known or chatroom_created or not chatroom:
        reason = "new_chatroom_observed"
    elif not cache_member_known or member_created:
        reason = "member_not_cached"
    elif str(chatroom.status or "").strip() in {"message_observed", "pending_sync"}:
        reason = "chatroom_profile_incomplete"

    if not reason:
        return None

    return {
        "chatroom_id": chatroom_id,
        "chatroom_ids": chatroom_ids,
        "reason": reason,
        "event_key": str(normalized.get("event_key") or "").strip() or "-",
        "message_type": message_type or "unknown",
        "sender_id": str(message_payload.get("sender_id") or "").strip() or "-",
        "content_preview": _build_chatroom_signal_preview(content),
        "status_before_sync": str(getattr(chatroom, "status", None) or "missing"),
        "direction": str(details.get("direction") or "in"),
        "event_type": event_type or "message",
        "cache_chatroom_known": "true" if cache_chatroom_known else "false",
        "cache_member_known": "true" if cache_member_known else "false",
    }


def _extract_chatroom_sync_target_ids(normalized: dict[str, Any]) -> list[str]:
    conversation_payload = normalized.get("conversation") or {}
    details = normalized.get("details") or {}
    targets: list[str] = []
    seen: set[str] = set()

    conversation_chatroom_id = str(conversation_payload.get("external_id") or "").strip()
    if (
        str(conversation_payload.get("chat_type") or "").strip() == "group"
        and conversation_chatroom_id.endswith("@chatroom")
        and conversation_chatroom_id not in seen
    ):
        seen.add(conversation_chatroom_id)
        targets.append(conversation_chatroom_id)

    mod_contacts_ids = details.get("mod_contacts_chatroom_ids")
    if isinstance(mod_contacts_ids, list):
        for item in mod_contacts_ids:
            chatroom_id = str(item or "").strip()
            if not chatroom_id or not chatroom_id.endswith("@chatroom") or chatroom_id in seen:
                continue
            seen.add(chatroom_id)
            targets.append(chatroom_id)

    return targets


def _is_wechat_ipad_member_change_event(*, event_type: str, message_type: str, content: str) -> bool:
    normalized_event_type = event_type.strip().lower()
    normalized_content = content.strip().lower()
    if normalized_event_type in {"modcontacts", "mod_contacts", "sync_push", "sync_callback", "sync_push_callback"}:
        return True
    if "modcontacts" in normalized_event_type:
        return True
    if message_type != "system":
        return False
    if "<sysmsg" in normalized_content or "<memberlist" in normalized_content or "sysmsgtemplate" in normalized_content:
        return True
    for keyword in (
        "加入了群聊",
        "加入群",
        "邀请",
        "移出群聊",
        "移出了群聊",
        "踢出群聊",
        "removed from the group",
        "joined the group",
        "invited",
        "scan the qr code to join",
    ):
        if keyword in normalized_content:
            return True
    return False


def _build_chatroom_signal_preview(content: str) -> str:
    cleaned = " ".join(str(content or "").split())
    if len(cleaned) <= 80:
        return cleaned or "-"
    return f"{cleaned[:77]}..."


def _extract_cache_summary(cache_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "wxid": cache_data.get("Wxid") or cache_data.get("UserName"),
        "nickname": cache_data.get("NickName") or cache_data.get("nickname"),
        "device_id": cache_data.get("DeviceID"),
    }
