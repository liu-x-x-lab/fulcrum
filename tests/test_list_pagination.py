"""用真实页面验证双向分页、跳页和列表到详情的往返。"""

from html.parser import HTMLParser
import os
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from starlette.requests import Request

from app.models.entities import Channel, Chatroom, Conversation, PointsAccount, WorkWechatContact
from app.web.shared.urls import preserve_return_to, safe_return_to
from test_chatroom_plugin_batch import batch_context  # noqa: F401
from test_friend_check_routes import check_context  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.rows = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.rows.append((tag, dict(attrs)))

    def find(self, tag, **attrs):
        return [row for name, row in self.rows if name == tag and all(row.get(k) == v for k, v in attrs.items())]


@pytest.fixture()
def paged_lists(provider_page_context):
    client, ids = provider_page_context
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_webridge"
        for number in range(155):
            room = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id,
                external_id=f"paging-{number}@chatroom", name=f"分页群 {number:03d}", management_status="enabled", is_active=True)
            contact = WorkWechatContact(tenant_id=ids["alpha"], channel_id=channel.id,
                contact_external_id=f"paging-friend-{number}", contact_kind="friend", display_name=f"分页好友 {number:03d}", sync_status="active")
            db.add_all([room, contact,
                PointsAccount(tenant_id=ids["alpha"], group_id="paging-group", user_id=f"paging-friend-{number}", user_name=f"分页好友 {number:03d}", total_points=10),
                Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id, channel_type=channel.channel_type,
                    external_id=f"paging-friend-{number}", title=f"分页好友 {number:03d}", chat_type="direct")])
        db.commit()
    return client, ids


@pytest.mark.parametrize("path, keyword", [("/chatrooms", "分页群"), ("/contacts", "分页好友"), ("/points", "分页好友")])
def test_pager_keeps_filters_and_handles_boundaries(paged_lists, path, keyword):
    client, ids = paged_lists
    _login(client, "operator")
    query = {"q": keyword, "page": 5}
    if path != "/points":
        query["channel_id"] = ids["channel_id"]
    response = client.get(path, params={**query, "notice": "操作成功", "selection_cleared": "1"})
    assert response.status_code == 200
    elements = Elements(response.text)
    for target in (4, 6):
        link = next(row for row in elements.find("a") if parse_qs(urlsplit(row.get("href", "")).query).get("page") == [str(target)])
        params = parse_qs(urlsplit(link["href"]).query)
        assert not urlsplit(link["href"]).netloc
        assert "notice" not in params and "selection_cleared" not in params
        assert params["q"] == [keyword]
        if path != "/points":
            assert params["channel_id"] == [str(ids["channel_id"])]
    jump = elements.find("input", name="page")[0]
    assert (jump["value"], jump["min"], jump["max"]) == ("5", "1", "6")
    assert elements.find("input", type="hidden", name="q")[0]["value"] == keyword
    assert not elements.find("input", type="hidden", name="selection_cleared")
    for requested, expected in ((1, "1"), (999, "6"), (-1, "1")):
        page = Elements(client.get(path, params={**query, "page": requested}).text)
        assert page.find("input", name="page")[0]["value"] == expected
        disabled = [row for row in page.find("button", type="button") if "disabled" in row]
        assert disabled


def test_group_detail_and_tabs_return_to_fifth_filtered_page(paged_lists):
    client, ids = paged_lists
    _login(client, "operator")
    source = "/chatrooms?" + urlencode({"q": "分页群", "channel_id": ids["channel_id"], "status": "enabled", "page": 5})
    elements = Elements(client.get(source).text)
    detail_href = next(row["href"] for row in elements.find("a") if "/chatrooms/" in row.get("href", "") and "return_to=" in row["href"])
    assert parse_qs(urlsplit(detail_href).query)["return_to"] == [source]
    for tab in ("features", "members", "profile", "plugins"):
        response = client.get(detail_href + "&tab=" + tab)
        assert response.status_code == 200
        page = Elements(response.text)
        assert page.find("a", **{"class": "page-back"})[0]["href"] == source
        tabs = [row for row in page.find("a") if "?tab=" in row.get("href", "")]
        assert len(tabs) >= 4
        assert all(parse_qs(urlsplit(row["href"]).query)["return_to"] == [source] for row in tabs)


