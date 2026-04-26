import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.asset import AssetStatus


# ── MediaFolder ─────────────────────────────────────────────────────────
class MediaFolderBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class MediaFolderCreate(MediaFolderBase):
    user_id: uuid.UUID


class MediaFolderRead(MediaFolderBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime


# ── Asset ───────────────────────────────────────────────────────────────
class AssetBase(BaseModel):
    file_path: str
    metadata_: dict[str, Any] | None = Field(None, alias="metadata")
    is_unique: bool = False
    status: AssetStatus = AssetStatus.RAW
    folder_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None


class AssetCreate(AssetBase):
    user_id: uuid.UUID


class AssetRead(AssetBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime


class UniqueizeRequest(BaseModel):
    """Optional body for ``POST /media/{asset_id}/uniqueize``."""

    copies: int = Field(default=10, ge=1, le=50)
