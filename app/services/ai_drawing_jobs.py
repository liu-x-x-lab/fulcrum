"""原生绘图事务边界：精确引用、唯一收费尝试、无会话调用、统一出站。"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import AiImageAttempt, Channel, Conversation, Message, TaskJob, TenantAiSetting
from app.services import ai_dispatch, ai_drawing, ai_media
from app.services.inbound_policy import expired_message_dispatch
from app.services.plugin_runtime.replies import image_reply
from app.services.self_sent import is_self_sent
from app.services.task_queue import complete_job, fail_job_terminal


@dataclass(frozen=True)
class DrawingInput:
    request: ai_drawing.ImageRequest
    config: ai_drawing.ImageConfig | None
    media: ai_media.MediaInput | None = None
    error: str = ""


def _owned_message(db: Session, payload: dict):
    """既校验租户，也校验机器人、渠道、会话和发送人，不信任务里的文件路径。"""

    channel = db.get(Channel, int(payload.get("channel_id") or 0))
    conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
    current = db.get(Message, int(payload.get("message_id") or 0))
    event = payload.get("normalized_event") or {}
    chat = event.get("conversation") or {}
    message = event.get("message") or {}
    if (channel is None or not channel.is_enabled or channel.deleted_at or not channel.bot or not channel.bot.is_enabled
        or channel.bot.tenant_id != channel.tenant_id
        or conversation is None or conversation.tenant_id != channel.tenant_id or conversation.channel_id != channel.id
        or conversation.bot_id != channel.bot_id or conversation.chat_type != chat.get("chat_type")
        or conversation.external_id != chat.get("external_id") or current is None or current.tenant_id != channel.tenant_id
        or current.conversation_id != conversation.id or current.direction != "in" or current.message_type not in {"text", "quote"}
        or current.message_type != message.get("message_type")
        or not current.sender_id or current.sender_id != message.get("sender_id")
        or current.status == "recalled" or current.external_status == "recalled"
        or current.content != message.get("content") or is_self_sent(event) or expired_message_dispatch(event)):
        raise ai_drawing.DrawingError("绘图请求已失效或无法核对会话归属")
    return channel, conversation, current


def _resolution(db, channel, conversation):
    return ai_dispatch.resolve_for_conversation(db, channel_id=channel.id, chat_type=conversation.chat_type,
                                               conversation_external_id=conversation.external_id)


def prepare(db: Session, payload: dict, *, allow_media_request: bool = True) -> DrawingInput | None:
    event = payload.get("normalized_event") or {}
    message = event.get("message") or {}
    request = ai_drawing.parse_request(message.get("content"), quoted=message.get("message_type") == "quote")
    if request is None:
        return None
    channel, conversation, current = _owned_message(db, payload)
    if not ai_dispatch.should_trigger(conversation.chat_type, bool((event.get("details") or {}).get("mentioned_self")), current.content):
        return None
    resolved = _resolution(db, channel, conversation)
    if resolved is None or not resolved.chat_enabled:
        return None
    if resolved.image_config is None:
        return DrawingInput(request, None, error="本群未允许绘图，或尚未启用并选择可用的绘图供应商和模型")
    if not request.prompt or len(request.prompt) > ai_drawing.MAX_PROMPT_LENGTH:
        return DrawingInput(request, resolved.image_config, error="请提供 1 到 4000 字的图片描述或修改要求")
    media = None
    if request.mode == "edit":
        # 出站图片同样按原消息编号从现有下载链取回；不读取响应里的任意 URL。
        media = ai_media.prepare_input(db, channel, payload, enabled=True, allow_request=allow_media_request,
                                      allow_video=False, allow_outgoing=True)
        if media is None:
            return DrawingInput(request, resolved.image_config, error="请引用本会话中的原图片，再说「改图 修改要求」")
    return DrawingInput(request, resolved.image_config, media)


def _reserve(db: Session, payload: dict, drawing: DrawingInput) -> tuple[AiImageAttempt | None, ai_drawing.ImageConfig]:
    # 先写锁再读取：MariaDB 可重复读事务若先查询，会用旧快照误算并发额度。
    db.execute(update(TenantAiSetting).values(image_daily_limit=TenantAiSetting.image_daily_limit))
    channel, conversation, current = _owned_message(db, payload)
    resolved = _resolution(db, channel, conversation)
    if resolved is None or not resolved.chat_enabled or resolved.image_config is None:
        raise ai_drawing.DrawingError("绘图已关闭或供应商不可用，未调用模型")
    if drawing.media:
        source = db.get(Message, drawing.media.message_id)
        if source is None or source.conversation_id != conversation.id or source.status == "recalled" or source.external_status == "recalled":
            raise ai_drawing.DrawingError("原图片已撤回或失效，未调用模型")
    existing = db.scalar(select(AiImageAttempt).where(AiImageAttempt.message_id == current.id))
    if existing is not None:
        return None, resolved.image_config
    start = now_shanghai().replace(hour=0, minute=0, second=0, microsecond=0)
    used = db.scalar(select(func.count(AiImageAttempt.id)).where(AiImageAttempt.created_at >= start)) or 0
    if used >= max(0, min(int(resolved.setting.image_daily_limit or 0), 200)):
        raise ai_drawing.DrawingError("今日绘图次数已用完，未调用模型")
    attempt = AiImageAttempt(channel_id=channel.id, conversation_id=conversation.id, message_id=current.id,
                             source_message_id=drawing.media.message_id if drawing.media else None, status="started")
    db.add(attempt)
    db.flush()
    return attempt, resolved.image_config


def execute(job_id: int, payload: dict, drawing: DrawingInput, *, session_factory) -> None:
    """有副作用的调用最多一次；收到图片后只让现有出站队列负责发送重试。"""

    attempt_id, result, reference = None, None, None
    error = drawing.error
    if not error and drawing.media:
        if drawing.media.state != "ready":
            error = drawing.media.error or "原图片尚未获取完成，请重新发送后引用"
        else:
            try:
                reference = ai_drawing.normalize_image(ai_media.read_media(drawing.media, max_bytes=ai_drawing.MAX_IMAGE_BYTES), reference=True)
            except ValueError as exc:
                error = str(exc)
    if (payload.get("normalized_event", {}).get("details") or {}).get("dry_run_outbound"):
        error = "演练模式不调用收费绘图接口"
    if not error:
        try:
            with tenant_session(session_factory) as db:
                attempt, config = _reserve(db, payload, drawing)
                if attempt is None:
                    complete_job(db, db.get(TaskJob, job_id), result={"status": "skipped", "reason": "image_attempt_exists"})
                    return
                attempt_id = attempt.id
                db.commit()
            result = ai_drawing.generate(config, drawing.request.prompt, reference=reference)
        except ai_drawing.DrawingError as exc:
            error = str(exc)
        except Exception:
            # 供应商调用或结果处理的未知异常可能发生在扣费后，不能通过队列恢复重画。
            error = "绘图结果不确定，可能已产生费用；未自动重试"
    with tenant_session(session_factory) as db:
        job = db.get(TaskJob, job_id)
        if job is None:
            return
        attempt = db.get(AiImageAttempt, attempt_id) if attempt_id else None
        try:
            channel, conversation, current = _owned_message(db, payload)
            if drawing.media and drawing.media.message_id:
                source = db.get(Message, drawing.media.message_id)
                if source is None or source.conversation_id != conversation.id or source.status == "recalled" or source.external_status == "recalled":
                    raise ai_drawing.DrawingError("原图片已撤回或失效")
            resolved = _resolution(db, channel, conversation)
            if resolved is None or not resolved.chat_enabled or result is not None and resolved.image_config is None:
                raise ai_drawing.DrawingError("绘图权限已改变")
        except ai_drawing.DrawingError:
            if attempt:
                attempt.status = "cancelled"
            complete_job(db, job, result={"status": "skipped", "reason": "image_scope_changed"})
            return
        try:
            from app.services.plugin_directory import send_ai_reply

            status = "image_error" if error else ("image_edited" if drawing.request.mode == "edit" else "image_generated")
            if attempt:
                attempt.status = "error" if error else "queued"
            ai_dispatch.record_decision(db, channel_id=channel.id, conversation_id=conversation.id,
                sender_id=current.sender_id, text=current.content,
                intent=ai_dispatch.AiIntent(status=status, reason=error or "图片已进入发送队列"))
            replies = [image_reply(base64_data=base64.b64encode(result).decode("ascii"))] if result and not error else None
            receipts = send_ai_reply(db, channel, payload["normalized_event"], error,
                                    conversation_id=conversation.id, replies=replies,
                                    media_origin="ai_edited" if drawing.request.mode == "edit" else "ai_generated",
                                    source_message_id=current.id)
            if not receipts or not all(item.get("ok") for item in receipts):
                raise ValueError("image_delivery_failed")
            complete_job(db, job, result={"status": status, "attempt_id": attempt_id, "queued": True})
        except Exception:
            db.rollback()
            if attempt_id:
                attempt = db.get(AiImageAttempt, attempt_id)
                attempt.status = "delivery_failed"
            fail_job_terminal(db, db.get(TaskJob, job_id), error_message="绘图回复未能确认入队，未重新调用模型")
