"""通过正式模板、脚本和认证路由验证 MaiBot 多连接选择器。"""

import json
from pathlib import Path

import pytest

expect = pytest.importorskip("playwright.sync_api").expect

from app.models.entities import BotPlugin, WorkWechatContact
from test_group_plugin_settings_browser import _application_server
from test_maibot_connections import _connection, _login, _save, _target, connections_context  # noqa: F401
from test_provider_node_pages import provider_page_context  # noqa: F401
from test_select_layout import browser  # noqa: F401


@pytest.mark.parametrize("width", [1440, 390])
def test_connections_search_transfer_save_and_mobile_layout(connections_context, browser, width):
    client, ids = connections_context
    csrf = _login(client, "operator")
    values = [_connection(targets=[_target(ids)], token="browser-daily-secret", enabled=False),
              _connection("hobby", targets=[_target(ids, "room-two", channel=ids["second"])], enabled=False)]
    assert _save(client, ids, csrf, values).status_code == 200
    context = browser.new_context(viewport={"width": width, "height": 960}, reduced_motion="reduce")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    screenshots = Path(".run/maibot-multi-qa")
    screenshots.mkdir(parents=True, exist_ok=True)
    try:
        with _application_server(client) as base:
            page.goto(f"{base}/plugins/{ids['plugin']}")
            root = page.locator("[data-maibot-manager]")
            expect(root.locator("[data-mb-feedback]")).to_contain_text("配置已载入")
            expect(root.locator("[data-mb-connections] button")).to_have_count(2)
            expect(root.locator("[data-mb-token]")).to_have_value("")
            expect(root.locator("[data-mb-token]")).to_have_attribute("placeholder", "已配置，留空保持不变")
            panel = root.locator("[data-mb-picker-panel]")
            groups = root.locator('[data-mb-type="group"]')
            direct = root.locator('[data-mb-type="direct"]')
            expect(panel).to_be_hidden()
            expect(groups).to_contain_text("已选 1 个")
            groups.click()
            expect(panel).to_be_visible()
            expect(groups).to_have_attribute("aria-expanded", "true")
            expect(root.locator(".mb-target")).to_have_count(3)
            expect(root.locator("[data-mb-count]")).to_contain_text("1 个群聊")
            root.locator("[data-mb-search]").fill("周末")
            expect(root.locator(".mb-target")).to_have_count(1)
            expect(root.locator(".mb-target input")).to_be_checked()
            root.locator("[data-mb-search]").fill("")
            expect(root.locator(".mb-target")).to_have_count(3)
            # 通过正式自绘下拉选择账号，确保动态数据同步给下拉组件。
            root.locator(".mb-search .ui-select-trigger").click()
            expect(root.locator(".mb-search .ui-select-trigger")).to_have_attribute("aria-expanded", "true")
            root.locator(".mb-search .ui-select-trigger").press("Escape")
            expect(panel).to_be_visible()
            root.locator(".mb-search .ui-select-trigger").click()
            root.get_by_role("option", name="第二微信", exact=True).click()
            expect(root.locator(".mb-target")).to_have_count(1)
            root.locator("[data-mb-transfer]").click()
            expect(root.locator(".mb-target input")).to_be_checked()
            expect(root.locator("[data-mb-count]")).to_contain_text("2 个群聊")
            expect(panel).to_be_visible()
            expect(groups).to_contain_text("已选 2 个")
            panel.screenshot(path=str(screenshots / f"maibot-dropdown-group-{width}.png"))
            root.locator('[data-mb-connection="hobby"]').click()
            expect(panel).to_be_hidden()
            expect(root.locator("[data-mb-count]")).to_contain_text("0 个群聊")
            groups.click()
            expect(root.locator("[data-mb-transfer]")).to_have_count(1)
            root.locator('[data-mb-connection="daily"]').click()
            root.locator("[data-mb-name]").fill("日常麦麦新版")
            direct.click()
            expect(root.locator(".mb-target")).to_have_count(1)
            root.locator(".mb-target input").check()
            expect(root.locator("[data-mb-count]")).to_contain_text("1 个私聊")
            expect(direct).to_contain_text("已选 1 个")
            panel.screenshot(path=str(screenshots / f"maibot-dropdown-private-{width}.png"))
            root.locator("[data-mb-picker-close]").click()
            expect(panel).to_be_hidden()
            root.locator("[data-mb-save]").first.click()
            expect(root.locator("[data-mb-feedback]")).to_contain_text("已保存并应用")
            with ids["factory"]() as db:
                binding = db.get(BotPlugin, ids["binding"])
                raw = json.loads(binding.config_json)
                assert len(raw["connections"][0]["scope"]["targets"]) == 3
                assert raw["connections"][1]["scope"]["targets"] == []
                assert "browser-daily-secret" not in binding.config_json
            page.reload()
            expect(root.locator("[data-mb-name]")).to_have_value("日常麦麦新版")
            expect(root.locator("[data-mb-count]")).to_contain_text("2 个群聊、1 个私聊")
            expect(panel).to_be_hidden()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            root.screenshot(path=str(screenshots / f"maibot-connections-{width}.png"))
            root.locator("[data-mb-add]").click()
            expect(root.locator("[data-mb-connections] button")).to_have_count(3)
            expect(root.locator("[data-mb-enabled]")).not_to_be_checked()
            expect(root.locator("[data-mb-count]")).to_contain_text("0 个群聊、0 个私聊")
            root.locator("[data-mb-delete]").click()
            page.locator('.app-dialog [data-role="confirm"]').click()
            root.locator("[data-mb-save]").first.click()
            expect(root.locator("[data-mb-feedback]")).to_contain_text("已保存并应用")
            assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("size", [(1440, 720), (390, 640), (390, 380)])
