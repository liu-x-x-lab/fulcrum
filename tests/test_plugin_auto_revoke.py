"""公共自动撤回：有界内存计时、真实发送回执、幂等及消息归属，不连接生产手机。"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.entities import Message, TaskJob
from app.services import plugin_auto_revoke as revoke, plugin_directory
from app.services.message_actions import settle_revoke
from app.services.wechat_webridge import service
from tests.test_webridge_outbound_record import env  # noqa: F401


POLICY = {"enabled": True, "min_seconds": 60, "max_seconds": 80}


@pytest.fixture(autouse=True)
def isolated_queue(monkeypatch):
    """每条测试使用自己的内存队列，不启动等待线程，也不污染应用全局计时状态。"""

    queue = revoke.AutoRevokeQueue()
    monkeypatch.setattr(revoke, "queue", queue)
    return queue


def _remember(queue, message_id=1, *, tenant_id=1, now=0, policy=None):
    return queue.remember(tenant_id=tenant_id, message_id=message_id,
        channel_id=1, conversation_id=3, policy=policy or POLICY, now=now)


def test_each_message_starts_after_confirmation_and_duplicate_receipts_do_not_reset(monkeypatch):
    """接收成功回执才取随机数；重复回执不延迟已安排的撤回。"""

    queue = revoke.AutoRevokeQueue()
    draws = iter((60, 80))
    monkeypatch.setattr(revoke.random, "randint", lambda low, high: next(draws))
    _remember(queue, 1)
    _remember(queue, 2)
    assert queue.take_due(now=100) == []
    queue.confirm(1, 1, ok=True, has_native_id=True, now=100)
    queue.confirm(1, 2, ok=True, has_native_id=True, now=100)
    queue.confirm(1, 1, ok=True, has_native_id=True, now=120)
    assert queue.take_due(now=159) == []
    assert [item.message_id for item in queue.take_due(now=160)] == [1]
    assert [item.message_id for item in queue.take_due(now=180)] == [2]
    assert len(queue) == 0


def test_capacity_receipt_timeout_failed_send_and_missing_ids_are_bounded():
    queue = revoke.AutoRevokeQueue(capacity=2)
    assert _remember(queue, 1) and _remember(queue, 2)
    assert not _remember(queue, 3)
    queue.confirm(1, 1, ok=False, has_native_id=True, now=1)
    queue.confirm(1, 2, ok=True, has_native_id=False, now=1)
    assert len(queue) == 0
    assert _remember(queue, 3)
    assert queue.take_due(now=revoke.RECEIPT_TIMEOUT_SECONDS + 1) == []
    assert len(queue) == 0
    queue.confirm(1, 3, ok=True, has_native_id=True, now=1000)
    assert len(queue) == 0  # 迟到回执不能复活已经过期的意图。


def test_bulk_countdowns_use_a_bounded_heap_and_deliver_in_batches(monkeypatch):
    """五千条同时计时只操作标量队列，不创建线程；批量领取不会重复或遗漏。"""

    queue = revoke.AutoRevokeQueue(capacity=5000)
    monkeypatch.setattr(revoke.random, "randint", lambda low, high: low)
    monkeypatch.setattr(revoke.threading, "Thread", lambda *args, **kwargs: pytest.fail("每条消息不得创建计时线程"))
    for index in range(5000):
        assert _remember(queue, index)
        queue.confirm(1, index, ok=True, has_native_id=True, now=1)
    assert queue.take_due(now=60) == []
    received = []
    while len(queue):
        batch = queue.take_due(now=61)
        assert 0 < len(batch) <= revoke.BATCH_SIZE
        received.extend(item.message_id for item in batch)
    assert len(received) == len(set(received)) == 5000
    # 旧的回执超时堆项会按阈值压缩，不随累计发送量无限增长。
    for index in range(10000, 15000):
        _remember(queue, index)
        queue.confirm(1, index, ok=False, has_native_id=False, now=1)
    assert len(queue._heap) <= 128
    queue.clear()
    assert queue._heap == []


def test_memory_keys_include_tenant_and_expired_ready_items_do_not_touch_database(monkeypatch):
    queue = revoke.AutoRevokeQueue()
    monkeypatch.setattr(revoke.random, "randint", lambda low, high: low)
    _remember(queue, 1, tenant_id=1)
    _remember(queue, 1, tenant_id=2)
    queue.confirm(1, 1, ok=True, has_native_id=True, now=1)
    assert queue.take_due(now=122) == []
    assert len(queue) == 1  # 第二个租户的同编号消息仍在等自己的回执。
    queue.clear()
    assert not queue.take_due(now=1000)


def test_worker_is_singleton_and_stop_discards_all_countdowns(monkeypatch, isolated_queue):
    """共享线程重复启动仍只有一个，停止时唤醒等待并丢弃内存，不创建恢复任务。"""

    _remember(isolated_queue)
    revoke.start_auto_revoke_worker()
    first = revoke._thread
    try:
        revoke.start_auto_revoke_worker()
        assert revoke._thread is first and first.is_alive()
    finally:
        revoke.stop_auto_revoke_worker()
    assert not first.is_alive() and len(isolated_queue) == 0


@pytest.mark.parametrize("patch", [
    {"min_seconds": 0}, {"max_seconds": 111}, {"min_seconds": 81},
    {"min_seconds": "60.5"}, {"min_seconds": True}, {"enabled": "maybe"},
    {"max_seconds": float("nan")}, {"unknown": True},
])
def test_invalid_policies_are_rejected(patch):
    with pytest.raises(ValueError):
        revoke.auto_revoke_policy({revoke.CONFIG_KEY: {**POLICY, **patch}})


def _reply(db, channel, conversation, *, policy=POLICY, message_type="text"):
    context = SimpleNamespace(
        plugin_code="message_sign", conversation_id=conversation.id,
        conversation_external_id=conversation.external_id, chat_type=conversation.chat_type,
        normalized_event={}, channel_type=channel.channel_type, message_type="text",
        binding_config={revoke.CONFIG_KEY: policy}, sender_id="wxid_a",
    )
    payload = {"message_type": message_type, "content": "测试回复"}
    if message_type == "image":
        payload["image_url"] = "https://example.test/image.png"
    result = plugin_directory._dispatch_auto_outbound_messages(db, SimpleNamespace(channel_id=channel.id),
        context, {"outbound_messages": [payload]})[0]
    assert result["ok"], result
    return db.get(Message, result["message_id"]), db.get(TaskJob, int(result["job_id"]))


@pytest.mark.parametrize("message_type", ["text", "image"])
def test_plugin_reply_receipt_schedules_in_memory_and_reuses_real_revoke_command(env, isolated_queue, monkeypatch, message_type):
    db, channel, conversation = env
    monkeypatch.setattr(revoke.random, "randint", lambda low, high: 60)
    message, job = _reply(db, channel, conversation, message_type=message_type)
    assert len(isolated_queue) == 1
    assert isolated_queue.take_due(now=time.monotonic() + 100) == []
    assert len(db.scalars(select(TaskJob)).all()) == 1  # 只有原发送命令，没有持久化计时任务。
    # 排队十分钟不消耗实际发送后的撤回窗口。
    from datetime import timedelta
    message.created_at = now_shanghai() - timedelta(minutes=10)
    service.settle_outbound_message(db, job, ok=True, result={"message_id": "native-001"})
    assert isolated_queue.take_due(now=time.monotonic() + 65) == []  # 未提交回执不触发。
    db.commit()
    due = isolated_queue.take_due(now=time.monotonic() + 65)
    assert len(due) == 1 and message.external_message_id == "native-001"
    assert "sent_at" in json.loads(message.raw_request_json)
    assert revoke.CONFIG_KEY not in json.loads(message.raw_request_json)
    assert revoke.execute_due_revoke(db, due[0])["ok"]
    db.commit()
    jobs = db.scalars(select(TaskJob).order_by(TaskJob.id)).all()
    assert len(jobs) == 2 and message.status == "sent"  # 只是请求撤回，不能提前隐藏。
    command = json.loads(jobs[-1].payload_json)
    assert command["revoke_message_id"] == message.id
    assert command["command"]["params"]["message_id"] == "native-001"
    assert revoke.execute_due_revoke(db, due[0])["ok"]
    db.commit()
    assert len(db.scalars(select(TaskJob)).all()) == 2  # 重复执行也只会有一条撤回命令。
    assert settle_revoke(db, jobs[-1], ok=True)
    db.commit()
    assert message.status == "revoked"


@pytest.mark.parametrize("ok,native_id", [(False, ""), (True, "")])
def test_failed_or_unidentifiable_sends_remove_the_memory_intent(env, isolated_queue, ok, native_id):
    db, channel, conversation = env
    message, job = _reply(db, channel, conversation)
    service.settle_outbound_message(db, job, ok=ok, result={"message_id": native_id})
    db.commit()
    assert len(isolated_queue) == 0
    assert isolated_queue.take_due(now=time.monotonic() + 80) == []
    assert len(db.scalars(select(TaskJob)).all()) == 1


def test_disabled_policy_does_not_keep_per_message_memory(env, isolated_queue):
    db, channel, conversation = env
    _reply(db, channel, conversation, policy={**POLICY, "enabled": False})
    assert len(isolated_queue) == 0


def test_rolled_back_send_registration_is_removed_without_waiting_for_ttl(env, isolated_queue):
    db, channel, conversation = env
    message = Message(tenant_id=channel.tenant_id, conversation_id=conversation.id,
        direction="out", content="不会提交的发送记录", status="sent", external_status="pending")
    db.add(message)
    db.flush()
    revoke.remember_outbound(channel, conversation, message, POLICY)
    assert len(isolated_queue) == 1
    db.rollback()
    assert len(isolated_queue) == 0


def test_receipt_finds_older_reply_among_many_pending_sends(env, isolated_queue):
    db, channel, conversation = env
    message, job = _reply(db, channel, conversation)
    db.add_all([Message(tenant_id=channel.tenant_id, conversation_id=conversation.id, direction="out",
        content="其他排队消息", status="sent", external_status="pending",
        raw_request_json=json.dumps({"request_id": f"other-{index}"})) for index in range(60)])
    db.commit()
    service.settle_outbound_message(db, job, ok=True, result={"message_id": "older-native"})
    db.commit()
    assert message.external_status == "sent" and message.external_message_id == "older-native"


def test_due_item_cannot_revoke_another_tenant_or_conversation(env, isolated_queue):
    db, channel, conversation = env
    message, job = _reply(db, channel, conversation)
    service.settle_outbound_message(db, job, ok=True, result={"message_id": "native-scope"})
    db.commit()
    item = isolated_queue.take_due(now=time.monotonic() + 81)[0]
    for forged in (replace(item, tenant_id=2), replace(item, conversation_id=999), replace(item, channel_id=999)):
        assert revoke.execute_due_revoke(db, forged)["reason"] == "message_scope_mismatch"
    assert len(db.scalars(select(TaskJob)).all()) == 1


def test_guga_receipt_saves_native_ids_and_uses_same_countdown(env, isolated_queue):
    """咕嘎消息使用手机本地/服务器编号，不能拿请求 UUID 冒充撤回编号。"""

    from app.services.channel_adapters.service import dispatch_outbound_message
    from app.services.guga_agent import service as guga

    db, channel, conversation = env
    channel.channel_type = conversation.channel_type = "wechat_gugatool"
    channel.runtime_info_json = json.dumps({"guga_agent": {
        "device_id": "g1", "session_id": "s1", "account_id": "wxid_bot",
    }})
    db.commit()
    message, result = dispatch_outbound_message(db, channel, conversation,
        {"message_type": "text", "content": "咕嘎回复"}, auto_revoke=POLICY)
    assert result["ok"], result
    job = db.scalar(select(TaskJob).order_by(TaskJob.id.desc()))
    guga._settle_outbound_message(db, job, ok=True, result={"Data": {"LocalId": 33300, "MsgSvrId": 9000001}})
    db.commit()
    item = isolated_queue.take_due(now=time.monotonic() + 81)[0]
    assert revoke.execute_due_revoke(db, item)["ok"]
    db.commit()
    revoke_job = db.scalar(select(TaskJob).order_by(TaskJob.id.desc()))
    params = json.loads(revoke_job.payload_json)["command"]["params"]
    assert params["ToWxid"] == conversation.external_id
    assert params["LocalId"] == 33300 and params["MsgSvrId"] == 9000001


@pytest.mark.parametrize("kind", ["wechat_webridge", "wechat_gugatool"])
def test_queued_manual_and_scheduled_replies_use_current_group_policy(env, isolated_queue, kind):
    """主动发送的队列入口同样应用群策略，插件载荷不能覆盖平台开关。"""

    from app.models.entities import BotPlugin, Chatroom, GroupPluginScope
    from app.services.async_workers import _process_outbound_dispatch_job
    from app.services.outbound_batch import enqueue_outbound_job
    from app.services.task_queue import parse_job_payload

    db, channel, conversation = env
    channel.channel_type = conversation.channel_type = kind
    conversation.chat_type = "group"
    if kind == "wechat_gugatool":
        channel.runtime_info_json = json.dumps({"guga_agent": {
            "device_id":"g1", "session_id":"s1", "account_id":"wxid_bot"}})
    binding = db.scalar(select(BotPlugin))
    binding.config_json = json.dumps({revoke.CONFIG_KEY: POLICY})
    room = Chatroom(tenant_id=channel.tenant_id, bot_id=channel.bot_id, channel_id=channel.id,
        external_id=conversation.external_id, name="撤回覆盖测试群", is_active=True, management_status="enabled")
    db.add(room)
    db.flush()
    scope = GroupPluginScope(tenant_id=channel.tenant_id, channel_id=channel.id, chatroom_id=room.id,
        bot_plugin_id=binding.id, enabled=True, scope_status="overridden",
        config_override_json=json.dumps({revoke.CONFIG_KEY: {"enabled":False}}))
    db.add(scope)
    db.commit()

    def send():
        job = enqueue_outbound_job(db, channel=channel, conversation=conversation,
            payload={"message_type":"text", "content":"主动回复", revoke.CONFIG_KEY: POLICY},
            plugin_code="message_sign", related_type="bot_plugin", related_id=binding.id, priority=90)
        db.commit()
        payload = parse_job_payload(job)
        assert "auto_revoke" not in payload
        return _process_outbound_dispatch_job(db, job, payload)

    assert send()["adapter_result"]["ok"]
    assert len(isolated_queue) == 0
    scope.config_override_json = json.dumps({revoke.CONFIG_KEY: {"enabled":True, "min_seconds":5}})
    db.commit()
    result = send()
    assert result["adapter_result"]["ok"] and len(isolated_queue) == 1
    message = db.get(Message, result["message_id"])
    assert revoke.CONFIG_KEY not in json.loads(message.raw_request_json)
    isolated_queue.clear()  # 重启允许丢弃，数据库记录不会重建计时任务。
    assert isolated_queue.take_due(now=time.monotonic() + 1000) == []
