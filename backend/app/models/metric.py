"""Time series metrics pulled from Instagram graphql and xhr responses."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.account import InstagramAccount


class MetricType(str, enum.Enum):
    """Metric kinds the network interceptor can parse out."""

    FOLLOWERS = "followers"
    REACH = "reach"
    REEL_VIEWS = "reel_views"


class AccountMetric(Base):
    """One metric data point taken from a network response.

    The interceptor writes one row per interesting json payload it sees on the
    wire. Typical queries:

    - latest follower count for an account:
        order by captured_at desc limit 1, with metric_type='followers'
    - last 10 reel views (used for shadowban baseline):
        order by captured_at desc limit 10, with metric_type='reel_views'
    """

    __tablename__ = "account_metrics"
    __table_args__ = (
        CheckConstraint(
            "metric_type IN ('followers', 'reach', 'reel_views')",
            name="ck_account_metrics_metric_type",
        ),
        Index(
            "ix_account_metrics_account_type_captured",
            "account_id",
            "metric_type",
            "captured_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    metric_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reel_pk: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc="IG media primary key for reel-level metrics. NULL for account-level.",
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        doc="Trimmed source payload, kept for debugging only.",
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    account: Mapped[InstagramAccount] = relationship()

    def __repr__(self) -> str:
        return (
            f"<AccountMetric {self.metric_type}={self.value} "
            f"account={self.account_id} at={self.captured_at}>"
        )
