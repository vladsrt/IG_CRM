"""add account_metrics table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-23

The AccountMetric model existed but was never migrated, so /metrics/dashboard
and per-account metrics 500'd with 'relation "account_metrics" does not exist'.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_metrics",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric_type", sa.String(length=32), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("reel_pk", sa.String(length=64), nullable=True),
        sa.Column("raw_payload", JSONB(), nullable=True),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "metric_type IN ('followers', 'reach', 'reel_views')",
            name="ck_account_metrics_metric_type",
        ),
    )
    op.create_index(
        "ix_account_metrics_account_type_captured",
        "account_metrics",
        ["account_id", "metric_type", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_account_metrics_account_type_captured", table_name="account_metrics")
    op.drop_table("account_metrics")
