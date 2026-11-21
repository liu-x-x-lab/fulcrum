"""媒体库的真实登录、租户边界、原件下载及受保护清理。"""

import io
import json
import wave
import re
from datetime import timedelta
from pathlib import Path

import pytest
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation, Message, MessageAttachment, TaskJob
from app.services import inbound_media, media_dedup, media_files, media_library, media_storage, outbound_files
from tests.media_audio_fixtures import silk_bytes
from tests.test_ai_drawing import picture_bytes
from tests.test_provider_node_pages import _login, provider_page_context  # noqa: F401


@pytest.fixture
def library_context(provider_page_context, tmp_path, monkeypatch):
    client, ids = provider_page_context
    data = tmp_path / "media"
    monkeypatch.setattr(settings, "data_dir", str(data))
    monkeypatch.setattr(outbound_files, "OUTBOUND_FILE_ROOT", data / "tenants")
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        channel = db.get(Channel, ids["channel_id"])
        conversation = Conversation(bot_id=channel.bot_id, channel_id=channel.id, channel_type=channel.channel_type,
                                    external_id="fixture-room", title="示例群", chat_type="group")
        db.add(conversation)
        db.flush()
        message = Message(conversation_id=conversation.id, message_type="voice", content="[语音]", direction="in", status="received")
        db.add(message)
        db.flush()
        attachment = inbound_media.create_wechat_media_attachment(db, channel=channel, raw_event_id=None,
            conversation_id=conversation.id, message_id=message.id, attachment_type="voice", metadata={})
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as sound:
            sound.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            sound.writeframes(b"\x00\x00" * 8000)
        inbound_media.store_uploaded_media_attachment(db, attachment=attachment, content=buffer.getvalue(), filename="voice.wav")
        db.commit()
        ids.update(attachment_id=attachment.id, conversation_id=conversation.id, message_id=message.id,
                   attachment_path=attachment.storage_path)
        stored = outbound_files.store_outbound_upload(tenant_id=channel.tenant_id, channel_id=channel.id,
            file_name="generated.png", content=picture_bytes(), content_type="image/png", message_type="image",
            provenance={"origin": "ai_generated", "conversation_id": conversation.id, "source_message_id": message.id})
        metadata = stored["metadata"]
        metadata["created_at"] = (now_shanghai() - timedelta(days=1)).isoformat()
        (stored["target_path"].parent / "meta.json").write_text(json.dumps(metadata))
        ids.update(stored=stored, outbound_ref=f"o{channel.id}-{stored['share_id']}")
    yield client, ids


def test_listing_filters_sources_and_original_download(library_context):
    client, ids = library_context
    _login(client, "admin")
    response = client.get("/media")
    assert response.status_code == 200 and "generated.png" in response.text and "voice.wav" in response.text
    assert "媒体库" in response.text and "AI 生图" in response.text
    assert "generated.png" not in client.get("/media?kind=audio").text
    assert "voice.wav" not in client.get("/media?source=ai_generated").text
    assert "generated.png" not in client.get("/media?query=missing").text
    original = client.get(f"/media/items/a{ids['attachment_id']}/file?download=1")
    assert original.status_code == 200 and original.content == Path(ids["attachment_path"]).read_bytes()
    assert "attachment;" in original.headers["content-disposition"]
    picture = client.get(f"/media/items/{ids['outbound_ref']}/file")
    assert picture.content == picture_bytes() and picture.headers["content-type"] == "image/png"
    partial = client.get(f"/media/items/{ids['outbound_ref']}/file", headers={"Range": "bytes=0-7"})
    assert partial.status_code == 206 and partial.content == picture_bytes()[:8]


def test_readonly_and_csrf_protect_deletion(library_context):
    client, ids = library_context
    _login(client, "readonly")
    assert "data-media-delete" not in client.get("/media").text
    assert client.get(f"/media/items/{ids['outbound_ref']}/file").status_code == 200
    assert client.post("/media/delete", data={"refs": ids["outbound_ref"]}).status_code == 403
    _login(client, "admin")
    assert client.post("/media/delete", data={"refs": ids["outbound_ref"], "csrf_token": "invalid"}).status_code == 403
    assert ids["stored"]["target_path"].exists()


