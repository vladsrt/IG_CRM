"""Celery tasks for media processing — currently the FFmpeg uniqueizer.

Goal of ``uniqueize_video``
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Generate ``copies`` perceptually-identical-but-byte-distinct variants of a
raw video so each Instagram account can post a clone whose MD5 differs
from every sibling. The anti-fraud bypass mix is:

* ``-map_metadata -1``                    — strip every original tag
* ``-metadata comment=<random uuid>``     — inject a unique fingerprint
* ``-vf noise=alls=<r>:allf=t``           — invisible per-frame temporal noise
* ``-b:v <0.95..1.05 × original>``        — slight bitrate jitter
* ``-c:v libx264 -preset <random>``       — re-encode forces a fresh container

Each variant is persisted as a child ``Asset`` row pointing at its raw
parent via ``parent_id``.
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


# ── Tunables ────────────────────────────────────────────────────────────
# Noise is locked at 4: anything higher (we'd been bumping up to 12 in
# the standalone CLI for fingerprint-strength experiments) visibly
# degrades the output. 4 is the highest value that's still imperceptible
# on phone playback while still moving every pixel's value enough to
# defeat byte-level dedup. Random jitter on noise is intentionally gone
# — keeping it deterministic makes uniqueizer regressions reproducible.
_NOISE_STRENGTH: int = 4
_BITRATE_MULTIPLIER_MIN: float = 0.95
_BITRATE_MULTIPLIER_MAX: float = 1.05
_PRESETS: list[str] = ["veryfast", "faster", "fast", "medium"]


# ── Helpers ─────────────────────────────────────────────────────────────
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
    """Return the input video's bitrate in bits/sec, or the configured fallback."""
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
        "ffprobe could not determine bitrate; falling back to %d bps",
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
        "-y",                                         # overwrite
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(input_path),
        "-map_metadata", "-1",                        # strip original metadata
        "-metadata", f"comment={fingerprint}",        # unique fingerprint
        "-metadata", f"title=ig-crm-{fingerprint[:8]}",
        "-vf", f"noise=alls={noise_strength}:allf=t", # invisible temporal noise
        "-c:v", "libx264",
        "-preset", preset,
        "-b:v", f"{bitrate_bps}",                     # bitrate jitter (bits/sec)
        "-c:a", "aac",
        "-movflags", "+faststart",                    # IG-friendly mp4 layout
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
    """Compute the on-disk path for a variant."""
    parent_path = Path(parent.file_path)
    suffix = parent_path.suffix or ".mp4"
    out_dir = _media_root() / "variants" / str(parent.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{copy_idx:03d}_{child_id.hex}{suffix}"


# ── Celery task ─────────────────────────────────────────────────────────
@celery_app.task(
    bind=True,
    name="ig_crm.uniqueize_video",
    acks_late=True,
)
def uniqueize_video(
    self: CeleryTask, asset_id: str, copies: int = 10
) -> dict[str, Any]:
    """Generate ``copies`` byte-distinct variants of the raw video at ``asset_id``."""
    if copies < 1:
        raise ValueError(f"copies must be >= 1, got {copies}")

    asset_uuid = uuid.UUID(asset_id)
    logger.info(
        "[uniqueize_video] asset_id=%s copies=%d", asset_uuid, copies
    )
    _ensure_binaries_available()

    # ── Load parent + flip to PROCESSING ───────────────────────────────
    with SessionLocal() as db:
        parent = crud_media.get_asset(db, asset_uuid)
        if parent is None:
            raise ValueError(f"Asset {asset_uuid} not found")
        if parent.parent_id is not None:
            raise ValueError(
                f"Asset {asset_uuid} is itself a variant — uniqueize the root parent instead"
            )

        input_path = Path(parent.file_path).expanduser().resolve()
        if not input_path.is_file():
            raise FileNotFoundError(f"Source video missing on disk: {input_path}")

        parent.status = AssetStatus.PROCESSING.value
        db.commit()
        # Snapshot fields we need outside the session.
        parent_user_id = parent.user_id
        parent_folder_id = parent.folder_id
        parent_id = parent.id

    # ── Probe & build variants ─────────────────────────────────────────
    base_bitrate_bps = _probe_input_bitrate_bps(input_path)
    created_assets: list[dict[str, Any]] = []

    try:
        for copy_idx in range(1, copies + 1):
            child_id = uuid.uuid4()
            output_path = _variant_output_path(
                Asset(id=parent_id, file_path=str(input_path)),  # path-only proxy
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
        # Roll the parent back to RAW so the operator can retry.
        with SessionLocal() as db:
            parent_row = crud_media.get_asset(db, asset_uuid)
            if parent_row is not None:
                parent_row.status = AssetStatus.RAW.value
                db.commit()
        # Best-effort cleanup of any half-written variant files.
        for produced in created_assets:
            try:
                os.remove(produced["file_path"])
            except OSError:
                pass
        raise RuntimeError(
            f"uniqueize_video failed: {type(exc).__name__}: {exc}\n{tb}"
        ) from exc

    # ── Mark parent READY ──────────────────────────────────────────────
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
