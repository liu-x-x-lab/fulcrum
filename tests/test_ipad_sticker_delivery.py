import base64
import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.models.entities import Channel, Conversation, Message, MessageAttachment
from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter
from app.services.plugin_directory import _normalize_reply_item
from app.services.plugin_runtime import emoji_reply, standard_replies
from app.services.wechat_ipad import stickers
from test_ipad_file_delivery import file_context  # noqa: F401
from test_message_center import seeded  # noqa: F401
from test_provider_node_pages import provider_page_context  # noqa: F401


def image(fmt="GIF"):
    buf = BytesIO()
    first, second = (Image.new("RGB", (8, 8), color) for color in ("red", "blue"))
    first.save(buf, fmt, save_all=True, append_images=[second], duration=80) if fmt == "GIF" else first.save(buf, fmt)
    return buf.getvalue()


def acknowledgement(data):
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
    size = len(data)
    return {"http_status": 200, "Success": True, "Code": 0, "Data": {
        "emojiMd5": md5, "emojiSize": size, "retCode": 0, "isSendSuccess": True,
        "resp": {"BaseResponse": {"ret": 0}, "emojiItemCount": 1, "emojiItem": [
            {"ret": 0, "md5": md5, "startPos": size, "totalLen": size, "newMsgId": 12345}]}}}


@pytest.mark.parametrize("fmt", ["GIF", "PNG", "WEBP"])
def test_plugin_sticker_preserves_bytes(fmt):
    data = image(fmt)
    encoded = base64.b64encode(data).decode()
    reply, = standard_replies([emoji_reply(base64_data=encoded)])
    item = _normalize_reply_item(reply, channel_type="wechat_ipad")
    meta, fields = stickers.prepare(item)
    assert fields == {"Base64": f"data:image/{fmt.lower()};base64,{encoded}"}
    assert stickers.receipt(acknowledgement(data), "bot", "room", meta) == ("12345", None)


@pytest.mark.parametrize("fields,expected", [
    ({"ret": -2}, "provider_rejected"), ({"startPos": 0}, "result_unknown"),
    ({"newMsgId": 0}, "result_unknown"), ({"md5": "other"}, "result_unknown")])
def test_incomplete_or_rejected_item(fields, expected):
    data = image()
    ack = acknowledgement(data)
    ack["Data"]["resp"]["emojiItem"][0].update(fields)
    assert stickers.receipt(ack, "bot", "room", {"md5": ack["Data"]["emojiMd5"], "size": len(data)}) == (None, expected)


def test_sticker_form_and_target(file_context, monkeypatch):
    client, ids, cid, csrf, calls = file_context
    data = image()
    form = {"csrf_token": csrf, "message_type": "emoji", "conversation_id": cid}

    def send(self, url, body):
        calls.append((url, body))
        return acknowledgement(data)

    monkeypatch.setattr(WechatIpadAdapter, "_post_json", send)
    assert client.post("/messages/send", data=form, files={"file": ("bad.gif", b"not an image")}).status_code == 400
    result = client.post("/messages/send", data=form, files={"file": ("dance.gif", data, "text/html")}).json()
    assert result["ok"], result
    encoded = base64.b64encode(data).decode()
    with ids["factory"]() as db:
        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.message_id == result["message_id"]))
        assert attachment.mime_type == "image/gif" and Path(attachment.storage_path).read_bytes() == data
        msg = db.get(Message, result["message_id"])
        assert msg.external_message_id == "12345" and encoded not in msg.raw_request_json
        denied = WechatIpadAdapter().send_message(db.get(Channel, ids["channel_id"]), db.get(Conversation, cid),
            {"message_type": "emoji", "receiver_id": "other", "emoji_base64": encoded})
        assert denied["error_code"] == "invalid_target"
    assert len(calls) == 1 and calls[0][0].endswith("/api/Msg/UploadEmojiData")
    assert calls[0][1] == {"Wxid": "bot", "ToWxid": "room@chatroom", "Base64": "data:image/gif;base64," + encoded}