def test_delete_keeps_messages_and_marks_attachments(library_context):
    client, ids = library_context
    token = _login(client, "admin")
    response = client.post("/media/delete", data={"refs": [f"a{ids['attachment_id']}", ids["outbound_ref"]], "csrf_token": token})
    assert response.status_code == 303
    assert not Path(ids["attachment_path"]).exists() and not ids["stored"]["target_path"].exists()
    with ids["factory"]() as db:
        assert db.get(Message, ids["message_id"]) is not None
        assert db.get(MessageAttachment, ids["attachment_id"]).status == "deleted"
    assert client.get(f"/media/items/{ids['outbound_ref']}/file").status_code == 404


@pytest.mark.parametrize("protection", ["queued", "fresh"])
def test_active_or_recent_files_block_whole_batch(library_context, protection):
    client, ids = library_context
    token = _login(client, "admin")
    if protection == "queued":
        with ids["factory"]() as db:
            db.add(TaskJob(tenant_id=ids["alpha"], job_type="outbound_dispatch", status="pending",
                payload_json=json.dumps({"url": "/outbound-files/" + ids["stored"]["share_id"] + "/generated.png"})))
            db.commit()
    else:
        metadata = dict(ids["stored"]["metadata"], created_at=now_shanghai().isoformat())
        (ids["stored"]["target_path"].parent / "meta.json").write_text(json.dumps(metadata))
    client.post("/media/delete", data={"refs": [f"a{ids['attachment_id']}", ids["outbound_ref"]], "csrf_token": token})
    assert Path(ids["attachment_path"]).exists() and ids["stored"]["target_path"].exists()


def test_foreign_channels_and_file_paths_never_expose_bytes(library_context, tmp_path):
    client, ids = library_context
    token = _login(client, "admin")
    secret = tmp_path / "private.txt"
    secret.write_text("not-media-secret")
    with ids["factory"]() as db:
        row = db.get(MessageAttachment, ids["attachment_id"])
        row.storage_path = str(secret)
        db.commit()
    assert client.get(f"/media/items/a{ids['attachment_id']}/file").status_code == 404
    assert client.get("/media/items/o999-" + ids["stored"]["share_id"] + "/file").status_code == 404
    client.post("/media/delete", data={"refs": [f"a{ids['attachment_id']}", ids["outbound_ref"]], "csrf_token": token})
    assert secret.read_text() == "not-media-secret" and ids["stored"]["target_path"].exists()
    with ids["factory"]() as db:
        db.get(MessageAttachment, ids["attachment_id"]).tenant_id = ids["beta"]
        db.commit()
    assert client.get(f"/media/items/a{ids['attachment_id']}/file").status_code == 404


def test_symlink_media_is_not_served_or_deleted(library_context, tmp_path):
    client, ids = library_context
    token = _login(client, "admin")
    secret = tmp_path / "private.png"
    secret.write_bytes(picture_bytes())
    path = ids["stored"]["target_path"]
    path.unlink()
    path.symlink_to(secret)
    assert client.get(f"/media/items/{ids['outbound_ref']}/file").status_code == 404
    client.post("/media/delete", data={"refs": ids["outbound_ref"], "csrf_token": token})
    assert path.is_symlink() and secret.exists()


def test_silk_original_download_and_preview_failure_are_distinct(library_context):
    client, ids = library_context
    _login(client, "admin")
    path = Path(ids["attachment_path"])
    media_storage.write(path, b"\x02#!SILK_V3fixture", tenant_id=ids["alpha"], boundary=path.parent)
    uri = f"/media/items/a{ids['attachment_id']}/file"
    assert client.get(uri + "?preview=1").status_code == 422
    original = client.get(uri + "?download=1")
    assert original.status_code == 200 and original.content == b"\x02#!SILK_V3fixture"
    assert "audio/silk" in original.headers["content-type"]


