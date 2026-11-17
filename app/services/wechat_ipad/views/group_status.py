"""群可见性、纳管状态、同步覆盖率和成员状态的展示规则。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.entities import Chatroom
from app.services.wechat_ipad.common import _build_recent_call_freshness, _parse_json


def _extract_chatroom_source(chatroom: Chatroom | None) -> str:
    if not chatroom:
        return "-"
    payload = _parse_json(chatroom.extra_json)
    source = str(payload.get("source") or "").strip()
    return source or "-"


def _build_chatroom_visibility_snapshot(
    *,
    chatroom_count: int,
    active_chatroom_count: int,
    message_observed_count: int,
    member_synced_chatroom_count: int,
    recent_items: list[dict[str, Any]],
    sync_depth: dict[str, Any],
) -> dict[str, Any]:
    recent_sources: list[str] = []
    for item in recent_items:
        source = str(item.get("observed_source") or "").strip()
        if not source or source == "-" or source in recent_sources:
            continue
        recent_sources.append(source)

    progress_items = [
        {"label": "已观测群", "value": chatroom_count, "tone": "outline"},
        {"label": "仅消息骨架", "value": message_observed_count, "tone": "muted"},
        {"label": "已补资料", "value": active_chatroom_count, "tone": "success"},
        {"label": "已有成员记录", "value": member_synced_chatroom_count, "tone": "success"},
    ]
    if chatroom_count <= 0:
        return {
            "summary_label": str(sync_depth.get("status_label") or "尚未形成群资料同步样本"),
            "detail_label": "当前后台还没有 chatroom / member 落库记录；一旦消息驱动或真实同步写入，这里会先显示覆盖度和来源。",
            "operator_summary": str(sync_depth.get("operator_summary") or ""),
            "status_key": str(sync_depth.get("status_key") or "no_sample"),
            "status_label": str(sync_depth.get("status_label") or "尚未形成群资料同步样本"),
            "status_tone": str(sync_depth.get("tone") or "muted"),
            "recent_sources": [],
            "progress_items": progress_items,
        }

    summary_label = str(sync_depth.get("status_label") or "")

    recent_source_label = "、".join(recent_sources) if recent_sources else "暂无明确来源"
    detail_label = (
        f"最近来源：{recent_source_label}；"
        f"其中 {message_observed_count} 个群仍是 message_observed 骨架。"
    )
    return {
        "summary_label": summary_label,
        "detail_label": detail_label,
        "operator_summary": str(sync_depth.get("operator_summary") or ""),
        "status_key": str(sync_depth.get("status_key") or "unknown"),
        "status_label": str(sync_depth.get("status_label") or summary_label),
        "status_tone": str(sync_depth.get("tone") or "outline"),
        "recent_sources": recent_sources,
        "progress_items": progress_items,
    }


def _build_chatroom_operator_snapshot(
    *,
    has_data: bool,
    message_observed_count: int,
    sync_depth: dict[str, Any],
    visibility_snapshot: dict[str, Any],
) -> dict[str, str]:
    if not has_data:
        detail = "当前还没有本地群资料样本；后续一旦消息驱动或真实同步写入，这里会先出现同步覆盖度与来源。"
    elif message_observed_count > 0:
        detail = (
            f"当前仍有 {message_observed_count} 个群停留在 message_observed 骨架阶段；"
            "建议继续核对是否已拿到真实群资料或成员同步样本。"
        )
    else:
        detail = "当前摘要未发现 message_observed 骨架，可继续核对最近来源是否稳定提供完整群资料。"
    return {
        "status_key": str(sync_depth.get("status_key") or "unknown"),
        "status_label": str(sync_depth.get("status_label") or "未知"),
        "status_tone": str(sync_depth.get("tone") or "outline"),
        "headline": (
            f"当前群资料同步档位：{str(sync_depth.get('status_label') or '未知')}"
        ),
        "summary": str(sync_depth.get("operator_summary") or ""),
        "detail": detail,
        "recent_source_summary": str(visibility_snapshot.get("detail_label") or ""),
    }


def _build_chatroom_ops_snapshot(
    *,
    chatroom_count: int,
    active_chatroom_count: int,
    message_observed_count: int,
    member_synced_chatroom_count: int,
    sync_depth: dict[str, Any],
    latest_source: str,
    sync_freshness: dict[str, str],
) -> dict[str, str]:
    status_key = str(sync_depth.get("status_key") or "unknown")
    status_label = str(sync_depth.get("status_label") or "未知")
    status_tone = str(sync_depth.get("tone") or "outline")

    if status_key == "no_sample":
        evidence = "当前没有本地 chatroom 或 chatroom_members 样本。"
        action = "先等待首批群消息或群资料同步落库，再判断是否进入下一档。"
    elif status_key == "message_skeleton_only":
        evidence = f"已观测 {chatroom_count} 个群，但 {message_observed_count} 个群仍全部停留在 message_observed 骨架。"
        action = "优先补群资料同步，至少让部分群进入“已有群资料”档。"
    elif status_key == "profile_without_members":
        evidence = (
            f"已观测 {chatroom_count} 个群，其中 {active_chatroom_count} 个群已有资料，"
            "但成员快照仍为 0。"
        )
        action = "优先补成员快照或成员同步结果，确认群成员链路已开始落库。"
    else:
        evidence = (
            f"已观测 {chatroom_count} 个群，{active_chatroom_count} 个群已有资料，"
            f"{member_synced_chatroom_count} 个群已有成员记录。"
        )
        action = "当前可继续核对最近来源和成员覆盖，进入更细的群运维排查。"

    source_hint = f"最近来源：{latest_source}。" if latest_source and latest_source != "-" else "最近来源：暂无明确来源。"
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "headline": f"当前群资料同步判断：{status_label}",
        "summary": f"当前处于“{status_label}”档。{evidence}",
        "evidence": evidence,
        "action": action,
        "source_hint": source_hint,
        "freshness_label": str(sync_freshness.get("label") or "尚无时间样本"),
        "freshness_key": str(sync_freshness.get("key") or "no_sample"),
        "freshness_tone": str(sync_freshness.get("tone") or "muted"),
        "freshness_hint": str(sync_freshness.get("hint") or ""),
    }


def _build_chatroom_stage_snapshot(
    *,
    sync_depth: dict[str, Any],
    ops_snapshot: dict[str, Any],
    sync_freshness: dict[str, Any],
) -> dict[str, str]:
    status_label = str(sync_depth.get("status_label") or "未知")
    status_key = str(sync_depth.get("status_key") or "unknown")
    status_tone = str(sync_depth.get("tone") or "outline")
    evidence = str(ops_snapshot.get("evidence") or "")
    action = str(ops_snapshot.get("action") or "")
    freshness_label = str(sync_freshness.get("label") or "尚无时间样本")

    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "headline": f"群资料同步一眼判断：{status_label}",
        "operator_summary": f"{status_label}；最近同步时效：{freshness_label}；{evidence}；下一步：{action}",
        "evidence": evidence,
        "action": action,
        "freshness_key": str(sync_freshness.get("key") or "no_sample"),
        "freshness_label": freshness_label,
        "freshness_tone": str(sync_freshness.get("tone") or "muted"),
        "freshness_hint": str(sync_freshness.get("hint") or ""),
    }


def _build_chatroom_summary_snapshot(
    *,
    sync_depth: dict[str, Any],
    ops_snapshot: dict[str, Any],
    sync_freshness: dict[str, Any],
) -> dict[str, str]:
    status_key = str(sync_depth.get("status_key") or "unknown")
    status_label = str(sync_depth.get("status_label") or "未知")
    status_tone = str(sync_depth.get("tone") or "outline")
    freshness_key = str(sync_freshness.get("key") or "no_sample")
    freshness_label = str(sync_freshness.get("label") or "尚无时间样本")
    freshness_tone = str(sync_freshness.get("tone") or "muted")
    evidence = str(ops_snapshot.get("evidence") or "")
    next_step = str(ops_snapshot.get("action") or "")

    if status_key == "no_sample":
        decision_label = "待首批样本"
        decision_tone = "muted"
    elif status_key == "message_skeleton_only":
        decision_label = "待补群资料"
        decision_tone = "muted"
    elif status_key == "profile_without_members":
        decision_label = "待补成员快照"
        decision_tone = "outline"
    elif freshness_key in {"stale", "expired"}:
        decision_label = "样本偏旧"
        decision_tone = "outline"
    else:
        decision_label = "可继续排查"
        decision_tone = "success"

    decision_summary = (
        f"档位={status_label}；时效={freshness_label}；判断={decision_label}；"
        f"证据={evidence}；下一步={next_step}"
    )
    short_summary = f"{status_label} / {decision_label}"
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "freshness_key": freshness_key,
        "freshness_label": freshness_label,
        "freshness_tone": freshness_tone,
        "decision_label": decision_label,
        "decision_tone": decision_tone,
        "headline": f"群资料运维摘要：{short_summary}",
        "short_summary": short_summary,
        "operator_summary": decision_summary,
        "evidence": evidence,
        "next_step": next_step,
    }


def _build_chatroom_coverage_snapshot(
    *,
    chatroom_count: int,
    active_chatroom_count: int,
    message_observed_count: int,
    member_synced_chatroom_count: int,
    latest_synced_at: str,
    latest_member_synced_at: str,
    latest_source: str,
    sync_freshness: dict[str, str],
    persisted_scope_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    persisted_scope_stats = persisted_scope_stats or {}
    profile_ratio = _build_ratio_item(
        label="群资料覆盖",
        numerator=active_chatroom_count,
        denominator=chatroom_count,
        tone="success" if active_chatroom_count > 0 else "outline",
    )
    member_ratio = _build_ratio_item(
        label="成员记录覆盖",
        numerator=member_synced_chatroom_count,
        denominator=chatroom_count,
        tone="success" if member_synced_chatroom_count > 0 else "outline",
    )
    skeleton_ratio = _build_ratio_item(
        label="消息骨架占比",
        numerator=message_observed_count,
        denominator=chatroom_count,
        tone="muted" if message_observed_count > 0 else "success",
    )
    if chatroom_count <= 0:
        return {
            "summary_label": "暂无群资料样本",
            "operator_summary": "当前还没有 chatroom / chatroom_members 本地样本，覆盖比例会在首次落库后显示。",
            "items": [profile_ratio, member_ratio, skeleton_ratio],
            "sync_freshness": sync_freshness,
            "detail_items": [
                {"label": "最近群资料同步", "value": latest_synced_at},
                {"label": "最近同步时效", "value": str(sync_freshness.get("label") or "尚无时间样本")},
                {"label": "最近成员记录同步", "value": latest_member_synced_at},
                {"label": "最近观测来源", "value": latest_source},
            ],
        }

    return {
        "summary_label": (
            f"已观测 {chatroom_count} 个群，群资料覆盖 {profile_ratio['percent_label']}，"
            f"成员记录覆盖 {member_ratio['percent_label']}，消息骨架占比 {skeleton_ratio['percent_label']}。"
        ),
        "operator_summary": (
            f"最近样本里，{active_chatroom_count} 个群已补基础资料，"
            f"{member_synced_chatroom_count} 个群已有成员记录，"
            f"{message_observed_count} 个群仍停留在 message_observed 骨架。"
        ),
        "sync_freshness": sync_freshness,
        "items": [profile_ratio, member_ratio, skeleton_ratio],
        "detail_items": [
            {"label": "最近群资料同步", "value": latest_synced_at},
            {"label": "最近同步时效", "value": str(sync_freshness.get("label") or "尚无时间样本")},
            {"label": "最近成员记录同步", "value": latest_member_synced_at},
            {"label": "最近观测来源", "value": latest_source},
        ],
    }


def _build_chatroom_sync_freshness(synced_at: Any, *, now: datetime | None = None) -> dict[str, str]:
    freshness = _build_recent_call_freshness(synced_at, now=now)
    key = str(freshness.get("key") or "no_sample")
    if key == "fresh":
        return {
            "key": "fresh",
            "label": "新鲜",
            "tone": "success",
            "hint": "最近群资料同步仍较新，可直接按当前样本继续核对覆盖度。",
        }
    if key == "stale":
        return {
            "key": "stale",
            "label": "偏旧",
            "tone": "outline",
            "hint": "最近群资料同步已偏旧，建议先刷新群资料样本再判断覆盖是否停滞。",
        }
    if key == "expired":
        return {
            "key": "expired",
            "label": "过旧",
            "tone": "outline",
            "hint": "最近群资料同步已过旧，当前覆盖度更可能只是历史样本。",
        }
    return {
        "key": "no_sample",
        "label": "尚无时间样本",
        "tone": "muted",
        "hint": "最近群资料缺少可用同步时间，当前无法判断样本是否仍新鲜。",
    }


def _build_ratio_item(*, label: str, numerator: int, denominator: int, tone: str) -> dict[str, str]:
    percent_value = 0 if denominator <= 0 else round((numerator / denominator) * 100)
    return {
        "label": label,
        "value_label": f"{numerator} / {denominator}",
        "percent_label": f"{percent_value}%",
        "tone": tone,
    }


def _build_chatroom_sync_depth(
    *,
    chatroom_count: int,
    active_chatroom_count: int,
    member_synced_chatroom_count: int,
) -> dict[str, str]:
    if chatroom_count <= 0:
        return {
            "status_key": "no_sample",
            "status_label": "尚未形成样本",
            "operator_summary": "尚未形成群资料同步样本，当前还没有本地 chatroom 或成员落库记录。",
            "tone": "muted",
        }
    if active_chatroom_count <= 0:
        return {
            "status_key": "message_skeleton_only",
            "status_label": "仅消息骨架",
            "operator_summary": f"已观测 {chatroom_count} 个群，但仍全部停留在 message_observed 骨架阶段。",
            "tone": "muted",
        }
    if member_synced_chatroom_count <= 0:
        return {
            "status_key": "profile_without_members",
            "status_label": "已有群资料，无成员快照",
            "operator_summary": f"已观测 {chatroom_count} 个群，其中 {active_chatroom_count} 个群已补到基础资料，但还没有成员快照。",
            "tone": "outline",
        }
    return {
        "status_key": "member_records_available",
        "status_label": "已有成员记录",
        "operator_summary": (
            f"已观测 {chatroom_count} 个群，{active_chatroom_count} 个群已补到资料，"
            f"{member_synced_chatroom_count} 个群已有成员记录。"
        ),
        "tone": "success",
    }


def _build_chatroom_detail_sync_depth(*, chatroom: Chatroom, member_items: list[dict[str, Any]]) -> str:
    status = str(chatroom.status or "").strip()
    if status == "message_observed":
        return "仅消息骨架"
    if status == "profile_synced":
        return "已有群资料，无成员快照"
    if status == "members_synced":
        return "已有成员记录"
    if member_items:
        return "已有成员记录"
    if status in {"active", "pending_sync"}:
        return "已有群资料，无成员快照"
    return "待判断"


def _format_chatroom_member_role(role: str | None) -> str:
    normalized = str(role or "").strip().lower()
    if normalized in {"owner", "group_owner"}:
        return "群主"
    if normalized in {"admin", "administrator"}:
        return "管理员"
    if normalized in {"member", "active", ""}:
        return "成员"
    return normalized or "成员"


def _format_chatroom_member_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"active", ""}:
        return "正常"
    if normalized == "message_observed":
        return "仅消息观测"
    if normalized in {"left", "removed"}:
        return "已退群"
    if normalized == "inactive":
        return "非活跃"
    return normalized or "未知"


def _format_chatroom_member_flag(flag: str | None) -> str:
    normalized = str(flag or "").strip()
    if not normalized:
        return "普通成员"
    labels = {
        "0": "普通成员",
        "1": "普通成员",
        "9": "特殊标记",
        "17": "邀请链路标记",
        "25": "群主/核心成员标记",
        "2049": "管理员",
        "2057": "管理员",
        "8193": "活跃成员标记",
        "8201": "活跃管理员标记",
    }
    return labels.get(normalized, f"Flag {normalized}")


def _format_group_scope_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"", "inherited"}:
        return "继承 bot_plugin"
    if normalized == "overridden":
        return "已存在群级覆盖"
    return normalized or "继承 bot_plugin"


def _format_chatroom_status(status: str | None) -> str:
    normalized = str(status or "").strip()
    if not normalized:
        return "未知"
    if normalized == "message_observed":
        return "消息观测骨架"
    if normalized == "profile_synced":
        return "已补群资料"
    if normalized == "members_synced":
        return "已补成员快照"
    if normalized == "active":
        return "已补充资料"
    if normalized == "pending_sync":
        return "待同步"
    return normalized
