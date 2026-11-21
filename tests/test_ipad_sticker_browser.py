from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter
from test_group_plugin_settings_browser import _application_server
from test_ipad_file_delivery import file_context  # noqa: F401
from test_ipad_sticker_delivery import acknowledgement, image
from test_message_center import seeded  # noqa: F401
from test_provider_node_pages import provider_page_context  # noqa: F401
from test_select_layout import browser  # noqa: F401


def test_sticker_picker_unconfirmed(file_context, browser, monkeypatch):
    client, _, cid, _, calls = file_context
    data = image()
    ack = acknowledgement(data)
    del ack["Data"]["resp"]["emojiItem"][0]["newMsgId"]

    def post(self, url, body):
        calls.append(url)
        return ack

    monkeypatch.setattr(WechatIpadAdapter, "_post_json", post)
    with _application_server(client) as base:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            page.goto(f"{base}/messages?conversation_id={cid}")
            with page.expect_file_chooser() as picker:
                page.get_by_role("button", name="发送表情", exact=True).click()
            picker.value.set_files({"name": "dance.gif", "mimeType": "image/gif", "buffer": data})
            page.locator("#wechat-attachment").get_by_text("dance.gif", exact=False).wait_for()
            with page.expect_response(lambda r: r.url.endswith("/messages/send")) as send:
                page.locator(".wechat-send").click()
            assert send.value.json()["unconfirmed"]
            page.locator("#wechat-composer-hint").get_by_text("待核实", exact=False).wait_for()
            assert len(calls) == 1 and page.locator("#wechat-attachment").is_hidden()
        finally:
            page.close()
