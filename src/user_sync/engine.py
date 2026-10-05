"""The sync engine: setup, preview, joining, the sync cycle, conflicts and status.

The personal library (``flows/.user``, or ``AGENTS_USER_FLOWS_DIR``) becomes the working tree of a
git repository whose only remote is one private repository per user, on one branch that is never
force-pushed. Every runner uses this module: the macOS daemon, the OS scheduler, stdio servers, the
web UI, the installer and the command line (``python -m src.user_sync``).

One cycle:

1. ``git fetch`` without any lock.
2. Under the library's ``.lock``, which every writer of ``src.user_flows`` and
   ``src.component_toggles`` takes: read the working tree once (scope rules, size limit, secret
   scanner), commit what changed, integrate the remote commit (nothing, a fast-forward or a merge
   by the policy of ``src.user_sync.merge``) and write the result to the working tree. Writers wait
   for these local steps only, never for the network. Before replacing or deleting a file the
   engine checks that it still holds the bytes it read, so even an edit that bypasses the lock is
   kept: the file stays and the remote version is reconciled as a conflict on the next cycle.
3. ``git push`` without any lock. A non-fast-forward rejection starts again at step 1, at most
   three times. History is never rewritten and never force-pushed.

One syncer runs per library: ``.git/agents-sync.lock`` is taken without waiting, and a second
runner reports ``lock_held``. Git lock files that a killed run left behind are removed once they
are older than a few seconds, because no other syncer can hold them.

Joining a library that already has flows to a remote with flows (no common history), or a remote
whose history was rewritten, needs the owner's confirmation of the exact preview, identified by
its hash; so does the first upload. Network failures retry after 1, 2, 5, 10 and 30 minutes;
access and safety failures stop with a reason until the owner acts.

Settings, state, this machine's key, ``known_hosts``, the isolated ``gitconfig`` and the log live
in a private per-installation directory (``default_state_dir``), never in the library.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time

from src import user_library
from src.file_lock import file_lock
from src.user_library import FORMAT, MARKER, REPO_META
from src.user_sync import gitcmd, keys, merge as merging, scope
from src.user_sync.gitcmd import Git, GitError, Remote, RemoteError, parse_remote
from src.user_sync.scope import CONFLICTS_DIR, MAX_FILE_BYTES, SCOPES_PATH, Rules, Scopes, Snapshot

SETTINGS_FILE = "user-sync.json"
STATE_FILE = "user-sync-state.json"
LOG_FILE = "user-sync.log"
LOG_BYTES = 1024 * 1024
LOG_BACKUPS = 3
SYNC_LOCK = "agents-sync.lock"
MANAGED_KEY = "agents-sync.managed"
TEMPORARY_INDEX = "agents-sync.index"
RETRY_MINUTES = (1, 2, 5, 10, 30)
STALE_AFTER = timedelta(hours=24)
ATTENTION_AFTER = timedelta(hours=72)
VISIBILITY_EVERY = timedelta(hours=24)
PUSH_ATTEMPTS = 3
ACTIVITY_LIMIT = 20
ANNOUNCED_LIMIT = 50
SIZE_WARNING = 200 * 1024 * 1024
STALE_GIT_LOCK_SECONDS = 10
TRAILER = "Agents-Sync-Machine"

_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_CONFLICT_ID = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{10}")
_ZERO = "0" * 40


class SyncError(Exception):
    """Sync stopped for ``reason``; ``state`` is ``attention`` unless the failure is temporary."""

    def __init__(self, reason: str, message: str, *, state: str = "attention"):
        super().__init__(message)
        self.reason, self.message, self.state = reason, message, state


class _ChangedWhileReading(Exception):
    """A file changed between reading it and storing it although the library lock was held."""


# --- locations ----------------------------------------------------------------------------


def installation_root() -> Path:
    return Path(__file__).resolve().parents[2]


def installation_id(root: Path | None = None) -> str:
    """The daemon's id for this installation: the first 16 hex digits of SHA-256 of its path."""
    return hashlib.sha256(str(Path(root or installation_root()).resolve()).encode()).hexdigest()[:16]


def default_state_dir() -> Path:
    """``<per-installation private directory>/user-sync`` (see the module docstring of config)."""
    if sys.platform == "darwin":
        from src.daemon.state import state_dir  # honors AGENTS_SERVICE_DIR inside the daemon
        return state_dir() / "user-sync"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Agents-Core"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "agents-core"
    return base / installation_id() / "user-sync"


