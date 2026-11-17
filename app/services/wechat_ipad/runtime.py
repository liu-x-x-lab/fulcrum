"""渠道运行状态和收发事件的持久化边界。

沿用原有提交与刷新语义，返回统一概览；只读视图不得反向调用本模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import json

from sqlalchemy.orm import Session

from app.models.entities import Channel
from app.services.wechat_ipad.common import _parse_json, _utcnow_iso
from app.services.wechat_ipad.diagnostics.errors import _normalize_error_context
from app.services.wechat_ipad.transport import _compact_wechat_ipad_runtime_info, logger
from app.services.wechat_ipad.views.overview import build_wechat_ipad_overview


def record_wechat_ipad_receive_event(
    db: Session,
    channel: Channel,
    *,
    event_key: str | None,
    mapped: bool,
    deduplicated: bool,
    source: str,
) -> dict[str, Any]:
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    runtime_info["last_receive_at"] = _utcnow_iso()
    runtime_info["last_receive_event_key"] = event_key or None
    runtime_info["last_receive_summary"] = (
        f"收到 {source} 事件，event_key={event_key or '-'}，"
        f"{'命中去重' if deduplicated else '已入处理链路'}。"
    )
    runtime_info["last_inbound_protocol_summary"] = {
        "source": source,
        "event_key": event_key,
        "mapped": mapped,
        "deduplicated": deduplicated,
    }
    runtime_info["receive_total"] = int(runtime_info.get("receive_total") or 0) + 1
    if deduplicated:
        runtime_info["dedupe_total"] = int(runtime_info.get("dedupe_total") or 0) + 1
        runtime_info["last_dedupe_event_key"] = event_key or None
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    db.commit()
    db.refresh(channel)
    return build_wechat_ipad_overview(channel)


def record_wechat_ipad_send_event(
    db: Session,
    channel: Channel,
    *,
    adapter_result: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    receiver_id = payload.get("receiver_id") or payload.get("external_id") or "-"
    runtime_info["last_send_at"] = _utcnow_iso()
    runtime_info["last_send_summary"] = (
        f"发送 {payload.get('message_type') or 'text'} 到 {receiver_id}，"
        f"回执 {adapter_result.get('external_status') or adapter_result.get('status') or 'unknown'}。"
    )
    runtime_info["last_send_result"] = {
        "ok": bool(adapter_result.get("ok")),
        "status": adapter_result.get("status"),
        "transport_status": adapter_result.get("transport_status"),
        "endpoint": adapter_result.get("endpoint"),
        "external_message_id": adapter_result.get("external_message_id"),
        "client_msg_id": adapter_result.get("client_msg_id"),
        "create_time": adapter_result.get("create_time"),
        "new_msg_id": adapter_result.get("new_msg_id"),
    }
    runtime_info["last_outbound_protocol_summary"] = {
        "direction": adapter_result.get("direction"),
        "session_id": adapter_result.get("session_id"),
        "receiver_id": receiver_id,
        "endpoint": adapter_result.get("endpoint"),
        "external_message_id": adapter_result.get("external_message_id"),
        "client_msg_id": adapter_result.get("client_msg_id"),
        "new_msg_id": adapter_result.get("new_msg_id"),
    }
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    db.commit()
    db.refresh(channel)
    return build_wechat_ipad_overview(channel)


def _persist_runtime(
    db: Session,
    channel: Channel,
    *,
    login_state: str | None = None,
    qrcode_status: str | None = None,
    summary: str | None = None,
    error: str | None = None,
    error_context: dict[str, Any] | None = None,
    heartbeat_at: datetime | None = None,
    login_at: datetime | None = None,
    sync_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    if summary is not None:
        runtime_info["last_status_summary"] = summary
    if sync_at is not None:
        runtime_info["last_sync_summary"] = summary
    if error is not None:
        runtime_info["last_error"] = error
        runtime_info["last_error_context"] = _normalize_error_context(error_context) if error_context else {}
    elif "last_error" in runtime_info:
        runtime_info.pop("last_error", None)
        runtime_info.pop("last_error_context", None)
    elif "last_error_context" in runtime_info:
        runtime_info.pop("last_error_context", None)
    if not runtime_info.get("last_error_context"):
        runtime_info.pop("last_error_context", None)
    if extra:
        runtime_info.update(extra)
    runtime_info = _compact_wechat_ipad_runtime_info(runtime_info)
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    if login_state is not None:
        channel.login_state = login_state
    if qrcode_status is not None:
        channel.qrcode_status = qrcode_status
    if heartbeat_at is not None:
        channel.last_heartbeat_at = heartbeat_at
    if login_at is not None:
        channel.last_login_at = login_at
    if sync_at is not None:
        channel.last_message_sync_at = sync_at
    db.commit()
    db.refresh(channel)
    logger.info(
        "wechat ipad runtime persisted",
        extra={
            "channel_id": channel.id,
            "login_state": channel.login_state,
            "qrcode_status": channel.qrcode_status,
            "status": channel.status,
            "last_login_at": str(channel.last_login_at or ""),
            "last_heartbeat_at": str(channel.last_heartbeat_at or ""),
            "last_message_sync_at": str(channel.last_message_sync_at or ""),
            "summary": summary or "",
            "error": error or "",
            "runtime_keys": sorted(runtime_info.keys()),
        },
    )
    return build_wechat_ipad_overview(channel)