def test_silk_preview_range_original_download_and_cleanup(library_context):
    client, ids = library_context
    _login(client, "readonly")
    original = Path(ids["attachment_path"])
    content = silk_bytes()
    media_storage.write(original, content, tenant_id=ids["alpha"], boundary=original.parent)
    uri = f"/media/items/a{ids['attachment_id']}/file"
    response = client.get(uri + "?preview=1")
    assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(response.content), "rb") as sound:
        assert sound.getframerate() == 24000 and sound.getnframes() == 24000
    with tenant_scope(ids["alpha"]):
        cached = media_files.preview_cache(original)
        assert cached.read_bytes() == response.content
    partial = client.get(uri + "?preview=1", headers={"Range": "bytes=0-43"})
    assert partial.status_code == 206 and partial.content == response.content[:44]
    download = client.get(uri + "?preview=1&download=1")
    assert download.status_code == 200 and download.content == content
    assert download.headers["content-type"] == "audio/silk" and original.read_bytes() == content
    token = _login(client, "admin")
    assert client.post("/media/delete", data={"refs": f"a{ids['attachment_id']}", "csrf_token": token}).status_code == 303
    assert not original.exists() and not cached.exists()
    with ids["factory"]() as db:
        assert db.get(Message, ids["message_id"]) is not None


def test_cached_silk_preview_still_requires_tenant_ownership(library_context):
    client, ids = library_context
    _login(client, "readonly")
    path = Path(ids["attachment_path"])
    media_storage.write(path, silk_bytes(), tenant_id=ids["alpha"], boundary=path.parent)
    uri = f"/media/items/a{ids['attachment_id']}/file?preview=1"
    assert client.get(uri).status_code == 200
    with ids["factory"]() as db:
        db.get(MessageAttachment, ids["attachment_id"]).tenant_id = ids["beta"]
        db.commit()
    assert client.get(uri).status_code == 404


def test_catalog_deduplicates_registered_outbound_files_and_bounds_scan(library_context, monkeypatch):
    _client, ids = library_context
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        stored = ids["stored"]
        db.add(MessageAttachment(channel_id=ids["channel_id"], conversation_id=ids["conversation_id"],
            direction="out", status="downloaded", attachment_type="image", storage_path=str(stored["target_path"])))
        db.flush()
        result = media_library.catalog(db)
        assert result["count"] == 2
        assert next(item for item in result["items"] if item.name == "generated.png").source == "ai_generated"
        monkeypatch.setattr(media_library, "SCAN_LIMIT", 1)
        result = media_library.catalog(db)
        assert result["limited"] and result["count"] <= 1


def duplicate_in_another_group(ids, *, legacy=False, content=None, filename="duplicate.png"):
    """创建不同群的独立来源；旧文件绕开新写入器，模拟尚未整理的历史副本。"""

    content = picture_bytes() if content is None else content
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        channel = db.get(Channel, ids["channel_id"])
        conversation = Conversation(bot_id=channel.bot_id, channel_id=channel.id, channel_type=channel.channel_type,
            external_id="another-room", title="另一个示例群", chat_type="group")
        db.add(conversation)
        db.flush()
        message = Message(conversation_id=conversation.id, message_type="image", content="[媒体]", direction="in", status="received")
        db.add(message)
        db.flush()
        attachment = inbound_media.create_wechat_media_attachment(db, channel=channel, raw_event_id=None,
            conversation_id=conversation.id, message_id=message.id, attachment_type="image", metadata={"file_name": filename})
        if legacy:
            path = inbound_media._build_attachment_path(channel.id, attachment.id, ".png")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            attachment.storage_path, attachment.status, attachment.file_size = str(path), "downloaded", len(content)
            attachment.mime_type = "image/png"
        else:
            inbound_media.store_uploaded_media_attachment(db, attachment=attachment, content=content, filename=filename)
            path = Path(attachment.storage_path)
        reference, conversation_id = f"a{attachment.id}", conversation.id
        db.commit()
    return reference, path, conversation_id


def test_groups_share_storage_not_source_records_or_deletion(library_context):
    client, ids = library_context
    reference, path, conversation_id = duplicate_in_another_group(ids)
    original = ids["stored"]["target_path"]
    assert original.samefile(path)
    token = _login(client, "admin")
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        catalog = media_library.catalog(db)
        assert catalog["count"] == 3 and catalog["unique_count"] == 2
        assert catalog["shared_bytes"] == len(picture_bytes())
        assert media_library.resolve(db, reference).conversation_id == conversation_id != ids["conversation_id"]
    client.post("/media/delete", data={"refs": ids["outbound_ref"], "csrf_token": token})
    assert not original.exists() and path.read_bytes() == picture_bytes()
    assert client.get(f"/media/items/{reference}/file?download=1").content == picture_bytes()
    with ids["factory"]() as db:
        assert db.get(Message, ids["message_id"]) is not None


