import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict

class SubscriptionBase(BaseModel):
    tier: str = "free"
    valid_until: datetime | None = None
    is_active: bool = True

class SubscriptionCreate(SubscriptionBase):
    user_id: uuid.UUID

class SubscriptionUpdate(BaseModel):
    tier: str | None = None
    valid_until: datetime | None = None
    is_active: bool | None = None

class SubscriptionRead(SubscriptionBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    updated_at: datetime
