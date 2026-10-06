"""Append-only intent/action/outcome history with optional semantic recall.

Three classes:

* ``HistoryWriter`` — stdlib-only. Writes deduplicated, content-hashed entries
  to ``history.md`` under an exclusive file lock. Rotates to
  ``history/YYYY-MM.md`` when the file grows past
  ``HISTORY_ROTATION_THRESHOLD_KB``.

* ``HistoryReader`` — stdlib-only. Parses entries from disk and serves
  recency / ``since`` queries.

* ``HistoryStore`` — lazy ``NumpyVectorStore`` wrapper. Built only on the
  first ``read_history(query=...)`` call; rebuilds when the content of the
  markdown file or the embedding fingerprint changes, re-embedding only new
  or edited entries while the fingerprint is unchanged. The embedder is
  imported lazily so ``HistoryWriter``/``HistoryReader`` users never pay the
  numpy cost.

With the user library sync installed (``set_sync``; the MCP servers do it at
startup), each append tells the sync's worker that the checkout changed (it
never waits for it), and reads merge the checkout's journal (``history.md`` and
``history/*.md``) with the entries the user's machines shared for the same
repository (``src/user_sync/history.py``), newest month first. This module
never imports the sync itself.
"""

from __future__ import annotations

from src.file_lock import file_lock

import contextlib
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from src.memory import config as _memory_config
from src.memory.config import (
    HISTORY_DEDUP_TAIL_SIZE,
    HISTORY_FORMAT_VERSION,
    HISTORY_ROTATION_THRESHOLD_KB,
    HISTORY_VECTOR_STORE_NAME,
)

logger = logging.getLogger(__name__)

# (path, errno) of rotation failures already logged: a persistent failure warns once.
_ROTATION_WARNED: set = set()


# A lock on history.md itself — fcntl on POSIX, a no-op shim on Windows. Appends
# also take the sidecar lock (src.file_lock), which excludes other processes on both.
try:  # pragma: no cover - exercised via integration only
    import fcntl as _fcntl

    def _lock_exclusive(fh) -> None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)

    def _unlock(fh) -> None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)

except ImportError:  # pragma: no cover - Windows
    def _lock_exclusive(fh) -> None:
        return None

    def _unlock(fh) -> None:
        return None

# Process-local mutex — fcntl.flock is advisory and per-process on Linux,
# so concurrent threads (e.g. multiple run_in_executor calls) can bypass it.
_WRITE_LOCK = threading.Lock()


_HEADER_RE = re.compile(
    r"^##\s+(?P<ts>\S+)\s+\|\s+(?P<id>[0-9a-f]{12})\s*$",
    re.MULTILINE,
)
_FIELD_RE = re.compile(r"^\*\*(?P<name>[A-Za-z]+):\*\*\s*(?P<value>.*)$")
# Monthly archives that rotation writes into history/.
_ARCHIVE_NAME = re.compile(r"[0-9]{4}-[0-9]{2}\.md")


@dataclass
class HistoryEntry:
    """In-memory representation of a single history entry.

    ``machine`` tells where a merged read found it: None for this checkout's
    own journal, this machine's label for an entry it wrote in another
    checkout of the repository, another machine's label for that machine's
    entries. A block parsed alone takes it from its ``**Machine:**`` line.
    """
    id: str
    timestamp: str
    intent: str
    action: str
    outcome: str
    files: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None
    machine: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _sidecar(history_path: str) -> str:
    """The stable lock file of a history file (``.history.md.lock`` beside it)."""
    return os.path.join(os.path.dirname(history_path), "." + os.path.basename(history_path) + ".lock")


