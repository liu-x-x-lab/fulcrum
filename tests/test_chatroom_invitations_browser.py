"""真实大群页面验证邀请汇总、成员查找与聚焦，资料只保存在临时测试库。"""

import json
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import select

expect = pytest.importorskip('playwright.sync_api').expect

from app.models.entities import ChatroomMember, TaskJob
from test_chatroom_profile_pages import profile_page_context  # noqa: F401
from test_group_plugin_settings_browser import _application_server
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_select_layout import browser  # noqa: F401


def _invitation(parent):
    return json.dumps({'invitation': {
        'inviter_external_id': parent, 'source': 'member_profile',
        'method': 'invite', 'recorded_at': '2026-11-16T10:00:00+08:00',
    }})


@pytest.fixture()
def large_invitation_group(profile_page_context):
    """500 个直属成员里仅 8 人继续邀请，另保留同名、长昵称和未关联成员。"""
    client, ids, channel_id, room_id = profile_page_context
    with ids['factory']() as db:
        for index in range(500):
            name = f'邀请分支 {index:02d}' if index < 8 else f'普通成员 {index:03d}'
            if index in (298, 299):
                name = '同名群友'
            elif index == 297:
                name = '很长的中文成员昵称' * 8 + '<script>成员文字</script>'
            db.add(ChatroomMember(tenant_id=ids['alpha'], bot_id=ids['bot'],
                channel_id=channel_id, chatroom_id=room_id,
                member_external_id=f'wxid_leaf_{index:03d}', nickname=name,
                extra_json=_invitation('wxid_owner'), status='active'))
            if index < 8:
                for child in range(2):
                    db.add(ChatroomMember(tenant_id=ids['alpha'], bot_id=ids['bot'],
                        channel_id=channel_id, chatroom_id=room_id,
                        member_external_id=f'wxid_grandchild_{index}_{child}',
                        nickname=f'分支群友 {index}-{child}',
                        extra_json=_invitation(f'wxid_leaf_{index:03d}'), status='active'))
        db.commit()
    return client, ids, channel_id, room_id


def _assert_page_width(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), '页面不应出现横向溢出'


