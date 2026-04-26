"""AI-driven task generation endpoint."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.crud import account as crud_account
from app.crud import task as crud_task
from app.models.task import TaskStatus
from app.schemas.ai import GenerateTaskRequest
from app.schemas.task import TaskCreate, TaskRead
from app.services.ai_parser import AIParser, AIParserError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


def get_ai_parser() -> AIParser:
    """FastAPI dependency — overridable in tests with `app.dependency_overrides`."""
    return AIParser()


@router.post(
    "/generate-task",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
    summary="Translate a natural-language prompt into a DRAFT Task",
)
def generate_task(
    body: GenerateTaskRequest,
    db: Session = Depends(get_db),
    parser: AIParser = Depends(get_ai_parser),
) -> TaskRead:
    # ── Validate target account exists ────────────────────────────────
    try:
        account_uuid = uuid.UUID(body.account_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"account_id is not a valid UUID: {exc}",
        ) from exc

    account = crud_account.get_account(db, account_uuid)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="InstagramAccount not found",
        )

    # ── Call the LLM ──────────────────────────────────────────────────
    try:
        plan = parser.parse(body.user_prompt)
    except AIParserError as exc:
        logger.warning("AI parser failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI parser failed: {exc}",
        ) from exc

    if not plan.commands:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The model could not translate the prompt into any actionable "
                f"commands. Reason: {plan.summary}"
            ),
        )

    # ── Persist as a DRAFT Task ───────────────────────────────────────
    task_in = TaskCreate(
        account_id=account_uuid,
        status=TaskStatus.DRAFT,
        payload=plan.to_payload_dict(),
        priority=plan.priority_as_int(),
    )

    try:
        task = crud_task.create_task(db, task_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return TaskRead.model_validate(task)
