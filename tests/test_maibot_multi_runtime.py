"""多连接消息路由、独立重连和转交后的两级出站拦截。"""

import copy
import base64
import io
import json
import wave
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, Message, MessageAttachment, TaskJob
from app.services import async_workers, maibot_wechat
from app.services.crud import toggle_binding
from app.services.maibot_config import SECRET_FIELD, connection_platform, connections_from_config, maibot_outbound_allowed
from app.services.maibot_management import connection_status_view
from app.services.plugin_directory import load_directory_plugin
from app.services.plugin_runtime.maibot_client import MaiBotBindingConnection, MaiBotClientRegistry, MaiBotConnectionKey
from app.services.task_queue import MAIBOT_MEDIA_FORWARD_JOB, complete_job, parse_job_payload
from test_plugin_maibot_adapter import _request, maibot_fixture  # noqa: F401


TEST_GIF = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


@pytest.mark.parametrize("channel_type", ["wechat_webridge", "wechat_gugatool"])
def test_wechat_mixed_reply_order_receipts_and_phone_scope(multi_runtime, monkeypatch, channel_type, tmp_path):
    from app.services import maibot_management
    from app.services.guga_agent import service as guga
    from app.services.wechat_webridge import service as webridge

    factory, ids, _, _, module = multi_runtime
    monkeypatch.setattr(maibot_wechat.settings, "public_base_url", "https://files.example.test")
    monkeypatch.setattr(maibot_wechat.settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(guga, "identity_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(webridge, "identity_matches", lambda *args: True)
    monkeypatch.setattr(webridge, "_poll_maintenance", lambda *args: None)
    service = guga if channel_type == "wechat_gugatool" else webridge
    key = MaiBotConnectionKey(ids["tenant_id"], ids["binding_id"], "daily")
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = channel_type
        db.get(Conversation, ids["conversation_id"]).channel_type = channel_type
        db.commit()
    package = {"message_info": {"message_id": "mixed-1"}, "message_segment": {"type": "seglist", "data": [
        {"type": "image", "data": TEST_GIF}, {"type": "text", "data": "图片说明"},
    ]}}
    first = module._handle_maibot_reply(key, ids["conversation_id"], package, "daily-v1")
    duplicate = module._handle_maibot_reply(key, ids["conversation_id"], package, "daily-v1")
    assert first.code == "reply_queued" and duplicate.code == "reply_duplicate" and first.job_id == duplicate.job_id

    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        channel = db.get(Channel, ids["channel_id"])
        binding = db.get(BotPlugin, ids["binding_id"])
        head = db.get(TaskJob, first.job_id)
        tail = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == head.dedupe_key + ":1"))
        receipt = lambda: maibot_management._outbound_batch_receipt(db, key, "daily-v1", head.id)["counts"]
        assert receipt() == {"delivery_queued": 2}
        for job in (head, tail):
            result = async_workers._process_outbound_dispatch_job(db, job, parse_job_payload(job))
            assert result["adapter_result"]["ok"]
            complete_job(db, job, result=result)
        assert receipt() == {"delivery_waiting_receipt": 2}
        if channel_type == "wechat_gugatool":
            # WS leaves an oversized first command for HTTP; its text must wait too.
            assert guga.claim_commands(db, channel, {}, max_payload_bytes=1) == []
        claimed = service.claim_commands(db, channel, {})
        assert len(claimed) == 1 and (claimed[0].get("type") or claimed[0].get("command_name")) == "send_image"
        assert service.claim_commands(db, channel, {}) == []
        pending = db.get(TaskJob, json.loads(tail.result_json)["adapter_result"]["job_id"])
        assert parse_job_payload(pending)["maibot_connection_revision"] == "daily-v1"
        config = json.loads(binding.config_json)
        config["connections"][0]["revision"] = "daily-v2"
        binding.config_json = json.dumps(config)
        db.commit()
        assert service.claim_commands(db, channel, {}) == []
        assert pending.status == "cancelled"
        assert receipt() == {"delivery_waiting_receipt": 1, "delivery_scope_changed": 1}


