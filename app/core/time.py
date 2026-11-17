from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


def get_system_timezone():
    # 业务时间以服务器系统时区为准；生产机需要保持 Asia/Shanghai。
    # 这里不写死 UTC，避免云端页面和任务时间显示成 8 小时前。
    return datetime.now().astimezone().tzinfo or timezone(timedelta(hours=8), name="Asia/Shanghai")


SHANGHAI_TZ = get_system_timezone()


def now_shanghai() -> datetime:
    # 保留历史函数名，实际返回服务器系统本地时间；当前部署机系统时区为 Asia/Shanghai。
    return datetime.now(get_system_timezone())


def ensure_shanghai_datetime(value: datetime | None) -> datetime:
    if value is None:
        return now_shanghai()
    if value.tzinfo is None:
        # 历史数据不做 UTC 兼容；无时区时间一律视为服务器本地时间。
        return value.replace(tzinfo=get_system_timezone())
    return value.astimezone(get_system_timezone())


def hour_bucket_shanghai(value: datetime | None = None) -> datetime:
    current = ensure_shanghai_datetime(value)
    return current.replace(minute=0, second=0, microsecond=0)


def format_shanghai_time(value) -> str:
    if not value:
        return "-"
    if isinstance(value, datetime):
        parsed = ensure_shanghai_datetime(value)
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=SHANGHAI_TZ)
    else:
        text = str(value).strip()
        if not text:
            return "-"
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return text
        parsed = ensure_shanghai_datetime(parsed)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def format_conversation_time(value: datetime | None, *, now: datetime | None = None) -> str:
    """会话列表按业务自然日缩短时间，让群名保留足够空间。"""
    if value is None:
        return "-"
    # 数据库的无时区值仍按现有业务时区解释；昨天按日期计算，不能用过去 24 小时判断，
    # 否则午夜后昨天深夜的消息会被误显示为今天。显式 now 让同批摘要跨午夜时保持一致。
    timestamp = ensure_shanghai_datetime(value)
    days_ago = (ensure_shanghai_datetime(now).date() - timestamp.date()).days
    if days_ago == 0:
        return timestamp.strftime("%H:%M")
    if days_ago == 1:
        return timestamp.strftime("昨天 %H:%M")
    return timestamp.strftime("%m/%d")
