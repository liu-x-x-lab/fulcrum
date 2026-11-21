"""保存 MoviePilot 直接下载确认和结果，避免把超时当成未提交。"""

from alembic import op
import sqlalchemy as sa

revision = "20261118_0044"
down_revision = "20261118_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "moviepilot_downloads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("binding_id", sa.Integer(), sa.ForeignKey("bot_plugins.id"), nullable=False),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("sender_id", sa.String(128), nullable=False),
        sa.Column("connection_key", sa.String(64), nullable=False),
        sa.Column("resource_key", sa.String(64), nullable=False),
        sa.Column("token", sa.String(12), nullable=False),
        sa.Column("media_json", sa.Text(), nullable=False),
        sa.Column("resource_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("download_id", sa.String(128), nullable=True),
        sa.Column("registration_id", sa.Integer(), sa.ForeignKey("moviepilot_subscriptions.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "binding_id", "connection_key", "resource_key", name="uq_mp_download_resource"),
    )
    op.create_index("ix_moviepilot_downloads_tenant_id", "moviepilot_downloads", ["tenant_id"])
    op.create_index("ix_mp_download_owner", "moviepilot_downloads", ["tenant_id", "binding_id", "conversation_id", "sender_id"])


def downgrade() -> None:
    op.drop_table("moviepilot_downloads")
