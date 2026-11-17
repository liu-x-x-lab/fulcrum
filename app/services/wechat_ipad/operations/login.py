"""扫码取码、轮询、退出和重连操作，使用调用方的单次代理会话。"""

from __future__ import annotations

from typing import Any
import json

from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _parse_json, _resolve_wxid, _utcnow_iso
from app.services.wechat_ipad.constants import WECHAT_IPAD_LOGIN_PROFILE_LABELS
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad.login_profiles import (
    _apply_login_profile_selection,
    _login_init_endpoint_for_profile,
)
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
from app.services.wechat_ipad.transport import (
    _compact_wechat_ipad_runtime_info,
    _safe_call,
    _summarize_provider_payload,
    logger,
)


def start_wechat_ipad_login(
    db: Session,
    channel: Channel,
    *,
    login_profile: str | None = None,
    proxy: dict[str, str] | None = None,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    """使用调用方已经解析好的单次登录代理获取二维码，不读取渠道长期代理配置。"""

    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    login_profile_key, device_name, device_id = _apply_login_profile_selection(
        channel,
        adapter_config,
        runtime_info,
        requested_profile=login_profile,
    )
    runtime_info = _compact_wechat_ipad_runtime_info(runtime_info)
    login_init_endpoint = _login_init_endpoint_for_profile(login_profile_key)
    channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False, sort_keys=True)
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    db.commit()
    db.refresh(channel)
    logger.info(
        "wechat ipad login init started",
        extra={
            "channel_id": channel.id,
            "channel_name": channel.name,
            "api_base": channel.api_base or "",
            "login_profile": login_profile_key,
            "device_name": device_name,
            "device_id": device_id,
            "login_init_endpoint": login_init_endpoint,
            "server_url": str(adapter_config.get("server_url") or ""),
        },
    )
    if not channel.api_base:
        return _persist_runtime(
            db,
            channel,
            login_state="error",
            qrcode_status="unavailable",
            summary="缺少 API Base，无法初始化登录。",
            error="missing_api_base",
        )

    response = _safe_call(
        api_client.get_qr_code,
        stage="login_init",
        trace_endpoint=login_init_endpoint,
        channel=channel,
        device_name=device_name,
        device_id=device_id,
        proxy=proxy,
        login_profile=login_profile_key,
    )
    if not response["ok"]:
        logger.warning(
            "wechat ipad login init failed",
            extra={
                "channel_id": channel.id,
                "login_profile": login_profile_key,
                "device_name": device_name,
                "device_id": device_id,
                "login_init_endpoint": login_init_endpoint,
                "provider_message": response.get("message") or "",
                "provider_code": response.get("code"),
                "error": response["error"] or "",
            },
        )
        return _persist_runtime(
            db,
            channel,
            login_state="login_init_failed",
            qrcode_status="failed",
            summary="GetQR 调用失败，未拿到二维码。",
            error=response["error"],
            error_context=response.get("error_context"),
            extra={
                "reconnect_status": "idle",
                "last_login_init_response": _summarize_provider_payload(response.get("payload") or {}),
                "last_login_init_endpoint": login_init_endpoint,
                "selected_login_profile": login_profile_key,
                "last_login_profile": login_profile_key,
            },
        )

    payload = response["payload"]
    qr_fields = WechatIpadApiClient.extract_qr_fields(payload)
    provider_summary = _summarize_provider_payload(payload)
    if not qr_fields["qr_url"]:
        return _persist_runtime(
            db,
            channel,
            login_state="login_init_failed",
            qrcode_status="failed",
            summary="协议服务未返回可展示的二维码链接。",
            error="qrcode_url_missing",
            extra={
                "last_login_init_response": provider_summary,
                "last_login_init_endpoint": login_init_endpoint,
                "selected_login_profile": login_profile_key,
                "last_login_profile": login_profile_key,
            },
        )
    previous_refresh_count = int(_parse_json(getattr(channel, "runtime_info_json", None)).get("qrcode_refresh_count") or 0)
    runtime_info = {
        "login_qr_uuid": qr_fields["uuid"],
        "qrcode_url": qr_fields["qr_url"],
        "qrcode_expires_at": _normalize_qrcode_expires_at(qr_fields["expires_at"]),
        "qrcode_refresh_count": previous_refresh_count + 1,
        "login_qr_message": response["message"] or "waiting_scan",
        "last_qrcode_at": _utcnow_iso(),
        "last_login_init_response": provider_summary,
        "last_login_init_endpoint": login_init_endpoint,
        "last_login_init_request": {"DeviceName": device_name, "DeviceID": device_id},
        "selected_login_profile": login_profile_key,
        "last_login_profile": login_profile_key,
    }
    logger.info(
        "wechat ipad login init completed",
        extra={
            "channel_id": channel.id,
            "login_profile": login_profile_key,
            "device_name": device_name,
            "device_id": device_id,
            "login_init_endpoint": login_init_endpoint,
            "qr_uuid": qr_fields["uuid"] or "",
            "has_qr_url": bool(qr_fields["qr_url"]),
            "expires_at": str(runtime_info.get("qrcode_expires_at") or ""),
            "provider_message": response.get("message") or "",
        },
    )
    return _persist_runtime(
        db,
        channel,
        login_state="qrcode_ready",
        qrcode_status="waiting_scan",
        summary="已获取登录二维码。",
        extra=runtime_info,
        error=None,
    )


