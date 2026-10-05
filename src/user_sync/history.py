"""Each repository's ``history.md`` merged across the user's machines through the synced library (#172).

Every checkout keeps its own append-only ``history.md`` (``src.memory.history``); nothing from other
machines is ever written into it. A repository's entries are shared only after the owner has seen
them: ``python -m src.user_sync history export`` lists every entry not shared yet, and
``--confirm`` records the repository's key as approved on this machine (``history_repositories``
in the settings) and exports them. From then on its new entries follow by themselves, until
``history revoke``. Approval is per machine; another machine's approval never shares this one's
entries. Shared entries live in this machine's segments of the library::

    repos/<key>/history/<label>/<YYYY-MM>.md     this machine's entries of that month (UTC)
    repos/<key>/history/<label>/<YYYY-MM>-2.md   the continuation once a part would pass 4 MiB

``<key>`` is ``src.user_flows.repo_key`` of the repository and ``<label>`` the machine label of the
sync settings. Only the machine with that label writes its files, so segments never conflict. Only
a checkout's top level (``git rev-parse --show-toplevel``) with an ``origin`` takes part; a workspace
in a subfolder keeps its history local. Exclusions apply on top: the ``history`` group,
``repos/<key>`` and single segment files. A segment holds the entry blocks of ``history.md`` with
one more field line, ``**Machine:** <label>``; the heading, and with it the content hash, is the
same on every machine. An entry the secret scanner would flag stays on this machine, because one
hit would keep the whole month's segment out of every commit.

Exporting is a reconcile, never fire-and-forget: for each approved repository and each of its
checkouts this machine has seen (a registry in the sync state directory, never in the library),
the journal entries missing from this machine's segment are appended, from a per-checkout
watermark (the newest entry handled, with a small overlap) and deduplicated by entry id. It runs in
one worker thread per process after appends, when a server starts, on resume and right after
approval. It takes the library's ``.lock`` without waiting: a held lock is retried with backoff
for about 30 seconds and then left to the next run, so ``log_interaction`` never waits for sync.
The last failure is kept in the state and shown by ``status`` as ``history_error``.

``read_history`` merges this checkout's journal with the segments of the same key
(``src.memory.history``): other machines', and this machine's own entries from its other checkouts.
Parts whose header or group names another ``origin`` are skipped and reported as ``other_origin``.

Writes append to the current part (``O_APPEND`` and ``fsync``) under the library's ``.lock``, never
while a history lock is held. Reading a segment needs no lock: a part only ever grows by whole
blocks while the lock is held, and a block a crash cut short is removed before the next append.
Nothing is pruned: segments grow by one file per machine and month.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time

from src import user_library
from src.file_lock import file_lock
from src.memory import history as journal
from src.user_library import REPO_META
from src.user_sync import engine, scope
from src.user_sync.engine import HISTORY_STATE_FILE, SETTINGS_FILE, Settings, SyncError

logger = logging.getLogger(__name__)

SEGMENTS = "history"              # repos/<key>/history/<label>/
PART_BYTES = 4 * 1024 * 1024      # a part that would pass this continues in the next one
FORMAT = 1
LOCK_WAIT_SECONDS = 30.0          # how long a run retries a held library lock before leaving the work
OVERLAP = timedelta(minutes=10)   # entries this much older than the watermark are checked again
WORKSPACE_SECONDS = 30.0          # how long a checkout's origin and top level are remembered
INTENT_CHARACTERS = 80
_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")   # what the engine accepts as a machine label
_PART = re.compile(r"(?P<month>[0-9]{4}-(?:0[1-9]|1[0-2]))(?:-(?P<number>[2-9]|[1-9][0-9]+))?\.md")
_MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_TOKEN = re.compile(r"(?P<digest>[0-9a-f]{64})-(?P<cutoff>[0-9]{8}T[0-9]{6})")
_MACHINE_LINE = "**Machine:**"
_STATE_LOCK = "history-state.lock"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- this machine: settings and the history state ----------------------------------------------


@dataclass(frozen=True)
class _Machine:
    state_dir: Path
    settings: Settings
    library: Path

    @property
    def label(self) -> str:
        return self.settings.label

    @property
    def approved(self) -> frozenset[str]:
        return _approved(self.settings)


def _approved(settings: Settings) -> frozenset[str]:
    """The keys whose history this machine shares; anything malformed counts as none."""
    value = settings.history_repositories
    return frozenset(key for key in value if isinstance(key, str)) if isinstance(value, list) else frozenset()


def _machine(state_dir=None, library=None) -> _Machine | None:
    """This machine's sync settings and library; None while sync is not set up here."""
    state_dir = Path(state_dir or engine.default_state_dir()).expanduser().resolve()
    settings = Settings.load(state_dir / SETTINGS_FILE)
    if settings is None:
        return None
    configured = library or settings.library or engine.default_library()
    return _Machine(state_dir, settings, Path(configured).expanduser().resolve())


