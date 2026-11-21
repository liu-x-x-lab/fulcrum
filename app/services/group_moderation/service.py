"""敏感词前置治理：独立于普通规则和插件消费顺序，命中消息停止后续自动处理。"""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import func, select, update

from app.core.tenant_context import require_tenant_id
from app.core.time import now_shanghai
from app.models.entities import Chatroom, ChatroomMember, Conversation, TaskJob
from app.models.group_moderation import GroupModerationViolation as Violation
from app.services.account_identity import account_identity
from app.services.channel_group_operations import remove_group_member
from app.services.group_features.moderation import FEATURE, normalized_text, validate, words
from app.services.group_features.store import get_state, ipad_removal_confirmed
from app.services.group_moderation import notices
from app.services.inbound_policy import expired_message_dispatch, parse_timestamp_value, resolve_event_time, MESSAGE_COMMAND_MAX_AGE
from app.services.self_sent import is_self_sent
from app.services.task_queue import parse_job_payload


def check_message(db, channel, event: dict, *, conversation_id: int | None, message_id: int | None = None,
                  raw_event_id: int | None = None) -> dict | None:
    """None 表示放行；命中返回可审计的阻断结果，任何副作用都留在调用方事务内。"""
    if channel.tenant_id != require_tenant_id():
        raise ValueError("moderation_tenant_mismatch")
    if not channel.is_enabled or is_self_sent(event) or expired_message_dispatch(event, now=now_shanghai()):
        return None
    message = event.get("message") or {}
    if message.get("message_type") != "text":
        return None
    conversation = db.get(Conversation, conversation_id) if conversation_id else None
    if (not conversation or conversation.chat_type != "group" or conversation.tenant_id != channel.tenant_id
            or conversation.channel_id != channel.id or conversation.bot_id != channel.bot_id
            or (event.get("conversation") or {}).get("external_id") != conversation.external_id):
        return None
    room = db.scalar(select(Chatroom).where(
        Chatroom.tenant_id == channel.tenant_id, Chatroom.bot_id == channel.bot_id,
        Chatroom.channel_id == channel.id, Chatroom.external_id == conversation.external_id,
    ).execution_options(populate_existing=True))
    if room is None or not room.is_active or room.status == "left" or room.management_status == "archived":
        return None
    enabled, config = get_state(room, FEATURE)
    if not enabled:
        return None
    validate(config)
    content = normalized_text(str(message.get("content") or ""))
    matched = next((word for word in words(config) if normalized_text(word) in content), None)
    if not matched:
        return None
    # UPDATE 的真实写锁同时覆盖 SQLite 与 MariaDB；避免只用 SQLite 不支持的 FOR UPDATE。
    # 先锁群再查询台账，同一群并发命中不会丢计数或重复创建踢人任务。
    db.flush()
    db.execute(update(Chatroom).where(Chatroom.id == room.id, Chatroom.tenant_id == channel.tenant_id)
               .values(id=Chatroom.id, updated_at=Chatroom.updated_at), execution_options={"synchronize_session": False})
    db.refresh(room, with_for_update=True)
    enabled, config = get_state(room, FEATURE)
    if not enabled or not room.is_active or room.status == "left" or room.management_status == "archived":
        return None
    validate(config)
    matched = next((word for word in words(config) if normalized_text(word) in content), None)
    if not matched:
        return None
    sender = str(message.get("sender_id") or "").strip()
    if channel.channel_type == "wechat_ipad":
        details = event.get("details") or {}
        original = parse_timestamp_value(details.get("timestamp"))
        own_id = account_identity(channel)["external_id"]
        if (not own_id or details.get("to_user_name") != own_id or details.get("from_user_name") != room.external_id
                or details.get("direction") != "in" or details.get("msg_type") != 1
                or original is None or not 0 <= (now_shanghai() - original).total_seconds() <= MESSAGE_COMMAND_MAX_AGE.total_seconds()
                or sender.endswith("@chatroom") or "," in sender):
            return {"blocked": True, "reason": "unverified_ipad_message"}
    if not sender or len(sender) > 128:
        return {"blocked": True, "reason": "missing_sender"}
    member = db.scalar(select(ChatroomMember).where(
        ChatroomMember.tenant_id == channel.tenant_id, ChatroomMember.chatroom_id == room.id,
        ChatroomMember.channel_id == channel.id, ChatroomMember.bot_id == channel.bot_id,
        ChatroomMember.member_external_id == sender,
    ).with_for_update().execution_options(populate_existing=True))
    exempt = set(str(config["exempt_members"]).splitlines())
    if (sender in {item.strip() for item in exempt} or sender == account_identity(channel)["external_id"]
            or sender == room.owner_external_id or member and member.role in {"owner", "admin"}):
        return None
    identity = message.get("external_message_id") or event.get("event_key")
    # 某些渠道消息号只在发送者内唯一，成员 ID 一并纳入去重范围。
    identity = f"external:{sender}:{identity}" if identity else (f"message:{message_id}" if message_id else f"raw:{raw_event_id}" if raw_event_id else "")
    if not identity:
        return {"blocked": True, "reason": "missing_event_identity"}
    key = hashlib.sha256(identity.encode()).hexdigest()
    existing = db.scalar(select(Violation).where(
        Violation.tenant_id == channel.tenant_id, Violation.chatroom_id == room.id, Violation.event_key == key,
    ).with_for_update())
    if existing:
        return {"blocked": True, "violation_id": existing.id, "count": existing.strike_count, "deduplicated": True}
    current = now_shanghai()
    query = select(func.count(Violation.id)).where(
        Violation.tenant_id == channel.tenant_id, Violation.chatroom_id == room.id,
        Violation.member_external_id == sender,
    )
    if config["window_hours"]:
        query = query.where(Violation.created_at >= current - timedelta(hours=config["window_hours"]))
    # MariaDB 默认可重复读：锁内计数必须是当前读，不能沿用锁前查询形成的旧快照。
    count = int(db.scalar(query.with_for_update()) or 0) + 1
    label = (member.display_name or member.nickname) if member else message.get("sender_name")
    values = dict(member=label or sender, count=count, limit=config["threshold"], group=room.name or room.external_id)
    violation = Violation(tenant_id=channel.tenant_id, bot_id=channel.bot_id, channel_id=channel.id,
                          chatroom_id=room.id, conversation_id=conversation.id, member_external_id=sender,
                          event_key=key, matched_word=matched, strike_count=count, status="warning",
                          kick_notice=notices.render(config["kick_text"], **values), created_at=current)
    db.add(violation)
    db.flush()
    removal_enabled = config["mode"] == "kick" and (channel.channel_type != "wechat_ipad" or ipad_removal_confirmed(room))
    if removal_enabled and count >= config["threshold"]:
        pending = db.scalar(select(Violation.id).where(
            Violation.tenant_id == channel.tenant_id, Violation.chatroom_id == room.id,
            Violation.member_external_id == sender, Violation.status.in_(("pending", "unknown")),
        ).limit(1).with_for_update())
        if pending:
            violation.result_note = "已有踢人请求待确认，请先核实执行结果"
        else:
            result = remove_group_member(db, channel=channel, room=room, member_id=sender,
                                         request_id=f"moderation_{violation.id}")
            if result.get("ok") and result.get("status") == "pending" and result.get("job_id"):
                job = db.get(TaskJob, int(result["job_id"]))
                job.related_type, job.related_id, job.max_attempts = "group_moderation", violation.id, 1
                # 手机离线排队不得刷新处罚时效，原始消息时间沿用入站总闸规则。
                payload = parse_job_payload(job)
                payload["moderation_expires_at"] = (resolve_event_time(event) + MESSAGE_COMMAND_MAX_AGE).isoformat()
                job.payload_json = json.dumps(payload, ensure_ascii=False)
                violation.action_job_id, violation.status = job.id, "pending"
            else:
                violation.status = "failed"
                notices.record_failure(db, violation, str(result.get("error_message") or "踢人未完成"))
    # 阈值之前及执行失败时仍可警告；成功提示只能由结果回报阶段生成。
    if violation.status != "pending":
        notices.send(db, channel, conversation, violation, notices.render(config["warning_text"], **values), "warning")
    return {"blocked": True, "violation_id": violation.id, "count": count, "status": violation.status}
