"""Media routes: folders, raw uploads, ffmpeg uniqueize trigger."""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.crud import media as crud_media
from app.models.asset import AssetStatus
from app.schemas.asset import (
    AssetRead,
    MediaFolderCreate,
    MediaFolderRead,
    UniqueizeRequest,
)
from app.workers.media_tasks import uniqueize_video

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/media", tags=["media"])


# helpers
_ALLOWED_VIDEO_SUFFIXES: frozenset[str] = frozenset(
    {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
)


class UniqueizeDispatchResponse(BaseModel):
    celery_task_id: str
    asset_id: str
    copies: int
    detail: str


def _media_root() -> Path:
    root = Path(settings.MEDIA_ROOT).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _save_upload_to_disk(upload: UploadFile, dest: Path) -> int:
    """Stream upload into dest. Returns how many bytes we wrote."""
    written = 0
    cap = settings.MEDIA_MAX_UPLOAD_BYTES
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > cap:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Upload exceeds {cap} bytes",
                )
            fh.write(chunk)
    return written


# folder routes
@router.post(
    "/folders",
    response_model=MediaFolderRead,
    status_code=status.HTTP_201_CREATED,
)
def create_folder(
    folder_in: MediaFolderCreate,
    db: Session = Depends(get_db),
) -> MediaFolderRead:
    try:
        folder = crud_media.create_folder(db, folder_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return MediaFolderRead.model_validate(folder)


@router.get("/folders", response_model=list[MediaFolderRead])
def list_folders(
    user_id: uuid.UUID | None = Query(default=None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[MediaFolderRead]:
    rows = crud_media.list_folders(db, user_id=user_id, skip=skip, limit=limit)
    return [MediaFolderRead.model_validate(f) for f in rows]


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    folder_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Response:
    if not crud_media.delete_folder(db, folder_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="MediaFolder not found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# asset routes
@router.get("/assets", response_model=list[AssetRead])
def list_assets(
    user_id: uuid.UUID | None = Query(default=None),
    folder_id: uuid.UUID | None = Query(default=None),
    parent_id: uuid.UUID | None = Query(default=None),
    asset_status: AssetStatus | None = Query(default=None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[AssetRead]:
    rows = crud_media.list_assets(
        db,
        user_id=user_id,
        folder_id=folder_id,
        parent_id=parent_id,
        status=asset_status,
        skip=skip,
        limit=limit,
    )
    return [AssetRead.model_validate(a) for a in rows]


@router.get("/assets/{asset_id}", response_model=AssetRead)
def get_asset(
    asset_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> AssetRead:
    asset = crud_media.get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found"
        )
    return AssetRead.model_validate(asset)


@router.post(
    "/upload",
    response_model=AssetRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload raw video and create parent Asset row",
)
def upload_video(
    user_id: uuid.UUID = Form(...),
    folder_id: uuid.UUID | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> AssetRead:
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="filename missing"
        )
    suffix = Path(file.filename).suffix.lower()
    if suffix not in _ALLOWED_VIDEO_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported video extension {suffix!r}. "
                f"Allowed: {sorted(_ALLOWED_VIDEO_SUFFIXES)}"
            ),
        )

    asset_id = uuid.uuid4()
    dest = _media_root() / "originals" / f"{asset_id.hex}{suffix}"

    try:
        bytes_written = _save_upload_to_disk(file, dest)
    finally:
        file.file.close()

    try:
        asset = crud_media.create_asset(
            db,
            user_id=user_id,
            folder_id=folder_id,
            file_path=str(dest),
            status=AssetStatus.RAW,
            is_unique=False,
            metadata={
                "original_filename": file.filename,
                "content_type": file.content_type,
                "size_bytes": bytes_written,
            },
        )
    except ValueError as exc:
        # bad user or folder fk, drop the orphan file
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return AssetRead.model_validate(asset)


@router.post(
    "/{asset_id}/uniqueize",
    response_model=UniqueizeDispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue ffmpeg job that makes N byte different variants",
)
def trigger_uniqueize(
    asset_id: uuid.UUID,
    copies: int = Query(default=10, ge=1, le=50),
    body: UniqueizeRequest | None = None,
    db: Session = Depends(get_db),
) -> UniqueizeDispatchResponse:
    # if body is sent, it wins over query param
    if body is not None and body.copies:
        copies = body.copies

    asset = crud_media.get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found"
        )
    if asset.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot uniqueize a variant, pass the root parent Asset",
        )
    if asset.status == AssetStatus.PROCESSING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Asset is already being processed",
        )

    if not Path(asset.file_path).is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Source file missing on disk: {asset.file_path}",
        )

    try:
        async_result = uniqueize_video.delay(str(asset.id), copies)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not dispatch FFmpeg job: {exc}",
        ) from exc

    return UniqueizeDispatchResponse(
        celery_task_id=async_result.id,
        asset_id=str(asset.id),
        copies=copies,
        detail=f"Uniqueize job queued for asset {asset.id}",
    )
