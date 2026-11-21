import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import open_tenant_session
from app.models.entities import Channel, Conversation, Message, MessageAttachment, Plugin, TaskJob
from app.services import outbound_files
from app.services.async_workers import _process_outbound_dispatch_job
from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter
from app.services.plugin_directory import _dispatch_auto_outbound_messages
from app.services.plugin_runtime.replies import file_reply
from app.services.task_queue import parse_job_payload
from app.services.wechat_ipad import files
from test_message_center import seeded  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401

CONTENT = b'{"receipt":"test"}\n'
DIGEST = hashlib.md5(CONTENT, usedforsecurity=False).hexdigest()


def confirmed(name="meta.json", target="room@chatroom"):
    return {"http_status": 200, "Success": True, "Code": 0, "Data": {
        "delivery": "file", "fileName": name, "fileSize": len(CONTENT), "fileMd5": DIGEST,
        "toUserName": target, "attachId": "private-id", "xml": "private-xml",
        "uploadResp": {"BaseResponse": {"ret": 0}, "mediaId": "private-id"}, "uploadRetCode": 0,
        "retCode": 0, "isSendSuccess": True, "sendResult": {"Code": 0, "Success": True, "Data": {
            "BaseResponse": {"ret": 0}, "fromUserName": "bot", "toUserName": target, "newMsgId": 123456}},
    }}


@pytest.fixture
def file_context(seeded, tmp_path, monkeypatch):
    client, ids, cid, _, csrf = seeded
    with ids["factory"]() as db:
        db.get(Channel, ids["channel_id"]).runtime_info_json = '{"active_wxid":"bot"}'
        db.commit()
    monkeypatch.setattr(outbound_files, "OUTBOUND_FILE_ROOT", tmp_path / "uploads")
    calls = []

    def protocol(self, url, body):
        calls.append((url, body))
        return confirmed(body["FileName"], body["ToWxid"])

    monkeypatch.setattr(WechatIpadAdapter, "_post_json", protocol)
    return client, ids, cid, csrf, calls


def test_manual_file_upload(file_context):
    client, ids, cid, csrf, calls = file_context

    def submit(token, name="meta.json", data=CONTENT):
        return client.post("/messages/send", data={"conversation_id": cid, "csrf_token": token, "message_type": "file"},
                           files={"file": (name, data, "application/octet-stream")})

    assert submit("bad-csrf").status_code == 403
    assert submit(csrf, data=b"").status_code == 400
    assert submit(csrf, name="a" * 252).status_code == 400
    result = submit(csrf).json()
    assert result["ok"], result
    assert len(calls) == 1
    assert calls[0][0].endswith("/api/Msg/SendFile")
    assert calls[0][1] == {"Wxid": "bot", "ToWxid": "room@chatroom", "FileName": "meta.json",
                           "FileData": "data:application/octet-stream;base64," + base64.b64encode(CONTENT).decode()}
    with ids["factory"]() as db:
        msg = db.get(Message, result["message_id"])
        assert (msg.tenant_id, msg.external_message_id, msg.content) == (ids["alpha"], "123456", "meta.json")
        request_summary = json.loads(msg.raw_request_json)
        assert request_summary.pop("sent_at")
        assert request_summary == {"file_name": "meta.json", "file_size": len(CONTENT)}
        assert json.loads(msg.raw_response_json) == {"newMsgId": "123456"}
        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.message_id == msg.id))
        assert Path(attachment.storage_path).read_bytes() == CONTENT
    assert submit(_login(client, "readonly")).status_code == 403
    assert len(calls) == 1


def test_timeout_is_shown_as_unconfirmed(file_context, monkeypatch):
    client, _, cid, csrf, _ = file_context
    calls = []

    def timeout(*args):
        calls.append(1)
        raise TimeoutError

    monkeypatch.setattr(WechatIpadAdapter, "_post_json", timeout)
    result = client.post("/messages/send", data={"conversation_id": cid, "csrf_token": csrf, "message_type": "file"},
                         files={"file": ("meta.json", CONTENT)}).json()
    assert not result["ok"] and "待核实" in result["notice"] and calls == [1]
    assert "发送结果待核实" in client.get(f"/messages?conversation_id={cid}").text


