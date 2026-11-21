"""语音无正文入站、群自动下载及本地音频格式；不连接手机或模型。"""

from datetime import timedelta
from io import BytesIO
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import wave

import pytest
from sqlalchemy import select

from app.core.time import now_shanghai
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.models.entities import MessageAttachment, TaskJob
from app.services import channel_events, inbound_media, media_files, message_media, silk_preview
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter
from app.services.group_features.store import save_state
from tests.media_audio_fixtures import silk_bytes, tone_pcm
from tests.test_ai_media import media_context  # noqa: F401


def test_voice_without_text_is_persisted(media_context):
    normalized = WechatWeBridgeAdapter().normalize_inbound_payload(media_context.channel, {
        "event_id": "voice-event", "body": {"msg_type": "voice", "chat_id": "room@chatroom",
        "sender_id": "alice", "message_id": "voice-native", "content": ""},
    })
    assert normalized["should_persist_message"] is True
    assert normalized["message"]["content"] == "[语音]"
    assert normalized["message"]["message_type"] == "voice"


@pytest.mark.parametrize("content,declared,suffix,mime", [
    (b"\x02#!SILK_V3\x00fixture", "application/octet-stream", ".silk", "audio/silk"),
    (b"#!AMR\nfixture", "application/octet-stream", ".amr", "audio/amr"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt fixture", "application/octet-stream", ".wav", "audio/wav"),
    (b"ID3fixture", "application/octet-stream", ".mp3", "audio/mpeg"),
])
def test_uploaded_voice_keeps_bytes_and_real_format(media_context, content, declared, suffix, mime):
    context = media_context
    row = inbound_media.create_wechat_media_attachment(context.db, channel=context.channel,
        raw_event_id=None, conversation_id=context.conversation.id, message_id=context.original.id,
        attachment_type="voice", metadata={}, provider="wechat_webridge")
    inbound_media.store_uploaded_media_attachment(context.db, attachment=row, content=content, content_type=declared)
    assert Path(row.storage_path).suffix == suffix
    assert row.mime_type == mime and Path(row.storage_path).read_bytes() == content
    assert row.status == "downloaded"


@pytest.mark.parametrize("enabled", [True, False])
def test_voice_auto_download_obeys_group_switch_and_reuses_task(media_context, enabled):
    context = media_context
    save_state(context.room, "media_download", enabled=enabled)
    context.original.message_type = "voice"
    context.db.flush()
    event = {"details": {"media": {}}}
    kwargs = dict(channel=context.channel, conversation=context.conversation, raw_event=SimpleNamespace(id=1),
                  normalized=event, message=context.original)
    channel_events._queue_inbound_media_fetch_if_needed(context.db, **kwargs)
    context.db.flush()
    rows = context.db.scalars(select(MessageAttachment)).all()
    assert len(rows) == int(enabled)
    if enabled:
        assert rows[0].attachment_type == "voice"
        task_count = len(context.db.scalars(select(TaskJob)).all())
        assert message_media.request(context.db, context.original)["state"] == "loading"
        channel_events._queue_inbound_media_fetch_if_needed(context.db, **kwargs)
        assert len(context.db.scalars(select(TaskJob)).all()) == task_count == 1


@pytest.mark.parametrize("details", [{"from_self": True}, {"timestamp": (now_shanghai() - timedelta(hours=1)).isoformat()}])
def test_voice_auto_download_skips_self_sent_and_old_events(media_context, details):
    context = media_context
    save_state(context.room, "media_download", enabled=True)
    context.original.message_type = "voice"
    channel_events._queue_inbound_media_fetch_if_needed(context.db, channel=context.channel,
        conversation=context.conversation, raw_event=SimpleNamespace(id=1), normalized={"details": details}, message=context.original)
    assert context.db.scalar(select(MessageAttachment)) is None


def test_amr_preview_is_bounded_cached_and_never_overwrites_original(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(media_files.shutil, "which", lambda name: "/test/ffmpeg")
    original = tmp_path / "voice.amr"
    original.write_bytes(b"#!AMR\nfixture")
    calls = []

    def convert(command, **options):
        calls.append(command)
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert command[command.index("-i") + 1] == str(original)
        assert command[command.index("-f") + 1] == "amr"
        assert command[command.index("-t") + 1] == "600"
        assert "-fs" in command and "-threads" in command
        assert options["timeout"] == 20 and options["check"]
        with wave.open(command[-1], "wb") as sound:
            sound.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            sound.writeframes(tone_pcm())

    monkeypatch.setattr(media_files.subprocess, "run", convert)
    with tenant_scope(1):
        preview, mime = media_files.audio_preview(original, "audio/amr")
        assert mime == "audio/wav" and preview == media_files.preview_cache(original)
        assert media_files.audio_preview(original, "audio/amr")[0] == preview
        assert len(calls) == 1 and original.read_bytes() == b"#!AMR\nfixture"
    with tenant_scope(2):
        assert media_files.preview_cache(original) != preview


@pytest.mark.parametrize("failure", ["timeout", "size", "symlink"])
@pytest.mark.parametrize("mime,content", [("audio/amr", b"#!AMR\nfixture"), ("audio/silk", b"\x02#!SILK_V3fixture")])
def test_encoded_audio_preview_failure_keeps_original_and_cleans_temporary_files(tmp_path, monkeypatch, failure, mime, content):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(media_files.shutil, "which", lambda name: "/test/ffmpeg")
    original = tmp_path / "voice.bin"
    original.write_bytes(content)
    calls = []

    def convert(command, **options):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(media_files.subprocess, "run", convert)
    with tenant_scope(1):
        cache = media_files.preview_cache(original)
        if failure == "size":
            with original.open("ab") as source:
                source.truncate(32 * 1024 * 1024 + 1)
        elif failure == "symlink":
            cache.parent.parent.mkdir(parents=True)
            external = tmp_path / "not-a-cache"
            external.mkdir()
            cache.parent.symlink_to(external, target_is_directory=True)
        with pytest.raises(ValueError, match="请下载原文件"):
            media_files.audio_preview(original, mime)
        assert original.exists() and not cache.exists()
        assert len(calls) == int(failure == "timeout")
        assert not list(cache.parent.glob("*.wav"))


@pytest.mark.parametrize("tencent,sample_rate,packet_ms", [(True, 24000, 20), (False, 24000, 20),
    (True, 8000, 20), (False, 24000, 40), (True, 24000, 100)])
def test_silk_preview_decodes_real_audio_and_reuses_fresh_tenant_cache(tmp_path, monkeypatch, tencent, sample_rate, packet_ms):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(media_files.shutil, "which", lambda name: None)
    original = tmp_path / "voice.silk"
    content = silk_bytes(tencent=tencent, sample_rate=sample_rate, packet_ms=packet_ms)
    original.write_bytes(content)
    run = media_files.subprocess.run
    calls = []

    def convert(command, **options):
        calls.append(command)
        assert "-I" in command and command[-2] == str(original)
        assert options["timeout"] == 20 and options["check"]
        assert options["stdin"] == subprocess.DEVNULL
        return run(command, **options)

    monkeypatch.setattr(media_files.subprocess, "run", convert)
    with tenant_scope(1):
        preview, mime = media_files.audio_preview(original, "audio/silk")
        assert mime == "audio/wav" and preview == media_files.preview_cache(original)
        with wave.open(str(preview), "rb") as sound:
            assert (sound.getnchannels(), sound.getsampwidth(), sound.getframerate()) == (1, 2, 24000)
            assert sound.getnframes() == 24000
            assert any(sound.readframes(24000))
        assert media_files.audio_preview(original, "audio/silk")[0] == preview
        assert len(calls) == 1
        modified = preview.stat().st_mtime_ns + 1
        os.utime(original, ns=(modified, modified))
        assert media_files.audio_preview(original, "audio/silk")[0] == preview
        assert len(calls) == 1 and original.read_bytes() == content
        assert list(preview.parent.iterdir()) == [preview]
    with tenant_scope(2):
        assert media_files.preview_cache(original) != preview


@pytest.mark.parametrize("content", [b"#!SILK_V3", b"\x02#!SILK_V3fixture", b"#!SILK_V3\x01",
    b"#!SILK_V3\xfe\xff", b"#!SILK_V3\xff\xfftrailing", b"#!SILK_V3\x05\x00ab"])
def test_malformed_silk_never_publishes_partial_preview(tmp_path, monkeypatch, content):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    original = tmp_path / "broken.silk"
    original.write_bytes(content)
    with tenant_scope(1):
        with pytest.raises(ValueError, match="请下载原文件"):
            media_files.audio_preview(original, "audio/silk")
        assert original.read_bytes() == content
        assert not list(media_files.preview_cache(original).parent.glob("*.wav"))


def test_ipad_voice_encoder_uses_the_builtin_streaming_codec(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    buffer = BytesIO()
    with wave.open(buffer, "wb") as sound:
        sound.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        sound.writeframes(tone_pcm())
    encoded = WechatIpadAdapter()._encode_audio_to_silk(buffer.getvalue(), audio_format="wav")
    assert encoded["duration_ms"] == 1000 and encoded["sample_rate"] == 24000
    assert encoded["bytes"].startswith(b"\x02#!SILK_V3")
    original = tmp_path / "encoded.silk"
    original.write_bytes(encoded["bytes"])
    with tenant_scope(1):
        preview, _mime = media_files.audio_preview(original, "audio/silk")
        with wave.open(str(preview), "rb") as sound:
            assert sound.getnframes() == 24000


def test_silk_duration_and_pcm_output_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    content = silk_bytes()
    packet_size = int.from_bytes(content[10:12], "little")
    original = tmp_path / "too-long.silk"
    original.write_bytes(content[:10] + content[10:12 + packet_size] * (silk_preview.MAX_SECONDS * 50 + 1))
    with tenant_scope(1):
        with pytest.raises(ValueError, match="超过 10 分钟"):
            media_files.audio_preview(original, "audio/silk")
        assert original.exists() and not list(media_files.preview_cache(original).parent.glob("*.wav"))
    monkeypatch.setattr(silk_preview, "MAX_PCM_BYTES", 8)
    with silk_preview._BoundedPCM() as pcm:
        pcm.write(b"\x00" * 8)
        with pytest.raises(OverflowError):
            pcm.write(b"\x00\x00")
        assert len(pcm.getvalue()) == 8


@pytest.mark.parametrize("returncode,message", [(2, "缺少 SILK 试听组件"), (-11, "音频无法试听")])
def test_silk_worker_failure_preserves_original(tmp_path, monkeypatch, returncode, message):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    original = tmp_path / "voice.silk"
    content = silk_bytes()
    original.write_bytes(content)

    def fail(command, **options):
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(returncode, command)

    monkeypatch.setattr(media_files.subprocess, "run", fail)
    with tenant_scope(1):
        with pytest.raises(ValueError, match=message):
            media_files.audio_preview(original, "audio/silk")
        assert original.read_bytes() == content
        assert not list(media_files.preview_cache(original).parent.glob("*.wav"))


def test_busy_preview_does_not_start_another_decoder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    original = tmp_path / "voice.silk"
    original.write_bytes(silk_bytes())
    assert media_files._preview_slots.acquire(blocking=False)
    assert media_files._preview_slots.acquire(blocking=False)
    try:
        with tenant_scope(1), pytest.raises(ValueError, match="转换繁忙"):
            media_files.audio_preview(original, "audio/silk")
    finally:
        media_files._preview_slots.release()
        media_files._preview_slots.release()
    with tenant_scope(1):
        assert media_files.audio_preview(original, "audio/silk")[0].is_file()


def test_source_deleted_during_decode_does_not_recreate_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    original = tmp_path / "voice.silk"
    original.write_bytes(silk_bytes())
    run = media_files.subprocess.run

    def convert(command, **options):
        result = run(command, **options)
        original.unlink()
        return result

    monkeypatch.setattr(media_files.subprocess, "run", convert)
    with tenant_scope(1):
        cache = media_files.preview_cache(original)
        with pytest.raises(ValueError, match="请下载原文件"):
            media_files.audio_preview(original, "audio/silk")
        assert not list(cache.parent.glob("*.wav"))


def test_preview_reuses_content_across_paths_and_detects_same_size_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(media_files.shutil, "which", lambda name: "/test/ffmpeg")
    first, second = tmp_path / "first.amr", tmp_path / "second.amr"
    first.write_bytes(b"#!AMR\nfirst")
    second.write_bytes(first.read_bytes())
    calls = []

    def convert(command, **options):
        calls.append(command)
        with wave.open(command[-1], "wb") as sound:
            sound.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            sound.writeframes(tone_pcm())

    monkeypatch.setattr(media_files.subprocess, "run", convert)
    with tenant_scope(1):
        preview = media_files.audio_preview(first, "audio/amr")[0]
        assert media_files.audio_preview(second, "audio/amr")[0] == preview
        assert len(calls) == 1
        modified = second.stat().st_mtime_ns
        second.write_bytes(b"#!AMR\nother")
        os.utime(second, ns=(modified, modified))
        assert media_files.audio_preview(second, "audio/amr")[0] != preview
        assert len(calls) == 2
