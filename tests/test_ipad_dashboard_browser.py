import json

import pytest

from app.models.entities import Channel, Chatroom, IpadFriend
from app.services.wechat_ipad.client import WechatIpadApiClient
from test_chatroom_profile_pages import profile_page_context  # noqa: F401
from test_group_plugin_settings_browser import _application_server
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_select_layout import browser  # noqa: F401


def test_edit_and_clear_friend_remark_across_directories(provider_page_context, browser, monkeypatch):
    client, ids = provider_page_context
    cid = ids["channel_id"]
    with ids["factory"]() as db:
        db.get(Channel, cid).runtime_info_json = '{"active_wxid":"owner"}'
        db.add(IpadFriend(tenant_id=ids["alpha"], channel_id=cid, owner_wxid="owner",
                          contact_external_id="peer", name="昵称", display_name="昵称"))
        db.commit()
    writes = []

    def protocol(self, channel, endpoint, *, body):
        if endpoint.endswith("/SetRemarks"):
            writes.append(body["Remarks"])
            data = {"ret": 0}
        else:
            data = {"BaseResponse": {"ret": 0}, "ContactList": [{"UserName": {"string": "peer"},
                    "Remark": {"string": writes[-1] if writes else ""}}]}
        return {"Success": True, "Code": 0, "Data": data, "http_status": 200}

    monkeypatch.setattr(WechatIpadApiClient, "request_json", protocol)
    _login(client, "operator")
    with _application_server(client) as base:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            for path, current, action in (("/contacts?q=peer", "昵称", "保存备注"),
                    (f"/channels/{cid}/wechat-ipad?tab=contacts&keyword=peer", "新备注", "清空备注")):
                page.goto(base + path)
                page.get_by_label(f"编辑 {current} 的备注").click()
                if action == "保存备注":
                    page.get_by_label("好友备注", exact=True).fill("新备注")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                with page.expect_navigation():
                    page.get_by_role("button", name=action).click()
                assert "peer" in page.url
                page.get_by_label("编辑 " + ("昵称" if action == "清空备注" else "新备注") + " 的备注").wait_for()
            assert writes == ["新备注", ""]
        finally:
            page.close()


def test_start_friend_chat_and_return_to_filtered_lists(provider_page_context, browser):
    client, ids = provider_page_context
    cid = ids["channel_id"]
    with ids["factory"]() as db:
        db.get(Channel, cid).runtime_info_json = '{"active_wxid":"owner"}'
        for key in ("local", "global"):
            db.add(IpadFriend(tenant_id=ids["alpha"], channel_id=cid, owner_wxid="owner",
                              contact_external_id=key, display_name="好友" + key))
        db.commit()
    _login(client, "operator")
    with _application_server(client) as base:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            for path in (f"/channels/{cid}/wechat-ipad?tab=contacts&keyword=local",
                         f"/contacts?channel_id={cid}&q=global"):
                page.goto(base + path)
                page.get_by_role("button", name="发起聊天", exact=True).click()
                page.get_by_role("link", name="返回列表", exact=True).click()
                page.wait_for_url(base + path)
                assert page.get_by_role("link", name="聊天记录", exact=True).is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        finally:
            page.close()


@pytest.mark.parametrize("width", [390, 1280])
def test_ipad_group_card_returns_to_filtered_directory(profile_page_context, browser, width):
    client, ids, cid, rid = profile_page_context
    with ids["factory"]() as db:
        db.get(Channel, cid).channel_type = "wechat_ipad"
        room = db.get(Chatroom, rid)
        extra = json.loads(room.extra_json)
        extra["group_features"] = {"member_change": {"enabled": True, "config": {
            "card_enabled": True, "card_url": "https://example.test/welcome"}}}
        room.extra_json = json.dumps(extra)
        db.commit()
    _login(client, "operator")
    with _application_server(client) as base:
        page = browser.new_page(viewport={"width": width, "height": 844})
        try:
            page.goto(f"{base}/channels/{cid}/wechat-ipad?tab=groups")
            page.get_by_label("搜索目录").fill("资料测试")
            page.get_by_role("button", name="搜索", exact=True).click()
            link = page.get_by_role("link", name="群详情", exact=True)
            link.wait_for()
            source = page.url
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            link.click()
            card = page.locator('form[data-gf-key="member_change"]')
            toggle = card.locator("[data-gf-toggle]")
            if toggle.get_attribute("aria-expanded") != "true":
                toggle.click()
            card.locator('[name="cfg_card_title"]').fill("新成员欢迎卡")
            with page.expect_response(lambda r: r.url.endswith("/features/member_change") and r.request.method == "POST") as saved:
                card.get_by_role("button", name="保存", exact=True).click()
            assert saved.value.json()["ok"]
            with ids["factory"]() as db:
                assert json.loads(db.get(Chatroom, rid).extra_json)["group_features"]["member_change"]["config"]["card_title"] == "新成员欢迎卡"
            page.locator(".page-back").click()
            page.wait_for_url(source)
            assert page.get_by_label("搜索目录").input_value() == "资料测试"
        finally:
            page.close()
