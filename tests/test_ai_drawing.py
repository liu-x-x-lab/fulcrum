"""原生绘图的显式意图、独立凭据和有界供应商契约。"""

import base64
import io
import json
from http.client import HTTPException
from unittest.mock import Mock

import pytest
from PIL import Image

from app.services import ai_drawing, safe_fetch
from app.services.plugin_runtime.http_client import PluginHttpError, PluginHttpResponse


def picture_bytes():
    buffer = io.BytesIO()
    Image.new("RGBA", (32, 32), (255, 0, 0, 128)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.parametrize("text,quoted,mode", [
    ("@bot 画图 一只猫", False, "generate"),
    ("请帮我画一张海报", False, "generate"),
    ("改图 把背景换成蓝色", True, "edit"),
    ("@bot 把背景换成蓝色", True, "edit"),
    ("@bot 换成水彩风格", True, "edit"),
    ("画图 按原图重新绘制", True, "edit"),
    ("改图 换背景", False, "edit"),
])
def test_explicit_requests(text, quoted, mode):
    request = ai_drawing.parse_request(text, quoted=quoted)
    assert request.mode == mode and request.prompt


@pytest.mark.parametrize("text", ["这张图是什么", "不要画图", "你能画图吗", "画图功能怎么配置", "画图 能用吗", "刚才他让我画一张图", "如何把背景换成蓝色"])
def test_discussion_does_not_start_paid_drawing(text):
    assert ai_drawing.parse_request(text, quoted=True) is None


@pytest.mark.parametrize("edit", [False, True])
def test_single_image_api_and_multipart_edit(monkeypatch, edit):
    calls = []

    def request(_client, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return PluginHttpResponse(200, {"content-type": "application/json"}, json.dumps({"data": [{"b64_json": base64.b64encode(picture_bytes()).decode()}]}).encode(), url)

    monkeypatch.setattr(ai_drawing.PluginHttpClient, "request", request)
    config = ai_drawing.ImageConfig("https://images.example/v1", "image-model", "image-only")
    result = ai_drawing.generate(config, "把背景换成蓝色", reference=picture_bytes() if edit else None)
    assert result.startswith(b"\x89PNG")
    method, url, kwargs = calls[0]
    assert method == "POST" and url.endswith("/images/edits" if edit else "/images/generations")
    assert kwargs["headers"]["Authorization"] == "Bearer image-only"
    assert kwargs["max_redirects"] == 0 and kwargs.get("retry_count", 0) == 0
    if edit:
        assert b'name="image"; filename="reference.png"' in kwargs["body"]
        assert picture_bytes() in kwargs["body"] and b"response_format" not in kwargs["body"]
    else:
        assert kwargs["json_body"]["n"] == 1
        assert kwargs["json_body"]["quality"] == "medium"
        assert "response_format" not in kwargs["json_body"]


@pytest.mark.parametrize("body", [{"data": [{"url": "http://127.0.0.1/private"}]}, {"data": [{"b64_json": "!!!!"}]}, {"data": [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nbroken").decode()}]}])
def test_invalid_images_and_urls_are_not_fetched(monkeypatch, body):
    monkeypatch.setattr(ai_drawing.PluginHttpClient, "request", lambda *args, **kwargs: PluginHttpResponse(200, {}, json.dumps(body).encode(), "https://images.example"))
    with pytest.raises(ai_drawing.DrawingError):
        ai_drawing.generate(ai_drawing.ImageConfig("https://images.example/v1", "image-model", "key"), "a cat")


def test_failure_is_safe_and_never_retried(monkeypatch):
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        raise PluginHttpError("secret upstream payload")

    monkeypatch.setattr(ai_drawing.PluginHttpClient, "request", failed)
    with pytest.raises(ai_drawing.DrawingError, match="未自动重试") as error:
        ai_drawing.generate(ai_drawing.ImageConfig("https://images.example/v1", "image-model", "key"), "a cat")
    assert len(calls) == 1 and "secret" not in str(error.value)


@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("extra", [{}, {"b64_json": None}, {"b64_json": ""}])
def test_url_response_uses_public_reader_without_provider_credentials(monkeypatch, edit, extra):
    """生图和改图共用响应路径，URL 下载不得继承供应商认证或再次生成。"""

    image_url = "https://cdn.example.test/generated.png?signature=fictional"
    request = Mock(return_value=PluginHttpResponse(200, {}, json.dumps({"data": [{"url": image_url, **extra}]}).encode(), "https://images.example/v1"))
    download = Mock(return_value=picture_bytes())
    monkeypatch.setattr(ai_drawing.PluginHttpClient, "request", request)
    monkeypatch.setattr(safe_fetch, "read_public_file", download)
    result = ai_drawing.generate(ai_drawing.ImageConfig("https://images.example/v1", "image-model", "private-key"),
                                 "a circle", reference=picture_bytes() if edit else None)
    assert result.startswith(b"\x89PNG")
    request.assert_called_once()
    assert request.call_args.args == ("POST", "https://images.example/v1/images/" + ("edits" if edit else "generations"))
    download.assert_called_once_with(image_url, ai_drawing.MAX_IMAGE_BYTES)


@pytest.mark.parametrize("encoded", ["valid", "!!!!", 42])
def test_base64_response_never_falls_back_to_image_url(monkeypatch, encoded):
    image = base64.b64encode(picture_bytes()).decode() if encoded == "valid" else encoded
    request = Mock(return_value=PluginHttpResponse(200, {}, json.dumps({"data": [{"b64_json": image, "url": "https://cdn.example.test/unneeded.png"}]}).encode(), "https://images.example"))
    download = Mock(side_effect=AssertionError("Base64 must not cause a download"))
    monkeypatch.setattr(ai_drawing.PluginHttpClient, "request", request)
    monkeypatch.setattr(safe_fetch, "read_public_file", download)
    config = ai_drawing.ImageConfig("https://images.example/v1", "image-model", "key")
    if encoded == "valid":
        assert ai_drawing.generate(config, "a circle").startswith(b"\x89PNG")
    else:
        with pytest.raises(ai_drawing.DrawingError):
            ai_drawing.generate(config, "a circle")
    request.assert_called_once()
    download.assert_not_called()


@pytest.mark.parametrize("error_type", [ValueError, safe_fetch.UnsafeUrlError, TimeoutError, HTTPException])
def test_image_download_failure_is_safe_and_does_not_regenerate(monkeypatch, error_type):
    request = Mock(return_value=PluginHttpResponse(200, {}, b'{"data":[{"url":"https://cdn.example.test/image.png?signature=private"}]}', "https://images.example"))
    download = Mock(side_effect=error_type("secret URL or upstream content"))
    monkeypatch.setattr(ai_drawing.PluginHttpClient, "request", request)
    monkeypatch.setattr(safe_fetch, "read_public_file", download)
    with pytest.raises(ai_drawing.DrawingError, match="图片下载失败.*未自动重试") as failure:
        ai_drawing.generate(ai_drawing.ImageConfig("https://images.example/v1", "image-model", "key"), "a circle")
    request.assert_called_once()
    download.assert_called_once()
    assert "secret" not in str(failure.value) and "signature" not in str(failure.value)


@pytest.mark.parametrize("content", [b"<html>not an image</html>", b"\x89PNG\r\n\x1a\nbroken", b"x" * (ai_drawing.MAX_IMAGE_BYTES + 1)], ids=["html", "corrupt", "oversized"])
def test_downloaded_image_is_still_validated(monkeypatch, content):
    request = Mock(return_value=PluginHttpResponse(200, {}, b'{"data":[{"url":"https://cdn.example.test/image.png"}]}', "https://images.example"))
    download = Mock(return_value=content)
    monkeypatch.setattr(ai_drawing.PluginHttpClient, "request", request)
    monkeypatch.setattr(safe_fetch, "read_public_file", download)
    with pytest.raises(ai_drawing.DrawingError):
        ai_drawing.generate(ai_drawing.ImageConfig("https://images.example/v1", "image-model", "key"), "a circle")
    request.assert_called_once()
    download.assert_called_once()


def test_http_client_preserves_nonstreaming_image_timeout():
    captured = []

    class Opener:
        def open(self, request, *, timeout):
            captured.append(timeout)
            raise TimeoutError

    client = ai_drawing.PluginHttpClient(opener_factory=lambda redirects: Opener(), clock=lambda: 0)
    with pytest.raises(PluginHttpError):
        client.request("POST", "https://images.example/v1/images/generations",
            connect_timeout_seconds=180, total_timeout_seconds=180, retry_count=2)
    assert captured == [180]
