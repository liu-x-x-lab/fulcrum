"""已授权本地视频的有界封面缓存；不联网、不改原件、不暴露内容指纹入口。"""

import os
import sys


def _decoder_child():
    """在独立子进程内设置资源限制，再替换为固定的本地 FFmpeg。"""

    if sys.platform == "linux":
        import resource

        for kind, limit in ((resource.RLIMIT_AS, 768 * 1024 * 1024), (resource.RLIMIT_CPU, 10),
                            (resource.RLIMIT_FSIZE, 2 * 1024 * 1024), (resource.RLIMIT_CORE, 0)):
            resource.setrlimit(kind, (limit, limit))
    os.execv(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    _decoder_child()
    raise SystemExit(1)

import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import BoundedSemaphore
import time

from PIL import Image

from app.core.settings import settings
from app.core.tenant_context import require_tenant_id
from app.services import media_files, media_storage

MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
PROCESS_TIMEOUT = 12
_slots = BoundedSemaphore(2)


def cache_path(digest: str) -> Path:
    key = hashlib.sha256(("video-cover-v1-first-640:" + digest).encode()).hexdigest()
    return Path(settings.data_dir) / "tenants" / str(require_tenant_id()) / "media_previews" / (key + ".jpg")


def _input_options(path: Path) -> list[str]:
    # 固定容器解析器，避免伪装成视频的播放列表读取任意本地文件或远端资源。
    with path.open("rb") as source:
        head = source.read(16)
    if head[4:8] == b"ftyp":
        return ["-f", "mov", "-enable_drefs", "0", "-use_absolute_path", "0"]
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ["-f", "matroska"]
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return ["-f", "avi"]
    raise ValueError("此视频格式暂不能生成封面")


def thumbnail(path: Path) -> Path:
    """内容与租户共同确定缓存；沿用预览锁和原件快照，防止删除后留下新缓存。"""

    tenant_id = require_tenant_id()
    path = media_files.owned_file(path, Path(settings.data_dir))
    if not _slots.acquire(blocking=False):
        raise ValueError("封面生成繁忙，请稍后重试")
    temporary = None
    try:
        digest, snapshot = media_storage.file_digest(path, max_bytes=MAX_INPUT_BYTES, deadline=time.monotonic() + 5)
        target = cache_path(digest)
        media_storage.checked_path(target, Path(settings.data_dir))
        with media_storage.locked(tenant_id, "preview-" + digest[:2]):
            if media_storage.signature(path) != snapshot:
                raise ValueError("视频已变化，请刷新重试")
            if target.is_file():
                return media_files.owned_file(target, target.parent)
            options = _input_options(path)
            binary = shutil.which("ffmpeg")
            if not binary:
                raise ValueError("服务器缺少视频封面组件")
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".video-cover-", suffix=".jpg", dir=target.parent)
            os.close(descriptor)
            # dar 包含像素宽高比；封面转成方形像素后仍保留实际显示比例。
            command = [sys.executable, "-I", str(Path(__file__).resolve()), binary,
                "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-max_alloc", "67108864",
                "-threads", "1", "-protocol_whitelist", "file,pipe", *options, "-i", str(path),
                "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", "1", "-threads", "1", "-filter_threads", "1",
                "-vf", "scale='min(640,640*dar)':'min(640,640/dar)',setsar=1", "-q:v", "4",
                "-fs", str(MAX_OUTPUT_BYTES), "-f", "image2", "-update", "1", temporary]
            subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=True, timeout=PROCESS_TIMEOUT)
            if not 0 < Path(temporary).stat().st_size <= MAX_OUTPUT_BYTES:
                raise ValueError("封面生成失败")
            with Image.open(temporary) as picture:
                if picture.format != "JPEG" or not 0 < picture.width <= 640 or not 0 < picture.height <= 640:
                    raise ValueError("封面格式无效")
                picture.verify()
            if media_storage.signature(path) != snapshot:
                raise ValueError("视频已变化，请刷新重试")
            os.replace(temporary, target)
            return target
    except (OSError, subprocess.SubprocessError):
        raise ValueError("视频暂不能生成封面，可直接播放或下载") from None
    finally:
        try:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
        finally:
            _slots.release()
