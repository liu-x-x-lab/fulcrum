from io import BytesIO
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from plugins.image_source_search import service
from plugins.image_source_search import plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse


def test_only_matching_pages_and_evidenced_profile():
    page = "https://x.com/example/status/123"
    data = {"responses": [{"webDetection": {
        "visuallySimilarImages": [{"url": "https://example.org/unrelated"}],
        "pagesWithMatchingImages": [
            {"url": page, "fullMatchingImages": [{}]}, {"url": page},
            {"url": "javascript:alert(1)"}
        ]}}]}
    assert service.web_results(data) == [
        f"匹配网页：{page}\n发帖账号：https://x.com/example"
    ]


def test_anime_requires_confident_match():
    item = {"similarity": .96, "anilist": {"id": 42, "title": {"native": "示例"}},
            "episode": 2}
    assert service.anime_results({"result": [item]}) == [
        "动画：示例 · 第 2 集\nhttps://anilist.co/anime/42"
    ]
    assert not service.anime_results({"result": [dict(item, similarity=.6)]})


def test_upload_reencodes_image_without_private_metadata():
    buffer = BytesIO()
    Image.new("RGB", (3, 2)).save(buffer, format="JPEG", comment=b"private marker")
    image = service.search_jpeg(buffer.getvalue())
    assert Image.open(BytesIO(image)).size == (3, 2)
    assert b"private marker" not in image
    with pytest.raises(ValueError):
        service.search_jpeg(b"not an image")


def test_explicit_command_only():
    def matches(text):
        return plugin.matches_event(SimpleNamespace(content=text, binding_config={}))
    assert matches("找鸡 https://example.org/image")
    assert not matches("/找鸡蛋")
    assert not matches("这张图是哪来的")


def test_scoped_quote_to_reply(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.core.db import Base
    from app.core.tenant_context import tenant_scope
    from app.models.entities import Conversation, Message, MessageAttachment

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    ctx = PluginExecutionContext(tenant_id=1, binding_id=1, channel_id=1,
                                 bot_id=1, conversation_id=1, message_id=10, sender_id="reader")
    ref = "9876543210987654321"
    with tenant_scope(1), Session(engine) as db:
        for room in (1, 2):
            db.add(Conversation(id=room, tenant_id=1, bot_id=1, channel_id=1, external_id=str(room),
                                channel_type="wechat_ipad", chat_type="group", title="Example"))
            db.add(Message(id=room, tenant_id=1, conversation_id=room, direction="in", message_type="image",
                           external_message_id=str(room), content="[image]",
                           raw_request_json=json.dumps({"native_ids": {"server": int(ref)}})))
            root = tmp_path / "inbound_media" / "channel_1"
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"attachment_{room}.png"
            Image.new("RGB", (room, 2)).save(path)
            db.add(MessageAttachment(id=room, tenant_id=1, channel_id=1, conversation_id=room,
                                     message_id=room, direction="in", attachment_type="image",
                                     status="downloaded", storage_path=str(path)))
        current = Message(id=10, tenant_id=1, conversation_id=1, direction="in", sender_id="reader",
                          content="找鸡", raw_request_json=json.dumps({"quote": {"ref_type": 3, "ref_external_id": ref}}))
        db.add(current)
        db.flush()
        monkeypatch.setattr(plugin, "settings", SimpleNamespace(data_dir=tmp_path))

        def fake_request(self, method, url, **kwargs):
            assert method == "POST" and url.startswith("https://vision.googleapis.com/")
            import base64
            content = kwargs["json_body"]["requests"][0]["image"]["content"]
            assert Image.open(BytesIO(base64.b64decode(content))).size == (1, 2)
            return PluginHttpResponse(200, {}, b'{"responses":[{"webDetection":{"pagesWithMatchingImages":'
                                      b'[{"url":"https://example.org/post/1"}]}}]}', url)

        monkeypatch.setattr(service.PluginHttpClient, "request", fake_request)
        request = PluginExecutionRequest("execute", {"vision_api_key": "test"}, {}, ctx)
        result = plugin.execute_message_action(db, request)
        assert result.status == "found" and "https://example.org/post/1" in result.output["reply_text"]
        # 即使其他群有对应图片，当前群没有已下载的附件也不能借用。
        db.delete(db.get(MessageAttachment, 1))
        db.flush()
        assert plugin.execute_message_action(db, request).status == "missing_image"
    engine.dispose()


def test_quota_failure_is_not_an_empty_result(monkeypatch):
    monkeypatch.setattr(service.PluginHttpClient, "request",
                        lambda *a, **kw: PluginHttpResponse(429, {}, b"", ""))
    status, text = service.search(b"example", "test", False)
    assert status == "provider_error" and "限流" in text and "没有匹配" not in text
