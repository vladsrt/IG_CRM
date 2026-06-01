"""AI plan generation endpoint, human in the loop.

No side effects here. We just take a user prompt, turn it into a ParsedTaskPlan,
and send it back to the frontend so the user can check it. Actual task save and
Celery dispatch happens in POST /orchestrator/tasks/fan-out.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.models.asset import Asset, AssetStatus
from app.models.user import User
from app.schemas.ai import GenerateTaskRequest, ParsedTaskPlan
from app.services.ai_parser import AIParser, AIParserError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


def get_ai_parser() -> AIParser:
    return AIParser()


# How many media rows to feed the LLM. Big enough for normal libraries, small
# enough to keep the prompt cheap. Newest first.
_MEDIA_INVENTORY_LIMIT: int = 50


def _build_media_inventory(db: Session, user_id) -> str | None:
    """Format the user's media library as a short text block for the LLM.

    Only parent assets (parent_id IS NULL) are listed — the orchestrator picks
    which variant goes to which account at dispatch. Returns None if the user
    has no media (the system prompt already tells the LLM how to handle that).
    """
    rows = db.execute(
        select(Asset)
        .where(
            Asset.user_id == user_id,
            Asset.parent_id.is_(None),
            Asset.status == AssetStatus.READY.value,
        )
        .order_by(Asset.created_at.desc())
        .limit(_MEDIA_INVENTORY_LIMIT)
    ).scalars().all()

    if not rows:
        return (
            "MEDIA INVENTORY: (empty). The operator has no uploaded media. "
            "If they ask for an upload, set clarification_needed asking them "
            "to upload media first."
        )

    lines: list[str] = [
        "MEDIA INVENTORY (use these media_asset_id values for any upload action):",
    ]
    for a in rows:
        name = os.path.basename(a.file_path or "") or str(a.id)
        kind = (a.metadata_ or {}).get("kind") or "media"
        lines.append(f"- id={a.id} name={name!r} kind={kind}")
    return "\n".join(lines)


@router.post(
    "/generate-task",
    response_model=ParsedTaskPlan,
    status_code=status.HTTP_200_OK,
    summary="Turn a text prompt into a ParsedTaskPlan, no db writes",
)
def generate_task(
    body: GenerateTaskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
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
    inventory = _build_media_inventory(db, current_user.id)
    try:
        plan = parser.parse(body.user_prompt, media_inventory=inventory)
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
