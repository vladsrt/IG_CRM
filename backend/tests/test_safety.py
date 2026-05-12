"""
tests for path traversal protection.
makes sure users cannot access files outside media root.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from workers.core.safety import UnsafePathError, resolve_within_media_root


# happy path
class TestHappyPath:
    def test_file_under_media_root_resolves_to_absolute_path(self, media_root):
        target = Path(media_root) / "video.mp4"
        target.write_bytes(b"hello")

        result = resolve_within_media_root(str(target), media_root)
        assert result == str(target.resolve())
        assert os.path.isfile(result)

    def test_nested_file_under_media_root_resolves(self, media_root):
        nested = Path(media_root) / "uploads" / "2026" / "clip.mp4"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"x")

        result = resolve_within_media_root(str(nested), media_root)
        assert result == str(nested.resolve())


# traversal attempts
class TestTraversalAttempts:
    def test_absolute_path_outside_media_root_is_rejected(self, media_root):
        with pytest.raises(UnsafePathError, match="escapes media root"):
            resolve_within_media_root("/etc/passwd", media_root)

    def test_relative_dotdot_traversal_is_rejected(self, media_root):
        # Build a file that DOES exist outside media_root, then traverse to it
        # via ../ from inside.
        deeper = Path(media_root) / "a" / "b"
        deeper.mkdir(parents=True)
        # /tmp/.../media/a/b/../../../etc/passwd → /etc/passwd
        attempt = str(deeper / ".." / ".." / ".." / "etc" / "passwd")
        with pytest.raises(UnsafePathError):
            resolve_within_media_root(attempt, media_root)

    def test_symlink_pointing_outside_is_rejected(self, media_root, tmp_path):
        """
        symlinks pointing outside media root should be rejected.
        """
        outside = tmp_path / "outside_secret.txt"
        outside.write_bytes(b"do not leak")

        sneaky = Path(media_root) / "looks_safe.mp4"
        sneaky.symlink_to(outside)

        with pytest.raises(UnsafePathError, match="escapes media root"):
            resolve_within_media_root(str(sneaky), media_root)


# bad inputs
class TestBadInputs:
    def test_empty_string_is_rejected(self, media_root):
        with pytest.raises(UnsafePathError, match="non-empty string"):
            resolve_within_media_root("", media_root)

    def test_none_is_rejected(self, media_root):
        with pytest.raises(UnsafePathError):
            resolve_within_media_root(None, media_root)  # type: ignore[arg-type]

    def test_nonexistent_path_is_rejected(self, media_root):
        ghost = str(Path(media_root) / "nope.mp4")
        with pytest.raises(UnsafePathError, match="does not exist"):
            resolve_within_media_root(ghost, media_root)

    def test_directory_is_rejected(self, media_root):
        subdir = Path(media_root) / "folder"
        subdir.mkdir()
        with pytest.raises(UnsafePathError, match="not a regular file"):
            resolve_within_media_root(str(subdir), media_root)

    def test_nonexistent_media_root_is_rejected(self, tmp_path):
        ghost_root = str(tmp_path / "no_such_dir")
        # A path candidate that exists; the failure should be on media_root.
        real_file = tmp_path / "real.mp4"
        real_file.write_bytes(b"x")
        with pytest.raises(UnsafePathError, match="media_root"):
            resolve_within_media_root(str(real_file), ghost_root)
