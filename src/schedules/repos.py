"""The runner's trusted repository, one-off session repositories and bundle import (#339).

Design: docs/autodev-plan.md, section 5.4, and 9.4 steps 1, 2 and 5; reference:
docs/autodev-runner.md. A session that can write a repository's ``.git`` could make any later git
command outside the sandbox run its code (hooks, ``core.fsmonitor``, filters), so the runner runs
git only on metadata no session can write.

* **Trusted repository.** A bare repository owned by the runner at ``clone`` from
  ``autodev.json`` (`src.schedules.config.runner_location`: never under ``~/Documents`` or the
  installation). ``fetch`` takes ``main`` from an explicit URL with the explicit refspec
  ``+refs/heads/main:refs/remotes/origin/main``, never from a remote name; ``pinned`` is the
  40-hex SHA of ``refs/remotes/origin/main``. Every git command runs through
  `src.schedules.gitsafe` with ``transfer.fsckObjects=true`` and automatic maintenance off, and a
  repository with ``info/grafts`` or ``objects/info/alternates`` is refused.
* **One-off repository.** ``create_session_repository`` clones the trusted repository with
  ``git clone --no-local --no-hardlinks --template=`` (no shared objects, no alternates, no hook
  samples) detached at ``pinned``, removes its ``origin`` remote and sets the session's commit
  identity locally. It returns only a path: no function of this module takes a one-off
  repository's path afterwards, so the runner never runs git on metadata the session wrote.
* **Bundles.** ``import_bundle`` takes a session's ``git bundle`` from its exchange directory: a
  regular file with a single link, not a symbolic link, of bounded size, copied before git reads
  it. It is checked against the session's ``base`` (``SessionRepository.pinned``), which must
  still be an ancestor of ``pinned``, so main may move while the session works. The bundle must
  name exactly ``refs/heads/<expected branch>`` with a 40-hex head and have its prerequisites in
  the trusted repository; a bundle refused here is kept as a read-only file
  ``autodev-quarantine/<run-id>/<n>.bundle`` inside the trusted repository. Its objects are then
  fetched with fsck into ``refs/autodev/quarantine/<run-id>/<n>``; the head must descend from
  ``base``, ``base..head`` must hold between 1 and ``MAX_IMPORT_COMMITS`` commits, each with
  exactly one parent, and no commit may add or change a gitlink or a symbolic link that is
  absolute, leaves the tree or has a target longer than ``MAX_LINK_TARGET`` bytes (sizes are read
  before any target, and targets through one bounded read). One ``diff-tree --stdin`` walks all
  commits, and the whole import has a deadline (``IMPORT_DEADLINE``). Only then is the head
  promoted to ``refs/heads/<branch>``, fast-forward only, and the quarantine ref removed. A
  refusal raises ``BundleRefused`` naming the reason; objects already fetched stay under the
  quarantine ref. ``prune_quarantine`` removes quarantine refs and files of runs older than
  ``QUARANTINE_RETENTION``; the caller runs it under the run lease.
* **Push.** ``push_arguments`` builds the git arguments of the broker's push (#342): an explicit
  URL and ``<sha>:refs/heads/claude/autodev-<issue>-<slug>``. Nothing here pushes.
* **Environment.** ``environment`` returns a virtual environment under the runner's directory,
  keyed by the SHA-256 of ``requirements.txt``, ``pyproject.toml`` and ``uv.lock`` at a commit of
  main (``pinned`` or an ancestor; an imported session head is refused), read with
  ``git cat-file blob``. It is built by an injectable builder (the default runs with a minimal
  environment) only when it is missing or has no completion marker, in a private sibling that is
  made read-only and renamed into place; ``venvs/`` and ``venvs/<key>`` must be directories of
  the runner's user, not symbolic links.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from src.file_lock import file_lock

from . import config as autodev_config
from . import gitsafe

PINNED_REF = "refs/remotes/origin/main"
MAIN_REFSPEC = f"+refs/heads/main:{PINNED_REF}"
QUARANTINE = "refs/autodev/quarantine"
# fsck on every object that arrives; no automatic gc or maintenance, which could run later. Blobs over
# 1 MiB are streamed rather than held whole, and one thread packs, so a bundle of small, highly
# compressible blobs cannot make git's own memory grow with their inflated size (#339 review round 2).
CONFIG = ("transfer.fsckObjects=true", "gc.auto=0", "maintenance.auto=false", "advice.detachedHead=false",
          "core.bigFileThreshold=1m", "pack.threads=1")
FETCH_TIMEOUT = 600
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
# Bounds on what untrusted bundle content can make the runner do (#339 review round 1).
MAX_IMPORT_COMMITS = 200
MAX_LINK_TARGET = 4096  # PATH_MAX
MAX_LINKS = 1000  # distinct symbolic link targets in one import
MAX_DIFF_OUTPUT = 16 * 1024 * 1024
IMPORT_DEADLINE = 900
QUARANTINE_FILES = "autodev-quarantine"  # inside the trusted repository: refused bundle files
QUARANTINE_RETENTION = timedelta(days=30)
SESSION_NAME = "Alexey Zhuchkov"
SESSION_EMAIL = "alexey.zhuchkov@gmail.com"
DEPENDENCY_FILES = ("requirements.txt", "pyproject.toml", "uv.lock")
COMPLETE_MARKER = ".autodev-complete"
VENV_TIMEOUT = 1800

_SHA = re.compile(r"[0-9a-f]{40}")
_NAME = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_URLS = (re.compile(rf"https://github\.com/{_NAME}\.git"), re.compile(rf"git@github\.com:{_NAME}\.git"),
         re.compile(rf"ssh://git@github\.com/{_NAME}\.git"))
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_BRANCH = re.compile(r"claude/autodev-([1-9][0-9]{0,8})-([a-z0-9]+(?:-[a-z0-9]+)*)")
_MAX_SLUG = 60
_RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
_RAW_HEADER = re.compile(r":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) ([ADMT])")
_SYMLINK, _GITLINK = "120000", "160000"

Builder = Callable[[Path, Mapping[str, bytes]], None]


class RepositoryError(Exception):
    """The trusted repository is unusable or a git command on it failed."""


class FetchError(RepositoryError):
    """``main`` could not be fetched: the network or the remote failed."""


class ImportTimeout(RepositoryError):
    """A git command was killed at its timeout; within an import, the bundle is refused."""


class BundleRefused(RepositoryError):
    """A session's bundle was not imported.

    ``quarantine_ref`` holds its objects when they were fetched; ``quarantine_file`` is the kept
    copy of a bundle refused before the fetch. A file refused by the copy itself (a link, not a
    regular file, too large) is not kept.
    """

    def __init__(self, reason: str, quarantine_ref: str | None = None, *, quarantine_file: Path | None = None):
        kept = quarantine_ref or quarantine_file
        super().__init__(reason if kept is None else f"{reason} (kept under {kept})")
        self.reason = reason
        self.quarantine_ref = quarantine_ref
        self.quarantine_file = quarantine_file


@dataclass(frozen=True)
class SessionRepository:
    """A one-off repository handed to one session. Nothing in this module runs git in it again."""

    path: Path
    pinned: str


@dataclass(frozen=True)
class ImportedBundle:
    """An imported bundle; ``commits`` are the commits this import added to the branch, oldest first."""

    branch: str
    head: str
    commits: tuple[str, ...]


def default_url(repository: str) -> str:
    """The HTTPS URL of a GitHub ``OWNER/NAME``."""
    return _url(f"https://github.com/{repository}.git")


def _url(value: object) -> str:
    """A GitHub URL or an absolute local path (tests); never a remote name or an option."""
    if isinstance(value, str) and (any(pattern.fullmatch(value) for pattern in _URLS)
                                   or (os.path.isabs(value) and not any(c in value for c in "\n\r\0"))):
        return value
    raise ValueError(f"not an explicit repository URL: {value!r}")


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"{name} must be a 40-character lowercase hex SHA")
    return value


def branch_name(issue: int, slug: str) -> str:
    """``claude/autodev-<issue>-<slug>``, validated."""
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        raise ValueError("issue must be a positive integer")
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or len(slug) > _MAX_SLUG:
        raise ValueError(f"slug must be lowercase letters and digits joined by hyphens, at most {_MAX_SLUG}")
    return _branch(f"claude/autodev-{issue}-{slug}")


def _branch(value: object) -> str:
    if not isinstance(value, str) or not _BRANCH.fullmatch(value) or len(value) > 100:
        raise ValueError(f"not an autodev branch claude/autodev-<issue>-<slug>: {value!r}")
    return value


def push_arguments(url: str, sha: str, issue: int, slug: str) -> list[str]:
    """The git arguments of the broker's push (#342): explicit URL, explicit ``<sha>:refs/heads/…``.

    The caller prefixes the clean-configuration git command (`src.schedules.gitsafe`); this
    function runs nothing.
    """
    refspec = f"{_sha(sha, 'sha')}:refs/heads/{branch_name(issue, slug)}"
    return ["push", "--porcelain", "--no-follow-tags", "--no-signed", _url(url), refspec]


class TrustedRepository:
    """The runner's bare repository; see the module docstring."""

    def __init__(self, path: str | os.PathLike[str], url: str, *, installation: str | os.PathLike[str] | None = None):
        self.path = autodev_config.runner_location(os.fspath(path), installation, what="the trusted repository")
        self.url = _url(url)
        self.installation = installation

    @classmethod
    def from_config(cls, config: Mapping, installation: str | os.PathLike[str] | None = None) -> TrustedRepository:
        return cls(config["clone"], default_url(config["repository"]), installation=installation)

    # --- git ----------------------------------------------------------------------------------

    def _git(self, *args: str, timeout: float = gitsafe.GIT_TIMEOUT) -> bytes:
        try:
            return gitsafe.git(self.path, *args, config=CONFIG, timeout=timeout)
        except gitsafe.GitTimeout as error:
            raise ImportTimeout(str(error)) from None
        except gitsafe.GitError as error:
            raise RepositoryError(str(error)) from None

    def _git_test(self, *args: str, timeout: float = gitsafe.GIT_TIMEOUT) -> subprocess.CompletedProcess[bytes]:
        try:
            return gitsafe.git_test(self.path, *args, config=CONFIG, timeout=timeout)
        except gitsafe.GitTimeout as error:
            raise ImportTimeout(str(error)) from None
        except gitsafe.GitError as error:
            raise RepositoryError(str(error)) from None

    # --- the repository -----------------------------------------------------------------------

    def ensure(self) -> TrustedRepository:
        """Create the bare repository when it is missing; refuse one that is not the runner's."""
        if not os.path.lexists(self.path) or (self.path.is_dir() and not any(self.path.iterdir())):
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                gitsafe.git(self.path.parent, "init", "--quiet", "--bare", "--initial-branch=main", "--template=",
                            "--", str(self.path), config=CONFIG)
            except gitsafe.GitError as error:
                raise RepositoryError(str(error)) from None
            os.chmod(self.path, 0o700)
        self.verify()
        return self

    def verify(self) -> None:
        """A bare repository at ``path`` itself, owned by this user, without grafts or alternates."""
        if self.path.is_symlink() or not self.path.is_dir():
            raise RepositoryError(f"the trusted repository {self.path} is not a directory")
        if self.path.stat().st_uid != os.getuid():
            raise RepositoryError(f"the trusted repository {self.path} is not owned by the runner's user")
        try:
            common = gitsafe.common_dir(self.path, config=CONFIG)
            if common.resolve() != self.path:
                raise RepositoryError(f"{self.path} is not a bare repository (its git directory is {common})")
            gitsafe.require_clean(self.path, common)
        except gitsafe.GitError as error:
            raise RepositoryError(str(error)) from None
        if self._git("rev-parse", "--is-bare-repository").strip() != b"true":
            raise RepositoryError(f"the trusted repository {self.path} is not bare")

    def fetch(self) -> str:
        """Fetch ``main`` from the explicit URL; returns the new ``pinned``."""
        self.verify()
        try:
            gitsafe.git(self.path, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                        "--no-recurse-submodules", "--", self.url, MAIN_REFSPEC, config=CONFIG, timeout=FETCH_TIMEOUT)
        except gitsafe.GitError as error:
            raise FetchError(f"fetch of main from {self.url}: {error}") from None
        return self.pinned()

    def pinned(self) -> str:
        """The 40-hex SHA of ``refs/remotes/origin/main``."""
        result = self._git_test("rev-parse", "--verify", "--quiet", "--end-of-options", f"{PINNED_REF}^{{commit}}")
        sha = result.stdout.decode("ascii", "replace").strip()
        if result.returncode != 0 or not _SHA.fullmatch(sha):
            raise RepositoryError(f"the trusted repository has no {PINNED_REF}; fetch it first")
        return sha

    def _is_commit(self, sha: str) -> bool:
        result = self._git_test("rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^{{commit}}")
        return result.returncode == 0 and result.stdout.strip() == sha.encode("ascii")

    # --- one-off repositories -----------------------------------------------------------------

    def create_session_repository(self, destination: str | os.PathLike[str]) -> SessionRepository:
        """A fresh clone at ``pinned`` for one session; the only git this module runs in it."""
        self.verify()
        pinned = self.pinned()
        destination = Path(os.path.abspath(destination))
        if os.path.lexists(destination):
            raise RepositoryError(f"{destination} already exists")
        if destination.is_relative_to(self.path) or not destination.parent.is_dir():
            raise RepositoryError(f"{destination} must be a new directory outside the trusted repository")
        try:
            gitsafe.git(destination.parent, "clone", "--quiet", "--no-local", "--no-hardlinks", "--template=",
                        f"--revision={PINNED_REF}", "--", str(self.path), str(destination),
                        config=CONFIG, timeout=FETCH_TIMEOUT)
            gitsafe.git(destination, "remote", "remove", "origin", config=CONFIG)
            gitsafe.git(destination, "config", "--local", "user.name", SESSION_NAME, config=CONFIG)
            gitsafe.git(destination, "config", "--local", "user.email", SESSION_EMAIL, config=CONFIG)
            head = gitsafe.git(destination, "rev-parse", "--verify", "HEAD", config=CONFIG).decode().strip()
        except gitsafe.GitError as error:
            shutil.rmtree(destination, ignore_errors=True)
            hint = " (clone --revision needs git 2.49 or later)" if "revision" in str(error) else ""
            raise RepositoryError(f"the session repository could not be created: {error}{hint}") from None
        if head != pinned:  # main moved between reading pinned and the clone
            shutil.rmtree(destination, ignore_errors=True)
            raise RepositoryError(f"the session repository is at {head}, not at pinned {pinned}")
        return SessionRepository(destination, pinned)


    # --- bundles ------------------------------------------------------------------------------

    def import_bundle(self, bundle: str | os.PathLike[str], exchange: str | os.PathLike[str], branch: str,
                      run_id: str, base: str) -> ImportedBundle:
        """Verify a session's bundle against its ``base`` and promote its head; see the module docstring.

        ``base`` is ``SessionRepository.pinned``, the commit the session's repository was created
        at; it must still be an ancestor of (or equal to) the current ``pinned``.
        """
        branch = _branch(branch)
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError(f"not a run ID: {run_id!r}")
        base = _sha(base, "base")
        deadline = _Deadline(IMPORT_DEADLINE)
        self.verify()
        pinned = self.pinned()
        if not self._is_commit(base) or self._git_test(
                "merge-base", "--is-ancestor", "--end-of-options", base, pinned).returncode != 0:
            raise RepositoryError(f"the session base {base} is not an ancestor of pinned {pinned}")
        with tempfile.TemporaryDirectory(prefix="autodev-bundle-") as private:
            copy = Path(private) / "session.bundle"
            _copy_bundle(Path(bundle), Path(exchange), copy)
            try:
                head = self._bundle_head(copy, branch, deadline)
                verified = self._git_test("bundle", "verify", str(copy), timeout=deadline.left(gitsafe.GIT_TIMEOUT))
                if verified.returncode != 0:
                    detail = verified.stderr.decode("utf-8", "replace").strip()[-300:]
                    reason = "bundle prerequisites missing from the trusted repository" \
                        if "prerequisite" in detail else "bundle does not verify"
                    raise BundleRefused(f"{reason}: {detail}")
            except BundleRefused as refused:
                raise BundleRefused(refused.reason, quarantine_file=self._keep_refused(copy, run_id)) from None
            quarantine = f"{QUARANTINE}/{run_id}/{self._next_number(run_id)}"
            try:
                self._git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                          "--", str(copy), f"refs/heads/{branch}:{quarantine}",
                          timeout=deadline.left(FETCH_TIMEOUT))
            except RepositoryError as error:
                raise BundleRefused(f"bundle fetch failed: {error}") from None
        deadline.quarantine = quarantine
        fetched = self._git("rev-parse", "--verify", "--end-of-options", quarantine).decode().strip()
        if fetched != head:
            raise BundleRefused(f"fetched head {fetched} is not the listed head {head}", quarantine)
        try:
            commits = self._check_range(base, head, quarantine, deadline)
        except ImportTimeout:  # the bundle's content made a check outlast the deadline
            raise BundleRefused(f"the import took longer than {deadline.seconds} seconds", quarantine) from None
        added = self._promote(branch, head, commits, quarantine, run_id)
        self._git("update-ref", "-d", "--", quarantine, head)
        return ImportedBundle(branch, head, added)

    def _bundle_head(self, copy: Path, branch: str, deadline: _Deadline) -> str:
        listed = self._git_test("bundle", "list-heads", str(copy), timeout=deadline.left(gitsafe.GIT_TIMEOUT))
        if listed.returncode != 0:
            raise BundleRefused("not a readable git bundle")
        lines = listed.stdout.decode("utf-8", "replace").splitlines()
        if len(lines) != 1:
            raise BundleRefused(f"the bundle names {len(lines)} refs, not exactly refs/heads/{branch}")
        sha, _, ref = lines[0].partition(" ")
        if ref != f"refs/heads/{branch}":
            raise BundleRefused(f"the bundle names {ref[:120]!r}, not the expected refs/heads/{branch}")
        if not _SHA.fullmatch(sha):
            raise BundleRefused("the bundle's head is not a 40-character hex SHA")
        return sha

    def _next_number(self, run_id: str) -> int:
        """The next free ``<n>`` of a run, over its quarantine refs and its kept bundle files."""
        prefix = f"{QUARANTINE}/{run_id}/"
        taken = [name[len(prefix):] for name in
                 self._git("for-each-ref", "--format=%(refname)", "--", prefix).decode().splitlines()]
        kept = self.path / QUARANTINE_FILES / run_id
        if os.path.isdir(kept) and not os.path.islink(kept):
            taken += [name.removesuffix(".bundle") for name in os.listdir(kept)]
        return max((int(name) for name in taken if name.isdigit()), default=0) + 1

    def _keep_refused(self, copy: Path, run_id: str) -> Path:
        """Move the private copy of a bundle refused before the fetch to the runner's quarantine files."""
        directory = self.path / QUARANTINE_FILES / run_id
        for level in (directory.parent, directory):
            try:
                level.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if level.is_symlink() or not level.is_dir():
                raise RepositoryError(f"{level} is not a directory of the trusted repository")
        kept = directory / f"{self._next_number(run_id)}.bundle"
        shutil.move(copy, kept)
        os.chmod(kept, 0o400)
        return kept

    def prune_quarantine(self, now: datetime | None = None,
                         retention: timedelta = QUARANTINE_RETENTION) -> list[str]:
        """Remove quarantine refs and kept bundle files of runs older than ``retention``.

        The age is the time in the runner-made run ID, never a date a session wrote. When a ref
        goes, ``git gc --prune=now`` drops the objects only it kept. Not called by this module: the
        caller runs it under the run lease (`src.schedules.lease`), so no import runs meanwhile.
        Returns what it removed.
        """
        self.verify()
        cutoff = (now or datetime.now(timezone.utc)) - retention
        removed: list[str] = []
        prefix = f"{QUARANTINE}/"
        for name in self._git("for-each-ref", "--format=%(refname)", "--", prefix).decode().splitlines():
            started = _run_started(name[len(prefix):].split("/")[0])
            if started is not None and started < cutoff:
                self._git("update-ref", "-d", "--", name)
                removed.append(name)
        files = self.path / QUARANTINE_FILES
        if files.is_dir() and not files.is_symlink():
            for entry in sorted(os.listdir(files)):
                started = _run_started(entry)
                if started is not None and started < cutoff:
                    _remove(files / entry)
                    removed.append(str(files / entry))
        if any(name.startswith(prefix) for name in removed):
            self._git("gc", "--quiet", "--prune=now", timeout=FETCH_TIMEOUT)
        return removed

    def _check_range(self, base: str, head: str, quarantine: str, deadline: _Deadline) -> tuple[str, ...]:
        """The commits of ``base..head``, oldest first, after every check; ``BundleRefused`` otherwise."""
        if self._git_test("merge-base", "--is-ancestor", "--end-of-options", base, head,
                          timeout=deadline.left(gitsafe.GIT_TIMEOUT)).returncode != 0:
            raise BundleRefused(f"head {head} does not descend from the session base {base}", quarantine)
        counted = self._git("rev-list", "--count", "--end-of-options", f"{base}..{head}",
                            timeout=deadline.left(gitsafe.GIT_TIMEOUT)).strip()
        if not counted.isdigit():
            raise BundleRefused("unexpected rev-list --count output", quarantine)
        if int(counted) == 0:
            raise BundleRefused("the bundle adds no commits to the session base", quarantine)
        if int(counted) > MAX_IMPORT_COMMITS:
            raise BundleRefused(f"the bundle adds {int(counted)} commits, more than {MAX_IMPORT_COMMITS}", quarantine)
        output = self._git("rev-list", "--parents", "--topo-order", "--reverse", "--end-of-options",
                           f"{base}..{head}", timeout=deadline.left(gitsafe.GIT_TIMEOUT))
        commits = []
        for line in output.decode("ascii", "replace").splitlines():
            shas = line.split(" ")
            if not all(_SHA.fullmatch(sha) for sha in shas):
                raise BundleRefused(f"unexpected rev-list output: {line[:120]!r}", quarantine)
            if len(shas) != 2:
                raise BundleRefused(f"commit {shas[0]} has {len(shas) - 1} parents, not exactly one", quarantine)
            commits.append(shas[0])
        links = []
        for commit, path, mode, obj in self._changes(commits, quarantine, deadline):
            if mode == _GITLINK:
                raise BundleRefused(f"commit {commit} adds or changes the gitlink {path}", quarantine)
            if mode == _SYMLINK:
                links.append((commit, path, obj))
        targets = self._link_targets(sorted({obj for _, _, obj in links}), quarantine, deadline)
        for commit, path, obj in links:
            if _link_escapes(path, targets[obj]):
                raise BundleRefused(f"commit {commit} adds the symbolic link {path}, which points "
                                    "outside the tree", quarantine)
        return tuple(commits)

    def _bounded(self, *args: str, input: bytes, max_output: int, quarantine: str, deadline: _Deadline,
                 what: str) -> bytes:
        try:
            return gitsafe.git_bounded(self.path, *args, input=input, max_output=max_output, config=CONFIG,
                                       timeout=deadline.left(gitsafe.GIT_TIMEOUT))
        except gitsafe.OutputTooLarge:
            raise BundleRefused(f"{what} are larger than {max_output} bytes", quarantine) from None
        except gitsafe.GitTimeout:
            raise BundleRefused(f"the import took longer than {deadline.seconds} seconds", quarantine) from None
        except gitsafe.GitError as error:
            raise RepositoryError(str(error)) from None

    def _changes(self, commits: list[str], quarantine: str,
                 deadline: _Deadline) -> Iterator[tuple[str, str, str, str]]:
        """``(commit, path, new mode, new object)`` for every path a commit adds or changes.

        One ``git diff-tree --stdin`` over all commits (each against its only parent), with a
        bounded output: with ``-z`` it prints each commit's ID, then a header and a path per change.
        """
        output = self._bounded("diff-tree", "--stdin", "-r", "--no-renames", "--raw", "-z",
                               "--ignore-submodules=none", input="".join(f"{c}\n" for c in commits).encode(),
                               max_output=MAX_DIFF_OUTPUT, quarantine=quarantine, deadline=deadline,
                               what="the changes of the bundle's commits")
        fields = output.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        wanted, commit, index = set(commits), None, 0
        while index < len(fields):
            field = fields[index].decode("ascii", "replace")
            if _SHA.fullmatch(field) and field in wanted:
                commit, index = field, index + 1
                continue
            match = _RAW_HEADER.fullmatch(field)
            if commit is None or match is None or index + 1 >= len(fields):
                raise BundleRefused("unexpected diff-tree output", quarantine)
            if match.group(5) != "D":
                yield commit, fields[index + 1].decode("utf-8", "backslashreplace"), match.group(2), match.group(4)
            index += 2

    def _link_targets(self, objects: list[str], quarantine: str, deadline: _Deadline) -> dict[str, bytes]:
        """The targets of symbolic links: sizes first, then one bounded read of targets that fit."""
        if not objects:
            return {}
        if len(objects) > MAX_LINKS:
            raise BundleRefused(f"the bundle adds {len(objects)} symbolic link targets, more than {MAX_LINKS}",
                                quarantine)
        listing = "".join(f"{obj}\n" for obj in objects).encode()
        checked = self._bounded("cat-file", "--batch-check", input=listing, max_output=100 * len(objects),
                                quarantine=quarantine, deadline=deadline, what="the sizes of symbolic links")
        lines = checked.decode("ascii", "replace").splitlines()
        if len(lines) != len(objects):
            raise BundleRefused("unexpected cat-file --batch-check output", quarantine)
        total = 0
        for obj, line in zip(objects, lines):
            parts = line.split(" ")
            if len(parts) != 3 or parts[0] != obj or parts[1] != "blob" or not parts[2].isdigit():
                raise BundleRefused(f"the symbolic link object {obj} is not a blob", quarantine)
            if int(parts[2]) > MAX_LINK_TARGET:
                raise BundleRefused(f"a symbolic link target is {int(parts[2])} bytes, more than "
                                    f"{MAX_LINK_TARGET}", quarantine)
            total += int(parts[2])
        output = self._bounded("cat-file", "--batch", input=listing, max_output=total + 100 * len(objects),
                               quarantine=quarantine, deadline=deadline, what="the symbolic link targets")
        targets, offset = {}, 0
        for obj in objects:
            end = output.find(b"\n", offset)
            parts = output[offset:end].decode("ascii", "replace").split(" ") if end >= 0 else []
            if len(parts) != 3 or parts[0] != obj or parts[1] != "blob" or not parts[2].isdigit():
                raise BundleRefused("unexpected cat-file --batch output", quarantine)
            start = end + 1
            targets[obj] = output[start:start + int(parts[2])]
            offset = start + int(parts[2]) + 1
        return targets

    def _promote(self, branch: str, head: str, commits: tuple[str, ...], quarantine: str,
                 run_id: str) -> tuple[str, ...]:
        """Move ``refs/heads/<branch>`` to ``head``, fast-forward only; the commits it gains."""
        ref = f"refs/heads/{branch}"
        result = self._git_test("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
        old = result.stdout.decode("ascii", "replace").strip() if result.returncode == 0 else None
        if old is not None and not _SHA.fullmatch(old):
            raise RepositoryError(f"unexpected rev-parse output for {ref}")
        if old is not None and old != head and self._git_test(
                "merge-base", "--is-ancestor", "--end-of-options", old, head).returncode != 0:
            raise BundleRefused(f"head {head} is not a fast-forward of {ref} at {old}", quarantine)
        self._git("update-ref", "-m", f"autodev import {run_id}", "--", ref, head, old or "0" * 40)
        if old == head:
            return ()
        return commits[commits.index(old) + 1:] if old in commits else commits

    # --- environments -------------------------------------------------------------------------

    def _main_commit(self, commit: str) -> str:
        """``commit`` when it is ``pinned`` or an ancestor of it; never a session's imported head."""
        commit = _sha(commit, "commit")
        if not self._is_commit(commit) or self._git_test(
                "merge-base", "--is-ancestor", "--end-of-options", commit, PINNED_REF).returncode != 0:
            raise RepositoryError(f"{commit} is not a commit of main ({PINNED_REF}) in the trusted repository")
        return commit

    def dependency_files(self, commit: str) -> dict[str, bytes | None]:
        """The dependency files at a commit of main; ``None`` for one that is absent."""
        commit = self._main_commit(commit)
        files: dict[str, bytes | None] = {}
        for name in DEPENDENCY_FILES:
            entry = self._git("ls-tree", "-z", "--full-tree", "--end-of-options", commit, "--", name)
            if not entry:
                files[name] = None
                continue
            mode, kind = entry.split(b" ", 2)[:2]
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise RepositoryError(f"{name} at {commit} is not a regular file")
            files[name] = self._git("cat-file", "blob", "--end-of-options", f"{commit}:{name}")
        return files

    def environment(self, commit: str, runner_dir: str | os.PathLike[str],
                    builder: Builder | None = None) -> Path:
        """The read-only virtual environment for the dependency files at ``commit`` of main.

        Built in a private sibling under ``venvs/`` and renamed into place, under a lock per key
        (``venvs/<key>.lock``), so concurrent callers never see, remove or replace each other's
        environment: the second caller waits and returns the first one's.
        """
        files = self.dependency_files(commit)
        root = autodev_config.runner_location(os.fspath(runner_dir), self.installation, what="the runner directory")
        if root.is_relative_to(self.path):
            raise RepositoryError("the runner directory must lie outside the trusted repository")
        key = dependency_hash(files)
        venvs = root / "venvs"
        venvs.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_own_directory(venvs)
        venv = venvs / key[:32]
        # One caller at a time per key: no caller removes or replaces another's environment.
        with file_lock(venvs / f"{key[:32]}.lock"):
            return self._build_environment(venv, key, files, builder)

    @staticmethod
    def _build_environment(venv: Path, key: str, files: Mapping[str, bytes | None],
                           builder: Callable | None) -> Path:
        if _complete(venv, key):
            _read_only(venv)
            return venv
        venvs = venv.parent
        build: Path | None = Path(tempfile.mkdtemp(prefix=f".build-{key[:12]}-", dir=venvs))
        try:
            (builder or default_builder)(build, {name: content for name, content in files.items()
                                                 if content is not None})
            _require_own_directory(build)
            _relocate(build, venv)
            (build / COMPLETE_MARKER).write_text(key + "\n", encoding="ascii")
            _read_only(build)
            if _complete(venv, key):
                return venv
            if os.path.lexists(venv):
                _remove(venv)  # incomplete; _complete checked it is a directory of ours
            try:
                os.rename(build, venv)
            except OSError as error:
                if _complete(venv, key):  # another caller renamed its build into place first
                    return venv
                raise RepositoryError(f"the environment could not be moved to {venv}: {error}") from None
            build = None
            return venv
        finally:
            if build is not None and os.path.lexists(build):
                _remove(build)


class _Deadline:
    """The overall time limit of one import; each git command gets at most what is left."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.end = time.monotonic() + seconds
        self.quarantine: str | None = None

    def left(self, cap: float) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise BundleRefused(f"the import took longer than {self.seconds} seconds", self.quarantine)
        return min(cap, remaining)


def _run_started(run_id: str) -> datetime | None:
    """The UTC start a run ID encodes; None for a name that is not a run ID."""
    if not _RUN_ID.fullmatch(run_id):
        return None
    try:
        return datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def dependency_hash(files: Mapping[str, bytes | None]) -> str:
    """SHA-256 over each dependency file's name, presence, length and content."""
    digest = hashlib.sha256()
    for name in DEPENDENCY_FILES:
        content = files.get(name)
        if content is None:
            digest.update(f"{name}\0absent\0".encode())
        else:
            digest.update(f"{name}\0{len(content)}\0".encode() + content)
    return digest.hexdigest()


def builder_environment() -> dict[str, str]:
    """The minimal environment of the default builder: no inherited pip, Python or proxy settings."""
    values = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    values.update(LC_ALL="C.UTF-8", PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1",
                  PYTHONNOUSERSITE="1")
    return values


def default_builder(venv: Path, files: Mapping[str, bytes]) -> None:
    """``python -m venv`` and ``pip install -r requirements.txt`` of main, in a minimal environment."""
    environment = builder_environment()
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, stdin=subprocess.DEVNULL,
                   timeout=VENV_TIMEOUT, env=environment)
    if "requirements.txt" not in files:
        return
    with tempfile.TemporaryDirectory(prefix="autodev-requirements-") as private:
        requirements = Path(private) / "requirements.txt"
        requirements.write_bytes(files["requirements.txt"])
        subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-input", "-r", str(requirements)], check=True, stdin=subprocess.DEVNULL,
                       timeout=VENV_TIMEOUT, env=environment)


