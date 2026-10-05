"""Each repository's ``history.md`` merged across the user's machines through the synced library (#172).

Every machine keeps its own append-only ``history.md`` (``src.memory.history``); nothing from other
machines is ever written into it. While sync runs, each new entry is also appended to this machine's
segment of the library::

    repos/<key>/history/<label>/<YYYY-MM>.md     this machine's entries of that month (UTC)
    repos/<key>/history/<label>/<YYYY-MM>-2.md   the continuation once a part would pass 4 MiB

``<key>`` is ``src.user_flows.repo_key`` of the repository and ``<label>`` the machine label of the
sync settings. Only the machine with that label writes its files, so segments never conflict. They
sync like the repository's flows: only for keys that come from an ``origin``, and only while neither
``repos/<key>`` nor ``history`` is excluded. A segment holds the entry blocks of ``history.md`` with
one more field line, ``**Machine:** <label>``; the heading, and with it the content hash, is the
same on every machine. An entry the secret scanner would flag stays on this machine, because one
hit would keep the whole month's segment out of every commit.

``read_history`` merges this machine's journal with the other machines' segments of the same key
(``src.memory.history.merge_entries``). Entries written before sync started are added with
``python -m src.user_sync history export``, after a preview.

The MCP servers install ``Integration`` at startup (``install``). Segments are written under the
library's ``.lock``, which a sync run holds for its local steps, and never while a history lock is
held; the backfill reads ``history.md`` under the shared history lock and releases it before it
takes the library lock. Reading a segment needs no lock, because every write replaces the whole
file. Nothing is pruned: segments grow by one file per machine and month.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat

from src import user_library
from src.file_lock import file_lock
from src.memory import history as journal
from src.user_library import REPO_META
from src.user_sync import scope
from src.user_sync.engine import SETTINGS_FILE, Settings, SyncError, default_library, default_state_dir

logger = logging.getLogger(__name__)

SEGMENTS = "history"              # repos/<key>/history/<label>/
PART_BYTES = 4 * 1024 * 1024      # a part that would pass this continues in the next one
FORMAT = 1
_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")   # what the engine accepts as a machine label
_PART = re.compile(r"(?P<month>[0-9]{4}-(?:0[1-9]|1[0-2]))(?:-(?P<number>[2-9]|[1-9][0-9]+))?\.md")
_MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_MACHINE_LINE = "**Machine:**"

# (library, kind, errno) of export failures already logged: a persistent failure warns once.
_WARNED: set = set()


# --- where -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Repository:
    """The sync settings of this machine and one repository's key in the library."""

    settings: Settings
    library: Path
    key: str
    origin: str | None

    @property
    def group(self) -> str:
        return f"repos/{self.key}"

    @property
    def label(self) -> str:
        return self.settings.label


def _repository(root: Path, state_dir=None, library=None) -> _Repository | None:
    """None while sync is not set up on this machine."""
    settings = Settings.load(Path(state_dir or default_state_dir()) / SETTINGS_FILE)
    if settings is None:
        return None
    from src.user_flows import repo_key
    key, origin = repo_key(Path(root))
    configured = library or settings.library or default_library()
    return _Repository(settings, Path(configured).expanduser().resolve(), key, origin)


def _blocked(repo: _Repository | None) -> str | None:
    """Why this machine does not share the repository's history now, or None."""
    if repo is None:
        return "not_set_up"
    if not repo.settings.started:
        return "not_started"
    if repo.settings.paused:
        return "paused"
    if not isinstance(repo.label, str) or not _LABEL.fullmatch(repo.label):
        return "label"
    if repo.origin is None or not scope.is_group(repo.group):
        return "no_origin"
    return None


