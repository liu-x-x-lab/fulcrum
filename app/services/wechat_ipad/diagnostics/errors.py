"""协议错误阶段、类别与处理建议的纯诊断规则。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.wechat_ipad.common import _build_recent_call_freshness, _format_beijing_time


def _build_error_context_view(value: Any, *, now: datetime | None = None) -> dict[str, str | None]:
    context = _normalize_error_context(value)
    freshness = _build_recent_call_freshness(context.get("at"), now=now)
    return {
        "stage": context.get("stage") or "-",
        "endpoint": context.get("endpoint") or "-",
        "at": context.get("at"),
        "at_display": _format_beijing_time(context.get("at")),
        "freshness_key": freshness["key"],
        "freshness_label": freshness["label"],
        "freshness_tone": freshness["tone"],
        "freshness_hint": freshness["hint"],
    }


def _normalize_error_context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key in ("stage", "endpoint", "at"):
        current = str(value.get(key) or "").strip()
        if current:
            normalized[key] = current
    return normalized


def _build_error_operator_summary(error: Any, error_context: Any, *, now: datetime | None = None) -> str:
    message = str(error or "").strip()
    if not message:
        return "-"
    context = _normalize_error_context(error_context)
    stage = str(context.get("stage") or "").strip()
    endpoint = str(context.get("endpoint") or "").strip()
    at_display = _format_beijing_time(context.get("at"))
    freshness = _build_recent_call_freshness(context.get("at"), now=now)
    stage_label = _format_error_stage_label(stage)

    parts: list[str] = []
    if stage_label != "-":
        parts.append(f"{stage_label}阶段")
    if endpoint:
        parts.append(f"接口 {endpoint}")
    if at_display and at_display != "-":
        parts.append(f"时间 {at_display}")
    if freshness["key"] != "no_sample":
        parts.append(f"时效 {freshness['label']}")
    if parts:
        return f"{'，'.join(parts)}；最近错误：{message}。"
    return f"最近错误：{message}；当前缺少阶段、接口和时间上下文。"


def _build_error_diagnostics(error: Any, error_context: Any) -> dict[str, str]:
    message = str(error or "").strip()
    if not message:
        return {
            "state": "empty",
            "state_label": "当前无错误",
            "state_tone": "success",
            "category": "无",
            "operator_hint": "最近没有记录到错误；若链路仍异常，优先执行状态同步或消息拉取，补齐最新运行态样本。",
        }

    context = _normalize_error_context(error_context)
    stage = str(context.get("stage") or "").strip()
    normalized_stage = _normalize_error_stage_name(stage)
    category = _classify_error_category(
        stage=stage,
        endpoint=context.get("endpoint"),
        message=message,
    )
    if category == "login":
        return {
            "state": "warning",
            "state_label": "登录态异常",
            "state_tone": "outline",
            "category": "登录流程",
            "operator_hint": _build_error_operator_hint(category="login", stage=normalized_stage),
        }
    if category == "sync":
        return {
            "state": "warning",
            "state_label": "同步异常",
            "state_tone": "outline",
            "category": "消息同步",
            "operator_hint": _build_error_operator_hint(category="sync", stage=normalized_stage),
        }
    if category == "runtime":
        return {
            "state": "warning",
            "state_label": "运行态异常",
            "state_tone": "outline",
            "category": "运行态检查",
            "operator_hint": _build_error_operator_hint(category="runtime", stage=normalized_stage),
        }
    return {
        "state": "warning",
        "state_label": "待归类异常",
        "state_tone": "outline",
        "category": "待人工判断",
        "operator_hint": "最近错误已落库，但阶段分类不足；建议结合错误摘要、接口和原始回包继续定位。",
    }


def _build_error_attention(
    *,
    has_error: bool,
    freshness_key: str,
    state_label: str,
) -> dict[str, str]:
    if not has_error:
        return {
            "label": "无需处理",
            "tone": "success",
            "action": "当前没有最近错误样本，保持常规观察即可。",
        }
    if freshness_key == "fresh":
        return {
            "label": "优先处理",
            "tone": "outline",
            "action": f"最近错误仍是新鲜样本，建议按“{state_label}”立即核对对应接口与最新回包。",
        }
    if freshness_key == "stale":
        return {
            "label": "先刷新再判断",
            "tone": "outline",
            "action": "最近错误样本已偏旧，建议先执行状态同步或相关探活，确认问题是否仍在持续。",
        }
    if freshness_key == "expired":
        return {
            "label": "历史样本",
            "tone": "muted",
            "action": "最近错误样本已过旧，建议先刷新运行态与同步样本，再决定是否继续处置旧错误。",
        }
    return {
        "label": "样本不足",
        "tone": "muted",
        "action": "最近错误缺少可用时间样本，建议先补一次最新状态同步或探活结果。",
    }


def _build_error_operator_snapshot(
    error: Any,
    error_context: Any,
    *,
    error_diagnostics: dict[str, str],
    operator_summary: str,
    now: datetime | None = None,
) -> dict[str, str]:
    message = str(error or "").strip()
    context = _normalize_error_context(error_context)
    stage_label = _format_error_stage_label(context.get("stage"))
    endpoint = str(context.get("endpoint") or "").strip() or "-"
    at_display = _format_beijing_time(context.get("at"))
    freshness = _build_recent_call_freshness(context.get("at"), now=now)
    state_label = str(error_diagnostics.get("state_label") or "待归类异常")
    state_tone = str(error_diagnostics.get("state_tone") or "outline")
    category = str(error_diagnostics.get("category") or "-")
    operator_hint = str(error_diagnostics.get("operator_hint") or "").strip()
    attention = _build_error_attention(
        has_error=bool(message),
        freshness_key=str(freshness.get("key") or "no_sample"),
        state_label=state_label,
    )

    if not message:
        return {
            "status_key": "no_error",
            "status_label": "当前无错误",
            "status_tone": "success",
            "headline": "当前错误态：当前无错误",
            "summary": operator_hint or "最近没有记录到错误；当前没有需要优先处理的失败样本。",
            "detail": "关键信息：最近错误=-；阶段=-；接口=-；时间=-；时效=尚无时间样本；处置=无需处理。",
            "attention_label": attention["label"],
            "attention_tone": attention["tone"],
            "attention_action": attention["action"],
        }

    detail_parts = [
        f"分类={state_label}",
        f"归类={category}",
        f"阶段={stage_label if stage_label != '-' else '-'}",
        f"接口={endpoint}",
        f"时间={at_display if at_display and at_display != '-' else '-'}",
        f"时效={freshness['label']}",
        f"处置={attention['label']}",
        f"最近错误={message}",
    ]
    summary = operator_hint or operator_summary or f"最近一次错误已落库，可按阶段、接口和时间继续排查。"
    return {
        "status_key": str(error_diagnostics.get("state") or "warning"),
        "status_label": state_label,
        "status_tone": state_tone,
        "headline": f"当前错误态：{state_label}",
        "summary": summary,
        "detail": "关键信息：" + "；".join(detail_parts) + "。",
        "attention_label": attention["label"],
        "attention_tone": attention["tone"],
        "attention_action": attention["action"],
    }


def _format_error_stage_label(stage: str | None) -> str:
    raw = str(stage or "").strip()
    normalized = _normalize_error_stage_name(raw)
    if not normalized:
        return "-"
    labels = {
        "login_init": "登录初始化",
        "qrcode_refresh": "二维码刷新",
        "check_qr": "CheckQR",
        "login_confirm": "登录确认",
        "waiting_scan": "等待扫码",
        "reconnect": "重连恢复",
        "status_sync": "状态同步",
        "heartbeat": "HeartBeat",
        "heartbeat_long": "HeartBeatLong",
        "cache_info": "CacheInfo",
        "awaken": "Awaken",
        "twice_auto_auth": "自动登录",
        "message_sync": "消息同步",
        "contact_sync": "联系人同步",
        "chatroom_sync": "群资料同步",
        "sync_push": "同步回调",
    }
    return labels.get(normalized, raw or normalized)


def _normalize_error_stage_name(stage: str | None) -> str:
    normalized = str(stage or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return ""
    aliases = {
        "get_qr": "login_init",
        "getqr": "login_init",
        "login_qr": "login_init",
        "refresh_qr": "qrcode_refresh",
        "refresh_qrcode": "qrcode_refresh",
        "qrcode_refresh": "qrcode_refresh",
        "checkqr": "check_qr",
        "qr_check": "check_qr",
        "check_qrcode": "check_qr",
        "qrcode_check": "check_qr",
        "scan_qr_check": "check_qr",
        "qr_confirm": "login_confirm",
        "login_confirm": "login_confirm",
        "confirm_login": "login_confirm",
        "confirm_qr": "login_confirm",
        "waiting_confirm": "login_confirm",
        "wait_confirm": "login_confirm",
        "login_confirmation": "login_confirm",
        "device_confirm": "login_confirm",
        "scan_confirm": "login_confirm",
        "waiting_scan": "waiting_scan",
        "wait_scan": "waiting_scan",
        "scan_qr": "waiting_scan",
        "scan_code": "waiting_scan",
        "qrcode_wait_scan": "waiting_scan",
        "qr_code": "qrcode_refresh",
        "qrcode": "qrcode_refresh",
        "refresh_qr_code": "qrcode_refresh",
        "refresh_login_qrcode": "qrcode_refresh",
        "login_qrcode_refresh": "qrcode_refresh",
        "reconnect": "reconnect",
        "reconnect_login": "reconnect",
        "reconnect_probe": "reconnect",
        "relogin": "reconnect",
        "status_sync": "status_sync",
        "sync_status": "status_sync",
        "status_probe": "status_sync",
        "heart_beat": "heartbeat",
        "heartbeatlong": "heartbeat_long",
        "heart_beat_long": "heartbeat_long",
        "get_cache_info": "cache_info",
        "cacheinfo": "cache_info",
        "awaken_login": "awaken",
        "reconnect_awaken": "awaken",
        "awake": "awaken",
        "wakeup": "awaken",
        "wake_up": "awaken",
        "auto_auth": "twice_auto_auth",
        "twiceauth": "twice_auto_auth",
        "twice_autoauth": "twice_auto_auth",
        "second_auto_auth": "twice_auto_auth",
        "reconnect_auto_auth": "twice_auto_auth",
        "auto_login": "twice_auto_auth",
        "auto_login_confirm": "twice_auto_auth",
        "msg_sync": "message_sync",
        "msgsync": "message_sync",
        "sync_message": "message_sync",
        "pull_message": "message_sync",
        "message_pull": "message_sync",
        "sync_pull": "message_sync",
        "sync_messages": "message_sync",
        "message_sync_continue": "message_sync",
        "sync_contact": "contact_sync",
        "contacts_sync": "contact_sync",
        "contact_sync_pull": "contact_sync",
        "sync_contacts": "contact_sync",
        "chatroom_sync": "chatroom_sync",
        "sync_chatroom": "chatroom_sync",
        "chatroom_member_sync": "chatroom_sync",
        "sync_chatroom_members": "chatroom_sync",
        "sync_callback": "sync_push",
        "sync_push_callback": "sync_push",
    }
    return aliases.get(normalized, normalized)


def _classify_error_category(*, stage: str | None, endpoint: str | None, message: str | None) -> str:
    normalized_stage = _normalize_error_stage_name(stage)
    if normalized_stage in {"login_init", "qrcode_refresh", "check_qr", "login_confirm", "waiting_scan"}:
        return "login"
    if normalized_stage in {"message_sync", "contact_sync", "chatroom_sync", "sync_push"}:
        return "sync"
    if normalized_stage in {"reconnect", "status_sync", "heartbeat", "heartbeat_long", "cache_info", "awaken", "twice_auto_auth"}:
        return "runtime"

    evidence = " ".join(
        str(item or "").strip().lower()
        for item in (stage, endpoint, message)
        if str(item or "").strip()
    )
    if _contains_any_token(
        evidence,
        (
            "/api/login/getqr",
            "/api/login/checkqr",
            "/api/login/qrcode",
            "/api/login/confirm",
            "二维码",
            "qrcode",
            "扫码",
            "等待确认",
            "手机确认",
            "scan qr",
            "登录确认",
            "login confirm",
            "confirm login",
        ),
    ):
        return "login"
    if _contains_any_token(
        evidence,
        (
            "/api/msg/sync",
            "/api/message/sync",
            "/api/contact/sync",
            "/api/chatroom/sync",
            "sync_key",
            "synckey",
            "continue_flag",
            "continueflag",
            "消息同步",
            "拉取消息",
            "联系人同步",
            "群资料同步",
            "继续拉取",
            "同步失败",
        ),
    ):
        return "sync"
    if _contains_any_token(
        evidence,
        (
            "/api/login/heartbeat",
            "/api/login/heartbeatlong",
            "/api/login/getcacheinfo",
            "/api/login/awaken",
            "/api/login/twiceautoauth",
            "/api/login/reconnect",
            "heartbeat",
            "cacheinfo",
            "cache info",
            "awaken",
            "auto auth",
            "auto login",
            "自动登录",
            "重连",
            "重新连接",
            "唤醒",
            "状态同步",
        ),
    ):
        return "runtime"
    return "unknown"


def _build_error_operator_hint(*, category: str, stage: str) -> str:
    if category == "login":
        if stage in {"login_init", "qrcode_refresh"}:
            return "问题出在二维码准备链路；建议先核对 GetQR/二维码刷新是否返回 uuid、二维码链接和过期时间。"
        if stage in {"check_qr", "login_confirm", "waiting_scan"}:
            return "问题出在扫码或登录确认链路；建议先核对 CheckQR 最近回包、二维码状态和手机端确认进度。"
        return "问题出在二维码或登录确认链路；建议先检查二维码是否过期，再重新执行登录初始化或 CheckQR。"
    if category == "sync":
        if stage in {"contact_sync", "chatroom_sync", "sync_push"}:
            return "问题出在资料同步链路；建议先核对对应同步接口回包、continue_flag 与最近一次落库摘要。"
        return "问题出在消息拉取链路；建议先核对 sync_key、continue_flag 和最近一次 Msg/Sync 回包。"
    if stage in {"reconnect", "awaken"}:
        return "问题出在重连唤醒链路；建议先核对 Awaken 回包、新二维码或 wxid 是否已经刷新。"
    if stage == "twice_auto_auth":
        return "问题出在自动登录链路；建议先核对 TwiceAutoAuth 回包、wxid 与最近 HeartBeat 是否恢复。"
    return "问题出在运行态探活或重连链路；建议先检查 HeartBeat、CacheInfo 与重连接口回包是否一致。"


def _contains_any_token(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)
