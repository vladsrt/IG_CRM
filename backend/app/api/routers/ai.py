"""AI plan generation endpoint, human in the loop.

No side effects here. We just take a user prompt, turn it into a ParsedTaskPlan,
and send it back to the frontend so the user can check it. Actual task save and
Celery dispatch happens in POST /orchestrator/tasks/fan-out.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas.ai import GenerateTaskRequest, ParsedTaskPlan
from app.services.ai_parser import AIParser, AIParserError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


def get_ai_parser() -> AIParser:
    # tests can override this via app.dependency_overrides
    return AIParser()


@router.post(
    "/generate-task",
    response_model=ParsedTaskPlan,
    status_code=status.HTTP_200_OK,
    summary="Turn a text prompt into a ParsedTaskPlan, no db writes",
)
def generate_task(
    body: GenerateTaskRequest,
    parser: AIParser = Depends(get_ai_parser),
) -> ParsedTaskPlan:
    """Call the LLM and return the plan as is.

    Frontend should handle three cases:
    - plan.clarification_needed is a string: show it as the assistant reply,
      then send the user answer plus the old prompt back here.
    - plan.clarification_needed is null and plan.commands has items: ready to
      send to POST /orchestrator/tasks/fan-out.
    - plan.clarification_needed is null and plan.commands is empty: the model
      could not handle the request; plan.summary has the reason.
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
