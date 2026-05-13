"""Pydantic schemas for the metrics dashboard API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DashboardResponse(BaseModel):
    """Aggregated totals across the authenticated user's account fleet."""

    total_accounts: int
    total_followers: int
    total_reel_views: int
    total_tasks_running: int


class AccountMetricRead(BaseModel):
    """One metric data point from the account_metrics table."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    metric_type: str
    value: int
    reel_pk: str | None
    captured_at: datetime


class AccountMetricsTimeSeriesResponse(BaseModel):
    """Time-series wrapper for one account's metrics."""

    account_id: uuid.UUID
    metrics: list[AccountMetricRead]
