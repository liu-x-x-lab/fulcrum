"""真实浏览器验证群插件卡片的保存、恢复与手机导航；数据只存在临时测试库。"""

import json
import mimetypes
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

expect = pytest.importorskip('playwright.sync_api').expect

from app.models.entities import BotPlugin, GroupPluginScope, Plugin
from test_group_plugin_settings import group_settings_context  # noqa: F401
from test_provider_node_pages import provider_page_context, _login  # noqa: F401
from test_select_layout import browser  # noqa: F401


@contextmanager
def _application_server(client):
    """本机 HTTP 桥接正式应用，完整执行浏览器的 POST → 303 → GET 导航。

    Playwright 对合成重定向只拦截首个请求，后续会直接解析测试域名。
    使用临时监听端口，所有页面、静态资源和重定向都真实经过同一个测试应用。
    """

    class Handler(BaseHTTPRequestHandler):
        def _forward(self):
            path = urlsplit(self.path).path
            asset = Path(__file__).resolve().parents[1] / 'app' / path.lstrip('/')
            if path.startswith('/static/') and asset.is_file():
                status, body = 200, asset.read_bytes()
                headers = {'content-type':mimetypes.guess_type(str(asset))[0] or 'application/octet-stream'}
            else:
                response = client.request(self.command, self.path,
                    content=self.rfile.read(int(self.headers.get('content-length', 0))),
                    headers={name:value for name,value in self.headers.items() if name.lower() in {'content-type','x-csrf-token','x-requested-with','accept'}},
                    follow_redirects=False)
                status, body = response.status_code, response.content
                headers = {name:value for name,value in response.headers.items() if name in {'content-type','location'}}
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _forward
        do_POST = _forward

        def log_message(self, *_args):
            """测试只保留应用日志，避免静态资源请求淹没失败信息。"""

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture()
def group_settings_server(group_settings_context):
    """桥接和数据库使用相同生命周期，测试结束后不留下服务进程。"""

    with _application_server(group_settings_context[0]) as base_url:
        yield base_url


def _assert_card_actions_align_right(card):
    """用实际几何位置验证操作列，覆盖介绍长度及手机换行，避免只检查样式类名。"""

    head_box = card.locator('.gf-head').bounding_box()
    actions_box = card.locator('.gf-head-actions').bounding_box()
    right_padding = card.locator('.gf-head').evaluate('node => parseFloat(getComputedStyle(node).paddingRight)')
    assert abs(head_box['x'] + head_box['width'] - right_padding - actions_box['x'] - actions_box['width']) <= 1


def _assert_switch_content_alignment(switch):
    """按含内边距的控件内容边缘对齐，且文字不能越过自身边界或挤到开关下方。"""

    row_box = switch.bounding_box()
    label_box = switch.locator('.gf-row-label').bounding_box()
    control_box = switch.locator('.ui-switch').bounding_box()
    insets = switch.evaluate('''node => {
        const style = getComputedStyle(node);
        return {
            left: parseFloat(style.paddingLeft) + parseFloat(style.borderLeftWidth),
            right: parseFloat(style.paddingRight) + parseFloat(style.borderRightWidth)
        };
    }''')
    assert row_box['height'] >= 44
    assert abs(row_box['x'] + row_box['width'] - insets['right'] - control_box['x'] - control_box['width']) <= 1
    assert label_box['x'] >= row_box['x'] + insets['left'] - 1
    assert label_box['x'] + label_box['width'] <= control_box['x'] - 12


