"""协议调用包装与载荷摘要，集中记录调用结果和安全诊断信息。"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _utcnow_iso
from app.services.wechat_ipad.diagnostics.errors import _normalize_error_context


logger = get_logger(__name__)


def _summarize_wechat_call_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key == "channel":
            channel = value
            summary["channel_id"] = getattr(channel, "id", None)
            summary["channel_name"] = getattr(channel, "name", None)
            continue
        if key in {"proxy"}:
            summary[key] = "configured" if value else "empty"
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            rendered = str(value or "")
            summary[key] = rendered[:160]
            continue
        summary[key] = type(value).__name__
    return summary


def _summarize_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把协议回包压缩为运行诊断所需字段，禁止把大体积业务载荷写入渠道表。"""

    # 已经压缩过的摘要会在后续状态写入中被重复使用；保持原字段清单，避免每次持久化继续丢失信息。
    if isinstance(payload.get("top_level_keys"), list) and isinstance(payload.get("data_keys"), list):
        return {
            "top_level_keys": list(payload.get("top_level_keys") or [])[:20],
            "data_keys": list(payload.get("data_keys") or [])[:20],
            "message": str(payload.get("message") or ""),
            "code": payload.get("code"),
            "http_status": payload.get("http_status"),
            "success": (
                payload.get("success")
                if isinstance(payload.get("success"), bool)
                else WechatIpadApiClient.is_success(payload)
            ),
        }
    data = WechatIpadApiClient.extract_data(payload)
    return {
        "top_level_keys": sorted(list(payload.keys()))[:20],
        "data_keys": sorted(list(data.keys()))[:20] if isinstance(data, dict) else [],
        "message": WechatIpadApiClient.extract_message(payload) or "",
        "code": WechatIpadApiClient.extract_code(payload),
        "http_status": payload.get("http_status"),
        "success": WechatIpadApiClient.is_success(payload),
    }


def _compact_wechat_ipad_runtime_info(runtime_info: dict[str, Any]) -> dict[str, Any]:
    """清除二维码图片和原始取码回包，只保留可长期写入 MariaDB TEXT 的短运行状态。"""

    compacted = dict(runtime_info)
    # 二维码图片由 874 返回的短 URL 展示，Base64 与媒体类型不属于渠道长期运行状态。
    compacted.pop("qrcode_image_base64", None)
    compacted.pop("qrcode_image_media_type", None)
    login_init_response = compacted.get("last_login_init_response")
    if isinstance(login_init_response, dict):
        compacted["last_login_init_response"] = _summarize_provider_payload(login_init_response)
    return compacted


def _safe_call(func: Any, *, stage: str, trace_endpoint: str | None = None, **kwargs: Any) -> dict[str, Any]:
    logger.info(
        "wechat ipad provider call started",
        extra={
            "stage": stage,
            "endpoint": trace_endpoint or "",
            "params": _summarize_wechat_call_kwargs(kwargs),
        },
    )
    try:
        payload = func(**kwargs)
    except Exception as exc:
        logger.error(
            "wechat ipad provider call raised",
            exc_info=stage != "message_sync",
            extra={
                "stage": stage,
                "endpoint": trace_endpoint or "",
                "params": _summarize_wechat_call_kwargs(kwargs),
            },
        )
        return {
            "ok": False,
            "error": str(exc),
            "payload": {},
            "data": {},
            "message": None,
            "code": None,
            "error_context": _normalize_error_context({"stage": stage, "endpoint": trace_endpoint, "at": _utcnow_iso()}),
        }
    http_status = int(payload.get("http_status") or 200)
    ok = http_status < 400 and WechatIpadApiClient.is_success(payload)
    logger.info(
        "wechat ipad provider call completed",
        extra={
            "stage": stage,
            "endpoint": trace_endpoint or "",
            "ok": ok,
            "http_status": http_status,
            "code": WechatIpadApiClient.extract_code(payload),
            "provider_message": "" if stage == "message_sync" else WechatIpadApiClient.extract_message(payload) or "",
            "payload_preview": {} if stage == "message_sync" else _summarize_provider_payload(payload),
        },
    )
    return {
        "ok": ok,
        "error": None if ok else (WechatIpadApiClient.extract_message(payload) or "provider_error"),
        "payload": payload,
        "data": WechatIpadApiClient.extract_data(payload),
        "message": WechatIpadApiClient.extract_message(payload),
        "code": WechatIpadApiClient.extract_code(payload),
        "error_context": None if ok else _normalize_error_context({"stage": stage, "endpoint": trace_endpoint, "at": _utcnow_iso()}),
    }
