"""Add independently configured, opt-in vision models."""

from alembic import op
import sqlalchemy as sa

revision = "20261121_0047"
down_revision = "20261118_0046"
branch_labels = None
depends_on = None


def upgrade():
    """新能力默认关闭，已有聊天模型、密钥和开关保持不变。"""

    with op.batch_alter_table("tenant_ai_settings") as batch:
        batch.add_column(sa.Column("vision_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("vision_api_base", sa.String(255), nullable=False, server_default=""))
        batch.add_column(sa.Column("vision_model", sa.String(100), nullable=False, server_default=""))
        batch.add_column(sa.Column("vision_api_key_encrypted", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("tenant_ai_settings") as batch:
        batch.drop_column("vision_api_key_encrypted")
        batch.drop_column("vision_model")
        batch.drop_column("vision_api_base")
        batch.drop_column("vision_enabled")