@contextlib.contextmanager
def _reading(history_path: str):
    """The history lock, shared. Readers take it so a rotation is never seen half way and,
    on Windows, never fails on a file a reader holds open; a reader that may not open the
    lock file (a read-only checkout) reads without it, as before."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(_sidecar(history_path), shared=True))
        except OSError:
            pass
        yield


def _archive_names(archive_dir: str) -> List[str]:
    try:
        return sorted(name for name in os.listdir(archive_dir) if _ARCHIVE_NAME.fullmatch(name))
    except OSError:
        return []


def _month_after(month: str) -> str:
    """``YYYY-MM-01T00:00:00`` of the month after ``month``: no entry of a ``month`` file is that late."""
    year, number = int(month[:4]), int(month[5:7])
    year, number = (year + 1, 1) if number == 12 else (year, number + 1)
    return f"{year:04d}-{number:02d}-01T00:00:00"


# --- Parsed files ------------------------------------------------------------

class _ParsedFiles:
    """Entries of history files by path, kept while a file's size and mtime stay the same.

    Archives and other machines' segments rarely change, so repeated reads and
    index checks parse only what changed. Bounded by file count and bytes.
    """

    def __init__(self, max_files: int = 256, max_bytes: int = 64 * 1024 * 1024):
        self._items: "OrderedDict[str, tuple]" = OrderedDict()
        self._bytes = 0
        self._max_files, self._max_bytes = max_files, max_bytes
        self._lock = threading.Lock()

    def load(self, path: str) -> Optional[Tuple[str, Tuple[HistoryEntry, ...]]]:
        """``(sha256 of the bytes, entries)``, or None when ``path`` is not a regular file.

        Other read errors propagate; entries are shared, so callers copy before changing them.
        """
        try:
            info = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        key = (info.st_ino, info.st_size, info.st_mtime_ns)  # a replaced file differs even at equal size and time
        with self._lock:
            cached = self._items.get(path)
            if cached is not None and cached[0] == key:
                self._items.move_to_end(path)
                return cached[1], cached[2]
        with open(path, "rb") as stream:
            data = stream.read()
            info = os.fstat(stream.fileno())
        digest = hashlib.sha256(data).hexdigest()
        entries = tuple(HistoryReader._parse(data.decode("utf-8", "replace")))
        with self._lock:
            previous = self._items.pop(path, None)
            if previous is not None:
                self._bytes -= previous[3]
            if len(data) == info.st_size:  # unchanged while it was read: safe to keep
                self._items[path] = ((info.st_ino, info.st_size, info.st_mtime_ns), digest, entries, len(data))
                self._bytes += len(data)
                while self._items and (len(self._items) > self._max_files or self._bytes > self._max_bytes):
                    _, dropped = self._items.popitem(last=False)
                    self._bytes -= dropped[3]
        return digest, entries


_PARSED = _ParsedFiles()


def parsed_file(path: str) -> Optional[Tuple[str, Tuple[HistoryEntry, ...]]]:
    """``(sha256, entries)`` of a history file through the shared parse cache; see ``_ParsedFiles``."""
    return _PARSED.load(path)


# --- Sync with the user's other machines (optional) -------------------------

@dataclass(frozen=True)
class MachineHistory:
    """The synced history of one repository, as the user library sync finds it.

    ``label`` names this machine. ``files`` holds ``(label, path)`` of history
    segments of the same repository: other machines', and this machine's own
    from its other checkouts of the repository (their label is ``label``).
    """
    label: str
    files: Tuple[Tuple[str, str], ...] = ()


_sync = None


def set_sync(integration):
    """Install the user library sync integration, or remove it with None; returns the previous one.

    ``integration.appended(history_path, entry_id)`` is called after each append, once
    every history lock is released, and must return at once;
    ``integration.machines(history_path)`` returns a ``MachineHistory``, or None
    while the repository's history does not take part in sync. Without an
    integration the history is this machine's journal only.
    ``src.user_sync.history.install`` sets it.
    """
    global _sync
    previous, _sync = _sync, integration
    return previous


def _machine_history(history_path: str) -> Optional[MachineHistory]:
    integration = _sync
    if integration is None:
        return None
    try:
        return integration.machines(history_path)
    except Exception:
        logger.warning("could not list other machines' history for %s", history_path, exc_info=True)
        return None


def _appended(history_path: str, entry_id: str) -> None:
    """Tell the sync that an entry was appended; its failure never fails the append."""
    integration = _sync
    if integration is None:
        return
    try:
        integration.appended(history_path, entry_id)
    except Exception:
        logger.warning("could not schedule sharing the history of %s", history_path, exc_info=True)


def entry_blocks(content: str) -> List[Tuple[HistoryEntry, str]]:
    """Each entry of ``content`` with its block: the heading and its field lines.

    Text between entries that is not a field (an archive's separator or
    header) is left out, so a block holds exactly what the writer rendered.
    """
    out: List[Tuple[HistoryEntry, str]] = []
    positions = [m.start() for m in _HEADER_RE.finditer(content)]
    positions.append(len(content))
    for start, end in zip(positions, positions[1:]):
        lines = content[start:end].splitlines()
        entry = HistoryReader._parse_block(content[start:end])
        if entry is None:
            continue
        kept = [lines[0].rstrip()] + [line.rstrip() for line in lines[1:] if _FIELD_RE.match(line)]
        out.append((entry, "\n".join(kept)))
    return out


def _journal_texts(history_path: str, archive_dir: Optional[str], since_month: Optional[str]) -> List[str]:
    """This machine's journal, oldest first: archives from ``since_month`` on, a pending rotation, history.md.

    ``history.md`` and the pending file are read under the shared history lock;
    the archives after it, without the lock: rotation replaces or creates them
    whole, so an entry that moved meanwhile is read twice (and deduplicated),
    never missed, and a reader never keeps an archive open while rotation runs.
    """
    archive_dir = archive_dir or os.path.join(os.path.dirname(history_path), "history")
    pending = history_path + ".rotating"
    live: List[str] = []
    if os.path.exists(history_path) or os.path.exists(pending):
        with _reading(history_path):
            for path in (pending, history_path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        live.append(fh.read())
                except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                    continue
    older = []
    for name in _archive_names(archive_dir):
        if since_month and name[:7] < since_month:
            continue
        try:
            with open(os.path.join(archive_dir, name), "r", encoding="utf-8", errors="replace") as fh:
                older.append(fh.read())
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            continue
    return older + live


def journal_blocks(
    history_path: str,
    archive_dir: Optional[str] = None,
    since_month: Optional[str] = None,
) -> List[Tuple[HistoryEntry, str]]:
    """Entries of this machine's journal with their blocks (see ``entry_blocks``), oldest file first.

    ``since_month`` (``YYYY-MM``) skips archives of earlier months, which hold
    no entry from that month on.
    """
    out: List[Tuple[HistoryEntry, str]] = []
    for text in _journal_texts(history_path, archive_dir, since_month):
        out.extend(entry_blocks(text))
    return out


def _older_sources(archive_dir: str, machines: MachineHistory):
    """``(bound, side, machine, path)`` of the archives and segments, newest month first.

    ``bound`` is a time no entry of the file reaches: an archive or segment of
    a month holds nothing later than that month (None when the name tells
    nothing, read first). This machine's archives (side 0) come before
    segments (side 1) of the same month.
    """
    out = []
    for name in _archive_names(archive_dir):
        out.append((_month_after(name[:7]), 0, None, os.path.join(archive_dir, name)))
    for label, path in machines.files:
        month = os.path.basename(path)[:7]
        bound = _month_after(month) if re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", month) else None
        out.append((bound, 1, label, path))
    out.sort(key=lambda source: source[1])
    out.sort(key=lambda source: source[0] or "~", reverse=True)  # stable: archives stay first
    return out


def _merge_into(chosen: Dict[str, HistoryEntry], machine: Optional[str], entries) -> None:
    """Add entries to a union by id: this machine's journal (machine None) wins, then the later copy."""
    for entry in entries:
        current = chosen.get(entry.id)
        if current is not None:
            if (current.machine is None) != (machine is None):
                if machine is not None:
                    continue
            elif entry.timestamp <= current.timestamp:
                continue
        chosen[entry.id] = replace(entry, machine=machine)


