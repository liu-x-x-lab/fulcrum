"""媒体库的真实浏览器筛选、预览、确认清理、只读及窄屏交互。"""

from pathlib import Path
import os
from urllib.parse import urlsplit

import pytest

from app.services import media_storage, outbound_files
from tests.media_audio_fixtures import silk_bytes

expect = pytest.importorskip("playwright.sync_api").expect

from tests.test_media_library import duplicate_in_another_group, library_context  # noqa: F401
from tests.test_provider_node_pages import _login, provider_page_context  # noqa: F401
from tests.test_select_layout import browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _serve(route, client):
    url = urlsplit(route.request.url)
    if url.path.startswith("/static/"):
        asset = ROOT / "app" / url.path.lstrip("/")
        if asset.is_file():
            route.fulfill(path=str(asset))
            return
    response = client.request(route.request.method, url.path + ("?" + url.query if url.query else ""),
        content=route.request.post_data,
        headers={name: value for name, value in route.request.headers.items() if name in {"content-type", "range"}},
        follow_redirects=True)
    route.fulfill(status=response.status_code, body=response.content,
        headers={name: value for name, value in response.headers.items() if name in {"content-type", "content-range", "accept-ranges"}})


def test_media_controls_filters_cleanup_and_responsive_layout(library_context, browser):
    client, ids = library_context
    _login(client, "admin")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    context.route("http://fulcrum.test/**", lambda route: _serve(route, client))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto("http://fulcrum.test/media")
        expect(page.get_by_role("heading", name="媒体库", exact=True)).to_be_visible()
        expect(page.locator('[data-media-delete]')).to_be_disabled()
        assert page.get_by_label("文件名", exact=True).evaluate("field => parseFloat(getComputedStyle(field).borderRadius)") > 0
        for width, height in ((1440, 900), (1024, 768), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"), width
            if os.environ.get("QA_SCREENSHOT_DIR"):
                directory = Path(os.environ["QA_SCREENSHOT_DIR"])
                directory.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(directory / f"media-{width}.png"), full_page=True)
        preview = page.get_by_role("button", name="预览 generated.png")
        preview.focus()
        preview.press("Enter")
        expect(page.get_by_role("dialog", name="媒体查看")).to_be_visible()
        expect(page.get_by_role("button", name="复原（适应窗口）")).to_be_enabled()
        page.keyboard.press("Shift+Tab")
        expect(page.get_by_role("button", name="复原（适应窗口）")).to_be_focused()
        page.keyboard.press("Tab")
        expect(page.get_by_role("dialog", name="媒体查看").get_by_role("button", name="关闭", exact=True)).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog", name="媒体查看")).to_be_hidden()
        expect(preview).to_be_focused()
        audio = page.locator("audio")
        audio.evaluate("player => player.play()")
        assert audio.evaluate("player => !player.paused")
        audio.evaluate("player => player.pause()")
        # 浏览器生成合成短片，验证真实播放而不是只断言出现 video 标签。
        clip = bytes(page.evaluate("""async () => {
            const canvas = document.createElement('canvas');
            canvas.width = 64; canvas.height = 48;
            const brush = canvas.getContext('2d'), stream = canvas.captureStream(10);
            const recorder = new MediaRecorder(stream, {mimeType:'video/mp4;codecs=vp9'});
            const chunks = [];
            recorder.ondataavailable = event => chunks.push(event.data);
            const stopped = new Promise(resolve => recorder.onstop = resolve);
            recorder.start();
            for (const color of ['red', 'blue', 'green']) {
                brush.fillStyle = color; brush.fillRect(0, 0, 64, 48);
                await new Promise(resolve => setTimeout(resolve, 150));
            }
            recorder.stop(); await stopped;
            stream.getTracks().forEach(track => track.stop());
            return Array.from(new Uint8Array(await new Blob(chunks).arrayBuffer()));
        }"""))
        outbound_files.store_outbound_upload(tenant_id=ids["alpha"], channel_id=ids["channel_id"],
            file_name="clip.mp4", content=clip, content_type="video/mp4", message_type="video")
        page.reload()
        video_trigger = page.get_by_role("button", name="播放 clip.mp4")
        expect(video_trigger.locator('img')).to_be_visible()
        assert video_trigger.locator('img').evaluate("image => image.naturalWidth > 0")
        video_trigger.click()
        expect(page.get_by_role("group", name="图片缩放")).to_be_hidden()
        video = page.locator(".app-lightbox video")
        video.evaluate("player => { player.loop = true; return player.play(); }")
        page.wait_for_function("document.querySelector('.app-lightbox video').currentTime > 0")
        assert video.evaluate("player => player.videoWidth") == 64
        page.keyboard.press("Escape")
        expect(video_trigger).to_be_focused()
        expect(page.locator(".app-lightbox video")).to_have_count(0)
        expect(page.get_by_label("文件名", exact=True)).to_be_hidden()
        page.get_by_text("筛选媒体", exact=True).click()
        page.get_by_label("文件名", exact=True).fill("generated")
        with page.expect_navigation():
            page.get_by_role("button", name="筛选", exact=True).click()
        expect(page.locator('[data-media-item]')).to_have_count(1)
        page.get_by_role("checkbox", name="选择本页", exact=True).check()
        page.locator('[data-media-delete]').click()
        dialog = page.locator(".app-dialog")
        expect(dialog).to_be_visible()
        page.get_by_role("button", name="取消", exact=True).click()
        assert ids["stored"]["target_path"].exists()
        page.locator('[data-media-delete]').click()
        with page.expect_navigation():
            page.get_by_role("button", name="确认", exact=True).click()
        assert not ids["stored"]["target_path"].exists()
        assert not errors
    finally:
        context.close()


