import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.task import TaskStatus


class TaskBase(BaseModel):
    status: TaskStatus = TaskStatus.DRAFT
    payload: dict[str, Any] | None = None
    priority: int = 0


class TaskCreate(TaskBase):
    account_id: uuid.UUID


class TaskRead(TaskBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    error_log: str | None = None
    created_at: datetime
