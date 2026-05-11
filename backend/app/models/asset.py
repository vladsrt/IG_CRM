from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User

# enums
class AssetStatus(str, enum.Enum):
    """Asset row states.

    raw: file is uploaded, no variants yet
    processing: an ffmpeg worker is making variants right now
    ready: the original (with variants attached) or one of the variants
    """

    RAW = "raw"
    PROCESSING = "processing"
    READY = "ready"


# media folder
class MediaFolder(Base):
    """User folder that groups media assets together."""

    __tablename__ = "media_folders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # relations
    assets: Mapped[list[Asset]] = relationship(
        back_populates="folder",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<MediaFolder {self.name}>"


# asset
class Asset(Base):
    """A media file uploaded by the user (image, video, etc).

    Assets are a parent-children tree. The fresh upload is the "parent" (raw
    original). The ffmpeg-made unique clones are children, each one linked
    back through parent_id.
    """

    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('raw', 'processing', 'ready')",
            name="ck_assets_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_folders.id", ondelete="SET NULL"),
        nullable=True,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=True,
    )

    file_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AssetStatus.RAW.value,
        server_default=AssetStatus.RAW.value,
    )
    is_unique: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # relations
    user: Mapped[User] = relationship(
        back_populates="assets",
    )
    folder: Mapped[MediaFolder | None] = relationship(
        back_populates="assets",
    )
    parent: Mapped[Asset | None] = relationship(
        "Asset",
        remote_side="Asset.id",
        back_populates="children",
    )
    children: Mapped[list[Asset]] = relationship(
        "Asset",
        back_populates="parent",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Asset {self.file_path} status={self.status}>"