def test_wechat_names_mentions_quotes_stay_in_conversation(multi_runtime):
    factory, ids, direct_ids, _, _ = multi_runtime
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_webridge"
        room = db.get(Conversation, ids["conversation_id"])
        msg = db.get(Message, ids["message_id"])
        other = db.get(Message, direct_ids["message_id"])
        msg.sender_name = None
        for room_id, display_name in [(room.external_id, "本群名片"), ("other@chatroom", "隔壁群名片")]:
            directory = Chatroom(bot_id=channel.bot_id, channel_id=channel.id, external_id=room_id, is_active=True)
            db.add(directory)
            db.flush()
            db.add(ChatroomMember(bot_id=channel.bot_id, channel_id=channel.id, chatroom_id=directory.id,
                                  member_external_id=msg.sender_id, nickname="微信昵称", display_name=display_name))
        db.flush()
        typed = maibot_wechat.inbound_context(db, channel, room, msg, "@本群名片", "wx", False)
        assert typed["nickname"] == "微信昵称" and typed["cardname"] == "本群名片"
        assert typed["segments"] == [{"type": "text", "data": "@本群名片"}]
        msg.raw_request_json = json.dumps({"mentions": [msg.sender_id]})
        native = maibot_wechat.inbound_context(db, channel, room, msg, "@本群名片", "wx", False)
        assert [item["type"] for item in native["segments"]] == ["at"]
        part, = maibot_wechat.reply_parts(db, channel, room, {"type": "seglist", "data": [
            {"type": "reply", "data": f"mbs-message-{other.id}"},
            {"type": "at", "data": "someone-else"}, {"type": "at", "data": msg.sender_id},
            {"type": "text", "data": "{{mention:someone-else}} 回复"},
        ]})
        assert part["mention_user_list"] == [msg.sender_id]
        assert "quote_message_id" not in part and "{{mention:" not in part["content"]
        assert maibot_wechat.checked_quote(db, channel, room, f"mbs-message-{msg.id}") == {"quote_message_id": msg.external_message_id}


def test_wechat_voice_and_gif_keep_declared_type():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        writer.writeframes(bytes(320))
    wav = buffer.getvalue()
    assert maibot_wechat.media_format(wav, "voice") == (wav, "wav", "audio/wav")
    assert maibot_wechat.media_format(wav[:-1], "voice") is None
    assert maibot_wechat.media_source("voice", "https://media.example.test/a.wav") is None
    assert maibot_wechat.media_source("image", TEST_GIF)[1] == "gif"
    assert maibot_wechat.media_source("emoji", base64.b64encode(wav).decode()) is None


@pytest.mark.parametrize("scenario", ["downloaded", "revision_changed", "timeout"])
def test_wechat_waits_for_media_then_rechecks_scope(multi_runtime, monkeypatch, tmp_path, scenario):
    from datetime import timedelta

    factory, ids, _, raw, module = multi_runtime
    submissions = []
    monkeypatch.setattr(module, "maibot_client_registry", SimpleNamespace(
        submit=lambda **kwargs: submissions.append(kwargs["api_message"]) or True, record_forward=lambda *args: None))
    monkeypatch.setattr(maibot_wechat.settings, "data_dir", str(tmp_path))
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_webridge"
        db.get(Conversation, ids["conversation_id"]).channel_type = channel.channel_type
        message = db.get(Message, ids["message_id"])
        message.message_type = "emoji"
        message.content = "[表情]"
        db.commit()
        request = _request(ids, config=raw)
        request = replace(request, context=replace(request.context, channel_type=channel.channel_type, message_type="emoji"))
        for _ in range(2):
            assert module.execute_message_action(db, request).output["provider_status"] == "waiting_media"
        assert not submissions
        jobs = db.scalars(select(TaskJob).order_by(TaskJob.id)).all()
        upload, deferred = jobs
        assert deferred.job_type == MAIBOT_MEDIA_FORWARD_JOB
        assert parse_job_payload(upload)["command"]["params"]["media_type"] == "emoji"
        payload = parse_job_payload(deferred)
        assert maibot_wechat.process_media_forward(db, deferred, payload) is None
        attachment = db.get(MessageAttachment, payload["attachment_id"])
        if scenario == "downloaded":
            path = tmp_path / "inbound_media" / f"channel_{channel.id}" / "sticker.gif"
            path.parent.mkdir(parents=True)
            path.write_bytes(base64.b64decode(TEST_GIF))
            attachment.storage_path = str(path)
            attachment.status = "downloaded"
        elif scenario == "revision_changed":
            binding = db.get(BotPlugin, ids["binding_id"])
            raw["connections"][0]["revision"] = "daily-v2"
            binding.config_json = json.dumps(raw)
        else:
            deferred.created_at -= timedelta(seconds=maibot_wechat.MEDIA_WAIT_SECONDS + 1)
        db.commit()
        result = maibot_wechat.process_media_forward(db, deferred, payload)
        if scenario == "revision_changed":
            assert result["provider_status"] == "media_scope_changed" and not submissions
        else:
            assert result["provider_status"] == "queued" and len(submissions) == 1
            expected = {"type": "emoji", "data": TEST_GIF} if scenario == "downloaded" else {"type": "text", "data": "[表情]"}
            assert submissions[0]["message_segment"] == expected


