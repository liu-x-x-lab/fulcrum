"""Store incoming iPad friend requests separately from chat messages."""

from alembic import op
import sqlalchemy as sa

revision = "20261118_0046"
down_revision = "20261118_0045"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ipad_friend_requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("channel_id", sa.Integer, sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_wxid", sa.String(128), nullable=False),
        sa.Column("applicant", sa.String(128), nullable=False),
        sa.Column("nickname", sa.String(255), nullable=False),
        sa.Column("ticket_digest", sa.String(64), nullable=False),
        sa.Column("credentials", sa.Text),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "channel_id", "owner_wxid", "ticket_digest", name="uq_ipad_friend_request_ticket"),
    )
    op.create_index("ix_ipad_friend_requests_tenant_id", "ipad_friend_requests", ["tenant_id"])


def downgrade():
    op.drop_table("ipad_friend_requests")
