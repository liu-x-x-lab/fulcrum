"""Translate bounded, conversation-scoped WeChat data to and from MaiM segments."""

import base64
import binascii
import re
import struct
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

from sqlalchemy import select

from app.core.settings import settings
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, Message, MessageAttachment, TaskJob, WorkWechatContact
from app.services.account_identity import account_identity
from app.services.maibot_config import connection_owner, parse_config
from app.services.message_actions import capabilities, quote_payload_for
from app.services.message_view import quote_view
from app.services.outbound_files import store_outbound_upload
from app.services.task_queue import MAIBOT_MEDIA_FORWARD_JOB, enqueue_job

WECHAT_CHANNELS = {"wechat_webridge", "wechat_gugatool", "wechat_ipad"}
MEDIA_KINDS = {"image", "emoji", "voice"}
MAX_MEDIA_BYTES = 1024 * 1024  # Leave room for base64, text and routing within the 2 MiB WebSocket frame.
MAX_MENTIONS = 20
MEDIA_WAIT_SECONDS = 22
ALL_IDS = {"all", "here", "*", "@all", "notify@all"}
AT_SOURCE = re.compile(r"<atuserlist>(?:<!\[CDATA\[)?([^<]{0,3000}?)(?:\]\]>)?</atuserlist>", re.I)


def identifier(value):
    if type(value) is int:
        value = str(value)
    if isinstance(value, str) and 0 < len(value) <= 128 and not re.search(r"[\x00-\x20\x7f{}]", value):
        return value
    return ""


def name(value, user_id):
    text = " ".join(str(value or "").split())[:128]
    return text if text and text != user_id and not text.startswith("wxid_") else ""


def mention_ids(value):
    if not isinstance(value, (list, tuple)) or len(value) > MAX_MENTIONS:
        return []
    return list(dict.fromkeys(target for item in value if (target := identifier(item))))


def source_mentions(source):
    match = AT_SOURCE.search(str(source or "")[:8192])
    return mention_ids([item.strip() for item in match.group(1).split(",")]) if match else []


def room_members(db, channel, conversation, ids):
    if conversation.chat_type != "group" or not ids:
        return {}
    rows = db.scalars(select(ChatroomMember).join(Chatroom, Chatroom.id == ChatroomMember.chatroom_id).where(
        Chatroom.tenant_id == channel.tenant_id, Chatroom.channel_id == channel.id, Chatroom.bot_id == channel.bot_id,
        Chatroom.external_id == conversation.external_id, Chatroom.is_active.is_(True), Chatroom.status != "left",
        ChatroomMember.tenant_id == channel.tenant_id, ChatroomMember.channel_id == channel.id,
        ChatroomMember.bot_id == channel.bot_id, ChatroomMember.status == "active", ChatroomMember.member_external_id.in_(ids),
    ))
    return {row.member_external_id: row for row in rows}


def bridge_account(channel):
    identity = account_identity(channel)
    runtime = parse_config(channel.runtime_info_json)
    adapter = parse_config(channel.adapter_config_json)
    profile = runtime.get("guga_agent")
    account = identity["external_id"] or (profile.get("account_id") if isinstance(profile, dict) else None) or adapter.get("wxid")
    return identifier(account), name(identity["name"], str(account or ""))


