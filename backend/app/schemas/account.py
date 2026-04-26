import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.account import AuthMethod


class InstagramAccountBase(BaseModel):
    ig_username: str
    ig_password: str
    auth_method: AuthMethod
    proxy_session_id: str | None = None
    cookies: dict[str, Any] | None = None
    status: str | None = None
    tags: list[str] = Field(default_factory=list)


class InstagramAccountCreate(InstagramAccountBase):
    user_id: uuid.UUID
    proxy_id: uuid.UUID | None = None


class InstagramAccountRead(InstagramAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    proxy_id: uuid.UUID | None = None
    error_log: str | None = None
    last_check: datetime | None = None