def _read_state(state_dir: Path) -> dict:
    try:
        value = json.loads((state_dir / HISTORY_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _change_state(state_dir: Path, change) -> dict:
    """Read, change and write ``user-sync-history.json`` under its lock; returns the new state."""
    with file_lock(state_dir / _STATE_LOCK):
        state = _read_state(state_dir)
        before = json.dumps(state, sort_keys=True)
        change(state)
        if json.dumps(state, sort_keys=True) != before:
            user_library.atomic_write(state_dir / HISTORY_STATE_FILE,
                                      json.dumps(state, indent=2, ensure_ascii=False).encode() + b"\n")
        return state


def _record_error(machine: _Machine, reason: str, message: str, key: str | None) -> None:
    def change(state):
        state["error"] = {"reason": reason, "message": message, "key": key, "time": _now()}
    try:
        _change_state(machine.state_dir, change)
    except OSError:
        logger.warning("could not record a history sync failure: %s: %s", reason, message)


# --- a checkout ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Workspace:
    root: str
    top: bool        # the checkout's top level, not a folder inside it
    key: str
    origin: str | None


_WORKSPACES: dict[str, tuple[float, _Workspace]] = {}
_WORKSPACES_GUARD = threading.Lock()


def _top_level(root: str) -> bool:
    try:
        result = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0 or not result.stdout.strip():
        return False
    try:
        return Path(result.stdout.strip()).resolve() == Path(root).resolve()
    except OSError:
        return False


def _workspace(root) -> _Workspace:
    """A checkout's key, origin and whether it is the top level; remembered for a few seconds."""
    root = str(Path(root).expanduser().resolve())
    now = time.monotonic()
    with _WORKSPACES_GUARD:
        cached = _WORKSPACES.get(root)
        if cached is not None and now - cached[0] < WORKSPACE_SECONDS:
            return cached[1]
    from src.user_flows import repo_key
    key, origin = repo_key(Path(root))
    workspace = _Workspace(root, _top_level(root), key, origin)
    with _WORKSPACES_GUARD:
        _WORKSPACES[root] = (now, workspace)
    return workspace


def _shared(workspace: _Workspace) -> bool:
    return workspace.top and bool(workspace.origin) and scope.is_group(f"repos/{workspace.key}")


def _register(machine: _Machine, workspace: _Workspace) -> dict:
    """Remember a checkout in the registry; returns its record (key, origin, watermark)."""
    def change(state):
        record = state.setdefault("roots", {}).setdefault(workspace.root, {})
        if record.get("key") != workspace.key or record.get("origin") != workspace.origin:
            record.update(key=workspace.key, origin=workspace.origin, watermark=None)
    return _change_state(machine.state_dir, change)["roots"][workspace.root]


def _roots(machine: _Machine) -> dict[str, dict]:
    roots = _read_state(machine.state_dir).get("roots", {})
    return roots if isinstance(roots, dict) else {}


# --- the library --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Repository:
    machine: _Machine
    key: str
    origin: str

    @property
    def group(self) -> str:
        return f"repos/{self.key}"

    @property
    def label(self) -> str:
        return self.machine.label

    @property
    def library(self) -> Path:
        return self.machine.library

    def relative(self, label: str) -> str:
        return f"{self.group}/{SEGMENTS}/{label}"


def _scopes(library: Path) -> scope.Scopes:
    """The working tree's exclusions; a malformed file raises, because "exclude nothing" could leak."""
    path = library / scope.SCOPES_PATH
    if path.is_symlink():
        raise scope.ScopeError(f"{scope.SCOPES_PATH} must not be a symlink")
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = None
    return scope.parse_scopes(data)


def _excluded(repo: _Repository, scopes: scope.Scopes) -> bool:
    return repo.group in scopes.exclude or "history" in scopes.exclude


def _directory(base: Path, relative: str, *, create: bool) -> Path | None:
    """``base/relative`` with every part a real directory (created when ``create``); None if unsafe."""
    current = base
    for part in relative.split("/"):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(current, 0o700)
            except OSError:
                return None
            continue
        except OSError:
            return None
        if not stat.S_ISDIR(info.st_mode) or scope.is_link(info, current):
            return None
    return current


def _regular(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not scope.is_link(info, path)


def _parts(directory: Path, month: str | None = None) -> list[tuple[str, int, Path]]:
    """``(month, number, path)`` of the segment parts in ``directory``, oldest first."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    parts = []
    for name in names:
        match = _PART.fullmatch(name)
        if match and (month is None or match["month"] == month) and _regular(directory / name):
            parts.append((match["month"], int(match["number"] or 1), directory / name))
    return sorted(parts)


def _part_name(month: str, number: int) -> str:
    return f"{month}.md" if number == 1 else f"{month}-{number}.md"


def _stored_origin(repo: _Repository) -> tuple[bool, str | None]:
    """``(usable, origin)`` of the group's ``.repo.json``; not usable when it is a link or not a file."""
    meta = repo.library / repo.group / REPO_META
    if meta.is_symlink() or (meta.exists() and not _regular(meta)):
        return False, None
    try:
        value = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, None
    origin = value.get("origin") if isinstance(value, dict) else None
    return True, origin if isinstance(origin, str) and origin else None


class _Recent(OrderedDict):
    """A small least-recently-used map: a long-running server keeps only the parts it touches often."""

    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
            return self[key]
        return default

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.limit:
            self.popitem(last=False)


_HEADERS: _Recent = _Recent(1024)   # path -> ((size, mtime_ns), origin named in the header)
_KNOWN: _Recent = _Recent(128)      # path -> ((size, mtime_ns), entry ids)
_CACHE_GUARD = threading.Lock()


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def _header_origin(path: Path) -> str | None:
    """The ``repo:`` line of a part's header, read once per size and mtime."""
    key = _stat_key(path)
    if key is None:
        return None
    with _CACHE_GUARD:
        cached = _HEADERS.get(str(path))
        if cached is not None and cached[0] == key:
            return cached[1]
    try:
        with open(path, "rb") as stream:
            head = stream.read(2048).decode("utf-8", "replace")
    except OSError:
        return None
    origin = None
    if head.startswith("---\n"):
        for line in head.split("\n")[1:]:
            if line == "---":
                break
            if line.startswith("repo: "):
                origin = line[len("repo: "):].strip()
                break
    with _CACHE_GUARD:
        _HEADERS[str(path)] = (key, origin)
    return origin


def _known_ids(path: Path) -> frozenset[str]:
    """Entry ids of a part, parsed once per size and mtime."""
    key = _stat_key(path)
    if key is None:
        return frozenset()
    with _CACHE_GUARD:
        cached = _KNOWN.get(str(path))
        if cached is not None and cached[0] == key:
            return cached[1]
    parsed = journal.parsed_file(str(path))
    ids = frozenset(entry.id for entry in parsed[1]) if parsed else frozenset()
    with _CACHE_GUARD:
        _KNOWN[str(path)] = (key, ids)
    return ids


def _segments(repo: _Repository, scopes: scope.Scopes | None = None, *, own: bool = True,
              others: bool = True) -> tuple[list[tuple[str, Path]], list[str]]:
    """``(label, path)`` of the key's segment parts that belong to this repository, and the
    origins found in parts that name another one (skipped).

    Parts excluded one by one, and parts whose header names another origin, are left out.
    """
    found, foreign = [], []
    base = _directory(repo.library, f"{repo.group}/{SEGMENTS}", create=False)
    try:
        labels = sorted(os.listdir(base)) if base is not None else []
    except OSError:
        labels = []
    for label in labels:
        mine = label == repo.label
        if not _LABEL.fullmatch(label) or (mine and not own) or (not mine and not others):
            continue
        directory = _directory(base, label, create=False)
        for _, _, path in _parts(directory) if directory is not None else ():
            relative = f"{repo.relative(label)}/{path.name}"
            if scopes is not None and relative in scopes.exclude_files:
                continue
            try:
                if path.stat().st_size > scope.MAX_FILE_BYTES:
                    continue  # never synced, so not a segment of this library
            except OSError:
                continue
            origin = _header_origin(path)
            if origin != repo.origin:
                if origin:
                    foreign.append(origin)
                continue
            found.append((label, path))
    return found, sorted(set(foreign))


def _note_other_origin(machine: _Machine, repo: _Repository, found: list[str]) -> None:
    def change(state):
        records = state.setdefault("other_origin", {})
        if found:
            records[repo.key] = {"origin": repo.origin, "found": found}
        else:
            records.pop(repo.key, None)
    _change_state(machine.state_dir, change)


# --- the entry as a segment stores it ------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    id: str
    month: str
    timestamp: str
    intent: str
    text: str


def _header(label: str, month: str, origin: str) -> str:
    return ("---\n"
            f"repo: {origin}\n"
            f"machine: {label}\n"
            f"month: {month}\n"
            f"format_version: {FORMAT}\n"
            "---\n")


def _item(entry, body: str, label: str, origin: str) -> tuple[_Item | None, str | None]:
    """``(item, None)``, or ``(None, reason)`` for an entry that stays on this machine."""
    month = entry.timestamp[:7]
    if not _MONTH.fullmatch(month):
        return None, "invalid"
    lines = [line for line in body.split("\n") if not line.startswith(_MACHINE_LINE)]
    text = "\n" + "\n".join(lines + [f"{_MACHINE_LINE} {label}"]) + "\n\n"
    alone = (_header(label, month, origin) + text).encode("utf-8")  # the part it would start
    if scope.scan(alone):
        return None, "secret"
    if len(alone) > PART_BYTES:
        return None, "too_large"  # it could never sync in one part
    return _Item(entry.id, month, entry.timestamp, entry.intent, text), None


def _journal(root: str, since: str | None = None) -> dict[str, tuple]:
    """``id -> (entry, block)`` of a checkout's journal from ``since`` on; a later copy of an id wins."""
    found: dict[str, tuple] = {}
    blocks = journal.journal_blocks(os.path.join(root, "history.md"), os.path.join(root, "history"),
                                    since_month=since[:7] if since else None)
    for entry, body in blocks:
        if since and entry.timestamp < since:
            continue
        if entry.id not in found or entry.timestamp > found[entry.id][0].timestamp:
            found[entry.id] = (entry, body)
    return found


# --- writing ------------------------------------------------------------------------------------


class _Busy(Exception):
    """The library's lock stayed held for the whole wait."""


def _acquire(stack: ExitStack, path: Path, wait: float) -> None:
    """Take ``path``'s lock without blocking; retry with backoff for ``wait`` seconds, else raise ``_Busy``."""
    deadline = time.monotonic() + wait
    delay = 0.05
    while True:
        try:
            stack.enter_context(file_lock(path, blocking=False))
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Busy(f"the library lock stayed held for {wait:.0f} s") from None
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 2.0)


def _repair(path: Path, label: str) -> bool:
    """Cut a block a crash left half written at the end of a part, so it is appended again whole.

    A part is created whole (header and first block), so only appended blocks can be cut short.
    Returns True when it cut something.
    """
    end = f"\n{_MACHINE_LINE} {label}\n\n".encode("utf-8")
    with open(path, "rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - len(end)))
        tail = stream.read()
    if tail.endswith(end):
        return False
    data = path.read_bytes()
    keep = data.rfind(end)
    if keep < 0:
        raise OSError(f"{path.name} holds no complete entry of {label}")
    os.truncate(path, keep + len(end))
    return True


def _append(path: Path, data: bytes, *, header: bytes | None, label: str) -> bool:
    """Append ``data`` to a part and fsync it; a new part (``header`` given) is written whole.

    Returns True when a block cut short by a crash was removed first.
    """
    if header is not None:
        user_library.atomic_write(path, header + data)
        return False
    repaired = _repair(path, label)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return repaired


def _write(repo: _Repository, items: list[_Item], *, wait: float) -> dict:
    """Append ``items`` (oldest first) to this machine's segments under the library lock.

    Returns ``{"status", "written": [ids], "present": [ids], "held": [ids]}``: ``present`` were
    already in the month's parts, ``held`` belong to a month whose part is excluded one by one and
    stay on this machine. A failure part way returns what was written before it with ``status:
    failed``; nothing is ever written twice, because ids already in the parts are skipped under
    the lock.
    """
    library, label = repo.library, repo.label
    if not (library / ".git").is_dir():  # moved or gone: never recreate it
        return {"status": "failed", "reason": "no_library", "message": f"{library} is not a synced library",
                "written": [], "present": [], "held": []}
    changed: list[str] = []
    written: list[str] = []
    present: list[str] = []
    held: list[str] = []
    try:
        with ExitStack() as stack:
            _acquire(stack, library / ".lock", wait)
            scopes = _scopes(library)
            if _excluded(repo, scopes):
                return {"status": "skipped", "reason": "excluded", "written": [], "present": [], "held": []}
            usable, stored = _stored_origin(repo)
            if not usable:
                return {"status": "failed", "reason": "unsafe_path", "message": f"{repo.group}/{REPO_META}",
                        "written": [], "present": [], "held": []}
            if stored and stored != repo.origin:
                return {"status": "skipped", "reason": "other_origin", "found": [stored], "written": [], "present": [], "held": []}
            relative = repo.relative(label)
            directory = _directory(library, relative, create=True)
            if directory is None:
                return {"status": "failed", "reason": "unsafe_path", "message": relative, "written": [], "present": [], "held": []}
            if stored != repo.origin:
                # The format of src.user_flows, so two machines write the same bytes.
                user_library.atomic_write(library / repo.group / REPO_META,
                                          json.dumps({"origin": repo.origin}, indent=2).encode() + b"\n")
                changed.append(f"{repo.group}/{REPO_META}")
            by_month: dict[str, list[_Item]] = {}
            for item in items:
                by_month.setdefault(item.month, []).append(item)
            for month, month_items in sorted(by_month.items()):
                parts = _parts(directory, month)
                for _, _, path in parts:
                    if _header_origin(path) != repo.origin:
                        return {"status": "skipped", "reason": "other_origin",
                                "found": [_header_origin(path) or "unknown"], "written": written, "present": present, "held": held}
                known = set().union(*(_known_ids(path) for _, _, path in parts))
                names = {f"{relative}/{path.name}" for _, _, path in parts}
                number = parts[-1][1] if parts else 1
                if names & scopes.exclude_files or f"{relative}/{_part_name(month, number)}" in scopes.exclude_files:
                    present += [item.id for item in month_items if item.id in known]
                    held += [item.id for item in month_items if item.id not in known]
                    continue
                path = directory / _part_name(month, number)
                exists = bool(parts)
                size = path.stat().st_size if exists else 0
                count = len(_known_ids(path)) if exists else 0
                header = _header(label, month, repo.origin).encode("utf-8")
                for item in month_items:
                    if item.id in known:
                        present.append(item.id)
                        continue
                    data = item.text.encode("utf-8")
                    if count and size + len(data) > PART_BYTES:
                        number += 1
                        path = directory / _part_name(month, number)
                        if f"{relative}/{path.name}" in scopes.exclude_files:
                            held += [later.id for later in month_items[month_items.index(item):]
                                     if later.id not in known]
                            break
                        exists, size, count = False, 0, 0
                    created = not exists
                    repaired = _append(path, data, header=None if exists else header, label=label)
                    if f"{relative}/{path.name}" not in changed:
                        changed.append(f"{relative}/{path.name}")
                    size = path.stat().st_size
                    exists, count = True, count + 1
                    known.add(item.id)
                    written.append(item.id)
                    with _CACHE_GUARD:  # the part's ids, without parsing it again
                        previous = _KNOWN.get(str(path))
                        if created:
                            _KNOWN[str(path)] = (_stat_key(path), frozenset({item.id}))
                        elif previous is not None and not repaired:
                            _KNOWN[str(path)] = (_stat_key(path), previous[1] | {item.id})
                        else:
                            _KNOWN.pop(str(path), None)  # parsed again on next use
            changed += user_library.ensure_root_files(library)
    except _Busy as error:
        return {"status": "failed", "reason": "library_busy", "message": str(error), "written": written, "present": present, "held": held}
    except scope.ScopeError as error:
        return {"status": "failed", "reason": "scopes_invalid", "message": str(error), "written": written, "present": present, "held": held}
    except OSError as error:
        return {"status": "failed", "reason": "write_failed", "message": f"{type(error).__name__}: {error}",
                "written": written, "present": present, "held": held}
    finally:
        user_library.notify(library, changed)
    return {"status": "exported" if written else "unchanged", "written": written, "present": present, "held": held}


# --- the reconcile ------------------------------------------------------------------------------


def _waiting(repo: _Repository, roots: list[str]) -> int:
    """Entries of the key's checkouts that this machine has not shared yet (before secret checks)."""
    shared = set()
    for _, path in _segments(repo, others=False)[0]:
        shared |= _known_ids(path)
    ids = set()
    for root in roots:
        reader = journal.HistoryReader(os.path.join(root, "history.md"), os.path.join(root, "history"))
        ids |= {entry.id for entry in reader.journal_entries()}
    return len(ids - shared)


def _set_waiting(machine: _Machine, repo: _Repository, count: int) -> None:
    def change(state):
        waiting = state.setdefault("waiting", {})
        if count:
            waiting[repo.key] = {"origin": repo.origin, "entries": count}
        else:
            waiting.pop(repo.key, None)
    _change_state(machine.state_dir, change)


def reconcile(root, *, state_dir=None, library=None, full: bool = False,
              wait: float | None = None) -> dict:
    """Share the checkout's journal entries that this machine's segment lacks.

    Registers the checkout, counts its waiting entries while its repository is not approved, and
    for an approved one appends the entries from its watermark on (all of them with ``full``).
    Never raises: a failure is recorded as ``history_error`` and left to the next run. ``wait``
    defaults to ``LOCK_WAIT_SECONDS``.
    """
    try:
        return _reconcile(root, state_dir=state_dir, library=library, full=full,
                          wait=LOCK_WAIT_SECONDS if wait is None else wait)
    except Exception as error:  # noqa: BLE001 - the caller is a worker or a command; record, never raise
        machine = None
        try:
            machine = _machine(state_dir, library)
            if machine is not None:
                _record_error(machine, "reconcile_failed", f"{type(error).__name__}: {error}", None)
        except Exception:  # noqa: BLE001
            pass
        if machine is None:
            logger.warning("sharing the history of %s failed", root, exc_info=True)
        return {"status": "failed", "reason": "reconcile_failed", "message": f"{type(error).__name__}: {error}"}


def _reconcile(root, *, state_dir, library, full: bool, wait: float) -> dict:
    machine = _machine(state_dir, library)
    if machine is None:
        return {"status": "skipped", "reason": "not_set_up"}
    if not _LABEL.fullmatch(machine.label or ""):
        return {"status": "skipped", "reason": "label"}
    workspace = _workspace(root)
    if not workspace.top:
        return {"status": "skipped", "reason": "not_top_level"}
    if not _shared(workspace):
        return {"status": "skipped", "reason": "no_origin"}
    repo = _Repository(machine, workspace.key, workspace.origin)
    record = _register(machine, workspace)
    if repo.key not in machine.approved:
        roots = [path for path, value in _roots(machine).items() if value.get("key") == repo.key]
        count = _waiting(repo, roots or [workspace.root])
        _set_waiting(machine, repo, count)
        return {"status": "waiting", "key": repo.key, "entries": count}
    _set_waiting(machine, repo, 0)
    if not machine.settings.started:
        return {"status": "skipped", "reason": "not_started"}
    if machine.settings.paused:
        return {"status": "skipped", "reason": "paused"}
    try:
        scopes = _scopes(repo.library)
    except scope.ScopeError as error:
        _record_error(machine, "scopes_invalid", str(error), repo.key)
        return {"status": "failed", "reason": "scopes_invalid"}
    if _excluded(repo, scopes):
        return {"status": "skipped", "reason": "excluded"}
    watermark = None if full else record.get("watermark")
    since = None
    if isinstance(watermark, str) and watermark:
        try:
            since = (datetime.fromisoformat(watermark) - OVERLAP).isoformat(timespec="seconds")
        except ValueError:
            since = None
    entries = sorted(_journal(workspace.root, since).values(), key=lambda pair: (pair[0].timestamp, pair[0].id))
    segments, foreign = _segments(repo, scopes)
    _note_other_origin(machine, repo, foreign)
    known = set()
    for label, path in segments:
        if label == repo.label:
            known |= _known_ids(path)
    items, skipped = [], Counter()
    for entry, body in entries:
        if entry.id in known:
            continue
        item, reason = _item(entry, body, repo.label, repo.origin)
        if item is None:
            skipped[reason] += 1
        else:
            items.append(item)
    outcome = _write(repo, items, wait=wait) if items else \
        {"status": "unchanged", "written": [], "present": [], "held": []}
    if outcome["status"] == "skipped" and outcome.get("reason") == "other_origin":
        _note_other_origin(machine, repo, outcome.get("found", []))
    # The watermark moves past every entry handled; the first one left (failed or held) stops it.
    pending = {item.id for item in items} - set(outcome["written"]) - set(outcome["present"])
    handled = {entry.id for entry, _ in entries} - pending
    stop = next((entry.timestamp for entry, _ in entries if entry.id not in handled), None)
    reached = [entry.timestamp for entry, _ in entries if stop is None or entry.timestamp < stop]
    failed = outcome["status"] == "failed"

    def change(state):
        roots = state.setdefault("roots", {})
        current = roots.setdefault(workspace.root, {"key": repo.key, "origin": repo.origin})
        if reached and outcome["status"] != "skipped":
            current["watermark"] = max([reached[-1]] + ([current["watermark"]] if current.get("watermark") else []))
        if failed:
            state["error"] = {"reason": outcome["reason"], "message": outcome.get("message"), "key": repo.key,
                              "time": _now()}
        elif state.get("error") and state["error"].get("key") in (None, repo.key):
            state["error"] = None
    _change_state(machine.state_dir, change)
    return {**outcome, "key": repo.key, "skipped": dict(skipped)}


def reconcile_all(*, state_dir=None, library=None, keys=None, full: bool = False,
                  wait: float | None = None) -> list[dict]:
    """``reconcile`` every registered checkout (of ``keys`` only, when given); forget removed ones."""
    machine = _machine(state_dir, library)
    if machine is None:
        return []
    results = []
    for root, record in sorted(_roots(machine).items()):
        if keys is not None and record.get("key") not in keys:
            continue
        if not os.path.isdir(root):
            _change_state(machine.state_dir, lambda state, root=root: state.get("roots", {}).pop(root, None))
            continue
        results.append({"root": root, **reconcile(root, state_dir=state_dir, library=library,
                                                  full=full, wait=wait)})
    return results


# --- one worker per process ---------------------------------------------------------------------


_EVERY = object()


class _Exporter:
    """Reconciles dirty checkouts in one daemon thread; requests for the same checkout coalesce."""

    def __init__(self):
        self._condition = threading.Condition()
        self._pending: dict[tuple, object] = {}   # (state_dir, library) -> set of roots, or _EVERY
        self._busy = 0
        self._thread: threading.Thread | None = None
        self.installed = False

    def mark(self, state_dir, library, root=None) -> None:
        key = (str(state_dir) if state_dir else None, str(library) if library else None)
        with self._condition:
            if root is None:
                self._pending[key] = _EVERY
            elif self._pending.get(key) is not _EVERY:
                self._pending.setdefault(key, set()).add(str(root))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="history-sync", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending:
                    self._condition.wait()
                key, roots = next(iter(self._pending.items()))
                del self._pending[key]
                self._busy += 1
            state_dir, library = key
            try:
                if roots is _EVERY:
                    reconcile_all(state_dir=state_dir, library=library)
                else:
                    for root in sorted(roots):
                        reconcile(root, state_dir=state_dir, library=library)
            except Exception:  # noqa: BLE001 - the worker must survive; reconcile records its own failures
                logger.warning("sharing history between machines failed", exc_info=True)
            finally:
                with self._condition:
                    self._busy -= 1
                    self._condition.notify_all()

    def drain(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the pending runs; True when none remain."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._pending or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True


_EXPORTER = _Exporter()


def drain(timeout: float) -> bool:
    """Wait briefly for this process's history sync worker; what is left runs at the next catch-up."""
    return _EXPORTER.drain(timeout)


def request_catch_up(state_dir=None, library=None) -> None:
    """Catch up every registered checkout: in this process's worker when a server installed it, else now."""
    if _EXPORTER.installed:
        _EXPORTER.mark(state_dir, library)
    else:
        reconcile_all(state_dir=state_dir, library=library)


# --- reading ------------------------------------------------------------------------------------


def machine_history(root, *, state_dir=None, library=None) -> journal.MachineHistory | None:
    """The segments ``read_history`` merges for ``root``; None while its history stays local.

    It takes part once sync is set up here, for a checkout's top level with an ``origin``. The
    files are other machines' segments of the key and this machine's own (from its other
    checkouts); none while ``history`` or ``repos/<key>`` is excluded, single excluded parts and
    parts of another origin are left out. Segments already in the library are read also while
    sync is paused or the repository is not approved here.
    """
    machine = _machine(state_dir, library)
    if machine is None or not _LABEL.fullmatch(machine.label or ""):
        return None
    workspace = _workspace(root)
    if not _shared(workspace):
        return None
    repo = _Repository(machine, workspace.key, workspace.origin)
    empty = journal.MachineHistory(label=machine.label)
    try:
        scopes = _scopes(repo.library)
    except scope.ScopeError:
        return empty
    usable, stored = _stored_origin(repo)
    if _excluded(repo, scopes) or not usable or (stored and stored != repo.origin):
        return empty
    files, _ = _segments(repo, scopes)
    return journal.MachineHistory(label=machine.label, files=tuple((label, str(path)) for label, path in files))


class Integration:
    """What ``install`` hands to ``src.memory.history.set_sync``."""

    def __init__(self, *, state_dir=None, library=None):
        self.state_dir, self.library = state_dir, library

    def appended(self, history_path: str) -> None:
        _EXPORTER.mark(self.state_dir, self.library, Path(history_path).parent)

    def machines(self, history_path: str) -> journal.MachineHistory | None:
        root = Path(history_path).parent
        view = machine_history(root, state_dir=self.state_dir, library=self.library)
        if view is not None:
            machine = _machine(self.state_dir, self.library)
            if machine is not None and str(Path(root).resolve()) not in _roots(machine):
                _EXPORTER.mark(self.state_dir, self.library, root)  # register it, so its entries can wait
        return view


def install(*, state_dir=None, library=None) -> Integration:
    """Share history through the library from now on in this process, and catch up now.

    The MCP servers call it at startup; the catch-up runs in the worker, not in the caller.
    """
    integration = Integration(state_dir=state_dir, library=library)
    journal.set_sync(integration)
    _EXPORTER.installed = True
    _EXPORTER.mark(state_dir, library)
    return integration


def status(state_dir) -> dict:
    """What ``status`` shows about history: waiting repositories, the last failure, other origins."""
    state = _read_state(Path(state_dir))
    settings = Settings.load(Path(state_dir) / SETTINGS_FILE)
    waiting = state.get("waiting") if isinstance(state.get("waiting"), dict) else {}
    other = state.get("other_origin") if isinstance(state.get("other_origin"), dict) else {}
    approved = sorted(_approved(settings)) if settings else []
    return {
        "history_repositories": sorted(approved),
        "history_waiting": [{"key": key, "origin": value.get("origin"), "entries": value.get("entries", 0)}
                            for key, value in sorted(waiting.items()) if isinstance(value, dict)
                            and value.get("entries") and key not in approved],
        "history_error": state.get("error") or None,
        "history_other_origin": [{"key": key, **value} for key, value in sorted(other.items())
                                 if isinstance(value, dict)],
    }


# --- the owner's preview, approval and revocation -----------------------------------------------


def _require(state_dir, library) -> _Machine:
    machine = _machine(state_dir, library)
    if machine is None:
        raise SyncError("not_set_up", "sync is not set up; run setup first", state="off")
    if library and machine.settings.library and \
            Path(library).expanduser().resolve() != Path(machine.settings.library).expanduser().resolve():
        raise SyncError("library_mismatch", f"sync was set up for {machine.settings.library}, not {library}")
    if not _LABEL.fullmatch(machine.label or ""):
        raise SyncError("identity", "the machine label of the sync settings is not valid; run setup again")
    return machine


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= INTENT_CHARACTERS else text[:INTENT_CHARACTERS - 1] + "…"


def _plan(machine: _Machine, keys=(), paths=()) -> dict:
    """The repositories whose entries this machine has not shared yet, with every such entry."""
    for path in paths:
        workspace = _workspace(path)
        if not workspace.top:
            raise SyncError("invalid", f"{path} is not the top level of a git checkout")
        if not _shared(workspace):
            raise SyncError("no_origin", f"{path} has no origin remote: its history stays on this machine")
        _register(machine, workspace)
    roots: dict[str, list[str]] = {}
    origins: dict[str, str] = {}
    for root, record in sorted(_roots(machine).items()):
        key = record.get("key")
        if not isinstance(key, str) or not os.path.isdir(root) or (keys and key not in keys):
            continue
        workspace = _workspace(root)
        if not _shared(workspace) or workspace.key != key:
            continue
        roots.setdefault(key, []).append(workspace.root)
        origins[key] = workspace.origin
    scopes = _scopes(machine.library)
    repositories, notes = [], []
    for key in sorted(roots):
        repo = _Repository(machine, key, origins[key])
        if key in machine.approved:
            notes.append({"key": key, "origin": repo.origin, "reason": "approved"})
            continue
        if _excluded(repo, scopes):
            notes.append({"key": key, "origin": repo.origin, "reason": "excluded"})
            continue
        usable, stored = _stored_origin(repo)
        if not usable or (stored and stored != repo.origin):
            notes.append({"key": key, "origin": repo.origin, "reason": "other_origin", "found": stored})
            continue
        shared, foreign = _segments(repo, scopes, others=False)
        if foreign:
            notes.append({"key": key, "origin": repo.origin, "reason": "other_origin", "found": foreign})
            continue
        known = set()
        for _, path in shared:
            known |= _known_ids(path)
        entries: dict[str, tuple] = {}
        for root in roots[key]:
            for entry_id, pair in _journal(root).items():
                if entry_id not in entries or pair[0].timestamp > entries[entry_id][0].timestamp:
                    entries[entry_id] = pair
        items, skipped = [], Counter()
        for entry, body in sorted(entries.values(), key=lambda pair: (pair[0].timestamp, pair[0].id)):
            if entry.id in known:
                continue
            item, reason = _item(entry, body, repo.label, repo.origin)
            if item is None:
                skipped[reason] += 1
            else:
                items.append(item)
        if items or skipped:
            repositories.append({"key": key, "origin": repo.origin, "roots": roots[key], "items": items,
                                 "skipped": dict(sorted(skipped.items()))})
    if keys:
        for key in keys:
            if key not in roots:
                notes.append({"key": key, "reason": "unknown"})
    return {"repositories": repositories, "notes": notes}


def _digest(repositories: list[dict], cutoff: str) -> str:
    """The keys and entry ids up to ``cutoff``: what a confirmation approves."""
    covered = {repo["key"]: sorted(item.id for item in repo["items"] if item.timestamp[:19] <= cutoff)
               for repo in repositories}
    covered = {key: ids for key, ids in covered.items() if ids}
    return hashlib.sha256(json.dumps(covered, sort_keys=True).encode()).hexdigest()


def export(*, state_dir=None, library=None, keys=(), paths=(), confirm: str | None = None,
           wait: float | None = None) -> dict:
    """``history export``: the preview, or with ``confirm`` the approval and the export.

    The preview lists every entry of each repository this machine has not shared yet. The
    token it prints covers the keys and the entry ids up to its newest entry; entries written
    after the preview do not change it, because an approved repository's new entries follow by
    themselves anyway. With a matching token the keys are recorded as approved on this machine
    and their entries are exported right away.
    """
    machine = _require(state_dir, library)
    keys = tuple(dict.fromkeys(keys or ()))
    plan = _plan(machine, keys, paths)
    repositories = plan["repositories"]
    shown = [{"key": repo["key"], "origin": repo["origin"], "roots": repo["roots"],
              "count": len(repo["items"]), "skipped": repo["skipped"],
              "entries": [{"timestamp": item.timestamp, "intent": _cut(item.intent), "id": item.id}
                          for item in repo["items"]]} for repo in repositories]
    total = sum(repo["count"] for repo in shown)
    result = {"repositories": shown, "entries": total, "notes": plan["notes"]}
    if not total:
        return {"status": "up_to_date", **result,
                "message": "no repository waits for approval on this machine"}
    newest = max(item.timestamp for repo in repositories for item in repo["items"])
    cutoff = newest[:19]
    token = f"{_digest(repositories, cutoff)}-{cutoff.replace('-', '').replace(':', '')}"
    match = _TOKEN.fullmatch(confirm or "")
    if match is None or match["digest"] != _digest(repositories, _iso(match["cutoff"])):
        message = ("the preview changed; review it again and confirm" if confirm else
                   f"{total} entries of {len(shown)} repositories would be shared from this machine; "
                   "review them and confirm")
        return {"status": "confirmation_needed", **result, "hash": token, "message": message}
    approved = sorted(repo["key"] for repo in repositories
                      if any(item.timestamp[:19] <= _iso(match["cutoff"]) for item in repo["items"]))
    engine.Syncer(machine.library, machine.state_dir)._update_settings(
        lambda settings: setattr(settings, "history_repositories", sorted(_approved(settings) | set(approved))))
    results = reconcile_all(state_dir=machine.state_dir, library=machine.library, keys=set(approved),
                            full=True, wait=wait)
    exported = sum(len(item.get("written", [])) for item in results)
    failed = [item for item in results if item.get("status") == "failed"]
    waiting_run = [item.get("reason") for item in results if item.get("status") == "skipped"]
    message = f"approved {', '.join(approved)}; {exported} entries added to this machine's history in the library"
    if failed:
        message += f"; {len(failed)} checkouts failed ({failed[0].get('reason')}) and are retried later"
    elif waiting_run:
        message += f"; the rest follows once sync runs ({waiting_run[0]})"
    return {"status": "exported" if not failed else "attention", "approved": approved, "exported": exported,
            "results": results, "message": message}


def _iso(compact: str) -> str:
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}T{compact[9:11]}:{compact[11:13]}:{compact[13:15]}"


def revoke(key: str, *, state_dir=None, library=None) -> dict:
    """``history revoke``: share nothing more of ``key`` from this machine; shared segments stay."""
    machine = _require(state_dir, library)
    if key not in machine.approved:
        return {"status": "unchanged", "key": key, "message": f"{key} is not shared from this machine"}
    engine.Syncer(machine.library, machine.state_dir)._update_settings(
        lambda settings: setattr(settings, "history_repositories", sorted(_approved(settings) - {key})))
    return {"status": "revoked", "key": key,
            "message": "nothing more of it is shared from this machine; entries already shared stay in the library"}
