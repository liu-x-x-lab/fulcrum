"""群级 AI 对话：开关、完整供应商选择、人格覆盖及自动下载附件。"""

from __future__ import annotations

import json
import os
import tempfile
import pytest

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="group-ai-logs-"))

from app.models.entities import Chatroom, TenantAiSetting
from app.services import ai_dispatch, ai_memory
from app.services.group_features.ai_chat import get_override
from app.services.group_features.store import get_state, is_enabled, save_state
from app.services.group_features.registry import get_feature
from app.services.group_features.view import build_feature_cards, save_from_form
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed
from tests.test_ai_providers import add_provider


def _room(db, channel, active: bool = True) -> Chatroom:
    room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="1@chatroom", name="群", is_active=active)
    db.add(room)
    db.commit()
    return room


def _resolve(db, channel, chat_type="group", external="1@chatroom"):
    return ai_dispatch.resolve_for_conversation(db, channel_id=channel.id, chat_type=chat_type, conversation_external_id=external)


def test_group_chat_follows_tenant_switch_until_group_sets_its_own() -> None:
    db = _session()
    channel = _seed(db)
    room = _room(db, channel)
    # 租户聊天开关关着:没设置过的群不聊天,但路由(意图)仍可用
    resolved = _resolve(db, channel)
    assert resolved is not None and resolved.chat_enabled is False
    # 租户开着:没单独设置过的群直接跟随,模型沿用租户设置
    db.query(TenantAiSetting).update({"chat_enabled": True})
    db.commit()
    resolved = _resolve(db, channel)
    assert resolved.chat_enabled is True
    assert (resolved.config.api_base, resolved.config.model, resolved.config.api_key) == ("https://llm.test/v1", "m", "k")
    # 群里明确关闭:以群为准
    save_state(room, "ai_chat", enabled=False)
    assert _resolve(db, channel).chat_enabled is False
    # 群里明确打开:租户关着也聊
    db.query(TenantAiSetting).update({"chat_enabled": False})
    save_state(room, "ai_chat", enabled=True)
    assert _resolve(db, channel).chat_enabled is True
    # 群已停用:总开关优先,不聊天
    room.is_active = False
    assert _resolve(db, channel).chat_enabled is False


def test_feature_card_shows_inherited_default_until_saved() -> None:
    from app.services.group_features.view import build_feature_cards

    db = _session()
    channel = _seed(db)
    room = _room(db, channel)
    card = lambda defaults: next(c for c in build_feature_cards(room, "wechat_webridge", defaults) if c["key"] == "ai_chat")
    assert card({"ai_chat": True})["enabled"] is True and card({"ai_chat": False})["enabled"] is False
    save_state(room, "ai_chat", enabled=False)
    assert card({"ai_chat": True})["enabled"] is False  # 保存过就以群为准


def test_memory_switch_defaults_on_and_can_be_turned_off_per_group() -> None:
    db = _session()
    channel = _seed(db)
    room = _room(db, channel)
    db.query(TenantAiSetting).update({"chat_enabled": True})
    db.commit()
    assert _resolve(db, channel).memory_enabled is True
    save_state(room, "ai_chat", enabled=True, config={"memory_enabled": False})
    assert _resolve(db, channel).memory_enabled is False
    # 私聊没有群设置:记忆默认开
    assert _resolve(db, channel, "direct", "wxid_a").memory_enabled is True


def test_group_overrides_model_partially_and_persona() -> None:
    db = _session()
    channel = _seed(db)
    room = _room(db, channel)
    db.query(TenantAiSetting).update({"persona": "租户人格"})
    # 只改模型名与人格,地址和密钥沿用租户
    save_state(room, "ai_chat", enabled=True, config={"model": "big-model", "persona": "群里的小牛"})
    cfg = _resolve(db, channel).config
    assert (cfg.api_base, cfg.model, cfg.api_key, cfg.persona) == ("https://llm.test/v1", "big-model", "k", "群里的小牛")
    provider = add_provider(db, api_base="https://other.test/v1/", api_key="group-key")
    save_state(room, "ai_chat", enabled=True, config={"provider_id": str(provider.id), "model": "x", "persona": ""})
    cfg = _resolve(db, channel).config
    assert (cfg.api_base, cfg.model, cfg.api_key, cfg.persona) == ("https://other.test/v1", "x", "group-key", "租户人格")


