"""各渠道"消息 → 建群"入口的回归:首次建群、重复消息不重复、并发只建一个、已有设置不被冲掉。

背景:建群逻辑改为统一的并发安全入口(chatroom_directory)后,iPad、Windows 微信、企业微信、咕嘎、微桥都改了。
winauto/企微的既有测试夹具有历史问题(造机器人时缺租户上下文,在测试开头就失败),它们的建群代码
长期没有任何测试跑到;这里在正确的租户上下文里直接调用各渠道的真实入口,补上这块覆盖。
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timedelta, timezone
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="channel-room-logs-"))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import open_tenant_session
from app.models.entities import Bot, Channel, Chatroom, ChatroomMember, Conversation
from app.models.saas import Tenant
from app.services.wechat_gugatool_chatrooms import sync_wechat_gugatool_chatroom_from_message
from app.services.wechat_ipad.chatrooms import sync_wechat_ipad_chatroom_from_message, upsert_wechat_ipad_chatroom_profile
from app.services.wechat_ipad.operations import sync as ipad_sync
from app.services.wechat_winauto_chatrooms import sync_wechat_winauto_chatroom_from_message
from app.services.workwechat_chatrooms import sync_workwechat_chatroom_from_message

# (渠道类型, 入口函数)
ENTRIES = [
    ("wechat_ipad", sync_wechat_ipad_chatroom_from_message),
    ("wechat_winauto", sync_wechat_winauto_chatroom_from_message),
    ("workwechat", sync_workwechat_chatroom_from_message),
    ("wechat_gugatool", sync_wechat_gugatool_chatroom_from_message),
]


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rooms.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(Tenant(id=7, code="t", name="t"))
        db.commit()
    return factory


def _channel(db, channel_type: str) -> Channel:
    bot = Bot(name=f"bot-{channel_type}", tenant_id=7)
    db.add(bot)
    db.flush()
    channel = Channel(bot_id=bot.id, name=channel_type, channel_type=channel_type, is_enabled=True, tenant_id=7)
    db.add(channel)
    db.flush()
    return channel


def _event(room: str, sender: str = "wxid_a") -> dict:
    return {
        "conversation": {"external_id": room, "chat_type": "group", "title": "测试群"},
        "message": {"sender_id": sender, "sender_name": "甲", "content": "hi", "message_type": "text"},
        "details": {},
    }


def _conversation(db, channel: Channel, room: str) -> Conversation:
    conversation = Conversation(tenant_id=7, bot_id=channel.bot_id, channel_id=channel.id, channel_type=channel.channel_type, external_id=room, chat_type="group", title="测试群")
    db.add(conversation)
    db.flush()
    return conversation


@pytest.mark.parametrize("channel_type, entry", ENTRIES, ids=[item[0] for item in ENTRIES])
def test_first_message_creates_the_group_and_repeats_do_not_duplicate_it(env, channel_type, entry) -> None:
    room = "first@chatroom"
    with tenant_scope(7), open_tenant_session(env) as db:
        channel = _channel(db, channel_type)
        conversation = _conversation(db, channel, room)
        first = entry(db, channel, _event(room), conversation=conversation)
        second = entry(db, channel, _event(room, "wxid_b"), conversation=conversation)
        db.commit()
        assert first["chatroom_created"] is True and second["chatroom_created"] is False
        assert db.scalar(select(func.count(Chatroom.id)).where(Chatroom.external_id == room)) == 1
        assert first["chatroom_id"] == second["chatroom_id"] and first["chatroom_id"]


@pytest.mark.parametrize("channel_type, entry", ENTRIES, ids=[item[0] for item in ENTRIES])
def test_concurrent_first_messages_never_duplicate_the_group(env, channel_type, entry) -> None:
    room = "race@chatroom"
    with tenant_scope(7), open_tenant_session(env) as db:
        channel = _channel(db, channel_type)
        _conversation(db, channel, room)
        db.commit()
        channel_id = int(channel.id)
    barrier = threading.Barrier(6)
    errors, created = [], []

    def run(index: int) -> None:
        try:
            with tenant_scope(7), open_tenant_session(env) as db:
                channel = db.get(Channel, channel_id)
                conversation = db.scalar(select(Conversation).where(Conversation.external_id == room))
                barrier.wait()
                result = entry(db, channel, _event(room, f"wxid_{index}"), conversation=conversation)
                db.commit()
                created.append(result["chatroom_created"])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=run, args=(index,)) for index in range(6)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert errors == [], errors
    with tenant_scope(7), open_tenant_session(env) as db:
        assert db.scalar(select(func.count(Chatroom.id)).where(Chatroom.external_id == room)) == 1
    assert created.count(True) == 1  # 只有一个请求被告知"这个群是你建的"——据此才会做首次启用/初始化


@pytest.mark.parametrize("channel_type, entry", ENTRIES, ids=[item[0] for item in ENTRIES])
def test_an_existing_groups_settings_survive_later_messages(env, channel_type, entry) -> None:
    room = "keep@chatroom"
    with tenant_scope(7), open_tenant_session(env) as db:
        channel = _channel(db, channel_type)
        conversation = _conversation(db, channel, room)
        entry(db, channel, _event(room), conversation=conversation)
        chatroom = db.scalar(select(Chatroom).where(Chatroom.external_id == room))
        chatroom.management_status = "archived"  # 管理员后来把它设成"完全关闭"
        chatroom.operator_note = "管理员备注"
        entry(db, channel, _event(room, "wxid_c"), conversation=conversation)
        db.commit()
        stored = db.scalar(select(Chatroom).where(Chatroom.external_id == room))
        assert stored.operator_note == "管理员备注"  # 后续消息不会把用户的设置冲掉


def test_the_manual_registration_path_is_idempotent(env) -> None:
    from app.services.chatroom_registry import upsert_manual_chatroom

    with tenant_scope(7), open_tenant_session(env) as db:
        channel = _channel(db, "wechat_ipad")
        first = upsert_manual_chatroom(db, channel=channel, external_id="manual@chatroom", name="手动群")
        again = upsert_manual_chatroom(db, channel=channel, external_id="manual@chatroom", remark="备注")
        db.commit()
        assert first.id == again.id and again.remark == "备注" and again.name == "手动群"
        assert db.scalar(select(func.count(Chatroom.id)).where(Chatroom.external_id == "manual@chatroom")) == 1


def _roster(room, version, *ids):
    return {"Code": 0, "Success": True, "Data": {
        "BaseResponse": {"ret": 0}, "ChatroomUserName": room, "ServerVersion": version,
        "NewChatroomData": {"MemberCount": len(ids), "ChatRoomMember": [{"UserName": i} for i in ids]},
    }}


@pytest.mark.parametrize("fault", ["truncated", "duplicate", "invalid_id", "wrong_group", "inner_error",
                                   "outer_error", "no_version", "older_version", "slow_request"])
def test_ipad_invalid_snapshot_preserves_directory(env, fault):
    room = "roster@chatroom"
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with tenant_scope(7), open_tenant_session(env) as db:
        channel = _channel(db, "wechat_ipad")
        upsert_wechat_ipad_chatroom_profile(db, channel, chatroom_external_id=room, synced_at=start,
                                           member_payload=_roster(room, 2, "a", "b"))
        receipt = _roster(room, 3, "a")
        at = start + timedelta(seconds=1)
        data = receipt["Data"]
        if fault == "truncated":
            data["NewChatroomData"]["MemberCount"] = 2
        elif fault in {"duplicate", "invalid_id"}:
            data["NewChatroomData"]["MemberCount"] = 2
            data["NewChatroomData"]["ChatRoomMember"].append({"UserName": "a" if fault == "duplicate" else room})
        elif fault == "wrong_group":
            data["ChatroomUserName"] = "other@chatroom"
        elif fault == "inner_error":
            data["BaseResponse"]["ret"] = -1
        elif fault == "outer_error":
            receipt["Success"] = False
        elif fault == "no_version":
            del data["ServerVersion"]
        elif fault == "older_version":
            data["ServerVersion"] = 1
        else:
            at = start - timedelta(seconds=1)
        upsert_wechat_ipad_chatroom_profile(db, channel, chatroom_external_id=room, synced_at=at, member_payload=receipt)
        db.commit()
        assert db.scalar(select(ChatroomMember.status).where(ChatroomMember.member_external_id == "b")) == "active"
        assert db.scalar(select(Chatroom.member_count)) == 2


def test_ipad_messages_respect_snapshot_and_settings(env):
    room = "members@chatroom"
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with tenant_scope(7), open_tenant_session(env) as db:
        channel = _channel(db, "wechat_ipad")
        upsert_wechat_ipad_chatroom_profile(db, channel, chatroom_external_id=room, synced_at=start,
                                           member_payload=_roster(room, 1, "a", "b"))
        group = db.scalar(select(Chatroom))
        group.extra_json = json.dumps({**json.loads(group.extra_json), "features": {"example": True}})
        group.name = "资料群名"
        upsert_wechat_ipad_chatroom_profile(db, channel, chatroom_external_id=room, synced_at=start + timedelta(seconds=2),
                                           member_payload=_roster(room, 2, "a"))
        for sender, kind in [("b", "text"), ("unknown_person", "text"), ("service_id", "system")]:
            event = _event(room, sender)
            event["details"]["timestamp"] = int(start.timestamp()) + (3 if kind == "system" else 1)
            event["conversation"]["title"] = room
            event["message"]["message_type"] = kind
            sync_wechat_ipad_chatroom_from_message(db, channel, event)
        db.commit()
        assert dict(db.execute(select(ChatroomMember.member_external_id, ChatroomMember.status)).all()) == {"a": "active", "b": "left"}
        assert group.name == "资料群名" and json.loads(group.extra_json)["features"] == {"example": True}


class TestIpadDirectory(unittest.TestCase):
    def test_membership_notifications_use_group_card_and_only_announce_transitions(self):
        from app.services.channel_events import ingest_inbound_event
        from app.services.group_features.dispatch import dispatch_group_features
        from app.services.group_features.member_change import parse_member_change
        from app.services.group_features.store import save_state

        room = "changes@chatroom"
        with self.session() as db:
            channel = db.get(Channel, 1)
            channel.adapter_config_json = '{"wxid":"bot"}'
            conversation = _conversation(db, channel, room)
            group = Chatroom(channel_id=channel.id, bot_id=channel.bot_id, external_id=room,
                             is_active=True, management_status="enabled")
            db.add(group)
            save_state(group, "member_change", enabled=True, config={"card_enabled": True, "join_template": "",
                       "card_title": "欢迎 {nickname}", "card_url": "https://example.com/rules"})
            db.commit()
            base = int(datetime.now(timezone.utc).timestamp()) - 60

            def receive(seq, offset, *, leave=False, msg_type=10000, to="bot"):
                template, link = ('"$username$"退出了群聊', "username") if leave else ('"$names$"加入了群聊', "names")
                content = (f'<sysmsg type="sysmsgtemplate"><content_template><template>{template}</template>'
                           f'<link name="{link}"><memberlist><member><username>alice</username><nickname>甲</nickname>'
                           '</member></memberlist></link></content_template></sysmsg>')
                return ingest_inbound_event(db, channel, {"MsgId": seq, "MsgType": msg_type, "FromUserName": {"string": room},
                    "ToUserName": {"string": to}, "Content": {"string": content}, "CreateTime": base + offset},
                    ipad_group_signals=[])

            for ignored in (receive(1, 0, msg_type=1), receive(2, 1, to="other")):
                assert parse_member_change(ignored["normalized"]) is None
            assert db.scalar(select(ChatroomMember).where(ChatroomMember.member_external_id == "alice")) is None
            joined = receive(3, 2)["normalized"]
            assert joined["message"]["message_type"] == "group_member_join"
            assert joined["details"]["member_change"]["members"] == [{"external_id": "alice", "display_name": "甲"}]
            with patch("app.services.group_features.dispatch.send_replies") as send:
                dispatch_group_features(db, channel, joined, conversation_id=conversation.id)
                card, = send.call_args.args[3]
                assert card["type"] == "link" and card["title"] == "欢迎 甲" and card["url"].endswith("/rules")
            assert receive(3, 2)["deduplicated"]
            assert parse_member_change(receive(4, 3)["normalized"]) is None
            assert receive(5, 5, leave=True)["normalized"]["message"]["message_type"] == "group_member_leave"
            assert parse_member_change(receive(6, 4)["normalized"]) is None
            with patch("app.services.group_features.dispatch.send_replies") as send:
                dispatch_group_features(db, channel, joined, conversation_id=conversation.id)
                send.assert_not_called()
            assert receive(7, 6)["normalized"]["message"]["message_type"] == "group_member_join"
            with patch("app.services.group_features.dispatch.send_replies") as send:
                dispatch_group_features(db, channel, joined, conversation_id=conversation.id)
                send.assert_not_called()
            assert group.member_count == 1

    @pytest.fixture(autouse=True)
    def _directory_setup(self, env):
        with tenant_scope(7):
            self.session = lambda: open_tenant_session(env)
            with self.session() as db:
                _channel(db, "wechat_ipad")
                db.commit()
            yield

    def test_profile_and_member_success_are_counted_separately(self):
        room = "target@chatroom"
        client = Mock()
        client.get_chatroom_info.return_value = {"Code": 0, "Success": True, "Data": {
            "BaseResponse": {"ret": 0}, "ContactList": [
                {"UserName": {"string": "other@chatroom"}, "NickName": {"string": "同名"}, "ChatRoomOwner": "wrong"},
                {"UserName": {"string": room}, "NickName": {"string": "同名"}, "ChatRoomOwner": "owner"},
            ]}}
        client.get_chatroom_member_detail.return_value = _roster(room, 2, "a")
        client.get_chatroom_member_detail.return_value["Data"]["NewChatroomData"]["MemberCount"] = 2
        with self.session() as db:
            channel = db.get(Channel, 1)
            channel.adapter_config_json = '{"wxid":"wxid_example"}'
            ipad_sync.sync_wechat_ipad_chatrooms(db, channel, chatroom_ids=[room], client=client)
            runtime = json.loads(channel.runtime_info_json)
            self.assertEqual(runtime["last_chatroom_sync_profile_count"], 1)
            self.assertEqual(runtime["last_chatroom_sync_member_count"], 0)
            self.assertEqual(runtime["last_chatroom_sync_failed_targets"],
                             [{"chatroom_id": room, "stage": "members", "error": "invalid_roster"}])
            self.assertEqual(db.scalar(select(Chatroom.owner_external_id)), "owner")
            self.assertIsNone(db.scalar(select(ChatroomMember)))

    def test_member_flags_cannot_promote_unknown_roles(self):
        room = "roles@chatroom"
        roster = _roster(room, 2, "a", "b", "c")
        for item, flag in zip(roster["Data"]["NewChatroomData"]["ChatRoomMember"], (1, 2057, 999)):
            item["ChatroomMemberFlag"] = flag
        with self.session() as db:
            channel = db.get(Channel, 1)
            upsert_wechat_ipad_chatroom_profile(db, channel, chatroom_external_id=room, member_payload=roster)
            self.assertEqual(dict(db.execute(select(ChatroomMember.member_external_id, ChatroomMember.role)).all()),
                             {"a": "member", "b": "admin", "c": "member"})

    def test_wechat_ipad_pull_messages_auto_syncs_new_chatroom_members(self):
        with self.session() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            adapter_config = json.loads(channel.adapter_config_json or "{}")
            adapter_config["wxid"] = "wxid_demo_account_001"
            channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False)
            channel.runtime_info_json = json.dumps({"active_wxid": "wxid_demo_account_001"}, ensure_ascii=False)
            db.commit()
            db.refresh(channel)

            with patch(
                "app.services.wechat_ipad.client.WechatIpadApiClient.request_json",
                side_effect=[
                    {
                        "Ret": 0,
                        "Data": {
                            "AddMsgs": [
                                {
                                    "Event": "AddMsg",
                                    "Wxid": "wxid_demo_account_001",
                                    "Data": {
                                        "FromUserName": {"string": "room-auto-sync@chatroom"},
                                        "ToUserName": {"string": "wxid_demo_account_001"},
                                        "Content": {"string": "你邀请张三加入了群聊"},
                                        "MsgType": 10000,
                                        "MsgId": "sync-chatroom-evt-001",
                                    },
                                }
                            ],
                            "Synckey": "sync-key-chatroom-001",
                            "ContinueFlag": 0,
                        },
                        "http_status": 200,
                    },
                    {
                        "Code": 0, "Success": True,
                        "Data": {
                            "BaseResponse": {"ret": 0},
                            "ContactList": [
                                {
                                    "UserName": {"string": "room-auto-sync@chatroom"},
                                    "NickName": {"string": "自动同步群"},
                                    "MemberCount": 2,
                                    "ChatRoomOwner": "wxid_owner_auto_sync",
                                }
                            ]
                        },
                        "http_status": 200,
                    },
                    {
                        "Code": 0, "Success": True,
                        "Data": {
                            "BaseResponse": {"ret": 0}, "ChatroomUserName": "room-auto-sync@chatroom", "ServerVersion": 1,
                            "NewChatroomData": {
                                "MemberCount": 2,
                                "ChatRoomMember": [
                                    {
                                        "UserName": "wxid_owner_auto_sync",
                                        "NickName": "群主",
                                        "DisplayName": "群主名片",
                                        "ChatroomMemberFlag": 1,
                                    },
                                    {
                                        "UserName": "wxid_member_auto_sync",
                                        "NickName": "张三",
                                        "DisplayName": "张三名片",
                                        "ChatroomMemberFlag": 0,
                                    },
                                ]
                            }
                        },
                        "http_status": 200,
                    },
                ],
            ) as provider:
                ipad_sync.pull_wechat_ipad_messages(db, channel)
                self.assertEqual(provider.call_count, 3)

        with self.session() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            runtime_info = json.loads(channel.runtime_info_json or "{}")
            self.assertEqual(runtime_info.get("last_message_sync_chatroom_candidate_count"), 1)
            self.assertTrue(runtime_info.get("last_message_sync_chatroom_sync_triggered"))
            self.assertIn("成功补齐群资料 1 个", runtime_info.get("last_chatroom_sync_summary") or "")

            candidate_signals = runtime_info.get("last_message_sync_chatroom_candidates") or []
            self.assertEqual(len(candidate_signals), 1)
            self.assertEqual(candidate_signals[0]["reason"], "member_change_event")
            self.assertEqual(candidate_signals[0]["chatroom_id"], "room-auto-sync@chatroom")

            chatroom = db.scalar(select(Chatroom).where(Chatroom.external_id == "room-auto-sync@chatroom"))
            self.assertIsNotNone(chatroom)
            self.assertEqual(chatroom.name, "自动同步群")
            self.assertEqual(chatroom.status, "members_synced")
            self.assertEqual(chatroom.member_count, 2)

            members = db.scalars(
                select(ChatroomMember)
                .where(ChatroomMember.chatroom_id == chatroom.id)
                .order_by(ChatroomMember.member_external_id.asc())
            ).all()
            self.assertEqual(len(members), 2)
            self.assertEqual(members[0].member_external_id, "wxid_member_auto_sync")
            self.assertEqual(members[1].member_external_id, "wxid_owner_auto_sync")

    def test_wechat_ipad_chatroom_profile_sync_promotes_status_from_message_observed(self):
        with self.session() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-promote-001@chatroom",
                name="初始骨架群",
                status="message_observed",
            )
            db.add(chatroom)
            db.commit()

            upsert_wechat_ipad_chatroom_profile(
                db,
                channel,
                chatroom_external_id="room-promote-001@chatroom",
                profile_payload={
                    "Code": 0, "Success": True,
                    "Data": {
                        "BaseResponse": {"ret": 0},
                        "ContactList": [
                            {
                                "UserName": {"string": "room-promote-001@chatroom"},
                                "NickName": {"string": "已补资料群"},
                                "Remark": {"string": "群备注"},
                                "ChatRoomOwner": "wxid_owner_promote",
                                "MemberCount": 2,
                            }
                        ]
                    }
                },
                member_payload={
                    "Code": 0, "Success": True,
                    "Data": {
                        "BaseResponse": {"ret": 0}, "ChatroomUserName": "room-promote-001@chatroom", "ServerVersion": 1,
                        "NewChatroomData": {
                            "MemberCount": 2,
                            "ChatRoomMember": [
                                {"UserName": "wxid_owner_promote", "NickName": "群主", "DisplayName": "群主", "ChatroomMemberFlag": 1},
                                {"UserName": "wxid_member_promote", "NickName": "成员", "DisplayName": "成员显示名", "ChatroomMemberFlag": 0},
                            ]
                        }
                    }
                },
            )
            db.commit()
            db.refresh(chatroom)

            self.assertEqual(chatroom.status, "members_synced")
            self.assertEqual(chatroom.name, "已补资料群")
            self.assertEqual(chatroom.owner_external_id, "wxid_owner_promote")
            self.assertEqual(chatroom.member_count, 2)
            members = db.scalars(select(ChatroomMember).where(ChatroomMember.chatroom_id == chatroom.id)).all()
            self.assertEqual(len(members), 2)

    def test_wechat_ipad_chatroom_member_status_marks_left_and_recovers_when_back(self):
        with self.session() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-member-status-001@chatroom",
                name="成员状态群",
                status="members_synced",
                member_count=2,
            )
            db.add(chatroom)
            db.flush()
            db.add_all(
                [
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=channel.bot_id,
                        channel_id=channel.id,
                        member_external_id="wxid_member_keep",
                        nickname="常驻成员",
                        display_name="常驻成员",
                        status="active",
                    ),
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=channel.bot_id,
                        channel_id=channel.id,
                        member_external_id="wxid_member_return",
                        nickname="回归成员",
                        display_name="回归成员",
                        status="active",
                    ),
                ]
            )
            db.commit()

            upsert_wechat_ipad_chatroom_profile(
                db,
                channel,
                chatroom_external_id="room-member-status-001@chatroom",
                member_payload={
                    "Code": 0, "Success": True,
                    "Data": {
                        "BaseResponse": {"ret": 0}, "ChatroomUserName": "room-member-status-001@chatroom", "ServerVersion": 1,
                        "NewChatroomData": {
                            "MemberCount": 1,
                            "ChatRoomMember": [
                                {"UserName": "wxid_member_keep", "NickName": "常驻成员", "DisplayName": "常驻成员", "ChatroomMemberFlag": 0},
                            ]
                        }
                    }
                },
                synced_at=datetime(2026, 3, 25, 8, 0, 0, tzinfo=timezone.utc),
            )
            db.commit()

            left_member = db.scalar(
                select(ChatroomMember).where(
                    ChatroomMember.chatroom_id == chatroom.id,
                    ChatroomMember.member_external_id == "wxid_member_return",
                )
            )
            self.assertIsNotNone(left_member)
            self.assertEqual(left_member.status, "left")

            upsert_wechat_ipad_chatroom_profile(
                db,
                channel,
                chatroom_external_id="room-member-status-001@chatroom",
                member_payload={
                    "Code": 0, "Success": True,
                    "Data": {
                        "BaseResponse": {"ret": 0}, "ChatroomUserName": "room-member-status-001@chatroom", "ServerVersion": 2,
                        "NewChatroomData": {
                            "MemberCount": 2,
                            "ChatRoomMember": [
                                {"UserName": "wxid_member_keep", "NickName": "常驻成员", "DisplayName": "常驻成员", "ChatroomMemberFlag": 0},
                                {"UserName": "wxid_member_return", "NickName": "回归成员", "DisplayName": "回归成员", "ChatroomMemberFlag": 0},
                            ]
                        }
                    }
                },
                synced_at=datetime(2026, 3, 25, 9, 0, 0, tzinfo=timezone.utc),
            )
            db.commit()

            returned_member = db.scalar(
                select(ChatroomMember).where(
                    ChatroomMember.chatroom_id == chatroom.id,
                    ChatroomMember.member_external_id == "wxid_member_return",
                )
            )
            self.assertIsNotNone(returned_member)
            self.assertEqual(returned_member.status, "active")
