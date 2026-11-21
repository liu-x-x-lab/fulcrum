"""媒体库文件边界与有界本地试听；不读取远端 URL，不运行用户提供的命令。"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import BoundedSemaphore
import wave

from app.core.settings import settings
from app.core.tenant_context import require_tenant_id
from app.services import media_storage, silk_preview
from app.services.media_formats import identify

_preview_slots = BoundedSemaphore(2)


def owned_file(value: str | Path, root: Path) -> Path:
    """路径必须处于指定账号目录，且任何一层都不得经过符号链接。"""

    path, boundary = Path(value).absolute(), root.absolute()
    try:
        path.relative_to(boundary)
        data_root = Path(settings.data_dir).absolute()
        relative = path.relative_to(data_root)
        current = data_root
        if current.is_symlink():
            raise ValueError
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError
        if not path.is_file() or not path.resolve().is_relative_to(boundary.resolve()):
            raise ValueError
    except (OSError, ValueError):
        raise ValueError("媒体不存在或不属于此账号") from None
    return path


def read_metadata(path: Path, root: Path) -> dict:
    try:
        path = owned_file(path, root)
        with path.open("rb") as source:
            content = source.read(65537)
        if len(content) > 65536:
            return {}
        value = json.loads(content)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def content_type(path: Path, declared: str = "") -> str:
    with path.open("rb") as source:
        return identify(source.read(64), declared, path.name)[0]


def preview_cache(path: Path, *, digest: str | None = None) -> Path:
    digest = digest or media_storage.file_digest(path)[0]
    key = hashlib.sha256(("wav-v2-mono24000:" + digest).encode()).hexdigest()
    return Path(settings.data_dir) / "tenants" / str(require_tenant_id()) / "media_previews" / (key + ".wav")


def audio_preview(path: Path, mime: str) -> tuple[Path, str]:
    """原件不改写；只用固定本地解码器，限制并发、时长、大小和子进程时间。"""

    if mime in {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/ogg", "audio/flac", "audio/aac"}:
        return path, mime
    if mime not in {"audio/amr", "audio/amr-wb", "audio/silk"}:
        raise ValueError("此语音编码暂不支持在线试听，请下载原文件")
    if path.stat().st_size > silk_preview.MAX_INPUT_BYTES:
        raise ValueError("音频过大，请下载原文件")
    digest, source_signature = media_storage.file_digest(path, max_bytes=silk_preview.MAX_INPUT_BYTES)
    target = preview_cache(path, digest=digest)
    tenant_id = require_tenant_id()
    root = Path(settings.data_dir) / "tenants" / str(tenant_id)
    for directory in (Path(settings.data_dir), root.parent, root, target.parent):
        if directory.is_symlink():
            raise ValueError("试听缓存目录无效，请下载原文件")
    with media_storage.locked(tenant_id, "preview-" + digest[:2]):
        if media_storage.signature(path) != source_signature:
            raise ValueError("音频已变化，请刷新后重试")
        if target.is_file():
            return owned_file(target, root), "audio/wav"
        return _convert_audio(path, mime, target, source_signature)


def _convert_audio(path: Path, mime: str, target: Path, source_signature: tuple) -> tuple[Path, str]:
    """同内容预览锁覆盖检查及发布，防止多群重复解码和删除时留下孤立副本。"""

    binary = shutil.which("ffmpeg") if mime != "audio/silk" else None
    if mime != "audio/silk" and not binary:
        raise ValueError("服务器未安装音频试听组件，请下载原文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not _preview_slots.acquire(blocking=False):
        raise ValueError("音频转换繁忙，请稍后重试或下载原文件")
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(suffix=".wav", dir=target.parent)
        os.close(descriptor)
        if mime == "audio/silk":
            command = [sys.executable, "-I", str(Path(silk_preview.__file__).resolve()), str(path), temporary]
        else:
            command = [binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", "1",
                "-protocol_whitelist", "file,pipe", "-f", "amr", "-i", str(path), "-t", str(silk_preview.MAX_SECONDS), "-vn",
                "-ac", "1", "-ar", str(silk_preview.SAMPLE_RATE), "-fs", str(30 * 1024 * 1024), "-f", "wav", temporary]
        subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=silk_preview.PROCESS_TIMEOUT, check=True)
        if not 44 < Path(temporary).stat().st_size <= silk_preview.MAX_INPUT_BYTES:
            raise ValueError
        with wave.open(temporary, "rb") as sound:
            if ((sound.getnchannels(), sound.getsampwidth(), sound.getframerate()) != (1, 2, silk_preview.SAMPLE_RATE)
                    or not 0 < sound.getnframes() <= silk_preview.SAMPLE_RATE * silk_preview.MAX_SECONDS):
                raise ValueError
        # 转换期间被删除或替换的原件不能重新生成孤立缓存。
        if media_storage.signature(path) != source_signature:
            raise ValueError
        Path(temporary).replace(target)
        return target, "audio/wav"
    except subprocess.TimeoutExpired:
        raise ValueError("音频转换超时，请下载原文件") from None
    except subprocess.CalledProcessError as error:
        if mime == "audio/silk" and error.returncode == 2:
            raise ValueError("服务器缺少 SILK 试听组件，请更新依赖或下载原文件") from None
        if mime == "audio/silk" and error.returncode == 3:
            raise ValueError("语音超过 10 分钟，请下载原文件") from None
        raise ValueError("音频无法试听，请下载原文件") from None
    except (OSError, ValueError, EOFError, wave.Error, subprocess.SubprocessError):
        raise ValueError("音频无法试听，请下载原文件") from None
    finally:
        try:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
        finally:
            _preview_slots.release()
