"""租户首页及统计聚合，仅在当前租户的可见数据范围内生成趋势。"""

from datetime import datetime, timedelta
import subprocess

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.application import APP_ROOT
from app.core.time import SHANGHAI_TZ, ensure_shanghai_datetime, hour_bucket_shanghai, now_shanghai
from app.core.logging import get_logger
from app.models.entities import (
    AlertRecord,
    Bot,
    BotPlugin,
    Channel,
    Conversation,
    ErrorLog,
    Message,
    MessageActivityHourly,
    Plugin,
    PluginActivityHourly,
    PluginExecutionLog,
    TaskJob,
)
from app.services.identity_directory import IdentityDirectory
from app.web.channels.definitions import BUILTIN_CHANNEL_TYPES
from app.web.shared.access import get_session
from app.web.shared.presentation import render


logger = get_logger(__name__)
router = APIRouter()


def get_cron_probe_snapshot() -> dict[str, str]:
    probe_script = APP_ROOT / "scripts" / "fulcrum_cron_probe.sh"
    if not probe_script.exists():
        return {"recommended_state": "unknown", "probe_error": "missing_script"}
    try:
        result = subprocess.run(
            [str(probe_script)],
            cwd=APP_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return {"recommended_state": "unknown", "probe_error": str(exc)}

    snapshot: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        snapshot[key.strip()] = value.strip()

    if result.returncode != 0:
        snapshot.setdefault("recommended_state", "unknown")
        stderr = (result.stderr or "").strip()
        snapshot["probe_error"] = stderr or f"probe_exit_{result.returncode}"

    snapshot.setdefault("recommended_state", "unknown")
    return snapshot


def _normalize_dt_utc(value: datetime | None) -> datetime:
    return ensure_shanghai_datetime(value)


def _hour_bucket_utc(value: datetime | None = None) -> datetime:
    return hour_bucket_shanghai(value)


def build_time_series_chart(items: list[dict[str, object]], *, value_key: str, tone: str = "primary") -> dict[str, object]:
    values = [max(int(item.get(value_key) or 0), 0) for item in items]
    max_value = max(values) if values else 0
    points: list[str] = []
    bars: list[dict[str, object]] = []
    count = len(values)
    for index, value in enumerate(values):
        x = 0 if count <= 1 else round((index / (count - 1)) * 100, 2)
        y = 100 if max_value <= 0 else round(100 - ((value / max_value) * 100), 2)
        points.append(f"{x},{y}")
        bars.append(
            {
                "label": str(items[index].get("label") or ""),
                "value": value,
                "height_percent": 0 if max_value <= 0 or value <= 0 else max(round((value / max_value) * 100, 2), 3),
            }
        )
    return {
        "max_value": max_value,
        "points": " ".join(points) if points else "",
        "items": bars,
        "tone": tone,
    }


def build_dashboard_trend_series(
    db: Session,
    *,
    hours: int = 24,
) -> dict[str, object]:
    now_bucket = _hour_bucket_utc()
    start_bucket = now_bucket - timedelta(hours=hours - 1)
    message_stmt = select(MessageActivityHourly).where(
        MessageActivityHourly.bucket_start >= start_bucket,
        MessageActivityHourly.bucket_start <= now_bucket,
    )
    plugin_stmt = select(PluginActivityHourly).where(
        PluginActivityHourly.bucket_start >= start_bucket,
        PluginActivityHourly.bucket_start <= now_bucket,
    )
    message_rows = db.scalars(message_stmt.order_by(MessageActivityHourly.bucket_start.asc())).all()
    plugin_rows = db.scalars(plugin_stmt.order_by(PluginActivityHourly.bucket_start.asc())).all()
    logger.info(
        "dashboard trend query window",
        extra={
            "hours": hours,
            "start_bucket": start_bucket.isoformat(),
            "now_bucket": now_bucket.isoformat(),
            "message_hourly_rows": len(message_rows),
            "plugin_hourly_rows": len(plugin_rows),
        },
    )
    message_map: dict[datetime, dict[str, int]] = {}
    for row in message_rows:
        bucket = _normalize_dt_utc(row.bucket_start)
        item = message_map.setdefault(bucket, {"in": 0, "out": 0})
        direction = str(row.direction or "").strip().lower()
        if direction == "out":
            item["out"] += int(row.message_count or 0)
        else:
            item["in"] += int(row.message_count or 0)
    if message_rows:
        logger.info(
            "dashboard message hourly rows",
            extra={
                # 环境筛选已从当前 SaaS 架构移除；日志只记录已受租户会话约束的统计行，
                # 避免有数据时访问已删除变量，导致整个企业总览返回 500。
                "rows": [
                    {
                        "bucket_start": _normalize_dt_utc(row.bucket_start).isoformat(),
                        "bot_id": row.bot_id,
                        "channel_id": row.channel_id,
                        "channel_type": row.channel_type or "",
                        "chat_type": row.chat_type or "",
                        "direction": row.direction or "",
                        "message_count": int(row.message_count or 0),
                    }
                    for row in message_rows
                ],
            },
        )
    plugin_map: dict[datetime, dict[str, int]] = {}
    for row in plugin_rows:
        bucket = _normalize_dt_utc(row.bucket_start)
        item = plugin_map.setdefault(bucket, {"trigger": 0, "success": 0, "failed": 0})
        item["trigger"] += int(row.attempted_count or 0)
        item["success"] += int(row.success_count or 0)
        failures = int(row.failed_count or 0) + int(row.timeout_count or 0)
        item["failed"] += failures
    if not message_map:
        raw_message_stmt = (
            select(
                Message.created_at,
                Message.direction,
            )
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(Message.created_at >= start_bucket, Message.created_at <= now_bucket + timedelta(hours=1))
        )
        raw_message_rows = db.execute(raw_message_stmt).all()
        logger.info(
            "dashboard trend fallback to raw messages",
            extra={
                "raw_message_rows": len(raw_message_rows),
            },
        )
        for created_at, direction in raw_message_rows:
            bucket = _hour_bucket_utc(created_at)
            item = message_map.setdefault(bucket, {"in": 0, "out": 0})
            normalized_direction = str(direction or "").strip().lower()
            if normalized_direction == "out":
                item["out"] += 1
            else:
                item["in"] += 1
    if not plugin_map:
        raw_plugin_stmt = (
            select(
                PluginExecutionLog.created_at,
                PluginExecutionLog.ok,
                PluginExecutionLog.status,
                PluginExecutionLog.attempted,
            )
            .join(Bot, PluginExecutionLog.bot_id == Bot.id, isouter=True)
            .where(
                PluginExecutionLog.created_at >= start_bucket,
                PluginExecutionLog.created_at <= now_bucket + timedelta(hours=1),
                PluginExecutionLog.attempted.is_(True),
            )
        )
        for created_at, ok, status, attempted in db.execute(raw_plugin_stmt).all():
            if not attempted:
                continue
            bucket = _hour_bucket_utc(created_at)
            item = plugin_map.setdefault(bucket, {"trigger": 0, "success": 0, "failed": 0})
            item["trigger"] += 1
            if ok:
                item["success"] += 1
            if str(status or "").strip().lower() in {"error", "timeout", "outbound_failed"}:
                item["failed"] += 1
    timeline: list[dict[str, object]] = []
    for offset in range(hours):
        bucket = start_bucket + timedelta(hours=offset)
        message_item = message_map.get(bucket, {"in": 0, "out": 0})
        plugin_item = plugin_map.get(bucket, {"trigger": 0, "success": 0, "failed": 0})
        timeline.append(
            {
                "bucket": bucket.isoformat(),
                "label": bucket.astimezone(SHANGHAI_TZ).strftime("%H:%M"),
                "inbound": int(message_item["in"]),
                "outbound": int(message_item["out"]),
                "plugin_trigger": int(plugin_item["trigger"]),
                "plugin_success": int(plugin_item["success"]),
                "plugin_failed": int(plugin_item["failed"]),
            }
        )
    logger.info(
        "dashboard trend summary",
        extra={
            "inbound_max": max((int(item["inbound"]) for item in timeline), default=0),
            "outbound_max": max((int(item["outbound"]) for item in timeline), default=0),
            "plugin_trigger_max": max((int(item["plugin_trigger"]) for item in timeline), default=0),
            "nonzero_inbound_buckets": [
                {
                    "label": item["label"],
                    "bucket": item["bucket"],
                    "inbound": item["inbound"],
                    "outbound": item["outbound"],
                }
                for item in timeline
                if int(item["inbound"]) > 0 or int(item["outbound"]) > 0
            ],
        },
    )
    return {
        "items": timeline,
        "inbound_chart": build_time_series_chart(timeline, value_key="inbound", tone="primary"),
        "plugin_chart": build_time_series_chart(timeline, value_key="plugin_trigger", tone="success"),
    }


def build_dashboard_rankings(db: Session) -> dict[str, list[dict[str, object]]]:
    since_24h = now_shanghai() - timedelta(hours=24)

    bot_rows = db.execute(
        select(Bot.id, Bot.name, func.count(Message.id).label("message_count"))
        .join(Conversation, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
        .join(Message, Message.conversation_id == Conversation.id)
        .where(Message.created_at >= since_24h, Message.direction == "in", Bot.deleted_at.is_(None))
        .group_by(Bot.id, Bot.name)
        .order_by(func.count(Message.id).desc(), Bot.id.asc())
    ).all()
    chatroom_rows = db.execute(
        select(
            Conversation.channel_id,
            Conversation.external_id,
            Conversation.title,
            func.count(Message.id).label("message_count"),
        )
        .join(Message, Message.conversation_id == Conversation.id)
        .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
        .where(
            Conversation.chat_type == "group",
            Message.direction == "in",
            Message.created_at >= since_24h,
        )
        .group_by(Conversation.channel_id, Conversation.external_id, Conversation.title)
        .order_by(func.count(Message.id).desc(), Conversation.external_id.asc())
        .limit(10)
    ).all()

    user_rows = db.execute(
        select(
            Conversation.channel_id,
            Message.sender_id,
            func.max(Message.sender_name).label("sender_name"),
            func.count(Message.id).label("message_count"),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
        .where(
            Message.direction == "in",
            Message.sender_id.is_not(None),
            Message.created_at >= since_24h,
        )
        # 同一个 wxid 只在所属渠道内才有意义，按渠道+发送者分组才能正确解析昵称头像。
        .group_by(Conversation.channel_id, Message.sender_id)
        .order_by(func.count(Message.id).desc(), Message.sender_id.asc())
        .limit(10)
    ).all()
    # 活跃用户统一解析昵称头像，wxid 只作为弱信息显示在第二行。
    identity_directory = IdentityDirectory(db)
    room_ids_by_channel: dict[int, set[str]] = {}
    for row in chatroom_rows:
        room_ids_by_channel.setdefault(int(row.channel_id or 0), set()).add(str(row.external_id))
    for channel_id_value, room_ids in room_ids_by_channel.items():
        identity_directory.preload(channel_id_value, room_ids)
    user_ids_by_channel: dict[int, set[str]] = {}
    for row in user_rows:
        user_ids_by_channel.setdefault(int(row.channel_id or 0), set()).add(str(row.sender_id))
    for channel_id_value, sender_ids in user_ids_by_channel.items():
        identity_directory.preload(channel_id_value, sender_ids)

    plugin_rows = db.execute(
        select(
            PluginExecutionLog.plugin_code,
            func.count(PluginExecutionLog.id).label("trigger_count"),
            func.sum(PluginExecutionLog.outbound_count).label("outbound_count"),
        )
        .join(Bot, PluginExecutionLog.bot_id == Bot.id, isouter=True)
        .where(
            PluginExecutionLog.created_at >= since_24h,
            PluginExecutionLog.attempted.is_(True),
        )
        .group_by(PluginExecutionLog.plugin_code)
        .order_by(func.count(PluginExecutionLog.id).desc(), PluginExecutionLog.plugin_code.asc())
        .limit(10)
    ).all()

    return {
        "bots": [
            {"label": str(row.name or f"Bot #{row.id}"), "value": int(row.message_count or 0), "href": f"/bots/{int(row.id)}"}
            for row in bot_rows[:10]
        ],
        "chatrooms": [
            {
                # 群名优先取群档案，其次会话标题；两者都没有时才退回群 ID。
                "identity": identity_directory.get(int(row.channel_id or 0), row.external_id, row.title),
                "value": int(row.message_count or 0),
            }
            for row in chatroom_rows
        ],
        "users": [
            {
                "identity": identity_directory.get(int(row.channel_id or 0), row.sender_id, row.sender_name),
                "value": int(row.message_count or 0),
            }
            for row in user_rows
        ],
        "plugins": [
            {"label": str(row.plugin_code or "-"), "value": int(row.trigger_count or 0), "sub_label": f"自动出站 {int(row.outbound_count or 0)}"}
            for row in plugin_rows
        ],
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = get_session()
    try:
        recent_cutoff = now_shanghai() - timedelta(hours=24)
        today_cutoff = now_shanghai().replace(hour=0, minute=0, second=0, microsecond=0)
        # 总览始终统计当前租户的完整数据集；机器人环境已经从产品和数据库中删除。
        stats = {
            "bots": db.scalar(select(func.count(Bot.id)).where(Bot.deleted_at.is_(None))) or 0,
            "channels": db.scalar(select(func.count(Channel.id)).where(Channel.deleted_at.is_(None))) or 0,
            "plugins": db.scalar(select(func.count(Plugin.id))) or 0,
            "messages": db.scalar(select(func.count(Message.id))) or 0,
            "errors": db.scalar(select(func.count(ErrorLog.id))) or 0,
            "conversations": db.scalar(select(func.count(Conversation.id))) or 0,
        }
        ops_snapshot = {
            "enabled_bots": db.scalar(select(func.count(Bot.id)).where(Bot.is_enabled.is_(True), Bot.deleted_at.is_(None))) or 0,
            "enabled_channels": db.scalar(select(func.count(Channel.id)).where(Channel.is_enabled.is_(True), Channel.deleted_at.is_(None))) or 0,
            "online_channels": db.scalar(
                select(func.count(Channel.id)).where(
                    Channel.channel_type.in_(BUILTIN_CHANNEL_TYPES),
                    Channel.deleted_at.is_(None),
                    Channel.is_enabled.is_(True),
                    Channel.status == "online",
                )
            )
            or 0,
            "abnormal_channels": db.scalar(
                select(func.count(Channel.id)).where(
                    Channel.channel_type.in_(BUILTIN_CHANNEL_TYPES),
                    Channel.deleted_at.is_(None),
                    Channel.is_enabled.is_(True),
                    Channel.status.in_(("degraded", "offline", "failed")),
                )
            )
            or 0,
            "inbound_24h": db.scalar(
                select(func.count(Message.id)).where(Message.direction == "in", Message.created_at >= recent_cutoff)
            )
            or 0,
            "recent_errors_24h": db.scalar(select(func.count(ErrorLog.id)).where(ErrorLog.created_at >= recent_cutoff)) or 0,
            "failed_jobs": db.scalar(select(func.count(TaskJob.id)).where(TaskJob.status == "failed")) or 0,
            "pending_jobs": db.scalar(select(func.count(TaskJob.id)).where(TaskJob.status.in_(("pending", "running")))) or 0,
        }
        total_open_alerts = db.scalar(select(func.count(AlertRecord.id)).where(AlertRecord.status == "open")) or 0
        active_bot_stmt = (
            select(func.count(func.distinct(Conversation.bot_id)))
            .join(Message, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(Message.created_at >= recent_cutoff)
        )
        active_user_stmt = (
            select(func.count(func.distinct(Message.sender_id)))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(
                Message.direction == "in",
                Message.sender_id.is_not(None),
                Message.created_at >= recent_cutoff,
            )
        )
        active_group_stmt = (
            select(func.count(func.distinct(Conversation.external_id)))
            .join(Message, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(
                Conversation.chat_type == "group",
                Message.direction == "in",
                Message.created_at >= recent_cutoff,
            )
        )
        inbound_today_stmt = (
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(Message.direction == "in", Message.created_at >= today_cutoff)
        )
        outbound_24h_stmt = (
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(Message.direction == "out", Message.created_at >= recent_cutoff)
        )
        plugin_trigger_stmt = (
            select(func.count(PluginExecutionLog.id))
            .join(Bot, PluginExecutionLog.bot_id == Bot.id, isouter=True)
            .where(
                PluginExecutionLog.created_at >= recent_cutoff,
                PluginExecutionLog.attempted.is_(True),
            )
        )
        plugin_success_stmt = (
            select(func.count(PluginExecutionLog.id))
            .join(Bot, PluginExecutionLog.bot_id == Bot.id, isouter=True)
            .where(
                PluginExecutionLog.created_at >= recent_cutoff,
                PluginExecutionLog.attempted.is_(True),
                PluginExecutionLog.ok.is_(True),
            )
        )
        plugin_attempt_stmt = (
            select(func.count(PluginExecutionLog.id))
            .join(Bot, PluginExecutionLog.bot_id == Bot.id, isouter=True)
            .where(
                PluginExecutionLog.created_at >= recent_cutoff,
                PluginExecutionLog.attempted.is_(True),
            )
        )
        plugin_success_count = db.scalar(plugin_success_stmt) or 0
        plugin_attempt_count = db.scalar(plugin_attempt_stmt) or 0
        plugin_success_rate = 0 if plugin_attempt_count <= 0 else round((plugin_success_count / plugin_attempt_count) * 100)
        dashboard_metrics = {
            "inbound_today": db.scalar(inbound_today_stmt) or 0,
            "inbound_24h": ops_snapshot["inbound_24h"],
            "outbound_24h": db.scalar(outbound_24h_stmt) or 0,
            "active_bots_24h": db.scalar(active_bot_stmt) or 0,
            "active_users_24h": db.scalar(active_user_stmt) or 0,
            "active_groups_24h": db.scalar(active_group_stmt) or 0,
            "plugin_triggers_24h": db.scalar(plugin_trigger_stmt) or 0,
            "plugin_success_rate_24h": plugin_success_rate,
            "abnormal_channels": ops_snapshot["abnormal_channels"],
            "open_alerts": total_open_alerts,
        }
        trend_series = build_dashboard_trend_series(db, hours=24)
        rankings = build_dashboard_rankings(db)
        bot_rows = []
        for bot in db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all():
            bot_rows.append(
                {
                    "bot": bot,
                    "channels": db.scalar(select(func.count(Channel.id)).where(Channel.bot_id == bot.id, Channel.deleted_at.is_(None))) or 0,
                    "plugins": db.scalar(select(func.count(BotPlugin.id)).where(BotPlugin.bot_id == bot.id)) or 0,
                    "conversations": db.scalar(select(func.count(Conversation.id)).where(Conversation.bot_id == bot.id)) or 0,
                    "messages": db.scalar(
                        select(func.count(Message.id))
                        .join(Conversation, Message.conversation_id == Conversation.id)
                        .where(Conversation.bot_id == bot.id)
                    )
                    or 0,
                }
            )
        return render(
            request,
            "dashboard.html",
            page_title="仪表盘",
            stats=stats,
            dashboard_metrics=dashboard_metrics,
            trend_series=trend_series,
            rankings=rankings,
            ops_snapshot=ops_snapshot,
            bot_rows=bot_rows,
        )
    finally:
        db.close()