def refresh_wechat_ipad_qrcode(
    db: Session,
    channel: Channel,
    *,
    login_profile: str | None = None,
    proxy: dict[str, str] | None = None,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    """刷新二维码时继续使用当前登录会话已经锁定的代理。"""

    overview = start_wechat_ipad_login(
        db,
        channel,
        login_profile=login_profile,
        proxy=proxy,
        client=client,
    )
    return _persist_runtime(
        db,
        channel,
        login_state=overview.get("login_state") or "qrcode_ready",
        qrcode_status=overview.get("qrcode_status") or "waiting_scan",
        summary="已刷新二维码。",
        error=None,
    )


def logout_wechat_ipad(
    db: Session,
    channel: Channel,
    *,
    login_profile: str | None = None,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    selected_profile, _, _ = _apply_login_profile_selection(
        channel,
        adapter_config,
        runtime_info,
        requested_profile=login_profile,
    )
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False, sort_keys=True)
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    db.commit()
    db.refresh(channel)
    logout_result = None
    if wxid:
        logout_result = _safe_call(
            api_client.log_out,
            stage="logout",
            trace_endpoint=IPAD_874_CONTRACT.endpoints.login_logout,
            channel=channel,
            wxid=wxid,
        )
    extra = {
        "selected_login_profile": selected_profile,
        "last_logout_at": _utcnow_iso(),
        "last_logout_response": (logout_result or {}).get("payload") if isinstance(logout_result, dict) else {},
        "last_logout_profile": selected_profile,
        "active_wxid": "",
        "active_nickname": "",
        "active_alias": "",
        "active_bind_mobile": "",
        "login_qr_uuid": "",
        "qrcode_url": "",
        "qrcode_expires_at": "",
        "heartbeat_status": "unknown",
        "heartbeat_long_status": "unknown",
        "reconnect_status": "idle",
        "sync_key": "",
    }
    summary = f"已退出当前登录，可切换到 {WECHAT_IPAD_LOGIN_PROFILE_LABELS.get(selected_profile, selected_profile)} 重新获取二维码。"
    error = None
    error_context = None
    if logout_result and not logout_result.get("ok"):
        summary = f"退出登录接口异常：{logout_result.get('error') or logout_result.get('message') or 'provider_error'}；本地状态已切回待登录。"
        error = logout_result.get("error")
        error_context = logout_result.get("error_context")
    return _persist_runtime(
        db,
        channel,
        login_state="idle",
        qrcode_status="idle",
        summary=summary,
        error=error,
        error_context=error_context,
        extra=extra,
    )


def poll_wechat_ipad_login_status(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    probe_at = _utcnow_iso()
    qr_uuid = str(runtime_info.get("login_qr_uuid") or "").strip()
    logger.info(
        "wechat ipad login poll started",
        extra={
            "channel_id": channel.id,
            "channel_name": channel.name,
            "login_state": channel.login_state,
            "qrcode_status": channel.qrcode_status,
            "qr_uuid": qr_uuid,
            "probe_at": probe_at,
        },
    )

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

    if not qr_uuid:
        return _persist_runtime(
            db,
            channel,
            login_state=getattr(channel, "login_state", "idle") or "idle",
            qrcode_status=getattr(channel, "qrcode_status", "idle") or "idle",
            summary="当前没有可轮询的二维码，请先获取二维码。",
            error=None,
            extra=runtime_info,
        )

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

    login_state = _derive_login_state_from_qr(qr_result) or getattr(channel, "login_state", "offline") or "offline"
    qrcode_status = _derive_qrcode_status(qr_result) or getattr(channel, "qrcode_status", "idle") or "idle"
    logger.info(
        "wechat ipad login poll evaluated",
        extra={
            "channel_id": channel.id,
            "provider_message": qr_result.get("message") or "",
            "provider_code": qr_result.get("code"),
            "qr_status_code": _extract_qr_status_code(qr_result),
            "derived_login_state": login_state,
            "derived_qrcode_status": qrcode_status,
            "qr_login_succeeded": _qr_login_succeeded(qr_result),
        },
    )
    if _qr_login_succeeded(qr_result):
        account_fields = WechatIpadApiClient.extract_account_fields(qr_result.get("payload") or {})
        if account_fields["wxid"]:
            runtime_info["active_wxid"] = account_fields["wxid"]
            adapter_config["wxid"] = account_fields["wxid"]
        if account_fields["nickname"]:
            runtime_info["active_nickname"] = account_fields["nickname"]
        if account_fields["alias"]:
            runtime_info["active_alias"] = account_fields["alias"]
        if account_fields["bind_mobile"]:
            runtime_info["active_bind_mobile"] = account_fields["bind_mobile"]
        channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False, sort_keys=True)
        runtime_info["last_success_login_profile"] = (
            runtime_info.get("selected_login_profile") or adapter_config.get("selected_login_profile") or "ipad"
        )
        channel.status = "online"
        logger.info(
            "wechat ipad login poll marked success",
            extra={
                "channel_id": channel.id,
                "provider_message": qr_result.get("message") or "",
                "provider_code": qr_result.get("code"),
                "qr_status_code": _extract_qr_status_code(qr_result),
                "active_wxid": runtime_info.get("active_wxid") or "",
                "active_nickname": runtime_info.get("active_nickname") or "",
            },
        )
        return _persist_runtime(
            db,
            channel,
            login_state="online",
            qrcode_status="success",
            summary=_build_qr_status_summary(qr_result),
            error=None,
            login_at=now_shanghai(),
            extra=runtime_info,
        )

    return _persist_runtime(
        db,
        channel,
        login_state=login_state,
        qrcode_status=qrcode_status,
        summary=_build_qr_status_summary(qr_result),
        error=None if qr_result["ok"] else qr_result["error"],
        error_context=None if qr_result["ok"] else qr_result.get("error_context"),
        extra=runtime_info,
    )


def reconnect_wechat_ipad(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    if not wxid:
        return _persist_runtime(
            db,
            channel,
            login_state="reconnect_blocked",
            summary="缺少 wxid，无法调用 Awaken/TwiceAutoAuth。",
            error="missing_wxid",
            extra={"reconnect_status": "blocked"},
        )

    awaken = _safe_call(
        api_client.awaken_login,
        stage="awaken",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_awaken,
        channel=channel,
        wxid=wxid,
    )
    extra = {
        "last_reconnect_response": awaken["payload"],
        "reconnect_method": "Awaken",
    }
    if not awaken["ok"]:
        return _persist_runtime(
            db,
            channel,
            login_state="reconnect_failed",
            summary="Awaken 调用失败。",
            error=awaken["error"],
            error_context=awaken.get("error_context"),
            extra={**extra, "reconnect_status": "failed"},
        )

    awakened_uuid = (
        awaken["data"].get("QrCodeResponse", {}).get("Uuid")
        if isinstance(awaken["data"].get("QrCodeResponse"), dict)
        else None
    )
    if awakened_uuid:
        extra["login_qr_uuid"] = awakened_uuid
        extra["qrcode_status_after_reconnect"] = "waiting_scan"
        extra["reconnect_status"] = "awakened"
        extra["qrcode_url"] = f"http://weixin.qq.com/x/{awakened_uuid}"
    twice_auth = _safe_call(
        api_client.twice_auto_auth,
        stage="twice_auto_auth",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_twice_auto_auth,
        channel=channel,
        wxid=wxid,
    )
    extra["last_twice_auto_auth_response"] = twice_auth["payload"]
    extra["reconnect_status"] = "auto_auth_ok" if twice_auth["ok"] else extra.get("reconnect_status", "awakened")
    summary = "已调用 Awaken/TwiceAutoAuth。"
    if awakened_uuid:
        summary = "已调用 Awaken 获取新的二维码，并尝试 TwiceAutoAuth。"
    return _persist_runtime(
        db,
        channel,
        login_state="reconnecting",
        qrcode_status="waiting_scan" if awakened_uuid else getattr(channel, "qrcode_status", "idle"),
        summary=summary,
        error=None if twice_auth["ok"] or awakened_uuid else twice_auth["error"],
        error_context=None if twice_auth["ok"] or awakened_uuid else twice_auth.get("error_context"),
        extra=extra,
    )
