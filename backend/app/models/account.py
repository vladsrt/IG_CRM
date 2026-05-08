from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, text
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


class Platform(str, enum.Enum):
    """Operating system the spoofed Chromium build advertises.

    Bound to the account at creation time and used to seed the
    User-Agent if the operator did not supply one. Once persisted, the
    UA stays pinned for the lifetime of the account so we don't emit
    "browser changed OS overnight" telemetry.
    """

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


class InstagramAccount(Base):
    """An Instagram account linked to a user."""

    __tablename__ = "instagram_accounts"
    __table_args__ = (
        CheckConstraint(
            "auth_method IN ('cookies', 'manual')",
            name="ck_instagram_accounts_auth_method",
        ),
        CheckConstraint(
            "platform IN ('windows', 'macos', 'linux')",
            name="ck_instagram_accounts_platform",
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

    # ── Browser fingerprint pinning ─────────────────────────────────────
    # ``platform`` decides which UA pool the account is seeded from at
    # creation time. ``user_agent`` is the resolved UA string; once set
    # it is NEVER auto-rotated — rotating UAs between sessions is itself
    # a strong bot signal, since real browsers don't change their major
    # version mid-week.
    platform: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=Platform.WINDOWS.value,
        server_default=text("'windows'"),
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )

    # ── Tagging (Sprint 5) ──────────────────────────────────────────────
    # Free-form list of lowercase string tags (e.g. ["crypto", "tier1"]).
    # Stored as a JSONB array so the AI orchestrator can target groups via
    # JSONB containment queries (`tags @> '["crypto"]'::jsonb`).
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
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
        return f"<InstagramAccount @{self.ig_username} tags={self.tags}>"
