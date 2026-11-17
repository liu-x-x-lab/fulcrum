"""群插件治理与启停模型的诊断视图。"""

from __future__ import annotations

from typing import Any


def _build_group_plugin_governance_snapshot(
    *,
    plugin_scope_snapshot: dict[str, Any] | None,
    plugin_scope_brief: dict[str, Any] | None,
    plugin_scope_overview: dict[str, Any] | None,
) -> dict[str, str]:
    snapshot = plugin_scope_snapshot or {}
    brief = plugin_scope_brief or {}
    overview = plugin_scope_overview or {}
    status_label = str(brief.get("status_label") or snapshot.get("stage_label") or "未纳入判断")
    status_tone = str(brief.get("status_tone") or snapshot.get("stage_tone") or "muted")
    focus_label = str(brief.get("focus_label") or "扩展位")
    priority_label = str(overview.get("priority_label") or brief.get("priority_label") or "观察")
    priority_tone = str(overview.get("priority_tone") or brief.get("priority_tone") or status_tone)
    summary = str(overview.get("summary") or brief.get("summary") or snapshot.get("summary") or "当前仅补到群级插件权限治理扩展位。")
    evidence = str(overview.get("evidence") or snapshot.get("evidence") or brief.get("evidence") or "群级插件权限准备度仍待判断。")
    action = str(brief.get("next_action") or snapshot.get("action") or "先补群资料样本。")
    missing_summary = str(brief.get("missing_summary") or "")
    boundary_note = str(snapshot.get("boundary_note") or "")
    next_step_label = str(brief.get("next_step_label") or "继续观察")
    next_step_summary = str(overview.get("next_step_summary") or brief.get("next_step_summary") or action)
    ops_digest = str(
        overview.get("ops_digest")
        or f"当前阶段={status_label}；关键证据={evidence}；下一步={next_step_summary}"
    )
    modeling_tier_label = str(overview.get("modeling_tier_label") or brief.get("modeling_tier_label") or status_label)
    modeling_tier_tone = str(overview.get("modeling_tier_tone") or brief.get("modeling_tier_tone") or status_tone)
    primary_gap_label = str(overview.get("primary_gap_label") or brief.get("primary_gap_label") or focus_label)
    primary_gap_summary = str(brief.get("primary_gap_summary") or missing_summary)
    decision_summary = str(brief.get("decision_summary") or f"{status_label}；主 blocker：{focus_label}；下一步：{next_step_summary}")
    blocker_family_label = str(overview.get("blocker_family_label") or brief.get("blocker_family_label") or "模型前置")
    blocker_type_label = str(overview.get("blocker_type_label") or brief.get("blocker_type_label") or primary_gap_label)
    blocker_tone = str(overview.get("blocker_tone") or brief.get("blocker_tone") or status_tone)
    blocker_headline = str(overview.get("blocker_headline") or brief.get("blocker_headline") or f"当前最卡：{blocker_type_label}")
    blocker_gate_summary = str(overview.get("blocker_gate_summary") or brief.get("blocker_gate_summary") or primary_gap_summary)
    blocker_short_summary = str(overview.get("blocker_short_summary") or brief.get("blocker_short_summary") or blocker_gate_summary)
    blocker_snapshot_summary = f"{blocker_family_label} / {blocker_type_label}：{blocker_short_summary}"
    single_overview_summary = (
        f"当前档位：{status_label}；"
        f"建模判断：{modeling_tier_label}；"
        f"优先级：{priority_label}；"
        f"主 blocker：{primary_gap_label}；"
        f"下一步：{next_step_summary}"
    )
    sample_gate_snapshot = _build_group_plugin_sample_gate_snapshot(
        plugin_scope_snapshot=snapshot,
        plugin_scope_brief=brief,
        plugin_scope_overview=overview,
    )
    return {
        "status_label": status_label,
        "status_tone": status_tone,
        "focus_label": focus_label,
        "priority_label": priority_label,
        "priority_tone": priority_tone,
        "headline": str(overview.get("headline") or f"群级治理准备度：{status_label}"),
        "summary": summary,
        "evidence": evidence,
        "action": action,
        "missing_summary": missing_summary,
        "next_step_label": next_step_label,
        "next_step_summary": next_step_summary,
        "boundary_note": boundary_note,
        "operator_summary": str(overview.get("operator_summary") or f"{status_label} / {focus_label} / {priority_label}；{summary}"),
        "ops_digest": ops_digest,
        "modeling_tier_label": modeling_tier_label,
        "modeling_tier_tone": modeling_tier_tone,
        "modeling_tier_summary": str(brief.get("modeling_tier_summary") or summary),
        "primary_gap_label": primary_gap_label,
        "primary_gap_summary": primary_gap_summary,
        "blocker_family_label": blocker_family_label,
        "blocker_type_label": blocker_type_label,
        "blocker_tone": blocker_tone,
        "blocker_headline": blocker_headline,
        "blocker_gate_summary": blocker_gate_summary,
        "blocker_short_summary": blocker_short_summary,
        "blocker_snapshot_summary": blocker_snapshot_summary,
        "decision_summary": decision_summary,
        "overview_summary": single_overview_summary,
        "single_overview_summary": single_overview_summary,
        "focus_gap": str(overview.get("focus_gap") or brief.get("focus_gap") or ""),
        "sample_gate_snapshot": sample_gate_snapshot,
    }


