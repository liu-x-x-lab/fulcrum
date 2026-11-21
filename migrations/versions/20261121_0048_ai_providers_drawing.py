"""Centralize tenant AI providers and add native drawing attempts."""

import json

from alembic import op
import sqlalchemy as sa

revision = "20261121_0048"
down_revision = "20261121_0047"
branch_labels = None
depends_on = None


def _json(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _provider(connection, providers, tenant_id, name, address, secret, model):
    """迁移只复制已有密文，不解密、不联网，不替用户授予新的付费能力。"""

    if not (address or secret):
        return None
    result = connection.execute(providers.insert().values(
        tenant_id=tenant_id, name=name, api_base=address or "", api_key_encrypted=secret,
        enabled=bool(address and secret), models_json="[]",
        manual_models_json=json.dumps([model] if model else []), revision=1,
    ))
    return result.inserted_primary_key[0]


def upgrade():
    op.create_table("ai_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("api_base", sa.String(255), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("models_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("manual_models_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("tenant_id", "name", name="uq_ai_providers_tenant_name"),
    )
    op.create_index("ix_ai_providers_tenant_id", "ai_providers", ["tenant_id"])
    with op.batch_alter_table("tenant_ai_settings") as batch:
        for name in ("provider_id", "vision_provider_id", "image_provider_id"):
            batch.add_column(sa.Column(name, sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_ai_settings_" + name, "ai_providers", [name], ["id"])
        batch.add_column(sa.Column("image_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("image_model", sa.String(100), nullable=False, server_default=""))
        batch.add_column(sa.Column("image_daily_limit", sa.Integer(), nullable=False, server_default="20"))
    connection = op.get_bind()
    metadata = sa.MetaData()
    providers = sa.Table("ai_providers", metadata, autoload_with=connection)
    settings = sa.Table("tenant_ai_settings", metadata, autoload_with=connection)
    rooms = sa.Table("chatrooms", metadata, autoload_with=connection)
    tenant_settings = {}
    for setting in connection.execute(sa.select(settings)).mappings():
        tenant_settings[setting["tenant_id"]] = dict(setting)
        text_id = _provider(connection, providers, setting["tenant_id"], "原对话供应商",
                            setting["api_base"], setting["api_key_encrypted"], setting["model"])
        vision_id = _provider(connection, providers, setting["tenant_id"], "原视觉供应商",
                              setting["vision_api_base"], setting["vision_api_key_encrypted"], setting["vision_model"])
        connection.execute(settings.update().where(settings.c.id == setting["id"]).values(provider_id=text_id, vision_provider_id=vision_id))
    for room in connection.execute(sa.select(rooms.c.id, rooms.c.tenant_id, rooms.c.extra_json)).mappings():
        extra = _json(room["extra_json"])
        features = extra.get("group_features")
        feature = features.get("ai_chat") if isinstance(features, dict) else None
        if not isinstance(feature, dict) or not isinstance(feature.get("config"), dict):
            continue
        config = feature["config"]
        base = config.pop("api_base", "")
        secret = config.pop("api_key", "")
        setting = tenant_settings.get(room["tenant_id"], {})
        if base or secret:
            provider_id = _provider(connection, providers, room["tenant_id"], f"原群供应商 {room['id']}",
                base or setting.get("api_base"), secret or setting.get("api_key_encrypted"),
                config.get("model") or setting.get("model"))
            config["provider_id"] = str(provider_id or "")
            config["model"] = config.get("model") or setting.get("model") or ""
        connection.execute(rooms.update().where(rooms.c.id == room["id"]).values(extra_json=json.dumps(extra, ensure_ascii=False)))
    with op.batch_alter_table("tenant_ai_settings") as batch:
        for name in ("api_base", "api_key_encrypted", "vision_api_base", "vision_api_key_encrypted"):
            batch.drop_column(name)
    op.create_table("ai_image_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "message_id", name="uq_ai_image_attempts_message"),
    )
    op.create_index("ix_ai_image_attempts_tenant_id", "ai_image_attempts", ["tenant_id"])
    op.create_index("ix_ai_image_attempts_tenant_created", "ai_image_attempts", ["tenant_id", "created_at"])


def downgrade():
    with op.batch_alter_table("tenant_ai_settings") as batch:
        batch.add_column(sa.Column("api_base", sa.String(255), nullable=False, server_default=""))
        batch.add_column(sa.Column("api_key_encrypted", sa.Text(), nullable=True))
        batch.add_column(sa.Column("vision_api_base", sa.String(255), nullable=False, server_default=""))
        batch.add_column(sa.Column("vision_api_key_encrypted", sa.Text(), nullable=True))
    connection = op.get_bind()
    metadata = sa.MetaData()
    providers = sa.Table("ai_providers", metadata, autoload_with=connection)
    settings = sa.Table("tenant_ai_settings", metadata, autoload_with=connection)
    rooms = sa.Table("chatrooms", metadata, autoload_with=connection)
    saved = {row["id"]: dict(row) for row in connection.execute(sa.select(providers)).mappings()}
    for setting in connection.execute(sa.select(settings)).mappings():
        values = {}
        for prefix in ("", "vision_"):
            provider = saved.get(setting[prefix + "provider_id"], {})
            if provider.get("tenant_id") == setting["tenant_id"]:
                values[prefix + "api_base"] = provider["api_base"]
                values[prefix + "api_key_encrypted"] = provider["api_key_encrypted"] if provider["enabled"] else None
        if values:
            connection.execute(settings.update().where(settings.c.id == setting["id"]).values(**values))
    for room in connection.execute(sa.select(rooms.c.id, rooms.c.tenant_id, rooms.c.extra_json)).mappings():
        extra = _json(room["extra_json"])
        features = extra.get("group_features")
        feature = features.get("ai_chat") if isinstance(features, dict) else None
        if not isinstance(feature, dict) or not isinstance(feature.get("config"), dict):
            continue
        config = feature["config"]
        provider_id = str(config.pop("provider_id", "") or "")
        provider = saved.get(int(provider_id), {}) if provider_id.isdigit() else {}
        if provider.get("tenant_id") == room["tenant_id"]:
            config["api_base"] = provider["api_base"]
            config["api_key"] = provider["api_key_encrypted"] or ""
            if not provider["enabled"]:
                feature["enabled"] = False
        config.pop("image_enabled", None)
        connection.execute(rooms.update().where(rooms.c.id == room["id"]).values(extra_json=json.dumps(extra, ensure_ascii=False)))
    op.drop_table("ai_image_attempts")
    with op.batch_alter_table("tenant_ai_settings") as batch:
        for name in ("provider_id", "vision_provider_id", "image_provider_id"):
            batch.drop_constraint("fk_ai_settings_" + name, type_="foreignkey")
            batch.drop_column(name)
        for name in ("image_enabled", "image_model", "image_daily_limit"):
            batch.drop_column(name)
    op.drop_table("ai_providers")
