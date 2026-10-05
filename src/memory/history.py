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
startup), each appended entry is also offered to the sync, and reads merge this
machine's journal (``history.md`` and ``history/*.md``) with the other
machines' entries for the same repository (``src/user_sync/history.py``).
This module never imports the sync itself.
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
import threading
from dataclasses import asdict, dataclass, field
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

    ``machine`` is the label of the machine that wrote it: None for this
    machine's own entries, set for other machines' entries in a merged read.
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


# --- Sync with the user's other machines (optional) -------------------------

@dataclass(frozen=True)
class MachineHistory:
    """Other machines' history of one repository, as the user library sync finds it.

    ``label`` names this machine; ``files`` holds ``(label, path)`` of the other
    machines' history segments for the same repository.
    """
    label: str
    files: Tuple[Tuple[str, str], ...] = ()


_sync = None


def set_sync(integration):
    """Install the user library sync integration, or remove it with None; returns the previous one.

    ``integration.exported(history_path, block)`` is called after each append,
    once every history lock is released; ``integration.machines(history_path)``
    returns a ``MachineHistory``, or None while the repository's history does
    not take part in sync. Without an integration the history is this
    machine's journal only. ``src.user_sync.history.install`` sets it.
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


def _exported(history_path: str, block: str) -> None:
    """Offer an appended entry to the sync; its failure never fails the append."""
    integration = _sync
    if integration is None:
        return
    try:
        integration.exported(history_path, block)
    except Exception:
        logger.warning("could not share a history entry of %s", history_path, exc_info=True)


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


def history_files(
    history_path: str,
    archive_dir: Optional[str] = None,
    machines: Optional[MachineHistory] = None,
) -> List[Tuple[Optional[str], str, str]]:
    """``(machine, path, text)`` of every file of the merged history.

    This machine's files come first, with machine None: the monthly archives,
    a rotation an interruption left pending, then ``history.md``; they are read
    under a shared history lock, so a concurrent rotation is never seen half
    way. The caller must not hold that lock. Other machines' segments follow
    in the order of ``machines.files``; they need no lock, because the sync
    replaces whole files. Missing or unreadable segments are skipped.
    """
    archive_dir = archive_dir or os.path.join(os.path.dirname(history_path), "history")
    pending = history_path + ".rotating"
    out: List[Tuple[Optional[str], str, str]] = []
    # A repository without any history gets no lock file from a read.
    if os.path.exists(history_path) or os.path.exists(pending) or _archive_names(archive_dir):
        with _reading(history_path):
            names = _archive_names(archive_dir)  # again under the lock: rotation may have added one
            for path in [os.path.join(archive_dir, name) for name in names] + [pending, history_path]:
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                    continue
                if text:
                    out.append((None, path, text))
    for label, path in (machines.files if machines else ()):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if text:
            out.append((label, path, text))
    return out


def merge_entries(files) -> List[HistoryEntry]:
    """A union by entry id of ``(machine, path, text)`` files, oldest first.

    This machine's copy (machine None) wins over another machine's; between
    copies from the same side, the later one wins.
    """
    chosen: Dict[str, HistoryEntry] = {}
    for machine, _path, text in files:
        for entry in HistoryReader._parse(text):
            entry.machine = machine
            current = chosen.get(entry.id)
            if current is None:
                chosen[entry.id] = entry
            elif (current.machine is None) != (entry.machine is None):
                if entry.machine is None:
                    chosen[entry.id] = entry
            elif entry.timestamp > current.timestamp:
                chosen[entry.id] = entry
    return sorted(chosen.values(), key=lambda e: (e.timestamp, e.id))


def _wanted_machine(machine: Optional[str], machines: Optional[MachineHistory]):
    """``(filter on, wanted machine value)``: ``local`` and this machine's label mean None."""
    machine = (machine or "").strip()
    if not machine:
        return False, None
    own = machines.label if machines else None
    return True, None if machine in ("local", own) else machine


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

        # Outside every history lock: the sync takes the library lock, which a
        # sync run holds for its local steps, and must never wait while holding ours.
        _exported(self.history_path, block)

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

    def read_merged(self, machines: MachineHistory) -> List[HistoryEntry]:
        """This machine's journal with its archives, and the other machines' entries; see ``merge_entries``."""
        return merge_entries(history_files(self.history_path, self.archive_dir, machines))

    def read_recent(
        self,
        limit: int = 20,
        since: Optional[str] = None,
        machine: Optional[str] = None,
    ) -> List[HistoryEntry]:
        """Newest-first list, optionally filtered by ``since`` (ISO timestamp prefix).

        With the user library sync set up for this repository, the list is the
        merged history of every machine; otherwise ``history.md`` alone.
        ``machine`` keeps only the entries of one machine label, or with
        ``local`` (or this machine's label) only this machine's own entries.
        """
        limit = max(1, limit) if limit > 0 else 20
        machines = _machine_history(self.history_path)
        entries = self.read_all() if machines is None else self.read_merged(machines)
        active, wanted = _wanted_machine(machine, machines)
        if active:
            entries = [e for e in entries if e.machine == wanted]
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        if since:
            entries = [e for e in entries if e.timestamp >= since]
        return entries[:limit]

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
            active, wanted = _wanted_machine(machine, machines)
            result = store.query(vec, n_results=store.count() if active else limit)
            out: List[Dict[str, Any]] = []
            for i, eid in enumerate(result.ids):
                meta = result.metadatas[i] or {}
                if active and meta.get("machine") != wanted:
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
        file of it instead (``history_files``).

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
            with file_lock(_sidecar(self.history_path)):
                # Content-based invalidation catches edits that preserve mtimes.
                with open(self.history_path, "rb") as source:
                    digest = hashlib.sha256(source.read()).hexdigest() + ":" + model_fingerprint()
                self._refresh(digest, embed_texts)
            return self._store

    def _ensure_merged(self, machines: MachineHistory, embed_texts=None):
        """The index of the merged history; embeds only entries it has not embedded yet.

        The files are read once, under a shared history lock that is released
        before any embedding, so appends never wait for the model; the digest
        and the entries come from the same reading.
        """
        files = history_files(self.history_path, self.archive_dir, machines)
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
        for machine, path, text in files:
            digest.update(f"{machine or ''}\0{os.path.basename(path)}\0".encode("utf-8"))
            digest.update(hashlib.sha256(text.encode("utf-8")).digest())
        # Rebuilds of one index from several processes take turns, as under the history lock.
        with file_lock(os.path.join(self.data_dir, f".{self.store_name}.lock")):
            self._refresh(digest.hexdigest() + ":" + model_fingerprint(), embed_texts,
                          load=lambda: merge_entries(files))
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

        ``entries`` defaults to those of ``history.md``; a merged history
        passes its own, already one per id. With *reuse_vectors*, an entry
        whose id and formatted document are already stored keeps its vector,
        so only new or edited entries are embedded; the caller passes it only
        when the stored vectors come from the current embedding fingerprint.
        """
        if entries is None:  # the caller holds the history lock
            entries = HistoryReader(self.history_path)._read_unlocked()
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
    "history_files",
    "merge_entries",
    "set_sync",
]
