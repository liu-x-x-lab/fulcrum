"""治理提示复用平台出站队列，跟随事务提交并通过台账编号防重复。"""

import re

from app.models.entities import ErrorLog
from app.services.outbound_batch import enqueue_outbound_job
from app.services.task_queue import OUTBOUND_DISPATCH_JOB, enqueue_job


def render(template: str, *, member: str, count: int, limit: int, group: str) -> str:
    # 只替换一次；昵称中的花括号不能变成微桥 @ 指令或再次展开的模板变量。
    values = {"member": member, "count": str(count), "limit": str(limit), "group": group}
    values = {key: str(value).translate(str.maketrans({"{": "（", "}": "）"})) for key, value in values.items()}
    return re.sub(r"\{(member|count|limit|group)\}", lambda match: values[match[1]], template)[:4000]


def send(db, channel, conversation, violation, text: str, kind: str) -> None:
    if not text:
        return
    payload = {"message_type": "text", "content": text, "receiver_id": conversation.external_id,
               "external_id": conversation.external_id, "chat_type": "group"}
    if channel.channel_type == "wechat_webridge":
        # 微桥直发入口会 commit，不能在计数事务中调用；交给现有出站 worker 再生成手机命令和消息记录。
        enqueue_job(db, job_type=OUTBOUND_DISPATCH_JOB,
                    payload={"channel_id": channel.id, "conversation_id": conversation.id,
                             "plugin_code": "group_feature:moderation", "payload": payload},
                    related_type="group_moderation_notice", related_id=violation.id, priority=110,
                    dedupe_key=f"moderation-notice:{violation.id}:{kind}", max_attempts=1)
        return
    enqueue_outbound_job(
        db, channel=channel, conversation=conversation,
        payload=payload, plugin_code="group_feature:moderation",
        related_type="group_moderation_notice", related_id=violation.id, priority=110,
        dedupe_key=f"moderation-notice:{violation.id}:{kind}", max_attempts=1,
    )


def record_failure(db, violation, reason: str) -> None:
    violation.result_note = reason[:255]
    # 敏感词和原文不写入通用日志，运营人员可从错误中心定位群和具体台账。
    db.add(ErrorLog(tenant_id=violation.tenant_id, source="group_moderation", level="WARNING",
                    message=f"群治理未完成 | chatroom_id={violation.chatroom_id} | violation_id={violation.id} | {reason[:255]}"))
