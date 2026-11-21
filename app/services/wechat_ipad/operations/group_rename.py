"""按绑定账号的实时群角色修改群名；写回执不代替对目标群名的独立读取。"""

import json
import re
from http.client import HTTPException as TransportError

from app.core.tenant_context import require_tenant_id
from app.core.time import now_shanghai
from app.services.account_identity import account_identity
from app.services.plugin_schemas import parse_config_json
from app.services.wechat_ipad.chatrooms import _normalize_chatroom_members, _normalize_chatroom_profile
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT


def available(channel, room):
    return (channel.channel_type == "wechat_ipad" and channel.tenant_id == room.tenant_id == require_tenant_id()
            and channel.id == room.channel_id and channel.bot_id == room.bot_id and not channel.deleted_at
            and channel.is_enabled and channel.api_base and room.status != "left" and room.is_active
            and room.management_status == "enabled" and re.fullmatch(r"[\w.-]+@chatroom", room.external_id)
            and bool(account_identity(channel)["external_id"]))


def rename(db, channel, room, *, expected_name: str, name: str) -> tuple[bool, str]:
    name = name.strip()
    if not name or len(name) > 255 or not name.isprintable():
        return False, "请输入不超过 255 个字符且不含控制字符的群名"
    if not available(channel, room):
        return False, "当前账号或群未启用管理"
    client = WechatIpadApiClient()
    wxid = account_identity(channel)["external_id"]
    room_id, base = room.external_id, channel.api_base
    args = {"wxid": wxid, "chatroom_id": room_id}

    def profile():
        reply = client.get_chatroom_info(channel, **args)
        return _normalize_chatroom_profile(reply, room_id) if reply.get("http_status") == 200 else {}

    try:
        before = profile()
        roster = client.get_chatroom_member_detail(channel, **args)
    except (OSError, TransportError, ValueError):
        return False, "未取得群资料或名册，未提交改名"
    snapshot = _normalize_chatroom_members(roster, room_id) if roster.get("http_status") == 200 else None
    owner = before.get("owner_external_id")
    if not snapshot or not owner or not any(row["member_external_id"] == wxid and
            (owner == wxid or row["role"] == "admin") for row in snapshot[1]):
        return False, "未确认绑定账号是当前群主或管理员，未提交改名"

    def same_target():
        return (available(channel, room) and channel.api_base == base and room.external_id == room_id
                and account_identity(channel)["external_id"] == wxid)

    # 该手动操作独占请求会话。释放读事务，重新读取在协议探测期间可能变更的账号和群状态。
    db.commit()
    db.refresh(channel)
    db.refresh(room)
    extra = parse_config_json(room.extra_json)
    if not same_target() or snapshot[0] < extra.get("ipad_roster_version", -1):
        return False, "群或账号状态已变更，请同步后再操作"
    if not before.get("name") or expected_name != before["name"] or expected_name != room.name:
        return False, "群名已变更或未同步，请同步当前群后再操作"
    if before["name"] == name:
        return True, "已核实当前群名，无需修改"

    unknown = "改名结果待核实，请同步当前群；不会自动重试"
    try:
        reply = client.request_json(channel, IPAD_874_CONTRACT.endpoints.group_set_name,
                                    body={"Wxid": wxid, "QID": room_id, "Content": name})
        data = reply.get("Data")
        if (reply.get("http_status") != 200 or reply.get("Success") is not True
                or type(reply.get("Code")) is not int or reply["Code"] != 0 or not isinstance(data, dict)
                or type(data.get("ret")) is not int):
            return False, unknown
        if data["ret"] != 0:
            return False, "微信拒绝修改群名"
        # 可选的逐条返回码是 bytes（JSON 中为 base64）；未知编码不猜作成功。
        if "oplogRet" in data:
            item = data["oplogRet"]
            if (not isinstance(item, dict) or type(item.get("count")) is not int or item["count"] != 1
                    or item.get("ret") != "AA=="):
                return False, unknown
        checked_at = now_shanghai().timestamp()
        after = profile()
    except (OSError, TransportError, ValueError):
        return False, unknown
    if after.get("name") != name:
        return False, unknown

    # 与资料同步共用时间水位；只改本次读回的群名，不从协议备注覆盖本地备注。
    db.refresh(channel, with_for_update=True)
    db.refresh(room, with_for_update=True)
    extra = parse_config_json(room.extra_json)
    if not same_target() or checked_at <= extra.get("ipad_profile_at", -1):
        return False, unknown
    room.name = after["name"]
    room.extra_json = json.dumps({**extra, "ipad_profile_at": checked_at}, ensure_ascii=False)
    return True, "已核实微信群名已修改"