def _search_member(page, query):
    page.locator('[data-invitation-search]').fill(query)
    result = page.locator('[data-invitation-results] button')
    expect(result).to_have_count(1)
    result.click()


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_large_invitation_graph_groups_members_and_keeps_lookup_readable(large_invitation_group, browser, width):
    """操作真实模板及脚本，确保大分支可读、人数准确且查看操作不改变邀请证据。"""
    client, ids, channel_id, room_id = large_invitation_group
    _login(client, 'readonly')
    with ids['factory']() as db:
        before = dict(db.execute(select(ChatroomMember.member_external_id, ChatroomMember.extra_json)
            .where(ChatroomMember.chatroom_id == room_id)).all())
    context = browser.new_context(viewport={'width': width, 'height': 960 if width > 500 else 844},
        reduced_motion='reduce', has_touch=width == 390)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    directory = Path(os.environ.get('QA_SCREENSHOT_DIR', '.run/invitation-graph-qa'))
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with _application_server(client) as base:
            page.goto(f'{base}/channels/{channel_id}/chatrooms/{room_id}?tab=members')
            panel = page.locator('[data-invitation-graph]')
            expect(panel).to_have_class(re.compile(r'\bis-ready\b'))
            scene = page.locator('[data-invitation-scene]')
            # 不能再将 500 位直属成员平铺为 14 万像素，也不能靠缩成细线隐藏问题。
            assert scene.evaluate('node => node.offsetWidth') < 5000
            assert page.locator('[data-invitation-node][data-connected="true"]:visible').count() <= 25
            assert float(page.locator('[data-invitation-scale]').inner_text().rstrip('%')) >= 30
            root = page.locator('[data-invitation-node][data-external-id="wxid_owner"]')
            expect(root.locator('[data-invitation-members]')).to_have_text('直接邀请 500 人')
            assert page.locator('[data-invitation-node][data-direct-invites="0"] .invitation-member-footer').count() == 0
            _assert_page_width(page)
            panel.scroll_into_view_if_needed()
            page.screenshot(path=str(directory / f'invitations-{width}-overview.png'))
            overview_keys = page.locator('[data-invitation-node][data-connected="true"]:visible').evaluate_all(
                'nodes => nodes.map(node => node.dataset.externalId)')

            # 通过人数入口查看全部直属成员，网格分页不能遗漏或重复第一页。
            _search_member(page, 'wxid_owner')
            page.locator('[data-invitation-detail-members]').click()
            members = page.locator('[data-invitation-member-browser]')
            expect(members).to_be_visible()
            expect(members.locator('[data-invitation-member-subtitle]')).to_contain_text('500')
            grid = members.locator('[data-invitation-member-grid]')
            first_page = grid.locator('button').all_text_contents()
            assert 1 < len(first_page) <= 48
            assert grid.locator('li').nth(1).bounding_box()['x'] > grid.locator('li').first.bounding_box()['x'] or width == 390
            assert grid.locator('li').last.bounding_box()['y'] > grid.locator('li').first.bounding_box()['y']
            members.scroll_into_view_if_needed()
            page.screenshot(path=str(directory / f'invitations-{width}-members.png'))
            members.locator('[data-invitation-member-next]').click()
            assert grid.locator('button').all_text_contents() != first_page
            members.locator('[data-invitation-member-prev]').click()
            assert grid.locator('button').all_text_contents() == first_page
            members.locator('[data-invitation-member-filter]').fill('wxid_leaf_299')
            expect(grid.locator('button')).to_have_count(1)
            expect(grid).to_contain_text('同名群友')
            expect(members.locator('[data-invitation-member-next]')).to_be_disabled()
            members.locator('[data-invitation-member-filter]').fill('不存在的成员')
            expect(members.locator('[data-invitation-member-empty]')).to_be_visible()
            expect(grid.locator('button')).to_have_count(0)
            members.locator('[data-invitation-member-filter]').fill('同名群友')
            expect(grid.locator('button')).to_have_count(2)
            _assert_page_width(page)
            members.scroll_into_view_if_needed()
            page.screenshot(path=str(directory / f'invitations-{width}-filtered.png'))
            members.locator('[data-invitation-member-close]').click()

            # 搜索被汇总的同名成员时，以 ID 唯一定位，不能因此展开全部兄弟。
            _search_member(page, 'wxid_leaf_299')
            expect(page.locator('[data-invitation-detail-id]')).to_have_text('wxid_leaf_299')
            expect(page.locator('[data-invitation-detail-inviter]')).to_have_text('群主群昵称')
            target = page.locator('[data-invitation-node][data-external-id="wxid_leaf_299"]')
            expect(target).to_be_visible()
            assert page.locator('[data-invitation-node][data-connected="true"]:visible').count() <= 25
            assert target.bounding_box()['width'] >= 70

            _search_member(page, 'wxid_leaf_000')
            page.locator('[data-invitation-focus]').click()
            expect(page.locator('[data-invitation-focus-bar]')).to_be_visible()
            expect(page.locator('[data-invitation-focus-label]')).to_contain_text('邀请分支 00')
            expect(page.locator('[data-invitation-node][data-external-id="wxid_leaf_001"]')).to_be_hidden()
            expect(page.locator('[data-invitation-node][data-external-id="wxid_grandchild_0_0"]')).to_be_visible()
            page.locator('[data-invitation-overview]').click()
            expect(page.locator('[data-invitation-focus-bar]')).to_be_hidden()
            assert page.locator('[data-invitation-node][data-connected="true"]:visible').evaluate_all(
                'nodes => nodes.map(node => node.dataset.externalId)') == overview_keys

            page.locator('[data-invitation-expand]').click()
            assert scene.evaluate('node => node.offsetWidth') < 6000
            assert page.locator('[data-invitation-node][data-connected="true"]:visible').count() < 60
            page.locator('[data-invitation-fullscreen]').click()
            expect(page.locator('[data-invitation-dialog]')).to_be_visible()
            expect(page.locator('[data-invitation-dialog] [data-invitation-workspace]')).to_have_count(1)
            # 全屏复用相同工作区，成员网格和筛选也必须继续工作。
            _search_member(page, 'wxid_owner')
            page.locator('[data-invitation-detail-members]').click()
            page.locator('[data-invitation-member-filter]').fill('wxid_leaf_499')
            expect(page.locator('[data-invitation-member-grid] button')).to_have_count(1)
            page.locator('[data-invitation-member-close]').click()
            _assert_page_width(page)
            page.screenshot(path=str(directory / f'invitations-{width}-expanded.png'))
            page.keyboard.press('Escape')
            expect(page.locator('[data-invitation-dialog]')).to_be_hidden()
            expect(page.locator('[data-invitation-fullscreen]')).to_be_focused()
            page.locator('[data-invitation-collapse]').click()
            assert page.locator('[data-invitation-node][data-connected="true"]:visible').count() == 1
            # 未关联成员仍可搜索和查看，并且不会被错误连接到群主。
            _search_member(page, 'wxid_silent')
            expect(page.locator('[data-invitation-detail-id]')).to_have_text('wxid_silent')
            expect(page.locator('[data-invitation-detail-inviter]')).to_have_text('未知')
            _assert_page_width(page)
            assert not errors
    finally:
        context.close()
    with ids['factory']() as db:
        after = dict(db.execute(select(ChatroomMember.member_external_id, ChatroomMember.extra_json)
            .where(ChatroomMember.chatroom_id == room_id)).all())
        assert after == before
        assert db.scalars(select(TaskJob)).all() == []


