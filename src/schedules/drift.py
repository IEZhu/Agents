"""The drift check of the runner's code (design 5.4): the installation must match ``pinned``.

``pinned`` is ``origin/main`` as the installation's repository knows it (this part does not
fetch; the trusted repository of later parts does). Drift is any difference between ``pinned`` and
the working tree in the modules the runner imported or in ``agents/``, ``rules/``, ``skills/`` and
``implants/``, including untracked files there. A drifted runner ends its run as
``failed: infra`` with the reason ``runner drift`` and the next slot retries.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Iterable

from src import executables

TREES = ("agents", "rules", "skills", "implants")
GIT_TIMEOUT = 60


class DriftCheckFailed(RuntimeError):
    """git could not answer; the run counts as an infrastructure failure."""


def _git(installation: Path, *args: str) -> str:
    try:
        result = subprocess.run([executables.path("git"), "-C", str(installation), *args], capture_output=True,
                                text=True, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT, check=False,
                                env=executables.git_environment(installation))
    except (OSError, subprocess.SubprocessError) as error:
        raise DriftCheckFailed(f"git {args[0]} failed: {type(error).__name__}") from None
    if result.returncode:
        raise DriftCheckFailed(f"git {args[0]} failed: {(result.stderr or '').strip()[-300:]}")
    return result.stdout


def pinned(installation: str | Path, ref: str = "origin/main") -> str:
    return _git(Path(installation), "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()


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
    """Paths that differ from ``commit`` (tracked or untracked); empty when the runner matches it."""
    installation = Path(installation)
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

