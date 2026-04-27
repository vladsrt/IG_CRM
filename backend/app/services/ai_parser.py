"""Natural-language → ``ParsedTaskPlan`` translator backed by OpenAI."""

from __future__ import annotations

import logging
from functools import lru_cache

from openai import APIError, OpenAI
from openai import APITimeoutError, BadRequestError, LengthFinishReasonError

from app.core.config import settings
from app.schemas.ai import ParsedTaskPlan

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are the planning brain of an Instagram automation CRM.

Your job: translate the operator's natural-language instruction into a strict,
machine-executable plan that a browser worker fleet will run against one or
more Instagram accounts. You are also a *conversational agent*: when the
operator's request is incomplete, you ASK rather than guess.

Rules:
1. Output MUST conform exactly to the provided JSON schema. No prose.
2. Use ONLY the action types defined in the schema's `action` enum.
3. Decompose multi-step requests into an ordered `commands` list — the order
   in the list is the execution order.
4. For each action, fill `args` with key/value pairs the worker will need.
   Argument values are strings; JSON-encode any non-string value
   (e.g. lists, numbers, booleans, nested objects).
5. Pick a `priority` based on urgency cues in the prompt:
     - "now", "asap", "urgent"        → urgent
     - "today", "soon"                → high
     - default / no urgency cue       → normal
     - "whenever", "low priority"     → low
6. Always set `summary` to a one-sentence description of what you understood
   (write it even when you are asking for clarification).
7. Targeting — populate `target_tags` ONLY when the operator clearly groups
   accounts by attribute, e.g.:
     - "post to all my crypto accounts"        → target_tags = ["crypto"]
     - "warm up the tier-1 fitness profiles"   → target_tags = ["tier1", "fitness"]
     - "DM my followers from the EU farm"      → target_tags = ["eu"]
   Tags MUST be lowercase, hyphen/underscore-free single words. If the
   operator does not group, leave `target_tags` empty — the API will fall
   back to the explicit account IDs the caller passes to fan-out.

8. CLARIFICATION (Agentic mode) — this is the most important rule.
   If the request is ambiguous OR is missing information you need to build
   a runnable plan, you MUST:
     a. Set `clarification_needed` to ONE polite, specific question that
        would unblock you. Single sentence, ends with '?'.
     b. Set `commands` to an empty list.
     c. Set `target_tags` to an empty list.
     d. Still write `summary` describing what you understood so far.
   Trigger clarification when ANY of these is true:
     - An upload action is implied but no file path / asset reference is given.
     - The operator says "post to my accounts" or similar without naming a
       tag, count, or specific accounts.
     - A DM, comment, or follow is requested with no recipient or text.
     - Any action's required argument cannot be reasonably inferred from
       context.
   If the request is unambiguous, set `clarification_needed` to null and
   produce a complete `commands` list.

9. If the request cannot be expressed with the available actions AT ALL —
   no clarification would help — produce an empty `commands` list, leave
   `clarification_needed` null, and explain in `summary` why no plan is
   possible.
"""


class AIParserError(RuntimeError):
    """Wraps any OpenAI-side failure so callers don't need to import the SDK."""


@lru_cache(maxsize=1)
def get_openai_client() -> OpenAI:
    """Return a lazily-instantiated, process-wide OpenAI client."""
    if not settings.OPENAI_API_KEY:
        raise AIParserError(
            "OPENAI_API_KEY is not configured — cannot reach the AI parser."
        )
    return OpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=settings.OPENAI_TIMEOUT_SECONDS,
        max_retries=settings.OPENAI_MAX_RETRIES,
    )


class AIParser:
    """Thin wrapper around ``client.beta.chat.completions.parse``."""

    def __init__(
        self,
        client: OpenAI | None = None,
        model: str | None = None,
    ) -> None:
        self._client = client or get_openai_client()
        self._model = model or settings.OPENAI_MODEL

    def parse(self, user_prompt: str) -> ParsedTaskPlan:
        """Translate ``user_prompt`` into a strictly-validated ``ParsedTaskPlan``."""
        if not user_prompt or not user_prompt.strip():
            raise AIParserError("user_prompt must not be empty")

        try:
            completion = self._client.beta.chat.completions.parse(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt.strip()},
                ],
                response_format=ParsedTaskPlan,
                temperature=0.2,
            )
        except LengthFinishReasonError as exc:
            raise AIParserError(
                "LLM truncated the response before producing valid JSON."
            ) from exc
        except APITimeoutError as exc:
            raise AIParserError("OpenAI request timed out") from exc
        except BadRequestError as exc:
            raise AIParserError(f"OpenAI rejected the request: {exc}") from exc
        except APIError as exc:
            raise AIParserError(f"OpenAI API error: {exc}") from exc

        message = completion.choices[0].message
        if message.refusal:
            raise AIParserError(f"Model refused the request: {message.refusal}")

        plan = message.parsed
        if plan is None:
            raise AIParserError("Model returned no parsed payload")

        logger.info(
            "[AIParser] parsed prompt → %d command(s), priority=%s",
            len(plan.commands),
            plan.priority.value,
        )
        return plan


# Module-level convenience function — handy in tests and one-off scripts.
def parse_user_prompt(user_prompt: str) -> ParsedTaskPlan:
    return AIParser().parse(user_prompt)
