"""Baseline-aware shadowban detector (Epic 6, Task 6.3).

Heuristic:
    1. Pull the most recent ``MIN_BASELINE_SAMPLES + 1`` reel-view rows for
       the account from ``account_metrics``.
    2. The first row is the *current* signal; the rest form the baseline.
    3. Compute the median of the baseline. If it's too small (account is
       new / barely-active) the check is skipped — we'd produce only false
       positives.
    4. If the current value is below ``ABSOLUTE_THRESHOLD_VIEWS`` AND below
       ``BASELINE_DROP_RATIO * baseline_median``, flag the account:
         * add ``"possible_shadowban"`` to ``tags`` (deduped, lowercase)
         * set ``status="possible_shadowban"``
         * append a warning line to ``error_log``

Returns a small dict for the caller's diagnostics.
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


# ── Tunables ────────────────────────────────────────────────────────────
MIN_BASELINE_SAMPLES: int = 10
"""Minimum number of historical reel samples required to evaluate."""

MIN_BASELINE_MEDIAN_VIEWS: int = 100
"""Median below this means the account is too quiet to have a baseline."""

ABSOLUTE_THRESHOLD_VIEWS: int = 50
"""Current views must be under this *and* under the ratio to flag."""

BASELINE_DROP_RATIO: float = 0.20
"""Current views must be < 20% of baseline median to flag."""

SHADOWBAN_TAG: str = "possible_shadowban"
SHADOWBAN_STATUS: str = "possible_shadowban"


def evaluate(db: Session, account_id: uuid.UUID) -> dict[str, Any]:
    """Inspect the account's reel-view history and mutate state on a hit.

    The caller is responsible for the surrounding transaction; we ``commit``
    only when we actually mutate the account row, so a no-op call is free.
    """
    account = db.get(InstagramAccount, account_id)
    if account is None:
        return {"evaluated": False, "reason": "account_not_found"}

    samples = _recent_reel_view_samples(db, account_id, MIN_BASELINE_SAMPLES + 1)

    if len(samples) < MIN_BASELINE_SAMPLES + 1:
        logger.info(
            "[shadowban] account=%s only %d reel sample(s) — skipping (need %d)",
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
            "[shadowban] account=%s baseline median %d below threshold %d — skipping",
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
            "[shadowban] account=%s FLAGGED — current=%d, baseline_median=%d "
            "(<%d absolute AND <%.0f%% baseline)",
            account_id, current_views, baseline_median,
            ABSOLUTE_THRESHOLD_VIEWS, BASELINE_DROP_RATIO * 100,
        )
    else:
        logger.info(
            "[shadowban] account=%s OK — current=%d, baseline_median=%d",
            account_id, current_views, baseline_median,
        )

    return diagnostics


# ── Internals ───────────────────────────────────────────────────────────
def _recent_reel_view_samples(
    db: Session, account_id: uuid.UUID, limit: int
) -> list[int]:
    """Return up to ``limit`` most-recent reel_views values, newest first."""
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
    """Add the tag, flip the status, append a log line. Single commit."""
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
