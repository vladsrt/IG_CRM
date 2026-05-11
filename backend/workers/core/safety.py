"""Filesystem safety helpers for the worker layer.

Anything that takes a path from the user (upload media, avatar image)
must go through resolve_within_media_root first. This is the main check
against path traversal. Otherwise a bad or hijacked AI plan could send
/etc/shadow or any other host file straight to Instagram.
"""

from __future__ import annotations

from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a worker is asked to access a path outside MEDIA_ROOT."""


def resolve_within_media_root(candidate: str, media_root: str) -> str:
    """Return the resolved absolute path of `candidate`, but only if it
    sits under `media_root`.

    Path.resolve(strict=True) follows symlinks, so a symlink inside
    MEDIA_ROOT that points outside can not be used to escape. We resolve
    both ends and compare the resolved paths with relative_to.

    Args:
        candidate: path from the user (Task.payload, etc).
        media_root: the trusted root, normally settings.MEDIA_ROOT.

    Returns:
        Fully resolved absolute path as a str, safe to pass to
        DrissionPage or subprocess.

    Raises:
        UnsafePathError: when candidate is empty or not a string, does
            not exist, is not a regular file, or escapes media_root.
    """
    if not candidate or not isinstance(candidate, str):
        raise UnsafePathError("path must be a non-empty string")
    if not media_root or not isinstance(media_root, str):
        raise UnsafePathError("media_root must be a non-empty string")

    try:
        root = Path(media_root).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise UnsafePathError(
            f"media_root does not exist on disk: {media_root}"
        ) from exc

    try:
        target = Path(candidate).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise UnsafePathError(f"path does not exist: {candidate}") from exc

    if not target.is_file():
        raise UnsafePathError(f"path is not a regular file: {candidate}")

    try:
        target.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(
            f"path {target} escapes media root {root}"
        ) from exc

    return str(target)
