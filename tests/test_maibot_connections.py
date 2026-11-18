"""多 MaiBot 连接的真实租户路由、聊天目录、原子分配和密文回归。"""

from __future__ import annotations

import base64
import json
import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="maibot-connections-logs-"))

import pytest
from sqlalchemy import func, select

import app.web.plugins.maibot_connections as web
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, Plugin, WorkWechatContact
from app.services.maibot_config import SECRET_FIELD, connection_owner, maibot_outbound_allowed
from app.services.plugin_directory import sync_plugins_from_directory
from app.services.plugin_secrets import decrypt_plugin_secret_config, encrypt_plugin_secret_config
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


@pytest.fixture()
def connections_context(provider_page_context, monkeypatch):
    client, ids = provider_page_context
    monkeypatch.setattr(settings, "plugin_secret_key", base64.urlsafe_b64encode(b"m" * 32).decode())
    applied = []
    monkeypatch.setattr(web, "execute_binding_action", lambda binding, action, **kwargs: applied.append((action, kwargs)))
    with ids["factory"]() as db:
        sync_plugins_from_directory(db, force=True)
        plugin = db.scalar(select(Plugin).where(Plugin.code == "maibot_adapter"))
        binding = BotPlugin(tenant_id=ids["alpha"], bot_id=ids["bot"], plugin_id=plugin.id,
                            status="enabled", config_json="{}", group_default_enabled=False)
        second = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="第二微信", channel_type="wechat_ipad", is_enabled=True)
        other_bot = Bot(tenant_id=ids["alpha"], name="其他机器人")
        beta_bot = Bot(tenant_id=ids["beta"], name="其他租户机器人")
        db.add_all([binding, second, other_bot, beta_bot]); db.flush()
        other = Channel(tenant_id=ids["alpha"], bot_id=other_bot.id, name="其他机器人账号", channel_type="wechat_ipad")
        beta = Channel(tenant_id=ids["beta"], bot_id=beta_bot.id, name="其他租户账号", channel_type="wechat_ipad")
        db.add_all([other, beta]); db.flush()
        rooms = [
            Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id="room-one", name="同名群", remark="周末讨论", status="active", management_status="enabled", is_active=True),
            Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=second.id, external_id="room-two", name="同名群", status="active", management_status="enabled", is_active=True),
            Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id="left-room", name="退出群", status="left"),
            Chatroom(tenant_id=ids["alpha"], bot_id=other_bot.id, channel_id=other.id, external_id="other-room", name="其他机器人群"),
            Chatroom(tenant_id=ids["beta"], bot_id=beta_bot.id, channel_id=beta.id, external_id="beta-room", name="其他租户群"),
        ]
        db.add_all(rooms); db.flush()
        db.add_all([
            WorkWechatContact(tenant_id=ids["alpha"], channel_id=ids["channel_id"], contact_kind="friend", contact_external_id="friend-1", name="小林", display_name="同学小林"),
            WorkWechatContact(tenant_id=ids["alpha"], channel_id=second.id, contact_kind="friend", contact_external_id="friend-2", name="好友无会话"),
            WorkWechatContact(tenant_id=ids["alpha"], channel_id=ids["channel_id"], contact_kind="friend", contact_external_id="deleted-friend", name="已删除好友", sync_status="deleted"),
            WorkWechatContact(tenant_id=ids["alpha"], channel_id=other.id, contact_kind="friend", contact_external_id="other-friend", name="其他机器人好友"),
            WorkWechatContact(tenant_id=ids["beta"], channel_id=beta.id, contact_kind="friend", contact_external_id="beta-friend", name="其他租户好友"),
            ChatroomMember(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], chatroom_id=rooms[0].id, member_external_id="member-only", display_name="只有群成员身份"),
            Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], channel_type="wechat_ipad", external_id="friend-1", title="旧私聊昵称", chat_type="direct"),
            Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], channel_type="wechat_ipad", external_id="direct-only", title="仅有私聊记录", chat_type="direct"),
            Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], channel_type="wechat_ipad", external_id="deleted-friend", title="已删除的历史私聊", chat_type="direct"),
        ])
        db.commit()
        ids = {**ids, "binding": binding.id, "plugin": plugin.id, "second": second.id, "other": other.id, "beta_channel": beta.id, "rooms": [room.id for room in rooms], "applied": applied}
    return client, ids


def _url(ids, suffix=""):
    return f"/plugins/{ids['binding']}/maibot{suffix}"


def _target(ids, external="room-one", chat_type="group", channel=None):
    return {"channel_id": channel or ids["channel_id"], "chat_type": chat_type, "external_id": external}


