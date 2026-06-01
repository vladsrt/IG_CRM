"""Turn a text prompt into a ParsedTaskPlan using an OpenAI-compatible LLM API.

Provider-agnostic: works with OpenAI, Ollama Cloud, z.ai / GLM, or any endpoint
that speaks the OpenAI ``/v1/chat/completions`` contract. Configure via
``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` / ``OPENAI_MODEL``.

We deliberately do NOT use OpenAI's ``beta.chat.completions.parse`` strict
structured-output path: non-OpenAI providers (Ollama, GLM) don't implement it.
Instead we ask for JSON mode, parse the text manually, and validate it with
Pydantic — with one corrective retry if the first answer doesn't fit the schema.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

from openai import APIError, APITimeoutError, OpenAI
from pydantic import ValidationError

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
1. Respond with a SINGLE JSON object and nothing else. No prose, no markdown,
   no code fences.
2. Use ONLY these action types in `action`:
   upload_reels, upload_post, upload_story, send_dm, like_post, follow_user,
   unfollow_user, comment_post, warmup, update_profile.
3. Decompose multi-step requests into an ordered `commands` list — list order
   is execution order.
4. Each command's `args` is a LIST of {"key": ..., "value": ...} pairs. Every
   value is a STRING; JSON-encode non-string values (numbers, booleans, lists).
   Example: {"key": "duration_minutes", "value": "15"}.
5. Pick `priority` from urgency cues:
     - "now", "asap", "urgent"   -> "urgent"
     - "today", "soon"           -> "high"
     - default / no cue          -> "normal"
     - "whenever", "low priority"-> "low"
6. Always set `summary` to a one-sentence description of what you understood
   (even when asking for clarification).
7. Populate `target_tags` (lowercase single words) ONLY when the operator
   groups accounts by attribute ("all my crypto accounts" -> ["crypto"]).
   Otherwise use an empty list.
8. MEDIA REFERENCES (CRITICAL): for upload_reels / upload_post / upload_story
   you MUST reference media by `media_asset_id` (a UUID from the inventory the
   user message provides), NEVER invent a `file_path`. The orchestrator will
   resolve the id to a per-account unique copy before dispatch.
     - Example: {"key":"media_asset_id","value":"3f9b...-uuid"}
     - If the operator names a file but it is not in the inventory, ASK for
       clarification (do not guess an id).
     - If the inventory is empty and the user asks for an upload, ASK them to
       upload media first.
9. CLARIFICATION (most important): if the request is ambiguous or missing info
   you need (upload with no media, "post to my accounts" with no tag/target, a
   DM/comment with no text or recipient, any required arg you cannot infer):
     - set `clarification_needed` to ONE polite question ending in '?'
     - set `commands` to []
     - set `target_tags` to []
     - still write `summary`.
   When the request is unambiguous, set `clarification_needed` to null and
   produce a complete `commands` list.
10. If the request cannot be expressed with the available actions at all and no
    clarification would help: empty `commands`, `clarification_needed` null,
    explain why in `summary`.

The JSON object MUST have exactly these top-level keys:
  "summary" (string),
  "priority" (one of: "low","normal","high","urgent"),
  "target_tags" (array of strings),
  "clarification_needed" (string or null),
  "commands" (array of {"action": string, "args": [{"key": string, "value": string}]}).

Example of a ready plan:
{"summary":"Warm up crypto accounts then post the reel","priority":"high",
 "target_tags":["crypto"],"clarification_needed":null,
 "commands":[{"action":"warmup","args":[{"key":"duration_minutes","value":"15"}]},
 {"action":"upload_reels","args":[
   {"key":"media_asset_id","value":"3f9b1c52-bb47-4a1f-9d20-6c4d8b1e9e10"},
   {"key":"caption","value":"gm"}]}]}

Example asking for clarification:
{"summary":"Operator wants to post a reel but no media is available","priority":"normal",
 "target_tags":[],"clarification_needed":"Which video do you want me to post?",
 "commands":[]}
"""


