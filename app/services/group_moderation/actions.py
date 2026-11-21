"""微桥治理命令的领取复核与结果落账，不自动重放结果不明的踢人操作。"""

from datetime import timedelta

from sqlalchemy import func, select, update

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Chatroom, Conversation
from app.models.group_moderation import GroupModerationViolation as Violation
from app.services.channel_group_operations import removal_permission_error
from app.services.group_features.moderation import FEATURE
from app.services.group_features.store import get_state
from app.services.group_moderation import notices
from app.services.inbound_policy import parse_timestamp_value
from app.services.task_queue import complete_job, fail_job_terminal, parse_job_payload

RELATED_TYPE = "group_moderation"


def _target(db, channel, job):
    """任务、违规记录、群都以当前可信租户和渠道核对，不能借 related_id 越权。"""
    if job.tenant_id != require_tenant_id() or channel.tenant_id != job.tenant_id:
        return None, None
    violation = db.scalar(select(Violation).where(
        Violation.id == job.related_id, Violation.tenant_id == channel.tenant_id,
        Violation.channel_id == channel.id, Violation.bot_id == channel.bot_id,
        Violation.action_job_id == job.id,
    ))
    room = db.get(Chatroom, violation.chatroom_id) if violation else None
    if room is None or room.tenant_id != channel.tenant_id or room.channel_id != channel.id or room.bot_id != channel.bot_id:
        return None, None
    data = parse_job_payload(job)
    command = data.get("command") or {}
    params = command.get("params") or {}
    if (data.get("channel_id") != channel.id or command.get("type") != "kick_group_member"
            or params != {"room_id": room.external_id, "member_id": violation.member_external_id}):
        return None, None
    return violation, room


def _locked_target(db, channel, job):
    """和计数使用同一群写锁，领取、超时和回执互斥，避免覆盖另一事务的终态。"""
    violation, room = _target(db, channel, job)
    if room is not None:
        db.flush()
        db.execute(update(Chatroom).where(Chatroom.id == room.id, Chatroom.tenant_id == channel.tenant_id)
                   .values(id=Chatroom.id, updated_at=Chatroom.updated_at), execution_options={"synchronize_session": False})
        db.refresh(room, with_for_update=True)
        db.refresh(job, with_for_update=True)
        db.refresh(violation, with_for_update=True)
    return violation, room


def claim_allowed(db, channel, job) -> bool:
    """排队后可能关闭治理、调整权限或成员退群，领取时必须复核，过期请求不补踢。"""
    violation, room = _locked_target(db, channel, job)
    return review_locked(db, channel, job, violation, room)


def review_locked(db, channel, job, violation, room) -> bool:
    """调用方须已取得本次任务、台账及群的同一把锁。"""
    ipad = channel.channel_type == "wechat_ipad"
    if job.status != ("running" if ipad else "pending"):
        return False
    reason = "治理任务所属群或违规记录不存在"
    if violation and room:
        enabled, config = get_state(room, FEATURE)
        reason = removal_permission_error(db, channel, room, violation.member_external_id)
        if not ipad:
            from app.services.wechat_webridge.service import _runtime
            capabilities = (_runtime(channel).get("webridge") or {}).get("capabilities", [])
        expiry = parse_timestamp_value(parse_job_payload(job).get("moderation_expires_at"))
        query = select(func.count(Violation.id)).where(
            Violation.tenant_id == channel.tenant_id, Violation.chatroom_id == room.id,
            Violation.member_external_id == violation.member_external_id,
        )
        if config["window_hours"]:
            query = query.where(Violation.created_at >= now_shanghai() - timedelta(hours=config["window_hours"]))
        count = int(db.scalar(query.with_for_update()) or 0)
        if not enabled or config["mode"] != "kick":
            reason = "本群已关闭自动踢出"
        elif not ipad and "kick_group_member" not in capabilities:
            reason = "设备未声明移出成员能力"
        elif violation.member_external_id in {item.strip() for item in config["exempt_members"].splitlines()}:
            reason = "成员已加入豁免名单"
        elif count < config["threshold"]:
            reason = "当前违规次数未达到新配置的踢出阈值"
        elif expiry is None or now_shanghai() > expiry:
            reason = "踢人请求已超过 10 分钟，取消延迟处罚"
        elif violation.status != "pending":
            reason = "治理任务已处理"
        if not reason:
            return True
        if violation.status == "pending":
            violation.status = "cancelled"
            violation.finished_at = now_shanghai()
            notices.record_failure(db, violation, reason)
    job.status, job.error_message, job.finished_at = "cancelled", reason, now_shanghai()
    return False