def test_readonly_empty_and_long_filename_layout(library_context, browser):
    client, ids = library_context
    _login(client, "readonly")
    with ids["factory"]() as db:
        from app.models.entities import MessageAttachment
        import json
        row = db.get(MessageAttachment, ids["attachment_id"])
        row.metadata_json = json.dumps({"file_name": "<script>alert(1)</script>" + "很长的语音名称" * 25 + ".wav"})
        db.commit()
    context = browser.new_context(viewport={"width": 390, "height": 844})
    context.route("http://fulcrum.test/**", lambda route: _serve(route, client))
    page = context.new_page()
    try:
        page.goto("http://fulcrum.test/media")
        expect(page.locator('[data-media-delete]')).to_have_count(0)
        expect(page.locator('[data-media-select]')).to_have_count(0)
        expect(page.locator('[data-media-scan]')).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
        path = Path(ids["attachment_path"])
        media_storage.write(path, b"\x02#!SILK_V3fixture", tenant_id=ids["alpha"], boundary=path.parent)
        page.locator("audio").evaluate("player => player.play().catch(() => undefined)")
        expect(page.locator(".media-play-error")).to_be_visible()
        page.goto("http://fulcrum.test/media?query=missing")
        expect(page.get_by_text("没有找到已保存的媒体", exact=False)).to_be_visible()
    finally:
        context.close()


def test_downloaded_silk_plays_in_the_browser_without_replacing_original(library_context, browser):
    client, ids = library_context
    _login(client, "readonly")
    original = Path(ids["attachment_path"])
    content = silk_bytes(packet_ms=100)
    media_storage.write(original, content, tenant_id=ids["alpha"], boundary=original.parent)
    context = browser.new_context(viewport={"width": 390, "height": 844})
    context.route("http://fulcrum.test/**", lambda route: _serve(route, client))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto("http://fulcrum.test/media?kind=audio")
        audio = page.get_by_label("试听 voice.wav", exact=True)
        audio.evaluate("player => player.play()")
        page.wait_for_function("document.querySelector('audio').currentTime > 0")
        assert audio.evaluate("player => player.duration") == pytest.approx(1, abs=0.05)
        assert audio.evaluate("player => player.error") is None
        expect(page.locator(".media-play-error")).to_be_hidden()
        assert original.read_bytes() == content and not errors
    finally:
        context.close()


