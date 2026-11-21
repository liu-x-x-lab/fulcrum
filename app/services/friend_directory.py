"""好友目录的共同字段、绑定边界和私聊入口。"""

from sqlalchemy import and_, case, literal, select, tuple_

from app.models.entities import IpadFriend, WorkWechatContact
from app.services.account_identity import account_identity
from app.services.channel_catalog import channel_supports


def friend_rows(channels):
    # 只组合当前页可见的账号。不能单独匹配 owner_wxid：两个渠道可能登录过同一个账号。
    regular = []
    ipad = []
    for channel in channels:
        if channel.channel_type == "wechat_ipad":
            owner = account_identity(channel)["external_id"]
            if owner:
                ipad.append((channel.tenant_id, channel.id, owner))
        else:
            regular.append((channel.tenant_id, channel.id))

    def columns(model):
        return (model.id, model.channel_id, model.contact_external_id, model.display_name, model.name,
                model.alias, model.avatar_url, model.last_synced_at)

    c = WorkWechatContact
    general = select(*columns(c), case((c.display_name != c.name, c.display_name)).label("remark"),
                     and_(c.sync_status == "active", c.deleted_at.is_(None)).label("active")).where(
        c.contact_kind == "friend", tuple_(c.tenant_id, c.channel_id).in_(regular))
    if not ipad:
        return general.subquery()
    f = IpadFriend
    bound = select(*columns(f), f.remark, literal(True).label("active")).where(
        tuple_(f.tenant_id, f.channel_id, f.owner_wxid).in_(ipad))
    return (general.union_all(bound) if regular else bound).subquery()


def friend_chat_fields(channel, item, bot_enabled):
    return {
        "channel_id": channel.id,
        "remark_action": (f"/channels/{channel.id}/wechat-ipad/friends/{item.id}/remark"
                          if channel.channel_type == "wechat_ipad" and channel.api_base and bot_enabled
                          and channel.is_enabled and channel.deleted_at is None else None),
        "contact_key": f"ipad-{item.id}" if channel.channel_type == "wechat_ipad" else str(item.id),
        "can_start_chat": bool(item.active and item.contact_external_id.strip() and bot_enabled
                               and channel.is_enabled and channel.deleted_at is None
                               and channel_supports(channel.channel_type, "direct_chat")),
    }