def inbound_context(db, channel, conversation, message, text, platform, mentioned_self):
    snapshot = parse_config(message.raw_request_json)
    sender = str(message.sender_id or "")
    own_id, own_name = bridge_account(channel)
    group = conversation.chat_type == "group"
    targets = mention_ids(snapshot.get("mentions")) if group else []
    if own_id and sender == own_id:
        return {"self_sent": True}
    if targets:
        mentioned_self = bool(own_id and own_id in targets)
    members = room_members(db, channel, conversation, {*targets, sender, own_id} - {"", *ALL_IDS})
    sender_member = members.get(sender)
    self_member = members.get(own_id)
    sender_name = name(message.sender_name, sender)
    card = ""
    if sender_member is not None:
        sender_name = sender_name or name(sender_member.nickname, sender)
        card = name(sender_member.display_name, sender)
    if not group and not sender_name:
        friend = db.scalar(select(WorkWechatContact).where(
            WorkWechatContact.tenant_id == channel.tenant_id, WorkWechatContact.channel_id == channel.id,
            WorkWechatContact.contact_kind == "friend", WorkWechatContact.contact_external_id == sender,
            WorkWechatContact.sync_status != "deleted",
        ).order_by(WorkWechatContact.id.desc()).limit(1))
        if friend is not None:
            sender_name = name(friend.name, sender) or name(friend.display_name, sender)
            card = name(friend.display_name, sender)
    own_name = own_name or (name(self_member.nickname, own_id) if self_member is not None else "")
    own_card = name(self_member.display_name, own_id) if self_member is not None else ""
    segments = []
    for target in targets:
        if target.lower() in ALL_IDS:
            continue
        row = members.get(target)
        nickname = own_name if target == own_id else name(row.nickname, target) if row else ""
        cardname = own_card if target == own_id else name(row.display_name, target) if row else ""
        data = {"target_user_id": target if target == own_id else f"mbs-user-{channel.id}-{target}"}
        if nickname:
            data["target_user_nickname"] = nickname
        if cardname:
            data["target_user_cardname"] = cardname
        segments.append({"type": "at", "data": data})
    # Strip only full, verified mentions. Any other prose or unknown name stays untouched.
    if targets and len(segments) == len(targets) and len(targets) <= 8:
        offsets = {0}
        for index, segment in enumerate(segments):
            data = segment["data"]
            names = {data.get("target_user_nickname"), data.get("target_user_cardname")} - {None}
            following = set()
            for offset in offsets:
                tail = text[offset:]
                trimmed = tail.lstrip()
                if index and trimmed == tail:
                    continue
                start = len(text) - len(trimmed)
                following.update(start + 1 + len(value) for value in names if trimmed.startswith("@" + value))
            offsets = following
        if any(not text[offset:].strip() for offset in offsets):
            text = ""
    quoted = snapshot.get("quote")
    if isinstance(quoted, dict):
        repr_quote = quote_view(quoted)
        if repr_quote["refer_text"]:
            text += f'\n[引用 {repr_quote["refer_name"][:128]}：{repr_quote["refer_text"]}]'
    elif quote_id := identifier(snapshot.get("quote_id")):
        original = conversation_message(db, conversation, quote_id)
        if original is not None and original.status not in {"revoked", "failed"}:
            excerpt = str(original.content or "")[:200] if original.message_type == "text" else "[媒体消息]"
            text += f'\n[引用 {name(original.sender_name, original.sender_id)}：{excerpt}]'
    media = inbound_media(db, channel, conversation, message) if message.message_type in MEDIA_KINDS else None
    if media:
        segments.append(media)
    elif text:
        segments.append({"type": "text", "data": text})
    identity = {"platform": platform, "account_id": own_id, "group_id": f"mbs-conversation-{conversation.id}" if group else None,
                "nickname": own_name or None, "cardname": own_card or None} if own_id and (own_name or own_card) else None
    return {"account_id": own_id, "bot_identity": identity, "nickname": sender_name or sender,
            "cardname": card or sender_name or sender, "segments": segments, "mentioned_self": mentioned_self, "media_ready": bool(media)}


def conversation_message(db, conversation, value):
    if value.startswith("mbs-message-"):
        suffix = value.removeprefix("mbs-message-")
        predicate = Message.id == int(suffix) if suffix.isascii() and suffix.isdecimal() and len(suffix) <= 10 else None
        if predicate is None:
            return None
    else:
        predicate = Message.external_message_id == value
    return db.scalar(select(Message).where(Message.tenant_id == conversation.tenant_id, Message.conversation_id == conversation.id,
                                          predicate).order_by(Message.id.desc()).limit(1))