@pytest.mark.parametrize('kind', ['small', 'empty', 'cycle'])
def test_small_empty_and_cyclic_groups_keep_exact_relationships(profile_page_context, browser, kind):
    """少量关系直接展示；空图可查资料；循环断边只影响画布，不影响直属成员计数。"""
    client, ids, channel_id, room_id = profile_page_context
    if kind != 'empty':
        with ids['factory']() as db:
            members = {member.member_external_id: member for member in db.scalars(
                select(ChatroomMember).where(ChatroomMember.chatroom_id == room_id))}
            members['wxid_recent'].extra_json = _invitation('wxid_owner')
            if kind == 'cycle':
                members['wxid_owner'].extra_json = _invitation('wxid_recent')
            db.commit()
    _login(client, 'readonly')
    context = browser.new_context(viewport={'width': 1024, 'height': 900}, reduced_motion='reduce')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        with _application_server(client) as base:
            page.goto(f'{base}/channels/{channel_id}/chatrooms/{room_id}?tab=members')
            expect(page.locator('[data-invitation-search]')).to_be_enabled()
            expect(page.locator('[data-invitation-summary]')).to_have_count(0)
            if kind == 'empty':
                expect(page.locator('[data-invitation-scene]')).to_have_count(0)
                _search_member(page, 'wxid_silent')
                expect(page.locator('[data-invitation-detail-id]')).to_have_text('wxid_silent')
                expect(page.locator('[data-invitation-focus]')).to_be_disabled()
                expect(page.locator('[data-invitation-detail-members]')).to_be_disabled()
            else:
                expect(page.locator('[data-invitation-node][data-connected="true"]:visible')).to_have_count(2)
                _search_member(page, 'wxid_owner')
                page.locator('[data-invitation-detail-members]').click()
                expect(page.locator('[data-invitation-member-grid] button')).to_have_count(1)
                expect(page.locator('[data-invitation-member-grid]')).to_contain_text('wxid_recent')
                if kind == 'cycle':
                    page.locator('[data-invitation-member-close]').click()
                    _search_member(page, 'wxid_recent')
                    page.locator('[data-invitation-detail-members]').click()
                    expect(page.locator('[data-invitation-member-grid] button')).to_have_count(1)
                    expect(page.locator('[data-invitation-member-grid]')).to_contain_text('wxid_owner')
            _assert_page_width(page)
            assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_hundreds_of_leaf_members_keep_one_summary_and_precise_selection(large_invitation_group, browser, width):
    """复现同一人邀请几百位普通成员，所有下级均无后续邀请的场景。"""
    client, ids, channel_id, room_id = large_invitation_group
    with ids['factory']() as db:
        grandchildren = db.scalars(select(ChatroomMember).where(
            ChatroomMember.chatroom_id == room_id,
            ChatroomMember.member_external_id.startswith('wxid_grandchild_'))).all()
        for member in grandchildren:
            db.delete(member)
        db.commit()
    _login(client, 'readonly')
    context = browser.new_context(viewport={'width': width, 'height': 900 if width > 500 else 844},
        reduced_motion='reduce')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        with _application_server(client) as base:
            page.goto(f'{base}/channels/{channel_id}/chatrooms/{room_id}?tab=members')
            expect(page.locator('[data-invitation-search]')).to_be_enabled()
            summary = page.locator('.invitation-group-summary:visible')
            expect(summary).to_have_count(1)
            expect(summary).to_contain_text('500 位直属成员')
            assert page.locator('[data-invitation-node][data-connected="true"]:visible').count() == 1
            summary.locator('button').click()
            members = page.locator('[data-invitation-member-browser]')
            expect(members).to_be_visible()
            members.locator('[data-invitation-member-filter]').fill('wxid_leaf_297')
            result = members.locator('[data-invitation-member-grid] button')
            expect(result).to_have_count(1)
            # 长昵称包含的 HTML 必须始终是文字，而非变成脚本或打断卡片。
            expect(result).to_contain_text('<script>成员文字</script>')
            result.click()
            expect(page.locator('[data-invitation-detail-id]')).to_have_text('wxid_leaf_297')
            expect(page.locator('[data-invitation-detail-count]')).to_have_text('0 人')
            expect(page.locator('[data-invitation-detail-members]')).to_be_disabled()
            page.locator('[data-invitation-focus]').click()
            expect(page.locator('[data-invitation-node][data-external-id="wxid_leaf_297"]')).to_be_visible()
            assert page.locator('[data-invitation-node][data-connected="true"]:visible').count() == 2
            page.locator('[data-invitation-overview]').click()
            expect(summary).to_have_count(1)
            _assert_page_width(page)
            assert not errors
    finally:
        context.close()
