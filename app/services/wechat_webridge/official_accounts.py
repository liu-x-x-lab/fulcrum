"""通过公众号专用列表接口补齐消息页的名称与头像,独立于普通好友目录。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel, TaskJob
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import service

SYNC_PRIORITY = 18
PAGE_SIZE = 100
REFRESH_SECONDS = 3600
RETRY_SECONDS = 300
STATE_KEY = "official_account_directory"


def _save_state(channel: Channel, **updates: Any) -> None:
    runtime = service._runtime(channel)
    runtime[STATE_KEY] = {**service.as_object(runtime.get(STATE_KEY)), **updates}
    service._save_runtime(channel, runtime)


def _enqueue_page(db: Session, channel: Channel, offset: int) -> TaskJob:
    job = service.enqueue_command(
        db, channel, command_name="official_accounts",
        args={"type": "all", "limit": PAGE_SIZE, "offset": offset}, priority=SYNC_PRIORITY,
    )
    data = parse_job_payload(job)
    # 只为内部完整同步续页;通用命令桥的关键词/类型筛选仍是单次查询。
    data[STATE_KEY] = True
    job.payload_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
    _save_state(channel, requested_at=now_shanghai().isoformat())
    db.flush()
    return job


def request_sync(db: Session, channel: Channel, *, force: bool = False) -> TaskJob | None:
    """手机轮询时自动补齐;失败退避,未完成的同渠道任务合并。调用方负责提交。"""

    if channel.is_enabled is False or channel.deleted_at is not None:
        return None
    db.flush()
    jobs = db.scalars(select(TaskJob).where(
        TaskJob.job_type == service.JOB_TYPE, TaskJob.status.in_(("pending", "running")),
        TaskJob.priority == SYNC_PRIORITY,
    )).all()
    for job in jobs:
        data = parse_job_payload(job)
        if int(data.get("channel_id") or 0) == int(channel.id) and data.get(STATE_KEY):
            return job
    state = service.as_object(service._runtime(channel).get(STATE_KEY))
    if not force:
        age = service._seconds_since(state.get("synced_at"))
        since_request = service._seconds_since(state.get("requested_at"))
        if (age is not None and age < REFRESH_SECONDS) or (since_request is not None and since_request < RETRY_SECONDS):
            return None
    return _enqueue_page(db, channel, 0)


def successful_result(payload: dict[str, Any], result: dict[str, Any]) -> bool:
    """登录/联系人未就绪及异常响应不能当作成功的空目录。"""

    return (
        payload.get("ok", result.get("ok")) is True
        and result.get("ok") is not False
        and result.get("status", "ok") == "ok"
        and isinstance(result.get("items"), list)
    )


def after_result(db: Session, channel: Channel, data: dict[str, Any], result: dict[str, Any]) -> None:
    """按返回的实际条数续取下一页,最后一页才标记完整同步成功。"""

    if not data.get(STATE_KEY):
        return
    args = service.as_object(service.as_object(data.get("command")).get("params"))
    offset = args.get("offset", 0)
    if result.get("offset", offset) != offset:
        return
    if result.get("has_more") is True:
        items = result["items"]
        # 空页不能推进游标;异常响应留待退避后重试,不无限下发相同分页。
        next_offset = offset + len(items)
        if items and next_offset <= 4294967295:
            _enqueue_page(db, channel, next_offset)
    elif result.get("has_more") is False:
        _save_state(channel, synced_at=now_shanghai().isoformat())
