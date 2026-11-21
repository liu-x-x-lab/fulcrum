"""874 system friend requests. Keep tickets out of chat and operation logs."""

import hashlib
import json
from http.client import HTTPException as TransportError
from xml.etree import ElementTree

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, IpadFriendRequest as RequestRow
from app.services.account_identity import account_identity
from app.services.plugin_secrets import PluginSecretError, decrypt_scoped_json, encrypt_scoped_json
from app.services.wechat_ipad.client import WechatIpadApiClient


LABELS = {"pending": "待处理", "unavailable": "凭据不可用", "unknown": "结果待核实",
          "rejected": "微信已拒绝", "submitted": "已提交，待通讯录确认", "confirmed": "已成为好友"}


def is_request(item):
    return isinstance(item, dict) and any(str(item.get(k)) == "37" for k in ("MsgType", "msg_type", "Type"))


def contains_request(payload):
    # Some callback producers wrap or batch AddMsg. Never archive a mixed raw envelope with tickets.
    stack = [payload]
    while stack:
        node = stack.pop()
        if is_request(node):
            return True
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _text(field):
    value = field.get("string") if isinstance(field, dict) else field
    return value if isinstance(value, str) else ""


def _scope(row):
    return json.dumps(["fulcrum:ipad-request:v1", row.tenant_id, row.channel_id, row.owner_wxid,
                       row.applicant, row.ticket_digest], separators=(",", ":")).encode()


def receive(db, channel, owner, item):
    if (not is_request(item) or not owner or len(owner) > 128
            or _text(item.get("FromUserName")) != "fmessage" or _text(item.get("ToUserName")) != owner):
        return
    content = _text(item.get("Content"))
    if len(content) > 65536 or "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
        return
    try:
        msg = ElementTree.fromstring(content)
    except (ElementTree.ParseError, ValueError):
        return
    applicant, nickname, v1, v2, scene = (msg.get(k, "") for k in
        ("fromusername", "fromnickname", "encryptusername", "ticket", "scene"))
    if (msg.tag != "msg" or not applicant or len(applicant) > 128 or applicant in {owner, "fmessage"}
            or applicant.endswith("@chatroom") or not (0 < len(v1) <= 8192 and 0 < len(v2) <= 8192)
            or not scene.isascii() or not scene.isdigit() or len(scene) > 3 or not 1 <= int(scene) <= 255):
        return
    digest = hashlib.sha256(v2.encode()).hexdigest()
    key = dict(tenant_id=channel.tenant_id, channel_id=channel.id, owner_wxid=owner, ticket_digest=digest)
    query = select(RequestRow).filter_by(**key)
    row = db.scalar(query.with_for_update())
    if row is not None and (row.status != "unavailable" or row.applicant != applicant):
        return
    fresh = row is None
    if fresh:
        row = RequestRow(**key, applicant=applicant, nickname=nickname[:255], received_at=now_shanghai())
    try:
        row.credentials = encrypt_scoped_json({"v1": v1, "v2": v2, "scene": int(scene)}, _scope(row))
        row.status = "pending"
    except PluginSecretError:
        row.status = "unavailable"
    if fresh:
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            if not db.scalar(query.with_for_update()):
                raise


def active_channels(tenant_id):
    return select(Channel).join(Bot).where(Channel.tenant_id == tenant_id, Bot.tenant_id == tenant_id,
        Channel.channel_type == "wechat_ipad", Channel.deleted_at.is_(None), Bot.deleted_at.is_(None))


def _current_channel(db, tenant, cid):
    return db.scalar(active_channels(tenant).where(Channel.id == cid, Channel.is_enabled.is_(True),
        Bot.is_enabled.is_(True)).with_for_update().execution_options(populate_existing=True))


def _send(channel, owner, applicant, credentials):
    try:
        reply = WechatIpadApiClient().accept_friend(channel, wxid=owner, **credentials)
    except (ValueError, OSError, TransportError):
        return "unknown"
    data = reply.get("Data")
    if (reply.get("http_status") != 200 or reply.get("Success") is not True or type(reply.get("Code")) != int
            or reply["Code"] != 0 or not isinstance(data, dict) or data.get("Username", applicant) != applicant):
        return "unknown"
    base = data.get("BaseResponse")
    if not isinstance(base, dict) or type(base.get("ret")) != int:
        return "unknown"
    return "submitted" if base["ret"] == 0 else "rejected"


def accept(db, tenant, cid, rid):
    channel = _current_channel(db, tenant, cid)
    owner = account_identity(channel)["external_id"] if channel and channel.api_base else ""
    scope = (RequestRow.id == rid, RequestRow.tenant_id == tenant, RequestRow.channel_id == cid,
             RequestRow.owner_wxid == owner)
    row = db.scalar(select(RequestRow).where(*scope).with_for_update()) if owner else None
    if not row or row.owner_wxid != owner:
        return None
    if row.status != "pending":
        return False, LABELS[row.status]
    try:
        credentials = decrypt_scoped_json(row.credentials or "", _scope(row))
    except PluginSecretError:
        return False, "无法读取申请凭据，未提交"
    applicant = row.applicant
    # Commit the single-use claim before any external I/O. A crash is an unknown result, never a retry.
    claimed = db.execute(update(RequestRow).where(*scope, RequestRow.status == "pending").values(
        status="unknown", credentials=None, claimed_at=now_shanghai())).rowcount
    db.commit()
    if not claimed:
        return False, "此申请已处理"
    channel = _current_channel(db, tenant, cid)
    if not channel or not channel.api_base or account_identity(channel)["external_id"] != owner:
        return False, "账号状态已变更，未提交"
    status = _send(channel, owner, applicant, credentials)
    db.execute(update(RequestRow).where(*scope, RequestRow.status == "unknown").values(status=status))
    db.commit()
    return status == "submitted", LABELS[status]
