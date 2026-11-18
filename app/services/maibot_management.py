"""MaiBot 专用配置页：范围校验、加密令牌和轻量聊天目录。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Mapping

from sqlalchemy import Integer, and_, case, cast, func, literal, or_, select, tuple_, union_all, update
from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Message, TaskJob, WorkWechatContact
from app.services.maibot_config import (
    BEHAVIOR_DEFAULTS, MAX_CONNECTIONS, MAX_TARGETS, SECRET_FIELD, TRANSPORT_DEFAULTS, MaiBotRouting,
    connection_platform, connections_from_config, parse_config, target_key, valid_ws_url,
)
from app.services.plugin_runtime.maibot_client import MaiBotConnectionKey, maibot_client_registry
from app.services.plugin_scopes import group_plugin_scope_enabled
from app.services.plugin_secrets import PLUGIN_SECRET_VERSION, decrypt_plugin_secret_config, encrypt_plugin_secret_config
from app.services.task_queue import OUTBOUND_DISPATCH_JOB, WORKWECHAT_CLOUD_OUTBOUND_JOB, parse_job_payload


class MaiBotConfigError(ValueError):
    pass


class MaiBotConfigConflict(MaiBotConfigError):
    pass


def config_version(binding: BotPlugin) -> str:
    return hashlib.sha256(json.dumps([binding.config_json, binding.secret_config_encrypted], ensure_ascii=False).encode()).hexdigest()


def connection_tokens(binding: BotPlugin) -> dict[str, str]:
    secret = decrypt_plugin_secret_config(tenant_id=binding.tenant_id, binding_id=binding.id, encrypted=binding.secret_config_encrypted)
    values = secret.get(SECRET_FIELD)
    tokens = {str(key): str(value) for key, value in values.items() if isinstance(value, str)} if isinstance(values, dict) else {}
    if "connections" not in parse_config(binding.config_json):
        legacy = secret.get("api_key") or parse_config(binding.config_json).get("api_key")
        if legacy:
            tokens["default"] = str(legacy)
    return tokens


def binding_channels(db: Session, binding: BotPlugin) -> list[Channel]:
    return list(db.scalars(select(Channel).where(Channel.bot_id == binding.bot_id, Channel.tenant_id == binding.tenant_id,
                                                 Channel.deleted_at.is_(None)).order_by(Channel.id)).all())


def _directory_statement(binding: BotPlugin, chat_type: str):
    channel_ids = select(Channel.id).where(Channel.tenant_id == binding.tenant_id, Channel.bot_id == binding.bot_id, Channel.deleted_at.is_(None))
    if chat_type == "group":
        return select(Chatroom.channel_id.label("channel_id"), Chatroom.external_id.label("external_id"),
                      func.coalesce(func.nullif(Chatroom.remark, ""), func.nullif(Chatroom.name, ""), Chatroom.external_id).label("name"),
                      func.coalesce(Chatroom.name, "").label("note"), Chatroom.id.label("room_id"),
                      (Chatroom.status != "left").label("valid")).where(
                          Chatroom.tenant_id == binding.tenant_id, Chatroom.bot_id == binding.bot_id, Chatroom.channel_id.in_(channel_ids))
    friends = select(WorkWechatContact.channel_id.label("channel_id"), WorkWechatContact.contact_external_id.label("external_id"),
                     func.coalesce(func.nullif(WorkWechatContact.display_name, ""), func.nullif(WorkWechatContact.name, ""), WorkWechatContact.contact_external_id).label("name"),
                     func.coalesce(WorkWechatContact.name, "").label("note"), literal(0).label("room_id"),
                     and_(WorkWechatContact.deleted_at.is_(None), WorkWechatContact.sync_status == "active").label("valid"),
                     literal(1).label("friend")).where(WorkWechatContact.tenant_id == binding.tenant_id, WorkWechatContact.contact_kind == "friend", WorkWechatContact.channel_id.in_(channel_ids))
    direct = select(Conversation.channel_id.label("channel_id"), Conversation.external_id.label("external_id"),
                    func.coalesce(func.nullif(Conversation.title, ""), Conversation.external_id).label("name"),
                    literal("").label("note"), literal(0).label("room_id"), literal(True).label("valid"), literal(0).label("friend")).where(
                        Conversation.tenant_id == binding.tenant_id, Conversation.bot_id == binding.bot_id, Conversation.chat_type == "direct", Conversation.channel_id.in_(channel_ids))
    combined = union_all(friends, direct).subquery()
    # 好友的删除状态优先于历史私聊；历史会话不能使已删除的好友再次可选。
    return select(combined.c.channel_id, combined.c.external_id,
                  func.coalesce(func.max(case((combined.c.friend == 1, combined.c.name))), func.max(combined.c.name)).label("name"),
                  func.max(combined.c.note).label("note"), literal(0).label("room_id"),
                  case((func.max(combined.c.friend) == 1, func.min(case((combined.c.friend == 1, cast(combined.c.valid, Integer)), else_=1))), else_=1).label("valid")).group_by(
                      combined.c.channel_id, combined.c.external_id)


def _rows_metadata(db: Session, binding: BotPlugin, rows, chat_type: str, channels: list[Channel]) -> list[dict[str, Any]]:
    channel_map = {item.id: item for item in channels}
    room_ids = [row.room_id for row in rows if row.room_id]
    rooms = {room.id: room for room in db.scalars(select(Chatroom).where(Chatroom.id.in_(room_ids))).all()} if room_ids else {}
    scopes = {item.chatroom_id: item for item in db.scalars(select(GroupPluginScope).where(
        GroupPluginScope.bot_plugin_id == binding.id, GroupPluginScope.chatroom_id.in_(room_ids))).all()} if room_ids else {}
    raw = parse_config(binding.config_json)
    routing = MaiBotRouting(raw)
    result = []
    for row in rows:
        channel = channel_map.get(row.channel_id)
        if channel is None:
            continue
        owner = routing.owner(channel_id=row.channel_id, chat_type=chat_type, external_id=row.external_id, include_disabled=True)
        room = rooms.get(row.room_id)
        reason = ""
        if not row.valid:
            reason = "已退出群聊" if chat_type == "group" else "好友已删除或失效"
        elif not channel.is_enabled:
            reason = "接入账号已停用"
        elif room and (not room.is_active or room.management_status != "enabled"):
            reason = "群聊尚未启用运行"
        elif room and not group_plugin_scope_enabled(binding, scopes.get(room.id)):
            reason = "本群尚未开放此插件"
        result.append({"channel_id": row.channel_id, "chat_type": chat_type, "external_id": row.external_id,
                       "name": row.name, "remark": row.note, "channel_name": channel.name,
                       "selectable": bool(row.valid and channel.is_enabled), "warning": reason,
                       "owner_id": owner["id"] if owner else None, "owner_name": owner["name"] if owner else ""})
    return result


def search_targets(db: Session, binding: BotPlugin, *, chat_type: str, keyword: str = "", channel_id: int = 0, page: int = 1) -> dict[str, Any]:
    if chat_type not in {"group", "direct"}:
        raise MaiBotConfigError("聊天类型无效")
    channels = binding_channels(db, binding)
    ids = [item.id for item in channels]
    if channel_id and channel_id not in ids:
        raise MaiBotConfigError("接入账号不属于当前机器人")
    directory = _directory_statement(binding, chat_type).subquery()
    stmt = select(directory).where(directory.c.channel_id.in_([channel_id] if channel_id else ids))
    if keyword.strip():
        q = keyword.strip()[:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{q}%"
        stmt = stmt.where(or_(*(column.like(like, escape="\\") for column in (directory.c.name, directory.c.note, directory.c.external_id))))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    page = max(1, min(max(1, (total + 19) // 20), page))
    rows = db.execute(stmt.order_by(directory.c.name, directory.c.channel_id, directory.c.external_id).offset((page - 1) * 20).limit(20)).all()
    return {"rows": _rows_metadata(db, binding, rows, chat_type, channels), "page": page, "total": total, "has_more": page * 20 < total}


def selected_target_metadata(db: Session, binding: BotPlugin, connections: list[dict]) -> list[dict]:
    targets = {target_key(target) for item in connections for target in (item.get("scope") or {}).get("targets", [])}
    channels = binding_channels(db, binding)
    result = []
    for chat_type in ("group", "direct"):
        keys = [(channel_id, external_id) for channel_id, kind, external_id in targets if kind == chat_type]
        if not keys:
            continue
        directory = _directory_statement(binding, chat_type).subquery()
        for start in range(0, len(keys), 400):
            rows = db.execute(select(directory).where(tuple_(directory.c.channel_id, directory.c.external_id).in_(keys[start:start + 400]))).all()
            result.extend(_rows_metadata(db, binding, rows, chat_type, channels))
    found = {target_key(item) for item in result}
    for channel_id, chat_type, external_id in targets - found:
        result.append({"channel_id": channel_id, "chat_type": chat_type, "external_id": external_id, "name": external_id,
                       "channel_name": "原账号不可用", "selectable": False, "warning": "聊天或接入账号已失效"})
    return result


def connection_view(db: Session, binding: BotPlugin) -> dict[str, Any]:
    connections = connections_from_config(parse_config(binding.config_json))
    tokens = connection_tokens(binding)
    for item in connections:
        item["token_configured"] = bool(tokens.get(item["id"]))
        item["runtime"] = _connection_runtime_view(db, binding, item)
    return {"ok": True, "version": config_version(binding), "connections": connections,
            "channels": [{"id": item.id, "name": item.name} for item in binding_channels(db, binding)],
            "selected_targets": selected_target_metadata(db, binding, connections), "binding_enabled": binding.status == "enabled"}


def connection_status_view(db: Session, binding: BotPlugin) -> dict[str, Any]:
    """状态轮询不解密令牌，也不重新查询已经选择的整个聊天目录。"""

    connections = connections_from_config(parse_config(binding.config_json))
    return {"ok": True, "version": config_version(binding), "binding_enabled": binding.status == "enabled",
            "connections": [{"id": item["id"], "runtime": _connection_runtime_view(db, binding, item)} for item in connections]}


def _connection_runtime_view(db: Session, binding: BotPlugin, item: Mapping[str, Any]) -> dict[str, Any]:
    if not item.get("enabled") or binding.status != "enabled":
        return {"state": "disabled"}
    if not valid_ws_url(str(item.get("api_server_ws_url") or "")):
        return {"state": "not_configured"}
    key = MaiBotConnectionKey(binding.tenant_id, binding.id, item["id"])
    runtime = maibot_client_registry.status(key, generation=str(item.get("revision") or ""))
    scope = item.get("scope") or {}
    runtime["no_targets"] = scope.get("mode") != "legacy_all" and not scope.get("targets")
    job_id = runtime.pop("outbound_job_id", None)
    if job_id:
        receipt = _outbound_batch_receipt(db, key, item.get("revision"), job_id)
        if receipt:
            runtime.setdefault("diagnostics", {})["delivery"] = receipt
    return runtime


def _outbound_batch_receipt(db: Session, key: MaiBotConnectionKey, revision: Any, job_id: int) -> dict[str, Any] | None:
    first = db.get(TaskJob, job_id)
    receipt = _outbound_receipt(db, key, revision, first)
    if not receipt or not first.dedupe_key:
        return receipt
    extras = db.scalars(select(TaskJob).where(TaskJob.tenant_id == key.tenant_id, TaskJob.job_type == first.job_type,
        TaskJob.dedupe_key.in_([f"{first.dedupe_key}:{index}" for index in range(1, 8)]))).all()
    if not extras:
        return receipt
    parts = [receipt, *(_outbound_receipt(db, key, revision, job) for job in extras)]
    counts = {}
    for part in filter(None, parts):
        counts[part["code"]] = counts.get(part["code"], 0) + 1
    return {"code": "delivery_batch", "counts": counts, "at": max((part["at"] for part in filter(None, parts) if part["at"]), default=None)}


def _outbound_receipt(db: Session, key: MaiBotConnectionKey, revision: Any, job: TaskJob | None) -> dict[str, Any] | None:
    if (job is None or job.tenant_id != key.tenant_id
            or job.job_type not in {OUTBOUND_DISPATCH_JOB, WORKWECHAT_CLOUD_OUTBOUND_JOB}):
        return None
    payload = parse_job_payload(job)
    if (payload.get("plugin_code") != "maibot_adapter" or payload.get("maibot_binding_id") != key.binding_id
            or payload.get("maibot_connection_id") != key.connection_id or payload.get("maibot_connection_revision") != revision):
        return None
    at = job.updated_at
    if job.status == "pending":
        code = "delivery_retry" if job.attempts else "delivery_queued"
    elif job.status == "running":
        code = "delivery_running"
    elif job.status == "failed":
        code = "delivery_failed"
    elif job.status != "completed":
        code = "delivery_skipped"
    else:
        result = parse_config(job.result_json)
        adapter = result.get("adapter_result") or {}
        if result.get("skipped"):
            code = {"expired": "delivery_expired", "plugin_scope_disabled": "delivery_scope_changed",
                    "maibot_assignment_changed": "delivery_scope_changed"}.get(result.get("reason"), "delivery_skipped")
        elif not isinstance(adapter, dict) or not adapter.get("ok"):
            code = "delivery_failed"
        else:
            command_id = adapter.get("job_id")
            command = db.get(TaskJob, command_id) if type(command_id) is int else None
            if command is not None and command.tenant_id == key.tenant_id and command.status == "cancelled":
                return {"code": "delivery_scope_changed", "at": command.finished_at.isoformat() if command.finished_at else None}
            message_id = result.get("message_id")
            message = db.get(Message, message_id) if type(message_id) is int else None
            if (message is not None and message.tenant_id == key.tenant_id and message.direction == "out"
                    and message.conversation_id == payload.get("conversation_id")):
                at = message.updated_at
                status = message.external_status
            else:
                status = adapter.get("external_status") or adapter.get("status")
            if status == "failed":
                code = "delivery_failed"
            elif status in {"pending", "queued"}:
                code = "delivery_waiting_receipt"
            elif status == "sent" or (adapter.get("adapter_type") == "wechat_ipad" and adapter.get("status") == "sent"):
                code = "delivery_sent"
            else:
                code = "delivery_unconfirmed"
    return {"code": code, "at": at.isoformat() if at else None}


def _targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_TARGETS:
        raise MaiBotConfigError(f"每条连接最多选择 {MAX_TARGETS} 个聊天")
    result, seen = [], set()
    for item in value:
        if (not isinstance(item, dict) or type(item.get("channel_id")) is not int
                or not isinstance(item.get("chat_type"), str) or not isinstance(item.get("external_id"), str)):
            raise MaiBotConfigError("聊天选择无效，请刷新列表后重新选择")
        try:
            key = target_key(item)
        except (ValueError, TypeError):
            raise MaiBotConfigError("聊天选择无效") from None
        if key[0] <= 0 or key[1] not in {"group", "direct"} or not key[2] or len(key[2]) > 128:
            raise MaiBotConfigError("聊天选择无效")
        if key not in seen:
            result.append({"channel_id": key[0], "chat_type": key[1], "external_id": key[2]})
            seen.add(key)
    return result


def save_connections(db: Session, binding: BotPlugin, submitted: Mapping[str, Any]) -> list[str]:
    """一次保存所有归属变更；乐观版本和行锁防止不同页面互相覆盖。"""

    db.refresh(binding, with_for_update=True)
    if submitted.get("version") != config_version(binding):
        raise MaiBotConfigConflict("配置已被其他页面修改，请重新载入后再保存")
    values = submitted.get("connections")
    if not isinstance(values, list) or len(values) > MAX_CONNECTIONS:
        raise MaiBotConfigError(f"最多添加 {MAX_CONNECTIONS} 条 MaiBot 连接")
    raw = parse_config(binding.config_json)
    previous = {item["id"]: item for item in connections_from_config(raw)}
    old_tokens = connection_tokens(binding)
    tokens = dict(old_tokens)
    result, names, endpoints, used_targets = [], set(), set(), set()
    for value in values:
        if not isinstance(value, dict):
            raise MaiBotConfigError("连接配置无效")
        connection_id = str(value.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", connection_id) or any(item["id"] == connection_id for item in result):
            raise MaiBotConfigError("连接编号无效或重复，请重新载入")
        old = previous.get(connection_id, {})
        name = str(value.get("name") or "").strip()
        if not name or len(name) > 40 or name.casefold() in names:
            raise MaiBotConfigError("请填写不重复的连接名称（最多 40 字）")
        names.add(name.casefold())
        address = str(value.get("api_server_ws_url") or "").strip()
        enabled = value.get("enabled")
        if not isinstance(enabled, bool):
            raise MaiBotConfigError("连接启用状态无效")
        if (enabled or address) and (len(address) > 2000 or not valid_ws_url(address)):
            raise MaiBotConfigError(f"「{name}」需要有效的 ws:// 或 wss:// 地址，请将令牌填写到独立的连接令牌栏")
        if address and address in endpoints:
            raise MaiBotConfigError("连接地址重复，同一个 MaiBot 无需重复添加")
        if address:
            endpoints.add(address)
        protocol = value.get("protocol", old.get("protocol", "maim"))
        if not isinstance(protocol, str) or protocol not in {"maim", "legacy_api"}:
            raise MaiBotConfigError("连接协议无效")
        scope_value = value.get("scope")
        if not isinstance(scope_value, dict):
            raise MaiBotConfigError("请选择连接负责的聊天")
        mode = scope_value.get("mode", "selected")
        if not isinstance(mode, str) or mode not in {"selected", "legacy_all"} or (mode == "legacy_all" and old.get("scope", {}).get("mode") != "legacy_all"):
            raise MaiBotConfigError("新连接需要明确选择聊天范围")
        scope = {"mode": mode, "targets": _targets(scope_value.get("targets", [])), "exclusions": _targets(scope_value.get("exclusions", [])) if mode == "legacy_all" else []}
        keys = {target_key(target) for target in scope["targets"]}
        if keys & {target_key(target) for target in scope["exclusions"]}:
            raise MaiBotConfigError("同一个聊天不能同时选择和排除")
        if keys & used_targets:
            raise MaiBotConfigError("同一个聊天不能重复分配，请使用「转给当前连接」")
        used_targets.update(keys)
        item = {**TRANSPORT_DEFAULTS, **BEHAVIOR_DEFAULTS,
                **{key: old[key] for key in {*TRANSPORT_DEFAULTS, *BEHAVIOR_DEFAULTS} if key in old},
                "id": connection_id, "name": name, "enabled": enabled, "api_server_ws_url": address, "protocol": protocol,
                "platform_name": old.get("platform_name") or connection_platform(binding.tenant_id, binding.id, connection_id), "scope": scope}
        for key in BEHAVIOR_DEFAULTS:
            item[key] = value.get(key, item[key])
            if not isinstance(item[key], bool):
                raise MaiBotConfigError("对话方式无效")
        clear_token = value.get("clear_token", False)
        if not isinstance(clear_token, bool):
            raise MaiBotConfigError("清除令牌选项无效")
        if clear_token:
            tokens.pop(connection_id, None)
        token = value.get("token", "")
        if not isinstance(token, str) or len(token) > 4096 or any(char in token for char in "\r\n\x00"):
            raise MaiBotConfigError("连接令牌无效")
        if clear_token and token.strip():
            raise MaiBotConfigError("清除令牌或填写新令牌，请选择其中一项")
        if token.strip():
            tokens[connection_id] = token.strip()
        old_comparable = {key: val for key, val in old.items() if key != "revision"}
        item["revision"] = old.get("revision") if item == old_comparable and tokens.get(connection_id) == old_tokens.get(connection_id) else uuid.uuid4().hex
        result.append(item)
    blanket = [item for item in result if item["scope"]["mode"] == "legacy_all"]
    if sum(len(item["scope"]["targets"]) + len(item["scope"]["exclusions"]) for item in result) > 5000:
        raise MaiBotConfigError("一个机器人最多保存 5000 项聊天选择及旧范围排除项")
    if len(blanket) > 1:
        raise MaiBotConfigError("旧范围只能由一条默认连接保留")
    if blanket:
        exclusions = {target_key(target) for target in blanket[0]["scope"]["exclusions"]}
        if any(target_key(target) not in exclusions for item in result if item is not blanket[0] for target in item["scope"]["targets"]):
            raise MaiBotConfigError("所选聊天仍由默认连接负责，请明确转交后保存")
    metadata = selected_target_metadata(db, binding, result)
    valid_keys = {target_key(item) for item in metadata if item.get("selectable")}
    previous_owners = {target_key(target): item["id"] for item in previous.values() for target in item.get("scope", {}).get("targets", [])}
    for item in result:
        for target in item["scope"]["targets"]:
            key = target_key(target)
            if key not in valid_keys and previous_owners.get(key) != item["id"]:
                raise MaiBotConfigError("所选聊天不存在、已失效，或不属于当前机器人的接入账号")
    tokens = {item["id"]: tokens[item["id"]] for item in result if tokens.get(item["id"])}
    encrypted = encrypt_plugin_secret_config(tenant_id=binding.tenant_id, binding_id=binding.id, config={SECRET_FIELD: tokens})
    new_json = json.dumps({"maibot_connections_version": 1, "connections": result}, ensure_ascii=False, sort_keys=True)
    changed = {item["id"] for item in result if previous.get(item["id"], {}).get("revision") != item["revision"]} | (set(previous) - {item["id"] for item in result})
    old_json, old_encrypted = binding.config_json, binding.secret_config_encrypted
    updated = db.execute(update(BotPlugin).where(BotPlugin.id == binding.id, BotPlugin.tenant_id == binding.tenant_id,
                                               BotPlugin.config_json == old_json, BotPlugin.secret_config_encrypted == old_encrypted).values(
                                                   config_json=new_json, secret_config_encrypted=encrypted,
                                                   secret_config_version=PLUGIN_SECRET_VERSION if encrypted else None).execution_options(synchronize_session=False))
    if updated.rowcount != 1:
        db.rollback()
        raise MaiBotConfigConflict("配置发生并发修改，请重新载入")
    db.commit()
    db.refresh(binding)
    return sorted(changed)
