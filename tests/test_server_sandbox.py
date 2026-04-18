"""Regression tests for the `_is_within` sandbox helper used by
`describe_repo` / `write_repo_summary` (issue #36).

The earlier implementation used ``realpath(boundary) + os.sep`` +
``startswith``, which rejected every `repo_path` when the boundary
resolved to the filesystem root (``"/"``): ``"/"`` + ``"/"`` → ``"//"``
→ ``rstrip`` → ``""``. These tests lock in the ``commonpath``-based
replacement.
"""

from __future__ import annotations

import os

import pytest

from src.server import _is_within


class TestIsWithin:
    def test_same_dir_is_within_itself(self, tmp_path):
        assert _is_within(str(tmp_path), str(tmp_path))

    def test_subdir_is_within_parent(self, tmp_path):
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert _is_within(str(sub), str(tmp_path))

    def test_sibling_is_not_within(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        assert not _is_within(str(tmp_path / "b"), str(tmp_path / "a"))

    def test_parent_is_not_within_child(self, tmp_path):
        child = tmp_path / "child"
        child.mkdir()
        assert not _is_within(str(tmp_path), str(child))

    def test_filesystem_root_as_boundary_accepts_descendants(self):
        """Regression: the old ``"/" + os.sep`` math rejected every path
        when the boundary was the filesystem root. ``commonpath`` handles
        this cleanly.

        POSIX-only — ``"/"`` isn't a meaningful drive root on Windows.
        Skipped (not silently passed) so CI reporting reflects that the
        case wasn't exercised on this platform.
        """
        if os.name != "posix":
            pytest.skip("filesystem-root semantics are POSIX-specific")
        # Any real absolute path should resolve as "within" /.
        assert _is_within("/etc", "/")
        assert _is_within("/", "/")

    def test_rejects_outside_path(self, tmp_path):
        # Create a fake "other" dir next to tmp_path.
        other = tmp_path.parent / (tmp_path.name + "_sibling")
        other.mkdir(exist_ok=True)
        try:
            assert not _is_within(str(other), str(tmp_path))
        finally:
            other.rmdir()

    def test_nonexistent_boundary_still_compares(self, tmp_path):
        # realpath() on a missing path returns the literal absolute path
        # without resolution; commonpath still produces a deterministic
        # answer.
        ghost = tmp_path / "does_not_exist"
        # A path that would be inside the ghost boundary if it existed.
        inside = ghost / "sub"
        assert _is_within(str(inside), str(ghost))
