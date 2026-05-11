"""Pydantic models for the task plan the LLM gives us back.

Why args is a list of pairs, not a dict:
OpenAI structured outputs run in strict json-schema mode which forbids
additionalProperties: true. So an open dict[str, Any] can not be sent through
response_format=... . The fix is to model args as a list of typed key/value
pairs and turn it into a normal dict after parsing. See
ActionCommand.args_as_dict().

Why clarification_needed is str | None (and not just left out when null):
strict mode wants every property in `required`. There is no real optional
field. We instead make it nullable: the LLM must always set something, but
can set null when no clarification is needed.
"""

from __future__ import annotations

import enum
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# enums
class ActionType(str, enum.Enum):
    """Browser actions the AI is allowed to plan."""

    UPLOAD_REELS = "upload_reels"
    UPLOAD_POST = "upload_post"
    UPLOAD_STORY = "upload_story"
    SEND_DM = "send_dm"
    LIKE_POST = "like_post"
    FOLLOW_USER = "follow_user"
    UNFOLLOW_USER = "unfollow_user"
    COMMENT_POST = "comment_post"
    WARMUP = "warmup"
    # profile and privacy edits on /accounts/edit/ and the privacy page.
    # args, all optional but at least one required:
    #   bio (str): new bio text. spintax is already resolved before this.
    #   avatar_path (str): absolute path to a local image file.
    #   is_private (bool): wanted privacy state. null means do not touch it.
    UPDATE_PROFILE = "update_profile"


class TaskPriority(str, enum.Enum):
    """Guessed urgency. Maps to the int Task.priority column."""

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


# argument primitive
class ActionArgument(BaseModel):
    """One key/value pair from the action's arguments.

    `value` is always a string. For non-string args (numbers, bools, nested
    objects) the LLM is told to json encode the value. The helper on
    ActionCommand tries to decode it back when it can.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="Arg name, like 'caption' or 'url'.")
    value: str = Field(
        description=(
            "Arg value as a string. JSON-encode lists, objects or numbers if "
            "you need a non-string type."
        ),
    )


# action command
class ActionCommand(BaseModel):
    """One browser action the worker has to run."""

    model_config = ConfigDict(extra="forbid")

    action: ActionType = Field(description="Browser action to run.")
    args: list[ActionArgument] = Field(
        default_factory=list,
        description="Args this action needs.",
    )

    def args_as_dict(self) -> dict[str, Any]:
        """Convert args list into a dict, decoding json values when possible."""
        out: dict[str, Any] = {}
        for arg in self.args:
            try:
                out[arg.key] = json.loads(arg.value)
            except (ValueError, TypeError):
                out[arg.key] = arg.value
        return out


# root model the LLM has to return
class ParsedTaskPlan(BaseModel):
    """Root schema the LLM has to produce."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="Short one-line summary of what was understood.",
    )
    priority: TaskPriority = Field(
        description="Guessed urgency for the whole task.",
    )
    target_tags: list[str] = Field(
        default_factory=list,
        description=(
            "Lowercase account tags this plan should target, like "
            "['crypto', 'tier1']. Empty list = use only the explicit "
            "account_ids the caller sends to fan-out."
        ),
    )
    clarification_needed: str | None = Field(
        default=None,
        description=(
            "If the prompt is unclear or missing key info (no file for an "
            "upload, no recipient for a DM, fuzzy targeting...), put one "
            "short polite question here ending with '?' and leave `commands` "
            "empty. The frontend will show this back to the user. Set to "
            "null when the plan is ready."
        ),
    )
    commands: list[ActionCommand] = Field(
        default_factory=list,
        description=(
            "Ordered list of browser actions to run. Must be empty when "
            "`clarification_needed` is set."
        ),
    )

    # helpers
    def is_actionable(self) -> bool:
        """True if the plan can be dispatched: no clarification and at least one command."""
        return self.clarification_needed is None and len(self.commands) > 0

    def to_payload_dict(self) -> dict[str, Any]:
        """Turn the plan into the dict we store on Task.payload.

        We drop `clarification_needed` here, that field is for planning only,
        the browser worker has no use for it.
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
        """Int that fits Task.priority."""
        return PRIORITY_TO_INT[self.priority]


# api request schema
class GenerateTaskRequest(BaseModel):
    """Request body for POST /ai/generate-task.

    account_id is optional. This endpoint no longer creates a Task in the db
    (after the human-in-the-loop change), so the field is just a hint the
    frontend can pass for context. Real targeting at dispatch time is done by
    POST /orchestrator/tasks/fan-out.
    """

    model_config = ConfigDict(extra="forbid")

    user_prompt: str = Field(min_length=1, max_length=4000)
    account_id: str | None = Field(
        default=None,
        description="Optional InstagramAccount UUID, for context only.",
    )
