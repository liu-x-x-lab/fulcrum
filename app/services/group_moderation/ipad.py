"""iPad 群治理任务：实时复核权限，单目标下发一次，只按直接回执结算。"""

from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation
from app.services.account_identity import account_identity
from app.services.group_features.moderation import FEATURE
from app.services.group_features.store import get_state, ipad_removal_confirmed
from app.services.group_moderation import notices
from app.services.group_moderation.actions import _locked_target, review_locked
from app.services.plugin_schemas import parse_config_json
from app.services.task_queue import complete_job, enqueue_job, fail_job_terminal, parse_job_payload
from app.services.wechat_ipad.chatrooms import _normalize_chatroom_members, _normalize_chatroom_profile
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT

JOB_TYPE = "ipad_moderation_remove"
UNKNOWN = "移出结果未知，请核实成员状态；不会再次下发"


def enqueue(db, channel, room, member, request_id):
    account = account_identity(channel)["external_id"]
    # 协议会按逗号拆分目标；此处仅接受一个确定的普通成员。
    if (not ipad_removal_confirmed(room) or channel.deleted_at or not channel.api_base or not account
            or not room.external_id.endswith("@chatroom") or member.endswith("@chatroom")
            or any("," in value or any(c.isspace() for c in value) for value in (member, room.external_id, account))):
        return {"ok": False, "status": "denied", "error_message": "请核对单个成员、账号并重新保存本群移出设置"}
    job = enqueue_job(db, job_type=JOB_TYPE, max_attempts=1, dedupe_key=f"ipad:remove:{channel.id}:{request_id}",
                      payload={"channel_id": channel.id, "wxid": account, "command": {
                          "type": "kick_group_member", "request_id": request_id,
                          "params": {"room_id": room.external_id, "member_id": member}}})
    return {"ok": True, "status": "pending", "job_id": job.id}


def _outcome(reply, member):
    if (not isinstance(reply, dict) or reply.get("http_status", 200) != 200 or reply.get("Success") is not True
            or type(reply.get("Code")) is not int or reply["Code"] != 0):
        return "unknown"
    data = reply.get("Data")
    base = data.get("baseResponse") if isinstance(data, dict) else None
    if not isinstance(base, dict) or type(base.get("ret")) is not int:
        return "unknown"
    if base["ret"] != 0:
        return "failed"
    rows = data.get("MemberList")
    if (type(data.get("MemberCount")) is int and data["MemberCount"] == 1 and isinstance(rows, list)
            and len(rows) == 1 and isinstance(rows[0], dict) and isinstance(rows[0].get("MemberName"), dict)
            and rows[0]["MemberName"].get("string") == member):
        return "kicked"
    return "unknown"


def _live_version(client, target, wxid, room_id, member):
    kwargs = {"wxid": wxid, "chatroom_id": room_id}
    profile = client.get_chatroom_info(target, **kwargs)
    roster = client.get_chatroom_member_detail(target, **kwargs)
    if any(r.get("http_status", 200) != 200 for r in (profile, roster)):
        return None
    owner = _normalize_chatroom_profile(profile, room_id).get("owner_external_id")
    snapshot = _normalize_chatroom_members(roster, room_id)
    if not owner or not snapshot or member in {owner, wxid}:
        return None
    version, rows = snapshot
    flags = {r["member_external_id"]: r["member_flag"] for r in rows}
    # 未识别标志可以作为目录展示，绝不能据此认定目标可以被移除。
    if (wxid not in flags or (wxid != owner and flags[wxid] not in {"2049", "2057"})
            or flags.get(member) not in {"0", "1"}):
        return None
    return version


def process(db, job, payload):
    channel = db.get(Channel, payload.get("channel_id"))
    if not channel or channel.channel_type != "wechat_ipad" or job.job_type != JOB_TYPE or job.related_type != "group_moderation":
        fail_job_terminal(db, job, error_message="移出任务与账号不匹配")
        return
    client = WechatIpadApiClient()
    # 第一次持锁读确定目标；只读探测后再次锁定并复核，跨网络期间不占用数据库写锁。
    def allowed():
        violation, room = _locked_target(db, channel, job)
        if not violation:
            fail_job_terminal(db, job, error_message="移出任务与违规记录不匹配")
            return None
        if job.status != "running":
            db.commit()
            return None
        if violation.status == "unknown":
            if not violation.finished_at:
                violation.finished_at = now_shanghai()
                notices.record_failure(db, violation, UNKNOWN)
            fail_job_terminal(db, job, error_message=UNKNOWN)
            return None
        db.refresh(channel)
        saved = parse_job_payload(job)
        if not review_locked(db, channel, job, violation, room):
            db.commit()
            return None
        if (not ipad_removal_confirmed(room) or room.management_status != "enabled" or channel.deleted_at or not channel.api_base
                or channel.channel_type != "wechat_ipad" or account_identity(channel)["external_id"] != saved.get("wxid")
                or saved["command"].get("request_id") != f"moderation_{violation.id}"):
            _cancel(db, job, violation, "群设置或协议账号已变更")
            return None
        return violation, room

    pair = allowed()
    if not pair:
        return
    violation, room = pair
    wxid, room_id, member = parse_job_payload(job)["wxid"], room.external_id, violation.member_external_id
    target = Channel(api_base=channel.api_base)
    db.commit()
    try:
        version = _live_version(client, target, wxid, room_id, member)
    except Exception:
        version = None
    pair = allowed()
    if not pair:
        return
    violation, room = pair
    if (version is None or version < parse_config_json(room.extra_json).get("ipad_roster_version", -1)
            or channel.api_base != target.api_base or account_identity(channel)["external_id"] != wxid
            or room.external_id != room_id or violation.member_external_id != member):
        _cancel(db, job, violation, "实时群资料未确认本账号权限或目标成员身份")
        return
    # 先持久化不确定状态。即使在发送前后崩溃，恢复任务也不会二次执行。
    violation.status, violation.result_note = "unknown", UNKNOWN
    db.commit()
    try:
        reply = client.request_json(target, IPAD_874_CONTRACT.endpoints.group_remove_member,
                                    body={"Wxid": wxid, "ChatRoomName": room_id, "ToWxids": member})
        status = _outcome(reply, member)
    except Exception:
        status = "unknown"
    violation, room = _locked_target(db, channel, job)
    if not violation or violation.status != "unknown":
        db.commit()
        return
    violation.status, violation.finished_at = status, now_shanghai()
    if status == "kicked":
        violation.result_note = "微信已确认移出指定成员"
        conversation = db.get(Conversation, violation.conversation_id)
        db.refresh(channel)
        if (conversation and conversation.tenant_id == channel.tenant_id and conversation.bot_id == channel.bot_id
                and conversation.channel_id == channel.id and conversation.external_id == room_id
                and channel.is_enabled and not channel.deleted_at and account_identity(channel)["external_id"] == wxid
                and room.is_active and room.management_status == "enabled" and get_state(room, FEATURE)[0]):
            notices.send(db, channel, conversation, violation, violation.kick_notice or "", "kicked")
        complete_job(db, job, result={"moderation": {"status": status, "violation_id": violation.id}})
    else:
        reason = UNKNOWN if status == "unknown" else "微信明确拒绝移出成员"
        notices.record_failure(db, violation, reason)
        fail_job_terminal(db, job, error_message=reason)


def _cancel(db, job, violation, reason):
    violation.status, violation.finished_at = "cancelled", now_shanghai()
    notices.record_failure(db, violation, reason)
    job.status, job.finished_at, job.error_message = "cancelled", now_shanghai(), reason
    db.commit()