def test_group_stores_only_provider_reference_and_rejects_invalid_selection() -> None:
    db = _session()
    channel = _seed(db)
    room = _room(db, channel)
    provider = add_provider(db)
    save_from_form(room, "ai_chat", {"enabled": "true", "cfg_provider_id": str(provider.id), "cfg_model": "m1", "cfg_api_key": "must-not-save"})
    assert get_override(room).provider_id == provider.id
    assert "api_key" not in room.extra_json and "must-not-save" not in room.extra_json
    with pytest.raises(ValueError):
        save_from_form(room, "ai_chat", {"enabled": "true", "cfg_provider_id": "99999", "cfg_model": "m2"})
    assert get_override(room).model == "m1"
    provider.enabled = False
    assert _resolve(db, channel) is None
    for invalid in ("99999", "broken", "0", "-2", "²", str(2**80)):
        save_state(room, "ai_chat", enabled=True, config={"provider_id": invalid, "model": "m2"})
        assert _resolve(db, channel) is None


def test_unusable_when_tenant_ai_off_or_model_incomplete() -> None:
    db = _session()
    channel = _seed(db)
    room = _room(db, channel)
    save_state(room, "ai_chat", enabled=True)
    ai_dispatch.get_ai_setting(db).provider.api_key_encrypted = None
    db.commit()
    assert _resolve(db, channel) is None  # 租户没配密钥,群里也没配
    provider = add_provider(db)
    save_state(room, "ai_chat", enabled=True, config={"provider_id": str(provider.id), "model": "m"})
    assert _resolve(db, channel) is not None
    db.query(TenantAiSetting).update({"enabled": False})
    db.commit()
    assert _resolve(db, channel) is None  # 租户 AI 总开关关闭


def test_private_chat_uses_tenant_switch() -> None:
    db = _session()
    channel = _seed(db)
    assert _resolve(db, channel, "direct", "wxid_a").chat_enabled is False
    db.query(TenantAiSetting).update({"chat_enabled": True})
    db.commit()
    assert _resolve(db, channel, "direct", "wxid_a").chat_enabled is True


def test_media_download_is_a_channel_limited_group_feature() -> None:
    db = _session()
    channel = _seed(db)
    room = _room(db, channel)
    names = lambda ct: [c["key"] for c in build_feature_cards(room, ct)]
    assert "media_download" in names("wechat_webridge") and "media_download" in names("wechat_ipad")
    assert "media_download" not in names("workwechat")
    assert not is_enabled(room, "media_download")
    save_state(room, "media_download", enabled=True)
    assert is_enabled(room, "media_download")
    from app.services import channel_events

    conversation = type("C", (), {"chat_type": "group", "external_id": "1@chatroom"})()
    assert channel_events._chatroom_auto_download_media_enabled(db, channel=channel, conversation=conversation) is True
    # 群停用后不再下载
    room.is_active = False
    assert channel_events._chatroom_auto_download_media_enabled(db, channel=channel, conversation=conversation) is False


def test_web_search_defaults_on_and_can_be_turned_off_per_group() -> None:
    db = _session()
    channel = _seed(db)
    room = _room(db, channel)
    db.query(TenantAiSetting).update({"chat_enabled": True})
    db.commit()
    # 默认开;租户 ID 带进聊天上下文,用来按租户计搜索额度
    assert _resolve(db, channel).web_search is True
    chat = ai_dispatch.prepare_intent(db, _payload(channel))[3]
    assert chat.web_search is True and chat.tenant_id == 1
    save_state(room, "ai_chat", enabled=True, config={"web_search": False})
    assert _resolve(db, channel).web_search is False
    assert ai_dispatch.prepare_intent(db, _payload(channel))[3].web_search is False
    # 私聊没有群设置:默认开
    assert _resolve(db, channel, "direct", "wxid_a").web_search is True


