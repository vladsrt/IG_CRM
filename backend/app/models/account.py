from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.proxy import Proxy
    from app.models.task import Task


class AuthMethod(str, enum.Enum):
    """Allowed authentication methods for Instagram accounts."""

    COOKIES = "cookies"
    MANUAL = "manual"


class InstagramAccount(Base):
    """An Instagram account linked to a user."""

    __tablename__ = "instagram_accounts"
    __table_args__ = (
        CheckConstraint(
            "auth_method IN ('cookies', 'manual')",
            name="ck_instagram_accounts_auth_method",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    proxy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxies.id", ondelete="SET NULL"),
        nullable=True,
    )
    ig_username: Mapped[str] = mapped_column(String(255), nullable=False)
    ig_password: Mapped[str] = mapped_column(String(1024), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(20), nullable=False)
    proxy_session_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    cookies: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ── Relationships ───────────────────────────────────────────────────
    user: Mapped[User] = relationship(
        back_populates="instagram_accounts",
    )
    proxy: Mapped[Proxy | None] = relationship(
        back_populates="instagram_accounts",
    )
    tasks: Mapped[list[Task]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<InstagramAccount @{self.ig_username}>"
