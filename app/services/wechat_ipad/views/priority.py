"""操作优先级、判断依据和置信度展示，不执行推荐动作。"""

from __future__ import annotations

from typing import Any

from app.services.wechat_ipad.diagnostics.errors import _format_error_stage_label


def _build_top_priority_snapshot(
    *,
    current_ops_snapshot: dict[str, Any],
    login_operator_snapshot: dict[str, Any],
    protocol_operator_snapshot: dict[str, Any],
    sync_operator_snapshot: dict[str, Any],
    message_operator_snapshot: dict[str, Any],
    error_operator_snapshot: dict[str, Any],
    error_context: dict[str, Any],
    group_stage_snapshot: dict[str, Any] | None = None,
    group_plugin_governance_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status_key = str(current_ops_snapshot.get("status_key") or "healthy")
    status_label = str(current_ops_snapshot.get("status_label") or "当前整体正常")
    status_tone = str(current_ops_snapshot.get("status_tone") or "muted")

    if status_key == "focus_error":
        module_label = "最近错误"
        tone = str(error_operator_snapshot.get("status_tone") or status_tone)
        issue_label = str(error_operator_snapshot.get("status_label") or "先看错误")
        evidence = str(error_operator_snapshot.get("detail") or error_operator_snapshot.get("summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str(error_operator_snapshot.get("attention_action") or current_ops_snapshot.get("action") or "先核对最近错误。")
        summary = f"当前优先处理最近错误，先核对最近失败阶段与回包，再决定是否刷新其他卡片。"
    elif status_key == "focus_login":
        module_label = "登录 / 二维码"
        tone = str(login_operator_snapshot.get("attention_tone") or login_operator_snapshot.get("status_tone") or status_tone)
        issue_label = str(login_operator_snapshot.get("attention_label") or login_operator_snapshot.get("status_label") or "先看登录 / 二维码")
        evidence = str(login_operator_snapshot.get("detail") or login_operator_snapshot.get("summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str(login_operator_snapshot.get("next_action") or current_ops_snapshot.get("action") or "先补登录 / 二维码样本。")
        summary = "当前优先处理登录 / 二维码链路，在登录未稳定前不把其他摘要视为已确认正常。"
    elif status_key == "focus_protocol":
        module_label = "协议探针"
        tone = str(protocol_operator_snapshot.get("focus_tone") or protocol_operator_snapshot.get("status_tone") or status_tone)
        issue_label = str(protocol_operator_snapshot.get("focus_label") or protocol_operator_snapshot.get("status_label") or "先刷新协议探针")
        evidence = str(protocol_operator_snapshot.get("detail") or protocol_operator_snapshot.get("summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str(protocol_operator_snapshot.get("action") or current_ops_snapshot.get("action") or "先刷新协议探针。")
        summary = "当前优先补协议探针，先确认失败、过旧或缺样本探针，再判断在线态。"
    elif status_key == "focus_sync":
        module_label = "最近同步"
        tone = str(sync_operator_snapshot.get("status_tone") or status_tone)
        issue_label = str(sync_operator_snapshot.get("status_label") or "同步优先")
        evidence = str(sync_operator_snapshot.get("detail") or sync_operator_snapshot.get("summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str(sync_operator_snapshot.get("action") or current_ops_snapshot.get("action") or "先补同步样本。")
        summary = "当前优先处理最近同步，先确认是否存在异常、待续拉取或仍缺首批同步样本。"
    elif status_key == "focus_message":
        module_label = "最近收发"
        tone = str(message_operator_snapshot.get("focus_tone") or message_operator_snapshot.get("status_tone") or status_tone)
        issue_label = str(message_operator_snapshot.get("focus_label") or message_operator_snapshot.get("status_label") or "先补收发样本")
        evidence = str(message_operator_snapshot.get("evidence") or message_operator_snapshot.get("summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str(message_operator_snapshot.get("action") or current_ops_snapshot.get("action") or "先补最近收发样本。")
        summary = "当前优先补最近收发样本，避免单侧缺口把链路误判成整体正常。"
    elif status_key == "focus_group_sync":
        module_label = "群资料同步"
        tone = str((group_stage_snapshot or {}).get("status_tone") or status_tone)
        issue_label = str((group_stage_snapshot or {}).get("status_label") or "群资料待补")
        evidence = str((group_stage_snapshot or {}).get("operator_summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str((group_stage_snapshot or {}).get("action") or current_ops_snapshot.get("action") or "先补群资料同步样本。")
        summary = "当前高优先异常不明显，优先补群资料同步深度与时效，再评估治理准备度。"
    else:
        governance_status = str((group_plugin_governance_snapshot or {}).get("status_label") or "")
        governance_focus = str((group_plugin_governance_snapshot or {}).get("focus_label") or "")
        module_label = "持续观察"
        tone = "success"
        issue_label = "无高优先异常"
        evidence = str(current_ops_snapshot.get("evidence") or "登录、协议、同步、收发当前没有明显高优先异常。")
        if governance_status and governance_status not in {"可进入群级权限建模", "未纳入判断"}:
            next_action = "当前无高优先异常，优先继续补真实样本并观察下一次同步。"
            summary = f"当前无高优先异常，可继续补真实样本并观察下一次同步；群级治理当前主要是{governance_focus or governance_status}。"
        else:
            next_action = "当前无高优先异常，优先继续补真实样本并观察下一次同步。"
            summary = "当前无高优先异常，优先继续补真实样本并观察下一次同步。"

    basis_parts = _build_top_priority_basis_parts(
        status_key=status_key,
        login_operator_snapshot=login_operator_snapshot,
        protocol_operator_snapshot=protocol_operator_snapshot,
        sync_operator_snapshot=sync_operator_snapshot,
        message_operator_snapshot=message_operator_snapshot,
        error_operator_snapshot=error_operator_snapshot,
        error_context=error_context,
        group_stage_snapshot=group_stage_snapshot,
        group_plugin_governance_snapshot=group_plugin_governance_snapshot,
    )
    basis_summary = " / ".join(basis_parts)
    confidence_snapshot = _build_top_priority_confidence_snapshot(
        status_key=status_key,
        login_operator_snapshot=login_operator_snapshot,
        protocol_operator_snapshot=protocol_operator_snapshot,
        sync_operator_snapshot=sync_operator_snapshot,
        message_operator_snapshot=message_operator_snapshot,
        error_context=error_context,
        group_stage_snapshot=group_stage_snapshot,
    )
    summary_line = f"当前首要处理项：{module_label}；{summary}"
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": tone,
        "module_label": module_label,
        "issue_label": issue_label,
        "headline": summary_line,
        "summary": summary,
        "summary_line": summary_line,
        "evidence": evidence,
        "basis_summary": basis_summary,
        "basis_parts": basis_parts,
        "confidence_key": confidence_snapshot["key"],
        "confidence_label": confidence_snapshot["label"],
        "confidence_tone": confidence_snapshot["tone"],
        "confidence_summary": confidence_snapshot["summary"],
        "next_action": next_action,
        "operator_summary": (
            f"当前优先项={module_label}；"
            f"判断={issue_label}；"
            f"当前把握={confidence_snapshot['label']}；"
            f"口径={confidence_snapshot['summary']}；"
            f"判定依据={basis_summary}；"
            f"关键证据={evidence}；"
            f"下一步={next_action}"
        ),
    }


def _build_top_priority_confidence_snapshot(
    *,
    status_key: str,
    login_operator_snapshot: dict[str, Any],
    protocol_operator_snapshot: dict[str, Any],
    sync_operator_snapshot: dict[str, Any],
    message_operator_snapshot: dict[str, Any],
    error_context: dict[str, Any],
    group_stage_snapshot: dict[str, Any] | None = None,
) -> dict[str, str]:
    if status_key == "focus_error":
        freshness_key = str(error_context.get("freshness_key") or "no_sample")
        if freshness_key in {"fresh", "stale"}:
            return {
                "key": "high_current_issue",
                "label": "高置信度当前故障",
                "tone": "outline",
                "summary": "当前故障信号明确，建议优先处理。",
            }
        return {
            "key": "refresh_then_confirm",
            "label": "历史旧样本 / 先刷新",
            "tone": "muted",
            "summary": "最近错误样本偏旧，先刷新后再判断是否仍是当前故障。",
        }

    if status_key == "focus_login":
        login_status_key = str(login_operator_snapshot.get("status_key") or "unknown")
        if login_status_key == "expired":
            return {
                "key": "high_current_issue",
                "label": "高置信度当前故障",
                "tone": "outline",
                "summary": "登录异常信号明确，建议优先处理当前登录链路。",
            }
        return {
            "key": "sample_gap_pending",
            "label": "缺样本待确认",
            "tone": "muted",
            "summary": "登录链路样本还不完整，先补样本再判断是否为当前故障。",
        }

    if status_key == "focus_protocol":
        protocol_status_key = str(protocol_operator_snapshot.get("status_key") or "unknown")
        if protocol_status_key == "failed":
            return {
                "key": "high_current_issue",
                "label": "高置信度当前故障",
                "tone": "outline",
                "summary": "协议失败信号明确，建议优先处理当前故障。",
            }
        if protocol_status_key in {"expired", "stale"}:
            return {
                "key": "refresh_then_confirm",
                "label": "历史旧样本 / 先刷新",
                "tone": "muted",
                "summary": "协议样本偏旧，先刷新探针再判断当前状态。",
            }
        return {
            "key": "sample_gap_pending",
            "label": "缺样本待确认",
            "tone": "muted",
            "summary": "协议样本仍不完整，先补样本再判断。",
        }

    if status_key == "focus_sync":
        sync_status_key = str(sync_operator_snapshot.get("status_key") or "unknown")
        if sync_status_key == "recent_error":
            return {
                "key": "high_current_issue",
                "label": "高置信度当前故障",
                "tone": "outline",
                "summary": "最近同步异常较明确，建议优先处理当前同步故障。",
            }
        if sync_status_key == "pending_continue":
            return {
                "key": "observe_in_progress",
                "label": "继续观察",
                "tone": "muted",
                "summary": "当前轮次仍在继续，先完成本轮拉取再判断是否异常。",
            }
        return {
            "key": "sample_gap_pending",
            "label": "缺样本待确认",
            "tone": "muted",
            "summary": "同步样本仍不足，先补样本再判断。",
        }

    if status_key == "focus_message":
        focus_label = str(message_operator_snapshot.get("focus_label") or "")
        if "刷新" in focus_label or "过旧" in str(message_operator_snapshot.get("focus_side_label") or ""):
            return {
                "key": "refresh_then_confirm",
                "label": "历史旧样本 / 先刷新",
                "tone": "muted",
                "summary": "最近收发样本偏旧，先刷新后再判断当前链路。",
            }
        return {
            "key": "sample_gap_pending",
            "label": "缺样本待确认",
            "tone": "muted",
            "summary": "最近收发样本不完整，先补样本再判断。",
        }

    if status_key == "focus_group_sync":
        freshness_key = str((group_stage_snapshot or {}).get("freshness_key") or "no_sample")
        if freshness_key in {"stale", "expired"}:
            return {
                "key": "refresh_then_confirm",
                "label": "历史旧样本 / 先刷新",
                "tone": "muted",
                "summary": "群资料样本偏旧，先刷新后再判断。",
            }
        return {
            "key": "sample_gap_pending",
            "label": "缺样本待确认",
            "tone": "muted",
            "summary": "群资料样本仍不足，先补样本再判断。",
        }

    return {
        "key": "observe_in_progress",
        "label": "继续观察",
        "tone": "success",
        "summary": "当前无高优先异常，以继续观察为主。",
    }


def _build_top_priority_basis_parts(
    *,
    status_key: str,
    login_operator_snapshot: dict[str, Any],
    protocol_operator_snapshot: dict[str, Any],
    sync_operator_snapshot: dict[str, Any],
    message_operator_snapshot: dict[str, Any],
    error_operator_snapshot: dict[str, Any],
    error_context: dict[str, Any],
    group_stage_snapshot: dict[str, Any] | None = None,
    group_plugin_governance_snapshot: dict[str, Any] | None = None,
) -> list[str]:
    if status_key == "focus_error":
        stage_label = _format_error_stage_label(error_context.get("stage"))
        freshness_label = str(error_context.get("freshness_label") or "尚无时间样本")
        return _compact_priority_basis_parts(
            [
                f"最近错误时效={freshness_label}，当前按{error_operator_snapshot.get('attention_label') or '优先处理'}看待",
                f"错误分类偏向{error_operator_snapshot.get('status_label') or '待归类异常'}",
                f"{stage_label}阶段已出现失败样本" if stage_label != "-" else "",
            ]
        )
    if status_key == "focus_login":
        login_status_key = str(login_operator_snapshot.get("status_key") or "unknown")
        login_basis_map = {
            "expired": "二维码样本已过期，旧登录信号不再可靠",
            "no_sample": "登录 / CheckQR 仍缺首批样本",
            "pending_result": "登录链路只有局部信号，尚未形成稳定回包",
            "waiting_scan": "二维码可见但当前仍停留在等待扫码阶段",
            "scanned": "已扫码但还在等待确认结果继续推进",
            "confirmed": "已扫码但还在等待确认结果继续推进",
        }
        return _compact_priority_basis_parts(
            [
                login_basis_map.get(login_status_key, "登录 / 二维码链路仍未形成稳定闭环"),
                f"当前关注点仍是{login_operator_snapshot.get('attention_label') or login_operator_snapshot.get('status_label') or '登录 / 二维码'}",
            ]
        )
    if status_key == "focus_protocol":
        protocol_status_key = str(protocol_operator_snapshot.get("status_key") or "unknown")
        protocol_basis_map = {
            "failed": "最近协议探针里已有失败样本",
            "expired": "协议探针样本已过旧，不能直接当作当前在线态",
            "stale": "协议探针样本偏旧，当前在线态仍待刷新确认",
            "no_sample": "四类协议探针仍缺最近样本",
            "partial_sample": "协议探针只有部分样本，结论还不完整",
        }
        return _compact_priority_basis_parts(
            [
                protocol_basis_map.get(protocol_status_key, "协议探针当前仍待判断"),
                f"当前更先看{protocol_operator_snapshot.get('focus_label') or protocol_operator_snapshot.get('status_label') or '协议探针'}",
            ]
        )
    if status_key == "focus_sync":
        sync_status_key = str(sync_operator_snapshot.get("status_key") or "unknown")
        sync_basis_map = {
            "recent_error": "最近同步样本显示异常，优先级高于补旧样本",
            "pending_continue": "continue_flag 仍提示这一轮还没拉到末尾",
            "no_sample": "最近同步还没形成首批可判断样本",
            "success_no_new": "当前没有高优先故障，更像同步空拉取或继续观察场景",
        }
        return _compact_priority_basis_parts(
            [
                sync_basis_map.get(sync_status_key, "最近同步链路仍待继续判断"),
                f"当前同步档位={sync_operator_snapshot.get('status_label') or '待判断'}",
            ]
        )
    if status_key == "focus_message":
        return _compact_priority_basis_parts(
            [
                f"最近收发的主要拖后腿侧是{message_operator_snapshot.get('focus_side_label') or message_operator_snapshot.get('focus_label') or '待判断'}",
                f"当前关注点={message_operator_snapshot.get('focus_label') or '先补收发样本'}",
                f"整体收发判断={message_operator_snapshot.get('status_label') or '待判断'}",
            ]
        )
    if status_key == "focus_group_sync":
        freshness_label = str((group_stage_snapshot or {}).get("freshness_label") or "尚无时间样本")
        return _compact_priority_basis_parts(
            [
                f"当前高优先异常不明显，主要剩群资料档位={((group_stage_snapshot or {}).get('status_label') or '待判断')}",
                f"群资料同步时效={freshness_label}",
            ]
        )

    governance_status = str((group_plugin_governance_snapshot or {}).get("status_label") or "")
    governance_focus = str((group_plugin_governance_snapshot or {}).get("focus_label") or "")
    parts = [
        "登录、协议、同步、收发当前都未见更高优先异常",
        "当前更适合继续补真实样本并观察下一次同步",
    ]
    if governance_status and governance_status not in {"可进入群级权限建模", "未纳入判断"}:
        parts.append(f"群级治理当前更像{governance_focus or governance_status}，先不按已确认故障处理")
    return _compact_priority_basis_parts(parts)


def _compact_priority_basis_parts(parts: list[str]) -> list[str]:
    compacted: list[str] = []
    for part in parts:
        text = str(part or "").strip().rstrip("。")
        if text:
            compacted.append(text)
        if len(compacted) >= 3:
            break
    return compacted


def _build_current_ops_item(label: str, state_label: Any, tone: Any, hint: Any) -> dict[str, str]:
    normalized_state = str(state_label or "-")
    normalized_hint = str(hint or "").strip()
    summary = normalized_state if not normalized_hint else f"{normalized_state} / {normalized_hint}"
    return {
        "label": label,
        "state_label": normalized_state,
        "tone": str(tone or "muted"),
        "summary": summary,
    }
