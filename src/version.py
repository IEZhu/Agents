"""Agents-Core version from the canonical root VERSION file (YY.MM.DD.HHMM).

Legacy checkouts without VERSION retain the commit-time calculation below.
``-dev`` marks an installation on the integration branch ``dev``, ``-dirty``
uncommitted changes to tracked files (``26.10.01.1001-dev-dirty`` has both);
``unknown`` means the version or required checkout metadata could not be read.
The value is computed once per process, which is correct because an applied
update always starts a new process.
"""
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import re
import subprocess

from src import calendar_version, executables

UNKNOWN = "unknown"
DEV_BRANCH = "dev"  # the integration branch; main takes its release pull requests
RELEASE_BRANCH = "main"
ROOT = Path(__file__).resolve().parents[1]
_TIMEOUT = 5
_BRANCH_HEADER = "# branch.head "


def _git(*args: str) -> str | None:
    # Without the user's git configuration: a core.fsmonitor there would run on `status` (#279).
    env = executables.git_environment(ROOT)
    try:
        # Never the server's stdin: under stdio it is the MCP pipe with a pending read, and on
        # Windows git blocks on that handle at startup until the client's next message. No console
        # either: the Windows service runs under pythonw, where git would open a visible window.
        result = subprocess.run(
            [executables.path("git"), "--no-optional-locks", "-C", str(ROOT), *args], capture_output=True,
            stdin=subprocess.DEVNULL, text=True, timeout=_TIMEOUT, check=False, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def release_time(merge: str, previous: str, merged: str) -> str:
    """The commit time a merge into ``main`` is versioned by: that of the revision it merged.

    A merged revision older than main's previous HEAD (a stale hotfix branch) keeps the merge's
    own time, so main's version never goes back. ``scripts/release_version.py`` applies the same rule.
    """
    moments = [datetime.fromisoformat(value.strip()) for value in (previous, merged)]
    if any(moment.tzinfo is None for moment in moments):
        raise ValueError("commit time without a timezone")
    return merged if moments[1] >= moments[0] else merge


def format_version(commit_time: str, dirty: bool = False, branch: str | None = None) -> str:
    moment = datetime.fromisoformat(commit_time.strip()).astimezone(timezone.utc)
    suffix = ("-dev" if branch == DEV_BRANCH else "") + ("-dirty" if dirty else "")
    return moment.strftime("%y.%m.%d.%H%M") + suffix


def _branch(status: str) -> str | None:
    """The checked-out branch from ``status --porcelain=v2 --branch``; ``(detached)`` on a detached HEAD."""
    for line in status.splitlines():
        if line.startswith(_BRANCH_HEADER):
            return line[len(_BRANCH_HEADER):].strip()
    return None


@lru_cache(maxsize=1)
def agents_core_version() -> str:
    try:
        stored = calendar_version.read(ROOT)
    except (OSError, ValueError):
        return UNKNOWN
    if not (ROOT / ".git").exists():  # never read the version of an enclosing repository
        return stored or UNKNOWN
    head = _git("log", "-1", "--format=%cI %P")
    if (not head or not head.strip()) and stored is None:
        return UNKNOWN
    try:
        # One call for both: the branch header and the changed tracked files. Without the
        # ahead/behind count, which would walk history for nothing.
        status = _git("status", "--porcelain=v2", "--branch", "--no-ahead-behind", "--untracked-files=no")
        if status is None:
            # A git older than 2.17 lacks --no-ahead-behind (an older git is still used when no
            # newer one exists): the plain status keeps the version, without the branch suffix.
            status = _git("status", "--porcelain", "--untracked-files=no")
        if status is None:
            return UNKNOWN
        dirty = any(line.strip() and not line.startswith("#") for line in status.splitlines())
        branch = _branch(status)
        if stored is not None:
            return stored + ("-dev" if branch == DEV_BRANCH else "") + ("-dirty" if dirty else "")
        commit_time, *parents = head.split()
        if branch == RELEASE_BRANCH and len(parents) == 2:
            # A shallow clone lacks the parents: the merge then keeps its own time.
            times = (_git("show", "-s", "--format=%cI", *parents) or "").split()
            if len(times) == 2:
                commit_time = release_time(commit_time, *times)
        return format_version(commit_time, dirty, branch)
    except ValueError:
        return UNKNOWN


@lru_cache(maxsize=1)
def agents_core_branch() -> str | None:
    """The installation's checked-out branch (#381); None on a detached HEAD or without git metadata.

    Computed once per process, like the version: switching branches restarts the server.
    """
    if not (ROOT / ".git").exists():
        return None
    found = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    return found.strip() or None if found else None


@lru_cache(maxsize=1)
def agents_core_commit() -> str | None:
    """The running source tree's HEAD, frozen during daemon startup.

    A source archive without its own Git metadata has no verified commit. Never
    substitute the canonical content installation's HEAD for its source identity.
    """
    if not (ROOT / ".git").exists():
        return None
    found = _git("rev-parse", "--verify", "HEAD^{commit}")
    commit = found.strip() if found else ""
    return commit if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) else None
