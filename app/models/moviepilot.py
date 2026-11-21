"""MoviePilot 原生订阅的群内归属；不保存上游凭据、种子地址或媒体文件。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TenantOwnedMixin, TimestampMixin


class MoviePilotSubscription(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "moviepilot_subscriptions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "binding_id", "registration_key", name="uq_mp_registration"),
        Index("ix_mp_binding_status", "tenant_id", "binding_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("bot_plugins.id"))
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    sender_id: Mapped[str] = mapped_column(String(128))
    registration_key: Mapped[str] = mapped_column(String(64))
    # 地址和凭据的摘要隔离不同 MP 实例；更换连接后旧记录不会误操作另一实例。
    connection_key: Mapped[str] = mapped_column(String(64))
    media_source: Mapped[str] = mapped_column(String(32))
    media_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(200))
    year: Mapped[str] = mapped_column(String(16), default="")
    remote_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    can_delete: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # uncertain 阻止不确定的外部写操作自动重试；cancelled 保留操作归属和历史。
    status: Mapped[str] = mapped_column(String(24), default="active")
    notification_json: Mapped[str] = mapped_column(Text, default="{}")


class MoviePilotDownload(Base, TenantOwnedMixin, TimestampMixin):
    """直接下载确认台账：只留安全快照，未知结果不因确认码过期而被自动重试。"""

    __tablename__ = "moviepilot_downloads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "binding_id", "connection_key", "resource_key", name="uq_mp_download_resource"),
        Index("ix_mp_download_owner", "tenant_id", "binding_id", "conversation_id", "sender_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("bot_plugins.id"))
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    sender_id: Mapped[str] = mapped_column(String(128))
    connection_key: Mapped[str] = mapped_column(String(64))
    resource_key: Mapped[str] = mapped_column(String(64))
    token: Mapped[str] = mapped_column(String(12))
    media_json: Mapped[str] = mapped_column(Text)
    resource_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    download_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    registration_id: Mapped[int | None] = mapped_column(ForeignKey("moviepilot_subscriptions.id"), nullable=True)