def test_shared_voice_preview_survives_until_last_reference(library_context):
    client, ids = library_context
    original = Path(ids["attachment_path"])
    content = silk_bytes()
    with tenant_scope(ids["alpha"]):
        media_storage.write(original, content, tenant_id=ids["alpha"], boundary=original.parent)
        cached = media_files.preview_cache(original)
    reference, second, _conversation = duplicate_in_another_group(ids, content=content, filename="copy.silk")
    assert original.samefile(second)
    token = _login(client, "admin")
    assert client.get(f"/media/items/a{ids['attachment_id']}/file?preview=1").status_code == 200
    client.post("/media/delete", data={"refs": f"a{ids['attachment_id']}", "csrf_token": token})
    assert cached.is_file() and second.read_bytes() == content
    client.post("/media/delete", data={"refs": reference, "csrf_token": token})
    assert not cached.exists() and not second.exists()


def scanned_plan(client, csrf_token):
    response = client.post("/media/deduplicate/scan", data={"csrf_token": csrf_token})
    assert response.status_code == 303
    match = re.fullmatch(r"/media/deduplicate/([0-9a-f]{32})", response.headers["location"])
    assert match
    return match[1]


def test_scan_only_then_confirm_shares_legacy_files_and_rejects_replay(library_context):
    client, ids = library_context
    reference, path, _conversation = duplicate_in_another_group(ids, legacy=True)
    original = ids["stored"]["target_path"]
    snapshot = media_storage.signature(path)
    csrf = _login(client, "admin")
    token = scanned_plan(client, csrf)
    review = client.get("/media/deduplicate/" + token)
    assert review.status_code == 200 and "确认整理" in review.text and "duplicate.png" in review.text
    assert media_storage.signature(path) == snapshot and not original.samefile(path)
    response = client.post("/media/deduplicate/apply", data={"csrf_token": csrf, "token": token}, follow_redirects=True)
    assert original.samefile(path) and path.read_bytes() == picture_bytes()
    assert "已合并 1 个重复副本" in response.text and f"释放 {len(picture_bytes())} B" in response.text
    assert client.get(f"/media/items/{reference}/file").status_code == 200
    repeated = client.post("/media/deduplicate/apply", data={"csrf_token": csrf, "token": token}, follow_redirects=True)
    assert "已过期或已使用" in repeated.text


def test_scan_permissions_and_csrf(library_context):
    client, ids = library_context
    csrf = _login(client, "readonly")
    assert "data-media-scan" not in client.get("/media").text
    for action in ("scan", "apply"):
        assert client.post("/media/deduplicate/" + action, data={"csrf_token": csrf}).status_code == 403
    assert client.get("/media/deduplicate/" + "a" * 32).status_code == 403
    _login(client, "admin")
    for action in ("scan", "apply"):
        assert client.post("/media/deduplicate/" + action, data={"csrf_token": "invalid"}).status_code == 403


@pytest.mark.parametrize("changed", ["content", "protected", "missing", "foreign"])
def test_confirmation_revalidates_history_and_pending_tasks(library_context, changed):
    client, ids = library_context
    reference, path, _conversation = duplicate_in_another_group(ids, legacy=True)
    original = ids["stored"]["target_path"]
    csrf = _login(client, "admin")
    token = scanned_plan(client, csrf)
    if changed == "content":
        path.write_bytes(b"changed" + picture_bytes()[7:])
    elif changed == "missing":
        path.unlink()
    else:
        with ids["factory"]() as db:
            if changed == "protected":
                db.add(TaskJob(tenant_id=ids["alpha"], job_type="outbound_dispatch", status="pending",
                    payload_json=json.dumps({"path": str(path)})))
            else:
                db.get(MessageAttachment, int(reference[1:])).tenant_id = ids["beta"]
            db.commit()
    response = client.post("/media/deduplicate/apply", data={"csrf_token": csrf, "token": token}, follow_redirects=True)
    assert "1 个文件已变化" in response.text
    assert original.read_bytes() == picture_bytes()
    if path.exists():
        assert not original.samefile(path)


