"""心跳保活与失败后的二次认证，统一复用协议调用和运行状态持久化。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import (
    _parse_datetime_like,
    _parse_json,
    _resolve_wxid,
    _utcnow_iso,
)
from app.services.wechat_ipad.constants import (
    WECHAT_IPAD_HEARTBEAT_INTERVAL_SECONDS,
    WECHAT_IPAD_HEARTBEAT_LONG_INTERVAL_SECONDS,
    WECHAT_IPAD_TWICE_AUTO_AUTH_COOLDOWN_SECONDS,
)
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad.runtime import _persist_runtime
from app.services.wechat_ipad.transport import _safe_call, logger


def heartbeat_wechat_ipad_channel(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    now = now_shanghai()
    last_heartbeat_at = _parse_datetime_like(getattr(channel, "last_heartbeat_at", None))
    last_heartbeat_long_at = _parse_datetime_like(runtime_info.get("last_heartbeat_long_at"))
    heartbeat_due = force or last_heartbeat_at is None or (now - last_heartbeat_at).total_seconds() >= WECHAT_IPAD_HEARTBEAT_INTERVAL_SECONDS
    heartbeat_long_due = force or last_heartbeat_long_at is None or (now - last_heartbeat_long_at).total_seconds() >= WECHAT_IPAD_HEARTBEAT_LONG_INTERVAL_SECONDS
    if not wxid:
        logger.info(
            "wechat ipad scheduled heartbeat skipped",
            extra={"channel_id": channel.id, "reason": "missing_wxid", "login_state": channel.login_state},
        )
        return {
            "ok": False,
            "skipped": True,
            "reason": "missing_wxid",
        }
    if not heartbeat_due and not heartbeat_long_due:
        logger.info(
            "wechat ipad scheduled heartbeat skipped",
            extra={
                "channel_id": channel.id,
                "reason": "not_due",
                "wxid": wxid,
                "last_heartbeat_at": str(last_heartbeat_at or ""),
                "last_heartbeat_long_at": str(last_heartbeat_long_at or ""),
            },
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": "not_due",
        }

    logger.info(
        "wechat ipad scheduled heartbeat started",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "heartbeat_due": heartbeat_due,
            "heartbeat_long_due": heartbeat_long_due,
            "last_heartbeat_at": str(last_heartbeat_at or ""),
            "last_heartbeat_long_at": str(last_heartbeat_long_at or ""),
        },
    )

    heartbeat_result = None
    heartbeat_long_result = None
    if heartbeat_due:
        heartbeat_result = _safe_call(
            api_client.heartbeat,
            stage="heartbeat",
            trace_endpoint=IPAD_874_CONTRACT.endpoints.login_heartbeat,
            channel=channel,
            wxid=wxid,
        )
        runtime_info["last_heartbeat_response"] = heartbeat_result["payload"]
        runtime_info["heartbeat_status"] = "ok" if heartbeat_result["ok"] else "failed"

    if heartbeat_long_due:
        heartbeat_long_result = _safe_call(
            api_client.heartbeat,
            stage="heartbeat_long",
            trace_endpoint=IPAD_874_CONTRACT.endpoints.login_heartbeat_long,
            channel=channel,
            wxid=wxid,
            long=True,
        )
        runtime_info["last_heartbeat_long_response"] = heartbeat_long_result["payload"]
        runtime_info["heartbeat_long_status"] = "ok" if heartbeat_long_result["ok"] else "failed"
        runtime_info["last_heartbeat_long_at"] = _utcnow_iso()

    heartbeat_ok = True if heartbeat_result is None else bool(heartbeat_result["ok"])
    heartbeat_long_ok = True if heartbeat_long_result is None else bool(heartbeat_long_result["ok"])
    twice_auto_auth_result = None
    if wxid and (not heartbeat_ok or not heartbeat_long_ok):
        twice_auto_auth_result = _maybe_run_twice_auto_auth_after_heartbeat_failure(
            api_client,
            channel=channel,
            adapter_config=adapter_config,
            runtime_info=runtime_info,
            wxid=wxid,
            heartbeat_result=heartbeat_result,
            heartbeat_long_result=heartbeat_long_result,
            now=now,
        )
    runtime_info["reconnect_status"] = "ready" if heartbeat_ok else "required"
    error = None
    error_context = None
    login_state = getattr(channel, "login_state", "online") or "online"
    channel_status = "online"
    summary = None
    if not heartbeat_ok:
        error = heartbeat_result["error"] if heartbeat_result else "heartbeat_failed"
        error_context = heartbeat_result.get("error_context") if heartbeat_result else None
        login_state = "reconnecting" if twice_auto_auth_result and twice_auto_auth_result.get("ok") else "offline"
        channel_status = "degraded"
        summary = (
            "HeartBeat 失败，已自动触发 TwiceAutoAuth。"
            if twice_auto_auth_result and twice_auto_auth_result.get("ok")
            else f"HeartBeat 失败：{error}"
        )
    elif not heartbeat_long_ok:
        error = heartbeat_long_result["error"] if heartbeat_long_result else "heartbeat_long_failed"
        error_context = heartbeat_long_result.get("error_context") if heartbeat_long_result else None
        login_state = "online"
        channel_status = "degraded"
        summary = (
            "HeartBeatLong 失败，已自动触发 TwiceAutoAuth。"
            if twice_auto_auth_result and twice_auto_auth_result.get("ok")
            else f"HeartBeatLong 失败：{error}"
        )
    elif twice_auto_auth_result and twice_auto_auth_result.get("ok"):
        runtime_info["reconnect_status"] = "auto_auth_ok"
        summary = "自动 TwiceAutoAuth 已执行成功。"

    state = _persist_runtime(
        db,
        channel,
        login_state=login_state,
        qrcode_status=getattr(channel, "qrcode_status", "success") or "success",
        summary=summary,
        error=error,
        error_context=error_context,
        heartbeat_at=now if heartbeat_due else None,
        extra=runtime_info,
    )
    channel.status = channel_status
    db.commit()
    logger.info(
        "wechat ipad scheduled heartbeat completed",
        extra={
            "channel_id": channel.id,
            "heartbeat_due": heartbeat_due,
            "heartbeat_long_due": heartbeat_long_due,
            "heartbeat_ok": heartbeat_ok,
            "heartbeat_long_ok": heartbeat_long_ok,
            "twice_auto_auth_ok": bool(twice_auto_auth_result and twice_auto_auth_result.get("ok")),
            "channel_status": channel_status,
            "login_state": login_state,
        },
    )
    return {
        "ok": heartbeat_ok and heartbeat_long_ok,
        "skipped": False,
        "heartbeat_due": heartbeat_due,
        "heartbeat_long_due": heartbeat_long_due,
        "state": state,
    }


def _maybe_run_twice_auto_auth_after_heartbeat_failure(
    api_client: WechatIpadApiClient,
    *,
    channel: Channel,
    adapter_config: dict[str, Any],
    runtime_info: dict[str, Any],
    wxid: str,
    heartbeat_result: dict[str, Any] | None,
    heartbeat_long_result: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any] | None:
    last_attempt_at = _parse_datetime_like(runtime_info.get("last_twice_auto_auth_at"))
    if last_attempt_at and (now - last_attempt_at).total_seconds() < WECHAT_IPAD_TWICE_AUTO_AUTH_COOLDOWN_SECONDS:
        logger.info(
            "wechat ipad twice auto auth skipped by cooldown",
            extra={
                "channel_id": channel.id,
                "wxid": wxid,
                "last_twice_auto_auth_at": str(last_attempt_at),
            },
        )
        return None

    trigger_error = ""
    if heartbeat_result and not heartbeat_result.get("ok"):
        trigger_error = str(heartbeat_result.get("error") or heartbeat_result.get("message") or "").strip()
    elif heartbeat_long_result and not heartbeat_long_result.get("ok"):
        trigger_error = str(heartbeat_long_result.get("error") or heartbeat_long_result.get("message") or "").strip()

    logger.info(
        "wechat ipad twice auto auth started",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "trigger_error": trigger_error,
        },
    )
    result = _safe_call(
        api_client.twice_auto_auth,
        stage="twice_auto_auth",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_twice_auto_auth,
        channel=channel,
        wxid=wxid,
    )
    runtime_info["last_twice_auto_auth_at"] = _utcnow_iso()
    runtime_info["last_twice_auto_auth_response"] = result.get("payload")
    runtime_info["last_twice_auto_auth_error"] = result.get("error") if not result.get("ok") else ""
    runtime_info["reconnect_status"] = "auto_auth_ok" if result.get("ok") else "auto_auth_failed"
    logger.info(
        "wechat ipad twice auto auth completed",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "ok": bool(result.get("ok")),
            "provider_message": result.get("message") or "",
            "provider_code": result.get("code"),
        },
    )
    return result