def _payload(channel) -> dict:
    return {
        "channel_id": channel.id,
        "conversation_id": 2,
        "normalized_event": {
            "conversation": {"external_id": "1@chatroom", "chat_type": "group"},
            "message": {"content": "你好呀", "sender_id": "wxid_a", "sender_name": "小明", "message_type": "text"},
            "details": {"mentioned_self": True},
        },
    }


def test_prepare_intent_builds_chat_context_with_memory_bank(monkeypatch) -> None:
    """聊天上下文带上记忆库名、发言人昵称和记忆开关;未部署记忆服务或群里关闭时记忆不启用。"""

    from app.core.settings import settings

    db = _session()
    channel = _seed(db)
    room = _room(db, channel)
    db.query(TenantAiSetting).update({"chat_enabled": True})
    db.commit()

    monkeypatch.setattr(settings, "hindsight_url", "http://hindsight:8888")
    prepared = ai_dispatch.prepare_intent(db, _payload(channel))
    chat = prepared[3]
    assert (chat.bank_id, chat.sender_name, chat.chat_type, chat.memory_enabled) == ("t1-1@chatroom", "小明", "group", True)
    # 记忆里用微信 ID;聊天时能把 ID 换回对方当前的昵称(这里对方自己只有 wxid 以外的昵称"小明")
    assert chat.own_label == ai_memory.speaker_label("wxid_a") and chat.bank_name == "群"

    save_state(room, "ai_chat", enabled=True, config={"memory_enabled": False})
    assert ai_dispatch.prepare_intent(db, _payload(channel))[3].memory_enabled is False

    save_state(room, "ai_chat", enabled=True, config={"memory_enabled": True})
    monkeypatch.setattr(settings, "hindsight_url", "")
    assert ai_dispatch.prepare_intent(db, _payload(channel))[3].memory_enabled is False


def test_speaker_names_come_from_recent_messages_and_the_member_table() -> None:
    """称呼映射不只靠最近发言:群成员表里没发过言的成员(含非 wxid_ 形式的 ID)也要能换成昵称。"""

    from app.models.entities import ChatroomMember, Conversation, Message
    from app.services import ai_chat

    db = _session()
    channel = _seed(db)
    room = _room(db, channel)
    db.add(Conversation(id=2, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="1@chatroom", chat_type="group"))
    db.flush()
    db.add_all([
        Message(conversation_id=2, direction="in", sender_id="wxid_a", sender_name="旧昵称", content="x", message_type="text"),
        Message(conversation_id=2, direction="in", sender_id="wxid_a", sender_name="新昵称", content="y", message_type="text"),
        Message(conversation_id=2, direction="in", sender_id="wxid_b", sender_name="wxid_b", content="z", message_type="text"),  # 昵称就是 ID:不收
        Message(conversation_id=2, direction="out", sender_id=None, sender_name=None, content="bot", message_type="text"),
        ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="member_x1", nickname="老刘", display_name="", status="active"),
        ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="R:10086", nickname="企微用户", display_name="群里的名字", status="active"),
        ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="wxid_a", nickname="成员表里的旧名", display_name="", status="active"),
        ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="wxid_c", nickname="wxid_c", display_name="", status="active"),
    ])
    db.commit()
    names = dict(ai_chat.collect_speaker_names(db, conversation_id=2, chatroom_id=room.id))
    # 最近发言里的最新昵称优先于成员表;成员表补全没发过言的;昵称是 ID 的不收
    assert names == {"wxid_a": "新昵称", "member_x1": "老刘", "R:10086": "群里的名字"}
    assert ai_chat.collect_speaker_names(db, conversation_id=None) == ()
