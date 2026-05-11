"""Shadowban detector that compares the latest reel views to a baseline.

How it works:
    1. Read the last MIN_BASELINE_SAMPLES + 1 reel_views rows for the
       account from account_metrics.
    2. The first row is the current value, the rest is the baseline.
    3. Take the median of the baseline. If it is too small (new or quiet
       account), skip the check, otherwise we just get false positives.
    4. If the current value is below ABSOLUTE_THRESHOLD_VIEWS and below
       BASELINE_DROP_RATIO * baseline_median, mark the account:
         - add "possible_shadowban" to tags (lowercase, deduped)
         - set status="possible_shadowban"
         - append one line to error_log

Returns a small dict with the details for the caller.
"""

from __future__ import annotations

import logging
import statistics
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import InstagramAccount
from app.models.metric import AccountMetric, MetricType

logger = logging.getLogger(__name__)


# knobs
MIN_BASELINE_SAMPLES: int = 10
"""How many old reel samples we need to even run the check."""

MIN_BASELINE_MEDIAN_VIEWS: int = 100
"""If the median is below this, the account is too quiet for a real baseline."""

ABSOLUTE_THRESHOLD_VIEWS: int = 50
"""Current views must be below this AND below the ratio to flag."""

BASELINE_DROP_RATIO: float = 0.20
"""Current views must be under 20% of baseline median to flag."""

SHADOWBAN_TAG: str = "possible_shadowban"
SHADOWBAN_STATUS: str = "possible_shadowban"


def evaluate(db: Session, account_id: uuid.UUID) -> dict[str, Any]:
    """Look at the account's reel views history and update state on a hit.

    The caller owns the outer transaction. We only commit when we actually
    change the account row, so a no-op call costs nothing.
    """
    account = db.get(InstagramAccount, account_id)
    if account is None:
        return {"evaluated": False, "reason": "account_not_found"}

    samples = _recent_reel_view_samples(db, account_id, MIN_BASELINE_SAMPLES + 1)

    if len(samples) < MIN_BASELINE_SAMPLES + 1:
        logger.info(
            "[shadowban] account=%s only %d reel sample(s), skipping (need %d)",
            account_id, len(samples), MIN_BASELINE_SAMPLES + 1,
        )
        return {
            "evaluated": False,
            "reason": "insufficient_history",
            "samples": len(samples),
        }

    current_views = samples[0]
    baseline = samples[1:]
    baseline_median = int(statistics.median(baseline))

    if baseline_median < MIN_BASELINE_MEDIAN_VIEWS:
        logger.info(
            "[shadowban] account=%s baseline median %d below threshold %d, skipping",
            account_id, baseline_median, MIN_BASELINE_MEDIAN_VIEWS,
        )
        return {
            "evaluated": False,
            "reason": "baseline_too_small",
            "baseline_median": baseline_median,
        }

    is_low_absolute = current_views < ABSOLUTE_THRESHOLD_VIEWS
    is_low_relative = current_views < int(baseline_median * BASELINE_DROP_RATIO)
    flagged = is_low_absolute and is_low_relative

    diagnostics: dict[str, Any] = {
        "evaluated": True,
        "flagged": flagged,
        "current_views": current_views,
        "baseline_median": baseline_median,
        "absolute_threshold": ABSOLUTE_THRESHOLD_VIEWS,
        "ratio_threshold_views": int(baseline_median * BASELINE_DROP_RATIO),
        "samples_considered": len(samples),
    }

    if flagged:
        _mark_shadowban(db, account, diagnostics)
        logger.warning(
            "[shadowban] account=%s FLAGGED, current=%d, baseline_median=%d "
            "(<%d absolute and <%.0f%% baseline)",
            account_id, current_views, baseline_median,
            ABSOLUTE_THRESHOLD_VIEWS, BASELINE_DROP_RATIO * 100,
        )
    else:
        logger.info(
            "[shadowban] account=%s OK, current=%d, baseline_median=%d",
            account_id, current_views, baseline_median,
        )

    return diagnostics


# internals
def _recent_reel_view_samples(
    db: Session, account_id: uuid.UUID, limit: int
) -> list[int]:
    """Return up to `limit` newest reel_views values, newest first."""
    stmt = (
        select(AccountMetric.value)
        .where(
            AccountMetric.account_id == account_id,
            AccountMetric.metric_type == MetricType.REEL_VIEWS.value,
        )
        .order_by(AccountMetric.captured_at.desc())
        .limit(limit)
    )
    return [int(v) for v in db.execute(stmt).scalars().all()]


def _mark_shadowban(
    db: Session,
    account: InstagramAccount,
    diagnostics: dict[str, Any],
) -> None:
    """Add the tag, change status, append a log line. One commit."""
    tags = list(account.tags or [])
    if SHADOWBAN_TAG not in tags:
        tags.append(SHADOWBAN_TAG)
    account.tags = tags
    account.status = SHADOWBAN_STATUS

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_line = (
        f"[{timestamp}] possible shadowban: current_views="
        f"{diagnostics['current_views']}, baseline_median="
        f"{diagnostics['baseline_median']}, threshold="
        f"{diagnostics['ratio_threshold_views']}"
    )
    account.error_log = (
        f"{account.error_log}\n{new_line}" if account.error_log else new_line
    )

    db.commit()
    db.refresh(account)
