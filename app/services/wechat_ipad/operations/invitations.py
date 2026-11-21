"""874 群邀请的目标与回执核验；发送邀请不是入群事件。"""

import re
from typing import Any

from app.models.entities import Channel
from app.services.wechat_ipad.client import WechatIpadApiClient


def send_invitation(client: WechatIpadApiClient, channel: Channel, wxid: str, room: str, members: list[str]) -> dict[str, Any]:
    def outcome(status, code="", message=""):
        result = {"adapter_type": "wechat_ipad", "action": "invite_group_members", "ok": status == "sent",
                  "status": status, "external_status": status}
        if code:
            result.update(error_code=code, error_message=message)
        return result

    if not channel.api_base or not wxid:
        return outcome("failed", "missing_account", "未配置协议地址或登录账号")
    # 客户端将列表拼成逗号串，874 再拆分；禁止单个 ID 隐式扩展为多个收件人。
    if (not re.fullmatch(r"[\w.-]+@chatroom", room) or not isinstance(members, list) or not members
            or any(not isinstance(m, str) or not re.fullmatch(r"[\w.-]{1,128}", m) or m == wxid for m in members)):
        return outcome("failed", "invalid_target", "群或待邀请成员标识无效")
    targets = list(dict.fromkeys(members))
    unknown = outcome("unknown", "result_unknown", "邀请结果待核实，请先查看微信，不要重复邀请")
    try:
        reply = client.invite_chatroom_member(channel, wxid=wxid, chatroom_id=room, to_wxids=targets)
    except Exception:
        return unknown
    if (not isinstance(reply, dict) or reply.get("http_status") != 200 or reply.get("Success") is not True
            or type(reply.get("Code")) is not int or reply["Code"] != 0):
        return unknown
    data = reply.get("Data")
    base = data.get("baseResponse") if isinstance(data, dict) else None
    if not isinstance(base, dict) or type(base.get("ret")) is not int:
        return unknown
    if base["ret"] != 0:
        return outcome("failed", "invite_rejected", "微信未接受本次群邀请")
    rows = data.get("MemberList")
    if (type(data.get("MemberCount")) is not int or data["MemberCount"] != len(targets)
            or not isinstance(rows, list) or len(rows) != len(targets)):
        return unknown
    confirmed = set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get("MemberStatus")) is not int or row["MemberStatus"] != 0:
            return unknown
        member = row.get("MemberName")
        if not isinstance(member, dict) or not isinstance(member.get("string"), str):
            return unknown
        confirmed.add(member["string"])
    if confirmed != set(targets):
        return unknown
    return {**outcome("sent"), "chatroom_external_id": room, "member_count": len(targets)}
