from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Numeric, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class BillingBalance(Base):
    """Token-based billing balance for a user."""

    __tablename__ = "billing_balance"

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
    tokens_balance: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=6),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )
    last_update: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # ── Relationships ───────────────────────────────────────────────────
    user: Mapped[User] = relationship(
        back_populates="billing_balance",
    )

    def __repr__(self) -> str:
        return f"<BillingBalance user={self.user_id} tokens={self.tokens_balance}>"
