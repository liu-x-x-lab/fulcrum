"""看图猜成语(内置插件):排行榜显示本群群昵称而不是 wxid。"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="idiom-logs-"))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import (
    Bot,
    BotPlugin,
    Channel,
    Chatroom,
    ChatroomMember,
    Conversation,
    Plugin,
    PluginStateRecord,
)
from app.services import plugin_directory
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest


@pytest.fixture()
def env():
    plugin_directory._SCAN_CACHE.clear()
    engine = create_engine("sqlite://")
    for table in (Bot, Plugin, BotPlugin, Channel, Conversation, Chatroom, ChatroomMember, PluginStateRecord):
        table.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with tenant_scope(1):
        with tenant_session(factory) as db:
            channel = Channel(tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge")
            db.add(channel)
            db.flush()
            conversation = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="g@chatroom", chat_type="group")
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="g@chatroom", name="群", is_active=True)
            db.add_all([conversation, room])
            db.flush()
            db.add(ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="wxid_a1", display_name="小明", status="active"))
            db.flush()
            yield db, channel, conversation


def test_the_bundled_plugin_is_valid_and_has_the_fixed_version() -> None:
    found = {item.manifest.code: item for item in plugin_directory.discover_plugins(force=True)}
    assert found["idiom_guess"].is_valid and found["idiom_guess"].manifest.version == "1.0.3"


def test_leaderboard_shows_group_nicknames_and_keeps_old_records_readable(env) -> None:
    db, channel, conversation = env
    module = plugin_directory.load_directory_plugin("idiom_guess", force=True).instance._module
    namespace = f"idiom_scores_{channel.id}_{module._room_key(channel.id, conversation.external_id).split(':')[-1]}"

    def put(key: str, **score) -> None:
        db.add(PluginStateRecord(tenant_id=1, bot_plugin_id=9, namespace=namespace, state_key=key, payload_json=json.dumps(score)))

    put("k1", count=5, name="wxid_a1", user_id="wxid_a1", last_correct_at=1.0)   # 当时记成了 wxid,成员表里有昵称
    put("k2", count=3, name="老朋友", last_correct_at=2.0)                         # 老记录:没有 user_id
    put("k3", count=2, name="wxid_zz", user_id="wxid_zz", last_correct_at=3.0)    # 成员表里没有:只能显示 ID
    db.flush()
    context = SimpleNamespace(tenant_id=1, binding_id=9, channel_id=channel.id)
    text = module._leaderboard(db, context, conversation, 10)
    assert text.splitlines() == ["【本群猜成语排行榜】", "1. 小明 - 5题", "2. 老朋友 - 3题", "3. wxid_zz - 2题"]


def test_targeted_schedule_does_not_create_other_group_conversations(env, monkeypatch) -> None:
    """群级定时任务只处理自己的会话，不再为其他受管群补建 Conversation。"""

    db, channel, conversation = env
    module = plugin_directory.load_directory_plugin("idiom_guess", force=True).instance._module
    bot = Bot(tenant_id=1, name="机器人", status="running", is_enabled=True)
    plugin = Plugin(code="idiom_guess", display_name="看图猜成语", plugin_type="message")
    db.add_all([bot, plugin])
    db.flush()
    binding = BotPlugin(
        tenant_id=1,
        bot_id=bot.id,
        plugin_id=plugin.id,
        status="enabled",
        config_json=json.dumps({"scheduled_enabled": True, "send_time": "20:00"}),
    )
    db.add(binding)
    db.flush()
    db.add(Chatroom(
        tenant_id=1,
        bot_id=bot.id,
        channel_id=channel.id,
        external_id="other@chatroom",
        name="其他群",
        is_active=True,
        management_status="enabled",
    ))
    db.flush()

    monkeypatch.setattr(
        module,
        "_resolve",
        lambda _db, _context, _config: (binding, conversation, channel, {"scheduled_enabled": True}),
    )
    monkeypatch.setattr(module, "_start", lambda *args, **kwargs: module._result("started", "ok", []))
    request = PluginExecutionRequest(
        action="idiom_daily_start",
        config={"scheduled_enabled": True, "send_time": "20:00"},
        payload={},
        context=PluginExecutionContext(
            tenant_id=1,
            binding_id=binding.id,
            bot_id=bot.id,
            channel_id=channel.id,
            conversation_id=conversation.id,
            chat_type="group",
        ),
    )

    result = module.execute_scheduled_action(db, request)
    db.flush()

    assert result.status == "completed"
    assert [int(item.id) for item in db.scalars(select(Conversation)).all()] == [int(conversation.id)]
