"""Append-only intent/action/outcome history with optional semantic recall.

Three classes:

* ``HistoryWriter`` — stdlib-only. Writes deduplicated, content-hashed entries
  to ``history.md`` under an exclusive file lock. Rotates to
  ``history/YYYY-MM.md`` when the file grows past
  ``HISTORY_ROTATION_THRESHOLD_KB``.

* ``HistoryReader`` — stdlib-only. Parses entries from disk and serves
  recency / ``since`` queries.

* ``HistoryStore`` — lazy ``NumpyVectorStore`` wrapper. Built only on the
  first ``read_history(query=...)`` call; rebuilds when the markdown file
  is newer than the persisted store. The embedder is imported lazily so
  ``HistoryWriter``/``HistoryReader`` users never pay the numpy cost.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from src.memory.config import (
    HISTORY_ARCHIVE_DIR,
    HISTORY_DEDUP_TAIL_SIZE,
    HISTORY_FILE,
    HISTORY_FORMAT_VERSION,
    HISTORY_ROTATION_THRESHOLD_KB,
    HISTORY_VECTOR_STORE_NAME,
    MEMORY_DATA_DIR,
)

logger = logging.getLogger(__name__)


# Cross-platform file lock — fcntl on POSIX, no-op shim on Windows where the
# MCP server does not support concurrent stdio sessions anyway.
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


_HEADER_RE = re.compile(
    r"^##\s+(?P<ts>\S+)\s+\|\s+(?P<id>[0-9a-f]{12})\s*$",
    re.MULTILINE,
)
_FIELD_RE = re.compile(r"^\*\*(?P<name>[A-Za-z]+):\*\*\s*(?P<value>.*)$")


@dataclass
class HistoryEntry:
    """In-memory representation of a single history entry."""
    id: str
    timestamp: str
    intent: str
    action: str
    outcome: str
    files: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
        self.history_path = history_path or HISTORY_FILE
        self.archive_dir = archive_dir or HISTORY_ARCHIVE_DIR
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
    ) -> Dict[str, Any]:
        """Append a single entry; returns a JSON-friendly status dict."""
        intent = (intent or "").strip()
        action = (action or "").strip()
        outcome = (outcome or "").strip()
        if not (intent and action and outcome):
            return {
                "status": "error",
                "message": "intent, action, and outcome are all required",
            }

        entry_id = self._compute_entry_hash(intent, action, outcome)

        os.makedirs(os.path.dirname(self.history_path) or ".", exist_ok=True)

        # Open in append+read mode so we can scan the tail under the lock.
        # Every write + header + dedup + rotation happens INSIDE the lock so
        # concurrent writers (cross-process: e.g. Claude Desktop + VS Code
        # attached to the same repo) cannot interleave and corrupt the file.
        rotated_to: Optional[str] = None
        with open(self.history_path, "a+", encoding="utf-8", newline="") as fh:
            try:
                _lock_exclusive(fh)

                # Re-check size under the lock — a concurrent writer may have
                # created the file between our os.makedirs and open().
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(self._render_header())
                    fh.flush()

                if self._is_duplicate(entry_id):
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

                # Rotate inside the lock — if we cross the threshold mid-write,
                # the move/merge must not race a second writer.
                rotated_to = self._maybe_rotate_locked()
            finally:
                _unlock(fh)

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
        lines = [
            "",  # blank separator before the heading
            f"## {timestamp} | {entry_id}",
            f"**Intent:** {intent}",
            f"**Action:** {action}",
            f"**Outcome:** {outcome}",
        ]
        if files:
            lines.append(f"**Files:** {', '.join(files)}")
        if tags:
            normalized = [t if t.startswith("#") else f"#{t}" for t in tags]
            lines.append(f"**Tags:** {' '.join(normalized)}")
        if metadata:
            lines.append(f"**Meta:** {json.dumps(metadata, ensure_ascii=False, sort_keys=True)}")
        lines.append("")  # trailing newline
        return "\n".join(lines) + "\n"

    def _is_duplicate(self, entry_id: str) -> bool:
        """Tail-scan the last ``self.dedup_tail`` entry IDs."""
        try:
            with open(self.history_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except FileNotFoundError:
            return False
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
            with open(self.history_path, "r", encoding="utf-8") as src:
                payload = src.read()
            with open(archive_path, "a", encoding="utf-8") as dst:
                dst.write("\n\n<!-- merged on rotation -->\n\n")
                dst.write(payload)
            os.unlink(self.history_path)
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

    def __init__(self, history_path: Optional[str] = None):
        self.history_path = history_path or HISTORY_FILE

    def read_all(self) -> List[HistoryEntry]:
        if not os.path.exists(self.history_path):
            return []
        with open(self.history_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        return self._parse(content)

    def read_recent(
        self,
        limit: int = 20,
        since: Optional[str] = None,
    ) -> List[HistoryEntry]:
        """Newest-first list, optionally filtered by ``since`` (ISO timestamp prefix)."""
        entries = self.read_all()
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
        )


# --- Lazy semantic store -----------------------------------------------------

class HistoryStore:
    """Lazy semantic recall over history entries.

    Wrapping ``NumpyVectorStore`` so callers don't pay numpy/embedder import
    costs unless they actually run a semantic query.
    """

    def __init__(
        self,
        history_path: Optional[str] = None,
        data_dir: Optional[str] = None,
        store_name: str = HISTORY_VECTOR_STORE_NAME,
    ):
        self.history_path = history_path or HISTORY_FILE
        self.data_dir = data_dir or MEMORY_DATA_DIR
        self.store_name = store_name
        self._store = None  # lazy

    # ------------------------------------------------------------------ public
    def search(
        self,
        query: str,
        limit: int = 5,
        embed_query=None,
        embed_texts=None,
    ) -> List[Dict[str, Any]]:
        """Return top-``limit`` entries with semantic distance.

        ``embed_query`` / ``embed_texts`` are injectable so tests can swap in
        deterministic fakes; production callers leave them None and the
        FastEmbed-backed defaults are loaded lazily.
        """
        store = self.ensure_index(embed_texts=embed_texts)
        if store.count() == 0:
            return []
        if embed_query is None:
            from src.engine.embedder import embed_query as _eq
            embed_query = _eq

        vec = embed_query(query)
        result = store.query(vec, n_results=limit)
        out: List[Dict[str, Any]] = []
        for i, eid in enumerate(result.ids):
            meta = result.metadatas[i] or {}
            out.append({
                "id": eid,
                "distance": float(result.distances[i]),
                "document": result.documents[i],
                "timestamp": meta.get("timestamp", ""),
                "intent": meta.get("intent", ""),
                "tags": meta.get("tags", []),
            })
        return out

    def ensure_index(self, embed_texts=None):
        """Build / refresh the vector index if the markdown file is newer.

        Returns the underlying ``NumpyVectorStore``.
        """
        from src.engine.vector_store import NumpyVectorStore  # heavy import — defer
        if self._store is None:
            self._store = NumpyVectorStore(name=self.store_name, data_dir=self.data_dir)

        # If the history file was deleted/moved, clear the store so semantic
        # search doesn't return stale entries.
        if not os.path.exists(self.history_path):
            if self._store.count() > 0:
                self._store.clear()
                self._store.save()
            return self._store

        # Refresh if file is newer than the store's npz.
        npz_path = os.path.join(self.data_dir, f"{self.store_name}.npz")
        history_mtime = os.path.getmtime(self.history_path)
        store_mtime = os.path.getmtime(npz_path) if os.path.exists(npz_path) else 0
        if history_mtime <= store_mtime and self._store.count() > 0:
            return self._store

        self._rebuild(embed_texts=embed_texts)
        return self._store

    # ------------------------------------------------------------------ helpers
    def _rebuild(self, embed_texts=None) -> None:
        reader = HistoryReader(self.history_path)
        entries = reader.read_all()
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

        documents = [self._format_for_embedding(e) for e in entries]
        embeddings = embed_texts(documents)

        ids = [e.id for e in entries]
        metadatas = [
            {
                "timestamp": e.timestamp,
                "intent": e.intent,
                "tags": e.tags,
                "files": e.files,
            }
            for e in entries
        ]
        self._store.replace(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
        self._store.save()

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
]
