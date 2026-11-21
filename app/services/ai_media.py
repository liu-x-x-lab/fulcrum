"""AI 引用媒体的归属核对及按需下载；不按最近一张图片猜测引用对象。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.entities import Channel, Conversation, Message, MessageAttachment
from app.services import message_media


@dataclass(frozen=True)
class MediaInput:
    """仅从归属已核对的归档生成；文件读取留到数据库会话关闭之后。"""

    media_type: str
    state: str = "error"
    error: str = ""
    message_id: int = 0
    attachment_id: int = 0
    channel_id: int = 0
    storage_path: str = field(default="", repr=False)


def _snapshot(message: Message) -> dict:
    try:
        value = json.loads(message.raw_request_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _reference_type(quote: dict) -> str:
    try:
        return {3: "image", 43: "video"}.get(int(quote.get("ref_type") or 0), "")
    except (TypeError, ValueError):
        return ""


def prepare_input(db: Session, channel: Channel, payload: dict, *, enabled: bool, allow_request: bool = True,
                  allow_video: bool = True, allow_outgoing: bool = False) -> MediaInput | None:
    """短事务内定位原媒体并入队下载；事件只能提示媒体类型，不能指定文件路径或附件归属。"""

    event = payload.get("normalized_event") or {}
    details = event.get("details") or {}
    message_event = event.get("message") or {}
    current = db.scalar(select(Message).join(Conversation).where(
        Message.id == payload.get("message_id"), Message.tenant_id == channel.tenant_id,
        Message.conversation_id == payload.get("conversation_id"), Message.direction == "in",
        Message.sender_id == message_event.get("sender_id"), Message.status != "recalled",
        Conversation.tenant_id == channel.tenant_id, Conversation.channel_id == channel.id,
        Conversation.bot_id == channel.bot_id,
    ))
    quote = _snapshot(current).get("quote", {}) if current else details.get("quote_payload", {})
    quote = quote if isinstance(quote, dict) else {}
    media_type = _reference_type(quote)
    if not media_type:
        return None
    if current is None:
        return MediaInput(media_type, error="无法核对这条引用的会话归属，请重新引用后提问")
    if current.external_status == "recalled":
        return MediaInput(media_type, error="引用消息已撤回，不能继续读取")
    if media_type == "video" and not allow_video:
        return MediaInput(media_type, error="改图只支持引用静态图片，不支持修改视频")
    if not enabled:
        return MediaInput(media_type, error="本群未允许视觉理解，或尚未启用并完整配置独立视觉模型")
    reference = str(quote.get("ref_external_id") or "").strip()
    if not reference or reference == "0" or len(reference) > 128:
        return MediaInput(media_type, error="引用中缺少原媒体消息编号，请重新引用原图片或视频")
    query = select(Message).where(
        Message.tenant_id == channel.tenant_id, Message.conversation_id == current.conversation_id,
        Message.message_type == media_type, Message.direction.in_(("in", "out") if allow_outgoing else ("in",)),
        or_(Message.external_message_id == reference, Message.raw_request_json.contains(reference, autoescape=True)),
    )
    originals = []
    for candidate in db.scalars(query).yield_per(100):
        native = _snapshot(candidate).get("native_ids") or {}
        server_id = str(native.get("server") or "") if isinstance(native, dict) else ""
        if server_id == reference or (not server_id and str(candidate.external_message_id or "") == reference):
            originals.append(candidate)
            if len(originals) > 1:
                break
    if len(originals) != 1:
        return MediaInput(media_type, error="找不到唯一的原媒体记录，请在本群重新发送图片或视频后引用")
    original = originals[0]
    if original.status == "recalled" or original.external_status == "recalled":
        return MediaInput(media_type, error="原媒体已撤回，不能继续读取")
    state = message_media.status(db, original)
    if state["state"] in {"none", "failed"} and allow_request:
        state = message_media.request(db, original)
    if state["state"] == "loading":
        return MediaInput(media_type, state="loading", message_id=original.id)
    if state["state"] != "ready":
        return MediaInput(media_type, error="原媒体获取失败或已过期，请确认手机在线，或重新发送后引用")
    attachment = db.scalar(select(MessageAttachment).where(
        MessageAttachment.id == state.get("attachment_id"), MessageAttachment.message_id == original.id,
        MessageAttachment.tenant_id == channel.tenant_id, MessageAttachment.channel_id == channel.id,
        MessageAttachment.conversation_id == current.conversation_id, MessageAttachment.direction == "in",
        MessageAttachment.attachment_type == media_type, MessageAttachment.status == "downloaded",
    ))
    if attachment is None:
        return MediaInput(media_type, error="无法核对媒体附件归属，请重新发送后引用")
    return MediaInput(media_type, state="ready", message_id=original.id, attachment_id=attachment.id,
                      channel_id=channel.id, storage_path=attachment.storage_path or "")


def read_media(media: MediaInput, *, max_bytes: int) -> bytes:
    """只读平台对应附件文件；拒绝 URL、任意路径、符号链接和跨附件读取。"""

    from app.services.ai_vision import VisionError

    try:
        supplied = Path(media.storage_path)
        if not media.storage_path or supplied.is_symlink():
            raise ValueError
        root = (Path(settings.data_dir) / "inbound_media" / f"channel_{media.channel_id}").resolve(strict=True)
        path = supplied.resolve(strict=True)
        if not path.is_relative_to(root) or path.stem != f"attachment_{media.attachment_id}" or not path.is_file():
            raise ValueError
        if not 0 < path.stat().st_size <= max_bytes:
            raise VisionError(f"媒体为空或超过 {max_bytes // (1024 * 1024)} MiB，请缩小后重新发送")
        with path.open("rb") as source:
            content = source.read(max_bytes + 1)
        if not 0 < len(content) <= max_bytes:
            raise ValueError
        return content
    except VisionError:
        raise
    except (OSError, ValueError):
        raise VisionError("媒体文件已失效或不属于当前附件，请重新发送后引用") from None