class AIParserError(RuntimeError):
    """Wraps any LLM-side error so callers do not import the SDK."""


@lru_cache(maxsize=1)
def get_openai_client() -> OpenAI:
    """Build the LLM client on first use and reuse it across the process."""
    if not settings.OPENAI_API_KEY:
        raise AIParserError(
            "OPENAI_API_KEY is not set, cannot reach the AI parser."
        )
    kwargs: dict = {
        "api_key": settings.OPENAI_API_KEY,
        "timeout": settings.OPENAI_TIMEOUT_SECONDS,
        "max_retries": settings.OPENAI_MAX_RETRIES,
    }
    if settings.OPENAI_BASE_URL:
        kwargs["base_url"] = settings.OPENAI_BASE_URL
    return OpenAI(**kwargs)


def _strip_to_json(text: str) -> str:
    """Best-effort cleanup: drop code fences and grab the outermost {...}."""
    t = text.strip()
    if t.startswith("```"):
        # remove ```json ... ``` fences
        t = t.split("```", 2)
        t = t[1] if len(t) > 1 else text
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    t = t.strip()
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start : end + 1]
    return t


class AIParser:
    """Provider-agnostic JSON-mode wrapper over chat.completions.create."""

    def __init__(
        self,
        client: OpenAI | None = None,
        model: str | None = None,
    ) -> None:
        self._client = client or get_openai_client()
        self._model = model or settings.OPENAI_MODEL

    def _complete(self, messages: list[dict], *, json_mode: bool) -> str:
        """One chat call. Falls back gracefully if the provider rejects
        response_format (some OpenAI-compatible servers don't support it)."""
        kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.2,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            completion = self._client.chat.completions.create(**kwargs)
        except (TypeError, APIError) as exc:
            if json_mode:
                logger.warning(
                    "[AIParser] response_format unsupported (%s); retrying "
                    "without JSON mode.", type(exc).__name__,
                )
                kwargs.pop("response_format", None)
                completion = self._client.chat.completions.create(**kwargs)
            else:
                raise
        content = completion.choices[0].message.content
        if not content:
            raise AIParserError("Model returned an empty response")
        return content

    def parse(
        self,
        user_prompt: str,
        media_inventory: str | None = None,
    ) -> ParsedTaskPlan:
        """Turn user_prompt into a validated ParsedTaskPlan (one retry).

        media_inventory: optional pre-formatted listing of the caller's media
        assets. Injected as a system message so the LLM can pick a real
        media_asset_id instead of hallucinating a file path.
        """
        if not user_prompt or not user_prompt.strip():
            raise AIParserError("user_prompt must not be empty")

        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if media_inventory:
            messages.append({"role": "system", "content": media_inventory})
        messages.append({"role": "user", "content": user_prompt.strip()})

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw = self._complete(messages, json_mode=True)
            except APITimeoutError as exc:
                raise AIParserError("LLM request timed out") from exc
            except APIError as exc:
                raise AIParserError(f"LLM API error: {exc}") from exc

            cleaned = _strip_to_json(raw)
            try:
                data = json.loads(cleaned)
                plan = ParsedTaskPlan.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                logger.warning(
                    "[AIParser] attempt %d produced invalid plan: %s",
                    attempt + 1, exc,
                )
                # feed the error back so the model can self-correct
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        "That was not valid. Return ONLY a corrected JSON "
                        f"object matching the schema. Error: {exc}"
                    ),
                })
                continue

            logger.info(
                "[AIParser] parsed prompt -> %d command(s), priority=%s",
                len(plan.commands), plan.priority.value,
            )
            return plan

        raise AIParserError(
            f"Model did not return a schema-valid plan after 2 tries: {last_error}"
        )


# shortcut for tests and small scripts
def parse_user_prompt(user_prompt: str) -> ParsedTaskPlan:
    return AIParser().parse(user_prompt)
