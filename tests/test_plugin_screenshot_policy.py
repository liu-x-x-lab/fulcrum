"""插件截图接入规范：辅助模块也应检查，同时避免把普通浏览器文案当作启动代码。"""
from __future__ import annotations

import os
import pytest

from app.services.plugin_screenshot_policy import validate_plugin_screenshot_policy


@pytest.mark.parametrize("source", [
    "import playwright.async_api as browser\n",
    "from selenium import webdriver\n",
    "from DrissionPage import ChromiumPage\n",
    "import pyppeteer\n",
    "from html2image import Html2Image\n",
    "import imgkit\n",
    "from importlib import import_module as load\nload('playwright.async_api')\n",
    "__import__('selenium.webdriver')\n",
    "import subprocess as proc\nproc.run(['/usr/bin/chromium', '--headless'])\n",
    "from subprocess import Popen as launch\nlaunch('google-chrome --headless', shell=True)\n",
    "import subprocess\ncommand = ['chromium-browser', '--headless']\nsubprocess.run(command)\n",
    "import os\nos.system('chromium --headless --screenshot')\n",
    "import asyncio\nasyncio.create_subprocess_exec('msedge.exe', '--headless')\n",
    "import shutil\nshutil.which('google-chrome')\n",
])
def test_browser_code_in_helper_is_rejected(tmp_path, source):
    """既检查直接导入，也覆盖常见别名、静态动态导入与进程执行写法。"""
    (tmp_path / "plugin.py").write_text("# 合法入口\n", encoding="utf-8")
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "render.py").write_text(source, encoding="utf-8")
    errors = validate_plugin_screenshot_policy(tmp_path, {})
    assert errors and all("helpers/render.py:" in error for error in errors)
    assert all("公共截图" in error for error in errors)


def test_public_screenshot_and_user_agent_are_allowed(tmp_path):
    """爬虫请求头里的 Chrome 文本、PIL 画图和公共截图不属于独立浏览器。"""
    (tmp_path / "plugin.py").write_text('''from app.services.web_screenshots import ScreenshotRequest, capture_screenshot
from PIL import Image
import subprocess
USER_AGENT = 'Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36'
subprocess.run(['curl', '-H', 'User-Agent: ' + USER_AGENT, 'https://example.com'])
image = capture_screenshot(ScreenshotRequest(html='<h1>中文</h1>'))
''', encoding="utf-8")
    assert validate_plugin_screenshot_policy(tmp_path, {"runtime": {"api_base": "https://example.com"}}) == ()


@pytest.mark.parametrize("manifest", [
    {"runtime": {"browser_path": "secret-browser-path"}},
    {"runtime": {"screenshot_engine": "chrome"}},
    {"runtime": {"chrome": {"headless": True}}},
    {"config": {"fields": [{"key": "kitesurf_ws_url", "default": "secret-cdp-url"}]}},
    {"plugin": {"dependencies": ["playwright>=1.58"]}},
    {"plugin": {"dependencies": ["chromium"]}},
])
def test_manifest_cannot_declare_browser_or_engine(tmp_path, manifest):
    """不把浏览器配置做成插件选项；错误信息不回显地址、默认值或其他配置内容。"""
    errors = validate_plugin_screenshot_policy(tmp_path, manifest)
    assert errors and "config.toml" in errors[0] and "公共截图" in errors[0]
    assert "secret-" not in " ".join(errors)


def test_unreadable_python_is_invalid_without_exposing_source(tmp_path):
    """无法解析的辅助代码不能静默跳过，也不能把源码中的凭证拼入错误。"""
    (tmp_path / "helper.py").write_text("secret-token = (", encoding="utf-8")
    errors = validate_plugin_screenshot_policy(tmp_path, {})
    assert len(errors) == 1 and "helper.py" in errors[0]
    assert "secret-token" not in errors[0]


def test_helper_changes_invalidate_discovery_and_prevent_loading(tmp_path, monkeypatch):
    """入口未变时也要重新审查辅助文件，且拒绝在加载入口时产生任何副作用。"""
    from app.services import plugin_directory

    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "config.toml").write_text('''[plugin]
code = "demo"
display_name = "示例"
plugin_type = "message"
[capability]
channel_types = ["simulator"]
chat_types = ["group"]
message_types = ["text"]
''', encoding="utf-8")
    (folder / "plugin.py").write_text('raise AssertionError("无效插件不能执行入口")\n', encoding="utf-8")
    helper = folder / "helper.py"
    helper.write_text("# 合法辅助模块\n", encoding="utf-8")
    monkeypatch.setattr(plugin_directory.settings, "plugins_dir", str(tmp_path))
    # 使用独立目录作为缓存键，不清空其他测试或应用已有的缓存。
    first = plugin_directory.get_discovered_plugin("demo")
    assert first.is_valid
    previous_mtime = helper.stat().st_mtime_ns
    helper.write_text("from selenium import webdriver\n", encoding="utf-8")
    os.utime(helper, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
    changed = plugin_directory.get_discovered_plugin("demo")
    assert not changed.is_valid
    assert any("helper.py" in error for error in changed.validation_errors)
    assert plugin_directory.load_directory_plugin("demo") is None


@pytest.mark.parametrize("private", [False, True], ids=["public-plugin", "private-plugin"])
def test_delivery_checks_use_the_same_helper_rule(tmp_path, monkeypatch, private):
    """交付脚本复用运行时规则，私有目录的数量豁免也不能绕过截图检查。"""
    from scripts import verify_plugin_migration as delivery

    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "config.toml").write_text('''[plugin]
code = "demo"
display_name = "示例"
plugin_type = "message"
[capability]
channel_types = ["simulator"]
chat_types = ["group"]
message_types = ["text"]
''', encoding="utf-8")
    (folder / "plugin.py").write_text("def execute_action(request):\n    return None\n", encoding="utf-8")
    (folder / "helper.py").write_text("from selenium import webdriver\n", encoding="utf-8")
    monkeypatch.setattr(delivery, "PLUGINS_ROOT", tmp_path)
    monkeypatch.setattr(delivery, "TARGET_PLUGIN_CODES", set() if private else {"demo"})
    monkeypatch.setattr(delivery, "SAAS_OWN_PLUGIN_CODES", set())
    monkeypatch.setattr(delivery, "PRIVATE_PLUGIN_CODES", {"demo"} if private else set())
    codes, errors = delivery.verify_plugin_migration()
    assert codes == (() if private else ("demo",))
    assert any("helper.py" in error and "公共截图" in error for error in errors)
