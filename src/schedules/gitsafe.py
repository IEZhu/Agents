"""git with a clean configuration, for the runner's trusted repositories (#339, #341).

Shared by the change check (`src.schedules.paths`) and the trusted repository
(`src.schedules.repos`). git runs without a shell and with a minimal environment:
``GIT_CONFIG_NOSYSTEM=1`` and ``GIT_CONFIG_GLOBAL`` at the null device, so no system or global
configuration is read; ``GIT_TERMINAL_PROMPT=0``, no pager, no replace objects
(``--no-replace-objects``) and no grafts (``GIT_GRAFT_FILE`` at the null device), literal
pathspecs, and ``-C`` the repository with discovery stopped above it. The repository's own
configuration, and the files it includes, is still read, with ``core.fsmonitor=false``,
``core.hooksPath`` at the null device, ``core.commitGraph=false`` and ``core.quotePath=false``
set on the command line over it, so no fsmonitor command and no hook runs, and parents come from
the commit objects, not a commit-graph file. Callers add more command-line configuration with
``config`` (the trusted repository adds ``transfer.fsckObjects=true``).

``require_clean`` refuses a repository with an ``info/grafts`` or ``objects/info/alternates``
file (``UntrustedRepository``): grafts rewrite parents, alternates add objects from elsewhere.
"""
from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import subprocess

from src import executables

GIT_TIMEOUT = 60
# Files of a repository that change what git reports: other parents, objects from elsewhere.
UNTRUSTED_FILES = ("info/grafts", "objects/info/alternates")


class GitError(Exception):
    """A git command that failed or printed output the caller does not understand."""


class UntrustedRepository(GitError):
    """A repository whose grafts or alternates could steer git."""


def environment(repository: Path) -> dict[str, str]:
    """Only what git needs; nothing inherited steers it (``GIT_DIR``, config, pager, prompts, grafts)."""
    values = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    values.update(LC_ALL="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                  GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat", GIT_OPTIONAL_LOCKS="0",
                  GIT_NO_REPLACE_OBJECTS="1", GIT_GRAFT_FILE=os.devnull,
                  GIT_CEILING_DIRECTORIES=str(repository.parent))
    return values


def run(repository: Path, args: tuple[str, ...], *, config: Sequence[str] = (),
        timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess[bytes]:
    """``git <args>`` in ``repository`` with the clean configuration plus ``-c`` for each of ``config``."""
    try:  # the lookup is inside: a missing git is a GitError like one that cannot start
        command = [executables.path("git"), "--no-pager", "--no-replace-objects", "--literal-pathspecs",
                   "-c", "core.quotePath=false", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
                   "-c", "core.commitGraph=false", *(part for item in config for part in ("-c", item)),
                   "-C", str(repository), *args]
        return subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
                              env=environment(repository), check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitError(f"git {args[0]} could not run: {error}") from None


def failure(args: tuple[str, ...], result: subprocess.CompletedProcess[bytes]) -> GitError:
    stderr = result.stderr.decode("utf-8", "replace").strip()[:500]
    return GitError(f"git {args[0]} failed with exit code {result.returncode}: {stderr}")


def git(repository: Path, *args: str, config: Sequence[str] = (), timeout: float = GIT_TIMEOUT) -> bytes:
    """The standard output of ``git <args>`` in ``repository``; ``GitError`` unless it succeeds."""
    result = run(repository, args, config=config, timeout=timeout)
    if result.returncode != 0:
        raise failure(args, result)
    return result.stdout


def git_test(repository: Path, *args: str, config: Sequence[str] = ()) -> subprocess.CompletedProcess[bytes]:
    """A yes-or-no git command, which exits with 0 or 1; ``GitError`` for anything else."""
    result = run(repository, args, config=config)
    if result.returncode not in (0, 1):
        raise failure(args, result)
    return result


def common_dir(repository: Path, *, config: Sequence[str] = ()) -> Path:
    """The repository's common git directory, as an absolute path."""
    # Not `--git-path`: it reports GIT_GRAFT_FILE, the null device here, for info/grafts.
    output = git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir", config=config)
    common = output.decode("utf-8", "surrogateescape").removesuffix("\n")
    if not common or "\n" in common or not os.path.isabs(common):
        raise GitError(f"unexpected rev-parse output for {repository}")
    return Path(common)


def require_clean(repository: Path, common: Path | None = None) -> Path:
    """``repository``'s common directory; ``UntrustedRepository`` when it has grafts or alternates."""
    common = common_dir(repository) if common is None else common
    for name in UNTRUSTED_FILES:
        if os.path.lexists(os.path.join(common, name)):
            raise UntrustedRepository(f"the trusted repository {repository} has {name}, which this check "
                                      "does not accept")
    return common
