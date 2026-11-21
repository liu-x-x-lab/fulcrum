"""原生绘图的显式意图和有界 Images API；不使用文字模型猜测收费指令。"""

from __future__ import annotations

import base64
import binascii
import io
import re
import uuid
from dataclasses import dataclass, field
from http.client import HTTPException

from PIL import Image, ImageOps, UnidentifiedImageError

from app.models.entities import TenantAiSetting
from app.services import ai_providers, safe_fetch
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_PROMPT_LENGTH = 4000
TIMEOUT_SECONDS = 180


class DrawingError(ValueError):
    """不包含上游原文、路径和密钥的可公开错误。"""


@dataclass(frozen=True)
class ImageConfig:
    api_base: str
    model: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class ImageRequest:
    mode: str
    prompt: str


def snapshot_setting(setting: TenantAiSetting) -> ImageConfig | None:
    if not setting.image_enabled:
        return None
    config = ai_providers.snapshot(setting.image_provider, setting.tenant_id, setting.image_model)
    return ImageConfig(config.api_base, config.model, config.api_key) if config else None


def parse_request(text: str, *, quoted: bool = False) -> ImageRequest | None:
    """只接受明确祈使句；讨论绘图能力、否定指令和普通看图不产生费用。"""

    text = re.sub(r"^(?:@[^\s]+\s+)+", "", str(text or "").strip()).strip()
    text = re.sub(r"^(?:(?:请|麻烦)(?:你)?[，,\s]*)?(?:(?:帮我|给我|替我)[，,\s]*)?", "", text)
    command = re.match(r"^(画图|绘图|生成图片|生成图像|改图|编辑图片|修改图片)(?:[\s:：,，]+|$)(.*)$", text, re.DOTALL)
    if command:
        prompt = command.group(2).strip()
        if re.match(r"^(?:能|会|可以|支持|怎么|如何|是否|功能)", prompt):
            return None
        return ImageRequest("edit" if quoted or command.group(1) in {"改图", "编辑图片", "修改图片"} else "generate", prompt)
    if re.match(r"^(?:画一[张幅只个]|生成一[张幅]).+", text, re.DOTALL):
        return ImageRequest("edit" if quoted else "generate", text)
    if quoted and (re.match(r"^(?:换成|改成|变成|去掉|移除|添加|加上|抠图|修复|扩图|美化|裁剪).+", text, re.DOTALL)
                   or re.match(r"^(?:把|将).{1,60}(?:换成|改成|变成|改为|替换为|去掉|移除|加上).+", text, re.DOTALL)):
        return ImageRequest("edit", text)
    return None


def normalize_image(content: bytes, *, reference: bool = False) -> bytes:
    """解码核验静态位图并重新编码 PNG，移除元数据且保留透明通道。"""

    if not 0 < len(content) <= MAX_IMAGE_BYTES:
        raise DrawingError("图片为空或超过 16 MiB，请发送较小的静态图片")
    try:
        with Image.open(io.BytesIO(content)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"} or source.width * source.height > 25_000_000 or getattr(source, "is_animated", False):
                raise DrawingError("仅支持 2500 万像素以内的静态 PNG、JPEG、WebP 图片")
            source.load()
            picture = ImageOps.exif_transpose(source).convert("RGBA" if "A" in source.getbands() or "transparency" in source.info else "RGB")
            picture.thumbnail((2048, 2048) if reference else (4096, 4096))
            picture.info.clear()
            buffer = io.BytesIO()
            picture.save(buffer, format="PNG")
            if buffer.tell() > MAX_IMAGE_BYTES:
                raise DrawingError("图片处理后仍过大，请缩小后再试")
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, DrawingError):
            raise
        raise DrawingError("图片损坏或不是受支持的静态图片") from None


def generate(config: ImageConfig, prompt: str, *, reference: bytes | None = None) -> bytes:
    """固定单次收费请求；Base64 和公网 URL 输出共用图片校验，不自动重画。"""

    if not prompt.strip() or len(prompt) > MAX_PROMPT_LENGTH:
        raise DrawingError("请提供 1 到 4000 字的图片描述或修改要求")
    fields = {"model": config.model, "prompt": prompt, "n": 1, "size": "1024x1024", "quality": "medium", "output_format": "png"}
    headers = {"Authorization": f"Bearer {config.api_key}", "Accept": "application/json"}
    options = dict(headers=headers, connect_timeout_seconds=TIMEOUT_SECONDS, total_timeout_seconds=TIMEOUT_SECONDS,
                   max_response_bytes=24 * 1024 * 1024, max_redirects=0)
    path = "/images/generations"
    if reference is None:
        options["json_body"] = fields
    else:
        picture = normalize_image(reference, reference=True)
        path = "/images/edits"
        boundary = "FulcrumImage" + uuid.uuid4().hex
        body = bytearray()
        for name, value in fields.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="reference.png"\r\nContent-Type: image/png\r\n\r\n'.encode())
        body.extend(picture)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        options["body"] = bytes(body)
    try:
        response = PluginHttpClient().request("POST", config.api_base + path, **options)
        if not 200 <= response.status_code < 300:
            raise DrawingError("绘图服务返回错误，请检查模型配置或内容要求；未自动重试")
        payload = response.json()
        images = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
            raise ValueError
        encoded = images[0].get("b64_json")
        if encoded is None or encoded == "":
            image_url = images[0].get("url")
            if not isinstance(image_url, str) or not image_url.strip():
                raise ValueError
            # 图片托管地址是不可信输入，不能沿用带密钥的供应商客户端。
            # 公共下载器固定连接已审核 IP，逐跳校验重定向且限制大小；失败不重发收费请求。
            try:
                content = safe_fetch.read_public_file(image_url, MAX_IMAGE_BYTES)
            except (OSError, ValueError, HTTPException):
                raise DrawingError("绘图服务已响应，但图片下载失败或链接不安全；可能已产生费用，未自动重试") from None
        else:
            if not isinstance(encoded, str) or len(encoded) > 23 * 1024 * 1024:
                raise ValueError
            content = base64.b64decode(encoded, validate=True)
        return normalize_image(content)
    except PluginHttpError:
        raise DrawingError("绘图请求超时或连接失败，结果不确定，可能已产生费用；未自动重试") from None
    except (ValueError, TypeError, binascii.Error) as exc:
        if isinstance(exc, DrawingError):
            raise
        raise DrawingError("绘图服务未返回有效图片；未自动重试") from None
