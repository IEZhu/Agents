"""The drift check of the runner's code (design 5.4): the installation must match ``pinned``.

``pinned`` is ``refs/remotes/origin/main`` of the runner's trusted repository
(`src.schedules.repos`), never the installation's own ``origin/main`` and never a branch a session
works on. Drift is any difference between ``pinned`` and the installation's working tree in the
modules the runner imported or in ``agents/``, ``rules/``, ``skills/`` and ``implants/``,
including untracked files there. The check compares content: ``git ls-tree -r <pinned>`` is read
from the trusted repository through `src.schedules.gitsafe`, and each working-tree file is hashed
as a git blob here (raw bytes, no filters; a symbolic link as its target). Nothing reads the
installation's index, configuration, fsmonitor or ignore rules, and the installation need not
have ``pinned``: a commit on main that changes none of these paths is no drift. Every file under
the checked directories counts, dot-files included; only ``.DS_Store`` (Finder's folder settings,
which nothing loads) is skipped. A drifted runner ends its run as ``failed: infra`` with the reason
``runner drift`` and the next slot retries.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Iterable

from src import executables

from . import gitsafe

TREES = ("agents", "rules", "skills", "implants")
SKIPPED = frozenset({".DS_Store"})
GIT_TIMEOUT = 60
_SHA = re.compile(r"[0-9a-f]{40}")


class DriftCheckFailed(RuntimeError):
    """git could not answer; the run counts as an infrastructure failure."""


def _run(installation: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run([executables.path("git"), "-C", str(installation), *args], capture_output=True,
                                text=True, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT, check=False,
                                env=executables.git_environment(installation))
    except (OSError, subprocess.SubprocessError) as error:
        raise DriftCheckFailed(f"git {args[0]} failed: {type(error).__name__}") from None
    return result


def _git(installation: Path, *args: str) -> str:
    result = _run(installation, *args)
    if result.returncode:
        raise DriftCheckFailed(f"git {args[0]} failed: {(result.stderr or '').strip()[-300:]}")
    return result.stdout


def imported_files(installation: str | Path, modules: Iterable = ()) -> list[str]:
    """Installation-relative paths of the loaded modules (default: ``sys.modules``) inside it."""
    root = Path(installation).resolve()
    found = set()
    for module in modules or list(sys.modules.values()):
        location = getattr(module, "__file__", None)
        if not location:
            continue
        path = Path(location).resolve()
        if path.suffix == ".py" and path.is_relative_to(root) and ".venv" not in path.relative_to(root).parts:
            found.add(path.relative_to(root).as_posix())
    return sorted(found)


def check(installation: str | Path, commit: str, paths: Iterable[str] | None = None, *,
          repository: str | Path) -> list[str]:
    """Paths whose working-tree content differs from ``commit`` of ``repository``; empty when none do.

    ``repository`` is the trusted repository that holds ``commit`` (``pinned``); the installation
    is only read as files.
    """
    installation = Path(installation)
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise DriftCheckFailed("pinned must be a 40-character lowercase hex SHA")
    paths = sorted(set(paths if paths is not None else [*imported_files(installation), *TREES]))
    if not paths:
        return []
    expected = _tree(Path(repository), commit, paths)
    try:
        found = _working_tree(installation, paths)
    except OSError as error:
        raise DriftCheckFailed(f"the installation cannot be read: {error}") from None
    return sorted(path for path in expected.keys() | found.keys() if expected.get(path) != found.get(path))


def _tree(repository: Path, commit: str, paths: list[str]) -> dict[str, tuple[str, str]]:
    """``{path: (mode, object)}`` of ``commit`` under ``paths``, read through gitsafe."""
    try:
        gitsafe.require_clean(repository)
        output = gitsafe.git(repository, "ls-tree", "-r", "-z", "--full-tree", "--end-of-options", commit,
                             "--", *paths)
    except gitsafe.GitError as error:
        raise DriftCheckFailed(f"the trusted repository: {error}") from None
    entries = {}
    for record in output.split(b"\0"):
        if not record:
            continue
        info, tab, path = record.partition(b"\t")
        parts = info.decode("ascii", "replace").split(" ")
        if not tab or len(parts) != 3 or not _SHA.fullmatch(parts[2]):
            raise DriftCheckFailed("unexpected ls-tree output")
        entries[path.decode("utf-8", "surrogateescape")] = (parts[0], parts[2])
    return entries


def _working_tree(installation: Path, paths: list[str]) -> dict[str, tuple[str, str]]:
    """``{path: (mode, blob id)}`` of every file at or under ``paths`` in the working tree."""
    found: dict[str, tuple[str, str]] = {}

    def visit(relative: str) -> None:
        full = installation / relative
        try:
            status = os.lstat(full)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(status.st_mode):
            for name in sorted(os.listdir(full)):
                if name not in SKIPPED:
                    visit(f"{relative}/{name}")
        elif stat.S_ISLNK(status.st_mode):
            found[relative] = ("120000", _blob_id(os.fsencode(os.readlink(full))))
        elif stat.S_ISREG(status.st_mode):
            mode = "100755" if status.st_mode & stat.S_IXUSR else "100644"
            found[relative] = (mode, _blob_id(full.read_bytes()))
        else:
            found[relative] = ("unsupported", "")

    for path in paths:
        visit(path.rstrip("/"))
    return found


def _blob_id(content: bytes) -> str:
    """The git blob ID of ``content`` (``git hash-object --no-filters``)."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def commit_of(installation: str | Path) -> str | None:
    """The installation's ``HEAD``, for the run record; None when git cannot say."""
    try:
        return _git(Path(installation), "rev-parse", "HEAD").strip()
    except DriftCheckFailed:
        return None