def test_plugin_file_reply_to_direct_conversation(file_context, monkeypatch):
    _, ids, _, _, calls = file_context
    fetched = []

    def download(url, limit):
        fetched.append((url, limit))
        return CONTENT

    monkeypatch.setattr(files, "read_public_file", download)
    with tenant_scope(ids["alpha"]), open_tenant_session(ids["factory"]) as db:
        peer = Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"],
                            channel_type="wechat_ipad", external_id="peer", chat_type="direct")
        db.add_all([peer, Plugin(code="report", display_name="report")])
        db.commit()
        ctx = SimpleNamespace(plugin_code="report", channel_type="wechat_ipad", normalized_event={},
                              conversation_id=peer.id, conversation_external_id="peer", chat_type="direct")
        url = "https://cdn.example.test/report?token=private"
        jobs = _dispatch_auto_outbound_messages(db, SimpleNamespace(channel_id=ids["channel_id"]), ctx,
            {"replies": [file_reply(file_name="report.txt", file_url=url)]})
        job = db.get(TaskJob, jobs[0]["job_id"])
        assert job.max_attempts == 1
        payload = parse_job_payload(job)
        processed = _process_outbound_dispatch_job(db, job, payload)
        result = processed["adapter_result"]
        assert db.get(Message, processed["message_id"]).content == "report.txt"
        assert result["ok"] and "private" not in json.dumps(result)
        assert fetched == [(url, files.MAX_UPLOAD_BYTES)]
        assert calls[0][1]["ToWxid"] == "peer"
        payload["payload"]["receiver_id"] = "someone-else"
        denied = _process_outbound_dispatch_job(db, job, payload)["adapter_result"]
        assert not denied["ok"] and denied["error_code"] == "invalid_target"
        assert len(fetched) == len(calls) == 1


@pytest.mark.parametrize("path,value,reason", [
    ("Data.sendResult", {}, "result_unknown"), ("Data", {}, "result_unknown"),
    ("Data.fileMd5", "wrong", "result_unknown"), ("Data.fileSize", False, "result_unknown"),
    ("Data.sendResult.Data.fromUserName", "other", "result_unknown"),
    ("Data.sendResult.Data.toUserName", "other", "result_unknown"),
    ("Data.uploadResp.mediaId", "other", "result_unknown"), ("Data.sendResult.Data.newMsgId", 0, "result_unknown"),
    ("Data.uploadRetCode", -1, "provider_rejected"), ("Data.sendResult.Data.BaseResponse.ret", -3, "provider_rejected"),
])
def test_receipt_requires_both_matching_acknowledgements(path, value, reason):
    reply = confirmed()
    node = reply
    *keys, last = path.split(".")
    for key in keys:
        node = node[key]
    node[last] = value
    assert files.receipt(reply, "bot", "room@chatroom", {"file_name": "meta.json", "size": len(CONTENT),
                                                         "md5": DIGEST}) == (None, reason)


@pytest.mark.parametrize("source", [{"file_url": "http://127.0.0.1/private"}, {"file_base64": "123*"},
                                   {"file_url": "/etc/passwd"}, {"file_url": "x", "file_base64": "YQ=="}])
def test_invalid_sources_are_never_submitted(source):
    with pytest.raises(ValueError):
        files.prepare({"file_name": "test.txt", **source})


def test_file_limit_checks_decoded_payload(monkeypatch):
    monkeypatch.setattr(files, "MAX_UPLOAD_BYTES", 4)
    assert files.prepare({"file_name": "note", "file_base64": "YWJjZA=="})[0]["file_name"] == "note.dat"
    with pytest.raises(ValueError):
        files.prepare({"file_name": "note", "file_base64": "YWJjZGU="})


def test_explicit_app_card_still_uses_send_app(file_context, monkeypatch):
    _, ids, cid, _, _ = file_context
    calls = []

    def protocol(self, url, body):
        calls.append((url, body))
        return {"Ret": 0, "MsgId": "card-170", "http_status": 200}

    monkeypatch.setattr(WechatIpadAdapter, "_post_json", protocol)
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.adapter_config_json = '{"wxid":"bot"}'
        xml = "<appmsg><title>报价单</title><type>74</type></appmsg>"
        WechatIpadAdapter().send_message(channel, db.get(Conversation, cid), {"message_type": "file", "xml": xml})
        assert len(calls) == 1 and calls[0][0].endswith("/api/Msg/SendApp")
        assert calls[0][1]["Type"] == 74 and calls[0][1]["Xml"] == xml
