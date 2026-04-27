"""Schemas for the fan-out orchestrator endpoint."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.ai import ParsedTaskPlan


class FanOutTaskRequest(BaseModel):
    """Body for ``POST /orchestrator/tasks/fan-out``.

    The ``plan`` is the LLM output the operator just reviewed (typically
    obtained from ``POST /ai/generate-task``). ``target_account_ids`` is a
    fallback / extension list — it is **unioned** with the accounts matched
    by ``plan.target_tags`` so the human can hand-pick extras without losing
    the tag-matched cohort.
    """

    model_config = ConfigDict(extra="forbid")

    plan: ParsedTaskPlan
    target_account_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description=(
            "Explicit InstagramAccount IDs to dispatch to. Used as a fallback "
            "when `plan.target_tags` is empty, AND merged with tag-matched "
            "accounts when both are present. Duplicates are removed."
        ),
    )


class FanOutDispatchedTask(BaseModel):
    """One row in the ``dispatched`` list of the response."""

    model_config = ConfigDict(from_attributes=True)

    task_id: uuid.UUID
    account_id: uuid.UUID
    celery_task_id: str


class FanOutSkippedAccount(BaseModel):
    """One row in the ``skipped`` list of the response.

    Covers both pre-dispatch misses (explicit account_id that doesn't exist)
    and post-dispatch failures (DB write failed, broker unreachable, etc.).
    """

    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID
    reason: str


class FanOutResponse(BaseModel):
    """Summary returned by the fan-out endpoint."""

    model_config = ConfigDict(from_attributes=True)

    requested_accounts: int = Field(
        description=(
            "Number of unique accounts resolved from tags + explicit IDs "
            "(before dispatch is attempted)."
        ),
    )
    dispatched_count: int = Field(
        description="Number of Tasks successfully created AND queued on Celery."
    )
    skipped_count: int = Field(
        description=(
            "Accounts that resolved but failed to either persist a Task row "
            "or hand off to Celery. See `skipped` for per-account reasons."
        ),
    )
    dispatched: list[FanOutDispatchedTask]
    skipped: list[FanOutSkippedAccount]
    celery_task_ids: list[str] = Field(
        description="Flat list of Celery AsyncResult IDs (= dispatched[*].celery_task_id)."
    )
