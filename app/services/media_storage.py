"""租户内不可变媒体存储：内容索引只作硬链接锚点，原有授权路径各自保留。"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

from app.core.settings import settings
from app.core.tenant_context import get_current_tenant_id

CHUNK_BYTES = 1024 * 1024


def checked_path(path: Path, boundary: Path) -> Path:
    """读写都拒绝任何层级的符号链接，不把 resolve 后的外部目录当成可信根。"""

    path, boundary = Path(path).absolute(), Path(boundary).absolute()
    if ".." in path.parts or not path.is_relative_to(boundary):
        raise ValueError("媒体路径无效")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("媒体路径无效")
    return path


def storage_root(tenant_id: int) -> Path:
    current = get_current_tenant_id()
    if type(tenant_id) is not int or tenant_id <= 0 or current is not None and tenant_id != current:
        raise ValueError("媒体租户归属无效")
    data = Path(settings.data_dir).absolute()
    return checked_path(data / "tenants" / str(tenant_id) / "media_objects", data)


def blob_path(tenant_id: int, digest: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("媒体指纹无效")
    root = storage_root(tenant_id)
    return checked_path(root / "blobs" / digest[:2] / digest, root)


def signature(path: Path) -> tuple[int, int, int, int]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("媒体不是普通文件")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def file_digest(path: Path, *, max_bytes: int | None = None, deadline: float | None = None):
    """分块校验完整字节，并拒绝扫描期间被替换或修改的文件。"""

    path = checked_path(path, path.parent)
    before = signature(path)
    if max_bytes is not None and before[2] > max_bytes:
        raise ValueError("媒体扫描达到大小限制")
    digest, consumed = hashlib.sha256(), 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != before:
            raise ValueError("媒体已变化，请重新扫描")
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise ValueError("媒体扫描达到时间限制")
            chunk = source.read(CHUNK_BYTES)
            if not chunk:
                break
            consumed += len(chunk)
            if max_bytes is not None and consumed > max_bytes:
                raise ValueError("媒体扫描达到大小限制")
            digest.update(chunk)
    if signature(path) != before or consumed != before[2]:
        raise ValueError("媒体已变化，请重新扫描")
    return digest.hexdigest(), before


@contextmanager
def locked(tenant_id: int, name: str = "objects", *, timeout: float = 5):
    """原生文件锁在进程退出后自动释放；写入、删除和预览使用独立锁域。"""

    if not re.fullmatch(r"objects|dedup|preview-[0-9a-f]{2}", name):
        raise ValueError("媒体锁无效")
    root = storage_root(tenant_id)
    path = checked_path(root / "locks" / (name + ".lock"), root)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        if sys.platform == "win32":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
        else:
            import fcntl
        while not acquired:
            try:
                if sys.platform == "win32":
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise ValueError("媒体存储繁忙，请稍后重试") from None
                time.sleep(0.025)
        yield
    finally:
        if acquired:
            if sys.platform == "win32":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _equal(first: Path, second: Path, *, deadline=None) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise ValueError("媒体扫描达到时间限制")
            content = left.read(CHUNK_BYTES)
            if content != right.read(CHUNK_BYTES):
                return False
            if not content:
                return True


def _prune(tenant_id: int, digest: str, inode: tuple[int, int]) -> bool:
    blob = blob_path(tenant_id, digest)
    if blob.is_file():
        info = blob.stat()
        if (info.st_dev, info.st_ino) == inode and info.st_nlink == 1:
            blob.unlink()
            return True
    return False


def _publish(source: Path, target: Path, tenant_id: int, digest: str, *, deadline=None) -> dict:
    """调用方持有租户对象锁；先准备新链接，最后一步才原子替换目标路径。"""

    blob = blob_path(tenant_id, digest)
    blob.parent.mkdir(parents=True, exist_ok=True)
    created, temporary = False, None
    try:
        if blob.exists():
            if not _equal(source, blob, deadline=deadline):
                raise ValueError("媒体内容校验失败，原文件未替换")
        else:
            os.link(source, blob)
            created = True
        previous = target.stat() if target.exists() else None
        if previous and target.samefile(blob):
            return {"digest": digest, "saved_bytes": 0}
        descriptor, temporary = tempfile.mkstemp(prefix=".media-link-", dir=target.parent)
        os.close(descriptor)
        Path(temporary).unlink()
        os.link(blob, temporary)
        os.replace(temporary, target)
        saved = previous.st_size if previous and previous.st_nlink == 1 else 0
        return {"digest": digest, "saved_bytes": saved}
    except Exception:
        if created:
            blob.unlink(missing_ok=True)
        raise
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _target(path: Path, boundary: Path, tenant_id: int) -> Path:
    target = checked_path(path, boundary)
    if target.is_relative_to(storage_root(tenant_id)):
        raise ValueError("不能把内部内容索引作为媒体引用")
    if target.exists():
        signature(target)
    return target


def write(path: Path, content: bytes, *, tenant_id: int, boundary: Path) -> None:
    """新内容写到临时文件，禁止原地截断共享 inode；旧引用只在发布成功后释放。"""

    target = _target(path, boundary, tenant_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".media-write-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        digest = hashlib.sha256(content).hexdigest()
        with locked(tenant_id):
            _target(target, boundary, tenant_id)
            previous = file_digest(target) if target.exists() else None
            _publish(Path(temporary), target, tenant_id, digest)
            if previous and previous[0] != digest:
                _prune(tenant_id, previous[0], previous[1][:2])
    finally:
        Path(temporary).unlink(missing_ok=True)


def adopt(path: Path, *, tenant_id: int, boundary: Path, expected=None, expected_digest: str | None = None,
          max_bytes: int | None = None, deadline: float | None = None) -> dict:
    """复用已完整落盘的文件；历史整理必须带扫描时的身份和内容指纹。"""

    with locked(tenant_id):
        target = _target(path, boundary, tenant_id)
        digest, current = file_digest(target, max_bytes=max_bytes, deadline=deadline)
        if expected is not None and tuple(expected) != current or expected_digest is not None and digest != expected_digest:
            raise ValueError("媒体已变化，请重新扫描")
        return _publish(target, target, tenant_id, digest, deadline=deadline)


def release(path: Path, *, tenant_id: int, boundary: Path) -> tuple[str, bool]:
    """只删除所选路径；锚点没有其他硬链接时才释放，其他来源记录无需改写。"""

    with locked(tenant_id):
        target = _target(path, boundary, tenant_id)
        digest, current = file_digest(target)
        target.unlink()
        _prune(tenant_id, digest, current[:2])
        blob = blob_path(tenant_id, digest)
        remaining = blob.is_file() and blob.stat().st_nlink > 1
        return digest, remaining
