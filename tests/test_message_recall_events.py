"""撤回回调只更新原消息；找不到可靠编号时保留可读通知，不触发业务回复。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel, Chatroom, Conversation, Message, RawWebhookEvent, TaskJob
from app.services import channel_events, message_actions, message_view
from app.services.message_view import recall_info
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed


SERVER_ID = "7423189012345678901"
NEW_RECALL = '<sysmsg type="revokemsgcontent"><revokemsgcontent><link/><scene>1</scene><reEditStringContent><![CDATA[不应重新展示的原文]]></reEditStringContent><reEditReferMsgSvrId>{target}</reEditReferMsgSvrId><stringContent><![CDATA[你撤回了一条消息]]></stringContent><text>你撤回了一条消息</text></revokemsgcontent></sysmsg>'
OLD_RECALL = '<sysmsg type="revokemsg"><revokemsg><session>1@chatroom</session><newmsgid>{target}</newmsgid><msgid>42</msgid><replacemsg><![CDATA[小明撤回了一条消息]]></replacemsg></revokemsg></sysmsg>'


@pytest.fixture()
def env():
    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": {"account_id": "wxid_bot"}})
    conversation = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="1@chatroom", chat_type="group")
    db.add(conversation)
    db.flush()
    message = Message(tenant_id=1, conversation_id=conversation.id, direction="in", sender_id="wxid_user", content="待撤回消息", message_type="text", external_message_id=SERVER_ID, status="received")
    db.add(message)
    db.commit()
    yield db, channel, conversation, message
    db.close()


def _event(xml: str, *, event_id: str = "recall-event", **body):
    return {"event_name": "message", "event_id": event_id, "body": {"chat_id": "1@chatroom", "sender_id": "wxid_user", "msg_type": "unknown", "raw_message_type": 10002, "content": xml, "message_id": "local-callback", **body}}


def test_real_revokemsgcontent_is_parsed_and_historical_system_rows_are_readable():
    xml = NEW_RECALL.format(target=SERVER_ID)
    parsed = message_view.recall_info(xml)
    assert parsed["message_ids"] == [SERVER_ID] and parsed["text"] == "你撤回了一条消息"
    for kind in ("unknown", "system", "text"):
        view = message_view.describe(xml, kind)
        assert view["kind"] == "system" and view["text"] == "你撤回了一条消息"
        assert "不应重新展示" not in view["text"] and SERVER_ID not in view["text"]
        assert message_view.preview(xml, kind) == "你撤回了一条消息"


def test_old_xml_uses_server_id_then_local_id_and_handles_group_sender_prefix():
    xml = "wxid_user: \n" + OLD_RECALL.format(target=SERVER_ID)
    # 解析结果同时保留两类原生编号，撤回匹配才能区分服务器编号与本地编号；
    # 合并后的契约不能再用旧字典断言把这两个防误撤回字段当成多余数据。
    assert message_view.recall_info(xml) == {
        "message_ids": [SERVER_ID, "42"],
        "server_message_ids": [SERVER_ID],
        "local_message_ids": ["42"],
        "session": "1@chatroom",
        "text": "小明撤回了一条消息",
    }


def test_templates_quotes_and_malformed_xml_are_never_mistaken_for_recall():
    for content in ('<sysmsg type="sysmsgtemplate"><sysmsgtemplate><text>通知</text></sysmsgtemplate></sysmsg>', '<msg><appmsg><type>57</type><refermsg><content>&lt;sysmsg type="revokemsg"&gt;</content></refermsg></appmsg></msg>', '<sysmsg type="revokemsg"><revokemsg>'):
        assert message_view.recall_info(content) is None
    empty = '<sysmsg type="revokemsgcontent"><revokemsgcontent><reEditStringContent>原文</reEditStringContent></revokemsgcontent></sysmsg>'
    assert message_view.recall_info(empty)["text"] == "一条消息已被撤回"


def test_recall_notification_strips_rendering_markup():
    xml = '<sysmsg type="revokemsgcontent"><revokemsgcontent><stringContent><![CDATA[你撤回了一条消息 <a href="javascript:x">重新编辑</a>]]></stringContent></revokemsgcontent></sysmsg>'
    assert message_view.describe(xml, "unknown")["text"] == "你撤回了一条消息 重新编辑"


def test_adapter_recognizes_unknown_10002_and_content_xml_fallback(env):
    _db, channel, _conversation, _message = env
    event = _event("", content_xml=NEW_RECALL.format(target=SERVER_ID))
    normalized = WechatWeBridgeAdapter().normalize_inbound_payload(channel, event)
    assert normalized["event_type"] == "message_recalled"
    assert normalized["message"]["message_type"] == "system"
    assert normalized["details"]["skip_plugin_dispatch"] is True
    assert normalized["details"]["recall"]["message_ids"] == [SERVER_ID]


def test_plain_text_containing_recall_xml_cannot_change_another_message(env):
    _db, channel, _conversation, _message = env
    event = _event(NEW_RECALL.format(target=SERVER_ID), msg_type="text", raw_message_type=1)
    normalized = WechatWeBridgeAdapter().normalize_inbound_payload(channel, event)
    assert normalized["event_type"] == "message" and "recall" not in normalized["details"]
    assert normalized["details"].get("skip_plugin_dispatch") is not True


@pytest.mark.parametrize("status,direction", [("received", "in"), ("sent", "out"), ("revoked", "out")])
def test_matching_callback_updates_original_and_adds_no_extra_system_card(env, status, direction):
    db, channel, conversation, message = env
    message.status, message.direction = status, direction
    db.commit()
    event = _event(NEW_RECALL.format(target=SERVER_ID))
    result = channel_events.ingest_inbound_event(db, channel, event)
    assert result["dispatch"]["matched"] is True and result["dispatch"]["queued"] is False
    assert message.status == message.external_status == "revoked" and message.content == "待撤回消息"
    assert len(db.scalars(select(Message).where(Message.conversation_id == conversation.id)).all()) == 1
    raw = db.get(RawWebhookEvent, result["raw_event_id"])
    assert json.loads(raw.payload_json) == event and json.loads(raw.dispatch_summary_json)["reason"] == "message_recalled"
    assert db.scalars(select(TaskJob)).all() == []


@pytest.mark.parametrize("target", ["", "9999999999999999999"])
def test_missing_or_unmatched_native_id_preserves_original_and_displays_notification(env, target):
    db, channel, conversation, message = env
    result = channel_events.ingest_inbound_event(db, channel, _event(NEW_RECALL.format(target=target)))
    assert result["dispatch"]["matched"] is False
    assert message.status == "received"
    rows = db.scalars(select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id)).all()
    assert len(rows) == 2 and rows[-1].message_type == "system" and rows[-1].content == "你撤回了一条消息"
    assert rows[-1].external_message_id is None and db.scalars(select(TaskJob)).all() == []


def test_recall_matches_only_same_conversation_and_keeps_hidden_echoes_hidden(env):
    db, channel, conversation, message = env
    message.external_message_id = "other-native-id"
    other = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="other@chatroom", chat_type="group")
    db.add(other)
    db.flush()
    foreign = Message(tenant_id=1, conversation_id=other.id, direction="in", content="另一个群的消息", external_message_id=SERVER_ID, status="received")
    hidden = Message(tenant_id=1, conversation_id=conversation.id, direction="out", content="历史重复回显", external_message_id=SERVER_ID, status="echo")
    db.add_all([foreign, hidden])
    db.commit()
    result = channel_events.ingest_inbound_event(db, channel, _event(NEW_RECALL.format(target=SERVER_ID)))
    assert result["dispatch"]["matched"] is False and foreign.status == "received" and hidden.status == "echo"


def test_duplicate_callback_is_idempotent_and_never_collides_with_original_event_key(env):
    db, channel, _conversation, message = env
    db.add(RawWebhookEvent(tenant_id=1, channel_id=channel.id, source="wechat_webridge", event_key="local-callback", payload_json="{}", status="mapped"))
    db.commit()
    event = _event(NEW_RECALL.format(target=""), event_id="")
    first = channel_events.ingest_inbound_event(db, channel, event)
    second = channel_events.ingest_inbound_event(db, channel, event)
    assert first["dispatch"]["matched"] is False and second["deduplicated"] is True
    assert len(db.scalars(select(RawWebhookEvent)).all()) == 2
    assert len(db.scalars(select(Message)).all()) == 2 and message.status == "received"


@pytest.fixture()
def recall_context():
    """同一成员先后发出相同正文；事件编号与微信编号属于不同编号空间。"""

    db = _session()
    _seed(db)
    channel = db.get(Channel, 1)
    channel.channel_type = "wechat_gugatool"
    conversation = Conversation(tenant_id=1, bot_id=1, channel_id=1,
        channel_type="wechat_gugatool", external_id="recall@chatroom", chat_type="group")
    db.add(conversation)
    db.flush()
    rows = [Message(tenant_id=1, conversation_id=conversation.id, direction="in", sender_id="wxid_member",
        message_type="text", content="好的", external_message_id=f"event-{index}", status="received",
        raw_request_json=json.dumps({"native_ids": {"local": index, "server": server}}))
        for index, server in ((101, 18446744073709551000), (102, 18446744073709551001))]
    db.add_all(rows)
    db.commit()
    try:
        yield db, channel, conversation, rows
    finally:
        db.close()


def _recall(db, channel, conversation, xml):
    return message_actions.apply_gugatool_recall_event(db, channel, conversation, recall_info(xml),
        raw_event_id=999)


def test_recall_uses_exact_server_id_before_repeated_text(recall_context):
    db, channel, conversation, rows = recall_context
    changed = _recall(db, channel, conversation,
        '<sysmsg type="revokemsg"><revokemsg><newmsgid>18446744073709551000</newmsgid>'
        '<reEditStringContent>好的</reEditStringContent></revokemsg></sysmsg>')
    assert [row.id for row in changed] == [rows[0].id]
    assert [row.status for row in rows] == ["revoked", "received"]


@pytest.mark.parametrize("identifier", [
    "", "<newmsgid>99999</newmsgid>", "<msgid>18446744073709551000</msgid>",
    "<newmsgid>99999</newmsgid><msgid>101</msgid>",
    "<newmsgid>18446744073709551000</newmsgid><reEditReferMsgSvrId>18446744073709551001</reEditReferMsgSvrId>",
])
def test_recall_never_guesses_from_body_or_other_id_namespace(recall_context, identifier):
    db, channel, conversation, rows = recall_context
    changed = _recall(db, channel, conversation,
        f'<sysmsg type="revokemsg"><revokemsg>{identifier}'
        '<reEditStringContent>好的</reEditStringContent></revokemsg></sysmsg>')
    assert changed == []
    assert [row.status for row in rows] == ["received", "received"]


def test_recall_rejects_duplicate_native_identifiers(recall_context):
    db, channel, conversation, rows = recall_context
    rows[1].raw_request_json = rows[0].raw_request_json
    db.commit()
    assert _recall(db, channel, conversation,
        '<sysmsg type="revokemsg"><revokemsg><newmsgid>18446744073709551000</newmsgid></revokemsg></sysmsg>') == []
    assert [row.status for row in rows] == ["received", "received"]


def test_recall_accepts_explicit_local_id(recall_context):
    db, channel, conversation, rows = recall_context
    changed = _recall(db, channel, conversation,
        '<sysmsg type="revokemsg"><revokemsg><msgid>101</msgid></revokemsg></sysmsg>')
    assert [row.id for row in changed] == [rows[0].id]


@pytest.mark.parametrize("notice", ["你撤回了一条消息", "一位成员撤回了一条消息"])
def test_unknown_recall_is_saved_without_peer_identity_or_dispatch(recall_context, notice):
    """走完整入站链路：未知撤回只保存可读系统通知，既不误撤也不产生群资料或指令任务。"""

    from app.services.channel_events import ingest_inbound_event

    db, channel, conversation, _ = recall_context
    with tenant_scope(1), tenant_session(sessionmaker(bind=db.get_bind())) as scoped:
        result = ingest_inbound_event(scoped, scoped.get(Channel, channel.id), {
            "id": "unknown-recall", "kind": "incoming", "fromUser": conversation.external_id,
            "toUser": "wxid_bot", "messageType": 10002,
            "content": '<sysmsg type="revokemsgcontent"><revokemsgcontent>'
                f'<text>{notice}</text><link><reEditStringContent>好的</reEditStringContent></link>'
                '</revokemsgcontent></sysmsg>',
        })
        assert result["dispatch"]["mode"] == "storage_only" and result["dispatch"]["matched"] is False
        notification = scoped.get(Message, result["dispatch"]["message_id"])
        assert notification.message_type == "system" and notification.content == notice
        assert notification.sender_id is None and notification.sender_name is None
        assert scoped.scalar(select(func.count()).select_from(TaskJob)) == 0
        assert scoped.scalar(select(func.count()).select_from(Chatroom)) == 0
        assert all(row.status == "received" for row in scoped.scalars(
            select(Message).where(Message.message_type == "text")).all())
