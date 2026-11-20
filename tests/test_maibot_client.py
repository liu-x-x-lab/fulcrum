"""MaiM 契约与回环 WebSocket 回归；不访问外部服务或真实微信。"""

from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest
from websockets.asyncio.server import serve

from app.services.plugin_runtime.maibot_client import (
    MaiBotBindingConnection,
    MaiBotConnectionConfig,
    MaiBotConnectionKey,
    MaiBotClientRegistry,
    _QueuedSubmission,
    _extract_conversation_id,
    maibot_message_payload,
)


def message(*, direct=False, conversation_id=123):
    token = f"mbs-conversation-{conversation_id}"
    return {
        "account_id": "test-robot",
        "scope": None,
        "message_info": {
            "platform": "fulcrum-test",
            "message_id": "incoming-1",
            "time": time.time(),
            "user_info": {"platform": "fulcrum-test", "user_id": token if direct else "member", "user_nickname": "成员"},
            "group_info": None if direct else {"platform": "fulcrum-test", "group_id": token, "group_name": "测试群"},
        },
        "message_segment": {"type": "text", "data": "你好"},
    }


def reply(*, direct=False, conversation_id=123):
    value = message(direct=direct, conversation_id=conversation_id)
    info = value["message_info"]
    info["message_id"] = "reply-1"
    info["receiver_info"] = {"group_info": copy.deepcopy(info["group_info"]), "user_info": copy.deepcopy(info["user_info"]) if direct else None}
    info["user_info"] = {"platform": "fulcrum-test", "user_id": "test-robot", "user_nickname": "麦麦"}
    info["sender_info"] = {"group_info": copy.deepcopy(info["group_info"]), "user_info": copy.deepcopy(info["user_info"])}
    value["message_segment"] = {"type": "seglist", "data": [{"type": "text", "data": "收到"}]}
    return value


def envelope(value):
    return {"type": "sys_std", "msg_id": "legacy-reply", "payload": value}


def connection(*, protocol="maim", handler=None, **kwargs):
    return MaiBotBindingConnection(
        key=MaiBotConnectionKey(1, 10),
        config=MaiBotConnectionConfig(ws_url="ws://127.0.0.1:1/test", api_key="test-token", platform_name="fulcrum-test", protocol=protocol, **kwargs),
        reply_handler=handler or (lambda *args: None),
    )


@pytest.mark.parametrize("direct", [False, True])
def test_maim_routes_by_receiver_instead_of_robot_sender(direct):
    assert _extract_conversation_id(reply(direct=direct), platform_name="fulcrum-test") == 123


def test_protocols_are_explicit_and_cannot_downgrade_by_packet_shape():
    raw = reply()
    wrapped = envelope(raw)
    assert _extract_conversation_id(wrapped) is None
    assert _extract_conversation_id(raw, protocol="legacy_api") is None
    assert _extract_conversation_id(wrapped, protocol="legacy_api", platform_name="fulcrum-test") == 123
    assert maibot_message_payload(raw, protocol="unknown") is None
    with pytest.raises(ValueError, match="unsupported_maibot_protocol"):
        connection(protocol="unknown")


@pytest.mark.parametrize("mutation", [
    "custom", "control", "wrong_platform", "no_message_id", "bad_receiver", "different_group",
    "missing_receiver_group", "bad_group", "wrong_group_platform", "unhashable_group_platform",
    "arbitrary_target", "oversized_token", "zero_conversation",
])
def test_maim_rejects_invalid_or_conflicting_routes(mutation):
    value = reply()
    info = value["message_info"]
    receiver = info["receiver_info"]
    if mutation == "custom":
        value["is_custom_message"] = True
    elif mutation == "control":
        value["message_type_name"] = "message_id_echo"
    elif mutation == "wrong_platform":
        info["platform"] = "other-tenant"
    elif mutation == "no_message_id":
        info.pop("message_id")
    elif mutation == "bad_receiver":
        info["receiver_info"] = []
    elif mutation == "different_group":
        receiver["group_info"]["group_id"] = "mbs-conversation-999"
    elif mutation == "missing_receiver_group":
        receiver["group_info"] = None
    elif mutation == "bad_group":
        info["group_info"] = []
    elif mutation == "wrong_group_platform":
        receiver["group_info"]["platform"] = "other-tenant"
    elif mutation == "unhashable_group_platform":
        receiver["group_info"]["platform"] = []
    else:
        token = {"arbitrary_target": "wxid-arbitrary", "oversized_token": "mbs-conversation-" + "9" * 5000, "zero_conversation": "mbs-conversation-0"}[mutation]
        receiver["group_info"]["group_id"] = info["group_info"]["group_id"] = token
    assert _extract_conversation_id(value, platform_name="fulcrum-test") is None