def _require_own_directory(path: Path) -> None:
    """``path`` itself (``lstat``) is a directory owned by this user, not a symbolic link."""
    try:
        status = os.lstat(path)
    except OSError as error:
        raise RepositoryError(f"{path} cannot be read: {error.strerror}") from None
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise RepositoryError(f"{path} is a symbolic link or not a directory")
    if status.st_uid != os.getuid():
        raise RepositoryError(f"{path} is not owned by the runner's user")


def _complete(venv: Path, key: str) -> bool:
    """Whether ``venv`` is a finished environment for ``key``; ``RepositoryError`` when it is not ours."""
    if not os.path.lexists(venv):
        return False
    _require_own_directory(venv)
    marker = venv / COMPLETE_MARKER
    try:
        status = os.lstat(marker)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(status.st_mode) and marker.read_text(encoding="ascii").strip() == key


def _relocate(build: Path, venv: Path) -> None:
    """Point the scripts and ``pyvenv.cfg`` a builder wrote at ``venv``, the environment's final path."""
    old, new = os.fsencode(build), os.fsencode(venv)
    bin_dir = build / "bin"
    candidates = [build / "pyvenv.cfg"]
    if bin_dir.is_dir() and not bin_dir.is_symlink():
        candidates += [bin_dir / name for name in os.listdir(bin_dir)]
    for path in candidates:
        try:
            status = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(status.st_mode) or status.st_size > 1024 * 1024:
            continue
        content = path.read_bytes()
        if old in content:
            path.write_bytes(content.replace(old, new))