def _excluded(repo: _Repository) -> str | None:
    """``excluded`` when the shared exclusions keep the repository or history out of sync.

    Raises ``scope.ScopeError`` for a malformed scopes file: exporting as if it excluded nothing
    could leak.
    """
    path = repo.library / scope.SCOPES_PATH
    if path.is_symlink():
        raise scope.ScopeError(f"{scope.SCOPES_PATH} must not be a symlink")
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = None
    scopes = scope.parse_scopes(data)
    return "excluded" if repo.group in scopes.exclude or "history" in scopes.exclude else None


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


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        return stream.read()


def _ids(text: str) -> set[str]:
    return {entry.id for entry, _ in journal.entry_blocks(text)}


# --- the entry as a segment stores it ---------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    id: str
    month: str
    timestamp: str
    text: str


def _item(block: str, label: str, origin: str) -> tuple[_Item | None, str | None]:
    """``(item, None)``, or ``(None, reason)`` for a block that stays on this machine."""
    pairs = journal.entry_blocks(block)
    if len(pairs) != 1:
        return None, "invalid"
    entry, body = pairs[0]
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
    return _Item(entry.id, month, entry.timestamp, text), None


def _header(label: str, month: str, origin: str) -> str:
    return ("---\n"
            f"repo: {origin}\n"
            f"machine: {label}\n"
            f"month: {month}\n"
            f"format_version: {FORMAT}\n"
            "---\n")


# --- writing --------------------------------------------------------------------------------


def _write(repo: _Repository, items: list[_Item]) -> dict:
    """Append ``items`` to this machine's segments under the library lock.

    An entry already in its month's parts is skipped. Every changed part is written once,
    atomically; the paths go to the library's change listeners, also after a failure part way.
    """
    library, label = repo.library, repo.label
    changed: list[str] = []
    written: list[str] = []
    if not (library / ".git").is_dir():  # moved or gone: never recreate it
        return {"status": "skipped", "reason": "no_library"}
    try:
        with file_lock(library / ".lock"):
            reason = _excluded(repo)
            if reason:
                return {"status": "skipped", "reason": reason}
            group = _directory(library, repo.group, create=True)
            if group is None:
                return {"status": "skipped", "reason": "unsafe_path"}
            meta = group / REPO_META
            if meta.is_symlink() or (meta.exists() and not _regular(meta)):
                return {"status": "skipped", "reason": "unsafe_path"}
            stored = None
            try:
                stored = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            stored_origin = stored.get("origin") if isinstance(stored, dict) else None
            if isinstance(stored_origin, str) and stored_origin and stored_origin != repo.origin:
                return {"status": "skipped", "reason": "other_origin"}
            relative = f"{repo.group}/{SEGMENTS}/{label}"
            directory = _directory(library, relative, create=True)
            if directory is None:
                return {"status": "skipped", "reason": "unsafe_path"}
            if stored_origin != repo.origin:
                # The format of src.user_flows, so two machines write the same bytes.
                user_library.atomic_write(meta, json.dumps({"origin": repo.origin}, indent=2).encode() + b"\n")
                changed.append(f"{repo.group}/{REPO_META}")
            by_month: dict[str, list[_Item]] = {}
            for item in items:
                by_month.setdefault(item.month, []).append(item)
            for month, month_items in sorted(by_month.items()):
                parts = _parts(directory, month)
                stored_parts = {part: part.read_bytes() for _, _, part in parts}
                known = set().union(*(_ids(data.decode("utf-8", "replace")) for data in stored_parts.values()))
                number = parts[-1][1] if parts else 1
                path = directory / _part_name(month, number)
                current = stored_parts.get(path)
                if current is None:
                    current = _header(label, month, repo.origin).encode("utf-8")
                chunks, size, count, dirty = [current], len(current), len(_ids(current.decode("utf-8", "replace"))), False
                for item in month_items:
                    if item.id in known:
                        continue
                    data = item.text.encode("utf-8")
                    if count and size + len(data) > PART_BYTES:
                        if dirty:
                            user_library.atomic_write(path, b"".join(chunks))
                            changed.append(f"{relative}/{path.name}")
                        number += 1
                        path = directory / _part_name(month, number)
                        header = _header(label, month, repo.origin).encode("utf-8")
                        chunks, size, count, dirty = [header], len(header), 0, False
                    chunks.append(data)
                    size, count, dirty = size + len(data), count + 1, True
                    known.add(item.id)
                    written.append(item.id)
                if dirty:
                    user_library.atomic_write(path, b"".join(chunks))
                    changed.append(f"{relative}/{path.name}")
            changed += user_library.ensure_root_files(library)
    finally:
        user_library.notify(library, changed)
    return {"status": "exported" if written else "unchanged", "entries": written,
            "paths": [path for path in changed if f"/{SEGMENTS}/" in path]}


