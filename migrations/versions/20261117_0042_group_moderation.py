"""增加按租户和群隔离的敏感词违规台账。"""

from alembic import op
import sqlalchemy as sa

revision = "20261117_0042"
down_revision = "20261115_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_moderation_violations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("bot_id", sa.Integer(), sa.ForeignKey("bots.id"), nullable=False),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("chatroom_id", sa.Integer(), sa.ForeignKey("chatrooms.id"), nullable=False),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("member_external_id", sa.String(128), nullable=False),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("matched_word", sa.String(128), nullable=False),
        sa.Column("strike_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("action_job_id", sa.Integer(), sa.ForeignKey("task_jobs.id"), nullable=True),
        sa.Column("result_note", sa.String(255), nullable=True),
        sa.Column("kick_notice", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "chatroom_id", "event_key", name="uq_moderation_room_event"),
    )
    op.create_index("ix_group_moderation_violations_tenant_id", "group_moderation_violations", ["tenant_id"])
    op.create_index("ix_moderation_member_time", "group_moderation_violations", ["tenant_id", "chatroom_id", "member_external_id", "created_at"])


def downgrade() -> None:
    op.drop_table("group_moderation_violations")