def _connection(connection_id="daily", *, targets=None, token="", enabled=True):
    return {"id": connection_id, "name": "麦麦 " + connection_id, "enabled": enabled,
            "api_server_ws_url": f"wss://{connection_id}.example.test/ws", "protocol": "maim", "token": token,
            "enable_reply_output": True, "mention_only": False,
            "scope": {"mode": "selected", "targets": targets or [], "exclusions": []}}


def _save(client, ids, csrf, connections, version=None):
    return client.post(_url(ids), json={"version": version or client.get(_url(ids)).json()["version"], "connections": connections}, headers={"X-CSRF-Token": csrf})


def test_directory_search_account_scope_and_private_union(connections_context):
    client, ids = connections_context; _login(client, "operator")
    rooms = client.get(_url(ids, "/targets"), params={"q": "同名群"}).json()["rows"]
    assert {item["channel_id"] for item in rooms} == {ids["channel_id"], ids["second"]}
    assert all(item["warning"] == "本群尚未开放此插件" for item in rooms)
    assert client.get(_url(ids, "/targets"), params={"q": "周末"}).json()["total"] == 1
    assert client.get(_url(ids, "/targets"), params={"q": "%"}).json()["total"] == 0
    assert client.get(_url(ids, "/targets"), params={"channel_id": ids["second"]}).json()["total"] == 1
    assert client.get(_url(ids, "/targets"), params={"channel_id": ids["other"]}).status_code == 400
    direct = client.get(_url(ids, "/targets"), params={"chat_type": "direct"}).json()["rows"]
    assert {item["external_id"] for item in direct} == {"friend-1", "friend-2", "direct-only", "deleted-friend"}
    assert next(item for item in direct if item["external_id"] == "friend-1")["name"] == "同学小林"
    assert next(item for item in direct if item["external_id"] == "deleted-friend")["selectable"] is False
    with ids["factory"]() as db:
        assert db.scalar(select(func.count(Conversation.id))) == 3


def test_multi_save_encrypted_tokens_keep_replace_clear_delete_and_stale_version(connections_context):
    client, ids = connections_context; csrf = _login(client, "operator")
    initial = client.get(_url(ids)).json()
    values = [_connection(targets=[_target(ids)], token="daily-secret-value"), _connection("hobby", targets=[_target(ids, "friend-2", "direct", ids["second"])], token="hobby-secret-value")]
    response = _save(client, ids, csrf, values)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert "secret-value" not in response.text
    assert all(item["token_configured"] for item in saved["connections"])
    with ids["factory"]() as db:
        binding = db.get(BotPlugin, ids["binding"])
        assert "secret" not in binding.config_json
        secrets = decrypt_plugin_secret_config(tenant_id=ids["alpha"], binding_id=binding.id, encrypted=binding.secret_config_encrypted)
        assert secrets[SECRET_FIELD] == {"daily": "daily-secret-value", "hobby": "hobby-secret-value"}
    assert _save(client, ids, csrf, saved["connections"], initial["version"]).status_code == 409
    values = saved["connections"]
    revisions = {item["id"]: item["revision"] for item in values}
    values[0]["name"] = "新名字"
    values[0]["token"] = ""
    values[1]["token"] = "new-hobby-secret"
    saved = _save(client, ids, csrf, values).json()
    assert all(item["token_configured"] for item in saved["connections"])
    assert saved["connections"][1]["revision"] != revisions["hobby"]
    with ids["factory"]() as db:
        binding = db.get(BotPlugin, ids["binding"])
        secrets = decrypt_plugin_secret_config(tenant_id=ids["alpha"], binding_id=binding.id, encrypted=binding.secret_config_encrypted)
        assert secrets[SECRET_FIELD] == {"daily": "daily-secret-value", "hobby": "new-hobby-secret"}
    values = saved["connections"]; values[0]["clear_token"] = True
    saved = _save(client, ids, csrf, values).json()
    assert saved["connections"][0]["token_configured"] is False
    assert _save(client, ids, csrf, [saved["connections"][0]]).status_code == 200
    with ids["factory"]() as db:
        assert db.get(BotPlugin, ids["binding"]).secret_config_encrypted is None