def test_private_receiver_platform_is_checked():
    value = reply(direct=True)
    value["message_info"]["receiver_info"]["user_info"]["platform"] = {"bad": "shape"}
    assert _extract_conversation_id(value, platform_name="fulcrum-test") is None


@pytest.mark.parametrize("protocol", ["maim", "legacy_api"])
@pytest.mark.parametrize("direct", [False, True])
def test_real_local_websocket_handshake_roundtrip_and_route_allowlist(protocol, direct):
    async def run():
        loop = asyncio.get_running_loop()
        received = asyncio.Event()
        handled = []
        captured = {}

        def handler(key, conversation_id, package):
            handled.append((key, conversation_id, package))
            loop.call_soon_threadsafe(received.set)

        async def server(ws):
            captured["headers"] = dict(ws.request.headers)
            captured["message"] = json.loads(await ws.recv())
            valid = reply(direct=direct)
            unknown = reply(direct=direct, conversation_id=999)
            wrong_platform = copy.deepcopy(valid)
            wrong_platform["message_info"]["platform"] = "other-tenant"
            await ws.send("invalid json")
            await ws.send("[]")
            for value in (unknown, wrong_platform, valid):
                if protocol == "legacy_api" and direct:
                    value["message_info"]["user_info"] = value["message_info"]["receiver_info"]["user_info"]
                await ws.send(json.dumps(envelope(value) if protocol == "legacy_api" else value))
            await ws.wait_closed()

        async with serve(server, "127.0.0.1", 0) as service:
            port = service.sockets[0].getsockname()[1]
            client = MaiBotBindingConnection(
                key=MaiBotConnectionKey(1, 10),
                config=MaiBotConnectionConfig(ws_url=f"ws://127.0.0.1:{port}/maim/test/ws", api_key="test-token", platform_name="fulcrum-test", protocol=protocol),
                reply_handler=handler,
            )
            incoming = message(direct=direct)
            try:
                assert client.submit(api_message=incoming, conversation_id=123)
                await asyncio.wait_for(received.wait(), timeout=5)
                assert len(handled) == 1
                assert handled[0][0] == MaiBotConnectionKey(1, 10)
                assert handled[0][1] == 123
                if protocol == "maim":
                    assert captured["headers"]["authorization"] == "test-token"
                    assert captured["headers"]["platform"] == "fulcrum-test"
                    assert "x-apikey" not in captured["headers"]
                    assert captured["message"] == incoming
                    assert "test-token" not in json.dumps(captured["message"])
                else:
                    assert captured["headers"]["x-apikey"] == "test-token"
                    assert captured["headers"]["x-platform"] == "fulcrum-test"
                    assert "authorization" not in captured["headers"]
                    assert captured["message"]["type"] == "sys_std"
                    assert captured["message"]["payload"] == incoming
            finally:
                await asyncio.to_thread(client.stop)
            assert client._thread is not None and not client._thread.is_alive()

    asyncio.run(run())


