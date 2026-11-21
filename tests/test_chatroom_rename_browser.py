"""真实资料页与共享提交反馈：权限核验失败时不丢失输入。"""

import pytest

expect = pytest.importorskip("playwright.sync_api").expect

from app.models.entities import Channel
from app.services.wechat_ipad.client import WechatIpadApiClient
from test_chatroom_profile_pages import profile_page_context  # noqa: F401
from test_group_plugin_settings_browser import _application_server
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_select_layout import browser  # noqa: F401


def test_mobile_rename_keeps_input_when_role_unverified(profile_page_context, browser, monkeypatch):
    client, ids, channel_id, room_id = profile_page_context
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        channel.channel_type, channel.api_base = "wechat_ipad", "https://example.test"
        channel.adapter_config_json = '{"wxid":"robot"}'
        db.commit()
    calls = []

    def no_evidence(self, target, endpoint, *, body):
        calls.append(endpoint)
        return {"http_status": 200, "Success": False}

    monkeypatch.setattr(WechatIpadApiClient, "request_json", no_evidence)
    _login(client, "operator")
    with _application_server(client) as base:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            page.goto(f"{base}/channels/{channel_id}/chatrooms/{room_id}?tab=profile")
            page.get_by_text("修改微信群名", exact=True).focus()
            page.keyboard.press("Enter")
            field = page.get_by_label("新群名")
            field.fill("新名称")
            page.get_by_role("button", name="确认改名").click()
            expect(page.get_by_text("未确认绑定账号是当前群主或管理员，未提交改名", exact=True)).to_be_visible()
            expect(field).to_have_value("新名称")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            assert calls == ["/api/Group/GetChatRoomInfo", "/api/Group/GetChatRoomMemberDetail"]
        finally:
            page.close()
