"""The drift check of the runner's code (design 5.4): the installation must match ``pinned``.

``pinned`` is ``refs/remotes/origin/main`` of the runner's trusted repository
(`src.schedules.repos`), never the installation's own ``origin/main`` and never a branch a session
works on. Drift is any difference between ``pinned`` and the installation's working tree in the
modules the runner imported or in ``agents/``, ``rules/``, ``skills/`` and ``implants/``,
including untracked files there; a ``pinned`` commit the installation does not have is drift too.
A drifted runner ends its run as ``failed: infra`` with the reason ``runner drift`` and the next
slot retries.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable

from src import executables

TREES = ("agents", "rules", "skills", "implants")
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


def known(installation: str | Path, commit: str) -> bool:
    """Whether the installation's repository has the commit ``commit``."""
    result = _run(Path(installation), "rev-parse", "--verify", "--quiet", "--end-of-options", f"{commit}^{{commit}}")
    if result.returncode not in (0, 1):
        raise DriftCheckFailed(f"git rev-parse failed: {(result.stderr or '').strip()[-300:]}")
    return result.returncode == 0 and result.stdout.strip() == commit


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


def check(installation: str | Path, commit: str, paths: Iterable[str] | None = None) -> list[str]:
    """Paths that differ from ``commit`` (tracked or untracked); empty when the runner matches it.

    A ``commit`` the installation does not have is drift: the only entry names it.
    """
    installation = Path(installation)
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise DriftCheckFailed("pinned must be a 40-character lowercase hex SHA")
    if not known(installation, commit):
        return [f"pinned {commit} is not in the installation"]
    paths = sorted(set(paths if paths is not None else [*imported_files(installation), *TREES]))
    if not paths:
        return []
    changed = _git(installation, "diff", "--name-only", "--no-renames", commit, "--", *paths).split("\n")
    untracked = _git(installation, "ls-files", "--others", "--exclude-standard", "--", *paths).split("\n")
    return sorted({path for path in changed + untracked if path})


def commit_of(installation: str | Path) -> str | None:
    """The installation's ``HEAD``, for the run record; None when git cannot say."""
    try:
        return _git(Path(installation), "rev-parse", "HEAD").strip()
    except DriftCheckFailed:
        return None

