import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AssetBase(BaseModel):
    file_path: str
    metadata_: dict[str, Any] | None = Field(None, alias="metadata")
    is_unique: bool = False


class AssetCreate(AssetBase):
    user_id: uuid.UUID


class AssetRead(AssetBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    user_id: uuid.UUID
