"""CRUD for ``MediaFolder`` and ``Asset`` (Sprint 5)."""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.asset import Asset, AssetStatus, MediaFolder
from app.models.user import User
from app.schemas.asset import MediaFolderCreate


# ── MediaFolder ─────────────────────────────────────────────────────────
def get_folder(db: Session, folder_id: uuid.UUID) -> MediaFolder | None:
    return db.get(MediaFolder, folder_id)

#list
def list_folders(
    db: Session,
    *,
    user_id: uuid.UUID | None = None,
    skip: int = 0,
    limit: int = 100,
) -> Sequence[MediaFolder]:
    stmt = select(MediaFolder)
    if user_id is not None:
        stmt = stmt.where(MediaFolder.user_id == user_id)
    stmt = stmt.order_by(MediaFolder.created_at.desc()).offset(skip).limit(limit)
    return db.execute(stmt).scalars().all()


def create_folder(db: Session, folder_in: MediaFolderCreate) -> MediaFolder:
    if db.get(User, folder_in.user_id) is None:
        raise ValueError(f"User {folder_in.user_id} does not exist")

    folder = MediaFolder(**folder_in.model_dump(mode="json"))
    db.add(folder)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not create folder (constraint violation)") from exc
    db.refresh(folder)
    return folder


def delete_folder(db: Session, folder_id: uuid.UUID) -> bool:
    folder = get_folder(db, folder_id)
    if folder is None:
        return False
    db.delete(folder)
    db.commit()
    return True


# ── Asset ───────────────────────────────────────────────────────────────
def get_asset(db: Session, asset_id: uuid.UUID) -> Asset | None:
    return db.get(Asset, asset_id)


def list_assets(
    db: Session,
    *,
    user_id: uuid.UUID | None = None,
    folder_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    status: AssetStatus | None = None,
    skip: int = 0,
    limit: int = 100,
) -> Sequence[Asset]:
    stmt = select(Asset)
    if user_id is not None:
        stmt = stmt.where(Asset.user_id == user_id)
    if folder_id is not None:
        stmt = stmt.where(Asset.folder_id == folder_id)
    if parent_id is not None:
        stmt = stmt.where(Asset.parent_id == parent_id)
    if status is not None:
        stmt = stmt.where(Asset.status == status.value)
    stmt = stmt.order_by(Asset.created_at.desc()).offset(skip).limit(limit)
    return db.execute(stmt).scalars().all()


def create_asset(
    db: Session,
    *,
    user_id: uuid.UUID,
    file_path: str,
    folder_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    status: AssetStatus = AssetStatus.RAW,
    is_unique: bool = False,
    metadata: dict[str, Any] | None = None,
) -> Asset:
    if db.get(User, user_id) is None:
        raise ValueError(f"User {user_id} does not exist")
    if folder_id is not None and db.get(MediaFolder, folder_id) is None:
        raise ValueError(f"MediaFolder {folder_id} does not exist")
    if parent_id is not None and db.get(Asset, parent_id) is None:
        raise ValueError(f"Parent Asset {parent_id} does not exist")

    asset = Asset(
        user_id=user_id,
        folder_id=folder_id,
        parent_id=parent_id,
        file_path=file_path,
        status=status.value,
        is_unique=is_unique,
        metadata_=metadata,
    )
    db.add(asset)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not create asset (constraint violation)") from exc
    db.refresh(asset)
    return asset


def set_asset_status(
    db: Session, asset_id: uuid.UUID, new_status: AssetStatus
) -> Asset | None:
    asset = get_asset(db, asset_id)
    if asset is None:
        return None
    asset.status = new_status.value
    db.commit()
    db.refresh(asset)
    return asset


def delete_asset(db: Session, asset_id: uuid.UUID) -> bool:
    asset = get_asset(db, asset_id)
    if asset is None:
        return False
    db.delete(asset)
    db.commit()
    return True
