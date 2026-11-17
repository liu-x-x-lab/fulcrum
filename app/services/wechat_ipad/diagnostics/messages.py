"""最近收发链路与双向流量状态的诊断规则。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.wechat_ipad.common import _build_recent_call_freshness, _format_beijing_time


def _build_recent_message_flow_summary(runtime_info: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    receive_at = runtime_info.get("last_receive_at")
    send_at = runtime_info.get("last_send_at")
    receive_summary = str(runtime_info.get("last_receive_summary") or "").strip()
    send_summary = str(runtime_info.get("last_send_summary") or "").strip()
    receive_event_key = str(runtime_info.get("last_receive_event_key") or "").strip() or "-"
    dedupe_event_key = str(runtime_info.get("last_dedupe_event_key") or "").strip() or "-"
    receive_total = int(runtime_info.get("receive_total") or 0)
    dedupe_total = int(runtime_info.get("dedupe_total") or 0)
    inbound_summary = _parse_protocol_summary_dict(runtime_info.get("last_inbound_protocol_summary"))
    outbound_summary = _parse_protocol_summary_dict(runtime_info.get("last_outbound_protocol_summary"))

    receive_view = _build_recent_message_flow_side(
        kind="receive",
        at=receive_at,
        summary=receive_summary,
        reference=inbound_summary.get("source") or receive_event_key,
        fallback_reference=receive_event_key,
        now=now,
    )
    send_view = _build_recent_message_flow_side(
        kind="send",
        at=send_at,
        summary=send_summary,
        reference=outbound_summary.get("receiver_id") or outbound_summary.get("external_message_id"),
        fallback_reference="-",
        now=now,
    )
    focus = _select_recent_message_flow_focus(receive_view=receive_view, send_view=send_view)
    side_by_side_summary = _build_recent_message_flow_side_by_side_summary(
        receive_view=receive_view,
        send_view=send_view,
        focus=focus,
    )

    if receive_view["has_sample"] and send_view["has_sample"]:
        status_key = "both_active"
        status_label = "最近收发均有样本"
        status_tone = "success"
        summary = "最近接收与发送链路都已有样本，可直接判断两侧是否都在持续产生有效回包。"
    elif receive_view["has_sample"]:
        status_key = "receive_only"
        status_label = "仅最近接收有样本"
        status_tone = "outline"
        summary = "当前只看到最近接收样本；若运维关注出站链路，建议补一次发送动作确认回执。"
    elif send_view["has_sample"]:
        status_key = "send_only"
        status_label = "仅最近发送有样本"
        status_tone = "outline"
        summary = "当前只看到最近发送样本；若运维关注入站链路，建议补一次接收或拉取样本确认消息是否入链。"
    else:
        status_key = "no_sample"
        status_label = "尚无最近收发样本"
        status_tone = "muted"
        summary = "当前还没有最近接收或发送样本；先补一条入站或出站动作，这里才会形成可读摘要。"

    if focus["priority"] > 0 and focus["side"] in {"receive", "send"}:
        summary = f"{summary} 当前更值得先处理{focus['side_label']}。"

    attention = _build_recent_message_flow_attention(
        receive_view=receive_view,
        send_view=send_view,
        focus=focus,
    )
    operator_snapshot = _build_recent_message_flow_operator_snapshot(
        status_label=status_label,
        status_tone=status_tone,
        summary=summary,
        receive_view=receive_view,
        send_view=send_view,
        attention=attention,
        focus=focus,
        receive_total=receive_total,
        dedupe_total=dedupe_total,
        dedupe_event_key=dedupe_event_key,
    )

    detail = (
        f"优先关注={focus['side_label']}；"
        f"接收判断={receive_view['judgement_label']}；"
        f"发送判断={send_view['judgement_label']}；"
        f"接收：{receive_view['summary_line']}；"
        f"发送：{send_view['summary_line']}；"
        f"累计接收/去重={receive_total}/{dedupe_total}；"
        f"最近去重事件={dedupe_event_key}。"
    )
    operator_summary = (
        f"分类={status_label}；"
        f"优先关注={focus['side_label']}；"
        f"接收判断={receive_view['judgement_label']}；"
        f"发送判断={send_view['judgement_label']}；"
        f"接收={receive_view['operator_summary']}；"
        f"发送={send_view['operator_summary']}；"
        f"累计接收/去重={receive_total}/{dedupe_total}；"
        f"最近去重事件={dedupe_event_key}"
    )
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "headline": f"最近收发摘要：{status_label}",
        "summary": summary,
        "detail": detail,
        "operator_summary": operator_summary,
        "attention_label": attention["label"],
        "attention_tone": attention["tone"],
        "attention_action": attention["action"],
        "freshness_label": attention["freshness_label"],
        "freshness_tone": attention["freshness_tone"],
        "focus_side": focus["side"],
        "focus_side_label": focus["side_label"],
        "focus_reason": focus["reason"],
        "side_by_side_summary": side_by_side_summary["summary"],
        "priority_side_summary": side_by_side_summary["priority_summary"],
        "operator_snapshot": operator_snapshot,
        "receive": receive_view,
        "send": send_view,
        "receive_total": receive_total,
        "dedupe_total": dedupe_total,
        "last_dedupe_event_key": dedupe_event_key,
    }


def _build_recent_message_flow_side(
    *,
    kind: str,
    at: Any,
    summary: str,
    reference: Any,
    fallback_reference: str,
    now: datetime | None = None,
) -> dict[str, str | bool]:
    reference_text = str(reference or "").strip() or fallback_reference
    called_at_display = _format_beijing_time(at)
    freshness = _build_recent_call_freshness(at, now=now)
    has_sample = any(item not in (None, "", "-") for item in (at, summary, reference_text if reference_text != "-" else ""))
    if has_sample:
        state_label = "有样本"
        state_tone = "success" if freshness["key"] == "fresh" else "outline"
        summary_line = (
            f"有样本，最近时间={called_at_display}，"
            f"时效={freshness['label']}，"
            f"{'来源' if kind == 'receive' else '接收方'}={reference_text}"
        )
        operator_summary = (
            f"有样本/时效={freshness['label']}/最近时间={called_at_display}/"
            f"{'来源' if kind == 'receive' else '接收方'}={reference_text}/摘要={summary or '-'}"
        )
    else:
        state_label = "暂无样本"
        state_tone = "muted"
        summary_line = "暂无样本，最近时间=-，时效=尚无时间样本。"
        operator_summary = "暂无样本/时效=尚无时间样本/最近时间=-/摘要=-"
    judgement = _build_recent_message_flow_side_judgement(
        kind=kind,
        has_sample=has_sample,
        freshness=freshness,
    )
    return {
        "has_sample": has_sample,
        "kind": kind,
        "side_label": "接收" if kind == "receive" else "发送",
        "state_label": state_label,
        "state_tone": state_tone,
        "called_at_display": called_at_display,
        "freshness_key": freshness["key"],
        "freshness_label": freshness["label"],
        "freshness_tone": freshness["tone"],
        "judgement_key": judgement["key"],
        "judgement_label": judgement["label"],
        "judgement_tone": judgement["tone"],
        "judgement_hint": judgement["hint"],
        "priority": judgement["priority"],
        "summary": summary or "-",
        "reference": reference_text,
        "summary_line": summary_line,
        "operator_summary": operator_summary,
        "ops_brief": f"{judgement['label']} / {freshness['label']} / {called_at_display}",
    }


def _build_recent_message_flow_side_by_side_summary(
    *,
    receive_view: dict[str, Any],
    send_view: dict[str, Any],
    focus: dict[str, Any],
) -> dict[str, str]:
    summary = (
        f"接收：{receive_view['judgement_label']}（{receive_view['freshness_label']}）"
        f"；发送：{send_view['judgement_label']}（{send_view['freshness_label']}）"
    )
    if focus["side"] in {"receive", "send"}:
        return {
            "summary": summary,
            "priority_summary": f"当前拖后腿侧：{focus['side_label']}；{focus['reason']}",
        }
    if focus["side_label"] == "双侧都需处理":
        return {
            "summary": summary,
            "priority_summary": f"当前拖后腿侧：双侧都需处理；{focus['reason']}",
        }
    return {
        "summary": summary,
        "priority_summary": f"当前拖后腿侧：双侧都正常；{focus['reason']}",
    }


def _build_recent_message_flow_operator_snapshot(
    *,
    status_label: str,
    status_tone: str,
    summary: str,
    receive_view: dict[str, Any],
    send_view: dict[str, Any],
    attention: dict[str, str],
    focus: dict[str, Any],
    receive_total: int,
    dedupe_total: int,
    dedupe_event_key: str,
) -> dict[str, Any]:
    ordered_views = [receive_view, send_view]
    if focus["side"] == "send":
        ordered_views = [send_view, receive_view]

    evidence_parts = [
        f"优先关注={focus['side_label']}",
        *[
            f"{view['side_label']}={view['judgement_label']} {view['freshness_label']} {view['called_at_display']}"
            for view in ordered_views
        ],
        f"累计接收/去重={receive_total}/{dedupe_total}",
        f"最近去重事件={dedupe_event_key}",
    ]
    items = []
    for view in ordered_views:
        items.append(
            {
                "label": view["side_label"],
                "state_label": view["state_label"],
                "state_tone": view["state_tone"],
                "freshness_label": view["freshness_label"],
                "freshness_tone": view["freshness_tone"],
                "judgement_label": view["judgement_label"],
                "judgement_tone": view["judgement_tone"],
                "detail": f"{view['called_at_display']} / {view['reference']}",
                "operator_summary": view["operator_summary"],
            }
        )
    return {
        "headline": f"最近收发摘要：{status_label}",
        "status_label": status_label,
        "status_tone": status_tone,
        "summary": summary,
        "evidence": "；".join(evidence_parts),
        "action": attention["action"],
        "focus_label": attention["label"],
        "focus_tone": attention["tone"],
        "focus_side": focus["side"],
        "focus_side_label": focus["side_label"],
        "focus_reason": focus["reason"],
        "freshness_label": attention["freshness_label"],
        "freshness_tone": attention["freshness_tone"],
        "items": items,
        "operator_summary": (
            f"当前判断={status_label}；"
            f"优先关注={focus['side_label']}；"
            f"接收判断={receive_view['judgement_label']}；"
            f"发送判断={send_view['judgement_label']}；"
            f"关键证据={';'.join(evidence_parts)}；"
            f"下一步={attention['action']}"
        ),
    }


def _build_recent_message_flow_attention(
    *,
    receive_view: dict[str, Any],
    send_view: dict[str, Any],
    focus: dict[str, Any],
) -> dict[str, str]:
    receive_has_sample = bool(receive_view.get("has_sample"))
    send_has_sample = bool(send_view.get("has_sample"))
    receive_freshness = str(receive_view.get("freshness_key") or "no_sample")
    send_freshness = str(send_view.get("freshness_key") or "no_sample")

    if receive_has_sample and send_has_sample and receive_freshness == "fresh" and send_freshness == "fresh":
        return {
            "label": "持续观察",
            "tone": "success",
            "action": "最近接收和发送都是新鲜样本，当前先按常规观察即可。",
            "freshness_label": "双向新鲜",
            "freshness_tone": "success",
        }
    if not receive_has_sample and not send_has_sample:
        return {
            "label": "补首批样本",
            "tone": "muted",
            "action": "当前收发两侧都还没有样本，建议先补一条入站或出站动作让卡片形成基线。",
            "freshness_label": "暂无样本",
            "freshness_tone": "muted",
        }
    if not receive_has_sample or not send_has_sample:
        missing_side = str(focus.get("target_label") or ("接收" if not receive_has_sample else "发送"))
        return {
            "label": f"先补{missing_side}样本",
            "tone": "outline",
            "action": f"当前缺少最近{missing_side}样本，建议优先补齐单侧链路，避免只凭另一侧样本误判整体收发状态。",
            "freshness_label": "单侧缺样本",
            "freshness_tone": "outline",
        }
    if "expired" in {receive_freshness, send_freshness}:
        expired_side = str(focus.get("target_label") or ("接收" if receive_freshness == "expired" else "发送"))
        return {
            "label": f"先刷新{expired_side}样本",
            "tone": "outline",
            "action": f"最近{expired_side}样本已过旧，建议先补一次最新动作再判断当前链路是否仍正常。",
            "freshness_label": f"{expired_side}侧过旧",
            "freshness_tone": "outline",
        }
    if focus["side"] in {"receive", "send"}:
        return {
            "label": f"先刷新{focus['target_label']}样本",
            "tone": "outline",
            "action": f"最近{focus['target_label']}样本已偏旧，建议优先补一次最新{focus['target_label']}动作后再继续排查。",
            "freshness_label": f"{focus['target_label']}侧偏旧",
            "freshness_tone": "outline",
        }
    return {
        "label": "刷新偏旧链路",
        "tone": "outline",
        "action": "最近收发样本已不够新鲜，建议补一次最新接收或发送动作后再继续排查。",
        "freshness_label": "存在偏旧样本",
        "freshness_tone": "outline",
    }


def _build_recent_message_flow_side_judgement(
    *,
    kind: str,
    has_sample: bool,
    freshness: dict[str, str],
) -> dict[str, str | int]:
    side_label = "接收" if kind == "receive" else "发送"
    freshness_key = str(freshness.get("key") or "no_sample")

    if not has_sample:
        return {
            "key": "no_sample",
            "label": f"{side_label}缺样本",
            "tone": "muted",
            "hint": f"{side_label}侧还没有最近样本，当前无法确认这侧链路是否真的在跑。",
            "priority": 100,
        }
    if freshness_key == "expired":
        return {
            "key": "expired",
            "label": f"{side_label}样本过旧",
            "tone": "outline",
            "hint": f"{side_label}侧样本已过旧，建议优先刷新这一侧。",
            "priority": 80,
        }
    if freshness_key == "stale":
        return {
            "key": "stale",
            "label": f"{side_label}样本偏旧",
            "tone": "outline",
            "hint": f"{side_label}侧样本已偏旧，建议尽快补一条新样本确认当前状态。",
            "priority": 40,
        }
    return {
        "key": "healthy",
        "label": f"{side_label}正常",
        "tone": "success",
        "hint": f"{side_label}侧已有新鲜样本，当前没有明显拖后腿迹象。",
        "priority": 0,
    }


def _select_recent_message_flow_focus(
    *,
    receive_view: dict[str, Any],
    send_view: dict[str, Any],
) -> dict[str, str | int]:
    candidates = [receive_view, send_view]
    ranked = sorted(
        candidates,
        key=lambda item: (
            -int(item.get("priority") or 0),
            0 if item.get("kind") == "receive" else 1,
        ),
    )
    top = ranked[0]
    second = ranked[1]
    top_priority = int(top.get("priority") or 0)
    second_priority = int(second.get("priority") or 0)

    if top_priority <= 0:
        return {
            "side": "both",
            "target_label": "双侧",
            "side_label": "双侧都正常",
            "reason": "接收和发送两侧当前都没有明显拖后腿迹象。",
            "priority": 0,
        }
    if top_priority == second_priority:
        return {
            "side": "both",
            "target_label": "双侧",
            "side_label": "双侧都需处理",
            "reason": f"接收和发送两侧当前同档异常：{receive_view['judgement_label']} / {send_view['judgement_label']}。",
            "priority": top_priority,
        }
    return {
        "side": str(top.get("kind") or "receive"),
        "target_label": str(top.get("side_label") or "接收"),
        "side_label": str(top.get("judgement_label") or "待判断"),
        "reason": str(top.get("judgement_hint") or "该侧当前更值得优先处理。"),
        "priority": top_priority,
    }


def _parse_protocol_summary_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
