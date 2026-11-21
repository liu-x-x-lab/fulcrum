"""单个已绑定好友的备注写入与独立回读；不自动重试外部写操作。"""

import json
from http.client import HTTPException as TransportError

from app.core.time import now_shanghai
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _parse_json
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT


def _data(reply):
    if (reply.get("http_status") == 200 and reply.get("Success") is True
            and type(reply.get("Code")) is int and reply["Code"] == 0
            and isinstance(reply.get("Data"), dict)):
        return reply["Data"]
    return {}


def _string(field):
    return field.get("string") if isinstance(field, dict) else field


def _read(client, channel, owner, target, *, initial=False):
    data = _data(client.get_contact_detail(channel, wxid=owner, to_wxids=[target]))
    base, rows = data.get("BaseResponse"), data.get("ContactList")
    if (not isinstance(base, dict) or type(base.get("ret")) is not int or base["ret"] != 0
            or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or _string(rows[0].get("UserName")) != target):
        return None
    if "Ret" in data and data["Ret"] != [0]:
        return None
    # 首次设置时协议可能省略 Remark；清空后的确认必须实际读到空字符串。
    value = _string(rows[0].get("Remark", "" if initial else None))
    return value if isinstance(value, str) else None


def change(channel, friend, *, expected: str, remark: str, clear: bool):
    target, owner = friend.contact_external_id, friend.owner_wxid
    if not target or "," in target or target == owner or target.endswith("@chatroom"):
        return False, "好友标识无效，未提交备注"
    value = "" if clear else remark.strip()
    if not clear and (not value or len(value) > 255 or not value.isprintable()):
        return False, "请输入有效备注，或选择清空备注"
    stale = "好友备注已变更，请同步好友后再操作"
    if expected != (friend.remark or ""):
        return False, stale
    client = WechatIpadApiClient()
    try:
        before = _read(client, channel, owner, target, initial=True)
    except (OSError, TransportError, ValueError):
        before = None
    if before is None:
        return False, "未核实当前好友资料，未提交备注"
    if before != expected:
        return False, stale
    if before == value:
        return True, "备注未变更，无需提交"

    unknown = "备注结果待核实，请同步好友；不会自动重试"
    try:
        data = _data(client.request_json(channel, IPAD_874_CONTRACT.endpoints.friend_set_remarks,
                    body={"Wxid": owner, "ToWxid": target, "Remarks": value}))
        if type(data.get("ret")) is not int:
            return False, unknown
        if data["ret"] != 0:
            return False, "微信拒绝修改备注"
        if "oplogRet" in data:
            op = data["oplogRet"]
            if not isinstance(op, dict) or type(op.get("count")) is not int or op["count"] != 1 or op.get("ret") != "AA==":
                return False, unknown
        if _read(client, channel, owner, target) != value:
            return False, unknown
    except (OSError, TransportError, ValueError):
        return False, unknown
    friend.remark = value or None
    friend.display_name = value or friend.name or friend.alias or target
    profile = _parse_json(friend.profile_json)
    profile["remark_verified_at"] = now_shanghai().timestamp()
    friend.profile_json = json.dumps(profile, ensure_ascii=False)
    return True, "已核实好友备注已清空" if clear else "已核实好友备注已修改"
