"""群治理以临时 SQLite、虚构群成员和命令回报验证，不连接真实手机。"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, Chatroom, ChatroomMember, Conversation, ErrorLog, TaskJob
from app.models.group_moderation import GroupModerationViolation as Violation
from app.models.saas import Tenant
from app.services.group_features.moderation import FEATURE
from app.services.group_features.store import get_state, save_state
from app.services.group_features.view import build_feature_cards, save_from_form
from app.services.group_moderation.service import check_message
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import service as bridge

IDENTITY = {"device_id": "moderation-device", "session_id": "moderation-session", "account_id": "robot"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'moderation.db'}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as seed:
        seed.add_all([Tenant(id=1, code="moderation-one", name="测试租户一"), Tenant(id=2, code="moderation-two", name="测试租户二")])
        seed.commit()
    with tenant_scope(1), tenant_session(factory) as db:
        bot = Bot(name="测试机器人")
        db.add(bot)
        db.flush()
        channel = Channel(bot_id=bot.id, name="测试账号", channel_type="wechat_webridge", is_enabled=True,
                          runtime_info_json=json.dumps({"webridge": {**IDENTITY, "capabilities": ["kick_group_member"]}}))
        db.add(channel)
        db.flush()
        room = Chatroom(bot_id=bot.id, channel_id=channel.id, external_id="moderation@chatroom", name="测试群",
                        is_active=True, status="synced", management_status="enabled", owner_external_id="owner")
        conversation = Conversation(bot_id=bot.id, channel_id=channel.id, external_id=room.external_id,
                                    channel_type="wechat_webridge", chat_type="group")
        db.add_all([room, conversation])
        db.flush()
        for member_id, role in (("robot", "admin"), ("owner", "owner"), ("alice", "member"), ("bob", "member")):
            db.add(ChatroomMember(bot_id=bot.id, channel_id=channel.id, chatroom_id=room.id,
                                  member_external_id=member_id, nickname=member_id, role=role, status="active"))
        save_state(room, "moderation", enabled=True, config={"words": "BAD\n广告测试", "threshold": 2})
        db.commit()
        monkeypatch.setattr(bridge, "_poll_maintenance", lambda *args: None)
        yield SimpleNamespace(db=db, channel=channel, room=room, conversation=conversation, factory=factory)
    engine.dispose()


def configure(env, **updates):
    enabled, config = get_state(env.room, FEATURE)
    save_state(env.room, "moderation", enabled=enabled, config={**config, **updates})
    env.db.commit()


def event(key="one", sender="alice", content="ｂａｄ 广告测试"):
    return {"event_key": key, "conversation": {"external_id": "moderation@chatroom", "chat_type": "group"},
            "message": {"external_message_id": key, "message_type": "text", "sender_id": sender, "content": content}}


def check(env, key="one", sender="alice", content="ｂａｄ 广告测试"):
    incoming = event(key, sender, content)
    if env.channel.channel_type == "wechat_ipad":
        incoming["details"] = {"timestamp": int(now_shanghai().timestamp()), "direction": "in", "msg_type": 1,
                               "from_user_name": env.room.external_id, "to_user_name": "robot"}
    result = check_message(env.db, env.channel, incoming, conversation_id=env.conversation.id)
    env.db.commit()
    return result


def records(env):
    return list(env.db.scalars(select(Violation).order_by(Violation.id)))


def notices(env):
    return list(env.db.scalars(select(TaskJob).where(TaskJob.related_type == "group_moderation_notice").order_by(TaskJob.id)))


def test_one_message_counts_once_even_with_many_words_and_duplicate_delivery(env):
    assert check(env)["count"] == 1
    assert check(env)["deduplicated"] is True
    assert len(records(env)) == len(notices(env)) == 1
    assert check(env, "two")["count"] == 2
    assert check(env, "three", "bob")["count"] == 1


@pytest.mark.parametrize("case", ["disabled", "inactive", "self", "expired", "direct", "unmatched", "owner", "admin", "exempt"])
def test_ineligible_messages_do_not_produce_strikes(env, case):
    incoming = event()
    if case == "disabled":
        save_state(env.room, "moderation", enabled=False)
    elif case == "inactive":
        env.room.is_active = False
    elif case == "self":
        incoming["details"] = {"from_self": True}
    elif case == "expired":
        incoming["details"] = {"timestamp": (now_shanghai() - timedelta(hours=1)).timestamp()}
    elif case == "direct":
        env.conversation.chat_type = "direct"
    elif case == "unmatched":
        incoming["message"]["content"] = "你好"
    elif case in {"owner", "admin"}:
        incoming["message"]["sender_id"] = "owner" if case == "owner" else "robot"
    elif case == "exempt":
        configure(env, exempt_members="alice")
    env.db.commit()
    assert check_message(env.db, env.channel, incoming, conversation_id=env.conversation.id) is None
    assert records(env) == notices(env) == []


def test_rolling_window_and_lifetime_counts(env):
    check(env)
    records(env)[0].created_at = now_shanghai() - timedelta(hours=25)
    env.db.commit()
    assert check(env, "two")["count"] == 1
    configure(env, window_hours=0)
    assert check(env, "three")["count"] == 3


def test_native_feature_form_and_config_validation(env):
    save_from_form(env.room, "moderation", {"enabled": "true", "cfg_words": "广告测试", "cfg_mode": "kick",
                                            "cfg_threshold": "4", "cfg_window_hours": "48", "cfg_warning_text": "{member}:{count}/{limit}"})
    enabled, config = get_state(env.room, FEATURE)
    assert enabled and config["threshold"] == 4 and config["window_hours"] == 48
    assert build_feature_cards(env.room, "wechat_webridge")[0]["key"] == "moderation"
    with pytest.raises(ValueError, match="提示语仅支持"):
        configure(env, warning_text="{secret}")
    with pytest.raises(ValueError, match="128"):
        configure(env, words="a" * 129)


def test_moderation_blocks_features_and_plugin_execution(env, monkeypatch):
    from app.services import async_workers
    def should_not_run(*args, **kwargs):
        pytest.fail("匹配后的消息不应进入普通群功能或插件")
    monkeypatch.setattr(async_workers, "dispatch_group_features", should_not_run)
    monkeypatch.setattr(async_workers, "build_dispatch_report", should_not_run)
    result = async_workers._process_plugin_dispatch_job(env.db, {
        "channel_id": env.channel.id, "conversation_id": env.conversation.id, "normalized_event": event(),
    })
    assert result["executed_plugins"] == [] and result["dispatch"]["blocked"] is True


def test_cross_tenant_and_cross_conversation_cannot_punish(env):
    with tenant_scope(2):
        with pytest.raises(ValueError, match="tenant_mismatch"):
            check(env)
    incoming = event()
    incoming["conversation"]["external_id"] = "another@chatroom"
    assert check_message(env.db, env.channel, incoming, conversation_id=env.conversation.id) is None
    assert records(env) == []


def test_missing_permissions_warns_and_records_failure_without_kick(env):
    configure(env, mode="kick", threshold=1)
    robot = env.db.scalar(select(ChatroomMember).where(ChatroomMember.member_external_id == "robot"))
    robot.role = "member"
    env.db.commit()
    assert check(env)["status"] == "failed"
    assert records(env)[0].action_job_id is None
    assert len(notices(env)) == 1
    assert "群管理权限" in env.db.scalar(select(ErrorLog)).message


def test_kick_threshold_enqueues_once_and_waits_for_phone(env):
    configure(env, mode="kick")
    assert check(env)["status"] == "warning"
    assert check(env, "two")["status"] == "pending"
    violation = records(env)[1]
    job = env.db.get(TaskJob, violation.action_job_id)
    assert job.related_type == "group_moderation" and job.max_attempts == 1
    assert len(notices(env)) == 1  # 此时还不能发“已踢出”。
    check(env, "three")
    assert env.db.scalar(select(func.count(TaskJob.id)).where(TaskJob.related_type == "group_moderation")) == 1


def test_rollback_keeps_count_and_notice_atomic(env):
    check_message(env.db, env.channel, event(), conversation_id=env.conversation.id)
    env.db.rollback()
    assert records(env) == notices(env) == []
    assert check(env)["count"] == 1


def pending_kick(env):
    configure(env, mode="kick", threshold=1)
    check(env)
    violation = records(env)[0]
    return violation, env.db.get(TaskJob, violation.action_job_id)


def reply(env, job, *, ok=True, result=None):
    command = parse_job_payload(job)["command"]
    return bridge.accept_command_result(env.db, env.channel, {
        **IDENTITY, "request_id": command["request_id"], "ok": ok,
        "result": result if result is not None else {
            "room_id": env.room.external_id, "member_ids": ["alice"], "member_count": 1,
        },
    })


def test_documented_command_and_result_confirm_once(env):
    violation, job = pending_kick(env)
    commands = bridge.claim_commands(env.db, env.channel, IDENTITY)
    assert len(commands) == 1
    assert commands[0]["command_name"] == "kick_group_member"
    assert commands[0]["args"] == {"room_id": env.room.external_id, "member_id": "alice"}
    assert bridge.claim_commands(env.db, env.channel, IDENTITY) == []
    assert reply(env, job)["status"] == "completed"
    assert violation.status == "kicked" and len(notices(env)) == 1
    member = env.db.scalar(select(ChatroomMember).where(ChatroomMember.member_external_id == "alice"))
    assert member.status == "left"
    assert reply(env, job)["deduplicated"] is True
    assert len(notices(env)) == 1


@pytest.mark.parametrize("change", ["disabled", "account_off", "exempt", "permission", "threshold", "window", "expired", "capability"])
def test_queued_removal_rechecks_authorization_and_original_age(env, change):
    violation, job = pending_kick(env)
    if change == "disabled":
        save_state(env.room, "moderation", enabled=False)
    elif change == "account_off":
        env.channel.is_enabled = False
    elif change == "exempt":
        configure(env, exempt_members="alice")
    elif change == "permission":
        env.db.scalar(select(ChatroomMember).where(ChatroomMember.member_external_id == "robot")).role = "member"
    elif change == "threshold":
        configure(env, threshold=10)
    elif change == "window":
        violation.created_at = now_shanghai() - timedelta(hours=2)
        configure(env, window_hours=1)
    elif change == "capability":
        env.channel.runtime_info_json = json.dumps({"webridge": {**IDENTITY, "capabilities": []}})
    else:
        data = parse_job_payload(job)
        data["moderation_expires_at"] = (now_shanghai() - timedelta(seconds=1)).isoformat()
        job.payload_json = json.dumps(data)
    env.db.commit()
    assert bridge.claim_commands(env.db, env.channel, IDENTITY) == []
    assert job.status == violation.status == "cancelled"
    assert notices(env) == []


@pytest.mark.parametrize("ok,result,status", [
    (False, {"ok": True}, "failed"),
    (True, {"ok": False}, "failed"),
    (True, {}, "unknown"),
    (True, {"room_id": "other@chatroom", "member_ids": ["alice"], "member_count": 1}, "unknown"),
    (True, {"room_id": "moderation@chatroom", "member_ids": ["bob"], "member_count": 1}, "unknown"),
    (False, {"error_code": "OPERATION_TIMEOUT"}, "unknown"),
])
def test_incomplete_or_failed_result_never_announces_success(env, ok, result, status):
    violation, job = pending_kick(env)
    bridge.claim_commands(env.db, env.channel, IDENTITY)
    assert reply(env, job, ok=ok, result=result)["status"] == "failed"
    assert violation.status == status and notices(env) == []
    member = env.db.scalar(select(ChatroomMember).where(ChatroomMember.member_external_id == "alice"))
    assert member.status == "active"
    if status == "unknown":
        check(env, "two")
        assert env.db.scalar(select(func.count(TaskJob.id)).where(TaskJob.related_type == "group_moderation")) == 1


def test_timeout_never_resends_but_late_evidence_settles(env):
    violation, job = pending_kick(env)
    bridge.claim_commands(env.db, env.channel, IDENTITY)
    job.locked_at = now_shanghai() - timedelta(hours=1)
    env.db.commit()
    assert bridge.reap_stale_commands(env.db, env.channel) == 1
    assert violation.status == "unknown"
    assert notices(env) == []
    check(env, "two")
    assert bridge.claim_commands(env.db, env.channel, IDENTITY) == []
    assert reply(env, job)["status"] == "completed"
    assert violation.status == "kicked"
    assert reply(env, job)["deduplicated"] is True
    assert len(notices(env)) == 2  # 后续消息的警告 + 一次成功提示。


def test_different_senders_may_share_external_message_number(env):
    assert check(env, "shared", "alice")["count"] == 1
    assert check(env, "shared", "bob")["count"] == 1
    assert len(records(env)) == 2


def test_concurrent_messages_count_without_lost_updates(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    gate = Barrier(2)
    channel_id, conversation_id = env.channel.id, env.conversation.id
    env.db.commit()

    def run(key):
        with tenant_scope(1), tenant_session(env.factory) as db:
            channel = db.get(Channel, channel_id)
            gate.wait(timeout=10)
            result = check_message(db, channel, event(key), conversation_id=conversation_id)
            db.commit()
            return result["count"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, key) for key in ("a", "b")]
        assert sorted(f.result(timeout=20) for f in futures) == [1, 2]
    assert len(records(env)) == 2


def test_unsupported_device_cannot_enqueue_removal(env):
    env.channel.runtime_info_json = json.dumps({"webridge": {**IDENTITY, "capabilities": []}})
    configure(env, mode="kick", threshold=1)
    assert check(env)["status"] == "failed"
    assert records(env)[0].action_job_id is None


def test_action_expiry_uses_incoming_timestamp(env):
    configure(env, mode="kick", threshold=1)
    incoming = event()
    original = now_shanghai() - timedelta(minutes=9)
    incoming["details"] = {"timestamp": original.isoformat()}
    check_message(env.db, env.channel, incoming, conversation_id=env.conversation.id)
    env.db.commit()
    data = parse_job_payload(env.db.get(TaskJob, records(env)[0].action_job_id))
    assert data["moderation_expires_at"] == (original + timedelta(minutes=10)).isoformat()


def test_notice_worker_creates_a_real_message_and_phone_command(env):
    from app.services.async_workers import _process_outbound_dispatch_job
    from app.models.entities import Message

    check(env)
    job = notices(env)[0]
    result = _process_outbound_dispatch_job(env.db, job, parse_job_payload(job))
    assert result["message_id"]
    message = env.db.get(Message, result["message_id"])
    assert message.external_status == "pending"
    commands = bridge.claim_commands(env.db, env.channel, IDENTITY)
    assert len(commands) == 1 and commands[0]["command_name"] == "send_message"
    assert commands[0]["args"]["receiver"] == env.room.external_id
    bridge.accept_command_result(env.db, env.channel, {
        **IDENTITY, "request_id": commands[0]["request_id"], "ok": True, "result": {"ok": True},
    })
    assert message.external_status == "sent"


def test_queued_notice_is_skipped_after_feature_is_disabled(env):
    from app.services.async_workers import _process_outbound_dispatch_job

    check(env)
    job = notices(env)[0]
    save_state(env.room, "moderation", enabled=False)
    env.db.commit()
    result = _process_outbound_dispatch_job(env.db, job, parse_job_payload(job))
    assert result == {"skipped": True, "reason": "plugin_scope_disabled"}
    assert bridge.claim_commands(env.db, env.channel, IDENTITY) == []


def test_result_cannot_substitute_numeric_job_id_for_saved_request(env):
    violation, job = pending_kick(env)
    bridge.claim_commands(env.db, env.channel, IDENTITY)
    with pytest.raises(ValueError, match="request_mismatch"):
        bridge.accept_command_result(env.db, env.channel, {**IDENTITY, "request_id": f"job_{job.id}", "ok": True})
    assert violation.status == "pending"


@pytest.fixture
def ipad(env, monkeypatch):
    from app.services.group_moderation import ipad as worker
    from app.services.task_queue import claim_next_job

    env.channel.channel_type = env.conversation.channel_type = "wechat_ipad"
    env.channel.adapter_config_json = '{"wxid":"robot"}'
    env.channel.runtime_info_json = "{}"
    env.channel.api_base = "https://example.test"
    configure(env, mode="kick", threshold=1)
    calls = []
    room_id = env.room.external_id
    rows = [{"UserName": key, "ChatroomMemberFlag": flag} for key, flag in (("robot", 2049), ("owner", 25), ("alice", 1))]
    data = {"baseResponse": {"ret": 0}, "MemberCount": 1, "MemberList": [{"MemberName": {"string": "alice"}}]}
    pre = SimpleNamespace(hook=None, timeout=False)

    def request(client, target, endpoint, *, body):
        assert target.api_base == "https://example.test"
        calls.append(endpoint)
        if endpoint.endswith("/GetChatRoomInfo"):
            assert body == {"Wxid": "robot", "QID": room_id}
            result = {"BaseResponse": {"ret": 0}, "ContactList": [
                {"UserName": {"string": room_id}, "ChatRoomOwner": "owner"}]}
        elif endpoint.endswith("/GetChatRoomMemberDetail"):
            if pre.hook:
                pre.hook()
            result = {"BaseResponse": {"ret": 0}, "ChatroomUserName": room_id, "ServerVersion": 10,
                      "NewChatroomData": {"MemberCount": len(rows), "ChatRoomMember": rows}}
        else:
            assert endpoint == "/api/Group/DelChatRoomMember" and body == {"Wxid": "robot", "ChatRoomName": room_id, "ToWxids": "alice"}
            if pre.timeout:
                raise TimeoutError
            result = data
        return {"Success": True, "Code": 0, "Data": result}

    monkeypatch.setattr(worker.WechatIpadApiClient, "request_json", request)
    def run():
        job = claim_next_job(env.db, job_types=(worker.JOB_TYPE,))
        assert job
        worker.process(env.db, job, parse_job_payload(job))
        return job
    return SimpleNamespace(run=run, data=data, rows=rows, calls=calls, pre=pre)


@pytest.mark.parametrize("case,expected", [
    ("success", "kicked"), ("denied", "failed"), ("timeout", "unknown"), ("wrong_target", "unknown"),
    ("missing_inner", "unknown"), ("extra_member", "unknown"), ("promoted", "cancelled"),
    ("unknown_role", "cancelled"), ("permission_lost", "cancelled"), ("setting_changed", "cancelled"),
    ("account_changed", "cancelled"), ("expired", "cancelled"), ("restart", "unknown"),
])
def test_ipad_removal_rechecks_then_settles_without_replaying(env, ipad, case, expected):
    from app.services.group_moderation import ipad as worker
    assert check(env)["status"] == "pending"
    violation, = records(env)
    if case == "denied":
        ipad.data["baseResponse"]["ret"] = -1
    elif case == "missing_inner":
        del ipad.data["baseResponse"]
    elif case == "wrong_target":
        ipad.data["MemberList"][0]["MemberName"]["string"] = "bob"
    elif case == "extra_member":
        ipad.data["MemberList"] *= 2
    elif case == "timeout":
        ipad.pre.timeout = True
    elif case in {"promoted", "unknown_role", "permission_lost"}:
        ipad.rows[0 if case == "permission_lost" else 2]["ChatroomMemberFlag"] = {"promoted": 2057, "unknown_role": 999, "permission_lost": 1}[case]
    elif case == "setting_changed":
        ipad.pre.hook = lambda: configure(env, exempt_members="alice")
    elif case == "account_changed":
        env.channel.runtime_info_json = '{"active_wxid":"other"}'
    elif case == "expired":
        job = env.db.get(TaskJob, violation.action_job_id)
        job.payload_json = json.dumps({**parse_job_payload(job), "moderation_expires_at": (now_shanghai() - timedelta(seconds=1)).isoformat()})
    elif case == "restart":
        # 模拟下发标记已提交但回执尚未落库时退出，恢复后只能结算，不能发请求。
        violation.status = "unknown"
    env.db.commit()
    job = ipad.run()
    assert violation.status == expected
    assert len(notices(env)) == int(expected == "kicked")
    assert ipad.calls.count("/api/Group/DelChatRoomMember") == int(expected != "cancelled" and case != "restart")
    original = list(ipad.calls)
    worker.process(env.db, job, parse_job_payload(job))
    assert ipad.calls == original
    if expected == "unknown":
        check(env, "another")
        assert env.db.scalar(select(func.count(TaskJob.id)).where(TaskJob.job_type == worker.JOB_TYPE)) == 1


def test_legacy_ipad_kick_configuration_warns_until_explicit_save(env, ipad):
    extra = json.loads(env.room.extra_json)
    del extra["group_features"]["moderation"]["ipad_removal_confirmed"]
    env.room.extra_json = json.dumps(extra)
    env.db.commit()
    assert check(env)["status"] == "warning"
    assert records(env)[0].action_job_id is None
    configure(env, mode="kick")
    assert check(env, "new")["status"] == "pending"


def test_ipad_unverified_source_does_not_count_towards_removal(env, ipad):
    for override in ({"timestamp": None}, {"timestamp": (now_shanghai() + timedelta(hours=1)).timestamp()},
                     {"from_user_name": "other@chatroom"}, {"to_user_name": "other"}, {"msg_type": 10000}):
        incoming = event()
        incoming["details"] = {"timestamp": int(now_shanghai().timestamp()), "direction": "in", "msg_type": 1,
                               "from_user_name": env.room.external_id, "to_user_name": "robot", **override}
        assert check_message(env.db, env.channel, incoming, conversation_id=env.conversation.id)["reason"] == "unverified_ipad_message"
    assert not records(env) and not ipad.calls
