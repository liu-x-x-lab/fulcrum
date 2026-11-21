"""原生绘图的精确引用、付费幂等、额度与慢调用期间权限重验。"""

import copy
import json
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.settings import settings
from app.core.time import now_shanghai
from app.models.entities import AiImageAttempt, Message, TaskJob, TenantAiSetting
from app.services import ai_dispatch, ai_drawing, ai_drawing_jobs, ai_intent_worker, channel_adapters, media_library, outbound_files, plugin_directory
from app.services.group_features.store import save_state
from app.services.task_queue import AI_INTENT_JOB, enqueue_job
from tests.test_ai_drawing import picture_bytes
from tests.test_ai_media import _ready, media_context  # noqa: F401
from tests.test_ai_providers import add_provider


def _configure(context, *, edit=True, enabled=True, limit=20):
    provider = add_provider(context.db)
    setting = ai_dispatch.get_ai_setting(context.db)
    setting.image_enabled, setting.image_provider_id, setting.image_model = True, provider.id, "image-model"
    setting.image_daily_limit = limit
    setting.vision_enabled = False
    save_state(context.room, "ai_chat", enabled=True, config={"image_enabled": enabled})
    message_type = "quote" if edit else "text"
    content = "@bot 改图 把背景换成蓝色" if edit else "@bot 画图 一只猫"
    context.current.content, context.current.message_type = content, message_type
    context.payload["normalized_event"]["message"].update(content=content, message_type=message_type)
    context.db.commit()


def _enqueue(context):
    job = enqueue_job(context.db, job_type=AI_INTENT_JOB, payload=context.payload, max_attempts=1)
    context.db.flush()
    job_id = job.id
    context.db.commit()
    return job_id


def _fake_output(monkeypatch, *, failure=None):
    called, sent = [], []

    def generate(config, prompt, **kwargs):
        called.append((config, prompt, kwargs))
        if failure:
            raise failure
        return picture_bytes()

    def send(db, channel, event, text, **kwargs):
        sent.append((text, kwargs))
        return [{"ok": True}]

    monkeypatch.setattr(ai_drawing, "generate", generate)
    monkeypatch.setattr(plugin_directory, "send_ai_reply", send)
    return called, sent


@pytest.mark.parametrize("edit,outgoing", [(False, False), (True, False), (True, True)])
def test_native_drawing_uses_exact_quote_and_independent_provider(media_context, monkeypatch, edit, outgoing):
    context = media_context
    _configure(context, edit=edit)
    if edit:
        context.original.direction = "out" if outgoing else "in"
        _ready(context, content=picture_bytes())
    called, sent = _fake_output(monkeypatch)
    job_id = _enqueue(context)
    ai_intent_worker._execute_intent(job_id)
    assert len(called) == 1 and len(sent) == 1
    config, prompt, options = called[0]
    assert config.api_key == "image-only" and config.model == "image-model"
    assert bool(options["reference"]) == edit and prompt
    assert sent[0][1]["replies"][0]["type"] == "image"
    context.db.expire_all()
    attempt = context.db.scalar(select(AiImageAttempt))
    assert attempt.status == "queued"
    assert attempt.source_message_id == (context.original.id if edit else None)
    assert context.db.get(TaskJob, job_id).status == "completed"
    ai_intent_worker._execute_intent(job_id)
    assert len(called) == len(sent) == 1


def test_media_wait_never_calls_model_and_resumes_once(media_context, monkeypatch):
    context = media_context
    _configure(context)
    called, sent = _fake_output(monkeypatch)
    job_id = _enqueue(context)
    ai_intent_worker._execute_intent(job_id)
    assert not called and not sent
    context.db.expire_all()
    assert context.db.get(TaskJob, job_id).status == "pending"
    assert context.db.scalar(select(AiImageAttempt)) is None
    _ready(context, content=picture_bytes())
    ai_intent_worker._execute_intent(job_id)
    assert len(called) == 1


@pytest.mark.parametrize("change", ["disabled", "missing", "recalled", "video", "corrupt", "dry_run"])
def test_invalid_or_disabled_request_never_reserves_paid_attempt(media_context, monkeypatch, change):
    context = media_context
    _configure(context, enabled=change != "disabled")
    if change == "missing":
        context.original.external_message_id = "different"
    elif change == "recalled":
        context.original.status = "recalled"
    elif change == "video":
        quote = json.loads(context.current.raw_request_json)
        quote["quote"]["ref_type"] = 43
        context.current.raw_request_json = json.dumps(quote)
    elif change == "dry_run":
        context.payload["normalized_event"]["details"]["dry_run_outbound"] = True
    _ready(context, content=b"broken-image" if change == "corrupt" else picture_bytes())
    called, sent = _fake_output(monkeypatch)
    ai_intent_worker._execute_intent(_enqueue(context))
    assert not called and len(sent) == 1 and not sent[0][1]["replies"]
    assert context.db.scalar(select(AiImageAttempt)) is None


