from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.services.plugin_scopes import binding_group_default_enabled
from app.services.chatroom_directory import get_or_create_chatroom, get_or_create_member
from app.services.chatroom_invitations import save_member_invitation
from app.services.plugin_schemas import parse_config_json
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, GroupPluginScope
from app.services.chatroom_registry import mark_chatroom_discovered
from app.services.wechat_ipad.cache import remember_chatroom, remember_chatroom_members, remember_member
from app.services.wechat_ipad.common import _resolve_wxid


def sync_wechat_ipad_chatroom_from_message(
    db: Session,
    channel: Channel,
    normalized: dict[str, Any],
    *,
    conversation: Conversation | None = None,
) -> dict[str, bool | str]:
    conversation_payload = normalized.get("conversation") or {}
    message_payload = normalized.get("message") or {}
    details = normalized.get("details") or {}

    if str(conversation_payload.get("chat_type") or "") != "group":
        return {"chatroom_created": False, "member_created": False, "chatroom_id": "", "member_external_id": ""}

    chatroom_external_id = str(conversation_payload.get("external_id") or "").strip()
    if not chatroom_external_id or not chatroom_external_id.endswith("@chatroom"):
        return {"chatroom_created": False, "member_created": False, "chatroom_id": "", "member_external_id": ""}

    event_at = _resolve_synced_at(details.get("timestamp"))
    synced_at = event_at or now_shanghai()
    chatroom, chatroom_created = get_or_create_chatroom(db, channel, chatroom_external_id, status="message_observed")
    remember_chatroom(channel.id, chatroom_external_id)
    mark_chatroom_discovered(
        db,
        channel=channel,
        chatroom=chatroom,
        seen_at=_latest(chatroom.last_seen_at, synced_at),
        registration_source="passive",
        enable_management=chatroom_created,
    )

    inferred_name = _infer_chatroom_name(conversation_payload, conversation, chatroom_external_id)
    if inferred_name and (not chatroom.name or chatroom.name == chatroom_external_id):
        chatroom.name = inferred_name
    extra = parse_config_json(chatroom.extra_json)
    if not chatroom.last_synced_at or synced_at >= ensure_shanghai_datetime(chatroom.last_synced_at):
        chatroom.last_synced_at = synced_at
        chatroom.extra_json = json.dumps(
            {
                **extra,
                "source": "wechat_ipad_message_ingest",
                "last_event_key": normalized.get("event_key"),
                "last_sender_id": message_payload.get("sender_id"),
                "last_sender_name": message_payload.get("sender_name"),
                "last_message_type": message_payload.get("message_type"),
                "is_group": True,
            },
            ensure_ascii=False, sort_keys=True,
        )

    member_external_id = _person_id(message_payload.get("sender_id"))
    if (message_payload.get("message_type") == "system" and details.get("msg_type") == 10000
            and details.get("direction") == "in" and details.get("from_user_name") == chatroom_external_id
            and details.get("to_user_name") and details["to_user_name"] == _resolve_wxid(
                channel, parse_config_json(channel.runtime_info_json), parse_config_json(channel.adapter_config_json))
            and normalized.get("event_key") and event_at and event_at.timestamp() > extra.get("ipad_roster_at", 0)):
        _apply_system_membership(db, channel, chatroom, normalized, event_at)
    # 系统通知只触发目录刷新；快照以前的迟到发言也不构成重新入群的证据。
    if (not member_external_id or message_payload.get("message_type") in {"system", "event"}
            or details.get("msg_type") in (10000, 10002)
            or "ipad_roster_at" in extra and (not event_at or synced_at.timestamp() <= extra["ipad_roster_at"])):
        return {
            "chatroom_created": chatroom_created,
            "member_created": False,
            "chatroom_id": chatroom_external_id,
            "member_external_id": "",
        }

    member, member_created = get_or_create_member(db, channel, chatroom, member_external_id)
    if (member.last_synced_at and synced_at <= ensure_shanghai_datetime(member.last_synced_at)
            or member.status == "left" and not event_at):
        return {"chatroom_created": chatroom_created, "member_created": member_created,
                "chatroom_id": chatroom_external_id, "member_external_id": member_external_id}

    observed_name = _resolve_member_nickname(message_payload, member_external_id)
    if (not member.nickname or member.nickname == member_external_id
            or "ipad_roster_at" not in extra and observed_name != member_external_id):
        member.nickname = observed_name
    member.status = "active"
    member.last_synced_at = synced_at
    member.last_active_at = synced_at
    member.extra_json = json.dumps(
        {
            # 发言观测必须保留已同步的成员资料，尤其不能清空邀请关系。
            **parse_config_json(member.extra_json),
            "source": "wechat_ipad_message_ingest",
            "last_event_key": normalized.get("event_key"),
            "observed_via": "group_message",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    remember_member(channel.id, chatroom_external_id, member_external_id)
    materialize_group_plugin_scopes_for_chatroom(
        db,
        channel=channel,
        chatroom=chatroom,
        source="wechat_ipad_message_ingest",
    )
    db.flush()
    return {
        "chatroom_created": chatroom_created,
        "member_created": member_created,
        "chatroom_id": chatroom_external_id,
        "member_external_id": member_external_id,
    }


def _apply_system_membership(db: Session, channel: Channel, room: Chatroom, event: dict, at: datetime) -> None:
    """群锁内按成员核对实际变化。新旧成员混合的通知只向业务层提供发生变化的人。"""
    from app.services.group_features.member_change import parse_wechat_member_template

    parsed = parse_wechat_member_template(str(event["message"].get("content") or ""), exact=True)
    if not parsed:
        return
    desired = "active" if parsed["change_type"] == "join" else "left"
    changed = []
    for item in parsed["members"]:
        person = _person_id(item["external_id"])
        if not person:
            continue
        member, created = get_or_create_member(db, channel, room, person)
        if member.last_synced_at and ensure_shanghai_datetime(member.last_synced_at) >= at:
            continue
        previous = member.status
        member.last_synced_at = at
        if not created and previous == desired:
            continue
        member.status = desired
        member.nickname = member.nickname or item["display_name"][:255]
        member.extra_json = json.dumps({**parse_config_json(member.extra_json),
                                        "ipad_membership_event": event["event_key"]}, ensure_ascii=False)
        if desired == "active":
            room.member_count = int(room.member_count or 0) + 1
        elif not created and previous == "active":
            room.member_count = max(0, int(room.member_count or 0) - 1)
        changed.append(item)
    if changed:
        event["details"]["member_change"] = {**parsed, "members": changed}
        event["details"]["member_directory"] = {"applied": True}
    db.flush()


def upsert_wechat_ipad_chatroom_profile(
    db: Session,
    channel: Channel,
    *,
    chatroom_external_id: str,
    profile_payload: dict[str, Any] | None = None,
    member_payload: dict[str, Any] | None = None,
    synced_at: datetime | None = None,
    source: str = "wechat_ipad_chatroom_sync",
) -> tuple[str, str]:
    normalized_chatroom_id = str(chatroom_external_id or "").strip()
    if not normalized_chatroom_id.endswith("@chatroom"):
        return "invalid_target", "invalid_target"

    profile = _normalize_chatroom_profile(profile_payload or {}, normalized_chatroom_id)
    snapshot = _normalize_chatroom_members(member_payload or {}, normalized_chatroom_id)
    if not profile and not snapshot:
        return "invalid_profile", "invalid_roster"
    resolved_synced_at = ensure_shanghai_datetime(synced_at)
    request_at = resolved_synced_at.timestamp()
    chatroom, chatroom_created = get_or_create_chatroom(db, channel, normalized_chatroom_id, status="pending_sync")
    extra = parse_config_json(chatroom.extra_json)
    profile_state = "invalid_profile"
    if profile:
        if request_at <= extra.get("ipad_profile_at", -1):
            profile_state = "stale_profile"
        else:
            for field, value in profile.items():
                if value:
                    setattr(chatroom, field, value)
            extra["ipad_profile_at"] = request_at
            profile_state = "applied"
            if chatroom.status != "members_synced":
                chatroom.status = "profile_synced"

    roster_state = "invalid_roster"
    if snapshot:
        version, members = snapshot
        # 群锁内比较版本和发起时间，不能让网络完成顺序决定名册状态。
        if request_at < extra.get("ipad_roster_at", -1) or version < extra.get("ipad_roster_version", -1):
            roster_state = "stale_roster"
        elif version == extra.get("ipad_roster_version"):
            roster_state = "unchanged"
        else:
            _replace_chatroom_members(db, chatroom=chatroom, channel=channel, members=members,
                                      synced_at=resolved_synced_at, source=source)
            chatroom.member_count = len(members)
            chatroom.status = "members_synced"
            extra.update(ipad_roster_at=request_at, ipad_roster_version=version)
            roster_state = "applied"
    if "applied" not in (profile_state, roster_state):
        return profile_state, roster_state

    remember_chatroom(channel.id, normalized_chatroom_id)
    mark_chatroom_discovered(
        db,
        channel=channel,
        chatroom=chatroom,
        seen_at=_latest(chatroom.last_seen_at, resolved_synced_at),
        registration_source="passive",
        enable_management=chatroom_created,
    )

    chatroom.last_synced_at = _latest(chatroom.last_synced_at, resolved_synced_at)
    extra.pop("profile_payload", None)
    chatroom.extra_json = json.dumps({**extra, "source": source}, ensure_ascii=False, sort_keys=True)
    materialize_group_plugin_scopes_for_chatroom(
        db,
        channel=channel,
        chatroom=chatroom,
        source=source,
    )
    db.flush()
    return profile_state, roster_state


def materialize_group_plugin_scopes_for_chatroom(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    source: str = "group_scope_materialized",
) -> list[GroupPluginScope]:
    bindings = db.scalars(
        select(BotPlugin).where(BotPlugin.bot_id == channel.bot_id, BotPlugin.status != "unbound").order_by(BotPlugin.id.asc())
    ).all()
    existing_scopes = db.scalars(
        select(GroupPluginScope).where(
            GroupPluginScope.channel_id == channel.id,
            GroupPluginScope.chatroom_id == chatroom.id,
        )
    ).all()
    existing_map = {item.bot_plugin_id: item for item in existing_scopes}
    scopes: list[GroupPluginScope] = []
    for binding in bindings:
        scope = existing_map.get(binding.id)
        if not scope:
            scope = GroupPluginScope(
                channel_id=channel.id,
                chatroom_id=chatroom.id,
                bot_plugin_id=binding.id,
            )
            db.add(scope)
            scope.scope_status = "inherited"
            scope.enabled = binding_group_default_enabled(binding)
            scope.action_scope = "all"
            scope.audit_note = "auto materialized from bot_plugin binding"
            scope.extra_json = json.dumps(
                {
                    "source": source,
                    "binding_status": binding.status,
                    "runtime_status": binding.runtime_status,
                    "materialized": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        scopes.append(scope)
    db.flush()
    return scopes


def update_group_plugin_scope_enabled(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    enabled: bool,
    operator_id: int | None = None,
    audit_note: str | None = None,
    source: str = "group_scope_manual_toggle",
) -> GroupPluginScope:
    binding = db.get(BotPlugin, bot_plugin_id)
    if not binding or binding.bot_id != channel.bot_id or binding.status == "unbound":
        raise ValueError("bot_plugin binding not found for channel bot")

    scope = _ensure_group_plugin_scope(
        db,
        channel=channel,
        chatroom=chatroom,
        binding=binding,
        source=source,
    )
    previous_enabled = bool(scope.enabled)
    previous_status = str(scope.scope_status or "inherited")
    scope.scope_status = "overridden"
    scope.enabled = bool(enabled)
    scope.action_scope = scope.action_scope or "all"
    scope.last_operator_id = operator_id
    scope.last_operated_at = now_shanghai()
    scope.audit_note = audit_note or (
        "manual group scope enabled" if enabled else "manual group scope disabled"
    )
    scope.extra_json = json.dumps(
        {
            "source": source,
            "binding_status": binding.status,
            "runtime_status": binding.runtime_status,
            "previous_status": previous_status,
            "previous_enabled": previous_enabled,
            "current_enabled": bool(enabled),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    return scope


def reset_group_plugin_scope_to_inherited(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    operator_id: int | None = None,
    audit_note: str | None = None,
    source: str = "group_scope_reset_to_inherited",
) -> GroupPluginScope:
    binding = db.get(BotPlugin, bot_plugin_id)
    if not binding or binding.bot_id != channel.bot_id or binding.status == "unbound":
        raise ValueError("bot_plugin binding not found for channel bot")

    scope = _ensure_group_plugin_scope(
        db,
        channel=channel,
        chatroom=chatroom,
        binding=binding,
        source=source,
    )
    scope.scope_status = "inherited"
    scope.enabled = binding_group_default_enabled(binding)
    scope.action_scope = scope.action_scope or "all"
    scope.policy_effect = None
    scope.config_override_json = None
    scope.last_operator_id = operator_id
    scope.last_operated_at = now_shanghai()
    scope.audit_note = audit_note or "reset group scope to inherited bot_plugin binding"
    scope.extra_json = json.dumps(
        {
            "source": source,
            "binding_status": binding.status,
            "runtime_status": binding.runtime_status,
            "current_enabled": scope.enabled,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    return scope


def update_group_plugin_scope_config_override(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    config_override_json: str | None,
    operator_id: int | None = None,
    audit_note: str | None = None,
    source: str = "group_scope_config_override",
) -> GroupPluginScope:
    binding = db.get(BotPlugin, bot_plugin_id)
    if not binding or binding.bot_id != channel.bot_id or binding.status == "unbound":
        raise ValueError("bot_plugin binding not found for channel bot")

    scope = _ensure_group_plugin_scope(
        db,
        channel=channel,
        chatroom=chatroom,
        binding=binding,
        source=source,
    )
    previous_status = str(scope.scope_status or "inherited")
    normalized_override = str(config_override_json or "").strip() or None
    binding_enabled = binding_group_default_enabled(binding)
    has_enable_override = bool(scope.enabled) != binding_enabled
    scope.scope_status = "overridden" if normalized_override is not None or has_enable_override else "inherited"
    if previous_status == "inherited":
        scope.enabled = binding_enabled
    scope.action_scope = scope.action_scope or "all"
    scope.config_override_json = normalized_override
    scope.last_operator_id = operator_id
    scope.last_operated_at = now_shanghai()
    scope.audit_note = audit_note or ("updated group config override" if normalized_override else "cleared group config override")
    scope.extra_json = json.dumps(
        {
            "source": source,
            "binding_status": binding.status,
            "runtime_status": binding.runtime_status,
            "previous_status": previous_status,
            "has_config_override": normalized_override is not None,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    return scope


def _ensure_group_plugin_scope(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    binding: BotPlugin,
    source: str,
) -> GroupPluginScope:
    scope = db.scalar(
        select(GroupPluginScope).where(
            GroupPluginScope.channel_id == channel.id,
            GroupPluginScope.chatroom_id == chatroom.id,
            GroupPluginScope.bot_plugin_id == binding.id,
        )
    )
    if scope:
        return scope

    materialized = materialize_group_plugin_scopes_for_chatroom(
        db,
        channel=channel,
        chatroom=chatroom,
        source=source,
    )
    scope_map = {item.bot_plugin_id: item for item in materialized}
    scope = scope_map.get(binding.id)
    if scope:
        return scope

    scope = GroupPluginScope(
        channel_id=channel.id,
        chatroom_id=chatroom.id,
        bot_plugin_id=binding.id,
        scope_status="inherited",
        enabled=binding_group_default_enabled(binding),
        action_scope="all",
    )
    db.add(scope)
    db.flush()
    return scope


def _parse_extra_json(raw: str | None) -> dict[str, Any]:
    normalized = str(raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _infer_chatroom_name(
    conversation_payload: dict[str, Any],
    conversation: Conversation | None,
    chatroom_external_id: str,
) -> str | None:
    for candidate in (
        conversation_payload.get("title"),
        getattr(conversation, "title", None),
        chatroom_external_id,
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _resolve_member_nickname(message_payload: dict[str, Any], member_external_id: str) -> str:
    for candidate in (
        message_payload.get("sender_name"),
        member_external_id,
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return member_external_id


def _latest(previous: datetime | None, incoming: datetime) -> datetime:
    return max(ensure_shanghai_datetime(previous), incoming) if previous else incoming


def _resolve_synced_at(timestamp: Any) -> datetime | None:
    if timestamp not in (None, ""):
        try:
            return datetime.fromtimestamp(int(timestamp), tz=now_shanghai().tzinfo)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return None


def _receipt_data(payload: dict[str, Any]) -> dict[str, Any]:
    # 874 外层 Code/Success 只表明解包成功，目录写入还需要微信侧的实际业务回执。
    data = payload.get("Data")
    if (payload.get("Success") is not True or type(payload.get("Code")) is not int or payload["Code"] != 0
            or not isinstance(data, dict)):
        return {}
    base = data.get("BaseResponse")
    return data if isinstance(base, dict) and type(base.get("ret")) is int and base["ret"] == 0 else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _person_id(value: Any) -> str:
    value = _text(value)
    if (len(value) > 128 or value.endswith("@chatroom") or any(c.isspace() for c in value)
            or value.lower() in {"0", "-", "null", "unknown", "undefined", "system", "weixin"}):
        return ""
    return value


def _normalize_chatroom_profile(payload: dict[str, Any], room_id: str) -> dict[str, str]:
    data = _receipt_data(payload)
    contacts = data.get("ContactList")
    if not isinstance(contacts, list):
        return {}

    def contact_text(contact, key):
        field = contact.get(key)
        return _text(field.get("string")) if isinstance(field, dict) else ""

    matches = [(index, c) for index, c in enumerate(contacts)
               if isinstance(c, dict) and contact_text(c, "UserName") == room_id]
    if len(matches) != 1:
        return {}
    index, contact = matches[0]
    results = data.get("Ret")
    if results is not None and (not isinstance(results, list) or len(results) != len(contacts)
                                or type(results[index]) is not int or results[index] != 0):
        return {}
    profile = {"name": contact_text(contact, "NickName"), "remark": contact_text(contact, "Remark"),
               "owner_external_id": _person_id(contact.get("ChatRoomOwner"))}
    return {key: value for key, value in profile.items() if value}


def _normalize_chatroom_members(payload: dict[str, Any], room_id: str) -> tuple[int, list[dict[str, Any]]] | None:
    data = _receipt_data(payload)
    version = data.get("ServerVersion")
    roster = data.get("NewChatroomData")
    if (data.get("ChatroomUserName") != room_id or type(version) is not int or version < 0
            or not isinstance(roster, dict)):
        return None
    raw_members, count = roster.get("ChatRoomMember"), roster.get("MemberCount")
    if not isinstance(raw_members, list) or type(count) is not int or count <= 0 or len(raw_members) != count:
        return None

    members: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in raw_members:
        external_id = _person_id(item.get("UserName")) if isinstance(item, dict) else ""
        if not external_id or external_id in ids:
            return None
        ids.add(external_id)
        flag = item.get("ChatroomMemberFlag")
        member_flag = str(flag) if type(flag) is int else ""
        members.append(
            {
                "member_external_id": external_id,
                "nickname": _text(item.get("NickName")),
                "display_name": _text(item.get("DisplayName")),
                "role": "admin" if member_flag in {"2049", "2057"} else "member",
                "member_flag": member_flag,
                "inviter_user_name": _person_id(item.get("InviterUserName")),
                "small_head_img_url": _text(item.get("SmallHeadImgUrl")),
            }
        )
    return version, members


def _replace_chatroom_members(
    db: Session,
    *,
    chatroom: Chatroom,
    channel: Channel,
    members: list[dict[str, Any]],
    synced_at: datetime,
    source: str,
) -> None:
    # 先落本事务更新再锁定成员，完整资料同步与入群消息不会互相抹掉邀请证据。
    db.flush()
    existing_members = db.scalars(
        select(ChatroomMember).where(ChatroomMember.chatroom_id == chatroom.id).with_for_update().execution_options(populate_existing=True)
    ).all()
    existing_map = {item.member_external_id: item for item in existing_members}
    incoming_ids = {item["member_external_id"] for item in members}

    active_ids = set(incoming_ids)
    for existing in existing_members:
        if existing.member_external_id not in incoming_ids:
            # 请求在途期间的新发言优先，不把活跃的成员从缓存和目录中删除。
            if existing.last_synced_at and ensure_shanghai_datetime(existing.last_synced_at) > synced_at:
                if existing.status == "active":
                    active_ids.add(existing.member_external_id)
                continue
            existing.status = "left"
            saved = parse_config_json(existing.extra_json)
            if saved.pop("ipad_membership_event", None) is not None:
                existing.extra_json = json.dumps(saved, ensure_ascii=False)
            existing.last_synced_at = synced_at

    for item in members:
        member = existing_map.get(item["member_external_id"])
        if member and member.status == "left" and member.last_synced_at and ensure_shanghai_datetime(member.last_synced_at) > synced_at:
            active_ids.discard(member.member_external_id)
            continue
        if not member:
            member = ChatroomMember(
                tenant_id=channel.tenant_id,
                chatroom_id=chatroom.id,
                bot_id=channel.bot_id,
                channel_id=channel.id,
                member_external_id=item["member_external_id"],
            )
            db.add(member)
        member.nickname = item["nickname"] or member.nickname or item["member_external_id"]
        member.display_name = item["display_name"] or member.display_name
        member.role = item["role"] or "member"
        member.status = "active"
        member.last_synced_at = _latest(member.last_synced_at, synced_at)
        member_extra = parse_config_json(member.extra_json)
        member_extra.pop("inviter_user_name", None)
        member.extra_json = json.dumps(
            {
                **member_extra,
                "source": source,
                "observed_via": "chatroom_member_detail",
                "member_flag": item.get("member_flag") or "",
                "small_head_img_url": item.get("small_head_img_url") or member_extra.get("small_head_img_url", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        save_member_invitation(member, item.get("inviter_user_name"), source="member_profile", recorded_at=synced_at)
    remember_chatroom_members(channel.id, chatroom.external_id, list(active_ids))
