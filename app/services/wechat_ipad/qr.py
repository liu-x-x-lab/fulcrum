"""二维码状态推导、过期时间及图片地址解析，不发起取码请求。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _format_beijing_time, _parse_datetime_like, _parse_json


def _build_qr_status_summary(qr_result: dict[str, Any]) -> str:
    if not qr_result:
        return "尚未获取二维码状态。"
    if _qr_login_succeeded(qr_result):
        return "CheckQR 返回登陆成功。"
    if qr_result["ok"]:
        return f"CheckQR 返回 {qr_result['message'] or '待确认状态'}。"
    return f"CheckQR 调用失败：{qr_result['error']}"


def _derive_login_state_from_qr(qr_result: dict[str, Any] | None) -> str | None:
    if not qr_result:
        return None
    status_code = _extract_qr_status_code(qr_result)
    message = qr_result.get("message") or ""
    if status_code in {2, 3}:
        return "online"
    if status_code == 1:
        return "qrcode_scanned"
    if status_code in {-1, 4}:
        return "qrcode_expired"
    if qr_result["ok"] and message in {"登陆成功", "登录成功"}:
        return "online"
    if "扫码" in message:
        return "qrcode_scanned"
    if "确认" in message:
        return "qrcode_confirmed"
    if "过期" in message:
        return "qrcode_expired"
    return "qrcode_ready" if qr_result["ok"] else "offline"


def _derive_qrcode_status(qr_result: dict[str, Any] | None) -> str | None:
    if not qr_result:
        return None
    status_code = _extract_qr_status_code(qr_result)
    message = qr_result.get("message") or ""
    if status_code in {2, 3}:
        return "success"
    if status_code == 1:
        return "scanned"
    if status_code in {-1, 4}:
        return "expired"
    if qr_result["ok"] and message in {"登陆成功", "登录成功"}:
        return "success"
    if "扫码" in message:
        return "scanned"
    if "确认" in message:
        return "confirmed"
    if "过期" in message:
        return "expired"
    return "waiting_scan" if qr_result["ok"] else "failed"


def _derive_display_qrcode_status(qrcode_status: str, is_expired: bool) -> str:
    if is_expired and qrcode_status not in {"success", "expired"}:
        return "已过期"
    labels = {
        "idle": "未初始化",
        "unknown": "未知",
        "waiting_scan": "等待扫码",
        "scanned": "已扫码，等待确认",
        "confirmed": "已确认，等待登录完成",
        "success": "登录成功",
        "expired": "已过期",
        "failed": "失败",
    }
    normalized = str(qrcode_status or "").strip()
    return labels.get(normalized, normalized or "-")


def _derive_display_login_state(login_state: str) -> str:
    labels = {
        "unknown": "未知",
        "idle": "未开始",
        "offline": "离线",
        "qrcode_ready": "二维码已就绪",
        "qrcode_scanned": "已扫码",
        "qrcode_confirmed": "已确认",
        "qrcode_expired": "二维码已过期",
        "online": "在线",
        "error": "异常",
        "reconnecting": "重连中",
        "reconnect_failed": "重连失败",
        "reconnect_blocked": "重连受阻",
    }
    normalized = str(login_state or "").strip()
    return labels.get(normalized, normalized or "-")


def _qr_login_succeeded(qr_result: dict[str, Any] | None) -> bool:
    if not qr_result or not qr_result.get("ok"):
        return False
    if _extract_qr_status_code(qr_result) in {2, 3}:
        return True
    message = str(qr_result.get("message") or "").strip()
    return message in {"登陆成功", "登录成功"}


def _extract_qr_status_code(qr_result: dict[str, Any] | None) -> int | None:
    if not qr_result:
        return None
    payload = qr_result.get("payload")
    if not isinstance(payload, dict):
        return None
    candidates = [payload.get("status")]
    data_payload = payload.get("Data")
    if isinstance(data_payload, dict):
        candidates.extend([data_payload.get("status"), data_payload.get("Status")])
    lower_data_payload = payload.get("data")
    if isinstance(lower_data_payload, dict):
        candidates.extend([lower_data_payload.get("status"), lower_data_payload.get("Status")])
    for value in candidates:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _is_polling_active(*, login_state: str, qrcode_status: str, is_expired: bool) -> bool:
    if is_expired:
        return False
    if login_state == "online" or qrcode_status == "success":
        return False
    return qrcode_status in {"waiting_scan", "scanned", "confirmed"}


def resolve_wechat_ipad_qrcode_image(channel: Channel) -> dict[str, Any]:
    """返回当前二维码短链接，图片内容不再从渠道运行状态中解码。"""

    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    resolved = _resolve_qrcode_image(runtime_info)
    if not resolved:
        return {"available": False}
    return {"available": True, "kind": "url", "url": resolved["url"]}


def _normalize_qrcode_expires_at(value: Any) -> str | None:
    if value in (None, ""):
        return None
    parsed = _parse_datetime_like(value)
    if parsed:
        return ensure_shanghai_datetime(parsed).isoformat()
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return now_shanghai().isoformat()
    return (now_shanghai() + timedelta(seconds=seconds)).isoformat()


def _build_qrcode_expiry_view(runtime_info: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    expires_at = _parse_datetime_like(runtime_info.get("qrcode_expires_at"))
    if not expires_at:
        return {
            "expires_at": None,
            "expires_at_display": "-",
            "remaining_seconds": None,
            "countdown_label": "-",
            "is_expired": False,
        }
    reference_time = now or now_shanghai()
    if reference_time.tzinfo is None:
        reference_time = ensure_shanghai_datetime(reference_time)
    remaining_seconds = int((expires_at - reference_time).total_seconds())
    is_expired = remaining_seconds <= 0
    seconds = max(remaining_seconds, 0)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        countdown_label = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    else:
        countdown_label = f"{minutes:02d}:{secs:02d}"
    return {
        "expires_at": expires_at.isoformat(),
        "expires_at_display": _format_beijing_time(expires_at),
        "remaining_seconds": seconds,
        "countdown_label": "已过期" if is_expired else countdown_label,
        "is_expired": is_expired,
    }


def _resolve_qrcode_link_url(runtime_info: dict[str, Any]) -> str | None:
    resolved = _resolve_qrcode_image(runtime_info)
    if not resolved or resolved["kind"] != "url":
        return None
    return resolved["url"]


def _resolve_qrcode_image(runtime_info: dict[str, Any]) -> dict[str, str] | None:
    """只解析 874 当前提供的二维码 URL，不接受历史 Base64 运行字段。"""

    normalized = WechatIpadApiClient.normalize_qr_image_source(runtime_info.get("qrcode_url"))
    if normalized["url"]:
        return {"kind": "url", "url": normalized["url"]}
    return None