def _build_group_plugin_sample_gate_snapshot(
    *,
    plugin_scope_snapshot: dict[str, Any] | None,
    plugin_scope_brief: dict[str, Any] | None,
    plugin_scope_overview: dict[str, Any] | None,
) -> dict[str, str]:
    snapshot = plugin_scope_snapshot or {}
    brief = plugin_scope_brief or {}
    overview = plugin_scope_overview or {}
    stage_key = str(snapshot.get("stage_key") or "unknown")
    stage_label = str(snapshot.get("stage_label") or "未纳入判断")
    freshness_key = str(snapshot.get("freshness_key") or "no_sample")
    freshness_label = str(snapshot.get("freshness_label") or "尚无时间样本")
    readiness_summary = str(snapshot.get("readiness_summary") or "前置条件仍待补齐。")
    source_hint = str(snapshot.get("source_hint") or "最近来源仍待补齐。")
    boundary_note = str(snapshot.get("boundary_note") or "")
    next_step_summary = str(overview.get("next_step_summary") or brief.get("next_step_summary") or snapshot.get("action") or "继续补样本。")

    if stage_key in {"not_ready", "skeleton_only", "profile_only"}:
        gate_key = "samples_missing"
        gate_label = "样本门槛未满足"
        gate_tone = "muted" if stage_key in {"not_ready", "skeleton_only"} else "outline"
        headline = "群级权限样本门槛：未达下一步"
        summary = f"当前仍处于“{stage_label}”，本地 chatroom/member 样本还不足以进入下一步群级权限建模。"
    elif freshness_key in {"stale", "expired"}:
        gate_key = "samples_stale"
        gate_label = "样本基本满足但偏旧"
        gate_tone = "outline"
        headline = "群级权限样本门槛：可进入下一步前先刷新"
        summary = f"当前样本数量已够，但群资料时效为“{freshness_label}”，建议先刷新样本再进入群级权限建模。"
    else:
        gate_key = "samples_ready"
        gate_label = "样本门槛已满足"
        gate_tone = "success"
        headline = "群级权限样本门槛：可进入下一步"
        summary = "当前本地 chatroom/member 样本已具备最小门槛，可以进入群级权限建模准备，但仍只是扩展位治理视图。"

    evidence = f"{readiness_summary.rstrip('。')}；样本时效={freshness_label}。"
    operator_summary = f"{gate_label}；关键证据={evidence}；下一步={next_step_summary}"
    return {
        "gate_key": gate_key,
        "gate_label": gate_label,
        "gate_tone": gate_tone,
        "headline": headline,
        "summary": summary,
        "evidence": evidence,
        "action": next_step_summary,
        "source_hint": source_hint,
        "boundary_note": boundary_note,
        "operator_summary": operator_summary,
    }


def _build_group_enablement_model_snapshot(
    *,
    plugin_scope_snapshot: dict[str, Any] | None,
    plugin_scope_brief: dict[str, Any] | None,
) -> dict[str, Any]:
    snapshot = plugin_scope_snapshot or {}
    brief = plugin_scope_brief or {}
    stage_label = str(snapshot.get("stage_label") or brief.get("status_label") or "未纳入判断")
    stage_tone = str(snapshot.get("stage_tone") or brief.get("status_tone") or "muted")
    readiness_summary = str(snapshot.get("readiness_summary") or "前置条件仍待补齐。")
    primary_gap_label = str(brief.get("primary_gap_label") or "群级启停模型")
    next_step_summary = str(
        brief.get("next_step_summary")
        or snapshot.get("action")
        or "继续用当前摘要口径观察群资料样本，不进入真实群级启停控制。"
    )
    primary_key_dimensions = [
        {
            "key": "channel_id",
            "label": "渠道",
            "summary": "限定在单个 wechat_ipad 渠道内，避免跨渠道误复用。",
        },
        {
            "key": "chatroom_id_or_external_id",
            "label": "群",
            "summary": "优先使用本地 chatroom.id；若仍处于样本阶段，可回退 chatroom.external_id。",
        },
        {
            "key": "bot_plugin_id",
            "label": "bot 插件绑定",
            "summary": "以 bot_plugins 作为最小控制对象，表达 bot 内具体插件实例的启停。",
        },
        {
            "key": "plugin_projection",
            "label": "插件镜像",
            "summary": "plugins/plugin.code 仅用于展示与对账，不作为真实群级控制主键。",
        },
    ]
    relationship_scope_summary = (
        "channel -> chatroom -> bot_plugin 为最小控制链路；plugin/plugin_code 仅作镜像展示与草案对账。"
    )
    boundary_note = "当前仅定义群级启停模型口径摘要与扩展位，不代表真实群级启停控制、写入接口或权限系统已经上线。"
    operator_summary = (
        "最小模型已定义为草案口径；"
        "建议主键=channel_id + chatroom_id(或 external_id) + bot_plugin_id；"
        f"当前准备度={stage_label}；下一步={next_step_summary}"
    )
    return {
        "model_key": "group_enablement_model_snapshot",
        "model_label": "群级启停模型口径",
        "status_key": "draft_defined",
        "status_label": "已定义草案级口径",
        "status_tone": "outline" if stage_tone == "muted" else stage_tone,
        "is_defined": True,
        "definition_level": "draft",
        "definition_level_label": "草案级",
        "primary_gap_label": primary_gap_label,
        "recommended_primary_key": "channel_id + chatroom_id(或 chatroom_external_id) + bot_plugin_id",
        "primary_key_dimensions": primary_key_dimensions,
        "relationship_scope_summary": relationship_scope_summary,
        "current_stage_label": stage_label,
        "current_stage_summary": readiness_summary,
        "summary": "当前已把群级启停模型收口到只读草案：群定位看 channel/chatroom，插件定位看 bot_plugin，plugin 镜像只做展示。",
        "next_step": next_step_summary,
        "operator_summary": operator_summary,
        "boundary_note": boundary_note,
    }


