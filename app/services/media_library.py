"""按租户聚合既有本地媒体；不触发下载，不扫描插件、密钥或运维目录。"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Bot, Channel, Conversation, Message, MessageAttachment, MomentsMedia, MomentsPost, TaskJob
from app.services import media_files, media_storage, outbound_files, video_preview
from app.services.media_formats import media_kind

SCAN_LIMIT = 10000
PAGE_SIZE = 24
SOURCES = {"received": "聊天下载", "sent": "出站文件", "ai_generated": "AI 生图", "ai_edited": "AI 改图", "moments": "朋友圈"}
KINDS = {"image": "图片", "video": "视频", "audio": "音频/语音", "file": "其他文件"}


@dataclass
class MediaItem:
    ref: str
    path: Path = field(repr=False)
    name: str
    channel_id: int
    channel_name: str
    mime: str
    kind: str
    source: str
    size: int
    created_at: datetime
    conversation_id: int | None = None
    source_message_id: int | None = None
    row: object | None = field(default=None, repr=False)
    metadata: dict = field(default_factory=dict, repr=False)

    @property
    def size_label(self):
        if self.size < 1024:
            return f"{self.size} B"
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.1f} KiB"
        return f"{self.size / (1024 * 1024):.1f} MiB"


def channels(db: Session) -> list[Channel]:
    tenant_id = require_tenant_id()
    return list(db.scalars(select(Channel).join(Bot, Bot.id == Channel.bot_id).where(
        Channel.tenant_id == tenant_id, Bot.tenant_id == tenant_id,
    ).order_by(Channel.name, Channel.id)))


def _object(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _conversation(db, channel, conversation_id, message_id=None):
    if not conversation_id:
        return None
    conversation = db.get(Conversation, conversation_id)
    if (conversation is None or conversation.tenant_id != channel.tenant_id
        or conversation.channel_id != channel.id or conversation.bot_id != channel.bot_id):
        raise ValueError("会话归属无效")
    if message_id:
        message = db.get(Message, message_id)
        if message is None or message.tenant_id != channel.tenant_id or message.conversation_id != conversation.id:
            raise ValueError("消息归属无效")
    return conversation.id


def _attachment(db, row, channel):
    if row.tenant_id != require_tenant_id() or row.status != "downloaded" or not row.storage_path:
        raise ValueError("媒体尚未下载或已删除")
    conversation_id = _conversation(db, channel, row.conversation_id, row.message_id)
    root = Path(settings.data_dir) / "inbound_media" / f"channel_{channel.id}"
    if row.direction == "out":
        root = outbound_files.OUTBOUND_FILE_ROOT / str(channel.tenant_id) / "outbound_files" / str(channel.id)
    path = media_files.owned_file(row.storage_path, root)
    if row.direction != "out" and path.stem != f"attachment_{row.id}":
        raise ValueError("附件路径无效")
    if row.direction == "out" and _outbound(db, channel, path.parent.name).path != path:
        raise ValueError("出站附件路径无效")
    meta = _object(row.metadata_json)
    name = str(meta.get("file_name") or path.name)[:255]
    return MediaItem(f"a{row.id}", path, name, channel.id, channel.name, row.mime_type or "",
        media_kind(row.mime_type or "", row.attachment_type, name), "sent" if row.direction == "out" else "received",
        path.stat().st_size, ensure_shanghai_datetime(row.created_at), conversation_id, row.message_id, row, meta)


def _outbound(db, channel, share_id):
    if outbound_files.SHARE_ID_PATTERN.fullmatch(share_id) is None:
        raise ValueError("文件编号无效")
    root = outbound_files.OUTBOUND_FILE_ROOT / str(channel.tenant_id) / "outbound_files" / str(channel.id)
    directory = root / share_id
    sidecar = directory / (".meta.json" if (directory / ".meta.json").exists() else "meta.json")
    meta = media_files.read_metadata(sidecar, root)
    if meta.get("tenant_id") != channel.tenant_id or meta.get("channel_id") != channel.id:
        raise ValueError("文件归属无效")
    name = str(meta.get("file_name") or "")
    if not name or name != outbound_files.sanitize_upload_filename(name, fallback=""):
        raise ValueError("文件名无效")
    path = media_files.owned_file(directory / name, root)
    if path == sidecar:
        raise ValueError("不能读取内部描述文件")
    conversation_id = _conversation(db, channel, meta.get("conversation_id"), meta.get("source_message_id"))
    source = meta.get("origin") if meta.get("origin") in {"ai_generated", "ai_edited"} else "sent"
    try:
        created_at = ensure_shanghai_datetime(datetime.fromisoformat(str(meta.get("created_at"))))
    except ValueError:
        created_at = datetime.fromtimestamp(path.stat().st_mtime, tz=now_shanghai().tzinfo)
    mime = str(meta.get("content_type") or "")
    return MediaItem(f"o{channel.id}-{share_id}", path, name, channel.id, channel.name, mime,
        media_kind(mime, str(meta.get("message_type") or ""), name), source, path.stat().st_size, created_at,
        conversation_id, meta.get("source_message_id"), metadata=meta)


def _moment(db, row, channel):
    if row.tenant_id != require_tenant_id() or row.status != "downloaded" or not row.storage_path:
        raise ValueError("媒体尚未下载或已删除")
    post = db.get(MomentsPost, row.post_id)
    if post is None or post.tenant_id != channel.tenant_id or post.channel_id != channel.id:
        raise ValueError("动态归属无效")
    root = Path(settings.data_dir) / "moments_media" / f"channel_{channel.id}"
    path = media_files.owned_file(row.storage_path, root)
    if path.stem != str(row.id):
        raise ValueError("动态媒体路径无效")
    return MediaItem(f"m{row.id}", path, path.name, channel.id, channel.name, row.mime_type or "",
        media_kind(row.mime_type or "", filename=path.name), "moments", path.stat().st_size,
        ensure_shanghai_datetime(row.created_at), row=row, metadata={"job_id": row.job_id})


def resolve(db: Session, reference: str) -> MediaItem:
    """客户端只传不透明编号，不接受路径、文件名、租户编号或下载地址。"""

    allowed = {channel.id: channel for channel in channels(db)}
    match = re.fullmatch(r"([am])(\d{1,18})", reference)
    if match:
        row = db.get(MessageAttachment if match[1] == "a" else MomentsMedia, int(match[2]))
        if row is not None and row.channel_id in allowed:
            return (_attachment if match[1] == "a" else _moment)(db, row, allowed[row.channel_id])
    match = re.fullmatch(r"o(\d{1,18})-([0-9a-f]{32})", reference)
    if match and int(match[1]) in allowed:
        return _outbound(db, allowed[int(match[1])], match[2])
    raise ValueError("媒体不存在或无权访问")


def collect(db: Session, *, channel_id=0, limit=None, deadline=None):
    """只读取小型描述与 stat；设扫描上限，避免租户请求无限占用共享进程。"""

    limit = SCAN_LIMIT if limit is None else min(limit, SCAN_LIMIT)
    available = channels(db)
    allowed = {channel.id: channel for channel in available if not channel_id or channel.id == channel_id}
    by_path, scanned = {}, 0
    limited = False
    # 历史整理与目录枚举共用时间预算；停止后不再继续遍历其他账号或来源。
    def expired():
        return deadline is not None and time.monotonic() >= deadline

    for model, builder in ((MessageAttachment, _attachment), (MomentsMedia, _moment)):
        if limited or expired():
            limited = True
            break
        records = db.scalars(select(model).where(model.channel_id.in_(allowed), model.status == "downloaded")
            .order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1))
        for row in records:
            scanned += 1
            if scanned > limit or expired():
                limited = True
                break
            try:
                item = builder(db, row, allowed[row.channel_id])
                by_path[str(item.path)] = item
            except (OSError, ValueError, TypeError):
                continue
    for channel in allowed.values():
        if limited or expired():
            limited = True
            break
        root = outbound_files.OUTBOUND_FILE_ROOT / str(channel.tenant_id) / "outbound_files" / str(channel.id)
        if not root.is_dir() or root.is_symlink():
            continue
        for directory in root.iterdir():
            scanned += 1
            if scanned > limit or expired():
                limited = True
                break
            try:
                item = _outbound(db, channel, directory.name)
                existing = by_path.get(str(item.path))
                if existing is not None:
                    item.row = existing.row
                    item.conversation_id = item.conversation_id or existing.conversation_id
                    item.source_message_id = item.source_message_id or existing.source_message_id
                by_path[str(item.path)] = item
            except (OSError, ValueError, TypeError):
                continue
    return {"items": list(by_path.values()), "channels": available, "limited": limited}


def catalog(db: Session, *, channel_id=0, kind="", source="", query="", start="", end="", page=1):
    collected = collect(db, channel_id=channel_id)
    found = [item for item in collected["items"] if (not kind or item.kind == kind) and (not source or item.source == source)
        and (not query or query.casefold() in item.name.casefold())
        and (not start or item.created_at.date().isoformat() >= start) and (not end or item.created_at.date().isoformat() <= end)]
    found.sort(key=lambda item: (item.created_at, item.ref), reverse=True)
    count = len(found)
    pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    current = max(1, min(page, pages))
    unique = {}
    for item in found:
        try:
            device, inode, size, _modified = media_storage.signature(item.path)
            unique[(device, inode)] = size
        except (OSError, ValueError):
            continue
    total = sum(item.size for item in found)
    stored = sum(unique.values())
    return {"items": found[(current - 1) * PAGE_SIZE:current * PAGE_SIZE], "count": count,
            "total_bytes": total, "stored_bytes": stored, "shared_bytes": max(0, total - stored),
            "unique_count": len(unique), "page": current, "pages": pages,
            "limited": collected["limited"], "channels": collected["channels"]}


def check_delete(db: Session, item: MediaItem):
    """待发送文件和刚落盘的出站文件不能清理；不依据客户端传来的可删标记。"""

    if item.source in {"sent", "ai_generated", "ai_edited"} and now_shanghai() - item.created_at < timedelta(minutes=5):
        raise ValueError("新生成或上传的文件暂受保护，请 5 分钟后再清理")
    tasks = list(db.scalars(select(TaskJob).where(TaskJob.status.in_(("pending", "running"))).limit(1001)))
    if len(tasks) > 1000:
        raise ValueError("后台任务较多，暂不能安全清理媒体")
    tokens = (str(item.path), item.path.parent.name) if item.source != "received" else (str(item.path),)
    task_ids = {item.metadata.get("job_id"), item.metadata.get("upload_job_id")}
    for task in tasks:
        if task.id in task_ids or any(token and len(token) > 10 and token in (task.payload_json or "") for token in tokens):
            raise ValueError("文件仍被待处理任务使用，暂不能删除")
    linked = list(db.scalars(select(MessageAttachment).where(MessageAttachment.storage_path == str(item.path))))
    if item.metadata.get("message_id"):
        message = db.get(Message, item.metadata["message_id"])
        if message and (message.status == "pending" or message.external_status == "pending"):
            raise ValueError("文件仍在发送中，暂不能删除")
    for attachment in linked:
        if attachment.direction == "out" and attachment.message_id:
            message = db.get(Message, attachment.message_id)
            if message and (message.status == "pending" or message.external_status == "pending"):
                raise ValueError("文件仍在发送中，暂不能删除")


def delete_file(db: Session, item: MediaItem):
    """只 unlink 已审核原件与试听副本；记录保留，调用方逐项提交并反馈实际成功数。"""

    verified = resolve(db, item.ref)
    if verified.path != item.path:
        raise ValueError("文件已变化，请刷新后重试")
    tenant_id = require_tenant_id()
    digest = media_storage.file_digest(item.path)[0]
    with media_storage.locked(tenant_id, "preview-" + digest[:2]):
        digest, remaining = media_storage.release(item.path, tenant_id=tenant_id, boundary=item.path.parent)
        # 最后一个受管原件引用消失时，试听与视频封面一起释放；共享来源仍在则保留。
        if not remaining:
            for cache in (media_files.preview_cache(item.path, digest=digest), video_preview.cache_path(digest)):
                try:
                    if cache.is_file():
                        media_files.owned_file(cache, cache.parent).unlink()
                except (OSError, ValueError):
                    pass
    for attachment in db.scalars(select(MessageAttachment).where(MessageAttachment.storage_path == str(item.path))):
        attachment.status = "deleted"
    if isinstance(item.row, MomentsMedia):
        item.row.status = "deleted"
    if re.fullmatch(r"[0-9a-f]{32}", item.path.parent.name):
        sidecar = item.path.parent / (".meta.json" if item.path.name.casefold() == "meta.json" else "meta.json")
        try:
            metadata = media_files.read_metadata(sidecar, item.path.parent)
            if metadata.get("file_name") == item.path.name and metadata.get("tenant_id") == require_tenant_id():
                media_files.owned_file(sidecar, item.path.parent).unlink()
                item.path.parent.rmdir()
        except (OSError, ValueError):
            pass
