"""Metrics dashboard API.

Provides aggregated fleet totals and per-account time-series data for the
frontend dashboard. All routes are JWT-protected and scoped to the
authenticated user's accounts to enforce multi-tenancy.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.models.account import InstagramAccount
from app.models.metric import AccountMetric
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.schemas.metrics import (
    AccountMetricRead,
    AccountMetricsTimeSeriesResponse,
    DashboardResponse,
)

router = APIRouter(tags=["metrics"])


@router.get("/metrics/dashboard", response_model=DashboardResponse)
def get_dashboard(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Aggregated totals across the authenticated user's account fleet.

    Returns:
        - total_accounts: count of user's Instagram accounts.
        - total_followers: sum of the latest follower_count per account.
        - total_reel_views: sum of the latest reel_views per account.
        - total_tasks_running: count of tasks currently in RUNNING status.
    """
    # all accounts of this user, with their status (for active/checkpoint counts)
    rows = db.execute(
        select(InstagramAccount.id, InstagramAccount.status).where(
            InstagramAccount.user_id == current_user.id
        )
    ).all()
    account_ids: list[uuid.UUID] = [r[0] for r in rows]

    accounts_total = len(account_ids)
    accounts_active = sum(1 for r in rows if r[1] == "active")
    accounts_checkpoint = sum(1 for r in rows if r[1] == "checkpoint_required")

    if not account_ids:
        return DashboardResponse(
            accounts_total=0,
            accounts_active=0,
            accounts_checkpoint=0,
            total_followers=0,
            total_reel_views=0,
            tasks_running=0,
            tasks_completed_24h=0,
            tasks_failed_24h=0,
        )

    # latest value per account for these metric types, summed across the fleet
    total_followers = _sum_latest_metric(db, account_ids, "followers")
    total_reel_views = _sum_latest_metric(db, account_ids, "reel_views")

    # task counts: currently running, plus completed/failed in the last 24h
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)

    def _count_tasks(status_value: str, since: datetime | None = None) -> int:
        stmt = select(func.count(Task.id)).where(
            Task.account_id.in_(account_ids),
            Task.status == status_value,
        )
        if since is not None:
            stmt = stmt.where(Task.created_at >= since)
        return int(db.execute(stmt).scalar_one() or 0)

    return DashboardResponse(
        accounts_total=accounts_total,
        accounts_active=accounts_active,
        accounts_checkpoint=accounts_checkpoint,
        total_followers=total_followers,
        total_reel_views=total_reel_views,
        tasks_running=_count_tasks(TaskStatus.RUNNING.value),
        tasks_completed_24h=_count_tasks(TaskStatus.COMPLETED.value, cutoff_24h),
        tasks_failed_24h=_count_tasks(TaskStatus.FAILED.value, cutoff_24h),
    )


@router.get(
    "/accounts/{account_id}/metrics",
    response_model=AccountMetricsTimeSeriesResponse,
)
def get_account_metrics(
    account_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AccountMetricsTimeSeriesResponse:
    """Time-series metrics for one account, filtered to the last 30 days.

    Returns all AccountMetric rows (followers, reach, reel_views) ordered
    by captured_at, ready for frontend charting.

    Raises 404 if the account does not exist or does not belong to the
    authenticated user (IDOR protection).
    """
    # verify ownership
    account = db.execute(
        select(InstagramAccount).where(
            InstagramAccount.id == account_id,
            InstagramAccount.user_id == current_user.id,
        )
    ).scalar_one_or_none()

    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    metrics_stmt = (
        select(AccountMetric)
        .where(
            AccountMetric.account_id == account_id,
            AccountMetric.captured_at >= cutoff,
        )
        .order_by(AccountMetric.captured_at.asc())
    )
    rows = list(db.execute(metrics_stmt).scalars().all())

    return AccountMetricsTimeSeriesResponse(
        account_id=account_id,
        metrics=[AccountMetricRead.model_validate(r) for r in rows],
    )


def _sum_latest_metric(
    db: Session,
    account_ids: list[uuid.UUID],
    metric_type: str,
) -> int:
    """Sum the latest value of `metric_type` across a set of accounts.

    For each account, picks the most recent AccountMetric row with the given
    metric_type and sums the values. Uses a lateral subquery for efficiency.
    """
    # latest metric value per account, correlated to the outer InstagramAccount
    latest = (
        select(AccountMetric.value)
        .where(
            AccountMetric.account_id == InstagramAccount.id,
            AccountMetric.metric_type == metric_type,
        )
        .order_by(AccountMetric.captured_at.desc())
        .limit(1)
        .correlate(InstagramAccount)
        .scalar_subquery()
    )

    # one row per account with its latest value (0 if none), then sum
    per_account = (
        select(func.coalesce(latest, 0).label("v"))
        .where(InstagramAccount.id.in_(account_ids))
        .subquery()
    )

    total = db.execute(
        select(func.coalesce(func.sum(per_account.c.v), 0))
    ).scalar_one()

    return int(total or 0)
