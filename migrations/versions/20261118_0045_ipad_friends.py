"""Store account-scoped iPad friends and import existing contact snapshots."""

from datetime import datetime
import json

from alembic import op
import sqlalchemy as sa

revision = "20261118_0045"
down_revision = "20261118_0044"
branch_labels = None
depends_on = None


def _value(item, key, limit=None):
    value = item.get(key)
    if not isinstance(value, str) or value.strip() in {"", "-"}:
        return None
    return value.strip()[:limit]


def upgrade():
    friends = op.create_table(
        "ipad_friends",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("channel_id", sa.Integer, sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_wxid", sa.String(128), nullable=False),
        sa.Column("contact_external_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("remark", sa.String(255)),
        sa.Column("alias", sa.String(128)),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("avatar_url", sa.Text),
        sa.Column("profile_json", sa.Text),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "channel_id", "owner_wxid", "contact_external_id",
                            name="uq_ipad_friend_identity"),
    )
    op.create_index("ix_ipad_friends_tenant_id", "ipad_friends", ["tenant_id"])
    db = op.get_bind()
    channels = sa.table("channels", *(sa.column(k) for k in
                        ("id", "tenant_id", "channel_type", "runtime_info_json")))
    for channel in db.execute(sa.select(channels).where(channels.c.channel_type == "wechat_ipad")).mappings():
        try:
            runtime = json.loads(channel["runtime_info_json"] or "{}")
        except ValueError:
            continue
        snapshot = runtime.get("last_contact_sync_snapshot") if isinstance(runtime, dict) else None
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("contacts"), list):
            continue
        owner = _value(snapshot, "wxid")
        if not owner or len(owner) > 128:
            continue
        try:
            synced_at = datetime.fromisoformat(snapshot.get("synced_at") or "")
        except (TypeError, ValueError):
            synced_at = None
        rows = {}
        for item in snapshot["contacts"]:
            if not isinstance(item, dict) or not item.get("is_personal_contact"):
                continue
            ident = _value(item, "wxid")
            if not ident or len(ident) > 128 or ident.endswith("@chatroom") or ident.startswith("gh_") or ident in {owner, "notifymessage"}:
                continue
            name, remark, alias = _value(item, "nickname", 255), _value(item, "remark", 255), _value(item, "alias", 128)
            rows[ident] = dict(tenant_id=channel["tenant_id"], channel_id=channel["id"], owner_wxid=owner,
                               contact_external_id=ident, name=name, remark=remark, alias=alias,
                               display_name=remark or name or alias or ident, avatar_url=_value(item, "avatar"),
                               profile_json=json.dumps({k: item[k] for k in ("gender", "region", "signature") if _value(item, k)},
                                                       ensure_ascii=False), last_synced_at=synced_at)
        if rows:
            db.execute(friends.insert(), list(rows.values()))
        del runtime["last_contact_sync_snapshot"]
        db.execute(channels.update().where(channels.c.id == channel["id"]).values(
            runtime_info_json=json.dumps(runtime, ensure_ascii=False)))


def downgrade():
    op.drop_table("ipad_friends")
