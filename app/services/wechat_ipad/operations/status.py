"""账号在线状态探测和账号资料刷新。"""

from __future__ import annotations

from typing import Any
import json

from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _parse_json, _resolve_wxid, _utcnow_iso
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad.login_profiles import _resolve_login_profile_key
from app.services.wechat_ipad.qr import (
    _build_qr_status_summary,
    _build_qrcode_expiry_view,
    _derive_login_state_from_qr,
    _derive_qrcode_status,
    _extract_qr_status_code,
    _normalize_qrcode_expires_at,
    _qr_login_succeeded,
)
from app.services.wechat_ipad.runtime import _persist_runtime
from app.services.wechat_ipad.transport import _safe_call, logger
from app.services.wechat_ipad.views.account import (
    _build_status_summary,
    _extract_user_profile_summary,
)


def refresh_wechat_ipad_status(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    probe_at = _utcnow_iso()
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    device_id = str(adapter_config.get("device_id") or "").strip()
    qr_uuid = str(runtime_info.get("login_qr_uuid") or "").strip()
    logger.info(
        "wechat ipad status refresh started",
        extra={
            "channel_id": channel.id,
            "channel_name": channel.name,
            "login_state": channel.login_state,
            "qrcode_status": channel.qrcode_status,
            "wxid": wxid,
            "device_id": device_id,
            "qr_uuid": qr_uuid,
            "probe_at": probe_at,
        },
    )

    qr_result: dict[str, Any] | None = None
    if _build_qrcode_expiry_view(runtime_info)["is_expired"] and getattr(channel, "qrcode_status", "") not in {"success", "expired"}:
        return _persist_runtime(
            db,
            channel,
            login_state="qrcode_expired",
            qrcode_status="expired",
            summary="二维码已过期，请刷新二维码后重新扫码。",
            error=None,
            extra=runtime_info,
        )
    if qr_uuid:
        qr_result = _safe_call(
            api_client.check_qr,
            stage="check_qr",
            trace_endpoint=IPAD_874_CONTRACT.endpoints.login_check_qr,
            channel=channel,
            uuid=qr_uuid,
        )
        runtime_info["last_check_qr_at"] = probe_at
        runtime_info["last_check_qr_response"] = qr_result.get("payload")
        runtime_info["login_qr_message"] = qr_result.get("message")
        qr_fields = WechatIpadApiClient.extract_qr_fields(qr_result.get("payload") or {})
        if qr_fields["qr_url"]:
            runtime_info["qrcode_url"] = qr_fields["qr_url"]
        if qr_fields["expires_at"] not in (None, ""):
            runtime_info["qrcode_expires_at"] = _normalize_qrcode_expires_at(qr_fields["expires_at"])
        if _qr_login_succeeded(qr_result):
            account_fields = WechatIpadApiClient.extract_account_fields(qr_result["payload"])
            runtime_info["active_wxid"] = account_fields["wxid"] or wxid
            runtime_info["active_nickname"] = account_fields["nickname"]
            runtime_info["active_alias"] = account_fields["alias"]
            runtime_info["active_bind_mobile"] = account_fields["bind_mobile"]
            if runtime_info.get("active_wxid"):
                adapter_config["wxid"] = runtime_info.get("active_wxid")
                channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False, sort_keys=True)
            wxid = _resolve_wxid(channel, runtime_info, adapter_config)
        logger.info(
            "wechat ipad check qr evaluated",
            extra={
                "channel_id": channel.id,
                "provider_message": qr_result.get("message") or "",
                "provider_code": qr_result.get("code"),
                "qr_status_code": _extract_qr_status_code(qr_result),
                "derived_login_state": _derive_login_state_from_qr(qr_result) or "",
                "derived_qrcode_status": _derive_qrcode_status(qr_result) or "",
                "qr_login_succeeded": _qr_login_succeeded(qr_result),
                "wxid_after_qr": wxid,
            },
        )

    if not channel.api_base or not wxid:
        summary = "缺少 API Base 或 wxid，无法继续同步登录态。"
        if qr_result and qr_result["ok"]:
            summary = _build_qr_status_summary(qr_result)
        return _persist_runtime(
            db,
            channel,
            login_state=_derive_login_state_from_qr(qr_result) if qr_result else "offline",
            qrcode_status=_derive_qrcode_status(qr_result) if qr_result else "idle",
            summary=summary,
            error="missing_api_base_or_wxid" if not wxid else None,
            error_context=qr_result.get("error_context") if qr_result and not qr_result["ok"] else None,
            extra=runtime_info,
        )

    heartbeat = _safe_call(
        api_client.heartbeat,
        stage="heartbeat",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_heartbeat,
        channel=channel,
        wxid=wxid,
    )
    heartbeat_long = _safe_call(
        api_client.heartbeat,
        stage="heartbeat_long",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_heartbeat_long,
        channel=channel,
        wxid=wxid,
        long=True,
    )
    user_profile = _safe_call(
        api_client.get_profile,
        stage="user_profile",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.user_get_profile,
        channel=channel,
        wxid=wxid,
        target_wxid=wxid,
    )

    login_ok = heartbeat["ok"]
    profile_data = user_profile["data"] if isinstance(user_profile.get("data"), dict) else {}
    profile_fields = WechatIpadApiClient.extract_account_fields(user_profile.get("payload") or {})
    selected_profile = _resolve_login_profile_key(
        runtime_info.get("selected_login_profile") or adapter_config.get("selected_login_profile")
    )
    adapter_config.pop("login_profiles", None)
    adapter_config["selected_login_profile"] = selected_profile
    runtime_info.update(
        {
            "active_wxid": wxid,
            "active_nickname": runtime_info.get("active_nickname") or profile_fields.get("nickname") or profile_data.get("NickName") or profile_data.get("nickname"),
            "active_alias": runtime_info.get("active_alias") or profile_fields.get("alias") or profile_data.get("Alias") or profile_data.get("alias"),
            "active_bind_mobile": runtime_info.get("active_bind_mobile") or profile_fields.get("bind_mobile") or profile_data.get("BindMobile") or profile_data.get("bind_mobile"),
            "heartbeat_status": "ok" if login_ok else "failed",
            "heartbeat_long_status": "ok" if heartbeat_long["ok"] else "failed",
            "reconnect_status": "ready" if login_ok else "required",
            "last_success_login_profile": (
                runtime_info.get("selected_login_profile") or adapter_config.get("selected_login_profile") or "ipad"
            ) if login_ok else runtime_info.get("last_success_login_profile"),
            "last_heartbeat_long_at": probe_at,
            "last_user_profile_at": probe_at,
            "last_heartbeat_response": heartbeat["payload"],
            "last_heartbeat_long_response": heartbeat_long["payload"],
            "last_user_profile_response": user_profile["payload"],
            "user_profile_summary": _extract_user_profile_summary(profile_data, profile_fields),
        }
    )
    if wxid:
        adapter_config["wxid"] = wxid
        channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False, sort_keys=True)
    summary = _build_status_summary(qr_result, heartbeat, user_profile)
    logger.info(
        "wechat ipad status refresh completed",
        extra={
            "channel_id": channel.id,
            "login_ok": login_ok,
            "wxid": wxid,
            "device_id": str(adapter_config.get("device_id") or ""),
            "heartbeat_ok": heartbeat["ok"],
            "heartbeat_long_ok": heartbeat_long["ok"],
            "user_profile_ok": user_profile["ok"],
            "summary": summary,
        },
    )
    state = _persist_runtime(
        db,
        channel,
        login_state="online" if login_ok else _derive_login_state_from_qr(qr_result) or "offline",
        qrcode_status="success" if login_ok else _derive_qrcode_status(qr_result) or getattr(channel, "qrcode_status", "idle"),
        summary=summary,
        heartbeat_at=now_shanghai(),
        login_at=now_shanghai() if login_ok else None,
        extra=runtime_info,
        error=None if login_ok else heartbeat["error"],
        error_context=None if login_ok else heartbeat.get("error_context"),
    )
    channel.status = "online" if login_ok else "degraded"
    db.commit()
    return state