@pytest.mark.parametrize("change", ["sender", "content", "conversation", "tenant"])
def test_forged_payload_or_scope_is_rejected_before_payment(media_context, change):
    context = media_context
    _configure(context, edit=False)
    payload = copy.deepcopy(context.payload)
    if change in {"sender", "content"}:
        payload["normalized_event"]["message"]["sender_id" if change == "sender" else "content"] = "forged" if change == "sender" else "@bot 画图 forged prompt"
    elif change == "conversation":
        payload["normalized_event"]["conversation"]["external_id"] = "another@chatroom"
    else:
        context.db.commit()
        with context.factory() as db:
            db.get(Message, context.current.id).tenant_id = 2
            db.commit()
        context.db.expire_all()
    with pytest.raises(ai_drawing.DrawingError):
        ai_drawing_jobs.prepare(context.db, payload)


@pytest.mark.parametrize("status", ["started", "error", "delivery_failed", "queued"])
def test_recovered_existing_attempt_never_redraws(media_context, monkeypatch, status):
    context = media_context
    _configure(context, edit=False)
    context.db.add(AiImageAttempt(channel_id=context.channel.id, conversation_id=context.conversation.id,
        message_id=context.current.id, status=status))
    called, sent = _fake_output(monkeypatch)
    ai_intent_worker._execute_intent(_enqueue(context))
    assert not called and not sent


def test_uncertain_call_is_charged_once_and_quota_blocks_next_request(media_context, monkeypatch):
    context = media_context
    _configure(context, edit=False, limit=1)
    called, sent = _fake_output(monkeypatch, failure=ai_drawing.DrawingError("结果不确定；未自动重试"))
    first_job = _enqueue(context)
    ai_intent_worker._execute_intent(first_job)
    context.db.expire_all()
    assert context.db.scalar(select(AiImageAttempt)).status == "error"
    next_message = Message(conversation_id=context.conversation.id, direction="in", sender_id="bob",
        content=context.current.content, message_type="text", external_message_id="second")
    context.db.add(next_message)
    context.db.flush()
    context.payload["message_id"] = next_message.id
    second_job = _enqueue(context)
    ai_intent_worker._execute_intent(second_job)
    assert len(called) == 1 and "次数已用完" in sent[-1][0]
    assert ai_dispatch.used_today(context.db) == 0


@pytest.mark.parametrize("change", ["recalled", "disabled"])
def test_network_call_has_no_held_sessions_and_rechecks_scope(media_context, monkeypatch, change):
    context = media_context
    _configure(context)
    _ready(context, content=picture_bytes())
    job_id = _enqueue(context)
    source_id = context.payload["message_id"]
    active = []
    original_session = ai_drawing_jobs.tenant_session

    @contextmanager
    def counted(factory):
        with original_session(factory) as db:
            active.append(db)
            try:
                yield db
            finally:
                active.remove(db)

    monkeypatch.setattr(ai_drawing_jobs, "tenant_session", counted)
    monkeypatch.setattr(ai_intent_worker, "tenant_session", counted)
    called, sent = _fake_output(monkeypatch)

    def generate(*args, **kwargs):
        assert active == []
        with context.factory() as db:
            assert db.scalar(select(AiImageAttempt)).status == "started"
            if change == "recalled":
                db.get(Message, source_id).status = "recalled"
            else:
                db.scalar(select(TenantAiSetting)).image_enabled = False
            db.commit()
        return picture_bytes()

    monkeypatch.setattr(ai_drawing, "generate", generate)
    ai_intent_worker._execute_intent(job_id)
    assert sent == []
    assert context.db.scalar(select(AiImageAttempt)).status == "cancelled"


def test_failed_delivery_is_terminal_without_new_generation(media_context, monkeypatch):
    context = media_context
    _configure(context, edit=False)
    called, _sent = _fake_output(monkeypatch)
    monkeypatch.setattr(plugin_directory, "send_ai_reply", lambda *args, **kwargs: [{"ok": False}])
    job_id = _enqueue(context)
    ai_intent_worker._execute_intent(job_id)
    assert context.db.get(TaskJob, job_id).status == "failed"
    assert context.db.scalar(select(AiImageAttempt)).status == "delivery_failed"
    ai_intent_worker._execute_intent(job_id)
    assert len(called) == 1


