"""AI 供应商 CRUD 通过真实登录与 CSRF，不接触实际供应商。"""

from sqlalchemy import select
import pytest

from app.models.entities import AiProvider
from app.services import ai_providers
from app.services.security import decrypt_secret
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


def _form(token, **overrides):
    return {"csrf_token": token, "name": "Example provider", "api_base": "https://models.example/v1",
        "api_key": "secret-example", "enabled": "on", "manual_models": "manual-one\nmanual-two", **overrides}


def test_provider_create_refresh_edit_and_delete(provider_page_context, monkeypatch):
    client, ids = provider_page_context
    token = _login(client, "admin")
    assert client.post("/tenant/ai-providers/save", data=_form(token)).status_code == 303
    with ids["factory"]() as db:
        provider = db.scalar(select(AiProvider))
        provider_id = provider.id
        assert provider.tenant_id == ids["alpha"] and decrypt_secret(provider.api_key_encrypted) == "secret-example"
    page = client.get(f"/tenant/ai-providers/{provider_id}")
    assert page.status_code == 200 and "secret-example" not in page.text
    assert "已配置，留空保持" in page.text
    calls = []
    monkeypatch.setattr(ai_providers, "fetch_models", lambda config: (calls.append(config) or ["fetched-model"]))
    assert client.post(f"/tenant/ai-providers/{provider_id}/refresh", data={"csrf_token": token}).status_code == 303
    assert len(calls) == 1 and calls[0].api_key == "secret-example"
    page = client.get("/tenant/ai-providers")
    assert "fetched-model" in page.text and "manual-one" in page.text and "secret-example" not in page.text
    assert client.post("/tenant/ai-providers/save", data=_form(token, provider_id=str(provider_id), name="Renamed", api_key="")).status_code == 303
    with ids["factory"]() as db:
        assert decrypt_secret(db.get(AiProvider, provider_id).api_key_encrypted) == "secret-example"
    assert client.post(f"/tenant/ai-providers/{provider_id}/delete", data={"csrf_token": token}).status_code == 303
    with ids["factory"]() as db:
        assert db.get(AiProvider, provider_id) is None


@pytest.mark.parametrize("role", ["operator", "readonly"])
def test_provider_mutations_require_admin(provider_page_context, role):
    client, _ids = provider_page_context
    token = _login(client, role)
    assert client.get("/tenant/ai-providers").status_code == 200
    assert client.get("/tenant/ai-providers/new").status_code == 403
    assert client.post("/tenant/ai-providers/save", data=_form(token)).status_code == 403
    for action in ("refresh", "delete"):
        assert client.post(f"/tenant/ai-providers/1/{action}", data={"csrf_token": token}).status_code == 403


def test_csrf_cross_tenant_and_invalid_input_never_expose_keys(provider_page_context):
    client, ids = provider_page_context
    token = _login(client, "admin")
    with ids["factory"]() as db:
        provider = AiProvider(tenant_id=ids["beta"], name="Foreign-private", api_base="https://foreign.example/v1", api_key_encrypted="hidden-ciphertext")
        db.add(provider)
        db.commit()
        foreign_id = provider.id
    assert client.post("/tenant/ai-providers/save", data=_form("bad-csrf")).status_code == 403
    for action in ("refresh", "delete"):
        assert client.post(f"/tenant/ai-providers/{foreign_id}/{action}", data={"csrf_token": "bad-csrf"}).status_code == 403
        result = client.post(f"/tenant/ai-providers/{foreign_id}/{action}", data={"csrf_token": token})
        assert result.status_code == 303 and "danger" in result.headers["location"]
    assert "Foreign-private" not in client.get("/tenant/ai-providers").text
    result = client.post("/tenant/ai-providers/save", data=_form(token, provider_id=str(foreign_id)))
    assert result.status_code == 200 and "不属于当前租户" in result.text
    result = client.post("/tenant/ai-providers/save", data=_form(token, name="Kept name", api_base="invalid"))
    assert result.status_code == 200 and 'value="Kept name"' in result.text and "secret-example" not in result.text


def test_stale_refresh_rejects_result_after_connection_change(provider_page_context, monkeypatch):
    client, ids = provider_page_context
    token = _login(client, "admin")
    client.post("/tenant/ai-providers/save", data=_form(token))
    with ids["factory"]() as db:
        provider_id = db.scalar(select(AiProvider.id))

    def refresh(_config):
        with ids["factory"]() as db:
            provider = db.get(AiProvider, provider_id)
            provider.revision += 1
            provider.models_json = '["newer"]'
            db.commit()
        return ["stale"]

    monkeypatch.setattr(ai_providers, "fetch_models", refresh)
    response = client.post(f"/tenant/ai-providers/{provider_id}/refresh", data={"csrf_token": token})
    assert response.status_code == 303 and "danger" in response.headers["location"]
    with ids["factory"]() as db:
        assert db.get(AiProvider, provider_id).models_json == '["newer"]'
