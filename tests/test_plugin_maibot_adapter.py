from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-maibot-adapter"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Message, Plugin, TaskJob
from app.models.saas import Tenant
from app.services.crud import toggle_binding
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.task_queue import parse_job_payload


@pytest.fixture()
def maibot_fixture(tmp_path: Path):
    """建立真实群消息和启用的 MaiBot 插件绑定。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'maibot.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="maibot", name="MaiBot 租户", status="active")
        db.add(tenant)
        db.flush()
        plugin = Plugin(
            code="maibot_adapter",
            display_name="MaiBot 适配器",
            plugin_type="observer",
        )
        db.add(plugin)
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="对话机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="对话 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="maibot-group-real",
            title="产品交流群",
            chat_type="group",
        )
        db.add(conversation)
        db.flush()
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "enabled": True,
                    "collect_group_messages": True,
                    "collect_private_messages": True,
                    "enable_reply_output": True,
                    "reply_group_messages": True,
                    "reply_private_messages": True,
                    "api_server_ws_url": "wss://maibot.example.test/ws",
                    "platform_name": "multi-bot-saas",
                }
            ),
        )
        db.add(binding)
        db.flush()
        message = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction="in",
            sender_id="member-real-100",
            sender_name="真实成员",
            content="今天发布进度如何？",
            message_type="text",
            external_message_id="maibot-message-real",
            status="received",
        )
        db.add(message)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "binding_id": int(binding.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "message_id": int(message.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _request(identity: dict[str, int], *, config: dict | None = None) -> PluginExecutionRequest:
    return PluginExecutionRequest(
        action="execute",
        config=config
        or {
            "enabled": True,
            "collect_group_messages": True,
            "collect_private_messages": True,
            "enable_reply_output": True,
            "reply_group_messages": True,
            "reply_private_messages": True,
            "api_server_ws_url": "wss://maibot.example.test/ws",
            "api_key": "tenant-maibot-key",
            "platform_name": "multi-bot-saas",
            "verify_ssl": True,
            "connect_timeout_seconds": 15,
            "receive_timeout_seconds": 120,
            "heartbeat_interval_seconds": 20,
            "reconnect_delay_seconds": 5,
            "queue_maxsize": 200,
            "max_send_retries": 2,
        },
        payload={
            "content": "伪造消息",
            "sender_id": "forged-user",
            "conversation_id": 999,
            "conversation_external_id": "forged-room",
            "channel_id": 999,
            "mentioned_self": True,
        },
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=identity["message_id"],
            sender_id="member-real-100",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def test_maibot_adapter_declares_observer_and_tenant_connection_config() -> None:
    """MaiBot 以旁路观察者采集消息，地址为普通配置，API Key 为租户密文。"""

    discovered = get_discovered_plugin("maibot_adapter", force=True)
    assert discovered is not None and discovered.is_valid
    assert discovered.manifest.plugin_type == "observer"
    assert discovered.matcher.commands == ()
    assert discovered.secret_fields == ("api_key", "connection_tokens")
    assert discovered.runtime_config["api_server_ws_url"] == ""
    assert discovered.runtime_config["collect_group_messages"] is True
    assert discovered.runtime_config["collect_private_messages"] is True
    assert discovered.runtime_config["protocol"] == "maim"
    assert "api_key" not in discovered.runtime_config
    assert discovered.capability.consume_policy == "continue"
    assert "telegram" in discovered.capability.channel_types

    source = (PROJECT_ROOT / "plugins/maibot_adapter/plugin.py").read_text(encoding="utf-8")
    assert "WechatAPIClient" not in source
    assert "GroupBotManager" not in source
    assert "send_text_message" not in source
    assert "global_client" not in source


@pytest.mark.parametrize("protocol", ["maim", "legacy_api"])
def test_maibot_adapter_submits_trusted_message_dimensions(maibot_fixture, monkeypatch, protocol) -> None:
    """插件只使用落库消息和内部会话令牌，不采信 payload 中的身份和目标。"""

    factory, identity = maibot_fixture
    loaded = load_directory_plugin("maibot_adapter", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    captured: dict[str, object] = {}

    class FakeRegistry:
        def submit(self, *, key, config, api_message, conversation_id, reply_handler):
            captured.update(
                {
                    "key": key,
                    "config": config,
                    "api_message": api_message,
                    "conversation_id": conversation_id,
                    "reply_handler": reply_handler,
                }
            )
            return True

    monkeypatch.setattr(module, "maibot_client_registry", FakeRegistry())
    request = _request(identity)
    request = replace(request, config={**request.config, "protocol": protocol})
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(db, request)

    assert result.ok is True and result.status == "completed"
    assert result.output == {"provider_status": "queued", "replies": []}
    assert captured["key"].tenant_id == identity["tenant_id"]
    assert captured["key"].binding_id == identity["binding_id"]
    assert captured["conversation_id"] == identity["conversation_id"]
    assert captured["config"].api_key == "tenant-maibot-key"
    api_message = captured["api_message"]
    assert api_message["message_segment"] == {"type": "text", "data": "今天发布进度如何？"}
    assert api_message["message_info"]["message_id"] == "maibot-message-real"
    sender_info = api_message["message_info"] if protocol == "maim" else api_message["message_info"]["sender_info"]
    assert sender_info["user_info"] == {
        "platform": "multi-bot-saas",
        "user_id": f"mbs-user-{identity['channel_id']}-member-real-100",
        "user_nickname": "真实成员",
    }
    assert sender_info["group_info"] == {
        "platform": "multi-bot-saas",
        "group_id": f"mbs-conversation-{identity['conversation_id']}",
        "group_name": "产品交流群",
    }
    serialized = json.dumps(api_message, ensure_ascii=False)
    assert "forged-user" not in serialized
    assert "forged-room" not in serialized
    assert "999" not in serialized
    if protocol == "maim":
        assert api_message["account_id"] == f"mbs-channel-{identity['channel_id']}"
        assert api_message["scope"] is None
        assert "message_dim" not in api_message
        assert "tenant-maibot-key" not in serialized
    else:
        assert api_message["message_dim"]["api_key"] == "tenant-maibot-key"


def test_maibot_adapter_honors_collection_switches_before_connection(maibot_fixture, monkeypatch) -> None:
    """采集关闭或租户连接未配置时，不创建或提交 WebSocket 连接。"""

    factory, identity = maibot_fixture
    loaded = load_directory_plugin("maibot_adapter", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    class ForbiddenRegistry:
        def submit(self, **kwargs):
            raise AssertionError("前置校验失败时不应提交 MaiBot 消息")

    monkeypatch.setattr(module, "maibot_client_registry", ForbiddenRegistry())
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        collection_off = module.execute_message_action(
            db,
            _request(
                identity,
                config={
                    "enabled": True,
                    "collect_group_messages": False,
                    "collect_private_messages": True,
                    "api_server_ws_url": "wss://maibot.example.test/ws",
                    "api_key": "key",
                },
            ),
        )
        unconfigured = module.execute_message_action(
            db,
            _request(
                identity,
                config={
                    "enabled": True,
                    "collect_group_messages": True,
                    "api_server_ws_url": "",
                    "api_key": "",
                },
            ),
        )

    assert collection_off.output["provider_status"] == "collection_disabled"
    assert collection_off.output["replies"] == []
    assert unconfigured.output["provider_status"] == "not_configured"


def test_maibot_client_registry_isolates_bindings_and_replaces_changed_config() -> None:
    """相同绑定复用连接，不同绑定完全隔离，配置变化会先停止旧连接。"""

    from app.services.plugin_runtime.maibot_client import (
        MaiBotClientRegistry,
        MaiBotConnectionConfig,
        MaiBotConnectionKey,
    )

    created = []

    class FakeConnection:
        def __init__(self, *, key, config, reply_handler):
            self.key = key
            self.config = config
            self.reply_handler = reply_handler
            self.started = False
            self.stopped = False
            self.submissions = []
            created.append(self)

        def start(self):
            self.started = True

        def submit(self, *, api_message, conversation_id):
            self.submissions.append((api_message, conversation_id))
            return True

        def stop(self):
            self.stopped = True

    registry = MaiBotClientRegistry(connection_factory=FakeConnection)
    key_a = MaiBotConnectionKey(tenant_id=1, binding_id=10)
    key_b = MaiBotConnectionKey(tenant_id=2, binding_id=10)
    config_a = MaiBotConnectionConfig(ws_url="wss://one.example/ws", api_key="key-a")
    config_b = MaiBotConnectionConfig(ws_url="wss://two.example/ws", api_key="key-b")
    handler = lambda *args: None

    assert registry.submit(
        key=key_a,
        config=config_a,
        api_message={"message": "one"},
        conversation_id=100,
        reply_handler=handler,
    )
    assert registry.submit(
        key=key_a,
        config=config_a,
        api_message={"message": "two"},
        conversation_id=101,
        reply_handler=handler,
    )
    assert registry.submit(
        key=key_b,
        config=config_a,
        api_message={"message": "tenant-two"},
        conversation_id=200,
        reply_handler=handler,
    )
    assert len(created) == 2
    assert created[0].submissions == [({"message": "one"}, 100), ({"message": "two"}, 101)]
    assert created[0] is not created[1]

    assert registry.submit(
        key=key_a,
        config=config_b,
        api_message={"message": "new-config"},
        conversation_id=102,
        reply_handler=handler,
    )
    assert len(created) == 3
    assert created[0].stopped is True
    assert created[2].config == config_b
    registry.stop_all()
    assert all(connection.stopped for connection in created)


def test_maibot_reply_queues_only_the_client_validated_conversation(maibot_fixture, monkeypatch) -> None:
    """回包目标由客户端验证后的内部会话编号决定，回包伪造路由字段不会生效。"""

    from app.services.plugin_runtime.maibot_client import MaiBotConnectionKey

    factory, identity = maibot_fixture
    loaded = load_directory_plugin("maibot_adapter", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(module, "TenantSessionLocal", factory)
    package = {
        "message_info": {
            "message_id": "reply-1",
            "group_info": {"group_id": "mbs-conversation-999"},
            "user_info": {"user_id": "mbs-conversation-999"},
        },
        "message_segment": {"type": "text", "data": "MaiBot 已收到，当前进度正常。"},
    }

    module._handle_maibot_reply(
        MaiBotConnectionKey(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
        ),
        identity["conversation_id"],
        package,
    )

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        job = db.scalar(select(TaskJob).order_by(TaskJob.id.desc()))
        assert job is not None and job.job_type == "outbound_dispatch"
        payload = parse_job_payload(job)
    assert payload["channel_id"] == identity["channel_id"]
    assert payload["conversation_id"] == identity["conversation_id"]
    assert payload["payload"]["external_id"] == "maibot-group-real"
    assert payload["payload"]["content"] == "MaiBot 已收到，当前进度正常。"
    assert "999" not in json.dumps(payload, ensure_ascii=False)


def test_disabling_maibot_binding_stops_its_connection(maibot_fixture, monkeypatch) -> None:
    """租户停用绑定时立即停止对应连接，不能等待下一条消息才发现状态变化。"""

    import app.services.plugin_runtime.maibot_client as client_module

    factory, identity = maibot_fixture
    stopped: list[tuple[int, int]] = []
    monkeypatch.setattr(
        client_module,
        "stop_maibot_connection",
        lambda tenant_id, binding_id: stopped.append((tenant_id, binding_id)),
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, identity["binding_id"])
        assert binding is not None
        toggle_binding(db, binding)

    assert stopped == [(identity["tenant_id"], identity["binding_id"])]


@pytest.mark.parametrize("registered_account", ["account-real", ""])
def test_maim_private_message_uses_trusted_conversation_and_channel_account(maibot_fixture, monkeypatch, registered_account):
    factory, identity = maibot_fixture
    module = load_directory_plugin("maibot_adapter", force=True).instance._module
    captured = {}

    class Registry:
        def submit(self, **kwargs):
            captured.update(kwargs)
            return True

    monkeypatch.setattr(module, "maibot_client_registry", Registry())
    request = _request(identity)
    request = replace(request, context=replace(request.context, chat_type="direct"))
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        db.get(Conversation, identity["conversation_id"]).chat_type = "direct"
        db.get(Channel, identity["channel_id"]).runtime_info_json = json.dumps(
            {"account_connection": {"account_id": registered_account}}
        )
        db.commit()
        assert module.execute_message_action(db, request).ok
    message = captured["api_message"]
    assert message["message_info"]["group_info"] is None
    assert message["message_info"]["user_info"]["user_id"] == f"mbs-conversation-{identity['conversation_id']}"
    assert message["account_id"] == (registered_account or f"mbs-channel-{identity['channel_id']}")
    assert "tenant-maibot-key" not in repr(captured["config"])
    assert "tenant-maibot-key" not in repr(module._load_config(request.config))


@pytest.mark.parametrize("protocol", ["maim", "legacy_api"])
def test_reply_deduplicates_by_message_id_even_when_body_changes(maibot_fixture, monkeypatch, protocol):
    from app.services.plugin_runtime.maibot_client import MaiBotConnectionKey

    factory, identity = maibot_fixture
    module = load_directory_plugin("maibot_adapter", force=True).instance._module
    monkeypatch.setattr(module, "TenantSessionLocal", factory)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, identity["binding_id"])
        binding.config_json = json.dumps({**json.loads(binding.config_json), "protocol": protocol})
        db.commit()
    for text in ("第一条回复", "同一个编号的重复回复"):
        package = {
            "message_info": {"message_id": "stable-reply-id"},
            "message_segment": {"type": "seglist", "data": [{"type": "text", "data": text}]},
        }
        if protocol == "legacy_api":
            package = {"type": "sys_std", "msg_id": "stable-reply-id", "payload": package}
        module._handle_maibot_reply(MaiBotConnectionKey(identity["tenant_id"], identity["binding_id"]), identity["conversation_id"], package)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        jobs = db.scalars(select(TaskJob)).all()
        assert len(jobs) == 1
        assert parse_job_payload(jobs[0])["payload"]["content"] == "第一条回复"


@pytest.mark.parametrize("disabled", ["binding", "bot", "channel", "output", "group_reply", "group_scope", "private_reply", "other_tenant"])
def test_late_reply_rechecks_current_permissions(maibot_fixture, monkeypatch, disabled):
    from app.services.plugin_runtime.maibot_client import MaiBotConnectionKey

    factory, identity = maibot_fixture
    module = load_directory_plugin("maibot_adapter", force=True).instance._module
    monkeypatch.setattr(module, "TenantSessionLocal", factory)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, identity["binding_id"])
        config = json.loads(binding.config_json)
        if disabled == "binding":
            binding.status = "disabled"
        elif disabled == "bot":
            db.get(Bot, identity["bot_id"]).is_enabled = False
        elif disabled == "channel":
            db.get(Channel, identity["channel_id"]).is_enabled = False
        elif disabled == "output":
            config["enable_reply_output"] = False
        elif disabled == "group_reply":
            config["reply_group_messages"] = False
        elif disabled == "private_reply":
            db.get(Conversation, identity["conversation_id"]).chat_type = "direct"
            config["reply_private_messages"] = False
        elif disabled == "group_scope":
            room = Chatroom(tenant_id=identity["tenant_id"], channel_id=identity["channel_id"], bot_id=identity["bot_id"], external_id="maibot-group-real", name="群")
            db.add(room)
            db.flush()
            db.add(GroupPluginScope(tenant_id=identity["tenant_id"], channel_id=identity["channel_id"], chatroom_id=room.id, bot_plugin_id=binding.id, enabled=False))
        binding.config_json = json.dumps(config)
        db.commit()
    tenant_id = identity["tenant_id"] + 1 if disabled == "other_tenant" else identity["tenant_id"]
    module._handle_maibot_reply(
        MaiBotConnectionKey(tenant_id, identity["binding_id"]), identity["conversation_id"],
        {"message_info": {"message_id": "late"}, "message_segment": {"type": "text", "data": "不能发出"}},
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        assert db.scalar(select(TaskJob.id)) is None


def test_invalid_protocol_does_not_create_connection(maibot_fixture, monkeypatch):
    factory, identity = maibot_fixture
    module = load_directory_plugin("maibot_adapter", force=True).instance._module
    request = _request(identity)
    request = replace(request, config={**request.config, "protocol": "guess"})
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(db, request)
    assert result.output["provider_status"] == "invalid_protocol"


@pytest.mark.parametrize("protocol", ["maim", "legacy_api"])
@pytest.mark.parametrize("chat_type", ["group", "direct"])
def test_real_message_roundtrip_through_local_websocket_and_outbound_queue(maibot_fixture, monkeypatch, protocol, chat_type):
    """真实插件、独立连接线程、回环服务及 SQLite 出站任务的完整闭环。"""
    from websockets.asyncio.server import serve
    from app.services.plugin_runtime.maibot_client import MaiBotClientRegistry

    factory, identity = maibot_fixture
    module = load_directory_plugin("maibot_adapter", force=True).instance._module
    registry = MaiBotClientRegistry()
    monkeypatch.setattr(module, "TenantSessionLocal", factory)
    monkeypatch.setattr(module, "maibot_client_registry", registry)

    async def run():
        loop = asyncio.get_running_loop()
        completed = asyncio.Event()
        handled = []
        original_handler = module._handle_maibot_reply

        def handler(*args):
            original_handler(*args)
            handled.append(args)
            if len(handled) == 2:
                loop.call_soon_threadsafe(completed.set)

        monkeypatch.setattr(module, "_handle_maibot_reply", handler)

        async def server(ws):
            packet = json.loads(await ws.recv())
            incoming = packet["payload"] if protocol == "legacy_api" else packet
            info = incoming["message_info"]
            sender = info["sender_info"] if protocol == "legacy_api" else info
            group = sender["group_info"]
            target = {"group_info": group, "user_info": sender["user_info"] if chat_type == "direct" else None}
            response_info = {
                "platform": "multi-bot-saas", "message_id": "real-roundtrip",
                "group_info": group,
                "user_info": {"platform": "multi-bot-saas", "user_id": "robot", "user_nickname": "麦麦"},
                "receiver_info": target,
            }
            if protocol == "legacy_api" and chat_type == "direct":
                response_info["user_info"] = target["user_info"]
            for text in ("麦麦收到了", "重复的同编号回复"):
                response = {"message_info": response_info, "message_segment": {"type": "seglist", "data": [{"type": "text", "data": text}]}}
                if protocol == "legacy_api":
                    response = {"type": "sys_std", "msg_id": "real-roundtrip", "payload": response}
                await ws.send(json.dumps(response))
            await ws.wait_closed()

        async with serve(server, "127.0.0.1", 0) as service:
            port = service.sockets[0].getsockname()[1]
            request = _request(identity)
            config = {**request.config, "protocol": protocol, "api_server_ws_url": f"ws://127.0.0.1:{port}/maim/test/ws"}
            request = replace(request, config=config, context=replace(request.context, chat_type=chat_type))
            with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
                conversation = db.get(Conversation, identity["conversation_id"])
                conversation.chat_type = chat_type
                binding = db.get(BotPlugin, identity["binding_id"])
                binding.config_json = json.dumps({key: value for key, value in config.items() if key != "api_key"})
                db.commit()
            try:
                with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
                    assert module.execute_message_action(db, request).ok
                await asyncio.wait_for(completed.wait(), timeout=10)
            finally:
                await asyncio.to_thread(registry.stop_all)
        with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
            jobs = db.scalars(select(TaskJob)).all()
            assert len(jobs) == 1
            payload = parse_job_payload(jobs[0])
            assert payload["channel_id"] == identity["channel_id"]
            assert payload["payload"]["external_id"] == "maibot-group-real"
            assert payload["payload"]["chat_type"] == chat_type
            assert payload["payload"]["content"] == "麦麦收到了"

    asyncio.run(run())