def _read_only(root: Path) -> None:
    """Remove every write bit below ``root`` (symbolic links are left alone); never through a link."""
    if os.path.islink(root):
        raise RepositoryError(f"{root} is a symbolic link")
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)
        os.chmod(directory, stat.S_IMODE(os.lstat(directory).st_mode) & ~0o222)


def _remove(root: Path) -> None:
    """Remove an incomplete environment or a kept bundle directory, read-only or not."""
    if root.is_symlink() or not root.is_dir():
        root.unlink()
        return
    for directory, _, _ in os.walk(root):
        os.chmod(directory, stat.S_IMODE(os.lstat(directory).st_mode) | 0o700)
    shutil.rmtree(root)


def _link_escapes(path: str, target: bytes) -> bool:
    """Whether a symbolic link at ``path`` with ``target`` may resolve outside the tree.

    Absolute and empty targets escape. Leading ``..`` components climb from the link's directory,
    which is a real directory of the tree; a ``..`` after a named component is refused too,
    because that component may itself be a link.
    """
    if not target or b"\0" in target or target.startswith(b"/"):
        return True
    try:
        text = target.decode("utf-8")
    except UnicodeDecodeError:
        return True
    depth = len(posixpath.dirname(path).split("/")) if posixpath.dirname(path) else 0
    named = False
    for component in text.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if named:
                return True
            depth -= 1
            if depth < 0:
                return True
        else:
            named = True
    return False


