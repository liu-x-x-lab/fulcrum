import base64
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import open_tenant_session
from app.models.entities import BotPlugin, Channel, Conversation, Message, MessageAttachment, Plugin, TaskJob
from app.services.async_workers import _process_outbound_dispatch_job
from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter
from app.services.plugin_directory import _dispatch_auto_outbound_messages
from app.services.plugin_runtime.replies import video_reply
from app.services.task_queue import parse_job_payload
from app.services.wechat_ipad import videos
from test_ipad_file_delivery import file_context  # noqa: F401
from test_message_center import seeded  # noqa: F401
from test_provider_node_pages import provider_page_context  # noqa: F401

CLIP = b"\x00\x00\x00\x14ftypmp42\0\0\0\0isom\x00\x00\x00\x09mdatx"


def encoded(data):
    return base64.b64encode(data).decode("ascii")


def cover():
    stream = BytesIO()
    Image.new("RGB", (6, 4), "red").save(stream, "PNG")
    return stream.getvalue()


def response(**fields):
    return {"http_status": 200, "Success": True, "Code": 0, "Data": {
        "BaseResponse": {"ret": 0}, "clientMsgId": "bot_1700000000", "videoStartPos": len(CLIP),
        "newMsgId": 123456, "aeskey": "secret-value", **fields}}


@pytest.mark.parametrize("fields,error", [
    ({}, None), ({"thumbStartPos": 300, "videoStartPos": 0}, "result_unknown"),
    ({"newMsgId": 0}, "result_unknown"), ({"videoStartPos": None}, "result_unknown"),
    ({"clientMsgId": "other_1700000000"}, "result_unknown"), ({"clientMsgId": None}, "result_unknown"),
    ({"fromUserName": "other"}, "result_unknown"), ({"toUserName": "other"}, "result_unknown"),
    ({"BaseResponse": {"ret": -5}}, "provider_rejected"),
])
def test_video_receipt(fields, error):
    assert videos.receipt(response(**fields), "bot", "peer", {"size": len(CLIP)}) == (
        None if error else "123456", error)


def test_invalid_media_and_size_limits(monkeypatch):
    payload = {"base64": encoded(CLIP), "image_base64": encoded(cover())}
    for invalid in ({"base64": encoded(b"not mp4")}, {"image_base64": encoded(b"not png")},
                    {"play_length": -1}, {"play_length": True}):
        with pytest.raises(ValueError):
            videos.prepare({**payload, **invalid})
    monkeypatch.setattr(videos, "MAX_COVER_BYTES", 4)
    with pytest.raises(ValueError):
        videos.prepare(payload)
    monkeypatch.setattr(videos, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(ValueError, match="过大"):
        videos.prepare(payload)


@pytest.mark.parametrize("timeout", [False, True])
def test_multipart_video(file_context, monkeypatch, timeout):
    client, ids, cid, csrf, calls = file_context

    def post(self, url, body):
        calls.append((url, body))
        if timeout:
            raise TimeoutError
        return response()

    monkeypatch.setattr(WechatIpadAdapter, "_post_json", post)
    data = {"conversation_id": cid, "csrf_token": csrf, "message_type": "video", "play_length": 3}
    uploads = {"file": ("clip.mp4", CLIP, "text/html")}
    assert client.post("/messages/send", data=data, files=uploads).status_code == 400
    uploads["video_cover"] = ("cover.png", cover(), "image/png")
    result = client.post("/messages/send", data=data, files=uploads).json()
    assert result["ok"] is not timeout, result
    assert bool(result.get("unconfirmed")) is timeout
    assert len(calls) == 1 and calls[0][0].endswith("/api/Msg/SendVideo")
    body = calls[0][1]
    assert body["Wxid"] == "bot" and body["ToWxid"] == "room@chatroom" and body["PlayLength"] == 3
    assert body["Base64"] == "data:video/mp4;base64," + encoded(CLIP)
    assert body["ImageBase64"].startswith("data:image/jpeg;base64,/9j/")
    with ids["factory"]() as db:
        msg = db.get(Message, result["message_id"])
        assert msg.external_status == ("unknown" if timeout else "sent")
        for value in ("secret-value", encoded(CLIP), body["ImageBase64"]):
            assert value not in (msg.raw_response_json or "") + (msg.raw_request_json or "")
        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.message_id == msg.id))
        assert attachment.mime_type == "video/mp4" and Path(attachment.storage_path).read_bytes() == CLIP


def test_plugin_video_reply(file_context, monkeypatch):
    _, ids, cid, _, calls = file_context

    def post(self, url, body):
        calls.append((url, body))
        return response(videoStartPos=0)

    monkeypatch.setattr(WechatIpadAdapter, "_post_json", post)
    with tenant_scope(ids["alpha"]), open_tenant_session(ids["factory"]) as db:
        plugin = Plugin(code="clips", display_name="Clips")
        db.add(plugin)
        db.commit()
        ctx = SimpleNamespace(plugin_code="clips", channel_type="wechat_ipad", normalized_event={},
                              message_type="text", binding_config={},
                              conversation_id=cid, conversation_external_id="room@chatroom", chat_type="group")
        jobs = _dispatch_auto_outbound_messages(db, SimpleNamespace(channel_id=ids["channel_id"]), ctx,
            {"replies": [video_reply(video_base64=encoded(CLIP), cover_base64=encoded(cover()), play_length=2)]})
        job = db.get(TaskJob, jobs[0]["job_id"])
        assert job.max_attempts == 1
        queued = parse_job_payload(job)
        assert _process_outbound_dispatch_job(db, job, queued)["reason"] == "plugin_scope_disabled"
        db.add(BotPlugin(tenant_id=ids["alpha"], bot_id=ids["bot"], plugin_id=plugin.id, group_default_enabled=True))
        db.commit()
        result = _process_outbound_dispatch_job(db, job, queued)["adapter_result"]
        assert result["external_status"] == "unknown" and "secret-value" not in json.dumps(result)
        channel = db.get(Channel, ids["channel_id"])
        conversation = db.get(Conversation, cid)
        for change, code in (({"receiver_id": "other"}, "invalid_target"), ({"xml": "<msg/>"}, "invalid_payload")):
            denied = WechatIpadAdapter().send_message(channel, conversation, {**queued["payload"], **change})
            assert denied["error_code"] == code
        conversation.tenant_id += 1
        assert WechatIpadAdapter().send_message(channel, conversation, queued["payload"])["error_code"] == "invalid_target"
        db.rollback()
        assert len(calls) == 1 and calls[0][1]["PlayLength"] == 2
