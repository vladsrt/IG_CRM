import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.account import AuthMethod, Platform


class _AccountPublic(BaseModel):
    """Non-secret fields, safe to return to the client."""

    ig_username: str
    auth_method: AuthMethod
    proxy_session_id: str | None = None
    status: str | None = None
    tags: list[str] = Field(default_factory=list)
    platform: Platform = Platform.WINDOWS
    user_agent: str | None = None


class InstagramAccountBase(_AccountPublic):
    """Public fields plus the secrets — used for create/input only."""

    ig_password: str
    cookies: list | None = None


class InstagramAccountCreate(InstagramAccountBase):
    user_id: uuid.UUID
    proxy_id: uuid.UUID | None = None


class InstagramAccountUpdate(BaseModel):
    """PATCH body. All fields optional, CRUD uses exclude_unset."""

    model_config = ConfigDict(extra="forbid")

    ig_username: str | None = None
    ig_password: str | None = None
    auth_method: AuthMethod | None = None
    proxy_id: uuid.UUID | None = None
    proxy_session_id: str | None = None
    cookies: list | None = None
    status: str | None = None
    error_log: str | None = None
    tags: list[str] | None = Field(default=None)
    platform: Platform | None = None
    user_agent: str | None = None


class InstagramAccountRead(_AccountPublic):
    """Response model. Deliberately omits ig_password and cookies so the API
    never returns account secrets."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    proxy_id: uuid.UUID | None = None
    error_log: str | None = None
    last_check: datetime | None = None
    # non-secret indicators so the UI can show "cookies stored" without leaking them
    has_cookies: bool = False
    has_password: bool = False
