"""复用真实测试应用与共享表单，在桌面和手机验证群治理配置。"""

import pytest

expect = pytest.importorskip("playwright.sync_api").expect

from test_group_feature_render_golden import feature_page  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_group_plugin_settings_browser import _application_server
from test_select_layout import browser  # noqa: F401


def test_moderation_card_save_and_responsive_layout(feature_page, browser):
    client, channel_id, room_id = feature_page
    _login(client, "admin")
    with _application_server(client) as base:
        context = browser.new_context()
        page = context.new_page()
        try:
            for width, height in ((1440, 900), (1024, 768), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                page.goto(f"{base}/channels/{channel_id}/chatrooms/{room_id}?tab=features")
                card = page.locator('[data-gf-key="moderation"]')
                toggle = card.locator('[data-gf-toggle]')
                if toggle.get_attribute("aria-expanded") != "true":
                    toggle.click()
                expect(card.locator('[name="cfg_words"]')).to_be_visible()
                card.locator('[name="cfg_words"]').fill("测试词\n第二词")
                card.locator('[name="cfg_mode"]').select_option("warn", force=True)
                expect(card.locator('[name="cfg_threshold"]')).to_be_hidden()
                card.locator('[name="cfg_warning_text"]').fill("{member} 已违规 {count} 次")
                with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/features/moderation")) as saved:
                    card.locator('.gf-foot button[type="submit"]').click()
                assert saved.value.status == 200
                page.reload()
                expect(card.locator('[name="cfg_words"]')).to_have_value("测试词\n第二词")
                card.locator('summary', has_text="最近治理记录").click()
                expect(card.get_by_text("暂无违规记录。")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
        finally:
            context.close()
