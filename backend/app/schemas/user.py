import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserBase(BaseModel):
    email: EmailStr


class UserCreate(UserBase):
    password: str = Field(..., min_length=8)


class UserRead(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime


class MeRead(UserBase):
    """The authenticated user's own profile, used by the frontend after login.

    Adds the billing tier and admin flag on top of the basic user fields so
    the UI can show the right badge and unlock the admin panel.
    """

    id: uuid.UUID
    created_at: datetime
    tier: str = "free"
    is_admin: bool = False
    agents_limit: int = 1
