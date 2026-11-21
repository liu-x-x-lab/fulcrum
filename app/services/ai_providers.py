"""租户 AI 连接、模型目录和凭据边界；不从消息或能力配置借用密钥。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.models.entities import AiProvider, Chatroom, TenantAiSetting
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.security import decrypt_secret, encrypt_secret

MAX_MODELS = 500


@dataclass(frozen=True)
class ModelConfig:
    api_base: str
    model: str
    api_key: str = field(repr=False)


def model_name(value: str) -> str:
    name = str(value or "").strip()
    if len(name) > 100 or any(character.isspace() or not character.isprintable() for character in name):
        raise ValueError("模型名称不能超过 100 个字符，且不能包含空白或控制字符")
    return name


def _model_list(value) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_MODELS:
        raise ValueError("模型列表最多允许 500 项")
    return sorted({model_name(item) for item in value if isinstance(item, str) and item.strip()})


def stored_models(value: str) -> list[str]:
    try:
        return _model_list(json.loads(value or "[]"))
    except (ValueError, TypeError):
        return []


def available_models(provider: AiProvider) -> list[str]:
    return sorted(set(stored_models(provider.models_json) + stored_models(provider.manual_models_json)))


def list_providers(db: Session) -> list[AiProvider]:
    return list(db.scalars(select(AiProvider).order_by(AiProvider.name, AiProvider.id)))


def get_provider(db: Session, provider_id: int) -> AiProvider:
    if not isinstance(provider_id, int) or not 0 < provider_id <= 2_147_483_647:
        raise ValueError("请选择有效的 AI 供应商")
    provider = db.get(AiProvider, provider_id)
    if provider is None:
        raise ValueError("AI 供应商不存在或不属于当前租户")
    return provider


def ready(provider: AiProvider | None, tenant_id: int) -> bool:
    return bool(provider and provider.tenant_id == tenant_id and provider.enabled and provider.api_base and provider.api_key_encrypted)


def snapshot(provider: AiProvider | None, tenant_id: int, model: str) -> ModelConfig | None:
    """在会话内取纯数据，慢请求只使用副本；外租户关系和停用连接均不降级。"""

    if not ready(provider, tenant_id) or not model:
        return None
    try:
        name = model_name(model)
    except ValueError:
        return None
    secret = decrypt_secret(provider.api_key_encrypted) or ""
    return ModelConfig(provider.api_base, name, secret) if secret else None


def validate_selection(db: Session, provider_id, model: str, *, required: bool = False) -> tuple[int | None, str]:
    try:
        selected = int(provider_id) if provider_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise ValueError("请选择有效的 AI 供应商") from None
    name = model_name(model)
    if selected is not None:
        provider = get_provider(db, selected)
        if required and not ready(provider, provider.tenant_id):
            raise ValueError("所选 AI 供应商已停用或尚未配置密钥")
        if not name:
            raise ValueError("选择供应商后请同时选择或填写模型")
    if required and not (selected and name):
        raise ValueError("启用该能力前请选择供应商和模型")
    return selected, name


def save_provider(db: Session, *, provider_id: int | None, name: str, api_base: str, api_key: str,
                  enabled: bool, manual_models: str, clear_key: bool = False) -> AiProvider:
    """改地址不沿用旧密钥；先完成所有验证，错误不会修改当前对象。"""

    provider = get_provider(db, provider_id) if provider_id is not None else None
    title, address, secret = name.strip(), api_base.strip().rstrip("/"), api_key.strip()
    if not title or len(title) > 100:
        raise ValueError("供应商名称必填且不能超过 100 个字符")
    try:
        parsed = urlsplit(address)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment
        parsed.port
    except ValueError:
        valid = False
    if not valid or len(address) > 255 or any(character.isspace() or not character.isprintable() for character in address):
        raise ValueError("接口地址必须是无账号、查询参数的 HTTP(S) API 地址")
    if clear_key and secret:
        raise ValueError("请勿同时填写并清除密钥")
    encrypted = provider.api_key_encrypted if provider and provider.api_base == address and not clear_key else None
    if secret:
        encrypted = encrypt_secret(secret)
    if enabled and not encrypted:
        raise ValueError("启用供应商前请填写密钥；更换接口地址需重新填写密钥")
    models = _model_list(manual_models.splitlines())
    duplicate = db.scalar(select(AiProvider.id).where(AiProvider.name == title, AiProvider.id != (provider_id or 0)))
    if duplicate:
        raise ValueError("供应商名称已存在")
    if provider is None:
        provider = AiProvider()
        db.add(provider)
    if provider.api_base != address or secret or clear_key:
        provider.models_json = "[]"
    provider.name, provider.api_base, provider.api_key_encrypted = title, address, encrypted
    provider.enabled = bool(enabled)
    provider.manual_models_json = json.dumps(models, ensure_ascii=False)
    provider.revision = AiProvider.revision + 1 if provider.id else 1
    db.flush()
    return provider


def fetch_models(config: ModelConfig) -> list[str]:
    """仅请求配置地址的模型目录；无生成费用，不跟随携带密钥的重定向。"""

    try:
        response = PluginHttpClient().request("GET", config.api_base + "/models",
            headers={"Authorization": f"Bearer {config.api_key}", "Accept": "application/json"},
            connect_timeout_seconds=15, total_timeout_seconds=15, max_response_bytes=2 * 1024 * 1024, max_redirects=0)
        if not 200 <= response.status_code < 300:
            raise ValueError
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise ValueError
        # 大目录只保留前 500 个，其他模型仍可手填；不相信目录声称的视觉/绘图能力。
        models = _model_list([item.get("id") for item in data[:MAX_MODELS] if isinstance(item, dict)])
        if not models:
            raise ValueError
        return models
    except (PluginHttpError, ValueError, TypeError):
        raise ValueError("模型目录读取失败；请检查地址和密钥，也可以手动填写模型名称") from None


def save_models(db: Session, provider_id: int, revision: int, models: list[str]) -> None:
    """条件更新阻止慢刷新覆盖已经修改的连接，校验与写入不可分成两步。"""

    result = db.execute(update(AiProvider).where(AiProvider.id == provider_id, AiProvider.revision == revision)
        .values(models_json=json.dumps(_model_list(models), ensure_ascii=False), revision=revision + 1))
    if result.rowcount != 1:
        raise ValueError("供应商配置已改变，请重新刷新模型")


def delete_provider(db: Session, provider: AiProvider) -> None:
    """正在使用的供应商不能删除；停用不会自动切到别人的连接。"""

    used = db.scalar(select(TenantAiSetting.id).where(or_(
        TenantAiSetting.provider_id == provider.id, TenantAiSetting.vision_provider_id == provider.id,
        TenantAiSetting.image_provider_id == provider.id,
    )).limit(1))
    for room in db.scalars(select(Chatroom)):
        try:
            config = json.loads(room.extra_json or "{}").get("group_features", {}).get("ai_chat", {}).get("config", {})
            used = used or str(config.get("provider_id") or "") == str(provider.id)
        except (ValueError, AttributeError, TypeError):
            continue
    if used:
        raise ValueError("供应商仍被 AI 助手或群使用，请先更换对应选择")
    db.delete(provider)
    db.flush()


def public_catalog(db: Session) -> list[dict]:
    """浏览器只获得选择所需字段，绝不序列化 ORM 对象或加密密钥。"""

    return [{"id": provider.id, "name": provider.name, "enabled": provider.enabled,
             "models": available_models(provider)} for provider in list_providers(db)]