def test_friend_chat_and_points_ledger_keep_origin(paged_lists, monkeypatch):
    import app.web.contact_check as check_web

    client, ids = paged_lists
    monkeypatch.setattr(check_web, "TenantSessionLocal", ids["factory"])
    csrf = _login(client, "operator")
    source = "/contacts?" + urlencode({"q": "分页好友", "channel_id": ids["channel_id"], "page": 5})
    elements = Elements(client.get(source).text)
    check_href = next(row["href"] for row in elements.find("a") if row.get("href", "").startswith("/contacts/check?"))
    check_page = client.get(check_href)
    assert check_page.status_code == 200
    assert Elements(check_page.text).find("a", **{"class": "page-back"})[0]["href"] == source
    chat_href = next(row["href"] for row in elements.find("a") if row.get("href", "").startswith("/messages?"))
    response = client.get(chat_href)
    assert response.status_code == 200
    assert Elements(response.text).find("a", **{"class": "page-back"})[0]["href"] == source
    unfiltered = Elements(client.get("/messages", params={"return_to": source}).text)
    kind_links = [row for row in unfiltered.find("a") if row.get("class", "").startswith("msg-kind-tab")]
    assert len(kind_links) == 4
    assert all(parse_qs(urlsplit(row["href"]).query)["return_to"] == [source] for row in kind_links)
    with ids["factory"]() as db:
        contact_id = db.query(WorkWechatContact.id).filter(WorkWechatContact.contact_external_id == "paging-friend-120").scalar()
    started = client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": csrf, "return_to": source})
    assert started.status_code == 303
    assert parse_qs(urlsplit(started.headers["location"]).query)["return_to"] == [source]

    source = "/points?" + urlencode({"q": "分页好友", "page": 5})
    elements = Elements(client.get(source).text)
    assert elements.find("input", name="back")[0]["value"] == source
    href = next(row["href"] for row in elements.find("a") if row.get("href", "").startswith("/points/ledger?"))
    ledger = client.get(href)
    assert ledger.status_code == 200
    page = Elements(ledger.text)
    assert page.find("a", **{"class": "page-back"})[0]["href"] == source
    assert page.find("input", name="return_to")[0]["value"] == source
    changed = client.post("/points/adjust", data={"csrf_token": csrf, "back": source, "group_id": "paging-group",
        "user_id": "paging-friend-120", "amount": 1, "mode": "grant", "form_token": "pagination-regression"})
    assert changed.status_code == 303
    assert parse_qs(urlsplit(changed.headers["location"]).query)["page"] == ["5"]


@pytest.mark.parametrize("value", ["https://example.com", "//example.com", "/\\example.com", "http://[", "/contacts\nunsafe"])
def test_return_links_reject_external_or_malformed_urls(value):
    assert safe_return_to(value, "/chatrooms") == "/chatrooms"


def test_plugin_form_save_and_reset_keep_list_origin_without_referer(batch_context):
    client, ids = batch_context
    csrf = _login(client, "operator")
    source = "/chatrooms/plugin-scopes?" + urlencode({"q": "批量", "plugin_id": ids["plugin"], "page": 5})
    detail = f"/channels/{ids['channel_id']}/chatrooms/{ids['rooms'][0]}"
    query = urlencode({"tab": "plugins", "return_to": source})
    page = Elements(client.get(detail + "?" + query).text)
    reset = page.find("form", id=f"reset-group-plugin-{ids['bindings'][0]}")[0]["action"]
    assert parse_qs(urlsplit(reset).query)["return_to"] == [source]
    for mode in ("enabled", "inherited"):
        saved = client.post(reset, data={"csrf_token": csrf, "mode": mode})
        assert saved.status_code == 303
        params = parse_qs(urlsplit(saved.headers["location"]).query)
        assert params["return_to"] == [source] and params["tab"] == ["plugins"]
        result = client.get(saved.headers["location"])
        assert result.status_code == 200
        assert Elements(result.text).find("a", **{"class": "page-back"})[0]["href"] == source


def test_detail_form_redirect_keeps_only_same_origin_return_link():
    source = "/chatrooms?q=分页&page=5"
    target = "/channels/1/chatrooms/2?tab=plugins&notice=ok"
    referer = "http://testserver/channels/1/chatrooms/2?" + urlencode({"return_to": source})
    def request(origin):
        return Request({"type": "http", "method": "POST", "scheme": "http", "server": ("testserver", 80),
            "path": "/channels/1/chatrooms/2/scopes/3/mode", "query_string": b"", "headers": [(b"host", b"testserver"), (b"referer", origin.encode("ascii"))]})
    result = preserve_return_to(request(referer), target)
    assert parse_qs(urlsplit(result).query)["return_to"] == [source]
    assert preserve_return_to(request(referer.replace("http://testserver", "https://example.com")), target) == target


