"""AI-driven plan generation endpoint (Human-in-the-Loop, Sprint X).

This endpoint is intentionally side-effect free: it converts a natural-language
prompt into a strict ``ParsedTaskPlan`` and returns it to the frontend for
human review. Persisting Tasks and dispatching them to Celery workers is the
job of ``POST /orchestrator/tasks/fan-out``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas.ai import GenerateTaskRequest, ParsedTaskPlan
from app.services.ai_parser import AIParser, AIParserError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


def get_ai_parser() -> AIParser:
    """FastAPI dependency — overridable in tests via ``app.dependency_overrides``."""
    return AIParser()


@router.post(
    "/generate-task",
    response_model=ParsedTaskPlan,
    status_code=status.HTTP_200_OK,
    summary="Translate a natural-language prompt into a ParsedTaskPlan (no DB writes)",
)
def generate_task(
    body: GenerateTaskRequest,
    parser: AIParser = Depends(get_ai_parser),
) -> ParsedTaskPlan:
    """Run the LLM and return the structured plan unchanged.

    Possible outcomes the frontend must handle:

    * ``plan.clarification_needed`` is a string → render it as the next
      assistant turn in the chat. The user's reply gets concatenated to the
      prior prompt and resubmitted to this endpoint.
    * ``plan.clarification_needed`` is null AND ``plan.commands`` is non-empty
      → ready to dispatch via ``POST /orchestrator/tasks/fan-out``.
    * ``plan.clarification_needed`` is null AND ``plan.commands`` is empty
      → the model decided the request is unsupported; ``plan.summary`` says why.
    """
    try:
        plan = parser.parse(body.user_prompt)
    except AIParserError as exc:
        logger.warning("AI parser failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI parser failed: {exc}",
        ) from exc

    if plan.clarification_needed:
        logger.info(
            "[ai.generate_task] clarification requested: %s",
            plan.clarification_needed,
        )
    else:
        logger.info(
            "[ai.generate_task] returning plan with %d command(s), tags=%s",
            len(plan.commands),
            plan.target_tags,
        )

    return plan
