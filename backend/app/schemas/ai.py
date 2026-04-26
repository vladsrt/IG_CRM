"""Strict Pydantic models for the LLM-produced task plan.

Why ``args`` is a list of pairs instead of a ``dict``
─────────────────────────────────────────────────────
OpenAI's Structured Outputs feature operates in *strict* JSON-schema mode,
which forbids ``additionalProperties: true``. That means an open-ended
``dict[str, Any]`` cannot round-trip through ``response_format=...``.
The portable workaround is to model arguments as a list of typed key/value
pairs and convert to a regular ``dict`` after parsing — see
``ActionCommand.args_as_dict()``.
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
        description="One-sentence human-readable summary of the user's intent.",
    )
    priority: TaskPriority = Field(
        description="Inferred urgency of the task as a whole.",
    )
    commands: list[ActionCommand] = Field(
        description="Ordered list of browser actions to execute.",
    )

    def to_payload_dict(self) -> dict[str, Any]:
        """Serialize the plan into the dict that gets stored on ``Task.payload``."""
        return {
            "summary": self.summary,
            "priority": self.priority.value,
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
    """Request body for ``POST /ai/generate-task``."""

    model_config = ConfigDict(extra="forbid")

    user_prompt: str = Field(min_length=1, max_length=4000)
    account_id: str = Field(
        description="UUID of the InstagramAccount this plan should run against.",
    )
