"""Celery tasks for media processing. Right now just the ffmpeg uniqueizer.

What uniqueize_video does:
Make `copies` variants of a raw video that look the same but have different
bytes, so every Instagram account can post a clone with its own md5. The
anti-fraud mix:

  -map_metadata -1                  drop every original tag
  -metadata comment=<random uuid>   add a unique fingerprint
  -vf noise=alls=<r>:allf=t         invisible per-frame temporal noise
  -b:v <0.95..1.05 * original>      small bitrate jitter
  -c:v libx264 -preset <random>     re-encode gives a fresh container

Each variant is saved as a child Asset row with parent_id pointing at the
raw parent.
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import subprocess
import traceback
import uuid
from pathlib import Path
from typing import Any

from celery import Task as CeleryTask

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.crud import media as crud_media
from app.models.asset import Asset, AssetStatus

logger = logging.getLogger(__name__)


# knobs
# noise is fixed at 4. higher values (we tried up to 12 in the CLI for
# fingerprint strength tests) visibly hurt the output. 4 is the highest
# value that you still can not see on phone playback, but moves enough
# pixels to beat byte-level dedup. we removed random jitter on noise on
# purpose, keeping it fixed makes regressions easier to reproduce.
_NOISE_STRENGTH: int = 4
_BITRATE_MULTIPLIER_MIN: float = 0.95
_BITRATE_MULTIPLIER_MAX: float = 1.05
_PRESETS: list[str] = ["veryfast", "faster", "fast", "medium"]


# helpers
def _media_root() -> Path:
    root = Path(settings.MEDIA_ROOT).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_binaries_available() -> None:
    if shutil.which(settings.FFMPEG_BIN) is None:
        raise RuntimeError(
            f"ffmpeg binary not found on PATH (looked for {settings.FFMPEG_BIN!r})"
        )
    if shutil.which(settings.FFPROBE_BIN) is None:
        raise RuntimeError(
            f"ffprobe binary not found on PATH (looked for {settings.FFPROBE_BIN!r})"
        )


def _probe_input_bitrate_bps(input_path: Path) -> int:
    """Return the video's bitrate in bits per second, or the fallback value."""
    for entries in ("stream=bit_rate", "format=bit_rate"):
        cmd = [
            settings.FFPROBE_BIN,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", entries,
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, check=False
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning("ffprobe failed (%s): %s", entries, exc)
            continue

        raw = (out.stdout or "").strip().splitlines()[0:1]
        if raw and raw[0].isdigit():
            return int(raw[0])

    logger.info(
        "ffprobe could not read the bitrate, using fallback %d bps",
        settings.FFMPEG_DEFAULT_BITRATE_BPS,
    )
    return settings.FFMPEG_DEFAULT_BITRATE_BPS


def _build_ffmpeg_cmd(
    input_path: Path,
    output_path: Path,
    *,
    bitrate_bps: int,
    noise_strength: int,
    preset: str,
    fingerprint: str,
) -> list[str]:
    return [
        settings.FFMPEG_BIN,
        "-y",                                         # overwrite output
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(input_path),
        "-map_metadata", "-1",                        # drop original metadata
        "-metadata", f"comment={fingerprint}",        # unique fingerprint
        "-metadata", f"title=ig-crm-{fingerprint[:8]}",
        "-vf", f"noise=alls={noise_strength}:allf=t", # invisible noise
        "-c:v", "libx264",
        "-preset", preset,
        "-b:v", f"{bitrate_bps}",                     # bitrate jitter, bits/sec
        "-c:a", "aac",
        "-movflags", "+faststart",                    # mp4 layout IG likes
        str(output_path),
    ]


def _run_ffmpeg(cmd: list[str]) -> None:
    logger.info("[ffmpeg] running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ffmpeg timed out after {settings.FFMPEG_TIMEOUT_SECONDS}s"
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (rc={result.returncode}): {result.stderr.strip()[-2000:]}"
        )


def _variant_output_path(parent: Asset, copy_idx: int, child_id: uuid.UUID) -> Path:
    """Build the on-disk path for a variant file."""
    parent_path = Path(parent.file_path)
    suffix = parent_path.suffix or ".mp4"
    out_dir = _media_root() / "variants" / str(parent.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{copy_idx:03d}_{child_id.hex}{suffix}"


# celery task
@celery_app.task(
    bind=True,
    name="ig_crm.uniqueize_video",
    acks_late=True,
)
def uniqueize_video(
    self: CeleryTask, asset_id: str, copies: int = 10
) -> dict[str, Any]:
    """Make `copies` byte-different variants of the raw video at asset_id."""
    if copies < 1:
        raise ValueError(f"copies must be >= 1, got {copies}")

    asset_uuid = uuid.UUID(asset_id)
    logger.info(
        "[uniqueize_video] asset_id=%s copies=%d", asset_uuid, copies
    )
    _ensure_binaries_available()

    # load parent and switch to PROCESSING
    with SessionLocal() as db:
        parent = crud_media.get_asset(db, asset_uuid)
        if parent is None:
            raise ValueError(f"Asset {asset_uuid} not found")
        if parent.parent_id is not None:
            raise ValueError(
                f"Asset {asset_uuid} is a variant. Run uniqueize on the root parent instead."
            )

        input_path = Path(parent.file_path).expanduser().resolve()
        if not input_path.is_file():
            raise FileNotFoundError(f"Source video missing on disk: {input_path}")

        parent.status = AssetStatus.PROCESSING.value
        db.commit()
        # save fields we will use after the session is closed
        parent_user_id = parent.user_id
        parent_folder_id = parent.folder_id
        parent_id = parent.id

    # probe and build variants
    base_bitrate_bps = _probe_input_bitrate_bps(input_path)
    created_assets: list[dict[str, Any]] = []

    try:
        for copy_idx in range(1, copies + 1):
            child_id = uuid.uuid4()
            output_path = _variant_output_path(
                Asset(id=parent_id, file_path=str(input_path)),  # only the path is used
                copy_idx,
                child_id,
            )

            multiplier = random.uniform(_BITRATE_MULTIPLIER_MIN, _BITRATE_MULTIPLIER_MAX)
            jittered_bitrate = max(100_000, int(base_bitrate_bps * multiplier))
            noise_strength = _NOISE_STRENGTH
            preset = random.choice(_PRESETS)
            fingerprint = uuid.uuid4().hex

            cmd = _build_ffmpeg_cmd(
                input_path,
                output_path,
                bitrate_bps=jittered_bitrate,
                noise_strength=noise_strength,
                preset=preset,
                fingerprint=fingerprint,
            )
            _run_ffmpeg(cmd)

            with SessionLocal() as db:
                child = Asset(
                    id=child_id,
                    user_id=parent_user_id,
                    folder_id=parent_folder_id,
                    parent_id=parent_id,
                    file_path=str(output_path),
                    status=AssetStatus.READY.value,
                    is_unique=True,
                    metadata_={
                        "fingerprint": fingerprint,
                        "bitrate_bps": jittered_bitrate,
                        "noise_strength": noise_strength,
                        "preset": preset,
                        "copy_index": copy_idx,
                        "source_asset_id": str(parent_id),
                    },
                )
                db.add(child)
                db.commit()
                db.refresh(child)
                created_assets.append(
                    {
                        "asset_id": str(child.id),
                        "file_path": child.file_path,
                        "bitrate_bps": jittered_bitrate,
                        "preset": preset,
                    }
                )

    except Exception as exc:
        tb = traceback.format_exc()
        logger.exception("[uniqueize_video] failed for asset_id=%s", asset_uuid)
        # roll the parent back to RAW so the user can retry
        with SessionLocal() as db:
            parent_row = crud_media.get_asset(db, asset_uuid)
            if parent_row is not None:
                parent_row.status = AssetStatus.RAW.value
                db.commit()
        # try to delete half-written variant files, ignore errors
        for produced in created_assets:
            try:
                os.remove(produced["file_path"])
            except OSError:
                pass
        raise RuntimeError(
            f"uniqueize_video failed: {type(exc).__name__}: {exc}\n{tb}"
        ) from exc

    # set parent to READY
    with SessionLocal() as db:
        parent_row = crud_media.get_asset(db, asset_uuid)
        if parent_row is not None:
            parent_row.status = AssetStatus.READY.value
            db.commit()

    logger.info(
        "[uniqueize_video] completed asset_id=%s produced=%d variant(s)",
        asset_uuid,
        len(created_assets),
    )
    return {
        "asset_id": str(asset_uuid),
        "copies_requested": copies,
        "copies_created": len(created_assets),
        "variants": created_assets,
    }
