"""渠道页使用的摘要和时间线，不发起协议操作。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Channel, HealthCheckRecord
from app.services.workwechat_cloud_poll import load_cloud_poll_config


def build_channel_chatroom_summary(list_view: dict[str, Any] | None) -> dict[str, Any] | None:
    if not list_view:
        return None
    items = list(list_view.get("items") or [])
    # 渠道详情页只需要一屏摘要；完整群列表在 /chatrooms 查看。
    recent_chatrooms = items[:8]
    return {
        "total": int(list_view.get("total") or len(items)),
        "active_chatroom_count": int(list_view.get("active_chatroom_count") or 0),
        "pending_chatroom_count": int(list_view.get("pending_chatroom_count") or 0),
        "member_snapshot_chatroom_count": int(list_view.get("member_snapshot_chatroom_count") or 0),
        "binding_total": int(list_view.get("binding_total") or 0),
        "binding_enabled_total": int(list_view.get("binding_enabled_total") or 0),
        "overridden_chatroom_count": int(list_view.get("overridden_chatroom_count") or 0),
        "sync_action": str(list_view.get("sync_action") or ""),
        "sync_button_label": str(list_view.get("sync_button_label") or "刷新群列表"),
        "intro_summary": str(list_view.get("intro_summary") or "基于本地群样本生成群管理摘要。"),
        "empty_state_message": str(list_view.get("empty_state_message") or "当前还没有观测到群消息，先同步群列表或等待群消息进入。"),
        "recent_chatrooms": recent_chatrooms,
    }


def build_workwechat_cloud_poll_config_map(channels: list[Channel]) -> dict[int, dict[str, Any]]:
    # 渠道列表页需要按每个企微渠道判断是否云端 WS/拉取模式；
    # 这里在服务端提前解析 JSON，避免模板里反复解析 adapter_config_json。
    result: dict[int, dict[str, Any]] = {}
    for channel in channels:
        if channel.channel_type == "workwechat":
            result[int(channel.id)] = load_cloud_poll_config(channel)
    return result


def build_channel_health_timeline(db: Session, channel_id: int) -> dict[str, object]:
    latest_checks = db.scalars(
        select(HealthCheckRecord)
        .where(HealthCheckRecord.target_type == "channel", HealthCheckRecord.target_id == channel_id)
        .order_by(HealthCheckRecord.id.desc())
        .limit(20)
    ).all()
    latest_success = next((item for item in latest_checks if item.status == "online"), None)
    latest_failure = next((item for item in latest_checks if item.status != "online"), None)
    return {
        "latest": latest_checks[0] if latest_checks else None,
        "latest_success_at": getattr(latest_success, "created_at", None),
        "latest_success_summary": getattr(latest_success, "summary", None),
        "latest_failure_at": getattr(latest_failure, "created_at", None),
        "latest_failure_summary": getattr(latest_failure, "summary", None),
    }
