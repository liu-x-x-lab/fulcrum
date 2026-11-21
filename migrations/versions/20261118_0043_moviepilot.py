"""保存 MoviePilot 原生订阅的租户归属和通知水位。"""

from alembic import op
import sqlalchemy as sa

revision = "20261118_0043"
down_revision = "20261117_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "moviepilot_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("binding_id", sa.Integer(), sa.ForeignKey("bot_plugins.id"), nullable=False),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("sender_id", sa.String(128), nullable=False),
        sa.Column("registration_key", sa.String(64), nullable=False),
        sa.Column("connection_key", sa.String(64), nullable=False),
        sa.Column("media_source", sa.String(32), nullable=False),
        sa.Column("media_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("year", sa.String(16), nullable=False),
        sa.Column("remote_id", sa.Integer(), nullable=True),
        sa.Column("remote_fingerprint", sa.String(64), nullable=True),
        sa.Column("can_delete", sa.Boolean(), nullable=False),
        sa.Column("notify_enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("notification_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "binding_id", "registration_key", name="uq_mp_registration"),
    )
    op.create_index("ix_moviepilot_subscriptions_tenant_id", "moviepilot_subscriptions", ["tenant_id"])
    op.create_index("ix_mp_binding_status", "moviepilot_subscriptions", ["tenant_id", "binding_id", "status"])


def downgrade() -> None:
    op.drop_table("moviepilot_subscriptions")
