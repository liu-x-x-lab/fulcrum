"""手动单群同步专用：读取协议公告，按原请求绑定的账号和群保存纯文本。"""

from http.client import HTTPException as TransportError
import re

from app.core.tenant_context import require_tenant_id
from app.core.time import now_shanghai
from app.services.account_identity import account_identity
from app.services.chatroom_profile import MAX_NOTICE_TEXT, store_notice_snapshot
from app.services.wechat_ipad.client import WechatIpadApiClient


def target(channel, room):
    if (channel.channel_type == "wechat_ipad" and channel.tenant_id == room.tenant_id == require_tenant_id()
            and room.channel_id == channel.id and room.bot_id == channel.bot_id and not channel.deleted_at
            and channel.is_enabled and channel.bot and channel.bot.tenant_id == channel.tenant_id
            and channel.bot.is_enabled and not channel.bot.deleted_at and channel.api_base and room.status != "left"
            and re.fullmatch(r"[\w.-]+@chatroom", room.external_id or "")):
        owner = account_identity(channel)["external_id"]
        if owner:
            return owner, room.external_id, channel.api_base
    return None


def read_notice(db, channel, room, expected):
    if not expected or target(channel, room) != expected:
        return "群或账号不可用，未读取公告"
    owner, room_id, _ = expected
    started = now_shanghai()
    text = None
    problem = "协议未返回完整的群公告"
    try:
        reply = WechatIpadApiClient().get_chatroom_announce(channel, wxid=owner, chatroom_id=room_id)
        data = reply.get("Data")
        if (reply.get("http_status") == 200 and reply.get("Success") is True and type(reply.get("Code")) is int
                and reply["Code"] == 0 and isinstance(data, dict) and isinstance(data.get("BaseResponse"), dict)
                and type(data["BaseResponse"].get("ret")) is int and data["BaseResponse"]["ret"] == 0
                and isinstance(data.get("Announcement"), str)):
            if len(data["Announcement"]) <= MAX_NOTICE_TEXT:
                text, problem = data["Announcement"], ""
            else:
                problem = "群公告内容超过读取上限"
    except (OSError, TransportError, ValueError):
        problem = "群公告请求失败"
    # 874 不回显目标群。只关联单次请求，重新核对请求期间账号、渠道和群是否换绑。
    db.refresh(channel, with_for_update=True)
    db.refresh(room, with_for_update=True)
    if target(channel, room) != expected:
        return "群或账号已变更，未保存公告"
    fields = {} if problem else dict(body=text, summary="", state="body" if text else "empty",
                                     has_images=False, fetched_at=now_shanghai().isoformat())
    return store_notice_snapshot(room, started, fields, problem, owner_wxid=owner)
