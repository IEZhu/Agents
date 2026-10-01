"""Agents-Core version: the UTC commit time of the installation's HEAD.

The version has the form ``YY.MM.DD.HHMM`` (for example ``26.10.01.1001``). On
``main`` HEAD is the last merged pull request. ``-dirty`` marks uncommitted
changes to tracked files; ``unknown`` means there is no readable git metadata.
The value is computed once per process, which is correct because an applied
update always starts a new process.
"""
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import os
import subprocess

UNKNOWN = "unknown"
ROOT = Path(__file__).resolve().parents[1]
_TIMEOUT = 5


def _git(*args: str) -> str | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(ROOT), *args], capture_output=True,
            text=True, timeout=_TIMEOUT, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def format_version(commit_time: str, dirty: bool = False) -> str:
    moment = datetime.fromisoformat(commit_time.strip()).astimezone(timezone.utc)
    return moment.strftime("%y.%m.%d.%H%M") + ("-dirty" if dirty else "")


@lru_cache(maxsize=1)
def agents_core_version() -> str:
    if not (ROOT / ".git").exists():  # never read the version of an enclosing repository
        return UNKNOWN
    commit_time = _git("log", "-1", "--format=%cI")
    if not commit_time or not commit_time.strip():
        return UNKNOWN
    try:
        status = _git("status", "--porcelain", "--untracked-files=no")
        if status is None:
            return UNKNOWN
        dirty = bool(status.strip())
        return format_version(commit_time, dirty)
    except ValueError:
        return UNKNOWN
