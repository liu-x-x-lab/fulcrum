"""模型选择器的键盘选择、手填保存和供应商页面窄屏布局。"""

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

expect = pytest.importorskip("playwright.sync_api").expect

from app.models.entities import AiProvider, Chatroom, TenantAiSetting
from app.services.group_features.ai_chat import get_override
from test_ai_vision_settings import vision_page, _form  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_select_layout import browser  # noqa: F401

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
        headers={name: value for name, value in route.request.headers.items() if name in {"content-type", "x-requested-with", "x-csrf-token"}},
        follow_redirects=True)
    route.fulfill(status=response.status_code, body=response.content,
        headers={name: value for name, value in response.headers.items() if name in {"content-type", "location"}})


def test_picker_keyboard_manual_alias_save_and_responsive_pages(vision_page, browser):
    client, ids = vision_page
    token = _login(client, "admin")
    client.post("/tenant/ai", data=_form(token, ids))
    with ids["factory"]() as db:
        provider = db.get(AiProvider, ids["vision"])
        provider.manual_models_json = '["vision-model","manual-image"]'
        provider.api_base = "https://example.test/" + "very-long-segment" * 12
        provider.name = "A long provider " * 5
        db.commit()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    context.route("http://fulcrum.test/**", lambda route: _serve(route, client))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto("http://fulcrum.test/tenant/ai")
        picker = page.locator("[data-ai-model-picker]").first
        model = picker.locator("[data-ai-model]")
        assert model.input_value() == "chat"
        trigger = picker.locator(".ui-select-trigger")
        trigger.focus()
        trigger.press("Enter")
        option = picker.get_by_role("option", name="A long provider", exact=False)
        option.click()
        expect(model).to_have_value("")
        assert picker.locator("datalist option").evaluate_all("nodes => nodes.map(node => node.value)") == ["manual-image", "vision-model"]
        model.fill("VISION")
        picker.get_by_role("option", name="vision-model", exact=True).click()
        expect(model).to_have_value("vision-model")
        model.fill("unlisted-alias")
        with page.expect_navigation():
            page.get_by_role("button", name="保存", exact=True).click()
        with ids["factory"]() as db:
            setting = db.scalar(select(TenantAiSetting))
            assert setting.provider_id == ids["vision"] and setting.model == "unlisted-alias"
        for path in ("/tenant/ai", "/tenant/ai-providers", f"/tenant/ai-providers/{ids['vision']}"):
            page.goto("http://fulcrum.test" + path)
            if path == "/tenant/ai-providers":
                page.locator("details.ai-provider-models summary").click()
            for width, height in ((1440, 900), (1024, 768), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"), (path, width)
        assert not errors
    finally:
        context.close()


@pytest.fixture
def picker_page(browser):
    """共享模板的轻量浏览器夹具，只使用合成目录和本地脚本，不连接供应商。"""

    pages = []

    def create(size=(1440, 900), models=None):
        catalog = [
            {"id": 1, "name": "Example provider", "enabled": True,
             "models": models if models is not None else [f"model-{index:03d}" for index in range(300)]},
            {"id": 2, "name": "Other provider", "enabled": True, "models": ["other-model"]},
            {"id": 3, "name": "Empty catalog", "enabled": True, "models": []},
        ]
        environment = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=select_autoescape())
        macro = environment.get_template("_ai_model_picker.html").module.model_picker
        fragment = macro(catalog, "provider_id", "model", 1, "model-299", "对话")
        fragment += macro(catalog, "vision_provider_id", "vision_model", 2, "other-model", "视觉")
        css = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")
        page = browser.new_page(viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce")
        pages.append(page)
        page.set_default_timeout(8000)
        page.set_content(f'<meta charset="utf-8"><style>{css}</style><main style="max-width:900px;margin:24px auto;padding:16px"><form class="form-grid">{fragment}<button type="reset">重置</button><button type="submit">保存</button></form></main>')
        for name in ("select.js", "config-suggestions.js", "ai-models.js"):
            page.add_script_tag(path=str(ROOT / "app/static/js" / name))
        page.locator("form").evaluate("form => form.addEventListener('submit', event => {event.preventDefault(); window.savedValues = Array.from(new FormData(form));})")
        return page

    yield create
    for page in pages:
        page.close()


@pytest.mark.parametrize("size", [(1440, 900), (1024, 768), (390, 844)])
def test_large_model_catalog_search_and_keyboard_selection(picker_page, size):
    page = picker_page(size)
    picker = page.locator("[data-ai-model-picker]").first
    model = page.get_by_role("combobox", name="对话模型", exact=True)
    panel = picker.locator(".config-suggestions-panel")
    model.click()
    assert model.get_attribute("list") is None
    assert panel.get_by_role("option").count() == 12
    assert panel.get_by_role("option").first.inner_text() == "model-299"
    assert "300" in panel.inner_text()
    box = panel.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= size[0] and box["height"] <= 280
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    position = page.evaluate("scrollY")
    model.fill("MoDeL 278")
    assert panel.get_by_role("option").count() == 1
    model.press("ArrowDown")
    model.press("Enter")
    expect(model).to_have_value("model-278")
    expect(panel).to_be_hidden()
    assert page.evaluate("scrollY") == position
    assert page.locator('[name="vision_model"]').input_value() == "other-model"
    page.get_by_role("button", name="保存", exact=True).click()
    assert page.evaluate("savedValues.filter(([name]) => name === 'model')") == [["model", "model-278"]]


def test_manual_models_provider_change_reset_and_empty_catalog(picker_page):
    page = picker_page()
    model = page.get_by_role("combobox", name="对话模型", exact=True)
    picker = page.locator("[data-ai-model-picker]").first
    panel = picker.locator(".config-suggestions-panel")
    model.fill("model-custom")
    assert panel.get_by_role("option").count() == 0
    assert "可直接使用" in panel.inner_text()
    model.press("Escape")
    expect(panel).to_be_hidden()
    expect(model).to_have_value("model-custom")
    model.fill("model-")
    model.press("Enter")
    expect(model).to_have_value("model-")
    assert page.evaluate("window.savedValues === undefined")
    model.press("Tab")
    expect(model).to_have_value("model-")
    picker.locator("[data-ai-provider]").select_option("2", force=True)
    expect(model).to_have_value("")
    model.click()
    expect(panel.get_by_role("option")).to_have_text(["other-model"])
    panel.get_by_role("option", name="other-model", exact=True).click()
    expect(model).to_have_value("other-model")
    picker.locator("[data-ai-provider]").select_option("3", force=True)
    model.fill("private-alias")
    assert panel.get_by_role("option").count() == 0
    page.get_by_role("button", name="保存", exact=True).click()
    assert page.evaluate("savedValues.filter(([name]) => name === 'model')") == [["model", "private-alias"]]
    page.get_by_role("button", name="重置", exact=True).click()
    expect(model).to_have_value("model-299")
    model.click()
    expect(panel.get_by_role("option").first).to_have_text("model-299")
    assert panel.get_by_role("option").count() == 12


def test_model_suggestions_escape_untrusted_ids_and_preserve_composition(picker_page):
    unusual = "<img/src=x/onerror=alert(1)>"
    long_model = "model-" + "x" * 94
    page = picker_page(size=(390, 844), models=[unusual, long_model])
    model = page.get_by_role("combobox", name="对话模型", exact=True)
    panel = page.locator("[data-ai-model-picker]").first.locator(".config-suggestions-panel")
    model.fill("onerror")
    panel.get_by_role("option", name=unusual, exact=True).click()
    assert model.input_value() == unusual and page.locator("img").count() == 0
    model.fill("model-")
    assert model.evaluate("input => {const event = new KeyboardEvent('keydown', {key:'Enter', isComposing:true, bubbles:true, cancelable:true}); input.dispatchEvent(event); return event.defaultPrevented;}") is False
    expect(panel).to_be_visible()
    panel.get_by_role("option", name=long_model, exact=True).click()
    expect(model).to_have_value(long_model)
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    model.evaluate("input => {input.readOnly = true}")
    model.click()
    expect(panel).to_be_hidden()


def test_group_picker_saves_reference_without_credentials(vision_page, browser):
    client, ids = vision_page
    token = _login(client, "admin")
    client.post("/tenant/ai", data=_form(token, ids))
    with ids["factory"]() as db:
        room = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"],
            name="Model selection test", external_id="model-test@chatroom", is_active=True)
        db.add(room)
        db.commit()
        room_id = room.id
    context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    context.route("http://fulcrum.test/**", lambda route: _serve(route, client))
    page = context.new_page()
    try:
        page.goto(f"http://fulcrum.test/channels/{ids['channel_id']}/chatrooms/{room_id}?tab=features")
        card = page.locator('[data-gf-key="ai_chat"]')
        card.locator("[data-gf-toggle]").click()
        card.locator("[data-ai-provider]").select_option(str(ids["vision"]), force=True)
        card.locator("[data-ai-model]").fill("group-alias")
        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/features/ai_chat")) as saved:
            card.locator('.gf-foot button[type="submit"]').click()
        assert saved.value.status == 200 and saved.value.json()["ok"]
        with ids["factory"]() as db:
            room = db.get(Chatroom, room_id)
            assert get_override(room).provider_id == ids["vision"] and get_override(room).model == "group-alias"
            assert "api_key" not in room.extra_json and "visual-secret" not in room.extra_json
        for width, height in ((1440, 900), (1024, 768), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
        page.reload()
        expect(card.locator("[data-ai-model]")).to_have_value("group-alias")
    finally:
        context.close()
