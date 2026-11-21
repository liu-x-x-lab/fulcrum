"""真实临时文件系统上的内容去重，不读取业务目录或连接外部服务。"""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

import pytest

from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.services import inbound_files, media_storage, moments_media, outbound_files


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path


def put(root, name, content=b"same-media", tenant_id=1):
    path = root / "references" / name
    media_storage.write(path, content, tenant_id=tenant_id, boundary=root)
    return path


def test_identical_content_shares_bytes_not_names_or_tenants(storage_root):
    first = put(storage_root, "one.png")
    second = put(storage_root, "other.mp4")
    changed = put(storage_root, "three.png", b"same-size!")
    foreign = put(storage_root, "foreign.png", tenant_id=2)
    assert first.samefile(second)
    assert not first.samefile(changed) and not first.samefile(foreign)
    assert second.read_bytes() == first.read_bytes() == b"same-media"


def test_replace_and_last_reference_cleanup(storage_root):
    first, second = put(storage_root, "one"), put(storage_root, "two")
    digest, _signature = media_storage.file_digest(first)
    blob = media_storage.blob_path(1, digest)
    put(storage_root, "one", b"changed")
    assert second.read_bytes() == b"same-media" and first.read_bytes() == b"changed"
    assert blob.exists()
    _digest, remaining = media_storage.release(second, tenant_id=1, boundary=storage_root)
    assert not remaining and not blob.exists() and first.is_file()


def test_release_only_one_shared_reference(storage_root):
    first, second = put(storage_root, "one"), put(storage_root, "two")
    digest, remaining = media_storage.release(first, tenant_id=1, boundary=storage_root)
    assert remaining and second.read_bytes() == b"same-media"
    assert media_storage.blob_path(1, digest).is_file()


def test_concurrent_publish_is_one_physical_file(storage_root):
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda number: put(storage_root, str(number)), range(16)))
    assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 1
    assert all(path.read_bytes() == b"same-media" for path in paths)


def test_historical_adoption_checks_snapshot_and_preserves_paths(storage_root):
    first = put(storage_root, "one")
    second = storage_root / "legacy"
    second.write_bytes(first.read_bytes())
    digest, signature = media_storage.file_digest(second)
    result = media_storage.adopt(second, tenant_id=1, boundary=storage_root, expected=signature, expected_digest=digest)
    assert result["saved_bytes"] == len(b"same-media") and first.samefile(second)
    assert media_storage.adopt(second, tenant_id=1, boundary=storage_root)["saved_bytes"] == 0
    replacement = storage_root / "replacement"
    replacement.write_bytes(b"new-content")
    os.replace(replacement, second)
    with pytest.raises(ValueError):
        media_storage.adopt(second, tenant_id=1, boundary=storage_root, expected=signature, expected_digest=digest)
    assert second.read_bytes() == b"new-content" and first.read_bytes() == b"same-media"


def test_failed_publish_preserves_previous_content(storage_root, monkeypatch):
    first, second = put(storage_root, "one"), put(storage_root, "two")
    original_replace = os.replace

    def fail_target(source, target):
        if Path(target) == first:
            raise OSError("fixture replace failure")
        return original_replace(source, target)

    monkeypatch.setattr(media_storage.os, "replace", fail_target)
    with pytest.raises(OSError):
        put(storage_root, "one", b"new-content")
    assert first.samefile(second) and first.read_bytes() == b"same-media"
    assert not list(first.parent.glob(".media-*"))


def test_corrupt_anchor_and_symlink_are_rejected(storage_root):
    original = put(storage_root, "one")
    digest, _signature = media_storage.file_digest(original)
    media_storage.blob_path(1, digest).write_bytes(b"corrupted!")
    with pytest.raises(ValueError):
        put(storage_root, "two")
    outside = storage_root / "outside"
    outside.write_bytes(b"outside")
    linked = storage_root / "linked"
    linked.symlink_to(outside)
    with pytest.raises(ValueError):
        media_storage.write(linked, b"overwrite", tenant_id=1, boundary=storage_root)
    with tenant_scope(2), pytest.raises(ValueError):
        put(storage_root, "mismatched")
    assert outside.read_bytes() == b"outside"


def test_upload_writers_share_content_and_keep_metadata_separate(storage_root, monkeypatch):
    monkeypatch.setattr(inbound_files, "INBOUND_FILE_ROOT", storage_root / "tenants")
    monkeypatch.setattr(outbound_files, "OUTBOUND_FILE_ROOT", storage_root / "tenants")
    values = dict(tenant_id=1, file_name="meta.json", content=b'{"original":true}',
        content_type="application/json", message_type="file")
    first = inbound_files.store_inbound_upload(channel_id=1, **values)
    second = outbound_files.store_outbound_upload(channel_id=2, **values)
    assert first["target_path"].samefile(second["target_path"])
    assert first["target_path"].read_bytes() == values["content"]
    assert (first["target_path"].parent / ".meta.json").is_file()
    outbound_files.record_media_delivery(second, job_id=2)
    assert second["target_path"].read_bytes() == values["content"]


def test_completed_moments_upload_adopts_existing_content(storage_root):
    from types import SimpleNamespace

    original = put(storage_root, "image.jpg")
    path = moments_media.storage_dir(2) / "10.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(original.read_bytes())
    row = SimpleNamespace(tenant_id=1, channel_id=2)
    moments_media.finish_upload(row, path, path.stat().st_size, "image/jpeg")
    assert path.samefile(original) and row.status == "downloaded" and row.token_hash is None
