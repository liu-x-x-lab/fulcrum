"""群插件作用域的更新与恢复继承操作。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import Channel, Chatroom, GroupPluginScope
from app.services.wechat_ipad.chatrooms import (
    reset_group_plugin_scope_to_inherited,
    update_group_plugin_scope_enabled,
)


def update_wechat_ipad_chatroom_scope_enabled(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    enabled: bool,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return update_group_plugin_scope_enabled(
        db,
        channel=channel,
        chatroom=chatroom,
        bot_plugin_id=bot_plugin_id,
        enabled=enabled,
        operator_id=operator_id,
        audit_note=audit_note,
        source="wechat_ipad_chatroom_scope_toggle",
    )


def reset_wechat_ipad_chatroom_scope(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return reset_group_plugin_scope_to_inherited(
        db,
        channel=channel,
        chatroom=chatroom,
        bot_plugin_id=bot_plugin_id,
        operator_id=operator_id,
        audit_note=audit_note,
        source="wechat_ipad_chatroom_scope_reset",
    )