def export_entry(root, block: str, *, state_dir=None, library=None) -> dict:
    """Append one entry block of ``root``'s ``history.md`` to this machine's segment.

    Returns ``{"status": "exported" | "unchanged" | "skipped" | "failed", ...}`` and never raises:
    the entry is already in ``history.md``, and sharing it must not fail ``log_interaction``.
    """
    repo = None
    try:
        repo = _repository(Path(root), state_dir, library)
        reason = _blocked(repo)
        if reason:
            return {"status": "skipped", "reason": reason}
        item, reason = _item(block, repo.label, repo.origin)
        if item is None:
            if reason == "secret":
                logger.info("A history entry of %s with possible credentials stays on this machine", root)
            return {"status": "skipped", "reason": reason}
        return _write(repo, [item])
    except Exception as error:  # noqa: BLE001 - reported, never raised
        library_name = str(repo.library) if repo else str(library)
        key = (library_name, type(error).__name__, getattr(error, "errno", None))
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning("Could not share a history entry of %s through %s: %s", root, library_name, error)
        return {"status": "failed", "error": f"{type(error).__name__}: {error}"}


# --- reading --------------------------------------------------------------------------------


def _segments(repo: _Repository, *, own: bool) -> list[tuple[str, Path]]:
    """``(label, path)`` of the repository's segment parts in the library, by label, then oldest first."""
    found = []
    base = _directory(repo.library, f"{repo.group}/{SEGMENTS}", create=False)
    try:
        labels = sorted(os.listdir(base)) if base is not None else []
    except OSError:
        labels = []
    for label in labels:
        if not _LABEL.fullmatch(label) or (label == repo.label and not own):
            continue
        directory = _directory(base, label, create=False)
        for _, _, path in _parts(directory) if directory is not None else ():
            try:
                if path.stat().st_size > scope.MAX_FILE_BYTES:
                    continue  # never synced, so not another machine's segment
            except OSError:
                continue
            found.append((label, path))
    return found


def machine_history(root, *, state_dir=None, library=None) -> journal.MachineHistory | None:
    """The other machines' segments for ``root``; None while its history does not take part in sync.

    It takes part once sync is set up on this machine and the repository's key comes from an
    ``origin``. Segments already in the library are read even while sync is paused or excluded.
    """
    repo = _repository(Path(root), state_dir, library)
    if repo is None or repo.origin is None or not scope.is_group(repo.group) \
            or not isinstance(repo.label, str) or not _LABEL.fullmatch(repo.label):
        return None
    return journal.MachineHistory(label=repo.label, files=tuple(
        (label, str(path)) for label, path in _segments(repo, own=False)))


class Integration:
    """What ``install`` hands to ``src.memory.history.set_sync``."""

    def __init__(self, *, state_dir=None, library=None):
        self.state_dir, self.library = state_dir, library

    def exported(self, history_path: str, block: str) -> None:
        export_entry(Path(history_path).parent, block, state_dir=self.state_dir, library=self.library)

    def machines(self, history_path: str) -> journal.MachineHistory | None:
        return machine_history(Path(history_path).parent, state_dir=self.state_dir, library=self.library)


def install(*, state_dir=None, library=None) -> Integration:
    """Share history through the library from now on in this process; the MCP servers call it at startup."""
    integration = Integration(state_dir=state_dir, library=library)
    journal.set_sync(integration)
    return integration