def test_chat_dropdown_keyboard_paging_and_dismissal(connections_context, browser, size):
    client, ids = connections_context
    csrf = _login(client, "operator")
    assert _save(client, ids, csrf, [_connection(targets=[], enabled=False)]).status_code == 200
    with ids["factory"]() as db:
        db.add_all([WorkWechatContact(tenant_id=ids["alpha"], channel_id=ids["channel_id"], contact_kind="friend",
                                     contact_external_id=f"picker-friend-{i}", name=f"下拉好友 {i:02}") for i in range(25)])
        db.commit()
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce")
    page = context.new_page()
    try:
        with _application_server(client) as base:
            page.goto(f"{base}/plugins/{ids['plugin']}")
            root = page.locator("[data-maibot-manager]")
            expect(root.locator("[data-mb-feedback]")).to_contain_text("配置已载入")
            direct = root.locator('[data-mb-type="direct"]')
            panel = root.locator("[data-mb-picker-panel]")
            search = root.locator("[data-mb-search]")
            direct.focus()
            direct.press("ArrowDown")
            expect(panel).to_be_visible()
            expect(search).to_be_focused()
            search.fill("下拉好友")
            expect(root.locator("[data-mb-result-count]")).to_have_text("25 项")
            expect(root.locator(".mb-target")).to_have_count(20)
            bounds = panel.bounding_box()
            assert 0 <= bounds['x'] and bounds['x'] + bounds['width'] <= size[0]
            assert 0 <= bounds['y'] and bounds['y'] + bounds['height'] <= size[1]
            screenshot_dir = Path('.run/maibot-multi-qa')
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_dir / f'picker-viewport-{size[0]}x{size[1]}.png'))
            before = page.evaluate("window.scrollY")
            root.locator("[data-mb-targets]").evaluate("node => { node.scrollTop = node.scrollHeight; }")
            expect(panel).to_be_visible()
            assert page.evaluate("window.scrollY") == before
            # 用键盘选择后焦点保留在当前选项，多选不关闭下拉。
            first = root.locator(".mb-target input").first
            first.focus()
            first.press("Space")
            expect(first).to_be_checked()
            expect(first).to_be_focused()
            expect(panel).to_be_visible()
            root.locator("[data-mb-next]").click()
            expect(root.locator(".mb-target")).to_have_count(5)
            expect(root.locator("[data-mb-page]")).to_have_text("第 2 页")
            assert root.locator('[data-mb-targets]').evaluate('node => node.clientHeight') >= 60
            root.locator(".mb-target input").last.check()
            expect(direct).to_contain_text("已选 2 个")
            search.fill("找不到的聊天")
            search.press("Enter")
            expect(root.locator("[data-mb-result-count]")).to_have_text("0 项")
            expect(root.locator("[data-mb-targets]")).to_contain_text("没有匹配的聊天")
            expect(root.locator("[data-mb-feedback]")).to_contain_text("有未保存的更改")
            search.press("Escape")
            expect(panel).to_be_hidden()
            expect(direct).to_be_focused()
            direct.press("Enter")
            expect(panel).to_be_visible()
            root.locator("[data-mb-name]").click()
            expect(panel).to_be_hidden()
            expect(direct).to_have_attribute("aria-expanded", "false")
            direct.click()
            expect(panel).to_be_visible()
            expect(root.locator('[data-mb-targets]')).not_to_have_attribute('aria-busy', 'true')
            # 手机短屏的面板会覆盖触发区，使用面板内始终可见的完成按钮收起。
            if size[0] < 600:
                root.locator('[data-mb-picker-close]').click()
            else:
                direct.click()
            expect(panel).to_be_hidden()
            root.locator("[data-mb-save]").first.click()
            expect(root.locator("[data-mb-feedback]")).to_contain_text("已保存并应用")
            page.reload()
            expect(direct).to_contain_text("已选 2 个")
            expect(panel).to_be_hidden()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()
