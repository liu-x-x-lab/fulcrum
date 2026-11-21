"""从 MP 历史轮询通知；水位和平台发送任务在同一调用方事务中保存。"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.entities import TaskJob
from app.models.moviepilot import MoviePilotDownload, MoviePilotSubscription
from app.services.outbound_batch import enqueue_outbound_job
from app.services.plugin_runtime.contracts import PluginExecutionResult
from plugins.moviepilot.client import MoviePilotClient, MoviePilotError, clean, digest, identity, positive_id, promotion, promotion_label
from plugins.moviepilot.state import CODE, binding_for, group_for, row_identity


def recent_history(api, kind, floor):
    """有界读取新历史，积压过大时保留水位并报告，不能静默跳到最新记录。"""
    found = {}
    for page in range(1, 6):
        rows = api.history(kind, page=page)
        for item in rows:
            found[positive_id(item.get("id"))] = item
        if len(rows) < 100 or any(positive_id(x.get("id")) <= floor for x in rows):
            return list(found.values())
    raise MoviePilotError("MoviePilot 通知积压超过 500 条，本轮保留水位；请管理员处理或重新登记关注。")


def event_promotion(db, row, event):
    """只对明确关联的下载使用提交时快照；原生历史无优惠字段时如实显示未知。"""
    if any(event.get(k) is not None for k in ("downloadvolumefactor", "uploadvolumefactor")):
        return "历史记录优惠：" + promotion_label(promotion(event))
    download_id = event.get("download_hash")
    if isinstance(download_id, str) and download_id:
        download = db.scalar(select(MoviePilotDownload).where(MoviePilotDownload.tenant_id == row.tenant_id,
            MoviePilotDownload.binding_id == row.binding_id, MoviePilotDownload.connection_key == row.connection_key,
            MoviePilotDownload.conversation_id == row.conversation_id, MoviePilotDownload.download_id == download_id,
            MoviePilotDownload.status == "submitted"))
        if download and identity(json.loads(download.media_json)) == row_identity(row):
            return "提交时优惠：" + promotion_label(json.loads(download.resource_json)["promotion"])
    return "优惠未知（MP 历史未提供优惠信息）"


def poll_notifications(db, request):
    binding = binding_for(db, request.context)
    if request.config.get("notifications_enabled", True) is not True:
        return PluginExecutionResult(ok=True, status="skipped", message="电影通知已关闭。")
    api = MoviePilotClient(request.config)
    query = select(MoviePilotSubscription).where(MoviePilotSubscription.tenant_id == binding.tenant_id,
        MoviePilotSubscription.binding_id == binding.id, MoviePilotSubscription.connection_key == api.connection_key,
        MoviePilotSubscription.status == "active", MoviePilotSubscription.notify_enabled.is_(True))
    if request.context.conversation_id:
        query = query.where(MoviePilotSubscription.conversation_id == request.context.conversation_id)
    rows = db.scalars(query.order_by(MoviePilotSubscription.id)).all()
    eligible = []
    groups = {}
    for row in rows:
        try:
            target = groups.get(row.conversation_id)
            if target is None:
                target = group_for(db, binding, row.conversation_id)
                groups[row.conversation_id] = target
            _, _, override = target
            # 显式开关覆盖由平台生成群级任务，全局任务跳过，以免重复轮询或绕过关闭设置。
            if not request.context.conversation_id and "notifications_enabled" in override:
                continue
            if override.get("notifications_enabled", True) is not True:
                continue
            saved = json.loads(row.notification_json)
            if not isinstance(saved.get("floor"), dict) or not isinstance(saved.get("seen"), dict):
                raise MoviePilotError("电影通知水位异常，请管理员检查登记记录。")
            eligible.append((row, saved))
        except MoviePilotError:
            # 已停用的群不访问 MP，也不推进水位；重新启用后仍可接续。
            if row.conversation_id not in groups:
                continue
            raise
    if not eligible:
        return PluginExecutionResult(ok=True, status="skipped", message="没有需要检查的电影关注。")
    histories = {kind: recent_history(api, kind, min(int(s["floor"][kind]) for _, s in eligible))
                 for kind in ("download", "transfer")}
    if any(type(item.get("status")) is not bool for item in histories["transfer"]):
        raise MoviePilotError("MoviePilot 整理状态格式异常，本轮未推进水位。")
    queued = 0
    for row, saved in eligible:
        conversation, channel, _ = group_for(db, binding, row.conversation_id)
        seen = saved["seen"]
        for kind, history in histories.items():
            floor = int(saved["floor"][kind])
            for event in sorted(history, key=lambda x: positive_id(x["id"])):
                event_id = positive_id(event["id"])
                key = f"{kind}:{event_id}"
                if event_id <= floor and key not in seen:
                    continue
                if identity(event) != row_identity(row):
                    continue
                if kind == "transfer" and type(event.get("status")) is not bool:
                    raise MoviePilotError("MoviePilot 整理状态格式异常，本轮未推进水位。")
                status = "submitted" if kind == "download" else ("success" if event["status"] else "failed")
                if seen.get(key) == status:
                    continue
                phrase = {"submitted": "已提交下载", "success": "整理完成", "failed": "整理失败，请到 MP 查看"}[status]
                text = f"MoviePilot · {phrase}\n{clean(row.title)}（{clean(row.year, 16)}）"
                if kind == "download" and event.get("torrent_name"):
                    text += "\n" + clean(event["torrent_name"], 220)
                text += "\n" + event_promotion(db, row, event)
                # 幂等键不含用户：同一群多人关注同一影片只排一条通知；跨群仍各自接收。
                event_key = digest(api.connection_key, conversation.id, kind, event_id, status)
                dedupe = f"mp:{binding.id}:{event_key}"
                existing = select(TaskJob.id).where(TaskJob.tenant_id == binding.tenant_id, TaskJob.dedupe_key == dedupe)
                if db.scalar(existing) is None:
                    try:
                        # 平台队列以唯一键拒绝重复，不会自动返回既有任务；保存点隔离并发冲突。
                        with db.begin_nested():
                            enqueue_outbound_job(db, conversation=conversation, channel=channel,
                                payload={"external_id": conversation.external_id, "receiver_id": conversation.external_id,
                                    "chat_type": "group", "message_type": "text", "content": text},
                                plugin_code=CODE, related_type="moviepilot_subscription", related_id=row.id,
                                priority=60, max_attempts=1, dedupe_key=dedupe)
                        queued += 1
                    except IntegrityError:
                        if db.scalar(existing) is None:
                            raise
                seen[key] = status
            saved["floor"][kind] = max([floor, *(positive_id(x["id"]) for x in history)])
        # 保存最近状态以识别整理失败后重试成功；不把种子链接、目录或上游错误存入状态。
        saved["seen"] = dict(sorted(seen.items(), key=lambda pair: int(pair[0].split(":")[1]))[-500:])
        row.notification_json = json.dumps(saved, ensure_ascii=False, sort_keys=True)
    db.flush()
    return PluginExecutionResult(ok=True, status="completed", message=f"电影状态检查完成，已排队 {queued} 条通知。")
