"""牛牛找鸡：仅在用户明确发起指令时，发送当前会话授权范围内的图片。"""

import json
from http.client import HTTPException
from pathlib import Path
from time import monotonic

from sqlalchemy import select

from app.core.settings import settings
from app.models.entities import Conversation, Message, MessageAttachment
from app.services.plugin_runtime.contracts import PluginExecutionResult
from app.services.plugin_runtime.rate_limit import PluginRateLimiter
from app.services.safe_fetch import open_public_url
from plugins._shared.resources import read_owned_bytes
from plugins.image_source_search.service import MAX_BYTES, search, search_jpeg

HELP = "引用图片后发「找鸡」，或发「找鸡 图片链接」。图片会提交给已启用的第三方搜索服务。"


def matches_event(context):
    # 分发器还支持前缀模糊匹配；外传图片前必须是独立命令词，不能因相似前缀误触。
    words = context.content.lstrip().lstrip("/#").split(maxsplit=1)
    commands = context.binding_config.get("commands") or ("牛牛找鸡", "找鸡")
    return bool(words and words[0].casefold() in {cmd.casefold() for cmd in commands if isinstance(cmd, str)})


def _public_image(url):
    # 限制整个读取窗口，避免慢速响应在每次 socket 超时之前持续滴入数据。
    deadline = monotonic() + 8
    data = bytearray()
    with open_public_url(url, timeout=4) as stream:
        while len(data) <= MAX_BYTES and monotonic() < deadline:
            chunk = stream.read1(min(64 * 1024, MAX_BYTES + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
    raise ValueError("图片过大或下载超时")


def reply(status, text, ok=True):
    return PluginExecutionResult(ok, status, status, {"reply_text": text})


def execute_action(request):
    if request.action == "reload":
        return PluginExecutionResult(True, "ready", "牛牛找鸡已就绪")
    if request.action == "execute":
        return PluginExecutionResult(False, "missing_context", "请从聊天消息发起查询")
    return PluginExecutionResult(False, "unsupported", "不支持此操作")


def _snapshot(message):
    try:
        value = json.loads(message.raw_request_json or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _attachment(db, ctx, current):
    quote = _snapshot(current).get("quote") or {}
    image_id = current.id
    if quote:
        ref = quote.get("ref_external_id") if isinstance(quote, dict) and quote.get("ref_type") == 3 else None
        if not ref:
            return None
        images = select(Message).where(
            Message.tenant_id == ctx.tenant_id, Message.conversation_id == ctx.conversation_id,
            Message.direction == "in", Message.message_type == "image", Message.id < current.id,
        )
        def native_id(message):
            native = _snapshot(message).get("native_ids")
            return str(native.get("server") or "") if isinstance(native, dict) else ""

        candidates = [msg for msg in db.scalars(images.where(Message.external_message_id == str(ref)).limit(2))
                      if native_id(msg) in {"", str(ref)}]
        if not candidates:
            # iPad 的归档键可能是本地消息编号。按完整的微信服务端编号核对，不转浮点数。
            candidates = [msg for msg in db.scalars(images.order_by(Message.id.desc()).limit(200))
                          if native_id(msg) == str(ref)]
        if len(candidates) != 1:
            return None
        image_id = candidates[0].id
    return db.scalar(select(MessageAttachment).where(
        MessageAttachment.tenant_id == ctx.tenant_id, MessageAttachment.channel_id == ctx.channel_id,
        MessageAttachment.conversation_id == ctx.conversation_id, MessageAttachment.message_id == image_id,
        MessageAttachment.direction == "in", MessageAttachment.attachment_type == "image",
        MessageAttachment.status == "downloaded",
    ).order_by(MessageAttachment.id).limit(1))


def execute_message_action(db, request):
    ctx = request.context
    if request.action != "execute" or not ctx or not ctx.message_id or not ctx.sender_id:
        return execute_action(request)
    current = db.scalar(select(Message).join(Conversation).where(
        Message.id == ctx.message_id, Message.tenant_id == ctx.tenant_id, Message.direction == "in",
        Message.sender_id == ctx.sender_id, Conversation.id == ctx.conversation_id,
        Conversation.tenant_id == ctx.tenant_id, Conversation.bot_id == ctx.bot_id,
        Conversation.channel_id == ctx.channel_id,
    ))
    if current is None:
        return PluginExecutionResult(False, "missing_context", "消息不属于当前会话")
    args = str(request.payload.get("command_args") or "").strip()
    if args in {"帮助", "help"}:
        return reply("help", HELP)
    key = request.config.get("vision_api_key")
    key = key.strip() if isinstance(key, str) else ""
    anime = request.config.get("anime_enabled", True) is True
    if not key and not anime:
        return reply("invalid_config", "尚未启用搜索服务，请配置网页搜索密钥或启用动画搜索。", False)
    if args and (not args.startswith(("https://", "http://")) or len(args.split()) != 1 or len(args) > 2048):
        return reply("invalid_input", HELP)
    attachment = None if args else _attachment(db, ctx, current)
    if not args and not attachment:
        return reply("missing_image", "没有可读取的图片。请引用已下载的群图片，或发「找鸡 图片链接」。")
    if not PluginRateLimiter(db, ctx).consume("user", identity=ctx.sender_id, limit=3, window_seconds=60).allowed:
        return reply("rate_limited", "查询太频繁，请稍后再试。")
    try:
        if args:
            data = _public_image(args)
        else:
            root = (Path(settings.data_dir) / "inbound_media" / f"channel_{ctx.channel_id}").resolve()
            path = Path(attachment.storage_path or "").resolve(strict=True)
            if not path.is_relative_to(root) or path.stem != f"attachment_{attachment.id}":
                raise ValueError("invalid path")
            data = read_owned_bytes(path, max_bytes=MAX_BYTES)
        image = search_jpeg(data)
    except (ValueError, OSError, HTTPException, RuntimeError):
        return reply("invalid_image", "无法读取图片。请使用 8 MiB 以内的 JPG、PNG、GIF 或 WebP；链接须为公网图片直链。", False)
    status, text = search(image, key, anime)
    return reply(status, text, status != "provider_error")