def settle_timeout(db, channel, job) -> bool:
    """手机已领取却没有回报，不能断言失败，更不能自动再次踢同一个人。"""
    violation, _room = _locked_target(db, channel, job)
    if job.status != "running":
        return False
    if violation and violation.status == "pending":
        violation.status = "unknown"
        violation.finished_at = now_shanghai()
        notices.record_failure(db, violation, "手机回报超时，踢人结果未知，请核实成员状态")
    return True


def accept_result(db, channel, job, payload: dict) -> dict:
    """只有领取过的命令可回报；群锁让重复/并发回报与成功提示保持同一事务。"""
    saved_request = (parse_job_payload(job).get("command") or {}).get("request_id")
    incoming_request = payload.get("request_id") or (payload.get("command") or {}).get("request_id")
    if not saved_request or incoming_request != saved_request:
        raise ValueError("moderation_request_mismatch")
    violation, room = _locked_target(db, channel, job)
    if violation is None:
        raise ValueError("moderation_action_not_found")
    # 超时任务仍可接受迟到的确定回报；未知状态只禁止再次下发处罚，不应吞掉成功证据。
    late = violation.status == "unknown" and job.status == "failed" and bool(job.started_at)
    if not late and (job.status in {"completed", "failed", "cancelled"} or violation.status != "pending"):
        db.commit()
        return {"ok": True, "deduplicated": True, "job_id": job.id, "status": job.status}
    if job.status != "running" and not late:
        raise ValueError("moderation_command_not_claimed")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    inner = result.get("result") if isinstance(result.get("result"), dict) else result
    # 外层成功也不能覆盖内层明确失败；只有布尔 True 可作为成功证据。
    ok = (payload.get("ok") if "ok" in payload else result.get("ok")) is True
    if result.get("ok") is False or inner.get("ok") is False:
        ok = False
    error = "微桥明确拒绝移出成员，请检查设备权限与运行状态"
    # 协议成功证据包含 room_id/member_ids/member_count；不能只看 HTTP 或信封 ok。
    targets = inner.get("member_ids")
    valid_target = (inner.get("room_id") == room.external_id and isinstance(targets, list)
                    and targets == [violation.member_external_id] and inner.get("member_count") == 1
                    and not isinstance(inner.get("member_count"), bool))
    uncertain = (payload.get("ok") is not False and result.get("ok") is not False
                 and inner.get("ok") is not False and (not ok or not valid_target))
    if inner.get("error_code") in {"OPERATION_TIMEOUT", "RESULT_UNKNOWN"} or inner.get("result_unknown") is True:
        uncertain = True
    ok = ok and valid_target and not uncertain
    if uncertain:
        error = "微桥回报缺少一致的成功证据，结果待确认，请核实成员状态"
        if late:
            # 相同的不确定回报可以正常 ACK，但不能反复追加错误记录或通知。
            db.commit()
            return {"ok": True, "deduplicated": True, "job_id": job.id, "status": job.status}
    violation.finished_at = now_shanghai()
    if ok:
        violation.status, violation.result_note = "kicked", "手机已确认移出群聊"
        conversation = db.get(Conversation, violation.conversation_id)
        if (conversation and conversation.tenant_id == channel.tenant_id and conversation.channel_id == channel.id
                and conversation.bot_id == channel.bot_id and conversation.external_id == room.external_id):
            from app.services.wechat_webridge.member_directory import apply_event

            # 复用目录事件版本保护；迟到回执不能覆盖处罚后再次入群的较新事件。
            apply_event(db, channel, {
                "event_key": f"moderation-kicked:{violation.id}",
                "details": {"timestamp": ensure_shanghai_datetime(job.started_at).isoformat(),
                            "member_change": {"change_type": "removed", "members": [
                                {"external_id": violation.member_external_id, "display_name": violation.member_external_id}]}},
            }, conversation)
            if channel.is_enabled and room.is_active and get_state(room, FEATURE)[0]:
                notices.send(db, channel, conversation, violation, violation.kick_notice or "", "kicked")
        complete_job(db, job, result={"moderation": {"status": "kicked", "violation_id": violation.id}})
    else:
        violation.status = "unknown" if uncertain else "failed"
        notices.record_failure(db, violation, error)
        fail_job_terminal(db, job, error_message=error, result={"moderation": {"status": violation.status, "violation_id": violation.id}})
    return {"ok": True, "job_id": job.id, "status": job.status}