def media_format(data, kind):
    if not 0 < len(data) <= MAX_MEDIA_BYTES:
        return None
    if kind == "voice":
        if len(data) <= 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return None
        total = struct.unpack_from("<I", data, 4)[0]
        if total not in {len(data) - 8, 0xffffffff}:
            return None
        offset, frame, audio_size = 12, None, 0
        normalized = None
        while offset + 8 <= len(data):
            tag = data[offset:offset + 4]
            size = struct.unpack_from("<I", data, offset + 4)[0]
            start = offset + 8
            if tag == b"data" and total == size == 0xffffffff:
                size = len(data) - start
                normalized = bytearray(data)
                struct.pack_into("<I", normalized, 4, len(data) - 8)
                struct.pack_into("<I", normalized, offset + 4, size)
            if start + size > len(data):
                return None
            if tag == b"fmt ":
                if frame or size < 16:
                    return None
                codec, channels, rate, bytes_per_second, align, bits = struct.unpack_from("<HHIIHH", data, start)
                if codec != 1 or bits != 16 or channels not in {1, 2} or not 8000 <= rate <= 48000 or align != channels * 2 or bytes_per_second != rate * align:
                    return None
                frame = (rate, align)
            elif tag == b"data":
                if audio_size or not size:
                    return None
                audio_size = size
            offset = start + size + size % 2
        if offset == len(data) and frame and audio_size and audio_size % frame[1] == 0 and audio_size <= frame[0] * frame[1] * 60:
            return bytes(normalized) if normalized is not None else data, "wav", "audio/wav"
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return data, "jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return data, "gif", "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return data, "webp", "image/webp"
    return None


def inbound_media(db, channel, conversation, message):
    attachment = db.scalar(select(MessageAttachment).where(
        MessageAttachment.tenant_id == channel.tenant_id, MessageAttachment.channel_id == channel.id,
        MessageAttachment.conversation_id == conversation.id, MessageAttachment.message_id == message.id,
        MessageAttachment.direction == "in", MessageAttachment.status == "downloaded",
        MessageAttachment.attachment_type == message.message_type,
    ).order_by(MessageAttachment.id.desc()).limit(1))
    if attachment is None or not attachment.storage_path:
        return None
    root = (Path(settings.data_dir) / "inbound_media" / f"channel_{channel.id}").resolve()
    try:
        candidate = Path(attachment.storage_path).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file() or candidate.stat().st_size > MAX_MEDIA_BYTES:
            return None
        with candidate.open("rb") as file:
            media = media_format(file.read(MAX_MEDIA_BYTES + 1), message.message_type)
    except (ValueError, OSError, RuntimeError):
        return None
    return {"type": message.message_type, "data": base64.b64encode(media[0]).decode("ascii")} if media else None


