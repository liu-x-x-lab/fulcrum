"""微信账号后台共享的只读目录、筛选及分页。"""

import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.entities import Bot, Channel, Chatroom, ChatroomMember, Conversation, Message
from app.services.identity_directory import IdentityDirectory, make_identity
from app.services.friend_directory import friend_chat_fields, friend_rows

PAGE_SIZE = 20
TABS = ("contacts", "groups", "members", "messages")


def _avatar(extra_json: str | None) -> str:
    """读取群和群成员 extra_json 中的头像地址，解析失败按无头像处理。"""

    try:
        extra = json.loads(extra_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    return str(extra.get("avatar_url") or "").strip() if isinstance(extra, dict) else ""


def _like(keyword: str) -> str:
    """转义 LIKE 通配符，避免用户输入 % 或 _ 时匹配到全部数据。"""

    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _paginate(db: Session, stmt, page: int) -> tuple[list, dict[str, Any]]:
    """对已带过滤条件的查询统计总数并取当前页。"""

    total = int(db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0)
    paging = _paging(total, page)
    rows = db.execute(stmt.offset((paging["page"] - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    return rows, paging


def _paging(total: int, page: int) -> dict:
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    current = min(max(int(page or 1), 1), total_pages)
    return {
        "page": current,
        "total": total,
        "total_pages": total_pages,
        "has_prev": current > 1,
        "has_next": current < total_pages,
        "prev_page": max(current - 1, 1),
        "next_page": min(current + 1, total_pages),
    }


def build_account_dashboard(
    db: Session,
    channel: Channel,
    *,
    tab: str = "contacts",
    keyword: str = "",
    page: int = 1,
    conversation_id: int = 0,
    sender: str = "",
) -> dict:
    """只读本地目录；iPad 渠道换绑后不显示之前账号的好友。"""

    active = tab if tab in TABS else "contacts"
    q = (keyword or "").strip()[:100]
    sender_q = (sender or "").strip()[:100]
    cid = int(channel.id)
    friends = friend_rows([channel])
    contact = friends.c
    counts = {
        "contacts": int(db.scalar(select(func.count()).select_from(friends)) or 0),
        "groups": int(db.scalar(select(func.count(Chatroom.id)).where(Chatroom.channel_id == cid)) or 0),
        "members": int(db.scalar(select(func.count(ChatroomMember.id)).where(ChatroomMember.channel_id == cid)) or 0),
        "messages": int(
            db.scalar(
                select(func.count(Message.id)).join(Conversation, Message.conversation_id == Conversation.id).where(Conversation.channel_id == cid)
            )
            or 0
        ),
    }
    # 外部 ID -> 会话 ID，用于“查看消息”链接；只取本页涉及的 ID，避免整渠道扫描。
    result: dict[str, Any] = {"tab": active, "keyword": q, "sender": sender_q, "conversation_id": int(conversation_id or 0), "conversations": [], "counts": counts, "rows": [], "message_identities": {}}

    if active == "contacts":
        stmt = select(friends)
        if q:
            like = _like(q)
            stmt = stmt.where(
                or_(
                    contact.display_name.like(like, escape="\\"),
                    contact.name.like(like, escape="\\"),
                    contact.alias.like(like, escape="\\"),
                    contact.contact_external_id.like(like, escape="\\"),
                )
            )
        contacts, pagination = _paginate(db, stmt.order_by(contact.display_name.asc(), contact.id.desc()), page)
        bot_enabled = db.scalar(select(Bot.is_enabled).where(Bot.id == channel.bot_id,
                              Bot.tenant_id == channel.tenant_id, Bot.deleted_at.is_(None))) if contacts else False
        conv_map = _conversation_ids(db, channel, [item.contact_external_id for item in contacts], "direct")
        result["rows"] = [
            {
                "identity": make_identity(item.contact_external_id, item.display_name or item.name, item.avatar_url),
                **friend_chat_fields(channel, item, bot_enabled),
                "remark": item.remark,
                "last_synced_at": item.last_synced_at,
                "conversation_id": conv_map.get(item.contact_external_id),
            }
            for item in contacts
        ]
    elif active == "groups":
        stmt = select(Chatroom).where(Chatroom.channel_id == cid)
        if q:
            like = _like(q)
            stmt = stmt.where(or_(Chatroom.name.like(like, escape="\\"), Chatroom.remark.like(like, escape="\\"), Chatroom.external_id.like(like, escape="\\")))
        rows, pagination = _paginate(db, stmt.order_by(Chatroom.name.asc(), Chatroom.id.desc()), page)
        rooms = [row[0] for row in rows]
        conv_map = _conversation_ids(db, channel, [item.external_id for item in rooms], "group")
        result["rows"] = [
            {
                "identity": make_identity(item.external_id, item.remark or item.name, _avatar(item.extra_json)),
                "room_id": int(item.id),
                "member_count": item.member_count,
                "last_synced_at": item.last_synced_at,
                "conversation_id": conv_map.get(item.external_id),
            }
            for item in rooms
        ]
    elif active == "members":
        stmt = select(ChatroomMember, Chatroom).join(Chatroom, ChatroomMember.chatroom_id == Chatroom.id).where(ChatroomMember.channel_id == cid, Chatroom.channel_id == cid)
        if q:
            like = _like(q)
            stmt = stmt.where(
                or_(
                    ChatroomMember.nickname.like(like, escape="\\"),
                    ChatroomMember.display_name.like(like, escape="\\"),
                    ChatroomMember.member_external_id.like(like, escape="\\"),
                    Chatroom.name.like(like, escape="\\"),
                )
            )
        rows, pagination = _paginate(db, stmt.order_by(Chatroom.name.asc(), ChatroomMember.id.asc()), page)
        result["rows"] = [
            {
                "identity": make_identity(member.member_external_id, member.display_name or member.nickname, _avatar(member.extra_json)),
                "role": member.role,
                "last_synced_at": member.last_synced_at,
                "room": make_identity(room.external_id, room.remark or room.name, _avatar(room.extra_json)),
                "room_id": int(room.id),
            }
            for member, room in rows
        ]
    else:
        stmt = select(Message).join(Conversation, Message.conversation_id == Conversation.id).where(Conversation.channel_id == cid)
        if q:
            like = _like(q)
            stmt = stmt.where(
                or_(Message.content.like(like, escape="\\"), Message.sender_name.like(like, escape="\\"), Message.sender_id.like(like, escape="\\"))
            )
        # 会话筛选（群或好友）：限定在本渠道内，避免跨渠道读取。
        if conversation_id:
            stmt = stmt.where(Message.conversation_id == int(conversation_id))
        # 发送者筛选：按昵称或 wxid 模糊匹配。
        if sender_q:
            like = _like(sender_q)
            stmt = stmt.where(or_(Message.sender_name.like(like, escape="\\"), Message.sender_id.like(like, escape="\\")))
        rows, pagination = _paginate(db, stmt.order_by(Message.created_at.desc(), Message.id.desc()), page)
        messages = [row[0] for row in rows]
        # 查询按最新优先分页，模板内再反转，保证页内最新消息在底部。
        result["rows"] = messages
        directory = IdentityDirectory(db)
        result["message_identities"] = directory.for_messages(messages)
        # 筛选下拉：按最近活跃排序的会话，最多 200 个，展示昵称而不是 wxid。
        convs = db.scalars(
            select(Conversation).where(Conversation.channel_id == cid).order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(200)
        ).all()
        conv_identities = directory.for_conversations(convs)
        result["conversations"] = [
            {"id": int(item.id), "name": (conv_identities.get(item.id) or {}).get("name") or item.title or item.external_id, "is_group": item.chat_type == "group"}
            for item in convs
        ]

    result["pagination"] = pagination
    return result


def _conversation_ids(db: Session, channel: Channel, external_ids: list[str], kind: str) -> dict[str, int]:
    """批量查询外部 ID 对应的会话，供列表直接跳转到聊天记录。"""

    ids = [item for item in external_ids if item]
    if not ids:
        return {}
    rows = db.execute(select(Conversation.external_id, func.min(Conversation.id)).where(
        Conversation.tenant_id == channel.tenant_id, Conversation.channel_id == channel.id,
        Conversation.bot_id == channel.bot_id, Conversation.chat_type == kind,
        Conversation.external_id.in_(ids)).group_by(Conversation.external_id)).all()
    return {external_id: int(conversation_id) for external_id, conversation_id in rows}