def test_dedup_scan_review_cancel_and_confirm(library_context, browser):
    client, ids = library_context
    _reference, path, _conversation = duplicate_in_another_group(ids, legacy=True)
    original = ids["stored"]["target_path"]
    _login(client, "admin")
    context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    context.route("http://fulcrum.test/**", lambda route: _serve(route, client))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto("http://fulcrum.test/media")
        page.get_by_role("button", name="扫描重复文件").click()
        expect(page.get_by_role("heading", name="整理重复媒体", exact=True)).to_be_visible()
        expect(page.get_by_text("duplicate.png", exact=False)).to_be_visible()
        assert not original.samefile(path)
        for width, height in ((1440, 900), (1024, 768), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"), width
        trigger = page.get_by_role("button", name="确认整理", exact=True)
        trigger.focus()
        trigger.press("Enter")
        expect(page.locator(".app-dialog")).to_be_visible()
        page.keyboard.press("Escape")
        expect(trigger).to_be_focused()
        assert not original.samefile(path)
        trigger.click()
        with page.expect_navigation():
            page.get_by_role("button", name="确认", exact=True).click()
        assert original.samefile(path)
        expect(page.locator('[data-media-item]')).to_have_count(3)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("size", [(1440, 900), (1024, 768), (390, 844)])
def test_masonry_info_and_shared_image_zoom(library_context, browser, size):
    import io
    from PIL import Image

    client, ids = library_context
    _login(client, "admin")
    buffer = io.BytesIO()
    Image.new("RGB", (2400, 1600), "royalblue").save(buffer, format="PNG")
    large = outbound_files.store_outbound_upload(tenant_id=ids["alpha"], channel_id=ids["channel_id"],
        file_name="wide.png", content=buffer.getvalue(), content_type="image/png", message_type="image")
    buffer = io.BytesIO()
    Image.new("RGB", (600, 1200), "orange").save(buffer, format="PNG")
    outbound_files.store_outbound_upload(tenant_id=ids["alpha"], channel_id=ids["channel_id"],
        file_name="portrait.png", content=buffer.getvalue(), content_type="image/png", message_type="image")
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce")
    context.route("http://fulcrum.test/**", lambda route: _serve(route, client))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto("http://fulcrum.test/media")
        trigger = page.get_by_role("button", name="预览 wide.png")
        picture = trigger.locator('img')
        picture.scroll_into_view_if_needed()
        page.wait_for_function("Array.from(document.querySelectorAll('.media-open img')).every(image => image.complete)")
        box = picture.bounding_box()
        assert box["width"] / box["height"] == pytest.approx(1.5, abs=0.01)
        assert page.locator('.media-grid audio').count() == 1
        assert page.locator('.media-grid').evaluate("node => getComputedStyle(node).gridTemplateColumns.split(' ').length") == {1440: 4, 1024: 3, 390: 1}[size[0]]
        if size[0] == 1440:
            # 高低四张卡片应从四列开头排起，不能因高度平衡把音频塞到第二列而留空第四列。
            positions = page.locator('[data-media-item]').evaluate_all("nodes => nodes.map(node => node.getBoundingClientRect().x)")
            assert len(set(positions)) == 4
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        panel = page.locator('.media-info').filter(has=page.get_by_label("文件信息 wide.png", exact=True))
        expect(panel.get_by_text("wide.png", exact=True)).to_be_hidden()
        info = page.get_by_label("文件信息 wide.png", exact=True)
        info.focus()
        info.press("Enter")
        expect(panel.get_by_role("link", name="下载", exact=True)).to_be_visible()
        assert picture.bounding_box()["height"] == box["height"]
        panel_box = panel.locator('.media-info-panel').bounding_box()
        assert panel_box['x'] >= 0 and panel_box['x'] + panel_box['width'] <= size[0]
        page.keyboard.press("Escape")
        expect(info).to_be_focused()
        expect(panel.get_by_role("link", name="下载", exact=True)).to_be_hidden()
        # 音频的元数据也收进同类按钮，保留原播放条。
        page.get_by_label("文件信息 voice.wav", exact=True).click()
        expect(page.locator('.media-info[open]').get_by_role('link', name='来源会话')).to_be_visible()
        trigger.click()
        image = page.locator('.app-lightbox-stage img')
        toolbar = page.get_by_role("group", name="图片缩放")
        expect(toolbar.get_by_role('button', name='放大', exact=True)).to_be_enabled()
        fitted = image.bounding_box()
        assert fitted['width'] < 2400 and fitted['height'] <= size[1] - 140
        toolbar.get_by_role('button', name='原始尺寸 1:1', exact=True).click()
        assert image.bounding_box()['width'] == pytest.approx(2400)
        expect(page.get_by_label('缩放比例', exact=True)).to_have_text('100%')
        toolbar.get_by_role('button', name='放大', exact=True).click()
        assert image.bounding_box()['width'] == pytest.approx(3000)
        toolbar.get_by_role('button', name='缩小', exact=True).click()
        before = image.bounding_box()
        stage = page.locator('.app-lightbox-stage').bounding_box()
        x, y = stage['x'] + stage['width'] / 2 + 30, stage['y'] + stage['height'] / 2 + 20
        page.mouse.move(x, y)
        page.mouse.wheel(0, -120)
        page.wait_for_function("document.querySelector('.app-lightbox-stage img').getBoundingClientRect().width > 2500")
        after = image.bounding_box()
        assert (x - before['x']) / before['width'] == pytest.approx((x - after['x']) / after['width'], abs=0.001)
        assert (y - before['y']) / before['height'] == pytest.approx((y - after['y']) / after['height'], abs=0.001)
        page.mouse.down()
        page.mouse.move(x + 50, y + 40, steps=5)
        page.mouse.up()
        moved = image.bounding_box()
        assert moved['x'] == pytest.approx(after['x'] + 50, abs=1)
        assert moved['y'] == pytest.approx(after['y'] + 40, abs=1)
        expect(page.get_by_role('dialog', name='媒体查看')).to_be_visible()
        toolbar.get_by_role('button', name='复原（适应窗口）', exact=True).click()
        assert image.bounding_box()['width'] == pytest.approx(fitted['width'], abs=1)
        if os.environ.get('QA_SCREENSHOT_DIR'):
            directory = Path(os.environ['QA_SCREENSHOT_DIR'])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / f'zoom-{size[0]}.png'))
        page.keyboard.press('1')
        assert image.bounding_box()['width'] == pytest.approx(2400)
        page.keyboard.press('0')
        assert image.bounding_box()['width'] == pytest.approx(fitted['width'], abs=1)
        # 共用 API 的多图切换与再次打开都应恢复各自适应窗口状态。
        page.evaluate("items => window.appLightbox.open(items, 0)", [
            {"type": "image", "url": f"/media/items/o{ids['channel_id']}-{large['share_id']}/file"},
            {"type": "image", "url": f"/media/items/{ids['outbound_ref']}/file"},
        ])
        expect(toolbar.get_by_role('button', name='放大', exact=True)).to_be_enabled()
        page.keyboard.press('1')
        page.keyboard.press('ArrowRight')
        expect(page.get_by_label('缩放比例', exact=True)).to_have_text('100%')
        assert image.bounding_box()['width'] == pytest.approx(32)
        page.keyboard.press('Escape')
        expect(trigger).to_be_focused()
        if os.environ.get('QA_SCREENSHOT_DIR'):
            page.screenshot(path=str(directory / f'masonry-{size[0]}.png'), full_page=True)
        assert not errors
    finally:
        context.close()


