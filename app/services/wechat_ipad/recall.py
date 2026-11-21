"""874 文本撤回：只使用原发送回执，下发过的操作在重启后不能重放。"""

import json

from sqlalchemy import update

from app.core.tenant_context import require_tenant_id
from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation, Message, TaskJob
from app.services.task_queue import complete_job, enqueue_job, fail_job_terminal
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _parse_json
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT


JOB_TYPE = "ipad_message_revoke"
UNKNOWN = "撤回结果未知，请核对微信会话；后台不会再次下发"


def _zero(value):
    return type(value) is int and value == 0


def text_receipt(response):
    """SendTxt 的字段大小写取自 NewMsgResponeNew；不可从 MsgId 或本地时间推算。"""
    data = response.get("Data")
    if (response.get("http_status", 200) != 200 or response.get("Success") is not True or not _zero(response.get("Code"))
            or not isinstance(data, dict) or not isinstance(data.get("BaseResponse"), dict)
            or not _zero(data["BaseResponse"].get("ret"))):
        return None
    rows = data.get("List")
    if data.get("Count", 1) != 1 or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    if not _zero(row.get("Ret")):
        return None
    ids = (row.get("ClientMsgid"), row.get("Createtime"), row.get("NewMsgId"))
    if any(type(v) is not int or not 0 < v < 2**64 for v in ids):
        return None
    target = WechatIpadApiClient.unwrap_value(row.get("ToUsetName"))
    if not isinstance(target, str) or not target:
        return None
    return dict(to_wxid=target, client_msg_id=ids[0], create_time=ids[1], new_msg_id=ids[2])


def recall_params(channel, conversation, message):
    if (channel.channel_type != "wechat_ipad" or channel.deleted_at or not channel.is_enabled or not channel.api_base
            or channel.id != conversation.channel_id or channel.bot_id != conversation.bot_id
            or message.conversation_id != conversation.id or channel.tenant_id != conversation.tenant_id
            or message.tenant_id != channel.tenant_id or message.direction != "out" or message.status != "sent"
            or message.message_type != "text"):
        return None
    raw = _parse_json(message.raw_request_json)
    body = raw.get("json")
    wxid = _parse_json(channel.adapter_config_json).get("wxid")
    if (not isinstance(body, dict) or not wxid or body.get("Wxid") != wxid
            or body.get("ToWxid") != conversation.external_id or
            not str(raw.get("url", "")).endswith(IPAD_874_CONTRACT.endpoints.message_send_text)):
        return None
    params = text_receipt(_parse_json(message.raw_response_json))
    if params and params["to_wxid"] == conversation.external_id:
        return {**params, "wxid": wxid}
    return None


def _lock_message(db, message_id):
    # SQLite 不支持 SELECT FOR UPDATE，先拿写锁，再重读另一请求刚提交的状态。
    db.execute(update(Message).where(Message.id == message_id, Message.tenant_id == require_tenant_id())
               .values(id=Message.id, updated_at=Message.updated_at), execution_options={"synchronize_session": False})
    message = db.get(Message, message_id, populate_existing=True, with_for_update=True)
    return message if message and message.tenant_id == require_tenant_id() else None


def _set_state(message, job, state, error=""):
    raw = _parse_json(message.raw_request_json)
    raw["revoke"] = {"job_id": job.id, "state": state, "error": error}
    message.raw_request_json = json.dumps(raw, ensure_ascii=False)


def enqueue(db, channel, conversation, message, window):
    message = _lock_message(db, message.id)
    params = recall_params(channel, conversation, message) if message else None
    if not params:
        raise ValueError("没有与当前账号、会话匹配的 874 文本发送回执，无法撤回")
    age = now_shanghai().timestamp() - params["create_time"]
    if not 0 <= age < window:
        raise ValueError("原消息不在微信的 2 分钟撤回期限内")
    state = (_parse_json(message.raw_request_json).get("revoke") or {}).get("state")
    if state == "unknown":
        raise ValueError(UNKNOWN)
    if state == "failed":
        raise ValueError("此消息的撤回请求已失败，不重复下发")
    if state not in {"pending", "dispatching"}:
        job = enqueue_job(db, job_type=JOB_TYPE, related_type="message", related_id=message.id,
                          priority=120, max_attempts=1, dedupe_key=f"ipad:revoke:{message.id}",
                          payload={"channel_id": channel.id, "conversation_id": conversation.id})
        _set_state(message, job, "pending")
    return {"ok": True, "notice": "已提交撤回请求，等待 874 回执"}


def process(db, job: TaskJob, payload):
    from app.services.message_actions import REVOKE_SECONDS

    message = _lock_message(db, job.related_id)
    channel = db.get(Channel, payload.get("channel_id"))
    conversation = db.get(Conversation, payload.get("conversation_id"))
    saved = (_parse_json(message.raw_request_json).get("revoke") or {}) if message else {}
    if (job.job_type != JOB_TYPE or job.tenant_id != require_tenant_id() or not message
            or saved.get("job_id") != job.id or job.related_type != "message"):
        fail_job_terminal(db, job, error_message="撤回任务与原消息不匹配")
        return
    if saved.get("state") != "pending":
        # 恢复已投递任务时只结算，不重发。迟到的原执行回执仍可将 unknown 修正为确定结果。
        if saved.get("state") == "revoked":
            complete_job(db, job, result={"state": "revoked"})
        else:
            if saved.get("state") == "dispatching":
                _set_state(message, job, "unknown", UNKNOWN)
            fail_job_terminal(db, job, error_message=saved.get("error") or UNKNOWN)
        return
    params = recall_params(channel, conversation, message) if channel and conversation else None
    if not params or not 0 <= now_shanghai().timestamp() - params["create_time"] < REVOKE_SECONDS:
        error = "原账号或回执无效，或已超过撤回期限"
        _set_state(message, job, "failed", error)
        fail_job_terminal(db, job, error_message=error)
        return
    # 将原账号/服务地址固定在校验时，网络调用期间不持有群或消息写锁。
    target = Channel(api_base=channel.api_base)
    _set_state(message, job, "dispatching")
    db.commit()
    try:
        reply = WechatIpadApiClient().revoke_message(target, **params)
        data = reply.get("Data")
        base = data.get("BaseResponse") if isinstance(data, dict) else None
        ret = base.get("ret") if isinstance(base, dict) else None
        confirmed = (reply.get("http_status", 200) == 200 and reply.get("Success") is True
                     and _zero(reply.get("Code")) and type(ret) is int)
    except (OSError, ValueError):
        confirmed, ret = False, None
    message = _lock_message(db, job.related_id)
    # 微信的撤回通知也可能先于本次 HTTP 回执入库，不得用迟到的错误回滚已确认状态。
    if message.status == "revoked":
        _set_state(message, job, "revoked")
        complete_job(db, job, result={"state": "revoked"})
        return
    if confirmed and ret == 0:
        message.status = message.external_status = "revoked"
        _set_state(message, job, "revoked")
        complete_job(db, job, result={"state": "revoked"})
    else:
        state, error = ("failed", "微信拒绝撤回") if confirmed else ("unknown", UNKNOWN)
        _set_state(message, job, state, error)
        fail_job_terminal(db, job, error_message=error, result={"state": state})