@pytest.mark.parametrize("kind", ["duplicate_group", "duplicate_private", "other_bot", "other_tenant", "left_group", "deleted_friend"])
def test_invalid_assignments_are_rejected_without_partial_save(connections_context, kind):
    client, ids = connections_context; csrf = _login(client, "admin")
    target = _target(ids)
    if kind == "duplicate_private": target = _target(ids, "friend-1", "direct")
    elif kind == "other_bot": target = _target(ids, "other-room", channel=ids["other"])
    elif kind == "other_tenant": target = _target(ids, "beta-room", channel=ids["beta_channel"])
    elif kind == "left_group": target = _target(ids, "left-room")
    elif kind == "deleted_friend": target = _target(ids, "deleted-friend", "direct")
    values = [_connection(targets=[target], token="never-save-this-token")]
    if kind.startswith("duplicate"): values.append(_connection("hobby", targets=[target]))
    response = _save(client, ids, csrf, values)
    assert response.status_code == 400, response.text
    with ids["factory"]() as db:
        binding = db.get(BotPlugin, ids["binding"])
        assert binding.config_json == "{}" and binding.secret_config_encrypted is None


def test_legacy_migration_retains_behavior_and_requires_explicit_transfer(connections_context):
    client, ids = connections_context; csrf = _login(client, "admin")
    with ids["factory"]() as db:
        binding = db.get(BotPlugin, ids["binding"])
        binding.config_json = json.dumps({"enabled": True, "api_server_ws_url": "wss://legacy.example.test/ws", "platform_name": "old-platform", "collect_private_messages": False, "enable_reply_output": False, "protocol": "legacy_api"})
        binding.secret_config_encrypted = encrypt_plugin_secret_config(tenant_id=ids["alpha"], binding_id=binding.id, config={"api_key": "old-secret"})
        db.commit()
    initial = client.get(_url(ids)).json()
    legacy = initial["connections"][0]
    assert legacy["scope"]["mode"] == "legacy_all" and legacy["token_configured"]
    assert legacy["collect_private_messages"] is False and legacy["enable_reply_output"] is False
    target = _target(ids)
    values = [legacy, _connection("hobby", targets=[target])]
    assert _save(client, ids, csrf, values).status_code == 400
    legacy["scope"]["exclusions"].append(target)
    response = _save(client, ids, csrf, values)
    assert response.status_code == 200, response.text
    with ids["factory"]() as db:
        binding = db.get(BotPlugin, ids["binding"]); raw = json.loads(binding.config_json)
        assert connection_owner(raw, **target)["id"] == "hobby"
        assert connection_owner(raw, **_target(ids, "room-two", channel=ids["second"]))["id"] == "default"
        assert connection_owner(raw, **_target(ids, "friend-1", "direct")) is None
        assert raw["connections"][0]["platform_name"] == "old-platform"
        secrets = decrypt_plugin_secret_config(tenant_id=ids["alpha"], binding_id=binding.id, encrypted=binding.secret_config_encrypted)
        assert secrets == {SECRET_FIELD: {"default": "old-secret"}}


def test_role_csrf_tenant_and_old_editor_boundaries(connections_context):
    client, ids = connections_context; csrf = _login(client, "readonly")
    assert client.get(_url(ids)).status_code == 200
    assert _save(client, ids, csrf, []).status_code == 403
    csrf = _login(client, "operator")
    assert client.post(_url(ids), json={"connections": []}).status_code == 403
    assert client.post(f"/plugins/{ids['binding']}/edit", data={"csrf_token": csrf, "config_json": '{"enabled":true}'}).status_code == 400
    with ids["factory"]() as db:
        other_bot = Bot(tenant_id=ids["beta"], name="乙专用机器人"); db.add(other_bot); db.flush()
        other = BotPlugin(tenant_id=ids["beta"], bot_id=other_bot.id, plugin_id=ids["plugin"], status="enabled")
        db.add(other); db.commit(); other_id = other.id
    assert client.get(f"/plugins/{other_id}/maibot").status_code == 404


def test_specialized_page_hides_technical_knobs(connections_context):
    client, ids = connections_context; _login(client, "admin")
    response = client.get(f"/plugins/{ids['plugin']}")
    assert response.status_code == 200
    assert "data-maibot-manager" in response.text and "搜索群名称、备注或 ID" in response.text
    assert 'data-mb-stage="delivery"' in response.text
    assert "schema__heartbeat_interval_seconds" not in response.text
    assert "schema__api_key" not in response.text and "schema__connection_tokens" not in response.text


def test_outbound_is_invalidated_by_transfer_disable_and_revision(connections_context):
    client, ids = connections_context; csrf = _login(client, "admin")
    saved = _save(client, ids, csrf, [_connection(targets=[_target(ids, "friend-1", "direct")]), _connection("hobby")]).json()
    original = saved["connections"][0]
    payload = {"maibot_binding_id": ids["binding"], "maibot_connection_id": "daily", "maibot_connection_revision": original["revision"]}
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        conversation = db.scalar(select(Conversation).where(Conversation.external_id == "friend-1"))
        assert maibot_outbound_allowed(db.get(BotPlugin, ids["binding"]), conversation, payload)
    target = saved["connections"][0]["scope"]["targets"].pop()
    saved["connections"][1]["scope"]["targets"].append(target)
    assert _save(client, ids, csrf, saved["connections"]).status_code == 200
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        conversation = db.scalar(select(Conversation).where(Conversation.external_id == "friend-1"))
        assert not maibot_outbound_allowed(db.get(BotPlugin, ids["binding"]), conversation, payload)


