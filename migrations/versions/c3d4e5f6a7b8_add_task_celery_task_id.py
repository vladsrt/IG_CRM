"""add celery_task_id to tasks

Lets the UI revoke a running task via Celery's control.revoke().

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "celery_task_id")
