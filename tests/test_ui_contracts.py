"""共享 UI 结构约束：统一展开图标与确认弹窗，不依赖浏览器或业务数据库。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((ROOT / "app/templates").glob("*.html"))
# 下拉菜单(更多、账号菜单、"…"操作、管理)是弹出菜单,不是展开内容,不使用箭头图标。
MENU_CLASSES = ("nav-more", "account-menu", "record-action-menu", "record-actions-menu")
DETAILS = re.compile(r"<details\b([^>]*)>")
SUMMARY = re.compile(r"<summary\b[^>]*>(.*?)</summary>", re.S)


def test_every_content_details_uses_the_shared_arrow() -> None:
    missing = []
    for path in TEMPLATES:
        for attrs in DETAILS.findall(path.read_text(encoding="utf-8")):
            classes = re.search(r'class="([^"]*)"', attrs)
            names = (classes.group(1) if classes else "").split()
            if "disclosure" not in names and not any(menu in names for menu in MENU_CLASSES):
                missing.append(f"{path.name}: <details{attrs}>")
    assert missing == [], "这些展开块没有 disclosure 类(右侧箭头图标):\n" + "\n".join(missing)


def test_no_text_style_expand_hints_remain() -> None:
    offenders = []
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        for summary in SUMMARY.findall(text):
            label = re.sub(r"<[^>]+>", "", summary).strip()
            if re.search(r"展开|收起|查看", label):
                offenders.append(f"{path.name}: <summary>{label}</summary>")
        # 摘要里不再放"展开配置…"这类提示文字
        if "plugin-toggle-summary-hint" in text:
            offenders.append(f"{path.name}: plugin-toggle-summary-hint")
    css = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")
    offenders += [f"style.css: {m}" for m in re.findall(r'content:\s*"(?:展开|收起|设置)"', css)]
    assert offenders == [], "\n".join(offenders)


def test_group_feature_toggle_is_an_icon_only_button() -> None:
    # 展开按钮分在两处:运行模式卡片(chatroom_detail.html)和各群功能共用的卡片宏(_config_card.html)。
    template = "".join((ROOT / "app/templates" / name).read_text(encoding="utf-8") for name in ("chatroom_detail.html", "_config_card.html"))
    buttons = re.findall(r'<button class="gf-toggle"[^>]*>(.*?)</button>', template, re.S)
    assert len(buttons) == 2 and all(label.strip() == "" for label in buttons)  # 没有"设置"之类的文字,图标由样式绘制


NATIVE = re.compile(r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\(")


def _sources():
    for base in ("app/templates", "app/static/js"):
        for path in (ROOT / base).rglob("*"):
            if path.suffix in {".html", ".js"} and path.name != "dialog.js":
                yield path


def test_no_native_browser_dialogs_in_templates_or_scripts() -> None:
    offenders = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if NATIVE.search(line) and "appDialog" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:80]}")
    assert not offenders, "请改用 appDialog.confirm/alert 或 data-confirm-*:\n" + "\n".join(offenders)


def test_base_template_loads_the_shared_dialog_script() -> None:
    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    assert "/static/js/dialog.js" in base
    # 旧的确认框标记和内联脚本已删除,只保留统一实现。
    assert "confirm-action-modal" not in base