@pytest.fixture()
def multi_runtime(maibot_fixture, monkeypatch):
    factory, ids = maibot_fixture
    module = load_directory_plugin("maibot_adapter", force=True).instance._module
    monkeypatch.setattr(module, "TenantSessionLocal", factory)
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        direct = Conversation(tenant_id=ids["tenant_id"], bot_id=ids["bot_id"], channel_id=ids["channel_id"],
                              channel_type="telegram", external_id="friend-runtime", chat_type="direct")
        db.add(direct); db.flush()
        message = Message(tenant_id=ids["tenant_id"], conversation_id=direct.id, direction="in",
                          sender_id="member-real-100", content="私聊消息", message_type="text", status="received")
        db.add(message); db.flush()
        direct_ids = {**ids, "conversation_id": direct.id, "message_id": message.id}
        raw = {"maibot_connections_version": 1, "connections": [
            {"id": connection_id, "name": connection_id, "enabled": True, "enable_reply_output": True,
             "api_server_ws_url": f"wss://{connection_id}.example.test/ws", "protocol": "maim",
             "revision": connection_id + "-v1", "platform_name": connection_platform(ids["tenant_id"], ids["binding_id"], connection_id),
             "scope": {"mode": "selected", "exclusions": [], "targets": [
                 {"channel_id": ids["channel_id"], "chat_type": kind, "external_id": external}]}}
            for connection_id, kind, external in [("daily", "group", "maibot-group-real"), ("hobby", "direct", "friend-runtime")]
        ]}
        db.get(BotPlugin, ids["binding_id"]).config_json = json.dumps(raw)
        db.commit()
    return factory, ids, direct_ids, raw, module


@pytest.mark.parametrize("chat_type,connection_id", [("group", "daily"), ("direct", "hobby")])
def test_trusted_messages_choose_only_the_assigned_connection(multi_runtime, monkeypatch, chat_type, connection_id):
    factory, ids, direct_ids, raw, module = multi_runtime
    submissions = []
    monkeypatch.setattr(module, "maibot_client_registry", SimpleNamespace(submit=lambda **kwargs: submissions.append(kwargs) or True))
    effective = {**raw, SECRET_FIELD: {"daily": "daily-test-token", "hobby": "hobby-test-token"}}
    request = _request(direct_ids if chat_type == "direct" else ids, config=effective)
    request = replace(request, context=replace(request.context, chat_type=chat_type))
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        assert module.execute_message_action(db, request).ok
    assert len(submissions) == 1
    submitted = submissions[0]
    assert submitted["key"] == MaiBotConnectionKey(ids["tenant_id"], ids["binding_id"], connection_id)
    assert submitted["config"].api_key == connection_id + "-test-token"
    assert submitted["config"].generation == connection_id + "-v1"
    assert "test-token" not in json.dumps(submitted["api_message"])
    event = SimpleNamespace(chat_type=chat_type, message_type="text", binding_config=effective,
                            channel_id=ids["channel_id"], conversation_external_id="friend-runtime" if chat_type == "direct" else "maibot-group-real")
    assert module.matches_event(event)["matched"]
    event.conversation_external_id = "unassigned-chat"
    assert not module.matches_event(event)["matched"]


