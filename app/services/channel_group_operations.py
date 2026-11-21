from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Channel, Chatroom, ChatroomMember
from app.core.tenant_context import require_tenant_id
from app.services.account_identity import account_identity
from app.services.channel_adapters import get_channel_adapter
from app.services.plugin_runtime.contracts import PluginExecutionContext


def invite_group_members(
    db: Session,
    *,
    context: PluginExecutionContext,
    chatroom_id: int,
    member_external_ids: Iterable[str],
) -> dict[str, object]:
    """在可信租户、机器人和渠道范围内派发群成员邀请动作。"""

    if context.channel_id is None:
        return _error("trusted_channel_required", "缺少可信渠道上下文")
    channel = db.get(Channel, int(context.channel_id))
    chatroom = db.get(Chatroom, int(chatroom_id))
    if (
        channel is None
        or chatroom is None
        or int(channel.tenant_id) != int(context.tenant_id)
        or int(channel.bot_id) != int(context.bot_id)
        or int(chatroom.tenant_id) != int(context.tenant_id)
        or int(chatroom.bot_id) != int(context.bot_id)
        or int(chatroom.channel_id) != int(context.channel_id)
    ):
        # chatroom_id 来自插件配置，但最终授权仍以这里加载到的租户资源为准，
        # 即使配置被篡改也不能邀请到其他机器人或其他租户的群。
        return _error("chatroom_not_found", "目标群不属于当前机器人渠道")
    if not chatroom.is_active:
        return _error("chatroom_inactive", "目标群尚未启用管理")

    members = _normalize_member_ids(member_external_ids)
    if not members:
        return _error("member_required", "待邀请成员为空")
    adapter = get_channel_adapter(channel)
    return adapter.invite_group_members(
        channel,
        chatroom_external_id=str(chatroom.external_id),
        member_external_ids=members,
    )


def _normalize_member_ids(values: Iterable[str]) -> list[str]:
    """保持输入顺序去重，避免同一渠道动作重复邀请同一成员。"""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        member_id = str(value or "").strip()
        if not member_id or member_id in seen:
            continue
        seen.add(member_id)
        normalized.append(member_id)
    return normalized


def _error(error_code: str, error_message: str) -> dict[str, object]:
    return {
        "ok": False,
        "action": "invite_group_members",
        "status": "failed",
        "external_status": "failed",
        "error_code": error_code,
        "error_message": error_message,
    }


def removal_permission_error(db: Session, channel: Channel, room: Chatroom, member_id: str) -> str:
    """踢人只使用可信租户与当前群成员身份，后台员工权限不能代替群管理权限。"""
    if (channel.tenant_id != require_tenant_id() or room.tenant_id != channel.tenant_id
            or room.channel_id != channel.id or room.bot_id != channel.bot_id):
        return "群不属于当前账号"
    if not channel.is_enabled or not room.is_active or room.status == "left" or room.management_status == "archived":
        return "账号或群已停止管理"
    own_id = account_identity(channel)["external_id"]
    if not own_id or member_id == own_id or member_id == room.owner_external_id:
        return "不能移除机器人自身或群主"
    members = {row.member_external_id: row for row in db.scalars(select(ChatroomMember).where(
        ChatroomMember.tenant_id == channel.tenant_id, ChatroomMember.bot_id == channel.bot_id,
        ChatroomMember.channel_id == channel.id, ChatroomMember.chatroom_id == room.id,
        ChatroomMember.member_external_id.in_((own_id, member_id)),
    ).with_for_update().execution_options(populate_existing=True))}
    own = members.get(own_id)
    if own is not None and own.status != "active":
        return "机器人已不在群内"
    if own_id != room.owner_external_id and not (own and own.role in {"owner", "admin"}):
        return "尚未确认机器人具有群管理权限，请同步群成员"
    target = members.get(member_id)
    if target is None or target.status != "active" or target.role != "member":
        return "目标不是已确认的普通群成员，请同步群成员"
    return ""


def remove_group_member(db: Session, *, channel: Channel, room: Chatroom, member_id: str, request_id: str) -> dict:
    """公共成员移除入口，适配器负责渠道协议；入队成功不表示成员已经移出。"""
    if not request_id or len(request_id) > 128:
        return {"ok": False, "status": "denied", "error_message": "移出操作需要有效的请求编号"}
    reason = removal_permission_error(db, channel, room, member_id)
    if reason:
        return {"ok": False, "status": "denied", "error_message": reason}
    if channel.channel_type == "wechat_ipad":
        from app.services.group_moderation.ipad import enqueue

        return enqueue(db, channel, room, member_id, request_id)
    return get_channel_adapter(channel).remove_group_member(
        channel, chatroom_external_id=room.external_id, member_external_id=member_id, request_id=request_id,
    )