def test_idle_receive_timeout_does_not_reconnect(monkeypatch):
    import app.services.plugin_runtime.maibot_client as module

    async def run():
        client = connection()
        client._remember_conversation(123)
        calls = 0

        async def wait_for(awaitable, *, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                awaitable.close()
                raise asyncio.TimeoutError
            return await awaitable

        class Socket:
            async def recv(self):
                return json.dumps(reply())

        def handler(*args):
            client._stop_requested.set()

        client.reply_handler = handler
        monkeypatch.setattr(module.asyncio, "wait_for", wait_for)
        await client._receive_loop(Socket())
        assert calls == 2

    asyncio.run(run())


@pytest.mark.parametrize("protocol,expected_attempts", [("maim", 1), ("legacy_api", 2)])
def test_ambiguous_maim_send_is_not_replayed_and_legacy_retry_keeps_id(protocol, expected_attempts):
    async def run():
        client = connection(protocol=protocol)
        client._connected_event = asyncio.Event()
        client._connected_event.set()
        sent = []

        class Socket:
            async def send(self, value):
                sent.append(json.loads(value))
                raise OSError("unknown remote delivery")

            async def close(self):
                pass

        socket = Socket()
        client._websocket = socket

        async def reconnect(seconds):
            client._websocket = socket
            client._connected_event.set()

        client._wait_or_stop = reconnect
        await client._send_submission(_QueuedSubmission(message(), 123))
        assert len(sent) == expected_attempts
        if protocol == "legacy_api":
            assert sent[0]["msg_id"] == sent[1]["msg_id"]

    asyncio.run(run())


def test_expired_queued_message_is_not_sent_after_reconnect():
    async def run():
        client = connection(max_message_age_seconds=1)
        client._connected_event = asyncio.Event()
        client._connected_event.set()

        class Socket:
            async def send(self, value):
                raise AssertionError("expired messages must not be sent")

        client._websocket = Socket()
        await client._send_submission(_QueuedSubmission(message(), 123, queued_at=time.monotonic() - 5))
        assert not client._conversation_is_recent(123)

    asyncio.run(run())


def test_recent_conversations_expire_and_are_not_shared_between_connections(monkeypatch):
    import app.services.plugin_runtime.maibot_client as module

    first, second = connection(), connection()
    first._remember_conversation(123)
    assert first._conversation_is_recent(123)
    assert not second._conversation_is_recent(123)
    now = time.monotonic()
    monkeypatch.setattr(module.time, "monotonic", lambda: now + module.RECENT_CONVERSATION_TTL_SECONDS + 1)
    assert not first._conversation_is_recent(123)


def test_two_live_connections_in_one_binding_keep_tokens_routes_and_shutdown_separate():
    async def run():
        loop = asyncio.get_running_loop()
        daily_received, hobby_replied, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        captured, handled = {}, []
        registry = MaiBotClientRegistry()

        def handler(key, conversation_id, package):
            handled.append((key.connection_id, conversation_id))
            loop.call_soon_threadsafe(hobby_replied.set)
            if len(handled) == 2: loop.call_soon_threadsafe(completed.set)

        async def server(ws):
            name = ws.request.path.strip("/")
            captured[name] = {"token": ws.request.headers.get("Authorization"), "messages": []}
            async for raw in ws:
                captured[name]["messages"].append(json.loads(raw))
                if name == "daily":
                    # 第一条连接保持安静，不应阻塞第二条的消息和回复。
                    daily_received.set()
                    continue
                # 第二条连接不能回复仅由第一条转交过的会话。
                await ws.send(json.dumps(reply(conversation_id=111)))
                response = reply(conversation_id=222)
                response["message_info"]["message_id"] = f"hobby-{len(captured[name]['messages'])}"
                await ws.send(json.dumps(response))

        async with serve(server, "127.0.0.1", 0) as service:
            port = service.sockets[0].getsockname()[1]
            keys = {name: MaiBotConnectionKey(1, 10, name) for name in ("daily", "hobby")}
            configs = {name: MaiBotConnectionConfig(ws_url=f"ws://127.0.0.1:{port}/{name}",
                        api_key=name + "-test-token", platform_name="fulcrum-test") for name in keys}
            try:
                for name, conversation_id in (("daily", 111), ("hobby", 222)):
                    assert registry.submit(key=keys[name], config=configs[name], api_message=message(conversation_id=conversation_id),
                                           conversation_id=conversation_id, reply_handler=handler)
                await asyncio.wait_for(daily_received.wait(), 5)
                await asyncio.wait_for(hobby_replied.wait(), 5)
                await asyncio.to_thread(registry.stop, keys["daily"])
                assert registry.status(keys["hobby"])["state"] == "connected"
                assert registry.submit(key=keys["hobby"], config=configs["hobby"], api_message=message(conversation_id=222),
                                       conversation_id=222, reply_handler=handler)
                await asyncio.wait_for(completed.wait(), 5)
                assert handled == [("hobby", 222), ("hobby", 222)]
                assert captured["daily"]["token"] == "daily-test-token" and len(captured["daily"]["messages"]) == 1
                assert captured["hobby"]["token"] == "hobby-test-token" and len(captured["hobby"]["messages"]) == 2
            finally:
                await asyncio.to_thread(registry.stop_all)

    asyncio.run(run())
