"""供应商的加密、隔离、模型目录和完整连接选择，不访问真实接口。"""

import json

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import AiProvider
from app.models.saas import Tenant
from app.services import ai_dispatch, ai_drawing, ai_providers, ai_vision
from app.services.plugin_runtime.http_client import PluginHttpResponse
from app.services.security import decrypt_secret
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed


def add_provider(db, **overrides):
    return ai_providers.save_provider(db, **{
        "provider_id": None, "name": "Images", "api_base": "https://images.example/v1",
        "api_key": "image-only", "enabled": True, "manual_models": "image-model\nimage-model\nmanual-alias",
        **overrides,
    })


def test_credentials_are_central_encrypted_and_never_in_public_catalog():
    with _session() as db:
        _seed(db)
        provider = add_provider(db)
        assert decrypt_secret(provider.api_key_encrypted) == "image-only"
        assert "image-only" not in provider.api_key_encrypted
        catalog = ai_providers.public_catalog(db)
        assert "image-only" not in json.dumps(catalog) and "api_key" not in json.dumps(catalog)
        assert catalog[0]["models"] == ["image-model", "manual-alias"]
        config = ai_providers.snapshot(provider, 1, "manual-unlisted")
        assert config.model == "manual-unlisted" and "image-only" not in repr(config)
        assert ai_providers.snapshot(provider, 2, "image-model") is None


def test_blank_key_retains_but_changed_endpoint_requires_new_key():
    with _session() as db:
        _seed(db)
        provider = add_provider(db)
        add_provider(db, provider_id=provider.id, api_key="")
        assert decrypt_secret(provider.api_key_encrypted) == "image-only"
        with pytest.raises(ValueError, match="重新填写密钥"):
            add_provider(db, provider_id=provider.id, api_base="https://other.example/v1", api_key="")
        assert provider.api_base == "https://images.example/v1"
        add_provider(db, provider_id=provider.id, enabled=False, api_key="", clear_key=True)
        assert provider.api_key_encrypted is None
        assert ai_providers.snapshot(provider, 1, "image-model") is None


@pytest.mark.parametrize("address", ["file:///tmp/x", "https://user:secret@images.example/v1", "https://images.example/v1?key=x", "https://images.example/v1#x", "https://bad.example:bad", "https://bad.example/\npath", "https://@images.example/v1"])
def test_provider_rejects_credential_or_invalid_urls(address):
    with _session() as db:
        _seed(db)
        with pytest.raises(ValueError):
            add_provider(db, api_base=address)


@pytest.mark.parametrize("provider_id", [-1, 2**80])
def test_out_of_range_selection_is_rejected_before_query(provider_id):
    with _session() as db:
        _seed(db)
        with pytest.raises(ValueError, match="有效"):
            ai_providers.validate_selection(db, provider_id, "model")


def test_capabilities_select_independent_providers_without_secret_fallback():
    with _session() as db:
        _seed(db)
        provider = add_provider(db)
        setting = ai_dispatch.get_ai_setting(db)
        ai_dispatch.save_ai_setting(db, enabled=True, provider_id=setting.provider_id, model="chat",
            vision_enabled=True, vision_provider_id=provider.id, vision_model="vision-alias",
            image_enabled=True, image_provider_id=provider.id, image_model="image-model")
        assert ai_dispatch.snapshot_setting(setting).api_key == "k"
        assert ai_vision.snapshot_setting(setting).model == "vision-alias"
        assert ai_drawing.snapshot_setting(setting).api_key == "image-only"
        provider.enabled = False
        assert ai_drawing.snapshot_setting(setting) is None and ai_vision.snapshot_setting(setting) is None
        assert ai_dispatch.is_ai_ready(setting)
        with pytest.raises(ValueError, match="仍被"):
            ai_providers.delete_provider(db, provider)
        ai_dispatch.save_ai_setting(db, enabled=False, provider_id=setting.provider_id, model="chat",
            vision_enabled=False, vision_provider_id=provider.id, vision_model="vision-alias",
            image_enabled=False, image_provider_id=provider.id, image_model="image-model")


def test_foreign_provider_is_inaccessible_for_get_selection_and_refresh():
    with _session() as db:
        _seed(db)
        db.add(Tenant(id=2, code="foreign", name="Foreign"))
        provider = AiProvider(tenant_id=2, name="Private", api_base="https://foreign.example/v1", api_key_encrypted="cipher")
        db.add(provider)
        db.commit()
        provider_id = provider.id
        factory = sessionmaker(bind=db.bind)
        with tenant_scope(1), tenant_session(factory) as scoped:
            assert all(item.tenant_id == 1 for item in ai_providers.list_providers(scoped))
            with pytest.raises(ValueError):
                ai_providers.validate_selection(scoped, provider_id, "foreign")
            with pytest.raises(ValueError):
                ai_providers.save_models(scoped, provider_id, 1, ["stolen"])


def test_model_refresh_is_bounded_non_generating_and_no_redirects(monkeypatch):
    calls = []

    def request(_self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return PluginHttpResponse(200, {}, b'{"data":[{"id":"b"},{"id":"a"},{"id":"a"}]}', url)

    monkeypatch.setattr(ai_providers.PluginHttpClient, "request", request)
    assert ai_providers.fetch_models(ai_providers.ModelConfig("https://models.example/v1", "catalog", "key")) == ["a", "b"]
    method, url, options = calls[0]
    assert method == "GET" and url.endswith("/models")
    assert options["max_redirects"] == 0 and options["max_response_bytes"] == 2 * 1024 * 1024


@pytest.mark.parametrize("body", [{"data": []}, {"data": [None]}, {"data": [{"id": "has space"}]}, {"error": "secret upstream"}])
def test_invalid_model_catalog_is_safe_and_does_not_overwrite_manual_models(monkeypatch, body):
    monkeypatch.setattr(ai_providers.PluginHttpClient, "request", lambda *args, **kwargs: PluginHttpResponse(200, {}, json.dumps(body).encode(), "https://models.example"))
    with pytest.raises(ValueError, match="手动填写") as caught:
        ai_providers.fetch_models(ai_providers.ModelConfig("https://models.example/v1", "catalog", "key"))
    assert "secret upstream" not in str(caught.value)


def test_refresh_cannot_overwrite_connection_revision_and_keeps_manual_models():
    with _session() as db:
        _seed(db)
        provider = add_provider(db)
        old_revision = provider.revision
        add_provider(db, provider_id=provider.id, api_key="replacement")
        with pytest.raises(ValueError, match="已改变"):
            ai_providers.save_models(db, provider.id, old_revision, ["stale"])
        ai_providers.save_models(db, provider.id, provider.revision, ["fresh"])
        db.refresh(provider)
        assert ai_providers.available_models(provider) == ["fresh", "image-model", "manual-alias"]
