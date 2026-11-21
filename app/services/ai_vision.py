"""独立视觉模型配置和有界媒体预处理；不读取渠道凭据、不保存识别结论。"""

from __future__ import annotations

import io
import json
import math
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.models.entities import TenantAiSetting
from app.services.plugin_runtime.text_generation import GenerationImage, PluginTextGenerationClient, PluginTextGenerationError
from app.services import ai_providers

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_VIDEO_BYTES = 40 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_VIDEO_SECONDS = 120
MAX_VIDEO_FRAMES = 8
VISION_TIMEOUT_SECONDS = 60
VIDEO_DISCLOSURE = "以下仅根据视频抽样画面分析，未分析声音，可能遗漏帧间细节。"


class VisionError(ValueError):
    """可直接告知用户的有限错误，不包含文件路径、媒体原文或供应商响应。"""


@dataclass(frozen=True)
class VisionConfig:
    api_base: str
    model: str
    api_key: str = field(repr=False)


def snapshot_setting(setting: TenantAiSetting) -> VisionConfig | None:
    """调用前复制纯数据，不让 ORM 对象或数据库连接进入慢调用。"""

    if not setting.vision_enabled:
        return None
    config = ai_providers.snapshot(setting.vision_provider, setting.tenant_id, setting.vision_model)
    return VisionConfig(config.api_base, config.model, config.api_key) if config else None


def normalize_image(data: bytes, *, caption: str = "") -> GenerationImage:
    """核对真实图片格式及像素，再压缩并移除 EXIF，模型只接收普通 JPEG。"""

    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise VisionError("图片为空或超过 16 MiB，请发送较小的图片")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP", "GIF"} or source.width * source.height > MAX_PIXELS:
                raise VisionError("图片格式不支持或像素过大")
            if getattr(source, "is_animated", False):
                raise VisionError("暂不分析动态图，请发送静态图片或 MP4 视频")
            picture = ImageOps.exif_transpose(source).convert("RGB")
            picture.thumbnail((1536, 1536))
            output = io.BytesIO()
            picture.save(output, format="JPEG", quality=85)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, VisionError):
            raise
        raise VisionError("图片损坏或无法解码，请重新发送") from None
    if output.tell() > 2 * 1024 * 1024:
        raise VisionError("图片压缩后仍过大，请发送较小的图片")
    return GenerationImage(output.getvalue(), "image/jpeg", caption)


def _run_media_tool(command: list[str], *, timeout: int) -> bytes:
    """只执行固定媒体工具参数；工具诊断不回传到群里或决策日志。"""

    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=timeout)
    except FileNotFoundError:
        raise VisionError("服务器缺少 FFmpeg/FFprobe，暂时无法分析视频") from None
    except (OSError, subprocess.TimeoutExpired):
        raise VisionError("视频处理超时或失败，请发送较短的视频") from None
    if result.returncode:
        raise VisionError("视频损坏或格式无法解析，请发送 MP4 视频")
    return result.stdout


def sample_video(data: bytes) -> tuple[tuple[GenerationImage, ...], str]:
    """限定 MP4、大小、时长、分辨率和帧数；禁用网络协议和 MOV 外部数据引用。"""

    if not 0 < len(data) <= MAX_VIDEO_BYTES:
        raise VisionError("视频为空或超过 40 MiB，请裁剪后重新发送")
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise VisionError("目前视频抽帧只支持 MP4，请转换后重新发送")
    with tempfile.TemporaryDirectory(prefix="fulcrum-vision-") as directory:
        source = Path(directory) / "video.mp4"
        source.write_bytes(data)
        input_options = ["-protocol_whitelist", "file,pipe", "-f", "mov", "-enable_drefs", "0", "-use_absolute_path", "0"]
        probe = _run_media_tool([
            "ffprobe", "-v", "error", "-max_alloc", "67108864", *input_options,
            "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(source),
        ], timeout=10)
        try:
            metadata = json.loads(probe)
            stream = metadata["streams"][0]
            duration = float(metadata["format"]["duration"])
            pixels = int(stream["width"]) * int(stream["height"])
        except (ValueError, TypeError, KeyError, IndexError):
            raise VisionError("无法确定视频时长或画面尺寸") from None
        if not math.isfinite(duration) or not 0 < duration <= MAX_VIDEO_SECONDS or not 0 < pixels <= MAX_PIXELS:
            raise VisionError("视频超过 120 秒或画面尺寸过大，请裁剪后重新发送")
        count = min(MAX_VIDEO_FRAMES, max(1, math.ceil(duration)))
        images = []
        for frame_index in range(count):
            timestamp = duration * (frame_index + 0.5) / count
            frame = _run_media_tool([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-max_alloc", "67108864",
                "-threads", "1", *input_options, "-ss", f"{timestamp:.3f}", "-i", str(source),
                "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", "1", "-threads", "1", "-filter_threads", "1",
                "-vf", "scale=1024:1024:force_original_aspect_ratio=decrease", "-q:v", "3",
                "-fs", "2097152", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ], timeout=5)
            images.append(normalize_image(frame, caption=f"视频时间 {timestamp:.2f} 秒"))
    return tuple(images), f"视频总长 {duration:.2f} 秒，均匀抽取 {count} 帧。{VIDEO_DISCLOSURE}"


def test_connection(config: VisionConfig | None) -> tuple[bool, str]:
    """用本地生成的合成图片测试实际图片请求，不把纯文本连通误报为视觉可用。"""

    if config is None:
        return False, "请先启用并完整配置独立视觉模型"
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(buffer, format="PNG")
    try:
        result = PluginTextGenerationClient(api_base=config.api_base, model=config.model, api_key=config.api_key).generate(
            user_prompt="请只说出图片的主要颜色。", images=(GenerationImage(buffer.getvalue(), "image/png"),),
            timeout_seconds=VISION_TIMEOUT_SECONDS, max_tokens=512,
        )
    except PluginTextGenerationError:
        return False, "图片请求失败，请核对接口、密钥以及模型是否支持图片输入"
    return True, f"图片请求成功，模型回复：{result.text[:40]}"
