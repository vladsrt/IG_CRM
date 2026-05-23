"""Orchestrator routes, push jobs to Celery."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.crud import account as crud_account
from app.crud import task as crud_task
from app.models.account import InstagramAccount
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.schemas.orchestrator import (
    FanOutDispatchedTask,
    FanOutResponse,
    FanOutSkippedAccount,
    FanOutTaskRequest,
)
from app.schemas.task import TaskCreate
from app.services.spintax import uniqueize_plan_payload
from app.services.trust import (
    DEFAULT_MIN_TRUST_SCORE,
    TrustReport,
    evaluate_trust,
)
from app.workers.celery_tasks import run_instagram_task, validate_account_session

# UA passed to the trust scorer. Keep this in sync with the UA used by the
# worker (workers.core.executor.DEFAULT_USER_AGENT).
_DISPATCH_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])


# response schemas
class DispatchResponse(BaseModel):
    """Returned when a route pushes a job to a Celery worker."""

    celery_task_id: str
    status: str
    detail: str


# account validation
@router.post(
    "/accounts/{account_id}/validate",
    response_model=DispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run background cookie/session check for an Instagram account",
)
def trigger_account_validation(
    account_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DispatchResponse:
    account = crud_account.get_account(db, account_id)
    if account is None or account.user_id != current_user.id:
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


# single task execution
_DISPATCHABLE_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.DRAFT.value, TaskStatus.PENDING.value}
)


@router.post(
    "/tasks/{task_id}/start",
    response_model=DispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue an existing Task (one account) for run",
)
def trigger_task_start(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DispatchResponse:
    task = crud_task.get_task(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    owner = crud_account.get_account(db, task.account_id)
    if owner is None or owner.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    if task.status not in _DISPATCHABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Task {task.id} status is '{task.status}', only "
                f"{sorted(_DISPATCHABLE_STATUSES)} can start."
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


# fan out: one ParsedTaskPlan turns into many Tasks and many Celery jobs
def _resolve_target_accounts(
    db: Session,
    request: FanOutTaskRequest,
    user_id: uuid.UUID,
) -> tuple[dict[uuid.UUID, InstagramAccount], list[uuid.UUID]]:
    """Returns (resolved_by_id, missing_explicit_ids).

    Scoped to the caller: tag matches and explicit ids only resolve to
    accounts owned by ``user_id``. Tag matches go first, then explicit ids.
    """
    resolved: dict[uuid.UUID, InstagramAccount] = {}

    if request.plan.target_tags:
        try:
            tag_accounts = crud_account.list_accounts_by_tags(
                db, tags=request.plan.target_tags, user_id=user_id
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
        if acct is None or acct.user_id != user_id:
            missing.append(acct_id)
        else:
            resolved.setdefault(acct.id, acct)

    return resolved, missing


def _evaluate_account_trust(account: InstagramAccount) -> TrustReport:
    """Run the trust gate for one account.

    Wrapped in try/except so a network or dns problem inside the probe gives
    back a score=0 report instead of breaking the whole fan-out loop.
    """
    try:
        return evaluate_trust(account, user_agent=_DISPATCH_USER_AGENT)
    except Exception as exc:
        logger.exception(
            "[fan_out] trust evaluation crashed for account_id=%s", account.id
        )
        return TrustReport(
            score=0,
            proxy_ok=False,
            proxy_latency_ms=None,
            user_agent_ok=False,
            hygiene_ok=False,
            reasons=[f"trust evaluator crashed: {type(exc).__name__}: {exc}"],
        )


def _create_and_dispatch_one(
    db: Session,
    account: InstagramAccount,
    request: FanOutTaskRequest,
) -> tuple[Task | None, str | None, str | None]:
    """Save one Task and push it to Celery.

    Returns (task, celery_task_id, error_reason). Only one of celery_task_id or
    error_reason is set.

    Two gates before dispatch:
      1. trust score, fail fast before any db write.
      2. spintax plus link obfuscation, build a per-account payload so 50
         dispatched tasks do not share the same bytes.
    """
    # gate 1: trust score
    report = _evaluate_account_trust(account)
    if not report.passed:
        logger.info(
            "[fan_out] trust gate failed for account_id=%s score=%d/%d",
            account.id, report.score, DEFAULT_MIN_TRUST_SCORE,
        )
        return None, None, report.to_skip_reason()

    # gate 2: per account uniqueize
    # this rewrites the plan dict with spintax variants and link obfuscation,
    # so two cloned tasks never end up with the same payload.
    base_payload = request.plan.to_payload_dict()
    unique_payload = uniqueize_plan_payload(base_payload)

    # save and dispatch
    try:
        task_in = TaskCreate(
            account_id=account.id,
            status=TaskStatus.PENDING,
            payload=unique_payload,
            priority=request.plan.priority_as_int(),
        )
        task = crud_task.create_task(db, task_in)
    except Exception as exc:
        logger.exception(
            "[fan_out] create_task failed for account_id=%s", account.id
        )
        return None, None, f"create_task failed: {type(exc).__name__}: {exc}"

    # send to celery. if broker fails, roll the row back to DRAFT.
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
    summary="Take an approved ParsedTaskPlan and create one Task per matching account",
)
def fan_out_plan(
    request: FanOutTaskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FanOutResponse:
    """Send the approved plan to every matching account.

    How accounts are picked:
        1. accounts matching plan.target_tags (jsonb contains).
        2. plus any accounts in target_account_ids (deduped).

    If one account fails (db write rejected, celery broker down) the rest of
    the run continues. Failures are put into the `skipped` list with a reason.
    """
    plan = request.plan

    # pre-flight: reject plans we cannot run
    if plan.clarification_needed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Plan still needs more info, fix it in the AI chat before "
                f"fan-out. Question: {plan.clarification_needed!r}"
            ),
        )
    if not plan.commands:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Plan has no commands, nothing to dispatch.",
        )

    # find target accounts (scoped to the caller)
    resolved, missing_explicit = _resolve_target_accounts(db, request, current_user.id)

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

    # fill the skipped list with explicit ids the db did not find
    skipped: list[FanOutSkippedAccount] = [
        FanOutSkippedAccount(
            account_id=missing_id,
            reason="account_id not found in database",
        )
        for missing_id in missing_explicit
    ]
    dispatched: list[FanOutDispatchedTask] = []

    # dispatch loop, one small tx per account
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