def test_plans_are_bound_to_user_tenant_and_expiry(library_context):
    _client, ids = library_context
    duplicate_in_another_group(ids, legacy=True)
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        plan = media_dedup.scan(db, user_id=1)
        with pytest.raises(ValueError):
            media_dedup.review(2, plan["token"])
        with tenant_scope(ids["beta"]), pytest.raises(ValueError):
            media_dedup.review(1, plan["token"])
        plan["expires_at"] = 0
        media_dedup._save(1, plan)
        with pytest.raises(ValueError):
            media_dedup.apply(db, user_id=1, token=plan["token"])
        plan = media_dedup.scan(db, user_id=1)
        plan["groups"][0]["items"][0]["signature"] = ["invalid"]
        media_dedup._save(1, plan)
        with pytest.raises(ValueError):
            media_dedup.review(1, plan["token"])


@pytest.mark.parametrize("limit,value", [("MAX_FILES", 1), ("MAX_BYTES", 1), ("MAX_SECONDS", 0)])
def test_scan_bounds_report_partial_without_modifying_originals(library_context, monkeypatch, limit, value):
    _client, ids = library_context
    _reference, path, _conversation = duplicate_in_another_group(ids, legacy=True)
    snapshot = media_storage.signature(path)
    monkeypatch.setattr(media_dedup, limit, value)
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        plan = media_dedup.scan(db, user_id=1)
        assert plan["limited"] and not plan["groups"]
    assert media_storage.signature(path) == snapshot


def test_expired_scan_budget_stops_catalog_enumeration(library_context, monkeypatch):
    """扫描时间预算应覆盖目录和数据库枚举，不能只约束后续哈希。"""

    _client, ids = library_context
    def unexpected_attachment(*args):
        pytest.fail("expired scan must not inspect attachment files")

    monkeypatch.setattr(media_library, "_attachment", unexpected_attachment)
    monkeypatch.setattr(media_dedup, "MAX_SECONDS", 0)
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        plan = media_dedup.scan(db, user_id=1)
    assert plan["limited"] and plan["scanned"] == 0 and not plan["groups"]


def test_video_cover_authorization_sharing_and_cleanup(library_context, tmp_path):
    from tests.media_video_fixtures import video_bytes
    from app.services import video_preview

    client, ids = library_context
    content = video_bytes(tmp_path)
    with tenant_scope(ids["alpha"]):
        stored = outbound_files.store_outbound_upload(tenant_id=ids["alpha"], channel_id=ids["channel_id"],
            file_name="clip.mp4", content=content, content_type="video/mp4", message_type="video")
        stored["metadata"]["created_at"] = (now_shanghai() - timedelta(days=1)).isoformat()
        (stored["target_path"].parent / "meta.json").write_text(json.dumps(stored["metadata"]))
        digest = media_storage.file_digest(stored["target_path"])[0]
        cache = video_preview.cache_path(digest)
    ref = f"o{ids['channel_id']}-{stored['share_id']}"
    url = f"/media/items/{ref}/cover"
    assert client.get(url).status_code == 303
    _login(client, "readonly")
    response = client.get(url)
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get(f"/media/items/o999-{stored['share_id']}/cover").status_code == 404
    assert client.get(f"/media/items/{ids['outbound_ref']}/cover").status_code == 404
    original = client.get(f"/media/items/{ref}/file?download=1")
    assert original.content == content
    reference, second, _ = duplicate_in_another_group(ids, content=content, filename="copy.mp4")
    # 上传时按文件头识别为视频；切换租户归属后，即使缓存存在也不能访问。
    with ids["factory"]() as db:
        row = db.get(MessageAttachment, int(reference[1:]))
        row.tenant_id = ids["beta"]
        db.commit()
    assert client.get(f"/media/items/{reference}/cover").status_code == 404
    with ids["factory"]() as db:
        db.get(MessageAttachment, int(reference[1:])).tenant_id = ids["alpha"]
        db.commit()
    assert client.get(f"/media/items/{reference}/cover").content == response.content
    csrf = _login(client, "admin")
    client.post("/media/delete", data={"refs": ref, "csrf_token": csrf})
    assert cache.is_file() and second.read_bytes() == content
    client.post("/media/delete", data={"refs": reference, "csrf_token": csrf})
    assert not cache.exists()
    assert client.get(url).status_code == 404