def test_cover_failure_keeps_playback_and_image_error_closes(library_context, browser):
    client, ids = library_context
    _login(client, "readonly")
    outbound_files.store_outbound_upload(tenant_id=ids["alpha"], channel_id=ids["channel_id"],
        file_name="broken.mp4", content=b"invalid video", content_type="video/mp4", message_type="video")
    context = browser.new_context(viewport={"width": 390, "height": 844})
    context.route("http://fulcrum.test/**", lambda route: _serve(route, client))
    page = context.new_page()
    try:
        page.goto("http://fulcrum.test/media")
        trigger = page.get_by_role("button", name="播放 broken.mp4", exact=True)
        expect(trigger).to_be_visible()
        expect(trigger.locator('img')).to_have_count(0)
        trigger.click()
        expect(page.get_by_role('group', name='图片缩放')).to_be_hidden()
        page.keyboard.press('Escape')
        expect(trigger).to_be_focused()
        page.evaluate("window.appLightbox.open([{type:'image',url:'/media/items/a999999/file'}],0)")
        expect(page.locator('.app-lightbox-error')).to_be_visible()
        expect(page.get_by_role('group', name='图片缩放')).to_be_hidden()
        page.keyboard.press('Escape')
        expect(trigger).to_be_focused()
    finally:
        context.close()