def test_unassigned_disabled_and_mention_only_do_not_forward(multi_runtime, monkeypatch):
    factory, ids, _, raw, module = multi_runtime
    submissions = []
    monkeypatch.setattr(module, "maibot_client_registry", SimpleNamespace(
        submit=lambda **kwargs: submissions.append(kwargs) or True, record_forward=lambda *args: None))
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        unassigned = copy.deepcopy(raw)
        unassigned["connections"][0]["scope"]["targets"] = []
        assert module.execute_message_action(db, _request(ids, config=unassigned)).output["provider_status"] == "unassigned"
        disabled = copy.deepcopy(raw); disabled["connections"][0]["enabled"] = False
        assert module.execute_message_action(db, _request(ids, config=disabled)).output["provider_status"] == "unassigned"
        mention = copy.deepcopy(raw); mention["connections"][0]["mention_only"] = True
        request = replace(_request(ids, config=mention), payload={"mentioned_self": False})
        assert module.execute_message_action(db, request).output["provider_status"] == "mention_required"
        assert not submissions
        assert module.execute_message_action(db, replace(request, payload={"mentioned_self": True})).ok
        assert len(submissions) == 1


def test_apply_connections_restarts_only_the_changed_connection(multi_runtime, monkeypatch):
    _, ids, _, raw, module = multi_runtime
    calls = []
    registry = SimpleNamespace(stop=lambda key: calls.append(("stop", key.connection_id)),
                               start=lambda **kwargs: calls.append(("start", kwargs["key"].connection_id, kwargs["config"].api_key)))
    monkeypatch.setattr(module, "maibot_client_registry", registry)
    request = replace(_request(ids, config={**raw, SECRET_FIELD: {"daily": "daily-token", "hobby": "hobby-token"}}),
                      action="apply_connections", payload={"connection_ids": ["daily", "removed"]})
    assert module.execute_action(request).ok
    assert calls == [("stop", "daily"), ("stop", "removed"), ("start", "daily", "daily-token")]


def test_startup_and_reenable_restore_connections_before_messages(multi_runtime, monkeypatch):
    factory, ids, _, _, module = multi_runtime
    started = []
    # Dispatch through the actual plugin startup action, with network threads substituted.
    monkeypatch.setattr(module, "maibot_client_registry", SimpleNamespace(start=lambda **kwargs: started.append(kwargs["key"].connection_id)))
    monkeypatch.setattr(async_workers, "SessionLocal", factory)
    monkeypatch.setattr(async_workers, "TenantSessionLocal", factory)
    async_workers._restore_maibot_bindings()
    assert started == ["daily", "hobby"]
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, ids["binding_id"])
        toggle_binding(db, binding)
        async_workers._restore_maibot_bindings()
        assert started == ["daily", "hobby"]
        toggle_binding(db, binding)
    assert started == ["daily", "hobby", "daily", "hobby"]


def test_diagnostics_distinguish_pending_device_receipt_from_sent(multi_runtime, monkeypatch):
    from app.services import maibot_management

    factory, ids, _, raw, module = multi_runtime
    key = MaiBotConnectionKey(ids["tenant_id"], ids["binding_id"], "daily")
    monkeypatch.setattr(MaiBotBindingConnection, "start", lambda self: None)
    registry = MaiBotClientRegistry()
    monkeypatch.setattr(module, "maibot_client_registry", registry)
    monkeypatch.setattr(maibot_management, "maibot_client_registry", registry)
    request = replace(_request(ids, config=raw), action="start_connections")
    assert module.execute_action(request).ok
    package = {"message_info": {"message_id": "receipt-test"}, "message_segment": {"type": "text", "data": "微信回复内容"}}
    outcome = module._handle_maibot_reply(key, ids["conversation_id"], package, "daily-v1")
    assert outcome.code == "reply_queued" and outcome.job_id
    registry._connections[key].record("reply", outcome.code, job_id=outcome.job_id)

    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, ids["binding_id"])

        def status():
            return connection_status_view(db, binding)["connections"][0]["runtime"]

        assert status()["diagnostics"]["delivery"]["code"] == "delivery_queued"
        message = Message(conversation_id=ids["conversation_id"], direction="out", status="sent", external_status="pending", content="私有消息内容")
        db.add(message)
        db.flush()
        job = db.get(TaskJob, outcome.job_id)
        complete_job(db, job, result={"message_id": message.id, "adapter_result": {"ok": True, "status": "pending"}})
        assert status()["diagnostics"]["delivery"]["code"] == "delivery_waiting_receipt"
        message.external_status = "sent"
        db.commit()
        assert status()["diagnostics"]["delivery"]["code"] == "delivery_sent"
        assert "outbound_job_id" not in status() and "私有消息内容" not in json.dumps(status(), ensure_ascii=False)


