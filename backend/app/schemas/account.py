import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.account import AuthMethod, Platform


class InstagramAccountBase(BaseModel):
    ig_username: str
    ig_password: str
    auth_method: AuthMethod
    proxy_session_id: str | None = None
    cookies: list | None = None
    status: str | None = None
    tags: list[str] = Field(default_factory=list)
    platform: Platform = Platform.WINDOWS
    user_agent: str | None = None


class InstagramAccountCreate(InstagramAccountBase):
    user_id: uuid.UUID
    proxy_id: uuid.UUID | None = None


class InstagramAccountUpdate(BaseModel):
    """PATCH payload — every field optional, ``exclude_unset`` semantics in CRUD."""

    model_config = ConfigDict(extra="forbid")

    ig_username: str | None = None
    ig_password: str | None = None
    auth_method: AuthMethod | None = None
    proxy_id: uuid.UUID | None = None
    proxy_session_id: str | None = None
    cookies: dict[str, Any] | None = None
    status: str | None = None
    error_log: str | None = None
    tags: list[str] | None = Field(default=None)
    platform: Platform | None = None
    user_agent: str | None = None


class InstagramAccountRead(InstagramAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    proxy_id: uuid.UUID | None = None
    error_log: str | None = None
    last_check: datetime | None = None
