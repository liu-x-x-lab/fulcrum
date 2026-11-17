"""验证 Markdown 排版适配器确实经过公共截图入口，而非另起浏览器。"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from app.core import markdown_to_image
from app.core.tenant_context import require_tenant_id, tenant_scope
from app.services.web_screenshots import ScreenshotError, ScreenshotResult


def test_markdown_uses_public_capture_without_blocking_event_loop(tmp_path, monkeypatch):
    """线程截图保留可信租户，中文排版和表格仍转成 HTML 并写出服务返回的图片。"""
    caller_thread = threading.get_ident()
    def capture(request):
        assert threading.get_ident() != caller_thread
        assert require_tenant_id() == 42
        assert request.url is None and "中文日报" in request.html
        assert "<table>" in request.html and 'class="sheet"' in request.html
        assert request.width == 820 and request.full_page and request.format == "png"
        return ScreenshotResult(b"public-png", "image/png", 820, 900, 1)
    monkeypatch.setattr(markdown_to_image, "capture_screenshot", capture)
    with tenant_scope(42):
        path = asyncio.run(markdown_to_image.convert_md_str_to_image(
            "# 中文日报\n\n|成员|条数|\n|---|---|\n|张三|12|", "daily.png", base_dir=tmp_path,
        ))
    assert Path(path).read_bytes() == b"public-png"
    assert Path(path).parent == tmp_path / "data" / "temp" / "md2image"


def test_markdown_capture_failure_preserves_public_error(tmp_path, monkeypatch):
    """不可用时向业务调用方报告公共错误，不产生假成功文件或切换引擎。"""
    error = ScreenshotError("screenshot_timeout", "截图超时", 504)
    def capture(request):
        raise error
    monkeypatch.setattr(markdown_to_image, "capture_screenshot", capture)
    with tenant_scope(42), pytest.raises(ScreenshotError) as caught:
        markdown_to_image.convert_md_str_to_image_sync("# 中文日报", "daily.png", base_dir=tmp_path)
    assert caught.value is error
    assert not (tmp_path / "data" / "temp" / "md2image" / "daily.png").exists()
