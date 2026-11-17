"""群总览和列表组装，只查询调用方租户会话内的本地快照。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import (
    BotPlugin,
    Channel,
    Chatroom,
    ChatroomMember,
    GroupPluginScope,
    Plugin,
)
from app.services.chatroom_registry import format_chatroom_management_status, management_tone
from app.services.plugin_scopes import binding_group_default_enabled
from app.services.wechat_ipad.common import _format_beijing_time
from app.services.wechat_ipad.views.group_governance import (
    _build_group_allow_deny_model_snapshot,
    _build_group_enablement_model_snapshot,
    _build_group_plugin_governance_snapshot,
)
from app.services.wechat_ipad.views.group_scopes import (
    _build_chatroom_plugin_scope_brief,
    _build_chatroom_plugin_scope_overview,
    _build_chatroom_plugin_scope_snapshot,
    _summarize_group_plugin_scope_rows,
)
from app.services.wechat_ipad.views.group_status import (
    _build_chatroom_coverage_snapshot,
    _build_chatroom_detail_sync_depth,
    _build_chatroom_operator_snapshot,
    _build_chatroom_ops_snapshot,
    _build_chatroom_stage_snapshot,
    _build_chatroom_summary_snapshot,
    _build_chatroom_sync_depth,
    _build_chatroom_sync_freshness,
    _build_chatroom_visibility_snapshot,
    _extract_chatroom_source,
    _format_chatroom_member_role,
    _format_chatroom_status,
)


def build_wechat_ipad_group_summary(db: Session, channel: Channel, *, now: datetime | None = None) -> dict[str, Any]:
    bot_plugin_count = int(
        db.scalar(select(func.count(BotPlugin.id)).where(BotPlugin.bot_id == channel.bot_id, BotPlugin.status != "unbound")) or 0
    )
    enabled_bot_plugin_count = int(
        db.scalar(
            select(func.count(BotPlugin.id)).where(
                BotPlugin.bot_id == channel.bot_id,
                BotPlugin.status == "enabled",
            )
        )
        or 0
    )
    plugin_registry_count = int(db.scalar(select(func.count(Plugin.id))) or 0)
    chatroom_count = db.scalar(select(func.count(Chatroom.id)).where(Chatroom.channel_id == channel.id)) or 0
    member_count = db.scalar(
        select(func.count(ChatroomMember.id))
        .join(Chatroom, ChatroomMember.chatroom_id == Chatroom.id)
        .where(Chatroom.channel_id == channel.id)
    ) or 0
    member_synced_chatroom_count = db.scalar(
        select(func.count(func.distinct(ChatroomMember.chatroom_id)))
        .join(Chatroom, ChatroomMember.chatroom_id == Chatroom.id)
        .where(Chatroom.channel_id == channel.id)
    ) or 0
    latest_member_synced_at = db.scalar(
        select(func.max(ChatroomMember.last_synced_at))
        .join(Chatroom, ChatroomMember.chatroom_id == Chatroom.id)
        .where(Chatroom.channel_id == channel.id)
    )
    latest_chatroom_synced_at = db.scalar(select(func.max(Chatroom.last_synced_at)).where(Chatroom.channel_id == channel.id))
    status_counts_raw = db.execute(
        select(Chatroom.status, func.count(Chatroom.id))
        .where(Chatroom.channel_id == channel.id)
        .group_by(Chatroom.status)
    ).all()
    status_counts = {str(status or "unknown"): int(count or 0) for status, count in status_counts_raw}
    latest_chatroom = db.scalars(
        select(Chatroom).where(Chatroom.channel_id == channel.id).order_by(Chatroom.updated_at.desc(), Chatroom.id.desc()).limit(1)
    ).first()
    recent_chatrooms = db.scalars(
        select(Chatroom).where(Chatroom.channel_id == channel.id).order_by(Chatroom.updated_at.desc(), Chatroom.id.desc()).limit(5)
    ).all()
    scope_rows = db.scalars(
        select(GroupPluginScope).where(GroupPluginScope.channel_id == channel.id).order_by(GroupPluginScope.id.asc())
    ).all()
    latest_source = _extract_chatroom_source(latest_chatroom)
    scope_stats = _summarize_group_plugin_scope_rows(scope_rows)
    recent_items = [
        {
            "id": chatroom.id,
            "name": chatroom.name or chatroom.external_id,
            "external_id": chatroom.external_id,
            "status": chatroom.status or "unknown",
            "status_label": _format_chatroom_status(chatroom.status),
            "member_count": int(chatroom.member_count or 0),
            "last_synced_at": _format_beijing_time(chatroom.last_synced_at),
            "observed_source": _extract_chatroom_source(chatroom),
            "is_message_observed_only": str(chatroom.status or "") == "message_observed",
            "scope_total": scope_stats["per_chatroom"].get(chatroom.id, {}).get("total", 0),
            "scope_overridden": scope_stats["per_chatroom"].get(chatroom.id, {}).get("overridden", 0),
        }
        for chatroom in recent_chatrooms
    ]
    message_observed_count = int(status_counts.get("message_observed", 0))
    active_chatroom_count = max(int(chatroom_count) - message_observed_count, 0)
    has_data = int(chatroom_count) > 0
    sync_depth = _build_chatroom_sync_depth(
        chatroom_count=int(chatroom_count),
        active_chatroom_count=active_chatroom_count,
        member_synced_chatroom_count=int(member_synced_chatroom_count),
    )
    visibility_snapshot = _build_chatroom_visibility_snapshot(
        chatroom_count=int(chatroom_count),
        active_chatroom_count=active_chatroom_count,
        message_observed_count=message_observed_count,
        member_synced_chatroom_count=int(member_synced_chatroom_count),
        recent_items=recent_items,
        sync_depth=sync_depth,
    )
    coverage_snapshot = _build_chatroom_coverage_snapshot(
        chatroom_count=int(chatroom_count),
        active_chatroom_count=active_chatroom_count,
        message_observed_count=message_observed_count,
        member_synced_chatroom_count=int(member_synced_chatroom_count),
        latest_synced_at=_format_beijing_time(latest_chatroom_synced_at),
        latest_member_synced_at=_format_beijing_time(latest_member_synced_at),
        latest_source=latest_source,
        sync_freshness=_build_chatroom_sync_freshness(latest_chatroom_synced_at, now=now),
    )
    operator_snapshot = _build_chatroom_operator_snapshot(
        has_data=has_data,
        message_observed_count=message_observed_count,
        sync_depth=sync_depth,
        visibility_snapshot=visibility_snapshot,
    )
    ops_snapshot = _build_chatroom_ops_snapshot(
        chatroom_count=int(chatroom_count),
        active_chatroom_count=active_chatroom_count,
        message_observed_count=message_observed_count,
        member_synced_chatroom_count=int(member_synced_chatroom_count),
        sync_depth=sync_depth,
        latest_source=latest_source,
        sync_freshness=coverage_snapshot["sync_freshness"],
    )
    plugin_scope_snapshot = _build_chatroom_plugin_scope_snapshot(
        chatroom_count=int(chatroom_count),
        active_chatroom_count=active_chatroom_count,
        message_observed_count=message_observed_count,
        member_synced_chatroom_count=int(member_synced_chatroom_count),
        bot_plugin_count=bot_plugin_count,
        enabled_bot_plugin_count=enabled_bot_plugin_count,
        plugin_registry_count=plugin_registry_count,
        latest_source=latest_source,
        sync_freshness=coverage_snapshot["sync_freshness"],
        persisted_scope_stats=scope_stats,
    )
    plugin_scope_brief = _build_chatroom_plugin_scope_brief(plugin_scope_snapshot=plugin_scope_snapshot)
    plugin_scope_overview = _build_chatroom_plugin_scope_overview(
        plugin_scope_snapshot=plugin_scope_snapshot,
        plugin_scope_brief=plugin_scope_brief,
    )
    group_plugin_governance_snapshot = _build_group_plugin_governance_snapshot(
        plugin_scope_snapshot=plugin_scope_snapshot,
        plugin_scope_brief=plugin_scope_brief,
        plugin_scope_overview=plugin_scope_overview,
    )
    group_enablement_model_snapshot = _build_group_enablement_model_snapshot(
        plugin_scope_snapshot=plugin_scope_snapshot,
        plugin_scope_brief=plugin_scope_brief,
    )
    group_allow_deny_model_snapshot = _build_group_allow_deny_model_snapshot(
        plugin_scope_snapshot=plugin_scope_snapshot,
        plugin_scope_brief=plugin_scope_brief,
    )
    stage_snapshot = _build_chatroom_stage_snapshot(
        sync_depth=sync_depth,
        ops_snapshot=ops_snapshot,
        sync_freshness=coverage_snapshot["sync_freshness"],
    )
    summary_snapshot = _build_chatroom_summary_snapshot(
        sync_depth=sync_depth,
        ops_snapshot=ops_snapshot,
        sync_freshness=coverage_snapshot["sync_freshness"],
    )
    return {
        "chatroom_count": int(chatroom_count),
        "member_count": int(member_count),
        "member_synced_chatroom_count": int(member_synced_chatroom_count),
        "latest_chatroom_name": (latest_chatroom.name or latest_chatroom.external_id) if latest_chatroom else "-",
        "latest_synced_at": _format_beijing_time(latest_chatroom_synced_at),
        "latest_member_synced_at": _format_beijing_time(latest_member_synced_at),
        "latest_observed_source": latest_source,
        "message_observed_count": message_observed_count,
        "active_chatroom_count": active_chatroom_count,
        "status_breakdown": status_counts,
        "recent_chatrooms": recent_items,
        "sync_depth": sync_depth,
        "visibility_snapshot": visibility_snapshot,
        "coverage_snapshot": coverage_snapshot,
        "operator_snapshot": operator_snapshot,
        "ops_snapshot": ops_snapshot,
        "stage_snapshot": stage_snapshot,
        "summary_snapshot": summary_snapshot,
        "plugin_scope_snapshot": plugin_scope_snapshot,
        "plugin_scope_brief": plugin_scope_brief,
        "plugin_scope_overview": plugin_scope_overview,
        "group_plugin_governance_snapshot": group_plugin_governance_snapshot,
        "group_enablement_model_snapshot": group_enablement_model_snapshot,
        "group_allow_deny_model_snapshot": group_allow_deny_model_snapshot,
        "persisted_scope_stats": scope_stats,
        "has_chatroom_data": has_data,
        "data_model_ready": True,
        "empty_state_message": (
            "尚未观测到群资料；本地 chatrooms / chatroom_members 实体骨架已就位，等待后续同步或消息驱动落库。"
            if not has_data
            else "已能在后台看到本地群资料摘要，可用于核对最近落库与同步进度。"
        ),
        "sync_visibility_note": (
            "当前大部分仍可能只是 message_observed 骨架，表示后台已观测到群消息并开始落库。"
            if message_observed_count
            else "当前摘要未发现 message_observed 骨架，可继续核对是否已有更完整群资料来源。"
        ),
        "capability_note": "当前仅为本地落库扩展位，不代表已完成真实群资料/群成员同步。",
    }


def build_wechat_ipad_chatroom_list(db: Session, channel: Channel) -> dict[str, Any]:
    chatrooms = db.scalars(
        select(Chatroom)
        .where(Chatroom.channel_id == channel.id)
        .order_by(Chatroom.last_synced_at.desc(), Chatroom.id.desc())
    ).all()
    bindings = db.scalars(
        select(BotPlugin)
        .where(BotPlugin.bot_id == channel.bot_id, BotPlugin.status != "unbound")
        .order_by(BotPlugin.id.asc())
    ).all()
    member_count_rows = db.execute(
        select(ChatroomMember.chatroom_id, func.count(ChatroomMember.id))
        .where(ChatroomMember.channel_id == channel.id)
        .group_by(ChatroomMember.chatroom_id)
    ).all()
    role_rows = db.execute(
        select(ChatroomMember.chatroom_id, ChatroomMember.role, func.count(ChatroomMember.id))
        .where(ChatroomMember.channel_id == channel.id)
        .group_by(ChatroomMember.chatroom_id, ChatroomMember.role)
    ).all()
    scope_rows = db.scalars(
        select(GroupPluginScope)
        .where(GroupPluginScope.channel_id == channel.id)
        .order_by(GroupPluginScope.chatroom_id.asc(), GroupPluginScope.bot_plugin_id.asc())
    ).all()

    binding_enabled_map = {
        binding.id: str(binding.status or "").strip() == "enabled"
        for binding in bindings
    }
    member_count_map = {int(chatroom_id): int(count or 0) for chatroom_id, count in member_count_rows}
    role_count_map: dict[int, dict[str, int]] = {}
    for chatroom_id, role, count in role_rows:
        chatroom_role_counts = role_count_map.setdefault(int(chatroom_id), {})
        normalized_role = str(role or "").strip() or "member"
        chatroom_role_counts[normalized_role] = int(count or 0)

    scope_map_by_chatroom: dict[int, dict[int, GroupPluginScope]] = {}
    for scope in scope_rows:
        scope_map_by_chatroom.setdefault(int(scope.chatroom_id), {})[int(scope.bot_plugin_id)] = scope

    items: list[dict[str, Any]] = []
    inherited_chatroom_count = 0
    overridden_chatroom_count = 0
    member_snapshot_chatroom_count = 0
    for chatroom in chatrooms:
        member_record_count = int(member_count_map.get(chatroom.id, 0))
        if member_record_count > 0:
            member_snapshot_chatroom_count += 1
        role_counts = role_count_map.get(chatroom.id, {})
        scope_map = scope_map_by_chatroom.get(chatroom.id, {})
        overridden_scope_count = 0
        effective_enabled_count = 0
        for binding in bindings:
            scope = scope_map.get(binding.id)
            if scope and str(scope.scope_status or "").strip() != "inherited":
                overridden_scope_count += 1
            if scope is not None:
                if bool(scope.enabled):
                    effective_enabled_count += 1
            elif binding_group_default_enabled(binding):
                effective_enabled_count += 1
        effective_disabled_count = max(len(bindings) - effective_enabled_count, 0)
        if overridden_scope_count > 0:
            overridden_chatroom_count += 1
        else:
            inherited_chatroom_count += 1
        role_summary = " / ".join(
            f"{_format_chatroom_member_role(role)} {count}"
            for role, count in sorted(role_counts.items(), key=lambda item: item[0])
        ) or "-"
        items.append(
            {
                "id": chatroom.id,
                "name": chatroom.name or chatroom.external_id,
                "external_id": chatroom.external_id,
                "remark": chatroom.remark or "-",
                "owner_external_id": chatroom.owner_external_id or "-",
                "status": chatroom.status or "unknown",
                "status_label": _format_chatroom_status(chatroom.status),
                "management_status": chatroom.management_status or "discovered",
                "management_status_label": format_chatroom_management_status(chatroom.management_status),
                "management_status_tone": management_tone(chatroom.management_status),
                "is_active": bool(chatroom.is_active),
                "sync_depth_label": _build_chatroom_detail_sync_depth(
                    chatroom=chatroom,
                    member_items=[{}] * member_record_count,
                ),
                "member_count": int(chatroom.member_count or 0),
                "member_record_count": member_record_count,
                "member_role_summary": role_summary,
                "scope_total": len(bindings),
                "scope_inherited": max(len(bindings) - overridden_scope_count, 0),
                "scope_overridden": overridden_scope_count,
                "scope_effective_enabled": effective_enabled_count,
                "scope_effective_disabled": effective_disabled_count,
                "last_synced_at": _format_beijing_time(chatroom.last_synced_at),
                "observed_source": _extract_chatroom_source(chatroom),
            }
        )

    return {
        "items": items,
        "total": len(items),
        "binding_total": len(bindings),
        "binding_enabled_total": sum(1 for enabled in binding_enabled_map.values() if enabled),
        "member_snapshot_chatroom_count": member_snapshot_chatroom_count,
        "pending_chatroom_count": sum(1 for item in items if item["management_status"] == "discovered"),
        "active_chatroom_count": sum(1 for item in items if item["is_active"]),
        "disabled_chatroom_count": sum(1 for item in items if item["management_status"] == "disabled"),
        "inherited_chatroom_count": inherited_chatroom_count,
        "overridden_chatroom_count": overridden_chatroom_count,
    }
