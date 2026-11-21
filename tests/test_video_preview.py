"""封面实际抽帧、共享缓存、隔离和失败原件保护。"""

from pathlib import Path
import subprocess

import pytest
from PIL import Image

from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.services import media_storage, video_preview
from tests.media_video_fixtures import video_bytes


@pytest.fixture
def video_context(tmp_path, monkeypatch):
    root = tmp_path / "media"
    monkeypatch.setattr(settings, "data_dir", str(root))
    with tenant_scope(1):
        content = video_bytes(tmp_path)
        path = root / "tenants/1/outbound_files/video.mp4"
        media_storage.write(path, content, tenant_id=1, boundary=root)
        yield root, path, content


def test_real_cover_reuses_content_cache_and_keeps_original(video_context, monkeypatch):
    root, path, content = video_context
    before = media_storage.signature(path)
    cover = video_preview.thumbnail(path)
    with Image.open(cover) as picture:
        assert picture.format == "JPEG" and max(picture.size) <= 640
        assert picture.width / picture.height == pytest.approx(1.5, abs=0.02)
    assert media_storage.signature(path) == before and path.read_bytes() == content
    other = path.with_name("another.mp4")
    media_storage.write(other, content, tenant_id=1, boundary=root)
    with tenant_scope(2):
        foreign = root / "tenants/2/outbound_files/video.mp4"
        media_storage.write(foreign, content, tenant_id=2, boundary=root)
        foreign_cover = video_preview.thumbnail(foreign)
        assert foreign_cover != cover and not foreign_cover.samefile(cover)
    monkeypatch.setattr(video_preview.subprocess, "run", lambda *args, **kwargs: pytest.fail("cached cover decoded again"))
    assert video_preview.thumbnail(other) == cover


def test_replaced_source_gets_new_cover(video_context):
    root, path, content = video_context
    previous = video_preview.thumbnail(path)
    # 尾部填充仍为有效 MP4，但内容已改变，必须换缓存键。
    media_storage.write(path, content + b"\0" * 8, tenant_id=1, boundary=root)
    assert video_preview.thumbnail(path) != previous


@pytest.mark.parametrize("failure", ["format", "size", "timeout", "changed", "missing", "output", "symlink", "busy"])
def test_failures_never_publish_or_modify_original(video_context, monkeypatch, failure):
    root, path, content = video_context
    if failure == "format":
        media_storage.write(path, b"#EXTM3U\nfile:///etc/passwd", tenant_id=1, boundary=root)
    elif failure == "size":
        monkeypatch.setattr(video_preview, "MAX_INPUT_BYTES", 1)
    elif failure == "missing":
        monkeypatch.setattr(video_preview.shutil, "which", lambda name: None)
    elif failure == "symlink":
        linked = path.with_name("symlink.mp4")
        linked.symlink_to(path)
        path = linked
    elif failure == "busy":
        from threading import BoundedSemaphore
        slots = BoundedSemaphore(1)
        slots.acquire()
        monkeypatch.setattr(video_preview, "_slots", slots)
    elif failure in {"timeout", "changed", "output"}:
        def run(command, **kwargs):
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 12)
            if failure == "output":
                Path(command[-1]).write_bytes(b"invalid image")
            else:
                Image.new("RGB", (64, 48), "red").save(command[-1], format="JPEG")
                media_storage.write(path, content + b"changed", tenant_id=1, boundary=root)
        monkeypatch.setattr(video_preview.subprocess, "run", run)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        video_preview.thumbnail(path)
    assert path.read_bytes() == (content + b"changed" if failure == "changed" else original)
    assert not list(root.glob("tenants/*/media_previews/*"))


def test_cover_preserves_display_ratio_for_non_square_video_pixels(video_context, tmp_path):
    root, path, _content = video_context
    media_storage.write(path, video_bytes(tmp_path, sar=2), tenant_id=1, boundary=root)
    with Image.open(video_preview.thumbnail(path)) as picture:
        assert picture.width / picture.height == pytest.approx(3, abs=0.03)
