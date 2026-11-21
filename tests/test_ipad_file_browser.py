from test_group_plugin_settings_browser import _application_server
from test_ipad_file_delivery import CONTENT, file_context  # noqa: F401
from test_message_center import seeded  # noqa: F401
from test_provider_node_pages import provider_page_context  # noqa: F401
from test_select_layout import browser  # noqa: F401


def test_file_picker_sends_attachment_in_selected_group(file_context, browser):
    client, _, cid, _, calls = file_context
    with _application_server(client) as base:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            page.goto(f"{base}/messages?conversation_id={cid}")
            with page.expect_file_chooser() as picker:
                page.get_by_role("button", name="发送文件", exact=True).click()
            picker.value.set_files({"name": "交班记录.txt", "mimeType": "text/plain", "buffer": CONTENT})
            page.locator("#wechat-attachment").get_by_text("交班记录.txt", exact=False).wait_for()
            with page.expect_response(lambda res: res.url.endswith("/messages/send")) as send:
                page.locator(".wechat-send").click()
            result = send.value.json()
            assert result["ok"], result
            page.locator(f'.wechat-message-row[data-id="{result["message_id"]}"]').get_by_text("交班记录.txt").wait_for()
            assert len(calls) == 1 and calls[0][1]["ToWxid"] == "room@chatroom"
        finally:
            page.close()