# --- adding entries written before sync started ------------------------------------------------


def backfill(repo_root, *, state_dir=None, library=None, confirm: str | None = None) -> dict:
    """Add the repository's earlier entries to this machine's segments, after a preview.

    Without ``confirm`` (or with a hash that no longer matches) it returns how many entries it
    would add per month and the hash that confirms exactly them. Entries already in a segment of
    this repository, from any machine, are skipped by hash, as are entries the secret scanner
    flags. Takes the shared history lock to read and the library lock to write, never both.
    """
    root = Path(repo_root).expanduser().resolve()
    repo = _repository(root, state_dir, library)
    if repo is None:
        raise SyncError("not_set_up", "sync is not set up; run setup first", state="off")
    if library and repo.settings.library and \
            Path(library).expanduser().resolve() != Path(repo.settings.library).expanduser().resolve():
        raise SyncError("library_mismatch", f"sync was set up for {repo.settings.library}, not {library}")
    reason = _blocked(repo)
    messages = {
        "not_started": ("sync has not started on this machine; run preview and start first", "off"),
        "paused": ("sync is paused on this machine; resume it first", "paused"),
        "label": ("the machine label of the sync settings is not valid; run setup again", "attention"),
        "no_origin": (f"{root} has no origin remote: its history stays on this machine", "attention"),
    }
    if reason:
        message, state = messages[reason]
        raise SyncError(reason, message, state=state)
    if _excluded(repo):
        raise SyncError("excluded", f"{repo.group} or history is excluded from sync; include it first")
    history_path = root / "history.md"
    files = journal.history_files(str(history_path), str(root / "history"))
    if not files:
        raise SyncError("invalid", f"{root} has no history.md or history/; pass the repository with --repo")
    latest: dict[str, tuple] = {}
    for _, _, text in files:
        for entry, body in journal.entry_blocks(text):
            if entry.id not in latest or entry.timestamp > latest[entry.id][0].timestamp:
                latest[entry.id] = (entry, body)
    present = set()
    for _, path in _segments(repo, own=True):
        present |= _ids(_read(path))
    items, skipped = [], Counter()
    for entry, body in sorted(latest.values(), key=lambda pair: (pair[0].timestamp, pair[0].id)):
        if entry.id in present:
            continue
        item, why = _item(body, repo.label, repo.origin)
        if item is None:
            skipped[why] += 1
        else:
            items.append(item)
    months = Counter(item.month for item in items)
    result = {"repo": repo.origin, "key": repo.key, "label": repo.label, "entries": len(items),
              "months": [{"month": month, "entries": count} for month, count in sorted(months.items())],
              "skipped": dict(sorted(skipped.items())), "present": len(present & set(latest))}
    if not items:
        return {"status": "up_to_date", **result,
                "message": "nothing to add: the other entries are already in the library"
                           if skipped else "every entry of this repository is already in the library"}
    plan = hashlib.sha256(json.dumps({
        "key": repo.key, "label": repo.label, "library": str(repo.library),
        "entries": [[item.month, item.id, hashlib.sha256(item.text.encode("utf-8")).hexdigest()] for item in items],
    }, sort_keys=True).encode()).hexdigest()
    if confirm != plan:
        message = ("the preview changed; review it again and confirm" if confirm else
                   f"{len(items)} entries would be added to this machine's history in the library and "
                   "uploaded by the next sync; review them and confirm")
        return {"status": "confirmation_needed", **result, "hash": plan, "message": message}
    outcome = _write(repo, items)
    if outcome["status"] == "skipped":
        raise SyncError(outcome["reason"], f"nothing was written: {outcome['reason']}")
    written = set(outcome["entries"])
    months = Counter(item.month for item in items if item.id in written)
    return {"status": "exported", **result, "entries": len(written),
            "months": [{"month": month, "entries": count} for month, count in sorted(months.items())],
            "paths": outcome["paths"],
            "message": "the next sync uploads them"}
