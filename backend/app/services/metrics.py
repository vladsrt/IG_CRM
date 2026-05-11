"""Save worker side MetricSample rows into the account_metrics table."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.metric import AccountMetric

if TYPE_CHECKING:
    from workers.core.observability import MetricSample

logger = logging.getLogger(__name__)


def persist_samples(
    db: Session,
    account_id: uuid.UUID,
    samples: Iterable["MetricSample"],
) -> int:
    """Bulk insert `samples` for `account_id`. Returns how many rows were saved.

    Best effort. If a constraint fails (like a bad account_id), we log it
    and roll back instead of raising, so a metrics write does not break
    the parent Task.
    """
    rows = [
        AccountMetric(
            account_id=account_id,
            metric_type=sample.metric_type,
            value=int(sample.value),
            reel_pk=sample.reel_pk,
            raw_payload=sample.raw_payload,
        )
        for sample in samples
    ]
    if not rows:
        return 0

    db.add_all(rows)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning(
            "[metrics] integrity error persisting %d sample(s) for account %s: %s",
            len(rows), account_id, exc,
        )
        return 0
    except Exception:
        db.rollback()
        logger.exception(
            "[metrics] unexpected failure persisting %d sample(s) for account %s",
            len(rows), account_id,
        )
        return 0

    logger.info(
        "[metrics] persisted %d sample(s) for account %s", len(rows), account_id
    )
    return len(rows)
