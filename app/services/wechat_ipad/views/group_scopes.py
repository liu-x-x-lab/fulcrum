"""群插件覆盖、配置和阻断原因的只读展示规则。"""

from __future__ import annotations

from typing import Any
import json

from app.models.entities import GroupPluginScope
from app.services.plugin_runtime.registry import get_plugin_schema
from app.services.plugin_schemas import deep_merge_config


def _build_chatroom_plugin_scope_snapshot(
    *,
    chatroom_count: int,
    active_chatroom_count: int,
    message_observed_count: int,
    member_synced_chatroom_count: int,
    bot_plugin_count: int,
    enabled_bot_plugin_count: int,
    plugin_registry_count: int,
    latest_source: str,
    sync_freshness: dict[str, str],
    persisted_scope_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    persisted_scope_stats = persisted_scope_stats or {}
    freshness_label = str(sync_freshness.get("label") or "尚无时间样本")
    freshness_tone = str(sync_freshness.get("tone") or "muted")
    freshness_hint = str(sync_freshness.get("hint") or "")
    source_hint = f"最近来源：{latest_source}。" if latest_source and latest_source != "-" else "最近来源：暂无明确来源。"
    preconditions = [
        {
            "label": "群资料实体",
            "value": str(chatroom_count),
            "state": "ready" if chatroom_count > 0 else "missing",
            "summary": f"本地 chatrooms 已有 {chatroom_count} 个样本" if chatroom_count > 0 else "本地 chatrooms 仍无样本",
        },
        {
            "label": "成员记录群",
            "value": str(member_synced_chatroom_count),
            "state": "ready" if member_synced_chatroom_count > 0 else "pending",
            "summary": (
                f"已有 {member_synced_chatroom_count} 个群具备 chatroom_members"
                if member_synced_chatroom_count > 0
                else "chatroom_members 仍未形成稳定样本"
            ),
        },
        {
            "label": "bot 插件绑定",
            "value": f"{enabled_bot_plugin_count} / {bot_plugin_count}",
            "state": "ready" if enabled_bot_plugin_count > 0 else "pending" if bot_plugin_count > 0 else "missing",
            "summary": (
                f"bot 插件绑定已存在，启用 {enabled_bot_plugin_count} / 总计 {bot_plugin_count}"
                if bot_plugin_count > 0
                else "bot 侧还没有插件绑定样本"
            ),
        },
    ]
    missing_capabilities = [
        "缺群级 enable/disable 映射",
        "缺 allow/deny 持久化口径",
        "缺群级命令/能力边界模型",
        "缺审计与运营动作留痕",
    ]

    if chatroom_count <= 0:
        stage_key = "not_ready"
        stage_label = "尚未具备"
        stage_tone = "muted"
        evidence = "当前没有本地 chatroom / chatroom_members 样本，群级插件权限仍停留在扩展位骨架。"
        action = "先补首批群消息或群资料样本，再判断是否进入群级权限建模。"
        summary = "后台已预留群级插件权限治理入口，但当前还没有足够本地群资料支撑下一步。"
    elif active_chatroom_count <= 0:
        stage_key = "skeleton_only"
        stage_label = "仅有群资料骨架"
        stage_tone = "muted"
        evidence = f"已观测 {chatroom_count} 个群，但 {message_observed_count} 个群仍全部停留在 message_observed 骨架。"
        action = "先补真实群资料同步，至少让部分群进入非骨架状态，再评估群级插件权限边界。"
        summary = "当前只能确认后台已开始观测群聊，仍不足以支撑群级插件权限建模。"
    elif member_synced_chatroom_count <= 0:
        stage_key = "profile_only"
        stage_label = "仅有群资料"
        stage_tone = "outline"
        evidence = (
            f"已观测 {chatroom_count} 个群，其中 {active_chatroom_count} 个群已有资料，"
            "但成员快照仍为 0。"
        )
        action = "下一步先补成员快照，确认本地样本能支撑按群做插件治理。"
        summary = "当前已有群资料基础，但群级插件权限仍缺成员维度证据，暂不适合落真实规则。"
    else:
        stage_key = "ready_for_modeling"
        stage_label = "可进入群级权限建模"
        stage_tone = "success"
        evidence = (
            f"已观测 {chatroom_count} 个群，{active_chatroom_count} 个群已有资料，"
            f"{member_synced_chatroom_count} 个群已有成员记录。"
        )
        action = "可开始设计群级启停 / allow/deny / 能力边界的数据口径，但仍需真实样本继续对齐。"
        summary = "当前本地群资料已足够进入群级插件权限建模准备阶段，可继续收口最小规则边界。"

    readiness_summary = (
        f"前置条件：群资料={chatroom_count}，成员记录群={member_synced_chatroom_count}，"
        f"bot 插件绑定启用/总计={enabled_bot_plugin_count}/{bot_plugin_count}，插件镜像={plugin_registry_count}。"
    )
    persisted_scope_summary = (
        f"persisted scopes={persisted_scope_stats.get('total', 0)}, "
        f"inherited={persisted_scope_stats.get('inherited', 0)}, overridden={persisted_scope_stats.get('overridden', 0)}, "
        f"chatrooms={persisted_scope_stats.get('overridden_chatrooms', 0)}/{persisted_scope_stats.get('scoped_chatrooms', 0)}"
    )
    return {
        "stage_key": stage_key,
        "stage_label": stage_label,
        "stage_tone": stage_tone,
        "headline": f"群级插件权限准备度：{stage_label}",
        "operator_summary": f"{stage_label}；{evidence}；下一步：{action}",
        "summary": summary,
        "evidence": evidence,
        "action": action,
        "readiness_summary": readiness_summary,
        "persisted_scope_summary": persisted_scope_summary,
        "persisted_scope_total": int(persisted_scope_stats.get("total", 0)),
        "persisted_scope_inherited": int(persisted_scope_stats.get("inherited", 0)),
        "persisted_scope_overridden": int(persisted_scope_stats.get("overridden", 0)),
        "persisted_scope_overridden_chatrooms": int(persisted_scope_stats.get("overridden_chatrooms", 0)),
        "persisted_scope_scoped_chatrooms": int(persisted_scope_stats.get("scoped_chatrooms", 0)),
        "preconditions": preconditions,
        "missing_capabilities": missing_capabilities,
        "source_hint": source_hint,
        "freshness_key": str(sync_freshness.get("key") or "no_sample"),
        "freshness_label": freshness_label,
        "freshness_tone": freshness_tone,
        "freshness_hint": freshness_hint,
        "boundary_note": "当前仅为群级插件权限治理的准备度/扩展位，不代表真实群级启停、allow/deny 或命令边界已经上线。",
    }


def _build_chatroom_plugin_scope_brief(*, plugin_scope_snapshot: dict[str, Any] | None) -> dict[str, str]:
    plugin_scope_snapshot = plugin_scope_snapshot or {}
    stage_key = str(plugin_scope_snapshot.get("stage_key") or "unknown")
    stage_label = str(plugin_scope_snapshot.get("stage_label") or "未知")
    stage_tone = str(plugin_scope_snapshot.get("stage_tone") or "outline")
    freshness_key = str(plugin_scope_snapshot.get("freshness_key") or "no_sample")
    evidence = str(plugin_scope_snapshot.get("evidence") or "")
    action = str(plugin_scope_snapshot.get("action") or "")
    missing_capabilities = [str(item).strip() for item in plugin_scope_snapshot.get("missing_capabilities") or [] if str(item).strip()]

    if stage_key == "not_ready":
        priority_label = "高"
        priority_tone = "muted"
        focus_label = "先补样本"
        summary = "当前仍停留在准备度，先补首批群资料样本，再谈群级权限治理。"
        next_step_label = "暂不可建模"
        next_step_summary = "下一步先补首批群消息或群资料样本，再回来看群级权限建模。"
    elif stage_key == "skeleton_only":
        priority_label = "高"
        priority_tone = "muted"
        focus_label = "先脱离骨架"
        summary = "当前只看到群消息骨架，优先补真实群资料同步，不适合直接建模。"
        next_step_label = "暂不可建模"
        next_step_summary = "下一步先补真实群资料同步，让群状态先脱离 message_observed 骨架。"
    elif stage_key == "profile_only":
        priority_label = "中"
        priority_tone = "outline"
        focus_label = "先补成员快照"
        summary = "当前已有群资料，但成员维度证据不足，先补成员快照再推进群级治理。"
        next_step_label = "暂不可建模"
        next_step_summary = "下一步先补成员快照，确认可以按群和成员维度进入权限建模。"
    else:
        focus_label = "可建模"
        if freshness_key in {"stale", "expired"}:
            priority_label = "中"
            priority_tone = "outline"
            summary = "当前已具备建模准备度，但群资料样本不够新，先刷新样本再收口群级权限边界。"
            next_step_label = "可进入群级权限建模"
            next_step_summary = "可进入群级权限建模；先刷新群资料样本，再定义群级启停 / allow/deny / 能力边界口径。"
        else:
            priority_label = "中"
            priority_tone = "success"
            summary = "当前已可进入群级权限建模准备，但仍只到扩展位，不代表真实规则已上线。"
            next_step_label = "可进入群级权限建模"
            next_step_summary = "可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。"

    modeling_judgement = _build_chatroom_plugin_scope_modeling_judgement(
        stage_key=stage_key,
        stage_label=stage_label,
        stage_tone=stage_tone,
        freshness_key=freshness_key,
        freshness_label=str(plugin_scope_snapshot.get("freshness_label") or "尚无时间样本"),
    )
    blocker_snapshot = _build_chatroom_plugin_scope_blocker(
        stage_key=stage_key,
        missing_capabilities=missing_capabilities,
    )
    missing_focus = _build_chatroom_plugin_scope_missing_focus(
        stage_key=stage_key,
        missing_capabilities=missing_capabilities,
    )
    missing_summary = _build_chatroom_plugin_scope_missing_summary(
        stage_key=stage_key,
        missing_capabilities=missing_capabilities,
    )
    return {
        "status_label": stage_label,
        "status_tone": stage_tone,
        "focus_label": focus_label,
        "priority_label": priority_label,
        "priority_tone": priority_tone,
        "summary": summary,
        "operator_summary": f"{stage_label} / {focus_label}；{summary}",
        "evidence": evidence,
        "next_action": action,
        "focus_gap": missing_focus,
        "missing_summary": missing_summary,
        "next_step_label": next_step_label,
        "next_step_summary": next_step_summary,
        "modeling_tier_key": str(modeling_judgement.get("tier_key") or ""),
        "modeling_tier_label": str(modeling_judgement.get("tier_label") or ""),
        "modeling_tier_tone": str(modeling_judgement.get("tier_tone") or "outline"),
        "modeling_tier_summary": str(modeling_judgement.get("summary") or ""),
        "primary_gap_key": str(blocker_snapshot.get("gap_key") or ""),
        "primary_gap_label": str(blocker_snapshot.get("gap_label") or ""),
        "primary_gap_summary": str(blocker_snapshot.get("summary") or ""),
        "blocker_family_key": str(blocker_snapshot.get("family_key") or ""),
        "blocker_family_label": str(blocker_snapshot.get("family_label") or ""),
        "blocker_type_key": str(blocker_snapshot.get("gap_key") or ""),
        "blocker_type_label": str(blocker_snapshot.get("gap_label") or ""),
        "blocker_tone": str(blocker_snapshot.get("tone") or stage_tone),
        "blocker_headline": str(blocker_snapshot.get("headline") or ""),
        "blocker_gate_summary": str(blocker_snapshot.get("gate_summary") or ""),
        "blocker_short_summary": str(blocker_snapshot.get("short_summary") or ""),
        "decision_summary": (
            f"{str(modeling_judgement.get('tier_label') or stage_label)}；"
            f"主 blocker：{str(blocker_snapshot.get('gap_label') or '核心治理模型')}；"
            f"下一步：{next_step_summary}"
        ),
    }


def _summarize_group_plugin_scope_rows(scope_rows: list[GroupPluginScope]) -> dict[str, Any]:
    per_chatroom: dict[int, dict[str, int]] = {}
    total = len(scope_rows)
    inherited = 0
    overridden = 0
    enabled = 0
    disabled = 0

    for row in scope_rows:
        chatroom_bucket = per_chatroom.setdefault(
            int(row.chatroom_id),
            {"total": 0, "inherited": 0, "overridden": 0, "enabled": 0, "disabled": 0},
        )
        chatroom_bucket["total"] += 1
        if bool(row.enabled):
            enabled += 1
            chatroom_bucket["enabled"] += 1
        else:
            disabled += 1
            chatroom_bucket["disabled"] += 1

        if str(row.scope_status or "inherited") == "inherited":
            inherited += 1
            chatroom_bucket["inherited"] += 1
        else:
            overridden += 1
            chatroom_bucket["overridden"] += 1

    overridden_chatrooms = sum(1 for item in per_chatroom.values() if item["overridden"] > 0)
    return {
        "total": total,
        "inherited": inherited,
        "overridden": overridden,
        "enabled": enabled,
        "disabled": disabled,
        "scoped_chatrooms": len(per_chatroom),
        "overridden_chatrooms": overridden_chatrooms,
        "per_chatroom": per_chatroom,
    }


def _build_chatroom_plugin_scope_overview(
    *,
    plugin_scope_snapshot: dict[str, Any] | None,
    plugin_scope_brief: dict[str, Any] | None,
) -> dict[str, str]:
    snapshot = plugin_scope_snapshot or {}
    brief = plugin_scope_brief or {}
    status_label = str(brief.get("status_label") or snapshot.get("stage_label") or "未纳入判断")
    status_tone = str(brief.get("status_tone") or snapshot.get("stage_tone") or "muted")
    modeling_tier_label = str(brief.get("modeling_tier_label") or status_label)
    modeling_tier_tone = str(brief.get("modeling_tier_tone") or status_tone)
    priority_label = str(brief.get("priority_label") or "观察")
    priority_tone = str(brief.get("priority_tone") or status_tone)
    primary_gap_label = str(brief.get("primary_gap_label") or "核心治理模型")
    next_step_summary = str(brief.get("next_step_summary") or snapshot.get("action") or "继续补样本。")
    readiness_summary = str(snapshot.get("readiness_summary") or "")
    focus_gap = str(brief.get("focus_gap") or "")
    decision_summary = str(brief.get("decision_summary") or f"{modeling_tier_label}；主 blocker：{primary_gap_label}；下一步：{next_step_summary}")
    blocker_family_label = str(brief.get("blocker_family_label") or "模型前置")
    blocker_type_label = str(brief.get("blocker_type_label") or primary_gap_label)
    blocker_tone = str(brief.get("blocker_tone") or status_tone)
    blocker_headline = str(brief.get("blocker_headline") or f"当前最卡：{blocker_type_label}")
    blocker_gate_summary = str(brief.get("blocker_gate_summary") or brief.get("primary_gap_summary") or "")
    blocker_short_summary = str(brief.get("blocker_short_summary") or blocker_gate_summary)
    blocker_snapshot_summary = f"{blocker_family_label} / {blocker_type_label}：{blocker_short_summary}"

    if readiness_summary:
        evidence = readiness_summary.rstrip("。")
    else:
        evidence = str(snapshot.get("evidence") or brief.get("evidence") or "当前群级治理准备度仍待判断。").rstrip("。")

    short_summary = f"{decision_summary}；优先级：{priority_label}；{evidence}。"
    if focus_gap:
        short_summary = f"{short_summary} {focus_gap}"
    ops_digest = f"当前阶段={status_label}；关键证据={evidence}；下一步={next_step_summary}"
    single_overview_summary = (
        f"当前档位：{status_label}；"
        f"建模判断：{modeling_tier_label}；"
        f"优先级：{priority_label}；"
        f"主 blocker：{primary_gap_label}；"
        f"下一步：{next_step_summary}"
    )

    return {
        "headline": f"群级权限治理摘要：{modeling_tier_label}",
        "status_label": status_label,
        "status_tone": status_tone,
        "modeling_tier_label": modeling_tier_label,
        "modeling_tier_tone": modeling_tier_tone,
        "priority_label": priority_label,
        "priority_tone": priority_tone,
        "primary_gap_label": primary_gap_label,
        "blocker_family_label": blocker_family_label,
        "blocker_type_label": blocker_type_label,
        "blocker_tone": blocker_tone,
        "summary": short_summary,
        "operator_summary": (
            f"建模判断={modeling_tier_label}；"
            f"准备度={status_label}；"
            f"优先级={priority_label}；"
            f"主 blocker={primary_gap_label}；"
            f"下一步={next_step_summary}"
        ),
        "next_step_summary": next_step_summary,
        "evidence": evidence,
        "focus_gap": focus_gap,
        "blocker_headline": blocker_headline,
        "blocker_gate_summary": blocker_gate_summary,
        "blocker_short_summary": blocker_short_summary,
        "blocker_snapshot_summary": blocker_snapshot_summary,
        "ops_digest": ops_digest,
        "single_overview_summary": single_overview_summary,
    }


def _build_chatroom_plugin_scope_missing_summary(*, stage_key: str, missing_capabilities: list[str]) -> str:
    labels = _build_chatroom_plugin_scope_gap_labels(missing_capabilities)
    if labels:
        gap_label = "、".join(labels)
        if len(labels) >= 4:
            gap_label = f"{gap_label}四类核心模型"
        else:
            gap_label = f"{gap_label}等核心模型"
    else:
        gap_label = "核心模型"

    if stage_key == "not_ready":
        return f"主缺{gap_label}；当前先补群资料样本。"
    if stage_key == "skeleton_only":
        return f"主缺{gap_label}；当前先脱离消息骨架。"
    if stage_key == "profile_only":
        return f"主缺{gap_label}；当前先补成员快照。"
    return f"主缺{gap_label}；当前可进入最小规则建模。"


def _build_chatroom_plugin_scope_missing_focus(*, stage_key: str, missing_capabilities: list[str]) -> str:
    labels = _build_chatroom_plugin_scope_gap_labels(missing_capabilities)
    gap_label = "、".join(labels) if labels else "核心模型缺口待补"
    if stage_key == "not_ready":
        return f"仍缺 {gap_label}；当前先补群资料样本。"
    if stage_key == "skeleton_only":
        return f"仍缺 {gap_label}；当前先脱离 message_observed 骨架。"
    if stage_key == "profile_only":
        return f"仍缺 {gap_label}；当前先补成员快照。"
    return f"仍缺 {gap_label}；当前可进入最小规则建模。"


def _build_chatroom_plugin_scope_gap_labels(missing_capabilities: list[str]) -> list[str]:
    labels: list[str] = []
    for item in missing_capabilities:
        cleaned = str(item).strip()
        if not cleaned:
            continue
        if cleaned.startswith("缺群级 "):
            cleaned = cleaned.removeprefix("缺群级 ")
        elif cleaned.startswith("缺"):
            cleaned = cleaned.removeprefix("缺")
        cleaned = cleaned.strip()
        if cleaned.startswith("群级"):
            cleaned = cleaned.removeprefix("群级").strip()
        labels.append(cleaned)
    return labels


def _build_chatroom_plugin_scope_modeling_judgement(
    *,
    stage_key: str,
    stage_label: str,
    stage_tone: str,
    freshness_key: str,
    freshness_label: str,
) -> dict[str, str]:
    if stage_key in {"not_ready", "skeleton_only", "profile_only"}:
        return {
            "tier_key": "not_modelable",
            "tier_label": "仍不可建模",
            "tier_tone": "muted" if stage_tone == "muted" else "outline",
            "summary": f"当前仍处于“{stage_label}”，群级权限治理还不适合进入建模。",
        }
    if freshness_key in {"stale", "expired"}:
        return {
            "tier_key": "modelable_stale",
            "tier_label": "可进入建模但样本偏旧",
            "tier_tone": "outline",
            "summary": f"当前已具备建模前提，但群资料样本时效为“{freshness_label}”，应先刷新样本再收口规则。",
        }
    return {
        "tier_key": "modelable_ready",
        "tier_label": "已具备建模前提",
        "tier_tone": "success",
        "summary": "当前群资料、成员记录已形成最小建模前提，可直接进入群级治理规则收口。",
    }


def _build_chatroom_plugin_scope_blocker(*, stage_key: str, missing_capabilities: list[str]) -> dict[str, str]:
    prioritized_items = [
        (
            "enable/disable",
            "model_prerequisite",
            "模型前置",
            "群级启停模型",
            "优先补群级启停映射，否则无法落最小开关治理。",
            "先补群级启停模型，补完后才适合继续收口 allow/deny 与命令/能力边界。",
        ),
        (
            "allow/deny",
            "model_prerequisite",
            "模型前置",
            "allow/deny 边界",
            "优先补 allow/deny 口径，否则无法稳定表达准入边界。",
            "先补 allow/deny 边界，补完后才适合继续固化命令/能力边界。",
        ),
        (
            "命令/能力边界",
            "model_prerequisite",
            "模型前置",
            "命令/能力边界",
            "优先补命令/能力边界，否则无法定义群内插件可用范围。",
            "先补命令/能力边界，补完后才适合稳定表达群内插件可用范围。",
        ),
        (
            "审计与运营动作",
            "model_prerequisite",
            "模型前置",
            "审计留痕",
            "优先补审计与运营动作留痕，否则后续治理动作不可追溯。",
            "先补审计留痕，补完后才适合把群级治理动作作为正式运维能力推进。",
        ),
    ]
    if stage_key in {"not_ready", "skeleton_only"}:
        return {
            "family_key": "sample_prerequisite",
            "family_label": "样本前置",
            "gap_key": "chatroom_sample",
            "gap_label": "群资料样本",
            "tone": "muted",
            "headline": "当前最卡：群资料样本",
            "summary": "当前最影响下一步的是群资料样本不足，未到真实群级治理模型收口阶段。",
            "gate_summary": "先补群资料样本，补完后才有资格进入群成员样本或群级规则建模。",
            "short_summary": "群资料样本不足；先补群资料样本，补完后才有资格进入下一步。",
        }
    if stage_key == "profile_only":
        return {
            "family_key": "sample_prerequisite",
            "family_label": "样本前置",
            "gap_key": "member_sample",
            "gap_label": "群成员样本",
            "tone": "outline",
            "headline": "当前最卡：群成员样本",
            "summary": "当前最影响下一步的是群成员样本缺失，先补成员快照再谈规则建模。",
            "gate_summary": "先补群成员样本，补完后才有资格进入群级启停、allow/deny 与能力边界建模。",
            "short_summary": "群成员样本缺失；先补成员快照，补完后才有资格进入下一步。",
        }

    joined = " / ".join(missing_capabilities)
    for keyword, family_key, family_label, label, summary, gate_summary in prioritized_items:
        if keyword in joined:
            return {
                "family_key": family_key,
                "family_label": family_label,
                "gap_key": keyword,
                "gap_label": label,
                "tone": "outline",
                "headline": f"当前最卡：{label}",
                "summary": summary,
                "gate_summary": gate_summary,
                "short_summary": f"{label}仍缺；{gate_summary}",
            }
    return {
        "family_key": "model_prerequisite",
        "family_label": "模型前置",
        "gap_key": "missing_model",
        "gap_label": "核心治理模型",
        "tone": "outline",
        "headline": "当前最卡：核心治理模型",
        "summary": "当前仍缺最小群级治理模型，需要继续按优先级收口。",
        "gate_summary": "先补核心治理模型，补完后才适合继续推进群级权限治理落地。",
        "short_summary": "核心治理模型仍缺；先补模型，补完后才有资格进入下一步。",
    }


def _build_chatroom_plugin_scope_primary_gap(*, stage_key: str, missing_capabilities: list[str]) -> dict[str, str]:
    blocker_snapshot = _build_chatroom_plugin_scope_blocker(
        stage_key=stage_key,
        missing_capabilities=missing_capabilities,
    )
    return {
        "gap_key": str(blocker_snapshot.get("gap_key") or ""),
        "gap_label": str(blocker_snapshot.get("gap_label") or ""),
        "summary": str(blocker_snapshot.get("summary") or ""),
    }


def _build_effective_group_config(
    *,
    plugin_code: str | None,
    binding_config: dict[str, Any],
    scope_config: dict[str, Any],
) -> dict[str, Any]:
    base: dict[str, Any] = {}
    schema = get_plugin_schema(plugin_code)
    if schema:
        for field in schema.fields:
            if field.default not in (None, "", []):
                base[field.name] = field.default
    return deep_merge_config(deep_merge_config(base, binding_config), scope_config)


def _format_config_for_editor(config: dict[str, Any]) -> str:
    if not config:
        return ""
    return json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True)
