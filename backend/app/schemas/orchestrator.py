"""Schemas for the fan-out orchestrator route."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.ai import ParsedTaskPlan


class FanOutTaskRequest(BaseModel):
    """Body for POST /orchestrator/tasks/fan-out.

    `plan` is the LLM output the user just approved (usually from
    POST /ai/generate-task). `target_account_ids` is a fallback or extra list.
    It is merged (union) with the accounts matched by `plan.target_tags`, so
    the user can hand pick extras without losing the tag-matched group.
    """

    model_config = ConfigDict(extra="forbid")

    plan: ParsedTaskPlan
    target_account_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description=(
            "Explicit InstagramAccount ids to dispatch to. Used as a fallback "
            "when `plan.target_tags` is empty, and merged with tag-matched "
            "accounts when both are set. Duplicates are dropped."
        ),
    )


class FanOutDispatchedTask(BaseModel):
    """One row inside the response `dispatched` list."""

    model_config = ConfigDict(from_attributes=True)

    task_id: uuid.UUID
    account_id: uuid.UUID
    celery_task_id: str


class FanOutSkippedAccount(BaseModel):
    """One row inside the response `skipped` list.

    Covers both: an explicit account_id that does not exist, and accounts that
    failed during dispatch (db write failed, broker down, etc).
    """

    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID
    reason: str


class FanOutResponse(BaseModel):
    """Summary the fan-out route returns."""

    model_config = ConfigDict(from_attributes=True)

    requested_accounts: int = Field(
        description=(
            "How many unique accounts we got from tags + explicit ids, before "
            "we try to dispatch."
        ),
    )
    dispatched_count: int = Field(
        description="How many Tasks were saved and queued on Celery."
    )
    skipped_count: int = Field(
        description=(
            "Accounts that were resolved but the Task row or Celery push "
            "failed. Per-account reasons are in `skipped`."
        ),
    )
    dispatched: list[FanOutDispatchedTask]
    skipped: list[FanOutSkippedAccount]
    celery_task_ids: list[str] = Field(
        description="Flat list of Celery AsyncResult ids (same as dispatched[*].celery_task_id)."
    )