def merge_entries(files) -> List[HistoryEntry]:
    """A union by entry id of ``(machine, entries)`` pairs, oldest first.

    This machine's journal (machine None) wins over a segment; between copies
    from the same side, the later one wins.
    """
    chosen: Dict[str, HistoryEntry] = {}
    for machine, entries in files:
        _merge_into(chosen, machine, entries)
    return sorted(chosen.values(), key=lambda e: (e.timestamp, e.id))


def _machine_filter(machine: Optional[str], machines: Optional[MachineHistory]):
    """A predicate for ``read_history(machine=...)``, or None for every entry.

    ``local`` keeps this workspace's journal; this machine's label also keeps
    what this machine wrote in its other checkouts; another label, that machine's.
    """
    machine = (machine or "").strip()
    if not machine:
        return None
    if machine == "local":
        return lambda entry: entry.machine is None
    if machines is not None and machine == machines.label:
        return lambda entry: entry.machine in (None, machine)
    return lambda entry: entry.machine == machine


# --- Writer ------------------------------------------------------------------

class HistoryWriter:
    """Append-only writer for ``history.md``."""

    def __init__(
        self,
        history_path: Optional[str] = None,
        archive_dir: Optional[str] = None,
        rotation_kb: int = HISTORY_ROTATION_THRESHOLD_KB,
        dedup_tail: int = HISTORY_DEDUP_TAIL_SIZE,
    ):
        # Resolve defaults lazily via module attribute access so PEP 562 picks
        # up the current client repo root each time (see src/memory/config.py).
        self.history_path = history_path or _memory_config.HISTORY_FILE
        self.archive_dir = archive_dir or _memory_config.HISTORY_ARCHIVE_DIR
        self.rotation_kb = rotation_kb
        self.dedup_tail = dedup_tail

    # ------------------------------------------------------------------ public
    def append_entry(
        self,
        intent: str,
        action: str,
        outcome: str,
        files: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        dedupe_action: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a single entry; returns a JSON-friendly status dict.

        ``dedupe_action`` replaces ``action`` in the content hash, so a retry that
        differs only in an appended attribution line is still a duplicate.
        """
        intent = (intent or "").strip()
        action = (action or "").strip()
        outcome = (outcome or "").strip()
        if not (intent and action and outcome):
            return {
                "status": "error",
                "error": "intent, action, and outcome are all required",
            }

        entry_id = self._compute_entry_hash(intent, (dedupe_action or action).strip(), outcome)

        os.makedirs(os.path.dirname(self.history_path) or ".", exist_ok=True)

        # Open in append+read mode so we can scan the tail under the lock.
        # Every write + header + dedup + rotation happens INSIDE the lock so
        # concurrent writers (cross-process: e.g. Claude Desktop + VS Code
        # attached to the same repo) cannot interleave and corrupt the file.
        rotated_to: Optional[str] = None
        with _WRITE_LOCK, file_lock(_sidecar(self.history_path)):
            with open(self.history_path, "a+", encoding="utf-8", newline="") as fh:
                try:
                    _lock_exclusive(fh)

                    # Re-check size under the lock — a concurrent writer may have
                    # created the file between our os.makedirs and open().
                    fh.seek(0, os.SEEK_END)
                    if fh.tell() == 0:
                        fh.write(self._render_header())
                        fh.flush()

                    if self._is_duplicate_from_handle(fh, entry_id):
                        return {
                            "status": "duplicate",
                            "entry_id": entry_id,
                            "path": self.history_path,
                        }

                    timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
                    block = self._render_entry(
                        entry_id, timestamp, intent, action, outcome, files, tags, metadata
                    )
                    fh.write(block)
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    _unlock(fh)

            # Rotate after the append handle is closed (Windows cannot move an
            # open file) but still inside both locks, so the move/merge cannot
            # race a second writer. The entry is already written: a rotation
            # failure is logged and never turns it into an error.
            try:
                rotated_to = self._maybe_rotate_locked()
            except Exception as err:
                key = (self.history_path, getattr(err, "errno", None))
                if key not in _ROTATION_WARNED:
                    _ROTATION_WARNED.add(key)
                    logger.warning("history rotation failed for %s: %s", self.history_path, err)

        # Outside every history lock, and it only schedules the work: sharing the
        # entry waits for the library lock in the sync's own worker, never here.
        _appended(self.history_path, entry_id)

        result: Dict[str, Any] = {
            "status": "recorded",
            "entry_id": entry_id,
            "path": self.history_path,
            "timestamp": timestamp,
        }
        if rotated_to:
            result["rotated_to"] = rotated_to
        return result

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _compute_entry_hash(intent: str, action: str, outcome: str) -> str:
        h = hashlib.sha256()
        h.update(intent.encode("utf-8"))
        h.update(b"\x1f")
        h.update(action.encode("utf-8"))
        h.update(b"\x1f")
        h.update(outcome.encode("utf-8"))
        return h.hexdigest()[:12]

    def _render_header(self) -> str:
        repo_name = os.path.basename(os.path.dirname(os.path.abspath(self.history_path))) or "repo"
        timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        return (
            "---\n"
            f"repo: {repo_name}\n"
            f"created: {timestamp}\n"
            f"format_version: {HISTORY_FORMAT_VERSION}\n"
            "---\n"
        )

    @staticmethod
    def _render_entry(
        entry_id: str,
        timestamp: str,
        intent: str,
        action: str,
        outcome: str,
        files: Optional[List[str]],
        tags: Optional[List[str]],
        metadata: Optional[Dict[str, Any]],
    ) -> str:
        # Collapse newlines to spaces so multi-line raw query/response
        # doesn't break the single-line **Field:** format that the parser expects.
        def _oneline(s: str) -> str:
            return " ".join(s.split())

        lines = [
            "",  # blank separator before the heading
            f"## {timestamp} | {entry_id}",
            f"**Intent:** {_oneline(intent)}",
            f"**Action:** {_oneline(action)}",
            f"**Outcome:** {_oneline(outcome)}",
        ]
        if files:
            lines.append(f"**Files:** {', '.join(_oneline(f) for f in files)}")
        if tags:
            normalized = [_oneline(t) for t in tags]
            normalized = [t if t.startswith("#") else f"#{t}" for t in normalized]
            lines.append(f"**Tags:** {' '.join(normalized)}")
        if metadata:
            lines.append(f"**Meta:** {json.dumps(metadata, ensure_ascii=False, sort_keys=True)}")
        lines.append("")  # trailing newline
        return "\n".join(lines) + "\n"

    def _is_duplicate_from_handle(self, fh, entry_id: str) -> bool:
        """Tail-scan the last ``self.dedup_tail`` entry IDs using the
        already-open file handle (avoids opening a second fd under the lock)."""
        pos = fh.tell()
        fh.seek(0)
        content = fh.read()
        fh.seek(pos)
        ids = _HEADER_RE.findall(content)
        recent = [match[1] for match in ids[-self.dedup_tail:]]
        return entry_id in recent

    def _maybe_rotate_locked(self) -> Optional[str]:
        """Move ``history.md`` to ``history/YYYY-MM.md`` if it is too large.

        MUST be called while the caller still holds the exclusive lock on
        ``history.md`` — otherwise concurrent writers could race with the
        move/merge step.

        Returns the archive path if rotation happened, else ``None``.
        """
        # A pending file left by an interrupted rotation is merged first, before
        # any early return, so its entries never stay hidden or get overwritten.
        pending = self.history_path + ".rotating"
        if os.path.exists(pending):
            self._merge_pending_locked(pending)
        if not os.path.exists(self.history_path):
            return None
        size_kb = os.path.getsize(self.history_path) / 1024
        if size_kb < self.rotation_kb:
            return None

        # Use the timestamp of the last entry for the archive month.
        last_ts = self._read_last_timestamp() or _dt.datetime.now(_dt.timezone.utc).isoformat()
        month = last_ts[:7]  # YYYY-MM
        os.makedirs(self.archive_dir, exist_ok=True)
        archive_path = os.path.join(self.archive_dir, f"{month}.md")

        # If a file for this month already exists, append-merge with a separator
        # so multi-rotation months stay in one archive file.
        if os.path.exists(archive_path):
            # Move the live file aside first: if another process holds it open
            # (Windows), this fails before anything reaches the archive, so a
            # repeated failure cannot merge the same payload twice.
            os.replace(self.history_path, pending)
            try:
                archive_path = self._merge_pending_locked(pending)
            except Exception:
                # The archive was rolled back: put the entries back so
                # read_history still sees them.
                if not os.path.exists(self.history_path):
                    os.replace(pending, self.history_path)
                raise
        else:
            shutil.move(self.history_path, archive_path)

        # Recreate fresh history.md with header pointing at the archive.
        with open(self.history_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(self._render_header())
            fh.write(
                f"\n> Previous entries archived to "
                f"`{os.path.relpath(archive_path, os.path.dirname(self.history_path))}` "
                f"on {_dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')}.\n"
            )

        return archive_path

    @staticmethod
    def _discard_pending(pending: str) -> None:
        """Remove (or empty) an already-archived pending file, never raising.

        The archive holds the payload at this point, so a cleanup failure must
        not look like a failed merge: a leftover payload is recognized by
        `_archive_ends_with` and skipped on the next recovery.
        """
        try:
            os.unlink(pending)
            return
        except OSError:
            pass
        try:
            with open(pending, "w", encoding="utf-8"):
                pass
        except OSError as err:
            key = (pending, getattr(err, "errno", None))
            if key not in _ROTATION_WARNED:
                _ROTATION_WARNED.add(key)
                logger.warning("could not clear archived pending file %s: %s", pending, err)

    @staticmethod
    def _archive_ends_with(archive_path: str, payload: str) -> bool:
        data = payload.encode("utf-8")
        try:
            with open(archive_path, "rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                if size < len(data):
                    return False
                fh.seek(size - len(data))
                return fh.read() == data
        except OSError:
            return False

    def _merge_pending_locked(self, pending: str) -> str:
        """Append the pending file to its month's archive, then empty it.

        The archive is rebuilt in a temporary file and replaced atomically, so a
        failure or crash cannot leave partial content. Returns the archive path.
        """
        with open(pending, "r", encoding="utf-8") as src:
            payload = src.read()
        if not payload:
            # Emptied after an archived merge: nothing to add to the archive.
            try:
                os.unlink(pending)
            except OSError:
                pass
            return ""
        matches = _HEADER_RE.findall(payload)
        month = (matches[-1][0] if matches else _dt.datetime.now(_dt.timezone.utc).isoformat())[:7]
        os.makedirs(self.archive_dir, exist_ok=True)
        archive_path = os.path.join(self.archive_dir, f"{month}.md")
        existed = os.path.exists(archive_path)
        if existed and self._archive_ends_with(archive_path, payload):
            # A crash after the append but before the unlink: already archived.
            self._discard_pending(pending)
            return archive_path
        # Build the merged archive beside the real one and replace it in one
        # step: a crash or error at any point leaves the archive as it was, and
        # the pending file is only emptied after the replace.
        temp = archive_path + ".tmp"
        try:
            with open(temp, "wb") as dst:
                if existed:
                    with open(archive_path, "rb") as old:
                        shutil.copyfileobj(old, dst)
                    dst.write(b"\n\n<!-- merged on rotation -->\n\n")
                dst.write(payload.encode("utf-8"))
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(temp, archive_path)
        except Exception:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        self._discard_pending(pending)
        return archive_path

    def _read_last_timestamp(self) -> Optional[str]:
        try:
            with open(self.history_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except FileNotFoundError:
            return None
        matches = _HEADER_RE.findall(content)
        if not matches:
            return None
        return matches[-1][0]


# --- Reader ------------------------------------------------------------------

class HistoryReader:
    """Parses ``history.md`` and serves recency/since/tag queries."""

    def __init__(self, history_path: Optional[str] = None, archive_dir: Optional[str] = None):
        self.history_path = history_path or _memory_config.HISTORY_FILE
        self.archive_dir = archive_dir or os.path.join(os.path.dirname(self.history_path), "history")

    def read_all(self) -> List[HistoryEntry]:
        """The entries of ``history.md`` alone, in file order.

        Reads under the shared history lock (``_reading``).
        """
        if not os.path.exists(self.history_path):
            return []
        with _reading(self.history_path):
            return self._read_unlocked()

    def _read_unlocked(self) -> List[HistoryEntry]:
        """``read_all`` for a caller that already holds the history lock."""
        try:
            with open(self.history_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except FileNotFoundError:
            return []
        return self._parse(content)

    def _live_files(self) -> List[Tuple[str, str, Tuple[HistoryEntry, ...]]]:
        """``(path, sha256, entries)`` of a pending rotation and ``history.md``, under the shared lock."""
        pending = self.history_path + ".rotating"
        if not (os.path.exists(self.history_path) or os.path.exists(pending)):
            return []
        out = []
        with _reading(self.history_path):
            for path in (pending, self.history_path):
                parsed = parsed_file(path)
                if parsed is not None:
                    out.append((path, parsed[0], parsed[1]))
        return out

    @staticmethod
    def _older_file(machine: Optional[str], path: str):
        """``(sha256, entries)`` of an archive or segment; an unreadable segment is skipped, an archive is not.

        A segment's last block without its ``**Machine:**`` line was cut short by a crash or a full
        disk on the machine that writes it; it is left out until that machine writes it again whole.
        """
        if machine is None:
            return parsed_file(path)
        try:
            parsed = parsed_file(path)
        except OSError:
            return None
        if parsed is not None and parsed[1] and parsed[1][-1].machine is None:
            return parsed[0], parsed[1][:-1]
        return parsed

    def merged_files(self, machines: MachineHistory) -> List[Tuple[Optional[str], str, str, Tuple[HistoryEntry, ...]]]:
        """``(machine, path, sha256, entries)`` of every file of the merged history.

        ``history.md`` and a pending rotation are read under the shared history
        lock, the archives after it (rotation replaces or creates them whole, so
        an entry that moved meanwhile is read twice and deduplicated, never
        missed) and the segments without any history lock.
        """
        files = [(None, path, digest, entries) for path, digest, entries in self._live_files()]
        for _bound, _side, machine, path in _older_sources(self.archive_dir, machines):
            parsed = self._older_file(machine, path)
            if parsed is not None:
                files.append((machine, path, parsed[0], parsed[1]))
        return files

    def journal_entries(self) -> List[HistoryEntry]:
        """This checkout's whole journal (``history.md``, a pending rotation, ``history/*.md``), parsed once per change."""
        entries = [entry for _, _, items in self._live_files() for entry in items]
        for name in _archive_names(self.archive_dir):
            parsed = parsed_file(os.path.join(self.archive_dir, name))
            if parsed is not None:
                entries.extend(parsed[1])
        return entries

    def read_merged(self, machines: MachineHistory) -> List[HistoryEntry]:
        """Every entry of the merged history, oldest first; see ``merge_entries``."""
        return merge_entries((machine, entries) for machine, _, _, entries in self.merged_files(machines))

    def read_recent(
        self,
        limit: int = 20,
        since: Optional[str] = None,
        machine: Optional[str] = None,
    ) -> List[HistoryEntry]:
        """Newest-first list, optionally filtered by ``since`` (ISO timestamp prefix).

        With the user library sync set up for this repository, the list is the
        merged history (``history.md``, ``history/*.md`` and the synced
        segments), read newest month first until ``limit`` is filled; otherwise
        ``history.md`` alone. ``machine`` filters as ``_machine_filter`` says.
        """
        limit = max(1, limit) if limit > 0 else 20
        machines = _machine_history(self.history_path)
        keep = _machine_filter(machine, machines)
        if machines is None:
            entries = self.read_all()
        else:
            entries = self._recent_merged(machines, limit, since, keep)
        if keep is not None:
            entries = [e for e in entries if keep(e)]
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        if since:
            entries = [e for e in entries if e.timestamp >= since]
        return entries[:limit]

    def _recent_merged(self, machines: MachineHistory, limit: int, since: Optional[str], keep) -> List[HistoryEntry]:
        """The merged entries that can be among the ``limit`` newest kept ones.

        Months are read newest first; once ``limit`` kept entries are newer than
        everything an older file can hold, the older files are not read. (An
        entry in both this machine's journal and a newer segment may then show
        the segment's copy: the journal copy is older than every entry shown.)
        """
        chosen: Dict[str, HistoryEntry] = {}
        for _path, _digest, entries in self._live_files():
            _merge_into(chosen, None, entries)

        def enough(bound: str) -> bool:
            if since and bound <= since:
                return True
            times = sorted((e.timestamp for e in chosen.values() if (keep is None or keep(e))
                            and (not since or e.timestamp >= since)), reverse=True)
            return len(times) >= limit and bound <= times[limit - 1]

        sources = _older_sources(self.archive_dir, machines)
        index = 0
        while index < len(sources):
            bound = sources[index][0]
            if bound is not None and enough(bound):
                break
            while index < len(sources) and sources[index][0] == bound:
                _bound, _side, source_machine, path = sources[index]
                parsed = self._older_file(source_machine, path)
                if parsed is not None:
                    _merge_into(chosen, source_machine, parsed[1])
                index += 1
        return list(chosen.values())

    # ------------------------------------------------------------------ parser
    @staticmethod
    def _parse(content: str) -> List[HistoryEntry]:
        # Split content on heading boundaries while keeping the headings.
        positions = [m.start() for m in _HEADER_RE.finditer(content)]
        if not positions:
            return []
        positions.append(len(content))
        out: List[HistoryEntry] = []
        for i in range(len(positions) - 1):
            block = content[positions[i] : positions[i + 1]]
            entry = HistoryReader._parse_block(block)
            if entry:
                out.append(entry)
        return out

    @staticmethod
    def _parse_block(block: str) -> Optional[HistoryEntry]:
        lines = block.splitlines()
        if not lines:
            return None
        header = _HEADER_RE.match(lines[0])
        if not header:
            return None
        ts = header.group("ts")
        eid = header.group("id")
        fields: Dict[str, str] = {}
        for line in lines[1:]:
            m = _FIELD_RE.match(line)
            if m:
                fields[m.group("name").lower()] = m.group("value").strip()
        intent = fields.get("intent", "")
        action = fields.get("action", "")
        outcome = fields.get("outcome", "")
        files_raw = fields.get("files", "")
        files = [f.strip() for f in files_raw.split(",") if f.strip()] if files_raw else []
        tags_raw = fields.get("tags", "")
        tags = [t.strip() for t in tags_raw.split() if t.strip()] if tags_raw else []
        meta_raw = fields.get("meta", "")
        metadata = None
        if meta_raw:
            try:
                metadata = json.loads(meta_raw)
            except json.JSONDecodeError:
                metadata = {"_raw": meta_raw}
        return HistoryEntry(
            id=eid,
            timestamp=ts,
            intent=intent,
            action=action,
            outcome=outcome,
            files=files,
            tags=tags,
            metadata=metadata,
            # Only a synced segment carries it; a merged read sets it from the file instead.
            machine=fields.get("machine") or None,
        )


# --- Lazy semantic store -----------------------------------------------------

class HistoryStore:
    """Lazy semantic recall over history entries.

    Wrapping ``NumpyVectorStore`` so callers don't pay numpy/embedder import
    costs unless they actually run a semantic query. With the user library
    sync set up for the repository, the index covers the merged history of
    every machine (see ``HistoryReader.read_recent``).
    """

    def __init__(
        self,
        history_path: Optional[str] = None,
        data_dir: Optional[str] = None,
        store_name: str = HISTORY_VECTOR_STORE_NAME,
        archive_dir: Optional[str] = None,
    ):
        self.history_path = history_path or _memory_config.HISTORY_FILE
        self.data_dir = data_dir or _memory_config.MEMORY_DATA_DIR
        self.store_name = store_name
        self.archive_dir = archive_dir or os.path.join(os.path.dirname(self.history_path), "history")
        self._store = None  # lazy
        self._index_lock = threading.RLock()

    # ------------------------------------------------------------------ public
    def search(
        self,
        query: str,
        limit: int = 5,
        embed_query=None,
        embed_texts=None,
        machine: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return top-``limit`` entries with semantic distance.

        ``embed_query`` / ``embed_texts`` are injectable so tests can swap in
        deterministic fakes; production callers leave them None and the
        FastEmbed-backed defaults are loaded lazily. ``machine`` filters as in
        ``HistoryReader.read_recent``.
        """
        from src.engine.embedder import model_fingerprint
        with self._index_lock:
            machines = _machine_history(self.history_path)
            checked = model_fingerprint()
            store = self._ensure(machines, embed_texts)
            if store.count() == 0:
                return []
            if embed_query is None:
                from src.engine.embedder import embed_query as _eq
                embed_query = _eq
            vec = embed_query(query)
            if model_fingerprint() != checked:
                # Loading the model changed the fingerprint the index was
                # checked against: check it again for the loaded snapshot.
                store = self._ensure(machines, embed_texts)
                if store.count() == 0:
                    return []
            keep = _machine_filter(machine, machines)
            result = store.query(vec, n_results=store.count() if keep else limit)
            out: List[Dict[str, Any]] = []
            for i, eid in enumerate(result.ids):
                meta = result.metadatas[i] or {}
                if keep is not None and not keep(HistoryEntry(eid, "", "", "", "", machine=meta.get("machine"))):
                    continue
                out.append({
                    "id": eid,
                    "distance": float(result.distances[i]),
                    "document": result.documents[i],
                    "timestamp": meta.get("timestamp", ""),
                    "intent": meta.get("intent", ""),
                    "tags": meta.get("tags", []),
                    "machine": meta.get("machine"),
                })
                if len(out) >= limit:
                    break
            return out

    def ensure_index(self, embed_texts=None):
        """Build / refresh the vector index when its content fingerprint changes.

        The fingerprint is sha256(history.md) plus the embedding fingerprint of
        the loaded model (``src.engine.embedder.model_fingerprint``: model,
        revision, index schema, preprocessing, fastembed version), compared with
        ``.history_fingerprint`` in ``data_dir``; a missing index file also
        triggers a rebuild. For a merged history, the first part hashes every
        file of it instead (``HistoryReader.merged_files``).

        Thread-safe: serialized via ``_index_lock`` so concurrent
        ``read_history(query=...)`` calls don't race on rebuild.

        Returns the underlying ``NumpyVectorStore``.
        """
        with self._index_lock:
            return self._ensure(_machine_history(self.history_path), embed_texts)

    # ------------------------------------------------------------------ helpers
    def _ensure(self, machines: Optional[MachineHistory], embed_texts=None):
        with self._index_lock:
            from src.engine.vector_store import NumpyVectorStore  # heavy import — defer
            if self._store is None:
                self._store = NumpyVectorStore(name=self.store_name, data_dir=self.data_dir)
            if machines is not None:
                return self._ensure_merged(machines, embed_texts)

            # If the history file was deleted/moved, clear the store so semantic
            # search doesn't return stale entries.
            if not os.path.exists(self.history_path):
                if self._store.count() > 0:
                    self._store.clear()
                    self._store.save()
                return self._store

            from src.engine.embedder import model_fingerprint
            with _reading(self.history_path):
                # Content-based invalidation catches edits that preserve mtimes.
                try:
                    with open(self.history_path, "rb") as source:
                        data = source.read()
                except FileNotFoundError:
                    data = b""
            digest = hashlib.sha256(data).hexdigest() + ":" + model_fingerprint()
            # Embedding runs under the index's own lock, never the history lock:
            # appends and readers do not wait for the model.
            with file_lock(os.path.join(self.data_dir, f".{self.store_name}.lock")):
                self._refresh(digest, embed_texts, load=lambda: HistoryReader._parse(data.decode("utf-8")))
            return self._store

    def _ensure_merged(self, machines: MachineHistory, embed_texts=None):
        """The index of the merged history; embeds only entries it has not embedded yet.

        The files are read once (``HistoryReader.merged_files``; unchanged ones
        come from the parse cache) and the shared history lock is released
        before any embedding, so appends and readers never wait for the model;
        the digest and the entries come from the same reading.
        """
        files = HistoryReader(self.history_path, self.archive_dir).merged_files(machines)
        if not files:
            if self._store.count() > 0:
                self._store.clear()
                self._store.save()
            # Content that comes back must be indexed again, not matched with an empty store.
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(self.data_dir, ".history_fingerprint"))
            return self._store
        from src.engine.embedder import model_fingerprint
        digest = hashlib.sha256(b"merged history\0")
        for machine, path, sha, _entries in files:
            digest.update(f"{machine or ''}\0{os.path.basename(path)}\0{sha}\0".encode("utf-8"))
        # Rebuilds of one index from several processes take turns.
        with file_lock(os.path.join(self.data_dir, f".{self.store_name}.lock")):
            self._refresh(digest.hexdigest() + ":" + model_fingerprint(), embed_texts,
                          load=lambda: merge_entries((machine, entries) for machine, _, _, entries in files))
        return self._store

    def _refresh(self, digest: str, embed_texts=None, load=None) -> None:
        """Rebuild the index unless ``.history_fingerprint`` already vouches for ``digest``.

        ``load`` returns the entries to index (default: those of ``history.md``);
        it runs only when a rebuild is needed.
        """
        from src.engine.vector_store import NumpyVectorStore
        from src.daemon.state import atomic_private
        marker = os.path.join(self.data_dir, ".history_fingerprint")
        try:
            with open(marker) as stream: saved = stream.read()
        except FileNotFoundError:
            saved = None
        if saved != digest or not os.path.exists(os.path.join(self.data_dir, f"{self.store_name}.npz")):
            # Stored vectors stay valid only for the embedding
            # configuration that produced them.
            reuse = saved is not None and saved.partition(":")[2] == digest.partition(":")[2]
            if reuse:
                # Reuse the vectors this marker describes: another
                # process may have rewritten the store since it loaded.
                self._store = NumpyVectorStore(name=self.store_name, data_dir=self.data_dir)
            if saved is not None:
                # A rebuild that saves the store but dies before the
                # new marker must not leave the old marker vouching
                # for vectors from another configuration.
                with contextlib.suppress(FileNotFoundError):
                    os.remove(marker)
            self._rebuild(embed_texts=embed_texts, reuse_vectors=reuse, entries=load() if load else None)
            atomic_private(marker, digest)

    def _rebuild(self, embed_texts=None, reuse_vectors: bool = False,
                 entries: Optional[List[HistoryEntry]] = None) -> None:
        """Replace the index with every current entry.

        ``entries`` defaults to those of ``history.md``; the callers pass the
        entries they hashed. With *reuse_vectors*, an entry
        whose id and formatted document are already stored keeps its vector,
        so only new or edited entries are embedded; the caller passes it only
        when the stored vectors come from the current embedding fingerprint.
        """
        if entries is None:
            entries = HistoryReader(self.history_path).read_all()
        if not entries:
            self._store.clear()
            self._store.save()
            return

        if embed_texts is None:
            from src.engine.embedder import embed_texts as _et
            embed_texts = _et

        # Deduplicate by id — entries outside the dedup_tail window can share
        # the same content hash. Keep the latest (last) entry for each id.
        seen: dict[str, int] = {}
        for i, e in enumerate(entries):
            seen[e.id] = i
        unique_indices = sorted(seen.values())
        entries = [entries[i] for i in unique_indices]

        import numpy as np  # heavy import — defer

        ids = [e.id for e in entries]
        documents = [self._format_for_embedding(e) for e in entries]
        vectors = self._reusable_vectors(dict(zip(ids, documents))) if reuse_vectors else {}
        missing = [i for i, id_ in enumerate(ids) if id_ not in vectors]
        if missing:
            fresh = list(embed_texts([documents[i] for i in missing]))
            if len(fresh) != len(missing):
                raise ValueError(f"Embedder returned {len(fresh)} vectors for {len(missing)} history entries")
            vectors.update((ids[i], vector) for i, vector in zip(missing, fresh))
        embeddings = np.stack([np.asarray(vectors[id_], dtype=np.float32) for id_ in ids])

        metadatas = [
            {
                "timestamp": e.timestamp,
                "intent": e.intent,
                "tags": e.tags,
                "files": e.files,
                "machine": e.machine,
            }
            for e in entries
        ]
        self._store.replace(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
        self._store.save()

    def _reusable_vectors(self, documents: dict[str, str]) -> dict:
        """Map each id whose stored document equals ``documents[id]`` to its stored vector."""
        stored = self._store.get(list(documents))
        unchanged = [id_ for id_, document in zip(stored.ids, stored.documents)
                     if document == documents[id_]]
        return self._store.get_embeddings(unchanged)

    @staticmethod
    def _format_for_embedding(entry: HistoryEntry) -> str:
        parts = [
            f"Intent: {entry.intent}",
            f"Action: {entry.action}",
            f"Outcome: {entry.outcome}",
        ]
        if entry.tags:
            parts.append("Tags: " + " ".join(entry.tags))
        return "\n".join(parts)


__all__ = [
    "HistoryEntry",
    "HistoryReader",
    "HistoryStore",
    "HistoryWriter",
    "MachineHistory",
    "entry_blocks",
    "journal_blocks",
    "merge_entries",
    "parsed_file",
    "set_sync",
]
