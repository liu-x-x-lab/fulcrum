"""引用媒体的严格归属、异步获取和 AI 消费闭环；不联系手机或真实模型。"""

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Channel, Chatroom, Conversation, Message, MessageAttachment, TaskJob
from app.services import ai_chat, ai_dispatch, ai_intent_worker, ai_media, ai_providers, ai_vision, inbound_media, plugin_directory
from app.services.task_queue import AI_INTENT_JOB, enqueue_job
from tests.test_ai_dispatch import _seed
from tests.test_ai_vision import image_bytes


@pytest.fixture
def media_context(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'media.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(ai_intent_worker, "TenantSessionLocal", factory)
    with factory() as db:
        _seed(db)
    with tenant_scope(1), tenant_session(factory) as db:
        channel = db.get(Channel, 1)
        channel.api_base = "https://fulcrum.example/api/wechat-webridge/1"
        visual = ai_providers.save_provider(db, provider_id=None, name="Vision", api_base="https://vision.example/v1",
            api_key="visual-key", enabled=True, manual_models="vision")
        ai_dispatch.save_ai_setting(db, enabled=True, provider_id=ai_dispatch.get_ai_setting(db).provider_id,
            model="text", chat_enabled=True, vision_enabled=True, vision_provider_id=visual.id, vision_model="vision")
        conversation = Conversation(bot_id=1, channel_id=1, channel_type="wechat_webridge", external_id="room@chatroom", chat_type="group")
        room = Chatroom(bot_id=1, channel_id=1, external_id="room@chatroom", name="虚构测试群", is_active=True)
        db.add_all([conversation, room])
        db.flush()
        original = Message(conversation_id=conversation.id, direction="in", sender_id="alice", content="[图片]", message_type="image", external_message_id="9007199254740993", status="received")
        db.add(original)
        db.flush()
        quote = {"ref_type": 3, "ref_external_id": original.external_message_id, "title": "@bot 看看图里有什么"}
        current = Message(conversation_id=conversation.id, direction="in", sender_id="bob", content=quote["title"], message_type="quote", external_message_id="quote-1", raw_request_json=json.dumps({"quote": quote}), status="received")
        db.add(current)
        db.flush()
        event = {
            "conversation": {"external_id": conversation.external_id, "chat_type": "group"},
            "message": {"message_type": "quote", "content": current.content, "sender_id": "bob", "external_message_id": "quote-1"},
            "details": {"mentioned_self": True, "quote_payload": quote},
        }
        payload = {"channel_id": 1, "conversation_id": conversation.id, "message_id": current.id, "normalized_event": event}
        db.commit()
        yield SimpleNamespace(db=db, channel=channel, conversation=conversation, original=original, current=current,
                              payload=payload, factory=factory, room=room)
    engine.dispose()


def _ready(context, *, content=None):
    attachment = inbound_media.create_wechat_media_attachment(
        context.db, channel=context.channel, raw_event_id=None, conversation_id=context.conversation.id,
        message_id=context.original.id, attachment_type=context.original.message_type, metadata={}, provider="wechat_webridge",
    )
    inbound_media.store_uploaded_media_attachment(context.db, attachment=attachment, content=content or image_bytes(), filename="sample.png", content_type="image/png")
    context.db.commit()
    return attachment


def test_reference_uses_exact_media_not_latest_image(media_context):
    context = media_context
    attachment = _ready(context)
    context.db.add(Message(conversation_id=context.conversation.id, direction="in", sender_id="other", content="newer", message_type="image", external_message_id="newer"))
    context.db.flush()
    media = ai_media.prepare_input(context.db, context.channel, context.payload, enabled=True)
    assert media.state == "ready" and media.attachment_id == attachment.id
    assert ai_media.read_media(media, max_bytes=ai_vision.MAX_IMAGE_BYTES) == image_bytes()


@pytest.mark.parametrize("change", ["tenant", "conversation", "recalled", "duplicate", "missing"])
def test_foreign_missing_recalled_or_ambiguous_references_are_never_read(media_context, change):
    context = media_context
    _ready(context)
    if change == "tenant":
        context.db.commit()
        with context.factory() as platform_db:
            platform_db.get(Message, context.original.id).tenant_id = 2
            platform_db.commit()
        context.db.expire_all()
    elif change == "conversation":
        context.original.conversation_id = 999
    elif change == "recalled":
        context.original.status = "recalled"
    elif change == "duplicate":
        context.db.add(Message(conversation_id=context.conversation.id, direction="in", sender_id="other", content="duplicate", message_type="image", external_message_id=context.original.external_message_id))
    else:
        context.original.external_message_id = "not-the-reference"
    context.db.flush()
    media = ai_media.prepare_input(context.db, context.channel, context.payload, enabled=True)
    assert media.state == "error" and not media.storage_path


def test_native_server_id_is_exact_and_not_confused_with_local_id(media_context):
    context = media_context
    _ready(context)
    context.original.raw_request_json = json.dumps({"native_ids": {"server": 9007199254740993, "local": 123}})
    context.original.external_message_id = "123"
    context.db.flush()
    assert ai_media.prepare_input(context.db, context.channel, context.payload, enabled=True).state == "ready"
    context.original.raw_request_json = json.dumps({"native_ids": {"server": 9007199254740992}})
    context.original.external_message_id = "9007199254740993"
    context.db.flush()
    assert ai_media.prepare_input(context.db, context.channel, context.payload, enabled=True).state == "error"


def test_disabled_vision_never_downloads_media(media_context):
    context = media_context
    media = ai_media.prepare_input(context.db, context.channel, context.payload, enabled=False)
    assert media.state == "error"
    assert context.db.scalar(select(TaskJob.id)) is None


def test_download_is_queued_once_and_resumed_from_ready_attachment(media_context):
    context = media_context
    first = ai_media.prepare_input(context.db, context.channel, context.payload, enabled=True)
    second = ai_media.prepare_input(context.db, context.channel, context.payload, enabled=True)
    assert first.state == second.state == "loading"
    assert len(context.db.scalars(select(TaskJob)).all()) == 1
    attachment = context.db.scalar(select(MessageAttachment))
    inbound_media.store_uploaded_media_attachment(context.db, attachment=attachment, content=image_bytes(), filename="image.png")
    context.db.flush()
    assert ai_media.prepare_input(context.db, context.channel, context.payload, enabled=True, allow_request=False).state == "ready"


def test_arbitrary_and_sibling_attachment_paths_are_rejected(media_context, tmp_path):
    context = media_context
    attachment = _ready(context)
    sibling = Path(attachment.storage_path).with_name("attachment_999.png")
    sibling.write_bytes(image_bytes())
    for path in [str(tmp_path / "outside.png"), str(sibling), "https://example.test/image.png"]:
        media = ai_media.MediaInput("image", state="ready", channel_id=1, attachment_id=attachment.id, storage_path=path)
        with pytest.raises(ai_vision.VisionError, match="不属于当前附件"):
            ai_media.read_media(media, max_bytes=ai_vision.MAX_IMAGE_BYTES)


def test_worker_waits_without_calling_model_then_uses_visual_config(media_context, monkeypatch):
    context = media_context
    seen, sent = [], []
    monkeypatch.setattr(ai_dispatch, "analyze_intent", lambda *args: pytest.fail("引用媒体不能先猜文字指令"))
    monkeypatch.setattr(ai_chat, "prepare_context", lambda *args: pytest.fail("媒体问题不自动搜索或写入记忆"))
    monkeypatch.setattr(ai_intent_worker.ai_memory, "retain", lambda *args, **kwargs: pytest.fail("不自动保存媒体推断"))
    monkeypatch.setattr(plugin_directory, "send_ai_reply", lambda db, channel, event, text, **kwargs: sent.append(text))

    class Client:
        def generate(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(text="图中是一块红色区域")

    def build(config):
        assert config.model == "vision" and config.api_key == "visual-key"
        return Client()

    monkeypatch.setattr(ai_chat, "_build_client", build)
    plan = plugin_directory.build_static_dispatch_plan(context.db, context.channel, context.payload["normalized_event"])
    assert plugin_directory._enqueue_ai_intent(context.db, plan, context.payload["normalized_event"], raw_event_id=None,
                                               conversation_id=context.conversation.id, message_id=context.current.id)
    context.db.commit()
    job_id = context.db.scalar(select(TaskJob.id).where(TaskJob.job_type == AI_INTENT_JOB))
    ai_intent_worker._execute_intent(job_id)
    context.db.expire_all()
    job = context.db.get(TaskJob, job_id)
    assert job.status == "pending" and "media_wait_started" in job.result_json
    assert seen == [] and sent == []
    attachment = context.db.scalar(select(MessageAttachment))
    inbound_media.store_uploaded_media_attachment(context.db, attachment=attachment, content=image_bytes(), filename="image.png")
    context.db.commit()
    ai_intent_worker._execute_intent(job_id)
    context.db.expire_all()
    assert context.db.get(TaskJob, job_id).status == "completed"
    assert len(seen) == 1 and len(seen[0]["images"]) == 1
    assert sent == ["图中是一块红色区域"]


def test_worker_reports_download_timeout_without_requeue_or_model_call(media_context, monkeypatch):
    context = media_context
    sent = []
    monkeypatch.setattr(plugin_directory, "send_ai_reply", lambda db, channel, event, text, **kwargs: sent.append(text))
    monkeypatch.setattr(ai_chat, "_build_client", lambda *args: pytest.fail("超时不能调用模型"))
    job = enqueue_job(context.db, job_type=AI_INTENT_JOB, payload=context.payload, max_attempts=1)
    context.db.commit()
    ai_intent_worker._execute_intent(job.id)
    context.db.expire_all()
    job.result_json = json.dumps({"media_wait_started": (now_shanghai() - timedelta(seconds=160)).isoformat()})
    context.db.commit()
    ai_intent_worker._execute_intent(job.id)
    context.db.expire_all()
    assert context.db.get(TaskJob, job.id).status == "completed"
    assert len(sent) == 1 and "超时" in sent[0]


def test_video_reply_discloses_sampling_and_never_uses_text_model(media_context, monkeypatch):
    context = media_context
    context.original.message_type = "video"
    quote = json.loads(context.current.raw_request_json)["quote"]
    quote["ref_type"] = 43
    context.current.raw_request_json = json.dumps({"quote": quote})
    _ready(context, content=b"\x00\x00\x00\x20ftypisom")
    monkeypatch.setattr(ai_vision, "sample_video", lambda data: ((ai_vision.normalize_image(image_bytes(), caption="1 秒"),), "视频抽样说明"))
    monkeypatch.setattr(ai_chat, "_build_client", lambda config: SimpleNamespace(generate=lambda **kwargs: SimpleNamespace(text="有红色画面")))
    config, _text, _catalog, chat = ai_dispatch.prepare_intent(context.db, context.payload)
    response = ai_chat.generate_reply(config, chat)
    assert response.startswith(ai_vision.VIDEO_DISCLOSURE) and "红色画面" in response


def test_visual_provider_failure_is_not_silent_or_fallback(media_context, monkeypatch):
    from app.services.plugin_runtime.text_generation import PluginTextGenerationError

    context = media_context
    _ready(context)
    def generate(**kwargs):
        raise PluginTextGenerationError("secret upstream payload")
    monkeypatch.setattr(ai_chat, "_build_client", lambda config: SimpleNamespace(generate=generate))
    config, _text, _catalog, chat = ai_dispatch.prepare_intent(context.db, context.payload)
    with pytest.raises(ai_vision.VisionError, match="视觉模型调用失败") as error:
        ai_chat.generate_reply(config, chat)
    assert "secret" not in str(error.value)


def test_group_can_disable_visual_without_changing_its_text_model(media_context):
    from app.services.group_features.store import save_state

    context = media_context
    save_state(context.room, "ai_chat", enabled=True, config={"vision_enabled": False, "model": "group-chat"})
    context.db.commit()
    config, _text, _catalog, chat = ai_dispatch.prepare_intent(context.db, context.payload)
    assert config.model == "group-chat" and chat.vision_config is None
    assert chat.media.state == "error"
    assert context.db.scalar(select(TaskJob.id)) is None


def test_ipad_on_demand_uses_archived_metadata_and_existing_worker(media_context):
    from app.models.entities import RawWebhookEvent
    from app.services import message_media
    from app.services.task_queue import INBOUND_MEDIA_FETCH_JOB

    context = media_context
    context.channel.channel_type = "wechat_ipad"
    context.channel.adapter_config_json = json.dumps({"wxid": "bot"})
    context.db.add(RawWebhookEvent(channel_id=1, event_key=context.original.external_message_id, source="wechat_ipad",
        payload_json=json.dumps({
            "MsgId": context.original.external_message_id, "MsgType": 3,
            "FromUserName": context.conversation.external_id, "ToUserName": "bot",
            "Content": '<msg><img aeskey="fake-key" cdnmidimgurl="fake-file-id" md5="fake-md5" /></msg>',
        })))
    context.db.commit()
    result = message_media.request(context.db, context.original)
    assert result["state"] == "loading"
    attachment = context.db.get(MessageAttachment, result["attachment_id"])
    assert json.loads(attachment.metadata_json)["file_no"] == "fake-file-id"
    assert context.db.scalar(select(TaskJob)).job_type == INBOUND_MEDIA_FETCH_JOB
    assert message_media.request(context.db, context.original)["attachment_id"] == attachment.id
    assert len(context.db.scalars(select(TaskJob)).all()) == 1


def test_auto_download_and_visual_request_share_one_upload(media_context, monkeypatch):
    from app.services import channel_events

    context = media_context
    monkeypatch.setattr(channel_events, "_chatroom_auto_download_media_enabled", lambda *args, **kwargs: True)
    channel_events._queue_inbound_media_fetch_if_needed(
        context.db, channel=context.channel, conversation=context.conversation, raw_event=SimpleNamespace(id=None),
        normalized={"details": {"media": {}}}, message=context.original,
    )
    result = ai_media.prepare_input(context.db, context.channel, context.payload, enabled=True)
    assert result.state == "loading"
    assert len(context.db.scalars(select(TaskJob)).all()) == 1


@pytest.mark.parametrize("target", ["original", "current"])
def test_recall_during_analysis_prevents_delayed_media_reply(media_context, monkeypatch, target):
    context = media_context
    _ready(context)
    getattr(context, target).status = "recalled"
    context.db.commit()
    monkeypatch.setattr(plugin_directory, "send_ai_reply", lambda *args, **kwargs: pytest.fail("不得复活撤回内容"))
    result = ai_intent_worker._deliver(context.db, context.payload, ai_dispatch.AiIntent(status="chatted"), "识别结果",
                                       media_message_id=context.original.id)
    assert result == {"status": "skipped", "reason": "referenced_media_unavailable"}
