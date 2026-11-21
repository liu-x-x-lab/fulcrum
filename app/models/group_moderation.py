"""群治理违规台账：一条消息一条证据，计数与异步处罚共享同一记录。"""

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TenantOwnedMixin, TimestampMixin


class GroupModerationViolation(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "group_moderation_violations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "chatroom_id", "event_key", name="uq_moderation_room_event"),
        Index("ix_moderation_member_time", "tenant_id", "chatroom_id", "member_external_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    chatroom_id: Mapped[int] = mapped_column(ForeignKey("chatrooms.id"))
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    member_external_id: Mapped[str] = mapped_column(String(128))
    event_key: Mapped[str] = mapped_column(String(64))
    # 不另存完整原文；消息归档仍由平台负责，台账仅保存命中的词和当时次数。
    matched_word: Mapped[str] = mapped_column(String(128))
    strike_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="warning")
    action_job_id: Mapped[int | None] = mapped_column(ForeignKey("task_jobs.id"), nullable=True)
    result_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kick_notice: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