def _copy_bundle(bundle: Path, exchange: Path, copy: Path) -> None:
    """Copy a regular, non-link, single-link, bounded bundle file directly inside ``exchange``."""
    try:
        exchange = exchange.resolve(strict=True)
    except OSError as error:
        raise BundleRefused(f"the exchange directory cannot be opened: {error}") from None
    location = bundle if bundle.is_absolute() else exchange / bundle
    name = location.name
    if not name or name in (".", "..") or Path(os.path.abspath(location.parent)).resolve() != exchange:
        raise BundleRefused(f"the bundle {bundle} is not directly inside the exchange directory {exchange}")
    try:
        directory = os.open(exchange, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise BundleRefused(f"the exchange directory cannot be opened: {error.strerror}") from None
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        if os.path.islink(location):
            raise BundleRefused(f"the bundle {name} is a symbolic link") from None
        raise BundleRefused(f"the bundle {name} cannot be opened: {error.strerror}") from None
    finally:
        os.close(directory)
    try:
        status = os.fstat(source)
        if not stat.S_ISREG(status.st_mode):
            raise BundleRefused(f"the bundle {name} is not a regular file")
        if status.st_nlink != 1:
            raise BundleRefused(f"the bundle {name} has {status.st_nlink} hard links, not exactly one")
        with open(copy, "xb") as writer:
            os.fchmod(writer.fileno(), 0o600)
            copied = 0
            while chunk := os.read(source, 1024 * 1024):
                copied += len(chunk)
                if copied > MAX_BUNDLE_BYTES:  # also when the file grows after fstat
                    raise BundleRefused(f"the bundle {name} is larger than {MAX_BUNDLE_BYTES} bytes")
                writer.write(chunk)
    except OSError as error:
        raise BundleRefused(f"the bundle {name} cannot be read: {error.strerror}") from None
    finally:
        os.close(source)
