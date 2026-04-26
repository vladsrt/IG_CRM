"""Human-in-the-loop orchestration endpoints — dispatch jobs to Celery."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.crud import account as crud_account
from app.crud import task as crud_task
from app.models.task import TaskStatus
from app.workers.celery_tasks import run_instagram_task, validate_account_session

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


# ── Task execution ──────────────────────────────────────────────────────
_DISPATCHABLE_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.DRAFT.value, TaskStatus.PENDING.value}
)


@router.post(
    "/tasks/{task_id}/start",
    response_model=DispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a Task for execution by the browser worker fleet",
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

    # Mark the task PENDING *before* dispatching so the UI reflects the
    # queued state immediately and so we never lose the transition if
    # Celery picks the job up before this commit lands.
    task.status = TaskStatus.PENDING.value
    task.error_log = None
    db.commit()

    try:
        async_result = run_instagram_task.delay(str(task.id))
    except Exception as exc:
        # Broker unavailable — roll the task back to DRAFT so the user can retry.
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