def test_browser_jump_roundtrip_and_mobile_layout(paged_lists, browser):
    from playwright.sync_api import expect
    from test_chatroom_plugin_browser import _serve_application

    client, ids = paged_lists
    _login(client, "operator")
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    context.route("http://fulcrum.test/**", lambda route: _serve_application(route, client))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto("http://fulcrum.test/chatrooms?" + urlencode({"q": "分页群", "channel_id": ids["channel_id"]}))
        expect(page.get_by_role("button", name="上一页", exact=True)).to_be_disabled()
        page.get_by_role("spinbutton", name="页码").fill("5")
        page.get_by_role("button", name="跳转", exact=True).click()
        expect(page.get_by_role("spinbutton", name="页码")).to_have_value("5")
        assert parse_qs(urlsplit(page.url).query)["q"] == ["分页群"]
        page.get_by_role("link", name="设置", exact=True).first.click()
        page.get_by_role("link", name="资料", exact=True).click()
        page.get_by_role("link", name="返回群管理", exact=True).click()
        expect(page.get_by_role("spinbutton", name="页码")).to_have_value("5")
        page.get_by_role("link", name="下一页", exact=True).click()
        expect(page.get_by_role("button", name="下一页", exact=True)).to_be_disabled()
        page.get_by_role("link", name="上一页", exact=True).click()
        page.set_viewport_size({"width": 390, "height": 844})
        pager = page.get_by_role("navigation", name="列表分页", exact=True)
        pager.scroll_into_view_if_needed()
        assert pager.evaluate("el => [...el.querySelectorAll('a, button, input:not([type=hidden])')].every(node => {const r = node.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth;})")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            directory = Path(os.environ["QA_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / "pagination-mobile.png"), full_page=True)
        assert not errors
    finally:
        context.close()


def test_friend_check_jump_retains_page_on_reload_and_fits_mobile(check_context, browser):
    from playwright.sync_api import expect
    from test_chatroom_plugin_browser import _serve_application

    client, _ids, _csrf, channel_id, *_rest = check_context
    _login(client, "readonly")
    contacts = [{"id": number, "name": f"分页好友 {number:03d}", "external_id": f"paging-{number}"} for number in range(1, 126)]
    saved = {"run_id": 1, "generation": "pagination-browser", "cursor": 125, "total": 125,
        "status": "completed", "interval_seconds": 1, "elapsed_ms": 0, "contacts": contacts,
        "results": [{"category": "friend", "contact": contact, "reason": "检测完成"} for contact in contacts], "pending": None}
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    context.route("http://fulcrum.test/**", lambda route: _serve_application(route, client))
    context.route(f"http://fulcrum.test/contacts/check/{channel_id}/state", lambda route: route.fulfill(json=saved))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://fulcrum.test/contacts/check?channel_id={channel_id}")
        expect(page.locator('[data-result-count="friend"]')).to_have_text("125")
        field = page.locator('[data-page-number="friend"]')
        field.fill("3")
        field.press("Enter")
        expect(page.locator('[data-page-label="friend"]')).to_have_text("3 / 3")
        expect(page.locator('[data-page-next="friend"]')).to_be_disabled()
        page.locator('[data-page-prev="friend"]').click()
        expect(page.locator('[data-page-label="friend"]')).to_have_text("2 / 3")
        page.locator('[data-page-size="friend"]').select_option("25")
        field.fill("5")
        field.press("Enter")
        page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(page.get_by_role("status")).to_contain_text("检测完成")
        expect(page.locator('[data-page-label="friend"]')).to_have_text("5 / 5")
        page.set_viewport_size({"width": 390, "height": 844})
        pager = page.locator('[data-pagination="friend"]')
        pager.scroll_into_view_if_needed()
        assert pager.evaluate("el => [...el.querySelectorAll('button, input, .ui-select-trigger')].every(node => {const r = node.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth;})")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            pager.screenshot(path=str(Path(os.environ["QA_SCREENSHOT_DIR"]) / "friend-check-pager-mobile.png"))
        assert not errors
    finally:
        context.close()


from test_select_layout import browser  # noqa: E402,F401