def media_source(kind, data):
    if not isinstance(data, str) or len(data) > 4 * ((MAX_MEDIA_BYTES + 2) // 3) + 64:
        return None
    encoded = re.sub(r"^data:(?:image/(?:jpe?g|png|gif|webp)|audio/(?:wav|wave|x-wav));base64,", "", data, flags=re.I)
    try:
        return media_format(base64.b64decode(encoded, validate=True), kind)
    except (ValueError, binascii.Error):
        return None


def outbound_formats(channel, conversation):
    supported = capabilities(channel, conversation)
    kinds = [kind for kind in ("image", "emoji", "voice") if supported[kind] and (kind != "emoji" or channel.channel_type != "wechat_ipad")]
    if channel.channel_type == "wechat_webridge" and not _public_base():
        kinds = []
    return ["text", "seglist", *kinds, *(["at"] if supported["mention"] else []), *(["reply"] if supported["quote"] else [])]


def _public_base():
    base = str(settings.public_base_url or "").strip().rstrip("/")
    try:
        url = urlsplit(base)
        return base if url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password and not url.query and not url.fragment else ""
    except ValueError:
        return ""


def outbound_media_payload(channel, kind, media):
    data, extension, mime = media
    if channel.channel_type != "wechat_webridge":
        return {"message_type": kind, "base64": base64.b64encode(data).decode("ascii"), "file_name": f"maibot.{extension}",
                **({"voice_format": "wav"} if kind == "voice" else {})}
    stored = store_outbound_upload(tenant_id=channel.tenant_id, channel_id=channel.id, file_name=f"maibot.{extension}",
                                   content=data, content_type=mime, message_type=kind)
    url = f"{_public_base()}/outbound-files/{channel.id}/{stored['share_id']}/{quote(stored['file_name'])}"
    return {"message_type": kind, "image_url" if kind == "image" else "voice_url" if kind == "voice" else "emoji": url}


def checked_quote(db, channel, conversation, value):
    target = conversation_message(db, conversation, value)
    if target is not None:
        try:
            return quote_payload_for(db, channel, target)
        except ValueError:
            pass
    return {}


def queue_media_forward(db, trusted, connection, mentioned_self):
    """Wait for the existing channel download without holding the dispatch worker."""
    channel, message = trusted.channel, trusted.message
    if message.message_type not in MEDIA_KINDS:
        return False
    from app.services import message_media

    dedupe_key = f"maibot-media:{trusted.binding.id}:{message.id}:{connection.get('revision', '')}"
    if db.scalar(select(TaskJob.id).where(TaskJob.dedupe_key == dedupe_key)) is not None:
        return True
    # Let the message center own fetch deduplication; if fetching is unavailable, send the placeholder.
    try:
        with db.begin_nested():
            latest = db.scalar(select(MessageAttachment).where(
                MessageAttachment.message_id == message.id, MessageAttachment.channel_id == channel.id,
                MessageAttachment.conversation_id == trusted.conversation.id, MessageAttachment.direction == "in",
                MessageAttachment.attachment_type == message.message_type,
            ).order_by(MessageAttachment.id.desc()).limit(1))
            if latest is not None and latest.status != "pending":
                return False
            fetch = message_media.status(db, message)
            if fetch["state"] == "none" and channel.channel_type != "wechat_ipad":
                fetch = message_media.request(db, message)
            if fetch["state"] != "loading":
                return False
            attachment_id = fetch["attachment_id"]
            context = trusted.execution
            enqueue_job(db, job_type=MAIBOT_MEDIA_FORWARD_JOB, priority=96, max_attempts=1, dedupe_key=dedupe_key,
                        related_type="message", related_id=message.id, available_at=now_shanghai() + timedelta(seconds=1),
                        payload={"binding_id": trusted.binding.id, "channel_id": channel.id, "message_id": message.id,
                                 "conversation_id": trusted.conversation.id, "attachment_id": attachment_id,
                                 "raw_event_id": context.raw_event_id, "connection_id": connection["id"],
                                 "revision": connection.get("revision"), "mentioned_self": mentioned_self})
    except (OSError, ValueError):
        return False
    return True


def process_media_forward(db, job, payload):
    from app.services.plugin_governance import blocked_plugin_codes
    from app.services.plugin_runtime import execute_binding_message_action
    from app.services.plugin_runtime.contracts import PluginExecutionContext
    from app.services.plugin_scopes import plugin_group_conversation_enabled

    binding = db.get(BotPlugin, payload["binding_id"])
    channel = db.get(Channel, payload["channel_id"])
    conversation = db.get(Conversation, payload["conversation_id"])
    message = db.get(Message, payload["message_id"])
    if (not all((binding, channel, conversation, message)) or binding.status != "enabled"
            or not binding.bot or not binding.bot.is_enabled or binding.bot.deleted_at is not None
            or not binding.plugin or binding.plugin.code != "maibot_adapter" or binding.plugin.missing_at is not None
            or not channel.is_enabled or channel.deleted_at is not None or channel.bot_id != binding.bot_id
            or conversation.channel_id != channel.id or conversation.bot_id != binding.bot_id
            or message.conversation_id != conversation.id or message.direction != "in" or message.message_type not in MEDIA_KINDS
            or "maibot_adapter" in blocked_plugin_codes(db, binding.tenant_id)
            or not plugin_group_conversation_enabled(db, channel=channel, conversation=conversation, plugin_code="maibot_adapter")):
        return {"provider_status": "media_scope_changed"}
    owner = connection_owner(parse_config(binding.config_json), channel_id=channel.id, chat_type=conversation.chat_type,
                             external_id=conversation.external_id)
    if not owner or owner.get("id") != payload["connection_id"] or owner.get("revision") != payload["revision"]:
        return {"provider_status": "media_scope_changed"}
    attachment = db.get(MessageAttachment, payload["attachment_id"])
    now = now_shanghai()
    age = (now - ensure_shanghai_datetime(job.created_at)).total_seconds()
    if age > 120:
        return {"provider_status": "expired"}
    if (attachment and attachment.message_id == message.id and attachment.channel_id == channel.id
            and attachment.status == "pending" and age < MEDIA_WAIT_SECONDS):
        job.status = "pending"
        job.available_at = now + timedelta(seconds=2)
        job.started_at = job.locked_at = None
        job.attempts = max(0, job.attempts - 1)
        db.commit()
        return None
    result = execute_binding_message_action(db, binding, "execute", payload={"mentioned_self": payload["mentioned_self"], "maibot_media_final": True},
        context=PluginExecutionContext(int(binding.tenant_id), int(binding.id), int(binding.bot_id), channel_id=channel.id,
                                      conversation_id=conversation.id, message_id=message.id, raw_event_id=payload.get("raw_event_id"),
                                      sender_id=message.sender_id, channel_type=channel.channel_type, chat_type=conversation.chat_type,
                                      message_type=message.message_type))
    return {"provider_status": result.output.get("provider_status") if result is not None else "unavailable"}


def reply_parts(db, channel, conversation, segment):
    """Preserve mixed reply order and resolve native targets against this chat only."""
    supported = set(outbound_formats(channel, conversation))
    parts, texts, mentions = [], [], []
    quote_fields = {}
    count = 0

    def flush():
        nonlocal quote_fields
        content = ''.join(texts).strip()
        if content:
            # WeBridge also interprets inline placeholders. MaiBot text must not
            # become a native mention without the member lookup below.
            content = re.sub(r"\{\{(?=mention\s*:)", "｛｛", content, flags=re.I)
            fields = {"message_type": "text", "content": content, **quote_fields}
            if mentions and not (quote_fields and channel.channel_type in {"wechat_gugatool", "wechat_ipad"}):
                fields["mention_user_list"] = list(dict.fromkeys(mentions))
                fields["at"] = ','.join(fields["mention_user_list"])
            parts.append(fields)
        texts.clear()
        mentions.clear()
        quote_fields = {}

    def walk(node, depth=0):
        nonlocal count, quote_fields
        count += 1
        if depth > 3 or count > 40 or not isinstance(node, dict):
            return
        kind, data = node.get("type"), node.get("data")
        if not isinstance(kind, str):
            return
        if kind in {"seglist", "seg"} and isinstance(data, list):
            for item in data[:20]:
                walk(item, depth + 1)
        elif kind == "text" and isinstance(data, str):
            texts.append(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", data)[:20_000])
        elif kind in MEDIA_KINDS and kind in supported:
            media = media_source(kind, data)
            if media is not None:
                flush()
                parts.append((kind, media))
        elif kind == "reply" and kind in supported and not quote_fields:
            value = identifier((data.get("id") or data.get("message_id")) if isinstance(data, dict) else data)
            if value:
                quote_fields = checked_quote(db, channel, conversation, value)
        elif kind == "at" and kind in supported and len(mentions) < MAX_MENTIONS:
            target = identifier(data.get("target_user_id") if isinstance(data, dict) else data)
            prefix = f"mbs-user-{channel.id}-"
            if target.startswith("mbs-user-") and not target.startswith(prefix):
                return
            target = target.removeprefix(prefix)
            row = room_members(db, channel, conversation, [target]).get(target) if target and "," not in target and target.lower() not in ALL_IDS else None
            if row is not None:
                label = name(row.display_name, target) or name(row.nickname, target) or target
                texts.append(f"@{label} ")
                mentions.append(target)

    walk(segment)
    flush()
    return parts[:8]
