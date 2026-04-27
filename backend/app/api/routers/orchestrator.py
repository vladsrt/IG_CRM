"""Human-in-the-loop orchestration endpoints — dispatch jobs to Celery."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.crud import account as crud_account
from app.crud import task as crud_task
from app.models.account import InstagramAccount
from app.models.task import Task, TaskStatus
from app.schemas.orchestrator import (
    FanOutDispatchedTask,
    FanOutResponse,
    FanOutSkippedAccount,
    FanOutTaskRequest,
)
from app.schemas.task import TaskCreate
from app.workers.celery_tasks import run_instagram_task, validate_account_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])


# ── Response schemas ────────────────────────────────────────────────────
class DispatchResponse(BaseModel):
    """Returned whenever an endpoint hands work off to a Celery worker."""

    celery_task_id: str
    status: str
    detail: str


# ── Account validation ──────────────────────────────────────────────────
@router.post(
    "/accounts/{account_id}/validate",
    response_model=DispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger background cookie/session validation for an Instagram account",
)
def trigger_account_validation(
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> DispatchResponse:
    account = crud_account.get_account(db, account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="InstagramAccount not found",
        )

    async_result = validate_account_session.delay(str(account.id))
    return DispatchResponse(
        celery_task_id=async_result.id,
        status="dispatched",
        detail=f"Validation job queued for account {account.id}",
    )


# ── Single-task execution ──────────────────────────────────────────────
_DISPATCHABLE_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.DRAFT.value, TaskStatus.PENDING.value}
)


@router.post(
    "/tasks/{task_id}/start",
    response_model=DispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue an *existing* Task (single account) for execution",
)
def trigger_task_start(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> DispatchResponse:
    task = crud_task.get_task(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    if task.status not in _DISPATCHABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Task {task.id} is in status '{task.status}' — only "
                f"{sorted(_DISPATCHABLE_STATUSES)} can be started."
            ),
        )

    task.status = TaskStatus.PENDING.value
    task.error_log = None
    db.commit()

    try:
        async_result = run_instagram_task.delay(str(task.id))
    except Exception as exc:
        task.status = TaskStatus.DRAFT.value
        task.error_log = f"Failed to enqueue Celery job: {exc}"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not dispatch task to worker queue: {exc}",
        ) from exc

    return DispatchResponse(
        celery_task_id=async_result.id,
        status=TaskStatus.PENDING.value,
        detail=f"Task {task.id} dispatched to worker queue",
    )


# ── Fan-out: ParsedTaskPlan → many Tasks → many Celery jobs ────────────
def _resolve_target_accounts(
    db: Session,
    request: FanOutTaskRequest,
) -> tuple[dict[uuid.UUID, InstagramAccount], list[uuid.UUID]]:
    """Return ``(resolved_by_id, missing_explicit_ids)``.

    Uses ``dict[id, account]`` rather than a set for O(1) dedupe + stable
    iteration order. Tag-matched accounts come first, explicit IDs second.
    """
    resolved: dict[uuid.UUID, InstagramAccount] = {}

    if request.plan.target_tags:
        try:
            tag_accounts = crud_account.list_accounts_by_tags(
                db, tags=request.plan.target_tags
            )
        except Exception as exc:
            logger.exception("[fan_out] tag query failed: %s", exc)
            tag_accounts = []
        for acct in tag_accounts:
            resolved.setdefault(acct.id, acct)

    missing: list[uuid.UUID] = []
    for acct_id in request.target_account_ids:
        if acct_id in resolved:
            continue
        acct = crud_account.get_account(db, acct_id)
        if acct is None:
            missing.append(acct_id)
        else:
            resolved.setdefault(acct.id, acct)

    return resolved, missing


def _create_and_dispatch_one(
    db: Session,
    account: InstagramAccount,
    request: FanOutTaskRequest,
) -> tuple[Task | None, str | None, str | None]:
    """Persist one Task and hand it to Celery.

    Returns ``(task, celery_task_id, error_reason)``. Exactly one of
    ``celery_task_id`` or ``error_reason`` is non-None.
    """
    # ── 1. Create the Task row in PENDING.
    try:
        task_in = TaskCreate(
            account_id=account.id,
            status=TaskStatus.PENDING,
            payload=request.plan.to_payload_dict(),
            priority=request.plan.priority_as_int(),
        )
        task = crud_task.create_task(db, task_in)
    except Exception as exc:
        logger.exception(
            "[fan_out] create_task failed for account_id=%s", account.id
        )
        return None, None, f"create_task failed: {type(exc).__name__}: {exc}"

    # ── 2. Hand off to Celery. Roll the row back to DRAFT on broker failure.
    try:
        async_result = run_instagram_task.delay(str(task.id))
    except Exception as exc:
        logger.exception(
            "[fan_out] celery dispatch failed for task_id=%s", task.id
        )
        try:
            task.status = TaskStatus.DRAFT.value
            task.error_log = f"Failed to enqueue Celery job: {exc}"
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "[fan_out] could not mark task %s DRAFT after dispatch failure",
                task.id,
            )
        return task, None, f"celery dispatch failed: {type(exc).__name__}: {exc}"

    return task, async_result.id, None


@router.post(
    "/tasks/fan-out",
    response_model=FanOutResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fan a reviewed ParsedTaskPlan out to one Task per matching account",
)
def fan_out_plan(
    request: FanOutTaskRequest,
    db: Session = Depends(get_db),
) -> FanOutResponse:
    """Dispatch the operator-approved plan to every matching account.

    Resolution order:
        1. Accounts matching ``plan.target_tags`` (JSONB containment).
        2. PLUS any accounts in ``target_account_ids`` (deduped).

    Per-account failures (DB write rejected, Celery broker down) do NOT
    abort the run — they are collected into ``skipped`` and the operator
    gets a per-account reason in the response.
    """
    plan = request.plan

    # ── Pre-flight: refuse to dispatch unactionable plans ─────────────
    if plan.clarification_needed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Plan still needs clarification — resolve it via the AI chat "
                f"before fan-out. Question: {plan.clarification_needed!r}"
            ),
        )
    if not plan.commands:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Plan has no commands — nothing to dispatch.",
        )

    # ── Resolve target accounts ───────────────────────────────────────
    resolved, missing_explicit = _resolve_target_accounts(db, request)

    if not resolved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No target accounts resolved. "
                f"target_tags={plan.target_tags}, "
                f"target_account_ids={[str(x) for x in request.target_account_ids]}, "
                f"missing_explicit={[str(x) for x in missing_explicit]}"
            ),
        )

    # Pre-populate the skipped list with explicit IDs the DB didn't know.
    skipped: list[FanOutSkippedAccount] = [
        FanOutSkippedAccount(
            account_id=missing_id,
            reason="account_id not found in database",
        )
        for missing_id in missing_explicit
    ]
    dispatched: list[FanOutDispatchedTask] = []

    # ── Dispatch loop — one short transaction per account ─────────────
    for account in resolved.values():
        task, celery_id, error_reason = _create_and_dispatch_one(
            db, account, request
        )

        if celery_id is not None and task is not None:
            dispatched.append(
                FanOutDispatchedTask(
                    task_id=task.id,
                    account_id=account.id,
                    celery_task_id=celery_id,
                )
            )
        else:
            skipped.append(
                FanOutSkippedAccount(
                    account_id=account.id,
                    reason=error_reason or "unknown failure",
                )
            )

    logger.info(
        "[fan_out] resolved=%d dispatched=%d skipped=%d",
        len(resolved),
        len(dispatched),
        len(skipped),
    )

    return FanOutResponse(
        requested_accounts=len(resolved),
        dispatched_count=len(dispatched),
        skipped_count=len(skipped),
        dispatched=dispatched,
        skipped=skipped,
        celery_task_ids=[d.celery_task_id for d in dispatched],
    )
