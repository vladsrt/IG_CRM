"""Strict Pydantic models for the LLM-produced task plan.

Why ``args`` is a list of pairs instead of a ``dict``
─────────────────────────────────────────────────────
OpenAI's Structured Outputs feature operates in *strict* JSON-schema mode,
which forbids ``additionalProperties: true``. That means an open-ended
``dict[str, Any]`` cannot round-trip through ``response_format=...``.
The portable workaround is to model arguments as a list of typed key/value
pairs and convert to a regular ``dict`` after parsing — see
``ActionCommand.args_as_dict()``.

Why ``clarification_needed`` is ``str | None`` (not omitted when null)
─────────────────────────────────────────────────────────────────────
OpenAI strict mode requires every property to appear in ``required``. A
truly optional field is impossible. We instead model it as a nullable
field — the LLM MUST emit a value, but is allowed (and instructed) to
emit ``null`` when no clarification is required.
"""

from __future__ import annotations

import enum
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── Enumerations ────────────────────────────────────────────────────────
class ActionType(str, enum.Enum):
    """Whitelisted browser actions the AI is allowed to plan."""

    UPLOAD_REELS = "upload_reels"
    UPLOAD_POST = "upload_post"
    UPLOAD_STORY = "upload_story"
    SEND_DM = "send_dm"
    LIKE_POST = "like_post"
    FOLLOW_USER = "follow_user"
    UNFOLLOW_USER = "unfollow_user"
    COMMENT_POST = "comment_post"
    WARMUP = "warmup"
    # Epic 9 — profile + privacy edits at /accounts/edit/ and the
    # privacy settings page. Args (all optional, at least one required):
    #   * bio          (str)  — new bio text; spintax is resolved upstream
    #   * avatar_path  (str)  — absolute path to a local image file
    #   * is_private   (bool) — desired privacy state; null means "leave alone"
    UPDATE_PROFILE = "update_profile"


class TaskPriority(str, enum.Enum):
    """Inferred urgency. Mapped to the integer ``Task.priority`` column."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


PRIORITY_TO_INT: dict[TaskPriority, int] = {
    TaskPriority.LOW: 0,
    TaskPriority.NORMAL: 5,
    TaskPriority.HIGH: 10,
    TaskPriority.URGENT: 20,
}


# ── Argument primitive ──────────────────────────────────────────────────
class ActionArgument(BaseModel):
    """A single key/value pair on an action's argument list.

    ``value`` is always a string. For non-string arguments (numbers, bools,
    nested objects) the LLM is instructed to JSON-encode the value; the
    helper on ``ActionCommand`` re-decodes opportunistically.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="Argument name, e.g. 'caption' or 'url'.")
    value: str = Field(
        description=(
            "Argument value as a string. JSON-encode lists/objects/numbers "
            "if you need a non-string type."
        ),
    )


# ── Action command ──────────────────────────────────────────────────────
class ActionCommand(BaseModel):
    """A single browser action the worker should execute."""

    model_config = ConfigDict(extra="forbid")

    action: ActionType = Field(description="The browser action to execute.")
    args: list[ActionArgument] = Field(
        default_factory=list,
        description="Arguments needed by the action.",
    )

    def args_as_dict(self) -> dict[str, Any]:
        """Materialize ``args`` as a Python dict, decoding JSON values where possible."""
        out: dict[str, Any] = {}
        for arg in self.args:
            try:
                out[arg.key] = json.loads(arg.value)
            except (ValueError, TypeError):
                out[arg.key] = arg.value
        return out


# ── Root model returned by the LLM ──────────────────────────────────────
class ParsedTaskPlan(BaseModel):
    """Root schema the LLM is contractually obliged to produce."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="One-sentence human-readable summary of what was understood.",
    )
    priority: TaskPriority = Field(
        description="Inferred urgency of the task as a whole.",
    )
    target_tags: list[str] = Field(
        default_factory=list,
        description=(
            "Lowercase account tags this plan should fan out to "
            "(e.g. ['crypto', 'tier1']). Empty list means: defer targeting "
            "to the explicit account_ids the caller passes to fan-out."
        ),
    )
    clarification_needed: str | None = Field(
        default=None,
        description=(
            "If the prompt is ambiguous or missing critical information "
            "(no file for an upload, no recipient for a DM, vague targeting, "
            "etc.), put a single polite question here ending in '?' and "
            "leave `commands` empty. The frontend will show this to the "
            "operator as a chat reply. Set to null when the plan is complete."
        ),
    )
    commands: list[ActionCommand] = Field(
        default_factory=list,
        description=(
            "Ordered list of browser actions to execute. MUST be empty when "
            "`clarification_needed` is set."
        ),
    )

    # ── Derived helpers ────────────────────────────────────────────────
    def is_actionable(self) -> bool:
        """True when the plan can actually be dispatched (no clarification, ≥1 command)."""
        return self.clarification_needed is None and len(self.commands) > 0

    def to_payload_dict(self) -> dict[str, Any]:
        """Serialize the plan into the dict that gets stored on ``Task.payload``.

        ``clarification_needed`` is intentionally excluded — it's planning
        metadata, not something the browser worker should consume.
        """
        return {
            "summary": self.summary,
            "priority": self.priority.value,
            "target_tags": list(self.target_tags),
            "commands": [
                {"action": cmd.action.value, "args": cmd.args_as_dict()}
                for cmd in self.commands
            ],
        }

    def priority_as_int(self) -> int:
        """Integer mapping suitable for ``Task.priority``."""
        return PRIORITY_TO_INT[self.priority]


# ── API request schema ──────────────────────────────────────────────────
class GenerateTaskRequest(BaseModel):
    """Request body for ``POST /ai/generate-task``.

    ``account_id`` is now OPTIONAL: since this endpoint no longer creates a
    Task in the DB (Human-in-the-Loop refactor), the field is purely a
    contextual hint the frontend may pass through. Targeting at dispatch
    time is handled by ``POST /orchestrator/tasks/fan-out``.
    """

    model_config = ConfigDict(extra="forbid")

    user_prompt: str = Field(min_length=1, max_length=4000)
    account_id: str | None = Field(
        default=None,
        description="Optional UUID of an InstagramAccount for context only.",
    )
