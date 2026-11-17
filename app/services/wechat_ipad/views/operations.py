"""登录流程和当前待处理事项的只读视图。"""

from __future__ import annotations

from typing import Any

from app.services.wechat_ipad.common import _format_beijing_time
from app.services.wechat_ipad.views.priority import _build_current_ops_item


def _build_login_operator_snapshot(
    *,
    login_state: str,
    qrcode_status: str,
    qrcode_status_display: str,
    qrcode_is_expired: bool,
    qrcode_countdown_label: str,
    qrcode_expires_at_display: str,
    last_login_at: Any,
    qrcode_refresh_count: Any,
    qrcode_message: Any,
    qrcode_image_available: bool,
    check_qr_result: dict[str, Any],
) -> dict[str, Any]:
    normalized_login_state = str(login_state or "unknown")
    normalized_qrcode_status = str(qrcode_status or "idle")
    normalized_qrcode_display = str(qrcode_status_display or normalized_qrcode_status or "unknown")
    check_qr_state_label = str(check_qr_result.get("state_label") or "尚未调用")
    check_qr_freshness_label = str(check_qr_result.get("freshness_label") or "尚无时间样本")
    check_qr_called_at = str(check_qr_result.get("called_at_display") or "-")
    check_qr_message = str(check_qr_result.get("message") or "-")
    last_login_display = _format_beijing_time(last_login_at)
    refresh_count = int(qrcode_refresh_count or 0)
    qrcode_message_text = str(qrcode_message or "").strip() or "-"
    has_check_qr_sample = str(check_qr_result.get("freshness_key") or "no_sample") != "no_sample"

    if normalized_login_state == "online" or normalized_qrcode_status == "success":
        status_key = "online"
        status_label = "已登录"
        status_tone = "success"
        attention_label = "转看运行态"
        attention_tone = "success"
        summary = "当前登录链路已闭环，二维码阶段可暂时降级，先转看同步、收发和错误态。"
        next_action = "优先核对最近同步诊断、收发摘要和最近错误态，确认在线后链路是否继续推进。"
    elif qrcode_is_expired or normalized_qrcode_display == "expired":
        status_key = "expired"
        status_label = "二维码已过期"
        status_tone = "outline"
        attention_label = "刷新二维码"
        attention_tone = "outline"
        summary = "当前二维码样本已过期，继续盯旧二维码或旧 CheckQR 结果没有意义。"
        next_action = "先执行“刷新二维码”，拿到新码后重新扫码；若连续过期，再检查登录初始化链路。"
    elif normalized_qrcode_status in {"scanned", "confirmed"}:
        status_key = normalized_qrcode_status
        status_label = "等待手机确认" if normalized_qrcode_status == "confirmed" else "已扫码，等待确认"
        status_tone = "outline"
        attention_label = "盯确认结果"
        attention_tone = "outline"
        summary = "二维码链路已进入扫码后阶段，当前不用再先关注二维码图片本身。"
        next_action = "先看最近 CheckQR 是否继续刷新；若长时间停在该阶段，补一次状态同步或刷新二维码。"
    elif normalized_qrcode_status == "waiting_scan":
        status_key = "waiting_scan"
        status_label = "等待扫码"
        status_tone = "outline"
        attention_label = "先看二维码"
        attention_tone = "outline"
        summary = "当前已有可用二维码链路，下一眼先判断二维码是否还有效、CheckQR 是否持续更新。"
        next_action = "先扫码；若运维侧已扫码但页面仍不推进，马上核对最近 CheckQR 时间和返回消息。"
    elif qrcode_image_available or qrcode_message_text != "-" or has_check_qr_sample:
        status_key = "pending_result"
        status_label = "等待链路回包"
        status_tone = "muted"
        attention_label = "补状态样本"
        attention_tone = "muted"
        summary = "当前已出现部分登录链路信号，但还不足以直接判定已登录或二维码可继续使用。"
        next_action = "优先执行状态同步或等待下一次 CheckQR 结果，再判断是否需要刷新二维码。"
    else:
        status_key = "no_sample"
        status_label = "尚未形成样本"
        status_tone = "muted"
        attention_label = "初始化登录"
        attention_tone = "muted"
        summary = "当前还没有二维码或最近 CheckQR 样本，不能直接判断登录链路已开始。"
        next_action = "先执行“初始化登录”，拿到二维码和首个 CheckQR 样本后再判断当前阶段。"

    detail_items = [
        {
            "label": "登录态",
            "value": normalized_login_state,
            "tone": "success" if status_key == "online" else "muted" if normalized_login_state in {"unknown", "offline"} else "outline",
        },
        {"label": "二维码状态", "value": normalized_qrcode_display, "tone": status_tone},
        {"label": "是否过期", "value": "已过期" if qrcode_is_expired else "有效中", "tone": "outline" if qrcode_is_expired else "success"},
        {"label": "CheckQR", "value": f"{check_qr_state_label} / {check_qr_freshness_label}", "tone": str(check_qr_result.get('state_tone') or 'muted')},
    ]
    detail = (
        f"最近登录={last_login_display}；最近 CheckQR={check_qr_called_at}；"
        f"二维码过期时间={qrcode_expires_at_display}；剩余有效期={qrcode_countdown_label or '-'}；"
        f"二维码刷新次数={refresh_count}；CheckQR 消息={check_qr_message}"
    )
    if qrcode_message_text != "-":
        detail = f"{detail}；当前提示={qrcode_message_text}"
    operator_summary = (
        f"分类={status_label}；登录态={normalized_login_state}；二维码={normalized_qrcode_display}；"
        f"是否过期={'是' if qrcode_is_expired else '否'}；最近登录={last_login_display}；"
        f"最近CheckQR={check_qr_called_at}；CheckQR={check_qr_state_label}/{check_qr_freshness_label}；下一步={next_action}"
    )
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "attention_label": attention_label,
        "attention_tone": attention_tone,
        "headline": f"登录 / 二维码一眼判断：{status_label}",
        "summary": summary,
        "detail": detail,
        "next_action": next_action,
        "operator_summary": operator_summary,
        "detail_items": detail_items,
    }


