"""群详情的查询与作用域准备边界。

缺少群作用域时沿用现有行为，从机器人绑定物化继承记录并 flush；不提交事务。
该入口有数据库副作用，因此独立于只读 views。调用方继续负责会话生命周期，
具体状态和配置展示复用 views 的纯函数，禁止在这里触发设备协议同步。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, GroupPluginScope
from app.services.chatroom_invitations import member_invitation
from app.services.chatroom_profile import effective_member_role, sort_chatroom_members
from app.services.chatroom_registry import format_chatroom_management_status, management_tone
from app.services.plugin_schemas import dump_config_json, parse_config_json
from app.services.plugin_scopes import group_plugin_scope_enabled
from app.services.wechat_ipad.chatrooms import materialize_group_plugin_scopes_for_chatroom
from app.services.wechat_ipad.common import _format_beijing_time, _parse_json
from app.services.wechat_ipad.views.group_scopes import (
    _build_effective_group_config,
    _format_config_for_editor,
)
from app.services.wechat_ipad.views.group_status import (
    _build_chatroom_detail_sync_depth,
    _extract_chatroom_source,
    _format_chatroom_member_flag,
    _format_chatroom_member_role,
    _format_chatroom_member_status,
    _format_chatroom_status,
    _format_group_scope_status,
)


def build_wechat_ipad_chatroom_detail(db: Session, channel: Channel, chatroom: Chatroom) -> dict[str, Any]:
    # 与其他群详情保持相同顺序，不能按角色英文字母或昵称把管理员排在群主前面。
    members = sort_chatroom_members(
        db.scalars(select(ChatroomMember).where(ChatroomMember.chatroom_id == chatroom.id)).all(),
        owner_external_id=chatroom.owner_external_id,
    )
    bindings = db.scalars(
        select(BotPlugin)
        .where(BotPlugin.bot_id == channel.bot_id, BotPlugin.status != "unbound")
        .order_by(BotPlugin.id.asc())
    ).all()
    scope_rows = db.scalars(
        select(GroupPluginScope)
        .where(
            GroupPluginScope.channel_id == channel.id,
            GroupPluginScope.chatroom_id == chatroom.id,
        )
        .order_by(GroupPluginScope.id.asc())
    ).all()
    if not scope_rows and bindings:
        scope_rows = materialize_group_plugin_scopes_for_chatroom(db, channel=channel, chatroom=chatroom, source="chatroom_detail_view")

    role_counts: dict[str, int] = {}
    member_items: list[dict[str, Any]] = []
    for member in members:
        role_key = effective_member_role(member, chatroom.owner_external_id)
        role_label = _format_chatroom_member_role(role_key)
        role_counts[role_label] = role_counts.get(role_label, 0) + 1
        member_extra = _parse_json(member.extra_json)
        province = str(member_extra.get("province") or "").strip()
        city = str(member_extra.get("city") or "").strip()
        member_flag = str(member_extra.get("member_flag") or "").strip()
        alias = str(member_extra.get("alias") or "").strip() or "-"
        remark = str(member_extra.get("remark") or "").strip() or "-"
        avatar_url = str(member_extra.get("small_head_img_url") or "").strip()
        region_parts = [item for item in (province, city) if item]
        member_items.append(
            {
                "id": member.id,
                "member_external_id": member.member_external_id,
                "nickname": member.nickname or "-",
                "display_name": member.display_name or "-",
                "role": role_key,
                "role_label": role_label,
                "status": member.status or "active",
                "status_label": _format_chatroom_member_status(member.status),
                "member_flag": member_flag or "-",
                "member_flag_label": _format_chatroom_member_flag(member_flag),
                "alias": alias,
                "remark": remark,
                "invitation": member_invitation(member),
                "province": province or "-",
                "city": city or "-",
                "region_label": " / ".join(region_parts) if region_parts else "-",
                "avatar_url": avatar_url,
                "has_avatar": bool(avatar_url),
                "last_active_at": _format_beijing_time(member.last_active_at),
                "last_synced_at": _format_beijing_time(member.last_synced_at),
            }
        )

    binding_items: list[dict[str, Any]] = []
    enabled_binding_count = 0
    inherited_scope_count = 0
    overridden_scope_count = 0
    effective_enabled_count = 0
    scope_map = {item.bot_plugin_id: item for item in scope_rows}
    for binding in bindings:
        plugin = binding.plugin
        binding_enabled = str(binding.status or "").strip() == "enabled"
        if binding_enabled:
            enabled_binding_count += 1
        scope = scope_map.get(binding.id)
        scope_status = str(getattr(scope, "scope_status", "") or "inherited")
        scope_enabled = group_plugin_scope_enabled(binding, scope)
        if scope_status == "inherited":
            inherited_scope_count += 1
        else:
            overridden_scope_count += 1
        if scope_enabled:
            effective_enabled_count += 1
        scope_action = str(getattr(scope, "action_scope", "") or "all")
        scope_effect = str(getattr(scope, "policy_effect", "") or "")
        binding_config = parse_config_json(binding.config_json)
        scope_config = parse_config_json(getattr(scope, "config_override_json", None))
        effective_config = _build_effective_group_config(
            plugin_code=plugin.code if plugin else None,
            binding_config=binding_config,
            scope_config=scope_config,
        )
        binding_items.append(
            {
                "binding_id": binding.id,
                "plugin_code": plugin.code if plugin else "-",
                "plugin_name": plugin.display_name if plugin else f"Plugin #{binding.plugin_id}",
                "binding_status": binding.status or "unknown",
                "binding_status_label": "已启用" if binding_enabled else "已停用",
                "binding_status_tone": "success" if binding_enabled else "muted",
                "runtime_status": binding.runtime_status or "idle",
                "last_execution_status": binding.last_execution_status or "-",
                "last_execution_summary": binding.last_execution_summary or "-",
                "last_execution_at": _format_beijing_time(binding.last_execution_at),
                "group_scope_status_label": _format_group_scope_status(scope_status),
                "group_scope_status_tone": "outline" if scope_status == "inherited" else "success",
                "group_scope_enabled_label": "启用" if scope_enabled else "停用",
                "group_scope_action_scope": scope_action,
                "group_scope_effect": scope_effect or "-",
                "group_scope_status": scope_status,
                "group_scope_enabled": scope_enabled,
                "group_scope_mode": "inherited" if scope_status == "inherited" else ("enabled" if scope_enabled else "disabled"),
                "group_scope_summary": (
                    "当前没有群级覆盖配置；该群直接继承 bot_plugin 的启停与配置。"
                    if scope_status == "inherited"
                    else "当前已存在群级作用域覆盖记录；该群会优先使用单群启停和配置覆盖。"
                ),
                "group_scope_boundary_note": "未填写群级配置时会继续继承 bot_plugin 默认配置；填写后仅覆盖当前群。",
                "group_scope_can_enable": not scope_enabled,
                "group_scope_can_disable": scope_enabled,
                "group_scope_can_reset": scope_status != "inherited",
                "group_scope_has_config_override": bool(getattr(scope, "config_override_json", None)),
                "group_scope_config_override_json": str(getattr(scope, "config_override_json", "") or "").strip(),
                "group_scope_effective_config_json": _format_config_for_editor(effective_config),
                "group_scope_effective_config_has_values": bool(effective_config),
                "binding_config_json": dump_config_json(binding_config) or "",
            }
        )

    source = _extract_chatroom_source(chatroom)
    return {
        "chatroom_id": chatroom.id,
        "chatroom_external_id": chatroom.external_id,
        "name": chatroom.name or chatroom.external_id,
        "remark": chatroom.remark or "-",
        "owner_external_id": chatroom.owner_external_id or "-",
        "status": chatroom.status or "unknown",
        "status_label": _format_chatroom_status(chatroom.status),
        "management_status": chatroom.management_status or "discovered",
        "management_status_label": format_chatroom_management_status(chatroom.management_status),
        "management_status_tone": management_tone(chatroom.management_status),
        "is_active": bool(chatroom.is_active),
        "member_count": int(chatroom.member_count or 0),
        "member_record_count": len(member_items),
        "last_synced_at": _format_beijing_time(chatroom.last_synced_at),
        "observed_source": source,
        "sync_depth_label": _build_chatroom_detail_sync_depth(chatroom=chatroom, member_items=member_items),
        "role_counts": role_counts,
        "members": member_items,
        "bindings": binding_items,
        "binding_summary": {
            "total": len(binding_items),
            "enabled": enabled_binding_count,
            "disabled": max(len(binding_items) - enabled_binding_count, 0),
            "materialized_scope_count": len(scope_rows),
            "inherited_scope_count": inherited_scope_count,
            "overridden_scope_count": overridden_scope_count,
            "effective_enabled_count": effective_enabled_count,
            "effective_disabled_count": max(len(binding_items) - effective_enabled_count, 0),
            "operator_summary": (
                f"当前 bot 共绑定 {len(binding_items)} 个插件，bot 级启用 {enabled_binding_count} 个；"
                f"已落库 {len(scope_rows)} 条群级作用域记录，继承 {inherited_scope_count} / 覆盖 {overridden_scope_count}；"
                f"当前有效启用 {effective_enabled_count} / 停用 {max(len(binding_items) - effective_enabled_count, 0)}，仍属于本地草稿控制。"
            ),
        },
    }