@pytest.mark.parametrize("change", ["transfer", "disable", "revision", "reply_off"])
def test_late_callback_and_actual_worker_recheck_assignment(multi_runtime, monkeypatch, change):
    factory, ids, _, raw, module = multi_runtime
    key = MaiBotConnectionKey(ids["tenant_id"], ids["binding_id"], "daily")
    package = {"message_info": {"message_id": "before-change"}, "message_segment": {"type": "text", "data": "已排队回复"}}
    module._handle_maibot_reply(key, ids["conversation_id"], package, "daily-v1")
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        jobs = db.scalars(select(TaskJob)).all()
        assert len(jobs) == 1
        job_id, payload = jobs[0].id, parse_job_payload(jobs[0])
        assert payload["maibot_connection_revision"] == "daily-v1"
        if change == "transfer":
            raw["connections"][1]["scope"]["targets"].extend(raw["connections"][0]["scope"]["targets"])
            raw["connections"][0]["scope"]["targets"] = []
        elif change == "disable": raw["connections"][0]["enabled"] = False
        elif change == "revision": raw["connections"][0]["revision"] = "daily-v2"
        else: raw["connections"][0]["enable_reply_output"] = False
        db.get(BotPlugin, ids["binding_id"]).config_json = json.dumps(raw)
        db.commit()
    # 已经在途的旧回调也不能再次入队。
    package["message_info"]["message_id"] = "after-change"
    module._handle_maibot_reply(key, ids["conversation_id"], package, "daily-v1")
    monkeypatch.setattr(async_workers, "dispatch_outbound_message", lambda *args, **kwargs: pytest.fail("stale reply was sent"))
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        assert len(db.scalars(select(TaskJob)).all()) == 1
        assert async_workers._process_outbound_dispatch_job(db, db.get(TaskJob, job_id), payload) == {
            "skipped": True, "reason": "maibot_assignment_changed"}


def test_current_assignment_still_reaches_the_outbound_adapter(multi_runtime, monkeypatch):
    factory, ids, _, _, module = multi_runtime
    package = {"message_info": {"message_id": "current-reply"}, "message_segment": {"type": "text", "data": "正常回复"}}
    module._handle_maibot_reply(MaiBotConnectionKey(ids["tenant_id"], ids["binding_id"], "daily"), ids["conversation_id"], package, "daily-v1")
    sent = []
    monkeypatch.setattr(async_workers, "dispatch_outbound_message", lambda db, channel, conversation, payload, **kwargs: (
        sent.append(payload) or SimpleNamespace(id=12345), {"ok": True}))
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        job = db.scalar(select(TaskJob))
        result = async_workers._process_outbound_dispatch_job(db, job, parse_job_payload(job))
        assert result["message_id"] == 12345 and sent[0]["content"] == "正常回复"


def test_long_connection_ids_keep_distinct_protocol_platforms():
    platforms = [connection_platform(100, 200, "a" * 63 + suffix) for suffix in ("x", "y")]
    assert platforms[0] != platforms[1]
    assert all(len(platform) <= 64 for platform in platforms)


@pytest.mark.parametrize("legacy", [False, True])
def test_master_disable_then_enable_cannot_revive_a_previous_reply(multi_runtime, monkeypatch, legacy):
    factory, ids, _, raw, _ = multi_runtime
    monkeypatch.setattr("app.services.plugin_runtime.maibot_client.stop_maibot_connection", lambda *_: None)
    if legacy:
        raw = {"enabled": True, "enable_reply_output": True, "api_server_ws_url": "wss://legacy.example.test/ws"}
    with tenant_scope(ids["tenant_id"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, ids["binding_id"])
        binding.config_json = json.dumps(raw); db.commit()
        original = connections_from_config(raw)[0]
        payload = {"maibot_binding_id": binding.id, "maibot_connection_id": original["id"], "maibot_connection_revision": original["revision"]}
        conversation = db.get(Conversation, ids["conversation_id"])
        assert maibot_outbound_allowed(binding, conversation, payload)
        toggle_binding(db, binding); toggle_binding(db, binding)
        assert binding.status == "enabled"
        assert not maibot_outbound_allowed(binding, conversation, payload)
        if legacy:
            assert not maibot_outbound_allowed(binding, conversation, {})