def default_library() -> Path:
    """The personal library, as ``src.user_flows.FlowLibrary`` resolves it."""
    configured = os.environ.get("AGENTS_USER_FLOWS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    from src.engine.config import FLOWS_DIR
    return (Path(FLOWS_DIR) / ".user").resolve()


def default_label() -> str:
    """A neutral machine label: the platform and a random suffix, never the hostname."""
    platform = {"darwin": "mac", "win32": "windows"}.get(sys.platform, "linux")
    return f"{platform}-{secrets.token_hex(2)}"


def _now_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise SyncError("state", f"{path} must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def _write_private(path: Path, data: bytes) -> None:
    """Atomic write with 0600 on POSIX; ``tempfile`` creates the file private."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as stream:
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            os.unlink(stream.name)
            raise
    os.replace(stream.name, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- settings -----------------------------------------------------------------------------


@dataclass
class Settings:
    """``user-sync.json``. Identity is required and never read from the user's git config."""

    remote: str
    name: str
    email: str
    label: str
    branch: str = "main"
    fetch_minutes: int = 5
    ask_new_repositories: bool = False
    paused: bool = False
    started: str | None = None          # first successful upload or join
    private_confirmed: bool = False     # the owner confirmed privacy where no check exists
    approved_groups: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Settings | None":
        data = _read_json(path)
        if not isinstance(data, dict):
            return None
        known = {item.name for item in fields(cls)}
        try:
            return cls(**{key: value for key, value in data.items() if key in known})
        except TypeError:
            return None

    def save(self, path: Path) -> None:
        _write_private(path, json.dumps(asdict(self), indent=2).encode() + b"\n")


def validate_identity(name: str, email: str, label: str) -> None:
    if not isinstance(name, str) or not name.strip() or any(ord(ch) < 32 for ch in name):
        raise SyncError("identity", "a commit name is required")
    if not isinstance(email, str) or not _EMAIL.fullmatch(email):
        raise SyncError("identity", "a commit email is required")
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise SyncError("identity", "the machine label must be 1-32 lowercase letters, digits or dashes")


# --- describing changes -------------------------------------------------------------------


def change_label(path: str) -> str | None:
    """How a commit message names a changed path; None for paths it leaves implied."""
    if path.startswith((".history/", f"{CONFLICTS_DIR}/")) or path in scope.LIBRARY_FILES:
        return None
    if path == scope.COMPONENTS:
        return "components"
    if path == SCOPES_PATH:
        return "scopes"
    if path.endswith(f"/{REPO_META}"):
        return None
    reference = merging.flow_reference(path).get("flow")
    if reference and path.startswith("personas/"):
        return f"persona {reference}"
    match = re.fullmatch(r"personas/builtin/([a-z0-9-]+)\.json", path)
    if match:
        return f"persona builtin:{match[1]}"
    return reference or path


def describe(old: dict, new: dict, limit: int = 6) -> str:
    """``add user:a, user:b, update repo:c, persona builtin:d`` for the changes from ``old`` to ``new``."""
    named: dict[str, str] = {}  # name -> verb ("" for names without one), in order of appearance
    for path in changed_paths(old, new):
        name = change_label(path)
        if name is None:
            continue
        verb = ""
        if name.startswith(("user:", "repo:")):
            verb = "update"
            if path.endswith(".md"):
                verb = "add" if path not in old else "delete" if path not in new else "update"
        if named.get(name) in (None, "update"):
            named[name] = verb if named.get(name) is None or verb != "update" else named[name]
    if not named:
        return "update library files"
    items, previous = [], None
    for name, verb in list(named.items())[:limit]:
        items.append(f"{verb} {name}" if verb and verb != previous else name)
        previous = verb or None
    text = ", ".join(items)
    return text + (f" and {len(named) - limit} more" if len(named) > limit else "")


def changed_paths(old: dict, new: dict) -> list[str]:
    return sorted(path for path in set(old) | set(new) if old.get(path) != new.get(path))


# --- the engine ---------------------------------------------------------------------------


@dataclass
class _Plan:
    """What integrating the remote would do; computed under the library lock."""

    local_head: str | None
    remote_head: str | None
    snapshot: Snapshot
    current: dict
    desired: dict
    remote_tree: dict
    target: dict | None = None             # the tree to write, None when nothing comes in
    merged: merging.MergeResult | None = None
    kind: str = "none"                     # none, push, fast_forward, merge, join
    held_groups: frozenset = frozenset()
    remote_sizes: dict = field(default_factory=dict)  # blob id -> size, for the preview


class Syncer:
    """Sync of one library. Tests pass a local bare repository with ``allow_file_remote``."""

    def __init__(self, library: Path | None = None, state_dir: Path | None = None, *,
                 visibility=None, github_host_keys=None, scan_host_keys=None,
                 allow_file_remote: bool = False, ssh_command: str | None = None, clock=None):
        self.library = Path(library or default_library()).expanduser().resolve()
        self.state_dir = Path(state_dir or default_state_dir()).expanduser().resolve()
        self._visibility = visibility or self.anonymous_visibility
        self._github_host_keys = github_host_keys or keys.github_host_keys
        self._scan_host_keys = scan_host_keys or keys.scan_host_keys
        self.allow_file_remote = allow_file_remote
        self._ssh_override = ssh_command
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --- files of the state directory -------------------------------------------------

    @property
    def settings_path(self) -> Path:
        return self.state_dir / SETTINGS_FILE

    @property
    def state_path(self) -> Path:
        return self.state_dir / STATE_FILE

    @property
    def gitconfig(self) -> Path:
        return self.state_dir / "gitconfig"

    @property
    def known_hosts(self) -> Path:
        return self.state_dir / "known_hosts"

    def settings(self) -> Settings | None:
        return Settings.load(self.settings_path)

    def _state(self) -> dict:
        value = _read_json(self.state_path)
        return value if isinstance(value, dict) else {}

    def _save_state(self, state: dict) -> None:
        _private_dir(self.state_dir)
        _write_private(self.state_path, json.dumps(state, indent=2, ensure_ascii=False).encode() + b"\n")

    def _log(self, message: str) -> None:
        """Append to ``user-sync.log``, rotated at 1 MiB with three backups. Never holds secrets."""
        try:
            path = self.state_dir / LOG_FILE
            if path.exists() and path.stat().st_size > LOG_BYTES:
                for index in range(LOG_BACKUPS - 1, 0, -1):
                    older = path.with_name(f"{LOG_FILE}.{index}")
                    if older.exists():
                        os.replace(older, path.with_name(f"{LOG_FILE}.{index + 1}"))
                os.replace(path, path.with_name(f"{LOG_FILE}.1"))
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(f"{_now_iso(self._clock())} {message}\n")
            if os.name == "posix":
                path.chmod(0o600)
        except OSError:
            pass

    def _write_support_files(self, settings: Settings) -> None:
        _private_dir(self.state_dir)
        hooks = _private_dir(self.state_dir / "hooks")  # stays empty: no hook ever runs
        _write_private(self.gitconfig, gitcmd.isolated_config(
            name=settings.name, email=settings.email, hooks=hooks).encode("utf-8"))
        for name in ("ssh_config", "empty-gitconfig"):
            if not (self.state_dir / name).exists():
                _write_private(self.state_dir / name, b"")

    def _remote(self, settings: Settings) -> Remote:
        try:
            return parse_remote(settings.remote, allow_file=self.allow_file_remote)
        except RemoteError as error:
            raise SyncError("unknown_remote", str(error)) from None

    def _git(self) -> Git:
        ssh = self._ssh_override or gitcmd.ssh_command(
            keys.key_path(self.state_dir), self.known_hosts, self.state_dir / "ssh_config")
        return Git(self.library, config=self.gitconfig, ssh=ssh, allow_file=self.allow_file_remote)

    # --- locks ------------------------------------------------------------------------

    @contextmanager
    def _sync_lock(self):
        """The single-syncer lock; raises ``SyncError(lock_held)`` when another runner has it."""
        try:
            with file_lock(self.library / ".git" / SYNC_LOCK, blocking=False):
                yield
        except BlockingIOError:
            raise SyncError("lock_held", "another sync of this library is running", state="busy") from None

    def _library_lock(self):
        return file_lock(self.library / ".lock")

    def _lock_busy(self) -> bool:
        if not (self.library / ".git").is_dir():
            return False
        try:
            with file_lock(self.library / ".git" / SYNC_LOCK, blocking=False):
                return False
        except BlockingIOError:
            return True
        except OSError:
            return False

    def _clear_stale_git_locks(self) -> None:
        """Remove git lock files a killed run left; called while holding the sync lock."""
        git_dir = self.library / ".git"
        candidates = [git_dir / name for name in ("index.lock", "HEAD.lock", "config.lock",
                                                  "packed-refs.lock", "shallow.lock",
                                                  f"{TEMPORARY_INDEX}.lock")]
        try:
            candidates += list((git_dir / "refs").rglob("*.lock"))
        except OSError:
            pass
        now = time.time()
        for path in candidates:
            try:
                age = now - path.lstat().st_mtime
            except OSError:
                continue
            if age >= STALE_GIT_LOCK_SECONDS:
                path.unlink(missing_ok=True)
                self._log(f"removed a stale git lock {path.relative_to(git_dir)}")

    # --- the repository ---------------------------------------------------------------

    def _check_git(self) -> None:
        version = gitcmd.git_version()
        if version is None:
            raise SyncError("git_too_old", "git is not installed or cannot run")
        if version < gitcmd.MIN_GIT_VERSION:
            wanted = ".".join(map(str, gitcmd.MIN_GIT_VERSION))
            raise SyncError("git_too_old", f"git {'.'.join(map(str, version))} is too old; sync needs {wanted} or newer")

    def _repository(self, settings: Settings, *, create: bool) -> Git:
        """The library's repository, initialized when ``create``; refuses a ``.git`` it did not make."""
        git = self._git()
        self._write_support_files(settings)
        dot_git = self.library / ".git"
        if dot_git.is_symlink() or (dot_git.exists() and not dot_git.is_dir()):
            raise SyncError("foreign_git", f"{dot_git} is not a repository created by Agents-Core")
        if not dot_git.exists():
            if not create:
                raise SyncError("not_set_up", "the library has no sync repository; run setup")
            self.library.mkdir(parents=True, exist_ok=True, mode=0o700)
            git.run("init", "--quiet", f"--initial-branch={settings.branch}", str(self.library),
                    repository=False)
            git.run("config", MANAGED_KEY, "true")
        elif git.text("config", "--get", MANAGED_KEY, check=False) != "true":
            raise SyncError("foreign_git", f"{dot_git} exists but was not created by Agents-Core; "
                                           "move it away to set up sync")
        url = git.text("config", "--get", "remote.origin.url", check=False)
        if url != settings.remote:
            git.run("remote", "set-url" if url else "add", "origin", settings.remote)
        # A glob refspec: fetching a branch the remote does not have yet must not fail.
        refspec = "+refs/heads/*:refs/remotes/origin/*"
        if git.text("config", "--get-all", "remote.origin.fetch", check=False) != refspec:
            git.run("config", "--replace-all", "remote.origin.fetch", refspec)
        if git.text("symbolic-ref", "HEAD", check=False) != f"refs/heads/{settings.branch}":
            git.run("symbolic-ref", "HEAD", f"refs/heads/{settings.branch}")
        return git

    def _rev(self, git: Git, ref: str) -> str | None:
        result = git.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        value = result.stdout.decode().strip()
        return value if result.returncode == 0 and value else None

    def _is_ancestor(self, git: Git, older: str, newer: str) -> bool:
        return git.run("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def _merge_base(self, git: Git, one: str, other: str) -> str | None:
        result = git.run("merge-base", one, other, check=False)
        return result.stdout.decode().strip() or None if result.returncode == 0 else None

    def _tree(self, git: Git, commit: str | None) -> dict[str, str]:
        """Regular files of ``commit`` as path -> blob id; other entries never materialize."""
        if commit is None:
            return {}
        entries = {}
        for record in git.run("ls-tree", "-r", "-z", "--full-tree", commit).stdout.split(b"\0"):
            if not record:
                continue
            meta, _, raw = record.partition(b"\t")
            mode, kind, blob = meta.decode("ascii").split()
            try:
                path = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if kind == "blob" and mode in ("100644", "100755"):
                entries[path] = blob
        return entries

    def _read_blobs(self, git: Git, blobs) -> dict[str, bytes]:
        wanted = sorted(set(blobs))
        if not wanted:
            return {}
        output = git.run("cat-file", "--batch", input="".join(f"{blob}\n" for blob in wanted).encode()).stdout
        result, position = {}, 0
        for blob in wanted:
            end = output.index(b"\n", position)
            header = output[position:end].split()
            if len(header) < 3 or header[1] != b"blob":
                raise GitError("cat-file", "local", f"object {blob} is missing")
            size = int(header[2])
            result[blob] = output[end + 1:end + 1 + size]
            position = end + 1 + size + 1
        return result

    def _blob_sizes(self, git: Git, blobs) -> dict[str, int]:
        wanted = sorted(set(blobs))
        if not wanted:
            return {}
        output = git.run("cat-file", "--batch-check", input="".join(f"{b}\n" for b in wanted).encode()).stdout
        sizes = {}
        for line in output.decode().splitlines():
            parts = line.split()
            if len(parts) == 3:
                sizes[parts[0]] = int(parts[2])
        return sizes

    def _store_files(self, git: Git, paths: list[str], expected: dict[str, str]) -> None:
        """Write these working-tree files as blobs; they must still hold the snapshot's bytes."""
        if not paths:
            return
        output = git.text("hash-object", "-w", "--no-filters", "--stdin-paths",
                          input="".join(f"{path}\n" for path in paths).encode("utf-8"))
        for path, blob in zip(paths, output.split()):
            if blob != expected[path]:
                raise _ChangedWhileReading(path)

    def _store_bytes(self, git: Git, data: bytes) -> str:
        return git.text("hash-object", "-w", "--stdin", input=data)

    def _write_tree(self, git: Git, tree: dict[str, str]) -> str:
        index = self.library / ".git" / TEMPORARY_INDEX
        index.unlink(missing_ok=True)
        try:
            entries = b"".join(f"100644 blob {blob}\t{path}".encode("utf-8") + b"\0"
                               for path, blob in sorted(tree.items()))
            git.run("update-index", "-z", "--index-info", input=entries, index=index)
            return git.text("write-tree", index=index)
        finally:
            index.unlink(missing_ok=True)

    def _commit(self, git: Git, settings: Settings, tree: dict[str, str], parents, summary: str) -> str:
        message = f"sync({settings.label}): {summary}\n\n{TRAILER}: {settings.label}\n"
        arguments = ["commit-tree", self._write_tree(git, tree)]
        for parent in parents:
            arguments += ["-p", parent]
        return git.text(*arguments, input=message.encode("utf-8"))

    def _set_head(self, git: Git, settings: Settings, commit: str, old: str | None) -> None:
        git.run("update-ref", "-m", "agents-sync", f"refs/heads/{settings.branch}", commit, old or _ZERO)
        git.run("read-tree", commit)  # keep the real index equal to HEAD for anyone running git status

    # --- reading the library ----------------------------------------------------------

    def _working_scopes(self) -> Scopes:
        path = self.library / SCOPES_PATH
        if path.is_symlink():
            raise scope.ScopeError(f"{SCOPES_PATH} must not be a symlink")
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            data = None
        return scope.parse_scopes(data)

    def _groups_in(self, tree: dict) -> set[str]:
        return {group for group in map(scope.repo_group, tree) if group}

    def _held_groups(self, settings: Settings, scopes: Scopes, *trees: dict) -> frozenset[str]:
        """New repository groups waiting for approval when "ask before uploading" is on."""
        if not settings.ask_new_repositories:
            return frozenset()
        known = set().union(*(self._groups_in(tree) for tree in trees))
        portable = scope.read_portable(self.library)
        return frozenset(group for group in portable if group not in known
                         and group not in settings.approved_groups and group not in scopes.exclude)

    def _desired(self, snap: Snapshot, current: dict, unwritten=()) -> dict:
        """The tree to commit: the snapshot's files, with some paths kept at their committed version.

        Kept are held and blocked files, and ``unwritten`` paths: versions from the remote that this
        machine did not write yet (``held_remote``). Committing the working tree over them would
        delete or replace another machine's text without a conflict.
        """
        desired = dict(snap.files)
        keep = set(snap.blocked) | set(snap.held) | set(unwritten)
        for path, blob in current.items():
            if path in keep:
                desired[path] = blob
        return desired

    def _rules_for(self, tree: dict, read, held_groups: frozenset = frozenset()) -> Rules:
        """Scope rules as ``tree`` defines them: its scopes file and its portable groups.

        Values are blob ids, which ``read`` resolves, or new bytes.
        """
        wanted = {path: blob for path, blob in tree.items()
                  if path == SCOPES_PATH or re.fullmatch(rf"repos/[^/]+/{re.escape(REPO_META)}", path)}
        def data(path):
            value = wanted[path]
            return value if isinstance(value, bytes) else read(value)
        scopes = scope.parse_scopes(data(SCOPES_PATH)) if SCOPES_PATH in wanted else Scopes()
        portable = {}
        for path in wanted:
            if path != SCOPES_PATH:
                origin = scope.portable_origin(data(path))
                if origin:
                    portable[path.rsplit("/", 1)[0]] = origin
        return Rules(scopes, portable, held_groups)

    # --- status -----------------------------------------------------------------------

    def conflicts(self) -> list[dict]:
        directory = self.library / CONFLICTS_DIR
        records = []
        try:
            entries = sorted(directory.glob("*.json"))
        except OSError:
            return records
        for path in entries:
            record = _read_json(path) if not path.is_symlink() else None
            if isinstance(record, dict) and _CONFLICT_ID.fullmatch(path.stem):
                records.append({**record, "id": path.stem})
        return records

    def _pending(self, settings: Settings) -> int:
        """Changes not on the remote yet, counted as the commit message names them (flows, personas…)."""
        if not (self.library / ".git").is_dir():
            return 0
        try:
            git = self._git()
            head = self._rev(git, f"refs/heads/{settings.branch}")
            current = self._tree(git, head)
            remote = self._tree(git, self._rev(git, f"refs/remotes/origin/{settings.branch}"))
            scopes = self._working_scopes()
            snap = scope.snapshot(self.library, scopes,
                                  held_groups=self._held_groups(settings, scopes, current, remote))
            paths = changed_paths(remote, self._desired(snap, current, self._state().get("held_remote", ())))
        except (GitError, OSError, scope.ScopeError):
            return 0
        named = {name for name in map(change_label, paths) if name}
        return len(named) or (1 if paths else 0)

    def status(self) -> dict:
        settings = self.settings()
        if settings is None:
            return {"state": "off", "reason": None, "message": "sync is not set up", "conflicts": 0}
        state = self._state()
        now = self._clock()
        try:
            display = self._remote(settings).display
        except SyncError:
            display = None
        result = {
            "remote": display, "branch": settings.branch, "label": settings.label,
            "identity": {"name": settings.name, "email": settings.email},
            "last_success": state.get("last_success"), "last_attempt": state.get("last_attempt"),
            "retry_at": state.get("retry_at"), "fetch_minutes": settings.fetch_minutes,
            "ask_new_repositories": settings.ask_new_repositories,
            "conflicts": len(self.conflicts()), "blocked": state.get("blocked", []),
            "held": state.get("held", []), "pending_groups": state.get("pending_groups", []),
            "announced_groups": state.get("announced_groups", []),
            "activity": state.get("activity", [])[-ACTIVITY_LIMIT:],
            "public_key": keys.public_key(self.state_dir), "size": state.get("size"),
            "size_warning": (state.get("size") or 0) > SIZE_WARNING,
            "reason": None, "message": None, "pending": 0, "stale": False,
        }
        if settings.paused:
            name = "paused"
        elif not settings.started:
            name = state.get("state") if state.get("state") in ("attention", "offline") else "waiting_for_access"
            if state.get("access") == "ok" and name == "waiting_for_access":
                name, result["reason"] = "attention", "confirmation_needed"
                result["message"] = "access works; review the preview and start sync"
        elif self._lock_busy():
            name = "syncing"
        elif state.get("state") in ("attention", "offline"):
            name = state["state"]
        else:
            result["pending"] = self._pending(settings)
            name = "pending" if result["pending"] else "synced"
        if name in ("attention", "offline") and result["reason"] is None:
            result["reason"], result["message"] = state.get("reason"), state.get("message")
        last = _parse_time(state.get("last_success"))
        if settings.started and last is not None and name not in ("paused",):
            age = now - last
            result["stale"] = age > STALE_AFTER
            if age > ATTENTION_AFTER and name in ("synced", "pending", "offline"):
                name, result["reason"] = "attention", "stale"
                result["message"] = f"no successful sync since {state.get('last_success')}"
        result["state"] = name
        return result

    # --- setup ------------------------------------------------------------------------

    def setup(self, *, remote: str, name: str, email: str, label: str | None = None,
              branch: str = "main", ask_new_repositories: bool | None = None,
              trust_host_key: str | None = None, confirm_private: bool | None = None) -> dict:
        """Save the settings, create this machine's key and trust the host's key.

        Returns ``waiting_for_access`` with the public key to add as a deploy key with write
        access, or ``host_key_unconfirmed`` with fingerprints to confirm for hosts other than
        github.com. Run ``check`` and ``preview``, then ``start`` with the preview's hash.
        """
        self._check_git()
        existing = self.settings()
        label = label or (existing.label if existing else default_label())
        validate_identity(name, email, label)
        if not _BRANCH.fullmatch(branch) or ".." in branch or branch.endswith((".", "/", ".lock")):
            raise SyncError("invalid", f"invalid branch name {branch!r}")
        try:
            remote_info = parse_remote(remote, allow_file=self.allow_file_remote)
        except RemoteError as error:
            raise SyncError("unknown_remote", str(error)) from None
        if existing and existing.started and (existing.remote != remote or existing.branch != branch):
            raise SyncError("connected", f"sync already uses {existing.remote}; disconnect first")
        settings = existing if existing else Settings(remote=remote, name=name, email=email, label=label)
        settings.remote, settings.name, settings.email, settings.label, settings.branch = \
            remote, name, email, label, branch
        if ask_new_repositories is not None:
            settings.ask_new_repositories = ask_new_repositories
        if confirm_private is not None:
            settings.private_confirmed = confirm_private
        _private_dir(self.state_dir)
        settings.save(self.settings_path)
        self._write_support_files(settings)
        result = {"status": "waiting_for_access", "remote": remote_info.display, "label": label}
        if remote_info.kind == "ssh":
            result["public_key"] = keys.ensure_key(self.state_dir, label)
            unconfirmed = self._trust_host(remote_info, trust_host_key)
            if unconfirmed:
                return {**result, **unconfirmed}
        self._repository(settings, create=True)
        self._log(f"setup for {remote_info.display} as {label}")
        result["message"] = ("add the public key to the repository as a deploy key with write access, "
                             "then run check" if remote_info.kind == "ssh" else "run check")
        return result

    def _trust_host(self, remote: Remote, confirmed: str | None) -> dict | None:
        """Write the host's keys to ``known_hosts``; a dict asks the owner to confirm a fingerprint."""
        if keys.trusted_keys(self.known_hosts, remote.host, remote.port):
            return None
        try:
            if remote.host == "github.com" and remote.port in (None, 22):
                keys.write_known_hosts(self.known_hosts, remote.host, remote.port, self._github_host_keys())
                return None
            offered = self._scan_host_keys(remote.host, remote.port)
        except keys.SSHKeyError as error:
            raise SyncError("host_key", str(error), state="offline") from None
        prints = {keys.fingerprint(key): key for key in offered}
        if confirmed in prints:
            keys.write_known_hosts(self.known_hosts, remote.host, remote.port, [prints[confirmed]])
            return None
        return {"status": "host_key_unconfirmed", "host": remote.host, "fingerprints": sorted(prints),
                "message": "compare a fingerprint with the one your host publishes and confirm it"}

    def check(self) -> dict:
        """Check that this machine can read the remote and that it is empty or an Agents-Core library."""
        settings = self._require_settings()
        state = self._state()
        try:
            with self._sync_lock_for(settings):
                git = self._repository(settings, create=True)
                remote_head, kind = self._inspect_remote(git, settings)
        except SyncError as error:
            if error.reason != "lock_held":
                self._record_failure(state, error, setup=not settings.started)
                self._save_state(state)
            raise
        except GitError as error:
            failure = self._git_failure(error)
            self._record_failure(state, failure, setup=not settings.started)
            self._save_state(state)
            raise failure from None
        state["access"] = "ok"
        if not settings.started:
            state.update(state="waiting_for_access", reason=None, message=None)
        self._save_state(state)
        return {"status": "ok", "remote_state": kind, "remote_head": remote_head}

    @contextmanager
    def _sync_lock_for(self, settings: Settings, *, create: bool = True):
        """The sync lock, after making sure ``.git`` exists to hold it (unless ``create`` is off).

        Two runners may start on a library without ``.git``; a lock in the state directory lets
        one of them create it. Everything else happens under the sync lock.
        """
        if not (self.library / ".git").is_dir():
            if not create:
                yield
                return
            _private_dir(self.state_dir)
            with file_lock(self.state_dir / "setup.lock"):
                try:
                    self._repository(settings, create=True)
                except GitError as error:
                    raise SyncError("git_error", f"could not create the sync repository: {error}") from None
        with self._sync_lock():
            yield

    def _inspect_remote(self, git: Git, settings: Settings) -> tuple[str | None, str]:
        """Fetch and classify the remote: ``empty`` or ``library``; foreign content is refused."""
        listing = git.run("ls-remote", "origin", network=True).stdout.decode().split("\n")
        refs = {line.split("\t")[1] for line in listing if "\t" in line}
        remote_head = self._fetch(git, settings)
        if remote_head is None:
            if refs - {"HEAD"}:
                raise SyncError("unknown_remote", "the remote holds branches but not "
                                                  f"{settings.branch}; choose an empty repository")
            return None, "empty"
        self._check_marker(git, remote_head)
        return remote_head, "library"

    def _check_marker(self, git: Git, commit: str) -> None:
        result = git.run("cat-file", "blob", f"{commit}:{MARKER}", check=False)
        if result.returncode != 0:
            raise SyncError("unknown_remote", "the remote holds content that is not an Agents-Core "
                                              "library; choose an empty repository or a library")
        try:
            marker = json.loads(result.stdout.decode("utf-8"))
            version = marker["format"]
            if not isinstance(version, int):
                raise TypeError
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            raise SyncError("unknown_remote", f"the remote's {MARKER} is not readable") from None
        if version > FORMAT:
            raise SyncError("format_newer", f"the library uses format {version}; update Agents-Core "
                                            "on this machine to keep syncing")

    def _fetch(self, git: Git, settings: Settings) -> str | None:
        git.run("fetch", "--no-tags", "--prune", "--quiet", "origin", network=True)
        return self._rev(git, f"refs/remotes/origin/{settings.branch}")

    # --- privacy ----------------------------------------------------------------------

    def anonymous_visibility(self, remote: Remote) -> str:
        """``public`` when the repository can be read anonymously over HTTPS, else ``private``.

        Runs with an empty configuration and no prompts, because a credential helper would make a
        private repository look public. ``unknown`` when the host cannot be reached that way.
        """
        if remote.kind == "file":
            return "private"
        empty = self.state_dir / "empty-gitconfig"
        if not empty.exists():
            _private_dir(self.state_dir)
            _write_private(empty, b"")
        environment = gitcmd.clean_environment()
        environment.update(GIT_CONFIG_GLOBAL=str(empty), GIT_CONFIG_NOSYSTEM="1",
                           GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https")
        try:
            result = subprocess.run(["git", "ls-remote", remote.https_url],
                                    capture_output=True, timeout=30, env=environment,
                                    stdin=subprocess.DEVNULL, **gitcmd.no_window())
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        if result.returncode == 0:
            return "public"
        return "private" if gitcmd.classify(result.stderr.decode("utf-8", "replace")) == "auth" else "unknown"

    def _check_visibility(self, settings: Settings, state: dict, *, required: bool) -> None:
        """Refuse a public remote. ``required`` (before the first upload) also refuses an unknown one."""
        last = _parse_time(state.get("visibility_checked"))
        if not required and last is not None and self._clock() - last < VISIBILITY_EVERY:
            return
        verdict = self._visibility(self._remote(settings))
        if verdict == "public":
            raise SyncError("public_repo", "the remote repository is public; make it private or choose another")
        if verdict != "private":
            if required and not settings.private_confirmed:
                raise SyncError("public_repo", "could not confirm that the repository is private; "
                                               "confirm it explicitly (setup --confirm-private)")
            return
        state["visibility_checked"] = _now_iso(self._clock())

    # --- planning and applying --------------------------------------------------------

    def _plan(self, git: Git, settings: Settings, state: dict, remote_head: str | None,
              *, joining: bool) -> _Plan:
        """Read the library and work out the integration. Call under the library lock."""
        scopes = self._working_scopes()
        local_head = self._rev(git, f"refs/heads/{settings.branch}")
        current = self._tree(git, local_head)
        remote_tree = self._tree(git, remote_head)
        held_groups = self._held_groups(settings, scopes, current, remote_tree)
        snap = scope.snapshot(self.library, scopes, held_groups=held_groups)
        desired = self._desired(snap, current, state.get("held_remote", ()))
        plan = _Plan(local_head, remote_head, snap, current, desired, remote_tree, held_groups=held_groups)
        if remote_head is None:
            plan.kind = "push" if plan.desired or local_head else "none"
            return plan
        base_commit = None
        if local_head is not None:
            if local_head == remote_head and plan.desired == current:
                return plan
            if self._is_ancestor(git, remote_head, local_head):
                plan.kind = "push"
                return plan
            base_commit = self._merge_base(git, local_head, remote_head)
        unrelated = base_commit is None and (bool(plan.desired) or local_head is not None)
        last_seen = state.get("remote_head")
        rewritten = bool(last_seen and settings.started and last_seen != remote_head
                         and not self._is_ancestor(git, last_seen, remote_head))
        if (unrelated or rewritten) and not joining:
            raise SyncError("confirmation_needed",
                            "the remote history was rewritten or is unrelated to this library; "
                            "review the preview and confirm it" if settings.started else
                            "review the preview and start sync")
        if local_head is not None and base_commit == local_head and plan.desired == current:
            plan.kind, plan.target = "fast_forward", remote_tree
            return plan
        if local_head is None and not plan.desired:
            plan.kind, plan.target = "fast_forward", remote_tree
            return plan
        base_tree = self._tree(git, base_commit)
        local_files = {blob: path for path, blob in plan.snapshot.files.items()}
        cache = {}

        def read(blob: str) -> bytes:
            if blob not in cache:
                if blob in local_files:
                    cache[blob] = (self.library / local_files[blob]).read_bytes()
                else:
                    cache[blob] = self._read_blobs(git, [blob])[blob]
            return cache[blob]

        merged = merging.merge(base_tree, plan.desired, remote_tree, read, label=settings.label,
                               now=self._clock(), local_commit=local_head, remote_commit=remote_head)
        rules = self._rules_for(merged.tree, read, held_groups)
        merged.tree = {path: value for path, value in merged.tree.items() if rules.reason(path) is None}
        plan.kind = "join" if unrelated or rewritten else "merge"
        plan.merged, plan.target = merged, merged.tree
        return plan

    def _preview_of(self, plan: _Plan) -> dict:
        """The owner-facing summary of a plan, and the hash that confirms exactly this plan."""
        target = plan.target if plan.target is not None else plan.desired
        generated = {path for path, value in target.items() if isinstance(value, bytes)}
        def entry(path, sizes):
            return {"path": path, **merging.flow_reference(path), "size": sizes.get(path)}
        remote_sizes = {path: plan.remote_sizes.get(blob) for path, blob in plan.remote_tree.items()}
        upload = [entry(p, plan.snapshot.sizes) for p in changed_paths(plan.remote_tree, target)
                  if p not in generated and p in target]
        removed = [p for p in changed_paths(plan.remote_tree, target) if p not in target]
        download = [entry(p, remote_sizes) for p in changed_paths(plan.desired, target)
                    if p in target and p not in generated]
        groups = {}
        for path in target:
            group = scope.repo_group(path)
            if group:
                origin = plan.snapshot.portable.get(group)
                groups.setdefault(group, {"group": group, "origin": origin, "files": 0,
                                          "new": group not in self._groups_in(plan.remote_tree)})
                groups[group]["files"] += 1
        conflicts = [{"path": c["path"], "kind": c["kind"], **merging.flow_reference(c["path"])}
                     for c in (plan.merged.conflicts if plan.merged else [])]
        blocked = [{"path": p, **v} for p, v in sorted(plan.snapshot.blocked.items())]
        identity = json.dumps({
            "remote": plan.remote_head, "local": plan.local_head,
            "desired": sorted(plan.desired.items()),
            "conflicts": sorted((c["path"], c["kind"]) for c in conflicts),
            "blocked": sorted((b["path"], b["sha256"]) for b in blocked),
            "pending": sorted(plan.held_groups),
        }, sort_keys=True)
        return {
            "kind": plan.kind, "remote_head": plan.remote_head, "local_head": plan.local_head,
            "upload": upload, "remove_from_remote": removed, "download": download,
            "conflicts": conflicts, "blocked": blocked,
            "held": [{"path": p, **v} for p, v in sorted(plan.snapshot.held.items())],
            "excluded": sorted(p for p, v in plan.snapshot.outside.items() if v["reason"] == "excluded"),
            "pending_groups": sorted(plan.held_groups), "repository_groups": sorted(groups.values(), key=lambda g: g["group"]),
            "upload_bytes": sum(e["size"] or 0 for e in upload),
            "hash": hashlib.sha256(identity.encode()).hexdigest(),
        }

    def _commit_local(self, git: Git, settings: Settings, plan: _Plan) -> str | None:
        """Commit the plan's desired tree when it differs from the last commit; returns the head."""
        if plan.desired == plan.current and plan.local_head is not None:
            return plan.local_head
        if not plan.desired and plan.local_head is None:
            return None
        changed = [path for path, blob in plan.desired.items()
                   if plan.current.get(path) != blob and path in plan.snapshot.files]
        self._store_files(git, changed, plan.snapshot.files)
        parents = [plan.local_head] if plan.local_head else []
        commit = self._commit(git, settings, plan.desired, parents, describe(plan.current, plan.desired))
        self._set_head(git, settings, commit, plan.local_head)
        return commit

    def _target_commit(self, git: Git, settings: Settings, plan: _Plan, local_head: str | None) -> str:
        """The commit that integrates the remote: the remote head itself or a merge commit."""
        if plan.kind == "fast_forward":
            return plan.remote_head
        tree = {}
        for path, value in plan.target.items():
            tree[path] = self._store_bytes(git, value) if isinstance(value, bytes) else value
        plan.target = tree
        conflicts = len(plan.merged.conflicts) if plan.merged else 0
        summary = "merge from the remote" + (f" ({conflicts} conflict{'s' if conflicts > 1 else ''})"
                                             if conflicts else "")
        parents = ([local_head] if local_head else []) + [plan.remote_head]
        return self._commit(git, settings, tree, parents, summary)

    def _apply(self, git: Git, state: dict, plan: _Plan, old: dict, new: dict) -> dict:
        """Write ``new`` over ``old`` in the working tree; never touches a file this run did not read.

        A held, blocked or outside file, or one that changed since the snapshot, stays as it is;
        its path goes to ``held_remote`` and the next cycle reconciles it as a conflict.
        """
        snap = plan.snapshot
        rules = self._rules_for(new, lambda blob: self._read_blobs(git, [blob])[blob], plan.held_groups)
        held_remote = set(state.get("held_remote", []))
        wanted = [new[p] for p in changed_paths(old, new) if p in new]
        sizes = self._blob_sizes(git, wanted)
        contents = self._read_blobs(git, [b for b in wanted if sizes.get(b, 0) <= MAX_FILE_BYTES])
        written, deleted = [], []
        for path in changed_paths(old, new):
            before, after = old.get(path), new.get(path)
            local = snap.local_blob(path)
            if path in snap.held or local != before or (after is not None and after not in contents):
                held_remote.add(path)
                continue
            target = self._safe_target(path)
            if target is None or self._current_blob(target) != local:
                held_remote.add(path)
                continue
            if after is None:
                if rules.reason(path) is None and local is not None:
                    target.unlink()
                    deleted.append(path)
                continue
            try:
                user_library.atomic_write(target, contents[after])
            except OSError as error:
                self._log(f"could not write {path}: {error}")
                held_remote.add(path)
                continue
            written.append(path)
        state["held_remote"] = sorted(held_remote)
        return {"written": written, "deleted": deleted}

    def _safe_target(self, path: str) -> Path | None:
        """``library/path`` with every parent a real directory inside the library (created as needed)."""
        if not scope.portable_name(path):
            return None
        current = self.library
        parts = path.split("/")
        for part in parts[:-1]:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                try:
                    os.mkdir(current, 0o700)
                except OSError:
                    return None
                continue
            except OSError:
                return None
            if not stat.S_ISDIR(info.st_mode):
                return None
        target = current / parts[-1]
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return target
        except OSError:
            return None
        return target if stat.S_ISREG(info.st_mode) else None

    @staticmethod
    def _current_blob(target: Path) -> str | None:
        try:
            return scope.blob_id(target.read_bytes())
        except FileNotFoundError:
            return None

    def _reconcile_held(self, git: Git, settings: Settings, state: dict, plan: _Plan) -> bool:
        """Settle paths whose remote version was not written because the local file was held.

        Once such a file can be committed, the version in the last commit (the remote's) goes to the
        working tree and the local text is kept like any losing version, with a conflict record.
        Returns True when files were written, so the caller reads the library again.
        """
        remaining, wrote = [], False
        snap, now = plan.snapshot, self._clock()
        for path in state.get("held_remote", []):
            committed = plan.current.get(path)
            if path in snap.held or path in snap.blocked or path in snap.outside:
                remaining.append(path)
                continue
            local = snap.files.get(path)
            if committed is None or local == committed:
                continue  # nothing to reconcile: the local file or its absence simply wins or matches
            target = self._safe_target(path)
            if target is None or self._blob_sizes(git, [committed]).get(committed, 0) > MAX_FILE_BYTES:
                remaining.append(path)  # never written here; the committed version stays in the tree
                continue
            files = {path: self._read_blobs(git, [committed])[committed]}
            if local is None:
                record = merging.conflict_record(path, "deletion_undone", "remote", label=settings.label,
                                                 now=now, local_blob=None, remote_blob=committed,
                                                 deleted_on="local")
            else:
                details, extra = merging.keep_local(path, target.read_bytes(), now)
                files.update(extra)
                record = merging.conflict_record(path, "both_changed", "remote", label=settings.label,
                                                 now=now, local_blob=local, remote_blob=committed, **details)
            files.update([merging.record_file(record)])
            for name, data in files.items():
                destination = self._safe_target(name)
                if destination is not None:
                    user_library.atomic_write(destination, data)
            wrote = True
        state["held_remote"] = remaining
        return wrote

    # --- the cycle --------------------------------------------------------------------

    def _require_settings(self) -> Settings:
        settings = self.settings()
        if settings is None:
            raise SyncError("not_set_up", "sync is not set up; run setup first", state="off")
        return settings

    def preview(self) -> dict:
        """What starting (or confirming) sync would upload and download; changes nothing."""
        settings = self._require_settings()
        state = self._state()
        with self._sync_lock_for(settings):
            try:
                git = self._repository(settings, create=True)
                self._clear_stale_git_locks()
                remote_head, _ = self._inspect_remote(git, settings)
                with self._library_lock():
                    user_library.ensure_root_files(self.library)
                    plan = self._plan(git, settings, state, remote_head, joining=True)
                plan.remote_sizes = self._blob_sizes(git, plan.remote_tree.values())
            except GitError as error:
                raise self._git_failure(error) from None
        return self._preview_of(plan)

    def start(self, confirm: str) -> dict:
        """Start sync after the owner confirmed the preview with this hash: the first upload or join."""
        settings = self._require_settings()
        if settings.started:
            return self.run(confirm=confirm, force=True)
        return self._run(settings, confirm=confirm, force=True)

    def run(self, *, force: bool = False, confirm: str | None = None) -> dict:
        """One sync cycle. ``force`` ignores the retry delay; ``confirm`` approves a previewed join."""
        settings = self.settings()
        if settings is None:
            return {"status": "off", "message": "sync is not set up"}
        if settings.paused:
            return {"status": "paused"}
        if not settings.started and confirm is None:
            return {"status": "attention", "reason": "confirmation_needed",
                    "message": "finish setup: run preview, then start with its hash"}
        return self._run(settings, confirm=confirm, force=force)

    def _run(self, settings: Settings, *, confirm: str | None, force: bool) -> dict:
        state = self._state()
        retry_at = _parse_time(state.get("retry_at"))
        if not force and retry_at is not None and self._clock() < retry_at:
            return {"status": "offline", "retry_at": state["retry_at"], "message": state.get("message")}
        try:
            with self._sync_lock_for(settings):
                state = self._state()  # another runner may have written it while we waited
                return self._run_locked(settings, state, confirm)
        except SyncError as error:
            if error.reason == "lock_held":
                return {"status": "lock_held", "message": error.message}
            raise

    def _run_locked(self, settings: Settings, state: dict, confirm: str | None) -> dict:
        state["last_attempt"] = _now_iso(self._clock())
        result = {"status": "synced", "sent": [], "received": [], "conflicts": [], "pushed": False}
        try:
            self._check_git()
            git = self._repository(settings, create=True)
            self._clear_stale_git_locks()
            validate_identity(settings.name, settings.email, settings.label)
            joining = confirm is not None
            self._check_visibility(settings, state, required=not settings.started)
            for attempt in range(PUSH_ATTEMPTS):
                outcome = self._cycle(git, settings, state, confirm=confirm)
                result["sent"] += outcome["sent"]
                result["received"] += outcome["received"]
                result["conflicts"] += outcome["conflicts"]
                if outcome["push"] is None:
                    break
                if self._push(git, settings, outcome["push"]):
                    result["pushed"] = True
                    break
                if joining:
                    raise SyncError("confirmation_needed", "the remote changed while starting; "
                                                           "review the preview again")
            else:
                raise SyncError("offline", "the remote kept changing; sync will retry", state="offline")
        except SyncError as error:
            return self._finish_failure(settings, state, error, result)
        except GitError as error:
            return self._finish_failure(settings, state, self._git_failure(error), result)
        except scope.ScopeError as error:
            return self._finish_failure(settings, state, SyncError("scopes_invalid", str(error)), result)
        except _ChangedWhileReading as error:
            return self._finish_failure(settings, state, SyncError(
                "busy", f"{error} changed while sync read it; sync will retry", state="pending"), result)
        return self._finish_success(git, settings, state, result)

    def _cycle(self, git: Git, settings: Settings, state: dict, *, confirm: str | None) -> dict:
        """Fetch, then commit and integrate under the library lock; returns what to push."""
        remote_head = self._fetch(git, settings)
        if remote_head is not None:
            self._check_marker(git, remote_head)
        elif state.get("remote_head") and settings.started and confirm is None:
            raise SyncError("confirmation_needed", "the remote branch is gone; review the preview and "
                                                   "confirm to upload this library again")
        with self._library_lock():
            user_library.ensure_root_files(self.library)
            plan = self._plan(git, settings, state, remote_head, joining=confirm is not None)
            if confirm is None and self._reconcile_held(git, settings, state, plan):
                plan = self._plan(git, settings, state, remote_head, joining=False)
            if confirm is not None and self._preview_of(plan)["hash"] != confirm:
                raise SyncError("confirmation_needed", "the preview changed; review it again and confirm")
            local_head = self._commit_local(git, settings, plan)
            sent = changed_paths(plan.remote_tree if remote_head else plan.current, plan.desired)
            outcome = {"sent": sent, "received": [], "conflicts": [], "push": None,
                       "new_groups": sorted(self._groups_in(plan.desired) - self._groups_in(plan.remote_tree))}
            head = local_head
            if plan.target is not None:
                head = self._target_commit(git, settings, plan, local_head)
                old = self._tree(git, local_head)
                generated = {p for p, v in (plan.merged.tree.items() if plan.merged else ()) if isinstance(v, bytes)}
                applied = self._apply(git, state, plan, old, plan.target)
                self._set_head(git, settings, head, local_head)
                outcome["received"] = sorted(set(applied["written"] + applied["deleted"]) - generated)
                outcome["conflicts"] = plan.merged.conflicts if plan.merged else []
                outcome["sent"] = changed_paths(plan.remote_tree, plan.target)
            state["pending_groups"] = sorted(plan.held_groups)
            state["blocked"] = [{"path": p, "pattern": v["pattern"], "sha256": v["sha256"]}
                                for p, v in sorted(plan.snapshot.blocked.items())]
            state["held"] = [{"path": p, **v} for p, v in sorted(plan.snapshot.held.items())]
        state["_new_groups"] = outcome["new_groups"]
        if head is not None and head != remote_head:
            outcome["push"] = head
        return outcome

    def _push(self, git: Git, settings: Settings, head: str) -> bool:
        """True when pushed; False when the remote moved on (a non-fast-forward rejection)."""
        result = git.run("push", "--porcelain", "origin", f"{head}:refs/heads/{settings.branch}",
                         network=True, check=False)
        if result.returncode == 0:
            git.run("update-ref", f"refs/remotes/origin/{settings.branch}", head)
            return True
        text = (result.stdout + result.stderr).decode("utf-8", "replace")
        if "[rejected]" in text or "non-fast-forward" in text or "fetch first" in text:
            return False
        raise GitError("push", gitcmd.classify(text), text, result.returncode)

    # --- recording outcomes -----------------------------------------------------------

    def _git_failure(self, error: GitError) -> SyncError:
        if error.kind in ("network", "timeout"):
            return SyncError("network", str(error), state="offline")
        if error.kind == "auth":
            return SyncError("auth", "the remote refused this machine's key; check that it is a deploy "
                                     "key with write access", state="attention")
        if error.kind == "host_key":
            return SyncError("host_key", "the host key does not match the trusted one", state="attention")
        if error.kind == "not_found":
            return SyncError("unknown_remote", "the remote repository was not found")
        return SyncError("git_error", str(error))

    def _record_failure(self, state: dict, error: SyncError, *, setup: bool = False) -> None:
        if error.state == "offline":
            failures = int(state.get("failures", 0)) + 1
            delay = RETRY_MINUTES[min(failures, len(RETRY_MINUTES)) - 1]
            state.update(state="offline", reason=error.reason, message=error.message, failures=failures,
                         retry_at=_now_iso(self._clock() + timedelta(minutes=delay)))
        elif error.state in ("pending", "busy"):
            state.update(state="pending", reason=None, message=error.message)
        else:
            state.update(state="attention", reason=error.reason, message=error.message,
                         failures=0, retry_at=None)
        if setup and error.reason == "auth":
            state.update(state="waiting_for_access", access="denied")

    def _finish_failure(self, settings: Settings, state: dict, error: SyncError, result: dict) -> dict:
        self._record_failure(state, error, setup=not settings.started)
        self._log(f"sync stopped: {error.reason}: {error.message}")
        state.pop("_new_groups", None)
        self._remember(state, result, outcome=error.reason, settings=settings)
        self._save_state(state)
        return {**result, "status": state["state"], "reason": error.reason, "message": error.message,
                "retry_at": state.get("retry_at")}

    def _finish_success(self, git: Git, settings: Settings, state: dict, result: dict) -> dict:
        now = _now_iso(self._clock())
        head = self._rev(git, f"refs/heads/{settings.branch}")
        state.update(state="synced", reason=None, message=None, failures=0, retry_at=None,
                     last_success=now, head=head, remote_head=self._rev(git, f"refs/remotes/origin/{settings.branch}"),
                     access="ok")
        if not settings.started:
            settings.started = now
            settings.save(self.settings_path)
        new_groups = state.pop("_new_groups", [])
        if result["pushed"] and new_groups:
            announced = state.setdefault("announced_groups", [])
            for group in new_groups:
                announced.append({"group": group, "origin": scope.read_portable(self.library).get(group),
                                  "time": now})
            del announced[:-ANNOUNCED_LIMIT]
        if state.get("blocked"):
            state.update(state="attention", reason="secret",
                         message="files with possible credentials were not uploaded")
        elif state.get("pending_groups"):
            state.update(state="attention", reason="new_repository",
                         message="new repository flows wait for your approval before upload")
        state["size"] = self._repository_size(git)
        self._remember(state, result, outcome="ok", settings=settings, new_groups=new_groups if result["pushed"] else [])
        self._save_state(state)
        if result["sent"] or result["received"]:
            self._log(f"synced: sent {len(result['sent'])}, received {len(result['received'])}, "
                      f"conflicts {len(result['conflicts'])}")
        return {**result, "status": state["state"], "reason": state.get("reason"),
                "message": state.get("message"), "head": head}

    def _remember(self, state: dict, result: dict, *, outcome: str, settings: Settings,
                  new_groups: list | None = None) -> None:
        """Add the cycle to the activity feed when something happened or it failed."""
        if outcome == "ok" and not (result["sent"] or result["received"] or result["conflicts"]):
            return
        activity = state.setdefault("activity", [])
        if outcome != "ok" and activity and activity[-1].get("result") == outcome:
            return  # a failure that repeats every few minutes is one entry
        def flows(paths):
            return sorted({name for name in map(change_label, paths) if name})
        scripts = sorted(p for p in result["received"]
                         if not p.endswith((".md", ".json")) and change_label(p))
        entry = {"time": _now_iso(self._clock()), "result": outcome, "machine": settings.label,
                 "sent": flows(result["sent"]), "received": flows(result["received"]),
                 "scripts": scripts, "conflicts": len(result["conflicts"])}
        if new_groups:
            entry["new_groups"] = new_groups
        activity.append(entry)
        del activity[:-ACTIVITY_LIMIT]

    def _repository_size(self, git: Git) -> int | None:
        try:
            values = dict(line.split(": ", 1) for line in git.text("count-objects", "-v").splitlines()
                          if ": " in line)
            return (int(values.get("size", 0)) + int(values.get("size-pack", 0))) * 1024
        except (GitError, ValueError):
            return None

    # --- conflicts, scope and lifecycle -----------------------------------------------

    def resolve(self, conflict_id: str, action: str) -> dict:
        """``keep`` the current version, use ``mine`` (the kept local version), or ``dismiss``."""
        if action not in ("keep", "mine", "dismiss"):
            raise SyncError("invalid", "action must be keep, mine or dismiss")
        if not _CONFLICT_ID.fullmatch(conflict_id or ""):
            raise SyncError("invalid", "unknown conflict")
        record_path = self.library / CONFLICTS_DIR / f"{conflict_id}.json"
        record = _read_json(record_path)
        if not isinstance(record, dict) or record_path.is_symlink():
            raise SyncError("invalid", "unknown conflict")
        if action == "mine":
            self._restore_mine(record)
        with self._library_lock():
            record_path.unlink(missing_ok=True)
        user_library.notify(self.library, [f"{CONFLICTS_DIR}/{conflict_id}.json"])
        return {"status": "resolved", "id": conflict_id, "action": action}

    def _restore_mine(self, record: dict) -> None:
        path = record.get("path", "")
        if scope.groups(path) is None:
            raise SyncError("invalid", "the conflict names a path outside the library")
        if record.get("kind") == "deletion_undone":
            if record.get("deleted_on") == "local":  # "mine" was the deletion
                with self._library_lock():
                    target = self._safe_target(path)
                    if target is not None and target.exists():
                        target.unlink()
                user_library.notify(self.library, [path])
            return
        if "local_version" in record:
            version = self._safe_target(record["local_version"])
            if version is None or not version.exists():
                raise SyncError("invalid", "the kept local version is gone")
            data = version.read_bytes()
        elif "local_content" in record:
            data = record["local_content"].encode("utf-8")
        elif "local_content_base64" in record:
            import base64
            data = base64.b64decode(record["local_content_base64"])
        else:
            raise SyncError("invalid", "the conflict keeps no local version")
        reference = merging.flow_reference(path)
        if path.endswith(".md") and reference:
            self._save_flow(path, reference, data)
            return
        with self._library_lock():
            target = self._safe_target(path)
            if target is None:
                raise SyncError("invalid", f"{path} cannot be written")
            user_library.atomic_write(target, data)
        user_library.notify(self.library, [path])

    def _save_flow(self, path: str, reference: dict, data: bytes) -> None:
        """A normal save, so the current text goes to the flow's history first."""
        from src.flows import FlowError
        from src.user_flows import FlowLibrary
        library = FlowLibrary(user_dir=self.library, repo_key=reference.get("repo_key"))
        flow_id = reference["flow"].split(":", 1)[1]
        scope_name = "user" if reference["flow"].startswith("user:") else "repo"
        current = self.library / path
        revision = hashlib.sha256(current.read_bytes()).hexdigest() if current.is_file() else None
        override = (self.library / path).with_name(f"{flow_id}.meta.json").is_file()
        try:
            library.save(f"{scope_name}:{flow_id}", data.decode("utf-8"), expected_revision=revision,
                         override=override)
        except (FlowError, UnicodeDecodeError) as error:
            raise SyncError("invalid", f"could not restore the local version: {error}") from None

    def scopes(self) -> dict:
        """Scope groups present in the library with whether they sync, and the stored exclusions."""
        current = self._working_scopes()
        portable = scope.read_portable(self.library)
        groups = [{"group": name, "syncs": name not in current.exclude} for name in scope.KIND_GROUPS]
        repos_dir = self.library / "repos"
        keys_present = sorted(p.name for p in repos_dir.iterdir() if p.is_dir() and not p.is_symlink()) \
            if repos_dir.is_dir() else []
        for key in keys_present:
            group = f"repos/{key}"
            reason = "excluded" if group in current.exclude else None if group in portable else "local"
            groups.append({"group": group, "origin": portable.get(group), "syncs": reason is None,
                           "reason": reason})
        return {"groups": groups, "excluded_files": sorted(current.exclude_files),
                "allowed": sorted(current.allow)}

    def change_scopes(self, *, exclude=(), include=(), exclude_files=(), include_files=(),
                      allow_paths=(), approve=(), confirm: str | None = None) -> dict:
        """Change the shared exclusions. Uploading more (include, allow) needs the change's hash."""
        for group in [*exclude, *include, *approve]:
            if not scope.is_group(group):
                raise SyncError("invalid", f"unknown scope group {group!r}")
        settings = self.settings()
        with self._library_lock():
            current = self._working_scopes()
            allow = set(current.allow)
            for path in allow_paths:
                target = self._safe_target(path)
                if target is None or not target.is_file():
                    raise SyncError("invalid", f"{path} is not a library file")
                allow.add(scope.content_hash(target.read_bytes()))
            updated = Scopes(frozenset((current.exclude | set(exclude)) - set(include)),
                             frozenset((current.exclude_files | set(exclude_files)) - set(include_files)),
                             frozenset(allow))
            more = (current.exclude - updated.exclude) | (current.exclude_files - updated.exclude_files) \
                | (updated.allow - current.allow) | set(approve)
            if more:
                change = hashlib.sha256(json.dumps({"more": sorted(more), "scopes": updated.dump().decode()},
                                                   sort_keys=True).encode()).hexdigest()
                if confirm != change:
                    uploads = self._scope_uploads(current, updated)
                    return {"status": "confirmation_needed", "hash": change, "upload": uploads}
            if updated != current:
                target = self._safe_target(SCOPES_PATH)
                user_library.atomic_write(target, updated.dump())
        if approve and settings:
            settings.approved_groups = sorted(set(settings.approved_groups) | set(approve))
            settings.save(self.settings_path)
        if updated != current:
            user_library.notify(self.library, [SCOPES_PATH])
        return {"status": "saved", **self.scopes()}

    def _scope_uploads(self, before: Scopes, after: Scopes) -> list[str]:
        old = scope.snapshot(self.library, before)
        new = scope.snapshot(self.library, after)
        return sorted(set(new.files) - set(old.files))

    def pause(self) -> dict:
        settings = self._require_settings()
        settings.paused = True
        settings.save(self.settings_path)
        return self.status()

    def resume(self) -> dict:
        settings = self._require_settings()
        settings.paused = False
        settings.save(self.settings_path)
        return self.status()

    def disconnect(self) -> dict:
        """Stop syncing here. Library files and ``.git`` stay; this machine's key is deleted."""
        settings = self._require_settings()
        with self._sync_lock_for(settings, create=False):
            for name in (SETTINGS_FILE, STATE_FILE, keys.KEY_NAME, f"{keys.KEY_NAME}.pub"):
                (self.state_dir / name).unlink(missing_ok=True)
        self._log("disconnected")
        return {"status": "off", "message": "sync is off; the library files and its .git stay. "
                                            "Remove this machine's deploy key from the repository."}
