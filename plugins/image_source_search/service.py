"""来源匹配只采用网页或作品的证据，不把相似人像推断为同一人。"""

import base64
from io import BytesIO
import re
from urllib.parse import urlencode, urlsplit

from PIL import Image, ImageOps

from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpTimeoutError

MAX_BYTES = 8 * 1024 * 1024
HTTP_LIMITS = dict(max_redirects=0, total_timeout_seconds=10, connect_timeout_seconds=5,
                   max_response_bytes=512 * 1024)


def search_jpeg(data):
    """限制解压像素，按拍摄方向转正，仅发送缩小后的像素，不传 EXIF 或文件名。"""
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("图片为空或超过 8 MiB。")
    try:
        with Image.open(BytesIO(data), formats=("JPEG", "PNG", "WEBP", "GIF")) as original:
            if original.width * original.height > 16_000_000:
                raise ValueError("图片超过 1600 万像素。")
            original.thumbnail((1600, 1600))
            pixels = ImageOps.exif_transpose(original).convert("RGB")
            pixels.info.clear()
            output = BytesIO()
            pixels.save(output, "JPEG", quality=90)
            return output.getvalue()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("无法读取图片，请使用 JPEG、PNG、GIF 或 WebP。") from exc


def web_results(data):
    response = data["responses"][0]
    if "error" in response:
        raise ValueError("provider error")
    pages = response.get("webDetection", {}).get("pagesWithMatchingImages", [])
    seen = set()
    results = []
    for page in sorted(pages, key=lambda item: not item.get("fullMatchingImages")):
        url = page.get("url")
        if not isinstance(url, str) or len(url) > 1500 or re.search(r"[\s\\\x00-\x1f]", url):
            continue
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None or url in seen:
            continue
        seen.add(url)
        line = f"匹配网页：{url}"
        # 只从单条帖子的规范路径推导发帖账号，不声称其为照片中的人物或首发作者。
        if parts.netloc.lower() in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            match = re.fullmatch(r"/([a-zA-Z0-9_]{1,15})/status/\d+/?", parts.path)
            if match and match[1].lower() != "i":
                line += f"\n发帖账号：https://x.com/{match[1]}"
        results.append(line)
        if len(results) == 3:
            break
    return results


def anime_results(data):
    if data.get("error"):
        raise ValueError("provider error")
    for item in data["result"]:
        if not .9 <= float(item["similarity"]) <= 1:
            continue
        info = item["anilist"]
        title = info["title"].get("native") or info["title"].get("romaji")
        if not title or type(info["id"]) is not int or info["id"] <= 0:
            continue
        label = " ".join(title.split())[:100]
        episode = item.get("episode")
        if type(episode) is int:
            label += f" · 第 {episode} 集"
        return [f"动画：{label}\nhttps://anilist.co/anime/{info['id']}"]
    return []


def search(image, key, anime_enabled):
    client = PluginHttpClient()
    failures = []
    completed = False
    engines = []
    if key:
        engines.append(("网页搜索", "https://vision.googleapis.com/v1/images:annotate?" + urlencode({"key": key}),
                        {"json_body": {"requests": [{"image": {"content": base64.b64encode(image).decode("ascii")},
                                                     "features": [{"type": "WEB_DETECTION"}]}]}}, web_results))
    if anime_enabled:
        engines.append(("动画搜索", "https://api.trace.moe/search?anilistInfo",
                        {"body": image, "headers": {"Content-Type": "image/jpeg"}}, anime_results))
    for label, url, options, parse in engines:
        try:
            response = client.request("POST", url, **options, **HTTP_LIMITS)
            if response.status_code != 200:
                reason = {401: "鉴权失败", 403: "访问被拒绝", 429: "已限流"}.get(response.status_code, "服务不可用")
                failures.append(f"{label}{reason}")
                continue
            hits = parse(response.json())
            completed = True
            if hits:
                return "found", "\n\n".join([*hits, *failures])
        except PluginHttpTimeoutError:
            failures.append(f"{label}超时")
        except (PluginHttpError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            failures.append(f"{label}暂不可用")
    notes = ["没有匹配到可核对的出处。"] if completed else []
    if not key:
        notes.append("未配置网页搜索密钥，未查询真人 Cos 或插画网页。")
    return ("no_match" if completed else "provider_error"), "\n".join(notes + failures)