@pytest.mark.parametrize("same_message", [True, False])
def test_concurrent_reservations_cannot_duplicate_or_exceed_quota(media_context, same_message):
    context = media_context
    _configure(context, edit=False, limit=1)
    payloads = [copy.deepcopy(context.payload), copy.deepcopy(context.payload)]
    if not same_message:
        other = Message(conversation_id=context.conversation.id, direction="in", sender_id="bob",
            content=context.current.content, message_type="text", external_message_id="parallel")
        context.db.add(other)
        context.db.flush()
        payloads[1]["message_id"] = other.id
    drawing = ai_drawing_jobs.prepare(context.db, context.payload)
    context.db.commit()
    barrier = threading.Barrier(2)

    def reserve(payload):
        with tenant_scope(1), tenant_session(context.factory) as db:
            barrier.wait(timeout=10)
            try:
                attempt, _config = ai_drawing_jobs._reserve(db, payload, drawing)
                result = attempt.id if attempt else None
                db.commit()
                return result
            except ai_drawing.DrawingError as exc:
                assert "次数已用完" in str(exc)
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, payloads))
    assert sum(result is not None for result in results) == 1
    assert len(context.db.scalars(select(AiImageAttempt)).all()) == 1


@pytest.mark.parametrize("channel_type,edit,delivery_ok", [
    ("simulator", False, True), ("wechat_webridge", False, True), ("wechat_webridge", True, False),
])
def test_native_image_retention_uses_standard_outbound_queue(media_context, monkeypatch, channel_type, edit, delivery_ok):
    context = media_context
    _configure(context, edit=edit)
    if edit:
        _ready(context, content=picture_bytes())
    context.channel.channel_type = context.conversation.channel_type = channel_type
    monkeypatch.setattr(outbound_files, "OUTBOUND_FILE_ROOT", Path(settings.data_dir) / "tenants")
    monkeypatch.setattr(settings, "public_base_url", "https://fulcrum.example")
    generated, delivered = [], []

    def generate(*args, **kwargs):
        generated.append(kwargs)
        return picture_bytes()

    def dispatch(db, channel, conversation, payload, **kwargs):
        delivered.append(payload)
        message = Message(conversation_id=conversation.id, direction="out", message_type="image", content="[图片]",
            status="pending" if delivery_ok else "failed", external_status="pending" if delivery_ok else "failed")
        db.add(message)
        db.flush()
        return message, {"ok": delivery_ok}

    monkeypatch.setattr(ai_drawing, "generate", generate)
    monkeypatch.setattr(channel_adapters, "dispatch_outbound_message", dispatch)
    job_id = _enqueue(context)
    ai_intent_worker._execute_intent(job_id)
    context.db.expire_all()
    jobs = context.db.scalars(select(TaskJob).where(TaskJob.job_type != AI_INTENT_JOB)).all()
    origin = "ai_edited" if edit else "ai_generated"
    items = media_library.catalog(context.db, source=origin)["items"]
    assert len(items) == 1
    item = items[0]
    assert item.path.read_bytes() == picture_bytes() and item.source_message_id == context.current.id
    assert item.conversation_id == context.conversation.id
    assert len(list(item.path.parent.parent.glob("*/meta.json"))) == 1
    if channel_type == "simulator":
        assert len(jobs) == 1 and not delivered
        outbound = json.loads(jobs[0].payload_json)
        assert outbound["plugin_code"] == "ai_dispatch" and outbound["channel_id"] == context.channel.id
        assert outbound["payload"]["message_type"] == "image" and outbound["payload"]["base64"]
        assert item.metadata["job_id"] == jobs[0].id
    else:
        assert not jobs and len(delivered) == 1
        assert "base64" not in delivered[0] and item.path.parent.name in delivered[0]["image_url"]
        assert item.metadata["message_id"]
    item.created_at = now_shanghai() - timedelta(days=1)
    if delivery_ok:
        with pytest.raises(ValueError, match="暂不能删除"):
            media_library.check_delete(context.db, item)
    else:
        media_library.check_delete(context.db, item)
    assert context.db.scalar(select(AiImageAttempt)).status == ("queued" if delivery_ok else "delivery_failed")
    ai_intent_worker._execute_intent(job_id)
    assert len(generated) == 1 and item.path.exists()
