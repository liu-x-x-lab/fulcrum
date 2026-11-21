from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter
from test_group_plugin_settings_browser import _application_server
from test_ipad_file_delivery import file_context  # noqa: F401
from test_ipad_video_delivery import response
from test_message_center import seeded  # noqa: F401
from test_provider_node_pages import provider_page_context  # noqa: F401
from test_select_layout import browser  # noqa: F401


def test_video_picker_and_thumbnail_only_receipt(file_context, browser, monkeypatch):
    client, _, cid, _, calls = file_context

    def post(self, url, body):
        calls.append(body)
        return response(videoStartPos=0)

    monkeypatch.setattr(WechatIpadAdapter, "_post_json", post)
    with _application_server(client) as base:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            page.goto(f"{base}/messages?conversation_id={cid}")
            clip = bytes(page.evaluate("""async () => {
                const canvas = document.createElement('canvas');
                canvas.width = 64; canvas.height = 48;
                const ctx = canvas.getContext('2d'), stream = canvas.captureStream(10);
                const recorder = new MediaRecorder(stream, {mimeType:'video/mp4;codecs=vp9'});
                const chunks = [];
                recorder.ondataavailable = e => chunks.push(e.data);
                const stopped = new Promise(r => recorder.onstop = r);
                recorder.start();
                for (const color of ['red', 'blue', 'green', 'yellow']) {
                    ctx.fillStyle = color; ctx.fillRect(0, 0, 64, 48);
                    await new Promise(r => setTimeout(r, 150));
                }
                recorder.stop(); await stopped;
                stream.getTracks().forEach(t => t.stop());
                return Array.from(new Uint8Array(await new Blob(chunks).arrayBuffer()));
            }"""))
            def pick(data):
                with page.expect_file_chooser() as chooser:
                    page.get_by_role("button", name="发送视频", exact=True).click()
                chooser.value.set_files({"name": "clip.mp4", "mimeType": "video/mp4", "buffer": data})
                page.locator("#wechat-attachment").get_by_text("clip.mp4", exact=False).wait_for()

            pick(b"broken-video")
            page.locator(".wechat-send").click()
            page.locator("#wechat-composer-hint").get_by_text("无法读取 MP4", exact=False).wait_for()
            assert page.locator("#wechat-attachment").is_visible() and not calls
            page.get_by_role("button", name="取消文件").click()
            pick(clip)
            with page.expect_response(lambda r: r.url.endswith("/messages/send"), timeout=15000) as sent:
                page.locator(".wechat-send").click()
            assert sent.value.json()["unconfirmed"]
            page.locator("#wechat-composer-hint").get_by_text("待核实", exact=False).wait_for()
            assert page.locator("#wechat-attachment").is_hidden()
            assert len(calls) == 1 and calls[0]["ToWxid"] == "room@chatroom"
            assert calls[0]["ImageBase64"].startswith("data:image/jpeg;base64,/9j/") and calls[0]["PlayLength"] >= 1
        finally:
            page.close()
