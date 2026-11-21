"""微桥设备上报、实时摘要及趋势。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, RawWebhookEvent, WebridgeDeviceSample

# 手机 state 上报字段 -> 页面标签。只展示微桥已经给出的字段，未识别字段统一进“其他上报”。
STATE_LABELS = (
    ("is_logged_in", "微信登录", "bool"),
    ("auth_status", "授权状态", "text"),
    ("auth_expires_at", "授权有效期", "text"),
    ("send_enabled", "发送开关", "bool"),
    ("receive_enabled", "接收开关", "bool"),
    ("forward_enabled", "转发开关", "bool"),
    ("plugin_enabled", "插件启用", "bool"),
    ("event_uploader_running", "事件上传线程", "bool"),
    ("log_capture_enabled", "日志采集", "bool"),
    ("cached_message_count", "缓存消息数", "text"),
    ("cached_room_snapshot_count", "缓存群快照", "text"),
    ("cached_red_packet_count", "缓存红包数", "text"),
    ("pending_event_queue_count", "待上传事件", "text"),
    ("oldest_pending_event_age_ms", "最久待传事件(毫秒)", "text"),
    ("last_hook_selector", "最近 Hook", "text"),
)


# 上报延迟超过该秒数视为“明显滞后”，摘要标签据此提示。
LAG_WARN_SECONDS = 30


def _event_lag_seconds(payload_json: str | None) -> int | None:
    """单条微桥消息事件的上报延迟：手机抓到消息的时刻减去微信消息产生时刻。

    只统计消息事件；缺少时间戳、或时钟偏差导致为负的事件返回 None，不参与统计。
    延迟出现在手机抓取之前，说明是手机/微信侧滞后，而不是服务端处理慢。
    """

    try:
        payload = json.loads(payload_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("event_name") not in (None, "message"):
        return None
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    created = body.get("create_time")
    captured = payload.get("captured_at_ms")
    if not isinstance(created, (int, float)) or not isinstance(captured, (int, float)) or created <= 0:
        return None
    lag = round(captured / 1000 - created)
    return lag if lag >= 0 else None


def _percentile(values: list[int], ratio: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * ratio + 0.5) - 1))]


def build_webridge_lag(db: Session, channel: Channel, *, hours: int = 24) -> dict[str, Any]:
    """近 hours 小时的消息上报延迟：最近 1 小时 p90 / 最大值，以及按小时的 p90 趋势。

    直接读取已落库的原始事件，不额外建表；每小时取 p90 是为了让偶发批量滞后可见，
    又不被个别极端值淹没。
    """

    now = now_shanghai()
    rows = db.execute(
        select(RawWebhookEvent.created_at, RawWebhookEvent.payload_json).where(
            RawWebhookEvent.channel_id == int(channel.id),
            RawWebhookEvent.source == "wechat_webridge",
            RawWebhookEvent.created_at >= now - timedelta(hours=hours),
        )
    ).all()
    by_hour: dict[Any, list[int]] = {}
    recent: list[int] = []
    for created_at, payload_json in rows:
        lag = _event_lag_seconds(payload_json)
        if lag is None:
            continue
        # 数据库取回的时间可能不带时区，统一转成北京时间后再比较和分桶。
        created_at = ensure_shanghai_datetime(created_at)
        by_hour.setdefault(created_at.replace(minute=0, second=0, microsecond=0), []).append(lag)
        if created_at >= now - timedelta(hours=1):
            recent.append(lag)
    items = [{"label": hour.strftime("%H:%M"), "value": _percentile(values, 0.9)} for hour, values in sorted(by_hour.items())]
    peak = max((item["value"] for item in items), default=0)
    count = len(items)
    points = " ".join(
        f"{0 if count <= 1 else round(index / (count - 1) * 100, 2)},{100 if peak <= 0 else round(100 - item['value'] / peak * 100, 2)}"
        for index, item in enumerate(items)
    )
    return {
        "recent_p90": _percentile(recent, 0.9) if recent else None,
        "recent_max": max(recent) if recent else None,
        "recent_count": len(recent),
        "chart": {"max_value": peak, "points": points, "items": items},
        "warn": bool(recent) and _percentile(recent, 0.9) >= LAG_WARN_SECONDS,
    }


def _chart(samples: list[WebridgeDeviceSample], field: str, label_format: str = "%H:%M") -> dict[str, Any]:
    """把采样序列转换为 _dashboard_chart.html 的 trend_chart 宏需要的结构；NULL 采样跳过。"""

    items = [{"label": item.bucket_start.strftime(label_format), "value": int(getattr(item, field))} for item in samples if getattr(item, field) is not None]
    values = [item["value"] for item in items]
    peak = max(values) if values else 0
    points = []
    for index, value in enumerate(values):
        x = 0 if len(values) <= 1 else round(index / (len(values) - 1) * 100, 2)
        y = 100 if peak <= 0 else round(100 - value / peak * 100, 2)
        points.append(f"{x},{y}")
    return {"max_value": peak, "points": " ".join(points), "items": items}


_BATTERY_STATE = {"unplugged": "未充电", "charging": "充电中", "full": "已充满", "unknown": "未知"}
_THERMAL_STATE = {"nominal": "正常", "fair": "轻微发热", "serious": "较热", "critical": "过热", "unknown": "未知"}
_APP_STATE = {"active": "前台", "inactive": "非活跃", "background": "后台", "unknown": "未知"}
_NETWORK_TYPE = {"wifi": "Wi-Fi", "cellular": "蜂窝网络", "none": "无网络", "unknown": "未知"}


def _hardware_items(dev: dict[str, Any]) -> list[dict[str, str]]:
    """把 device_status 快照转成页面条目；读不到的值（null、system_memory 采集失败）直接省略。"""

    def obj(key: str) -> dict[str, Any]:
        return dev.get(key) if isinstance(dev.get(key), dict) else {}

    items: list[dict[str, str]] = []
    device, battery, power, thermal, network, app = obj("device"), obj("battery"), obj("power"), obj("thermal"), obj("network"), obj("application")
    system, process = obj("system_memory"), obj("process_memory")
    if device.get("device_model"):
        items.append({"label": "机型", "value": f"{device['device_model']} · iOS {device.get('ios_version') or '-'}"})
    if isinstance(battery.get("percent"), (int, float)):
        low = "，低电量模式" if power.get("low_power_mode") else ""
        items.append({"label": "电量", "value": f"{int(battery['percent'])}%（{_BATTERY_STATE.get(battery.get('state'), '未知')}{low}）"})
    if system.get("ok") and isinstance(system.get("used_estimate_percent"), (int, float)):
        items.append({"label": "系统内存", "value": f"已用约 {system['used_estimate_percent']:.0f}% · 可用约 {system.get('available_estimate_mb', 0):.0f} MB / 共 {system.get('physical_total_mb', 0):.0f} MB"})
    if isinstance(process.get("phys_footprint_mb"), (int, float)):
        items.append({"label": "插件进程内存", "value": f"{process['phys_footprint_mb']:.0f} MB（常驻 {process.get('resident_mb', 0):.0f} MB）"})
    if thermal.get("state"):
        items.append({"label": "温度状态", "value": _THERMAL_STATE.get(thermal["state"], str(thermal["state"]))})
    if network:
        net = _NETWORK_TYPE.get(network.get("network_type"), "未知")
        gen = f" {network['cellular_generation']}" if network.get("cellular_generation") not in (None, "", "unknown") else ""
        items.append({"label": "网络", "value": f"{net}{gen}" + ("" if network.get("connected") else "（不可达）")})
    if app.get("state"):
        items.append({"label": "微信前后台", "value": _APP_STATE.get(app["state"], str(app["state"]))})
    return items


def build_webridge_live_summary(db: Session, channel: Channel) -> dict[str, Any]:
    """顶部精简摘要的实时数据：在线、电量、系统内存、实时积压、数据新鲜度。

    页面首次渲染和每 10 秒的自动刷新共用此函数，保证两处口径一致。
    """

    from app.services.wechat_webridge.service import count_backlog

    try:
        runtime = json.loads(channel.runtime_info_json or "{}").get("webridge")
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        runtime = None
    runtime = runtime if isinstance(runtime, dict) else {}
    dev = runtime.get("device") if isinstance(runtime.get("device"), dict) else {}

    def num(group: str, key: str) -> int | None:
        value = (dev.get(group) or {}).get(key) if isinstance(dev.get(group), dict) else None
        return int(round(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    system_ok = isinstance(dev.get("system_memory"), dict) and dev["system_memory"].get("ok")
    pending, running = count_backlog(db, channel)
    lag = build_webridge_lag(db, channel, hours=1)
    return {
        "lag_p90": lag["recent_p90"],
        "lag_warn": lag["warn"],
        "online": bool(runtime.get("online")),
        "last_seen_at": runtime.get("last_seen_at"),
        "battery_percent": num("battery", "percent"),
        "battery_state": _BATTERY_STATE.get((dev.get("battery") or {}).get("state"), "") if isinstance(dev.get("battery"), dict) else "",
        "memory_used_percent": num("system_memory", "used_estimate_percent") if system_ok else None,
        "pending": pending,
        "running": running,
        "device_ts_ms": dev.get("ts_ms"),
    }


def build_webridge_device_panel(db: Session, channel: Channel, *, hours: int = 24) -> dict[str, Any]:
    """构建 iPhone 状态区：当前在线与上报字段 + 近 hours 小时的积压/缓存趋势。"""

    try:
        runtime = json.loads(channel.runtime_info_json or "{}").get("webridge")
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        runtime = None
    runtime = runtime if isinstance(runtime, dict) else {}
    state = runtime.get("state") if isinstance(runtime.get("state"), dict) else {}

    def _show(value: Any, kind: str) -> str:
        if kind == "bool":
            return "是" if value else "否"
        return str(value)

    dev = runtime.get("device") if isinstance(runtime.get("device"), dict) else {}
    known = {key for key, _, _ in STATE_LABELS}
    fields = [{"label": label, "value": _show(state[key], kind)} for key, label, kind in STATE_LABELS if key in state]
    extra = [{"label": key, "value": str(value)} for key, value in sorted(state.items()) if key not in known and not isinstance(value, (dict, list))]
    since = now_shanghai() - timedelta(hours=hours)
    samples = db.scalars(
        select(WebridgeDeviceSample).where(WebridgeDeviceSample.channel_id == int(channel.id), WebridgeDeviceSample.bucket_start >= since).order_by(WebridgeDeviceSample.bucket_start.asc())
    ).all()
    # 采样按分钟，24 小时最多 1440 点；每 10 分钟取一个点即可，曲线更平滑且页面更轻。
    samples = samples[::10] if len(samples) > 144 else samples
    capabilities = runtime.get("capabilities") if isinstance(runtime.get("capabilities"), list) else []
    return {
        "lag": build_webridge_lag(db, channel),
        "hardware": _hardware_items(dev),
        "battery_chart": _chart(samples, "battery_percent"),
        "memory_chart": _chart(samples, "memory_used_percent"),
        "online": bool(runtime.get("online")),
        "last_seen_at": runtime.get("last_seen_at"),
        "registered_at": runtime.get("registered_at"),
        "basic": [
            {"label": "运行版本", "value": runtime.get("runtime_version") or "-"},
            {"label": "构建标记", "value": runtime.get("build_marker") or "-"},
            {"label": "微信账号", "value": runtime.get("account_id") or "-"},
            {"label": "设备 ID", "value": runtime.get("device_id") or "-"},
            {"label": "会话 ID", "value": runtime.get("session_id") or "-"},
        ],
        "fields": fields,
        "extra": extra,
        "capability_count": len(capabilities),
        "capabilities": [str(item) for item in capabilities],
        "pending_chart": _chart(samples, "pending_commands"),
        "cache_chart": _chart(samples, "cached_message_count"),
        "event_chart": _chart(samples, "pending_event_queue_count"),
        "sample_count": len(samples),
    }
