"""最近协议调用的状态与时效诊断。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import (
    _build_recent_call_freshness,
    _format_beijing_time,
    _join_probe_labels,
    _map_recent_call_state_tone,
)


def _build_recent_call_view(
    value: Any,
    *,
    fallback_message: Any = None,
    called_at: Any = None,
    now: datetime | None = None,
) -> dict[str, str]:
    payload = value if isinstance(value, dict) else {}
    message = (
        WechatIpadApiClient.extract_message(payload)
        or str(fallback_message or "").strip()
        or "-"
    )
    code = WechatIpadApiClient.extract_code(payload)
    http_status = payload.get("http_status")
    has_payload = bool(payload)
    # 874 的成功取码回包是 Success=true、Code=1；结构化成功标记优先于不同接口各自的返回码约定。
    reported_success = payload.get("success")
    is_success = (
        has_payload
        and (
            bool(reported_success)
            if isinstance(reported_success, bool)
            else WechatIpadApiClient.is_success(payload)
        )
        and http_status in (None, "", 200, "200")
    )
    has_structured_summary = message != "-"
    if is_success and has_structured_summary:
        state = "healthy"
        state_label = "调用成功"
        operator_hint = "最近一次调用返回成功回包，可继续结合消息和时间判断链路是否持续推进。"
    elif is_success:
        state = "blank"
        state_label = "返回空摘要"
        operator_hint = "最近一次调用没有硬错误，但返回摘要仍为空；建议回看原始回包结构是否发生漂移。"
    elif has_payload:
        state = "warning"
        state_label = "调用失败"
        operator_hint = f"最近一次调用返回异常：{message}；建议先核对 HTTP 状态、返回码和接口回包。"
    elif message != "-":
        state = "pending"
        state_label = "等待结果"
        operator_hint = f"最近一次还没有结构化回包，可先结合当前提示“{message}”继续判断流程阶段。"
    else:
        state = "empty"
        state_label = "尚未调用"
        operator_hint = "还没有最近调用记录；执行对应动作后，这里会显示结构化结果。"
    freshness = _build_recent_call_freshness(called_at, now=now)
    called_at_display = _format_beijing_time(called_at)
    operator_summary = (
        f"分档={state_label}；时效={freshness['label']}；最近调用={called_at_display}；消息={message}；"
        f"HTTP={http_status if http_status not in (None, '') else '-'}；Code={code if code not in (None, '') else '-'}"
    )
    return {
        "state": state,
        "state_label": state_label,
        "state_tone": _map_recent_call_state_tone(state),
        "operator_hint": f"{operator_hint} 当前时效分档：{freshness['hint']}",
        "operator_summary": operator_summary,
        "called_at_display": called_at_display,
        "message": message,
        "code": str(code) if code not in (None, "") else "-",
        "http_status": str(http_status) if http_status not in (None, "") else "-",
        "freshness_key": freshness["key"],
        "freshness_label": freshness["label"],
        "freshness_tone": freshness["tone"],
    }


def _build_recent_protocol_operator_snapshot(protocol_state: dict[str, Any]) -> dict[str, Any]:
    probe_specs = (
        ("CheckQR", "last_check_qr_result"),
        ("HeartBeat", "last_heartbeat_result"),
        ("HeartBeatLong", "last_heartbeat_long_result"),
        ("CacheInfo", "last_cache_info_result"),
    )
    items: list[dict[str, str]] = []
    failed_labels: list[str] = []
    expired_labels: list[str] = []
    stale_labels: list[str] = []
    missing_labels: list[str] = []
    healthy_labels: list[str] = []
    available_sample_count = 0

    for probe_label, key in probe_specs:
        result = protocol_state.get(key) if isinstance(protocol_state.get(key), dict) else {}
        state = str(result.get("state") or "empty")
        freshness_key = str(result.get("freshness_key") or "no_sample")
        state_label = str(result.get("state_label") or "尚未调用")
        freshness_label = str(result.get("freshness_label") or "尚无时间样本")
        called_at_display = str(result.get("called_at_display") or "-")
        message = str(result.get("message") or "-")

        if state == "warning":
            failed_labels.append(probe_label)
        elif state == "healthy" and freshness_key == "fresh":
            healthy_labels.append(probe_label)

        if state in {"empty", "pending"} or freshness_key == "no_sample":
            missing_labels.append(probe_label)
        else:
            available_sample_count += 1

        if freshness_key == "expired":
            expired_labels.append(probe_label)
        elif freshness_key == "stale":
            stale_labels.append(probe_label)

        items.append(
            {
                "label": probe_label,
                "state_label": state_label,
                "state_tone": str(result.get("state_tone") or _map_recent_call_state_tone(state)),
                "freshness_label": freshness_label,
                "freshness_tone": str(result.get("freshness_tone") or "muted"),
                "summary": f"{state_label} / {freshness_label}",
                "detail": f"最近调用={called_at_display}；消息={message}",
                "operator_summary": f"{probe_label}={state_label}；时效={freshness_label}；最近调用={called_at_display}；消息={message}",
            }
        )

    if failed_labels:
        status_key = "failed"
        status_label = "存在失败"
        status_tone = "outline"
        headline = "最近协议调用：部分失败"
        summary = (
            f"失败探针：{_join_probe_labels(failed_labels)}；"
            f"新鲜探针：{_join_probe_labels(healthy_labels)}；"
            f"下一眼先看失败探针和最近错误卡片。"
        )
        detail = (
            f"失败探针：{'、'.join(failed_labels)}；"
            f"偏旧/过旧：{_join_probe_labels(expired_labels + stale_labels)}；"
            f"样本不足：{_join_probe_labels(missing_labels)}。"
        )
        action = "先查看失败探针的 HTTP 状态、返回码和最近错误上下文，再决定是否补刷其余探针。"
        focus_label = "先看失败探针"
        focus_tone = "outline"
    elif available_sample_count <= 0:
        status_key = "no_sample"
        status_label = "样本不足"
        status_tone = "muted"
        headline = "最近协议调用：样本不足"
        summary = "四类探针都还没有形成可判断的最近样本，当前不能误判为正常。"
        detail = "当前四类探针缺少最近成功/失败样本；建议先触发状态同步或刷新二维码后再看总览。"
        action = "先补最近样本，优先执行状态同步，必要时补刷二维码或重连。"
        focus_label = "先补样本"
        focus_tone = "muted"
    elif expired_labels:
        status_key = "expired"
        status_label = "存在过旧样本"
        status_tone = "outline"
        headline = "最近协议调用：存在过旧样本"
        summary = (
            f"过旧探针：{_join_probe_labels(expired_labels)}；"
            f"新鲜探针：{_join_probe_labels(healthy_labels)}；"
            f"建议先刷新过旧探针，再判断当前在线态。"
        )
        detail = (
            f"过旧探针：{'、'.join(expired_labels)}；"
            f"偏旧探针：{_join_probe_labels(stale_labels)}；"
            f"样本不足：{_join_probe_labels(missing_labels)}。"
        )
        action = "优先刷新过旧探针，确认是否仍在线，再决定是否继续按旧回包排查。"
        focus_label = "优先刷新过旧探针"
        focus_tone = "outline"
    elif stale_labels:
        status_key = "stale"
        status_label = "存在偏旧样本"
        status_tone = "outline"
        headline = "最近协议调用：存在偏旧样本"
        summary = (
            f"偏旧探针：{_join_probe_labels(stale_labels)}；"
            f"新鲜探针：{_join_probe_labels(healthy_labels)}；"
            f"建议尽快刷新偏旧探针，避免把旧状态当成当前状态。"
        )
        detail = f"偏旧探针：{'、'.join(stale_labels)}；样本不足：{_join_probe_labels(missing_labels)}；新鲜探针：{_join_probe_labels(healthy_labels)}。"
        action = "先补刷偏旧探针；如果刷新后仍稳定，再按当前总览继续排查。"
        focus_label = "建议刷新偏旧探针"
        focus_tone = "outline"
    elif missing_labels:
        status_key = "partial_sample"
        status_label = "样本待补齐"
        status_tone = "muted"
        headline = "最近协议调用：样本待补齐"
        summary = f"{_join_probe_labels(healthy_labels)} 已有新鲜样本，但 {_join_probe_labels(missing_labels)} 仍缺少稳定样本。"
        detail = f"样本不足：{'、'.join(missing_labels)}；其余探针当前无失败且结果较新。"
        action = "优先补齐缺样本探针，补齐后再统一判断登录态与缓存态。"
        focus_label = "先补缺样本探针"
        focus_tone = "muted"
    else:
        status_key = "all_fresh"
        status_label = "全部新鲜"
        status_tone = "success"
        headline = "最近协议调用：全部新鲜"
        summary = "四类探针最近结果都较新，当前没有失败或旧样本信号。"
        detail = f"新鲜探针：{_join_probe_labels(healthy_labels)}；可优先结合同步诊断和最近错误卡片继续排查。"
        action = "下一眼先看同步诊断和当前错误态，确认是否存在协议正常但同步链路异常的情况。"
        focus_label = "转看同步与错误"
        focus_tone = "success"

    operator_summary = (
        f"总览={status_label}；失败={_join_probe_labels(failed_labels)}；"
        f"过旧={_join_probe_labels(expired_labels)}；偏旧={_join_probe_labels(stale_labels)}；"
        f"样本不足={_join_probe_labels(missing_labels)}；新鲜={_join_probe_labels(healthy_labels)}；"
        f"下一眼={focus_label}"
    )
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "headline": headline,
        "focus_label": focus_label,
        "focus_tone": focus_tone,
        "summary": summary,
        "detail": detail,
        "action": action,
        "operator_summary": operator_summary,
        "items": items,
    }
