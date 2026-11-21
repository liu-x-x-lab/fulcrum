"""能力选择页面的认证、CSRF、独立供应商与租户边界。"""

import pytest
from sqlalchemy import select

from app.models.entities import AiProvider, TenantAiSetting
from app.services import ai_memory, ai_vision
from app.services.security import encrypt_secret
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


@pytest.fixture
def vision_page(provider_page_context, monkeypatch):
    monkeypatch.setattr(ai_memory, "list_banks", lambda tenant_id: None)
    client, ids = provider_page_context
    with ids["factory"]() as db:
        for key, tenant, secret in [("chat", ids["alpha"], "chat-secret"), ("vision", ids["alpha"], "visual-secret"), ("foreign", ids["beta"], "foreign-secret")]:
            provider = AiProvider(tenant_id=tenant, name=key, api_base=f"https://{key}.example/v1", api_key_encrypted=encrypt_secret(secret))
            db.add(provider)
            db.flush()
            ids[key] = provider.id
        db.commit()
    return client, ids


def _form(token, ids, **overrides):
    return {
        "csrf_token": token, "enabled": "on", "chat_enabled": "on",
        "provider_id": str(ids["chat"]), "model": "chat",
        "vision_enabled": "on", "vision_provider_id": str(ids["vision"]), "vision_model": "vision", **overrides,
    }


def test_visual_form_selects_providers_and_never_renders_keys(vision_page):
    client, ids = vision_page
    token = _login(client, "admin")
    assert client.post("/tenant/ai", data=_form(token, ids)).status_code == 303
    page = client.get("/tenant/ai")
    assert page.status_code == 200
    assert 'name="vision_provider_id"' in page.text and 'value="test_vision"' in page.text
    assert 'name="api_key"' not in page.text and 'name="vision_api_base"' not in page.text
    assert "visual-secret" not in page.text and "chat-secret" not in page.text and "foreign" not in page.text
    assert "抽帧分析不含声音" in page.text
    with ids["factory"]() as db:
        stored = db.scalar(select(TenantAiSetting).where(TenantAiSetting.tenant_id == ids["alpha"]))
        assert stored.vision_provider_id == ids["vision"] and stored.provider_id == ids["chat"]
        assert db.scalar(select(TenantAiSetting).where(TenantAiSetting.tenant_id == ids["beta"])) is None
    assert client.post("/tenant/ai", data=_form(token, ids, vision_enabled="", vision_provider_id="", vision_model="")).status_code == 303
    with ids["factory"]() as db:
        stored = db.scalar(select(TenantAiSetting))
        assert stored.vision_provider_id is None and not stored.vision_enabled
        assert stored.provider_id == ids["chat"]


@pytest.mark.parametrize("role", ["readonly", "operator"])
def test_non_admin_cannot_configure_visual_model(vision_page, role):
    client, ids = vision_page
    token = _login(client, role)
    assert client.post("/tenant/ai", data=_form(token, ids)).status_code == 403


def test_visual_form_requires_csrf_and_preserves_nonsecret_invalid_input(vision_page):
    client, ids = vision_page
    token = _login(client, "admin")
    assert client.post("/tenant/ai", data=_form("wrong-token", ids)).status_code == 403
    response = client.post("/tenant/ai", data=_form(token, ids, vision_provider_id=str(ids["foreign"]), vision_model="my-visual-model"))
    assert response.status_code == 200
    assert "不属于当前租户" in response.text
    assert 'value="my-visual-model"' in response.text
    assert "chat-secret" not in response.text
    with ids["factory"]() as db:
        assert db.scalar(select(TenantAiSetting)) is None


def test_vision_test_action_uses_only_saved_visual_configuration(vision_page, monkeypatch):
    client, ids = vision_page
    token = _login(client, "admin")
    captured = []
    monkeypatch.setattr(ai_vision, "test_connection", lambda config: (captured.append(config) or True, "图片请求成功"))
    response = client.post("/tenant/ai", data=_form(token, ids, action="test_vision"))
    assert response.status_code == 303
    assert captured[0].api_base == "https://vision.example/v1" and captured[0].api_key == "visual-secret"


def test_drawing_is_opt_in_and_requires_a_separate_selection(vision_page):
    client, ids = vision_page
    token = _login(client, "admin")
    response = client.post("/tenant/ai", data=_form(token, ids, image_enabled="on", image_model="image-model"))
    assert response.status_code == 200 and "请选择供应商和模型" in response.text
    assert client.post("/tenant/ai", data=_form(token, ids, image_enabled="on", image_provider_id=str(ids["vision"]), image_model="image-model", image_daily_limit="3")).status_code == 303
    with ids["factory"]() as db:
        stored = db.scalar(select(TenantAiSetting))
        assert stored.image_provider_id == ids["vision"] and stored.image_daily_limit == 3