def _build_group_allow_deny_model_snapshot(
    *,
    plugin_scope_snapshot: dict[str, Any] | None,
    plugin_scope_brief: dict[str, Any] | None,
) -> dict[str, Any]:
    snapshot = plugin_scope_snapshot or {}
    brief = plugin_scope_brief or {}
    stage_label = str(snapshot.get("stage_label") or brief.get("status_label") or "未纳入判断")
    stage_tone = str(snapshot.get("stage_tone") or brief.get("status_tone") or "muted")
    readiness_summary = str(snapshot.get("readiness_summary") or "前置条件仍待补齐。")
    next_step_summary = str(
        brief.get("next_step_summary")
        or snapshot.get("action")
        or "继续只读展示 allow/deny 草案摘要，不进入真实群级 allow/deny 判定执行。"
    )
    recommended_dimensions = [
        {
            "key": "channel_id",
            "label": "渠道",
            "summary": "限定在单个 wechat_ipad 渠道内，避免跨渠道复用未确认的口径。",
        },
        {
            "key": "chatroom_id_or_external_id",
            "label": "群",
            "summary": "优先使用本地 chatroom.id；样本阶段允许回退 chatroom.external_id 做草案对账。",
        },
        {
            "key": "bot_plugin_id",
            "label": "bot 插件绑定",
            "summary": "仍以 bot_plugins 作为最小控制对象，不把 plugin 镜像误当执行主键。",
        },
        {
            "key": "action_scope",
            "label": "动作范围",
            "summary": "只描述群级启停、消息消费或命令触发这类最小动作范围，不扩成完整能力矩阵。",
        },
        {
            "key": "policy_effect",
            "label": "策略效果",
            "summary": "草案仅区分 allow / deny 两类效果，用于摘要表达最小权限边界。",
        },
    ]
    relationship_scope_summary = (
        "channel -> chatroom -> bot_plugin 为主体链路，再补 action_scope + policy_effect 表达群级 allow/deny 草案边界。"
    )
    boundary_note = (
        "当前仅定义群级 allow/deny 口径摘要与扩展位，不代表真实群级 allow/deny 控制、写入接口、判定执行或权限系统已经上线。"
    )
    operator_summary = (
        "最小 allow/deny 口径已定义为草案摘要；"
        "建议维度=channel_id + chatroom_id(或 external_id) + bot_plugin_id + action_scope + policy_effect；"
        f"当前准备度={stage_label}；下一步={next_step_summary}"
    )
    return {
        "model_key": "group_allow_deny_model_snapshot",
        "model_label": "群级 allow/deny 口径",
        "status_key": "draft_defined",
        "status_label": "已定义草案级口径",
        "status_tone": "outline" if stage_tone == "muted" else stage_tone,
        "is_defined": True,
        "definition_level": "draft",
        "definition_level_label": "草案级",
        "is_live": False,
        "current_stage_label": stage_label,
        "current_stage_summary": readiness_summary,
        "summary": "当前已把群级 allow/deny 收口到只读草案摘要：按 channel/chatroom/bot_plugin 定位，再用 action_scope 与 policy_effect 表达最小权限边界。",
        "recommended_dimensions": recommended_dimensions,
        "recommended_dimension_keys": [item["key"] for item in recommended_dimensions],
        "relationship_scope_summary": relationship_scope_summary,
        "next_step": next_step_summary,
        "operator_summary": operator_summary,
        "boundary_note": boundary_note,
    }
