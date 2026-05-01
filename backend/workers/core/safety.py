"""
File-system safety helpers for the worker layer.

Anything that consumes an operator-supplied path (upload media, avatar
image) must run it through :func:`resolve_within_media_root` first. This
is the load-bearing defense against path-traversal: a malicious or
compromised AI plan could otherwise dispatch ``/etc/shadow`` or any
other host file to Instagram's servers.
"""

from __future__ import annotations

from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a worker is asked to access a path outside MEDIA_ROOT."""


def resolve_within_media_root(candidate: str, media_root: str) -> str:
    """Return the absolute resolved path of ``candidate`` IFF it sits under ``media_root``.

    ``Path.resolve(strict=True)`` follows symlinks, so a symlink under
    MEDIA_ROOT pointing outside cannot be used to escape — we resolve
    both ends and compare resolved paths via ``relative_to``.

    Args:
        candidate: Operator-supplied path (from ``Task.payload`` etc.).
        media_root: The trusted root, normally ``settings.MEDIA_ROOT``.

    Returns:
        The fully-resolved absolute path as a ``str``, safe to hand to
        ``DrissionPage`` or ``subprocess``.

    Raises:
        UnsafePathError: If ``candidate`` is empty / non-string, does not
            exist, is not a regular file, or escapes ``media_root``.
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