def test_connection_test_only_handshakes_and_never_echoes_errors(connections_context, monkeypatch):
    client, ids = connections_context; csrf = _login(client, "operator")
    captured = {}
    class Connection:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
    def connect(address, **kwargs): captured.update(address=address, **kwargs); return Connection()
    monkeypatch.setattr(web, "connect", connect)
    response = client.post(_url(ids, "/test"), json={"id": "draft", "api_server_ws_url": "wss://test.example/ws", "protocol": "maim", "token": "test-token"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and "尚未发送聊天消息" in response.json()["notice"]
    assert captured["additional_headers"]["Authorization"] == "test-token"
    def failed(*args, **kwargs): raise RuntimeError("secret-token-in-error")
    monkeypatch.setattr(web, "connect", failed)
    response = client.post(_url(ids, "/test"), json={"id": "draft", "api_server_ws_url": "wss://test.example/ws"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400 and "secret-token-in-error" not in response.text


def test_status_polling_does_not_decrypt_tokens_or_load_target_directory(connections_context, monkeypatch):
    client, ids = connections_context; csrf = _login(client, "operator")
    saved = _save(client, ids, csrf, [_connection(targets=[_target(ids)], token="status-test-token")]).json()
    import app.services.maibot_management as management
    monkeypatch.setattr(management, "connection_tokens", lambda *_: pytest.fail("status must not decrypt"))
    monkeypatch.setattr(management, "selected_target_metadata", lambda *_: pytest.fail("status must not load targets"))
    response = client.get(_url(ids, "/status"))
    assert response.status_code == 200 and response.json()["version"] == saved["version"]
    assert set(response.json()["connections"][0]) == {"id", "runtime"}
    assert "status-test-token" not in response.text and response.headers["cache-control"] == "no-store"


def test_generic_bind_cannot_bypass_manager_and_rebinding_preserves_configuration(connections_context):
    client, ids = connections_context; csrf = _login(client, "operator")
    saved = _save(client, ids, csrf, [_connection(targets=[_target(ids)], token="rebind-test-token", enabled=False)]).json()
    data = {"csrf_token": csrf, "bot_id": ids["bot"], "plugin_id": ids["plugin"], "status": "enabled", "group_default_enabled": "false"}
    assert client.post("/plugins/bind", data={**data, "config_json": '{"connections":[]}'}).status_code == 400
    assert client.post("/plugins/bind", data={**data, "schema__connection_tokens": "unvalidated-token"}).status_code == 400
    assert client.post("/plugins/bind", data=data).status_code == 400
    assert client.get(_url(ids)).json()["version"] == saved["version"]
    assert client.post(f"/plugins/{ids['binding']}/unbind", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert client.post("/plugins/bind", data=data, follow_redirects=False).status_code == 303
    restored = client.get(_url(ids)).json()["connections"][0]
    assert restored["id"] == "daily" and restored["scope"] == saved["connections"][0]["scope"]
    assert restored["token_configured"] and restored["revision"] != saved["connections"][0]["revision"]
    with ids["factory"]() as db:
        new_bot = Bot(tenant_id=ids["alpha"], name="新建绑定测试"); db.add(new_bot); db.commit(); bot_id = new_bot.id
    assert client.post("/plugins/bind", data={**data, "bot_id": bot_id}, follow_redirects=False).status_code == 303
    with ids["factory"]() as db:
        new_id = db.scalar(select(BotPlugin.id).where(BotPlugin.bot_id == bot_id, BotPlugin.plugin_id == ids["plugin"]))
    assert client.get(f"/plugins/{new_id}/maibot").json()["connections"] == []


def test_malformed_target_and_switch_values_fail_without_changes(connections_context):
    client, ids = connections_context; csrf = _login(client, "operator")
    for patch in [{"channel_id": True}, {"channel_id": 1.5}, {"external_id": {}}, {"chat_type": []}]:
        target = {**_target(ids), **patch}
        assert _save(client, ids, csrf, [_connection(targets=[target])]).status_code == 400
    for patch in [{"protocol": []}, {"clear_token": "true"}, {"clear_token": True, "token": "test-token"}]:
        assert _save(client, ids, csrf, [{**_connection(), **patch}]).status_code == 400
    assert client.get(_url(ids)).json()["connections"] == []