def _build_current_ops_snapshot(
    *,
    login_operator_snapshot: dict[str, Any],
    protocol_operator_snapshot: dict[str, Any],
    sync_operator_snapshot: dict[str, Any],
    message_operator_snapshot: dict[str, Any],
    error_operator_snapshot: dict[str, Any],
    error_context: dict[str, Any],
    group_stage_snapshot: dict[str, Any] | None = None,
    group_plugin_governance_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_freshness_key = str((error_context or {}).get("freshness_key") or "no_sample")
    error_attention_label = str(error_operator_snapshot.get("attention_label") or "")
    login_status_key = str(login_operator_snapshot.get("status_key") or "unknown")
    protocol_status_key = str(protocol_operator_snapshot.get("status_key") or "unknown")
    sync_status_key = str(sync_operator_snapshot.get("status_key") or "unknown")
    message_focus_label = str(message_operator_snapshot.get("focus_label") or "")
    group_status_key = str((group_stage_snapshot or {}).get("status_key") or "unknown")
    group_freshness_key = str((group_stage_snapshot or {}).get("freshness_key") or "no_sample")

    items = [
        _build_current_ops_item("错误", error_operator_snapshot.get("status_label"), error_operator_snapshot.get("status_tone"), error_operator_snapshot.get("attention_label")),
        _build_current_ops_item("登录", login_operator_snapshot.get("status_label"), login_operator_snapshot.get("status_tone"), login_operator_snapshot.get("attention_label")),
        _build_current_ops_item("协议", protocol_operator_snapshot.get("status_label"), protocol_operator_snapshot.get("status_tone"), protocol_operator_snapshot.get("focus_label")),
        _build_current_ops_item("同步", sync_operator_snapshot.get("status_label"), sync_operator_snapshot.get("status_tone"), None),
        _build_current_ops_item("收发", message_operator_snapshot.get("status_label"), message_operator_snapshot.get("status_tone"), message_operator_snapshot.get("focus_label")),
        _build_current_ops_item("群资料", (group_stage_snapshot or {}).get("status_label") or "未纳入本次判断", (group_stage_snapshot or {}).get("status_tone") or "muted", None),
        _build_current_ops_item("群级治理", (group_plugin_governance_snapshot or {}).get("status_label") or "未纳入本次判断", (group_plugin_governance_snapshot or {}).get("status_tone") or "muted", (group_plugin_governance_snapshot or {}).get("focus_label")),
    ]

    if error_attention_label == "优先处理" and error_freshness_key in {"fresh", "stale"}:
        status_key = "focus_error"
        status_label = "先看错误"
        status_tone = "outline"
        focus_label = "错误优先"
        focus_tone = "outline"
        summary = "当前总体状态：存在需要优先处理的最近错误，先不要把旧探针或旧同步样本误判成正常。"
        evidence = str(error_operator_snapshot.get("detail") or error_operator_snapshot.get("summary") or "最近错误样本提示需要优先处理。")
        action = str(error_operator_snapshot.get("attention_action") or "先核对最近错误接口、阶段和最新回包。")
    elif login_status_key in {"expired", "no_sample", "pending_result"}:
        status_key = "focus_login"
        status_label = "先看登录 / 二维码"
        status_tone = "outline" if login_status_key == "expired" else "muted"
        focus_label = str(login_operator_snapshot.get("attention_label") or "先看登录 / 二维码")
        focus_tone = str(login_operator_snapshot.get("attention_tone") or status_tone)
        summary = "当前总体状态：登录 / 二维码链路还没稳定闭环，其他摘要只能作为旁证，不能先判整体正常。"
        evidence = str(login_operator_snapshot.get("detail") or login_operator_snapshot.get("summary") or "登录 / 二维码链路仍待确认。")
        action = str(login_operator_snapshot.get("next_action") or "先补登录 / 二维码样本。")
    elif protocol_status_key in {"failed", "expired", "stale", "no_sample", "partial_sample"}:
        status_key = "focus_protocol"
        status_label = "先刷新协议探针"
        status_tone = "outline" if protocol_status_key in {"failed", "expired", "stale"} else "muted"
        focus_label = str(protocol_operator_snapshot.get("focus_label") or "先刷新协议探针")
        focus_tone = str(protocol_operator_snapshot.get("focus_tone") or status_tone)
        summary = "当前总体状态：协议探针还不足以稳定说明在线态，先补失败、过旧或缺样本探针。"
        evidence = str(protocol_operator_snapshot.get("detail") or protocol_operator_snapshot.get("summary") or "最近协议探针样本不足。")
        action = str(protocol_operator_snapshot.get("action") or "先刷新协议探针。")
    elif sync_status_key in {"no_sample", "recent_error", "pending_continue", "success_no_new"}:
        status_key = "focus_sync"
        status_label = "先看同步 / 补样本"
        status_tone = "outline" if sync_status_key in {"recent_error", "pending_continue"} else "muted"
        focus_label = "同步优先"
        focus_tone = status_tone
        summary = "当前总体状态：同步链路还没有完全收口，先确认是否异常、待续拉取或仍缺首批同步样本。"
        evidence = str(sync_operator_snapshot.get("detail") or sync_operator_snapshot.get("summary") or "最近同步摘要仍提示需要继续判断。")
        action = str(sync_operator_snapshot.get("action") or sync_operator_snapshot.get("operator_summary") or "先补同步样本。")
    elif message_focus_label != "持续观察":
        status_key = "focus_message"
        status_label = "先补收发样本"
        status_tone = "outline" if "刷新" in message_focus_label else "muted"
        focus_label = message_focus_label or "先补收发样本"
        focus_tone = str(message_operator_snapshot.get("focus_tone") or status_tone)
        summary = "当前总体状态：最近收发链路样本还不够完整或不够新鲜，先补单侧样本再判断整体运行。"
        evidence = str(message_operator_snapshot.get("evidence") or message_operator_snapshot.get("summary") or "最近收发样本不足。")
        action = str(message_operator_snapshot.get("action") or "先补一条最近收发样本。")
    elif group_stage_snapshot and (group_status_key != "member_records_available" or group_freshness_key in {"stale", "expired", "no_sample"}):
        status_key = "focus_group_sync"
        status_label = "先补群资料同步"
        status_tone = "outline" if group_status_key != "no_sample" else "muted"
        focus_label = "群资料待补"
        focus_tone = status_tone
        summary = "当前总体状态：基础运行样本大致正常，但群资料同步档位仍偏浅或偏旧，先补群资料/成员样本。"
        evidence = str(group_stage_snapshot.get("operator_summary") or group_stage_snapshot.get("evidence") or "群资料同步档位仍待提升。")
        action = str(group_stage_snapshot.get("action") or "先补群资料同步样本。")
    else:
        status_key = "healthy"
        status_label = "当前整体正常"
        status_tone = "success"
        focus_label = "继续观察"
        focus_tone = "success"
        summary = "当前总体状态：登录、协议、同步、收发都没有明显异常信号，可继续按常规观察。"
        evidence = (
            f"登录={login_operator_snapshot.get('status_label') or '-'}；"
            f"协议={protocol_operator_snapshot.get('status_label') or '-'}；"
            f"同步={sync_operator_snapshot.get('status_label') or '-'}；"
            f"收发={message_operator_snapshot.get('status_label') or '-'}。"
        )
        if group_stage_snapshot:
            evidence = f"{evidence}群资料={group_stage_snapshot.get('status_label') or '-'}。"
        action = "当前无需切换处理焦点，继续观察最近错误、同步和探针是否出现新变化。"

    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "headline": f"当前运维一眼判断：{status_label}",
        "focus_label": focus_label,
        "focus_tone": focus_tone,
        "summary": summary,
        "evidence": evidence,
        "action": action,
        "items": items,
        "operator_summary": f"当前总体状态={status_label}；关键证据={evidence}；下一步={action}",
    }
