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
  regular file, not a symbolic link, of bounded size, copied before git reads it. The bundle must
  name exactly ``refs/heads/<expected branch>`` with a 40-hex head and have its prerequisites in
  the trusted repository. Its objects are fetched with fsck into
  ``refs/autodev/quarantine/<run-id>/<n>``; the head must descend from ``pinned``, every commit of
  ``pinned..head`` must have exactly one parent, and no commit may add or change a gitlink or a
  symbolic link that is absolute or leaves the tree. Only then is the head promoted to
  ``refs/heads/<branch>`` and the quarantine ref removed. A refusal raises ``BundleRefused``
  naming the reason; objects already fetched stay under the quarantine ref.
* **Push.** ``push_arguments`` builds the git arguments of the broker's push (#342): an explicit
  URL and ``<sha>:refs/heads/claude/autodev-<issue>-<slug>``. Nothing here pushes.
* **Environment.** ``environment`` returns a virtual environment under the runner's directory,
  keyed by the SHA-256 of ``requirements.txt``, ``pyproject.toml`` and ``uv.lock`` at ``pinned``
  (read with ``git cat-file blob``), built by an injectable builder only when it is missing or has
  no completion marker, then made read-only.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
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

from . import config as autodev_config
from . import gitsafe

PINNED_REF = "refs/remotes/origin/main"
MAIN_REFSPEC = f"+refs/heads/main:{PINNED_REF}"
QUARANTINE = "refs/autodev/quarantine"
# fsck on every object that arrives; no automatic gc or maintenance, which could run later.
CONFIG = ("transfer.fsckObjects=true", "gc.auto=0", "maintenance.auto=false", "advice.detachedHead=false")
FETCH_TIMEOUT = 600
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
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


class BundleRefused(RepositoryError):
    """A session's bundle was not imported; ``quarantine_ref`` holds its objects when they were fetched."""

    def __init__(self, reason: str, quarantine_ref: str | None = None):
        super().__init__(reason if quarantine_ref is None else f"{reason} (kept under {quarantine_ref})")
        self.reason = reason
        self.quarantine_ref = quarantine_ref


@dataclass(frozen=True)
class SessionRepository:
    """A one-off repository handed to one session. Nothing in this module runs git in it again."""

    path: Path
    pinned: str


@dataclass(frozen=True)
class ImportedBundle:
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
        except gitsafe.GitError as error:
            raise RepositoryError(str(error)) from None

    def _git_test(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return gitsafe.git_test(self.path, *args, config=CONFIG)
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
                      run_id: str) -> ImportedBundle:
        """Verify a session's bundle and promote its head to ``refs/heads/<branch>``; see the module docstring."""
        branch = _branch(branch)
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError(f"not a run ID: {run_id!r}")
        self.verify()
        pinned = self.pinned()
        with tempfile.TemporaryDirectory(prefix="autodev-bundle-") as private:
            copy = Path(private) / "session.bundle"
            _copy_bundle(Path(bundle), Path(exchange), copy)
            head = self._bundle_head(copy, branch)
            verified = self._git_test("bundle", "verify", str(copy))
            if verified.returncode != 0:
                detail = verified.stderr.decode("utf-8", "replace").strip()[:300]
                reason = "bundle prerequisites missing from the trusted repository" \
                    if "prerequisite" in detail else "bundle does not verify"
                raise BundleRefused(f"{reason}: {detail}")
            quarantine = self._next_quarantine(run_id)
            try:
                self._git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                          "--", str(copy), f"refs/heads/{branch}:{quarantine}", timeout=FETCH_TIMEOUT)
            except RepositoryError as error:
                raise BundleRefused(f"bundle fetch failed: {error}") from None
        fetched = self._git("rev-parse", "--verify", "--end-of-options", quarantine).decode().strip()
        if fetched != head:
            raise BundleRefused(f"fetched head {fetched} is not the listed head {head}", quarantine)
        commits = self._check_range(pinned, head, quarantine)
        self._git("update-ref", "-m", f"autodev import {run_id}", "--", f"refs/heads/{branch}", head)
        self._git("update-ref", "-d", "--", quarantine, head)
        return ImportedBundle(branch, head, commits)

    def _bundle_head(self, copy: Path, branch: str) -> str:
        listed = self._git_test("bundle", "list-heads", str(copy))
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

    def _next_quarantine(self, run_id: str) -> str:
        prefix = f"{QUARANTINE}/{run_id}/"
        taken = self._git("for-each-ref", "--format=%(refname)", "--", prefix).decode().splitlines()
        numbers = [int(name[len(prefix):]) for name in taken if name[len(prefix):].isdigit()]
        return f"{prefix}{max(numbers, default=0) + 1}"

    def _check_range(self, pinned: str, head: str, quarantine: str) -> tuple[str, ...]:
        if self._git_test("merge-base", "--is-ancestor", "--end-of-options", pinned, head).returncode != 0:
            raise BundleRefused(f"head {head} does not descend from pinned {pinned}", quarantine)
        output = self._git("rev-list", "--parents", "--topo-order", "--reverse", "--end-of-options",
                           f"{pinned}..{head}")
        commits = []
        for line in output.decode("ascii", "replace").splitlines():
            shas = line.split(" ")
            if not all(_SHA.fullmatch(sha) for sha in shas):
                raise BundleRefused(f"unexpected rev-list output: {line[:120]!r}", quarantine)
            if len(shas) != 2:
                raise BundleRefused(f"commit {shas[0]} has {len(shas) - 1} parents, not exactly one", quarantine)
            commits.append(shas[0])
            for path, mode, obj in self._added(shas[1], shas[0], quarantine):
                if mode == _GITLINK:
                    raise BundleRefused(f"commit {shas[0]} adds or changes the gitlink {path}", quarantine)
                if mode == _SYMLINK and _link_escapes(path, self._git("cat-file", "blob", "--end-of-options", obj)):
                    raise BundleRefused(f"commit {shas[0]} adds the symbolic link {path}, which points "
                                        "outside the tree", quarantine)
        if not commits:
            raise BundleRefused("the bundle adds no commits to pinned", quarantine)
        return tuple(commits)

    def _added(self, parent: str, commit: str, quarantine: str) -> Iterator[tuple[str, str, str]]:
        """``(path, new mode, new object)`` for every path ``commit`` adds or changes."""
        output = self._git("diff-tree", "-r", "--no-renames", "--raw", "-z", "--no-commit-id",
                           "--ignore-submodules=none", "--end-of-options", parent, commit)
        fields = output.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 2:
            raise BundleRefused(f"unexpected diff-tree output for {commit}", quarantine)
        for header, path in zip(fields[::2], fields[1::2]):
            match = _RAW_HEADER.fullmatch(header.decode("ascii", "replace"))
            if match is None:
                raise BundleRefused(f"unexpected diff-tree record for {commit}", quarantine)
            if match.group(5) != "D":
                yield path.decode("utf-8", "backslashreplace"), match.group(2), match.group(4)

    # --- environments -------------------------------------------------------------------------

    def dependency_files(self, pinned: str) -> dict[str, bytes | None]:
        """The dependency files at ``pinned``; ``None`` for one that is absent."""
        pinned = _sha(pinned, "pinned")
        if not self._is_commit(pinned):
            raise RepositoryError(f"{pinned} is not a commit in the trusted repository")
        files: dict[str, bytes | None] = {}
        for name in DEPENDENCY_FILES:
            entry = self._git("ls-tree", "-z", "--full-tree", "--end-of-options", pinned, "--", name)
            if not entry:
                files[name] = None
                continue
            mode, kind = entry.split(b" ", 2)[:2]
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise RepositoryError(f"{name} at {pinned} is not a regular file")
            files[name] = self._git("cat-file", "blob", "--end-of-options", f"{pinned}:{name}")
        return files

    def environment(self, pinned: str, runner_dir: str | os.PathLike[str],
                    builder: Builder | None = None) -> Path:
        """The read-only virtual environment for the dependency files at ``pinned``."""
        files = self.dependency_files(pinned)
        root = autodev_config.runner_location(os.fspath(runner_dir), self.installation, what="the runner directory")
        if root.is_relative_to(self.path):
            raise RepositoryError("the runner directory must lie outside the trusted repository")
        key = dependency_hash(files)
        venv = root / "venvs" / key[:32]
        marker = venv / COMPLETE_MARKER
        if marker.is_file() and not marker.is_symlink() and marker.read_text(encoding="ascii").strip() == key:
            _read_only(venv)
            return venv
        if os.path.lexists(venv):
            _remove(venv)
        venv.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        (builder or default_builder)(venv, {name: content for name, content in files.items() if content is not None})
        if not venv.is_dir():
            raise RepositoryError(f"the builder did not create {venv}")
        marker.write_text(key + "\n", encoding="ascii")
        _read_only(venv)
        return venv


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