def _assert_settings_group_alignment(card):
    """输入必须落在自己的分块内；背景图占整行，经验上下限成对对齐，开关齐右。"""

    sections = card.locator('.config-section')
    boxes = [section.bounding_box() for section in sections.all()]
    assert len(boxes) == 4
    assert max(box['x'] for box in boxes) - min(box['x'] for box in boxes) <= 1
    for previous, current in zip(boxes, boxes[1:]):
        assert 15 <= current['y'] - previous['y'] - previous['height'] <= 19
    points = card.locator('[data-config-group="签到积分"]')
    assert points.locator('input[type="number"]').count() == 5
    fortune = card.locator('[data-config-group="运势签到"]')
    background = fortune.locator('[name="schema__background_url"]').bounding_box()
    minimum = fortune.locator('[name="schema__sign_exp_min"]').bounding_box()
    maximum = fortune.locator('[name="schema__sign_exp_max"]').bounding_box()
    assert abs(minimum['y'] - maximum['y']) <= 1
    assert background['width'] > minimum['width'] + maximum['width']
    assert background['y'] + background['height'] < minimum['y']
    for switch in card.locator('.config-switch').all():
        _assert_switch_content_alignment(switch)


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_group_plugin_cards_save_refresh_and_restore(group_settings_context, group_settings_server, browser, width):
    """使用正式模板、JS、认证与路由，验证卡片交互对数据库产生的真实结果。"""

    client, ids = group_settings_context
    _login(client, 'operator')
    with ids['factory']() as db:
        for code in ('weather', 'guess_image'):
            plugin = db.scalar(select(Plugin).where(Plugin.code == code))
            db.add(BotPlugin(tenant_id=ids['alpha'], bot_id=ids['bot'], plugin_id=plugin.id,
                             status='enabled', runtime_status='running'))
        db.commit()
    context = browser.new_context(viewport={'width':width, 'height':960 if width > 500 else 844}, reduced_motion='reduce')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error:errors.append(str(error)))
    page.on('requestfailed', lambda request: errors.append(f'{request.method} {request.url}: {request.failure}'))
    directory = Path(os.environ.get('QA_SCREENSHOT_DIR', '.run/group-plugin-settings'))
    directory.mkdir(parents=True, exist_ok=True)
    try:
        url = f"{group_settings_server}/channels/{ids['channel_id']}/chatrooms/{ids['room']}?tab=plugins"
        page.goto(url)
        expect(page.locator('.chatroom-tabs [aria-current="page"]')).to_contain_text('插件')
        assert page.locator('[data-group-plugin-settings]').count() == 3
        card = page.locator(f'[data-gf-key$="-{ids["binding"]}"]')
        expect(card.locator('.gf-head-copy')).to_contain_text('群聊运势签到卡片')
        page.screenshot(path=str(directory / f'group-plugins-{width}-collapsed.png'), full_page=True)
        # 总开关和展开按钮属于卡片的操作列，必须贴齐头部右侧，不能跟着介绍文字漂移。
        _assert_card_actions_align_right(card)
        card.locator('[data-gf-toggle]').click()
        _assert_settings_group_alignment(card)
        page.screenshot(path=str(directory / f'group-plugin-{width}-aligned.png'), full_page=True)
        card.locator('[name="command__0"]').fill('打卡')
        card.locator('[name="command__1"]').fill('补打卡')
        card.locator('input[type="checkbox"][name="schema__fortune_image_enabled"]').uncheck()
        card.locator('input[type="checkbox"][name="require_mention"]').check()
        # 真正的公共撤回字段需完成开关、保存、刷新和恢复；关闭时区间保持但不占空间。
        expect(card.locator('[data-revoke-range]')).to_be_hidden()
        card.locator('input[type="checkbox"][name="delivery__revoke_enabled"]').check()
        expect(card.locator('[data-revoke-range]')).to_be_visible()
        card.locator('[name="delivery__revoke_min_seconds"]').fill('61')
        card.locator('[name="delivery__revoke_max_seconds"]').fill('79')
        card.get_by_role('button', name='保存本群设置', exact=True).click()
        expect(card.locator('[data-scope-feedback]')).to_have_text('本群插件设置已保存')
        expect(card.locator('[data-scope-commands]')).to_have_text('指令：打卡、补打卡')
        with ids['factory']() as db:
            scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids['room'], GroupPluginScope.bot_plugin_id == ids['binding']))
            assert json.loads(scope.config_override_json)['fortune_image_enabled'] is False
            assert json.loads(scope.config_override_json)['reply_auto_revoke'] == {'enabled':True, 'min_seconds':61, 'max_seconds':79}
            assert scope.enabled
        page.reload()
        expect(card.locator('[name="command__0"]')).to_have_value('打卡')
        expect(card.locator('input[type="checkbox"][name="schema__fortune_image_enabled"]')).not_to_be_checked()
        expect(card.locator('input[type="checkbox"][name="delivery__revoke_enabled"]')).to_be_checked()
        expect(card.locator('[name="delivery__revoke_min_seconds"]')).to_have_value('61')
        # 群启停独立即时保存，随后仅恢复参数默认也不能把本群重新启用。
        with page.expect_response(lambda response: response.url.endswith(f'/scopes/{ids["binding"]}/mode')) as toggle:
            card.locator('[data-gf-master]').uncheck()
        assert toggle.value.status == 200
        page.wait_for_function("el => !el.disabled", arg=card.locator('[data-gf-master]').element_handle())
        expect(page.locator('[data-group-plugin-count]')).to_have_text('2/3')
        expect(card.locator('.gf-body')).to_have_css('opacity', '1')
        with ids['factory']() as db:
            scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids['room'], GroupPluginScope.bot_plugin_id == ids['binding']))
            assert not scope.enabled
        # 长卡片截图保留页面顶部，避免固定顶栏遮住标题后被误判为布局缺失。
        page.evaluate('window.scrollTo(0, 0)')
        page.screenshot(path=str(directory / f'group-plugin-{width}-expanded.png'), full_page=True)
        card.get_by_role('button', name='恢复配置默认', exact=True).click()
        expect(card.locator('[name="command__0"]')).to_have_value('签到')
        expect(card.locator('input[type="checkbox"][name="schema__fortune_image_enabled"]')).to_be_checked()
        expect(card.locator('input[type="checkbox"][name="delivery__revoke_enabled"]')).not_to_be_checked()
        expect(card.locator('[data-revoke-range]')).to_be_hidden()
        expect(card.locator('[name="delivery__revoke_min_seconds"]')).to_have_value('60')
        expect(card.locator('[name="delivery__revoke_max_seconds"]')).to_have_value('80')
        expect(card.locator('[data-gf-master]')).not_to_be_checked()
        with page.expect_navigation():
            card.get_by_role('button', name='全部恢复默认', exact=True).click()
        expect(card.locator('[data-gf-master]')).to_be_checked()
        expect(card.locator('[data-scope-source]')).to_have_text('跟随默认')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        # 同一套真实页面补查插件详情和群功能，卡片头操作列使用相同的右侧基准。
        page.goto(f"{group_settings_server}/plugins/{ids['plugin']}")
        binding = page.locator(f'[data-gf-key="plugin-binding-{ids["binding"]}"]')
        if binding.locator('[data-gf-toggle]').get_attribute('aria-expanded') != 'true':
            binding.locator('[data-gf-toggle]').click()
        _assert_card_actions_align_right(binding)
        expect(binding.locator('[data-revoke-settings]')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.evaluate('window.scrollTo(0, 0)')
        page.screenshot(path=str(directory / f'plugin-detail-{width}-compact.png'), full_page=True)
        page.goto(url.replace('tab=plugins', 'tab=features'))
        for toggle in page.locator('[data-gf-toggle]').all():
            if toggle.get_attribute('aria-expanded') != 'true':
                toggle.click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        # 原来“开关跟着文字走”的断言已被用户替换：现在按所属行的右侧基准对齐。
        for feature_card in page.locator('[data-gf-card]').all():
            _assert_card_actions_align_right(feature_card)
        for switch in page.locator('.gf-switch-list .config-switch').all():
            _assert_switch_content_alignment(switch)
        page.evaluate('window.scrollTo(0, 0)')
        page.screenshot(path=str(directory / f'group-features-{width}-compact.png'), full_page=True)
        # 手机最后一个入口必须能在导航自身滚动后访问，不能因裁切而不可点击。
        profile = page.locator('.chatroom-tabs a').last
        profile.scroll_into_view_if_needed()
        profile.click()
        expect(page.locator('.chatroom-tabs [aria-current="page"]')).to_have_text('资料')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(directory / f'group-profile-{width}.png'), full_page=True)
        assert not errors
    finally:
        context.close()
