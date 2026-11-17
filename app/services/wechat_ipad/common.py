"""协议值、时间和短文本的纯解析函数，不读写数据库或访问网络。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import json

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel
from app.services.wechat_ipad.constants import RECENT_CALL_FRESH_SECONDS, RECENT_CALL_STALE_SECONDS


CST = timezone(timedelta(hours=8), name="CST")


def _format_json_preview(value: Any) -> str:
    if value in (None, "", [], {}):
        return "{}"
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        return str(value)


def _map_recent_call_state_tone(state: str) -> str:
    if state == "healthy":
        return "success"
    if state in {"pending", "empty"}:
        return "muted"
    return "outline"


def _join_probe_labels(labels: list[str]) -> str:
    return "、".join(labels) if labels else "-"


def _build_recent_call_freshness(called_at: Any, *, now: datetime | None = None) -> dict[str, str]:
    parsed_called_at = _parse_datetime_like(called_at)
    if not parsed_called_at:
        return {
            "key": "no_sample",
            "label": "尚无时间样本",
            "tone": "muted",
            "hint": "尚无时间样本，当前无法判断最近结果是否过旧。",
        }

    reference_time = now or now_shanghai()
    if reference_time.tzinfo is None:
        reference_time = ensure_shanghai_datetime(reference_time)
    age_seconds = max(int((reference_time - parsed_called_at).total_seconds()), 0)

    if age_seconds <= RECENT_CALL_FRESH_SECONDS:
        return {
            "key": "fresh",
            "label": "新鲜",
            "tone": "success",
            "hint": "结果较新鲜，可直接按当前样本继续排查。",
        }
    if age_seconds <= RECENT_CALL_STALE_SECONDS:
        return {
            "key": "stale",
            "label": "偏旧",
            "tone": "outline",
            "hint": "结果已偏旧，建议结合最近动作确认探针是否还在持续刷新。",
        }
    return {
        "key": "expired",
        "label": "过旧",
        "tone": "outline",
        "hint": "结果已过旧，建议优先重新触发探针，避免继续参考陈旧样本。",
    }


def _resolve_wxid(channel: Channel, runtime_info: dict[str, Any], adapter_config: dict[str, Any]) -> str:
    return str(runtime_info.get("active_wxid") or adapter_config.get("wxid") or "").strip()


def _parse_json(raw: str | None) -> dict[str, Any]:
    normalized = (raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _utcnow_iso() -> str:
    return now_shanghai().isoformat()


def _parse_datetime_like(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_shanghai_datetime(value)
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return ensure_shanghai_datetime(parsed)


def _format_beijing_time(value: Any) -> str:
    parsed = _parse_datetime_like(value)
    if not parsed:
        return "-"
    return parsed.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