def default_builder(venv: Path, files: Mapping[str, bytes]) -> None:
    """``python -m venv`` and ``pip install -r requirements.txt`` at ``pinned``."""
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, stdin=subprocess.DEVNULL,
                   timeout=VENV_TIMEOUT)
    if "requirements.txt" not in files:
        return
    with tempfile.TemporaryDirectory(prefix="autodev-requirements-") as private:
        requirements = Path(private) / "requirements.txt"
        requirements.write_bytes(files["requirements.txt"])
        subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-input", "-r", str(requirements)], check=True, stdin=subprocess.DEVNULL,
                       timeout=VENV_TIMEOUT)


def _read_only(root: Path) -> None:
    """Remove every write bit below ``root`` (symbolic links are left alone)."""
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)
        os.chmod(directory, stat.S_IMODE(os.lstat(directory).st_mode) & ~0o222)


def _remove(root: Path) -> None:
    """Remove an incomplete environment, read-only or not."""
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
    """Copy a regular, non-link, bounded bundle file directly inside ``exchange`` to ``copy``."""
    try:
        exchange = exchange.resolve(strict=True)
    except OSError as error:
        raise BundleRefused(f"the exchange directory cannot be opened: {error}") from None
    location = bundle if bundle.is_absolute() else exchange / bundle
    name = location.name
    if not name or name in (".", "..") or Path(os.path.abspath(location.parent)).resolve() != exchange:
        raise BundleRefused(f"the bundle {bundle} is not directly inside the exchange directory {exchange}")
    directory = os.open(exchange, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError as error:
            if os.path.islink(location):
                raise BundleRefused(f"the bundle {name} is a symbolic link") from None
            raise BundleRefused(f"the bundle {name} cannot be opened: {error.strerror}") from None
    finally:
        os.close(directory)
    with os.fdopen(source, "rb") as reader, open(copy, "xb") as writer:
        os.fchmod(writer.fileno(), 0o600)
        status = os.fstat(reader.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise BundleRefused(f"the bundle {name} is not a regular file")
        copied = 0
        while chunk := reader.read(1024 * 1024):
            copied += len(chunk)
            if copied > MAX_BUNDLE_BYTES:  # also when the file grows after fstat
                raise BundleRefused(f"the bundle {name} is larger than {MAX_BUNDLE_BYTES} bytes")
            writer.write(chunk)
