"""Shared memory across AI clients (#369): what each client keeps in its native memory, for all.

Each AI client keeps what it learned about the user and the project in its own place and format
(``src/memory/native.py``). This module reads those places on this machine, read-only, and serves
them to every client through ``read_shared_memory``:

1. **Read.** Every item file of every discovered memory directory is a candidate. A file is skipped,
   and only counted, when it is Agents-Core's own routing reminder, when its name marks key material,
   when it is not a regular UTF-8 text file under ``MAX_FACT_BYTES``, or when the user sync's credential
   scan (``src.user_sync.scope.scan``) finds a secret in it. Nothing else reads its bytes.
2. **Facts.** One fact per distinct content: identical bytes in two places are one fact with two
   sources. A source names the client, its configuration directory, the project (Claude Code's project
   directory name; empty outside projects, and always for Codex) and the file.
3. **Index.** Facts are split at Markdown headings into chunks. A lexical BM25 index over the chunks
   answers at once. Their vectors (the installation's embedding model) live in a ``NumpyVectorStore``
   under the installation data directory and are built by a background thread in batches of
   ``EMBED_BATCH``, so
   a query never waits for the build (compare #582); until it finishes, a query is lexical for the
   chunks without vectors and says how far the build got. Unchanged chunks keep their vectors, a
   model change drops them, and a cross-process lock keeps two servers from building at once.
4. **Serve.** A query fuses the lexical and semantic ranks (reciprocal rank fusion) per fact. A
   result carries ``similar``: facts from another client or project whose nearest chunk is at least
   ``SIMILAR_MIN`` cosine-similar. Nothing is merged or dropped: two clients that disagree both show.

Scope: ``project`` (the default) keeps the facts of the caller's project and the user-level ones,
``user`` only the user-level ones, ``all`` every project's.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from src.memory import native
from src.memory.history_results import preview, to_json
from src.result_size import INLINE_LIMIT, shown_size
from src.utils.prompt_loader import split_frontmatter

logger = logging.getLogger(__name__)

# Agents-Core's own routing reminder in a Claude configuration directory's top-level memory
# (scripts/_helpers/migrate_routing_memory.py): an instruction every client already gets, not a fact.
MANAGED_ITEMS = frozenset({"feedback_agents_core_routing.md"})
# Item names that hold key material whatever their content (Codex items may have any name).
SECRET_NAME = re.compile(r"(?i)^(?:\.env(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\..*)?|.*\.(?:pem|key|p12|pfx|kdbx|keychain))$")
# Credentials the way notes write them, on top of the user sync's scan, which looks for the forms
# configuration files use. A labelled value counts only when it looks like one: at least six
# characters without spaces, with a letter and a digit or symbol, so prose such as "the token is
# rotated daily" stays a fact.
MEMORY_SECRETS = (
    ("labelled_credential", re.compile(
        r"(?i)(?<![\w-])(?:password|passwd|passphrase|пароль|secret|token|api[_ -]?key|access[_ -]?key|"
        r"client[_ -]?secret)(?![\w-])[\"'`*]*\s*(?::|=|\bis\b|—|-)\s*[\"'`]?"
        r"(?=[^\s\"'`<>]*[^\W\d_])(?=[^\s\"'`<>]*[\d!@#%^&*_+=~])(?!\$\{|\{\{)[^\s\"'`<>]{6,}")),
    ("url_credential", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}")),
)
MAX_FACT_BYTES = native.MAX_SHOW
CHUNK_CHARS = 1500
STORE_NAME = "shared_memory_store"
LOCK_NAME = ".shared_memory.lock"  # one builder at a time
SAVE_LOCK_NAME = ".shared_memory.save.lock"  # a save excludes reloads, which share it
# Chunks per embedding call. In the shared service every embedding goes through one inference worker,
# so a persona routing call waits for at most one batch: two chunks are a few seconds on a small CPU.
EMBED_BATCH = 2
SAVE_EVERY_SECONDS = 60.0
RRF_K = 60
# Cosine similarity at which a fact of another client or project counts as similar. Calibrated on
# 2026-11-18 on 105 real Claude Code notes (first chunk each) with harrier-oss-v1-270m: half of all
# pairs are below 0.53, 99% below 0.71, and the closest pair reaches 0.81. 0.78 flags about the dozen
# closest pairs, such as the notes on one logging extension kept in two projects. Another model has
# another scale (docs/shared-memory.md).
SIMILAR_MIN = 0.78
SCOPES = ("project", "user", "all")
CLIENTS = ("claude", "codex")
FACT_ID = re.compile(r"[0-9a-f]{12}")

RESULT_BUDGET = INLINE_LIMIT - 5_000
PREVIEW_CHARS = 600
MIN_PREVIEW_CHARS = 100
FIELD_CHARS = 300  # a fact's name and description in a listing
TITLE_CHARS = 200  # the name and description that start each chunk
MAX_SOURCES = 10  # sources listed per fact; sources_total gives the count beyond

_TOKEN = re.compile(r"\w+", re.UNICODE)
_HEADING = re.compile(r"^#{1,6}\s")
_STEM = 6  # tokens longer than this are cut to it: a crude stem for inflected languages such as Russian


# --- facts ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    client: str
    store: str  # the configuration directory's label, e.g. ``~/.claude``
    project: str  # Claude Code's project directory name; "" outside projects and for Codex
    file: str
    path: str  # where the file is, under the store's label
    mtime: float = 0.0  # the file's modification time; not shown

    def to_dict(self) -> dict:
        return {"client": self.client, "store": self.store, "project": self.project, "file": self.file,
                "path": self.path}


@dataclass
class Fact:
    id: str
    sha256: str
    text: str
    name: Optional[str]
    description: Optional[str]
    type: Optional[str]
    timestamp: float  # its frontmatter ``modified``, else the newest file time
    sources: List[Source] = field(default_factory=list)
    dated: bool = False  # the timestamp comes from the frontmatter

    @property
    def scope(self) -> str:
        return "project" if any(source.project for source in self.sources) else "user"

    @property
    def modified(self) -> Optional[str]:
        try:
            return datetime.fromtimestamp(self.timestamp, timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None

    def summary(self) -> dict:
        """The fact without its text; long fields are cut so one fact always fits a listing."""
        out = {"id": self.id, "name": _cut(self.name, FIELD_CHARS), "description": _cut(self.description, FIELD_CHARS),
               "type": _cut(self.type, 100), "modified": self.modified, "scope": self.scope,
               "sources": [source.to_dict() for source in self.sources[:MAX_SOURCES]]}
        if len(self.sources) > MAX_SOURCES:
            out["sources_total"] = len(self.sources)
        return out


def _cut(text: Optional[str], limit: int) -> Optional[str]:
    return preview(text, limit) if text is not None and len(text) > limit else text


@dataclass
class Collection:
    facts: Dict[str, Fact]
    skipped: Counter  # by reason
    skipped_at: Counter = field(default_factory=Counter)  # by (project, reason), to report only what a scope shows


# Verdicts (skip reason, or the frontmatter fields) by content hash: a file is read again on every
# call, which no file time can fool, and only the scan and the parse are remembered.
_verdicts: Dict[str, tuple] = {}
_verdicts_lock = threading.Lock()


def _read_item(store: native.Store, path: Path) -> tuple:
    """``("ok", bytes, mtime, sha256, frontmatter fields)`` or ``(reason,)`` for a file that is skipped."""
    try:
        with native.open_in_store(store, path) as stream:
            info = os.fstat(stream.fileno())
            if info.st_size > MAX_FACT_BYTES:
                return ("too_large",)
            data = stream.read(MAX_FACT_BYTES + 1)
    except native.Refused:
        return ("refused",)
    except OSError:
        return ("unreadable",)
    if len(data) > MAX_FACT_BYTES:
        return ("too_large",)
    digest = hashlib.sha256(data).hexdigest()
    with _verdicts_lock:
        verdict = _verdicts.get(digest)
    if verdict is None:
        verdict = _verdict(data)
        with _verdicts_lock:
            if len(_verdicts) > 10_000:
                _verdicts.clear()
            _verdicts[digest] = verdict
    if verdict[0] != "ok":
        return verdict
    return ("ok", data, info.st_mtime, digest, verdict[1])


def _verdict(data: bytes) -> tuple:
    """``("ok", frontmatter fields)``, or ``(reason,)`` when the content must not be served."""
    if b"\0" in data:
        return ("not_text",)
    from src.user_sync import scope as sync_scope
    if sync_scope.scan(data):
        return ("secret",)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ("not_text",)
    if any(pattern.search(text) for _, pattern in MEMORY_SECRETS):
        return ("secret",)
    meta, _ = native.frontmatter(text)
    return ("ok", native.fields(meta))


def collect(stores: Iterable[native.Store]) -> Collection:
    """The facts of every discovered memory directory, and what was skipped, by reason.

    A directory this account may not list is counted against its own project and the walk goes
    on. Entries that are no regular file (a link, a junction, a FIFO) are counted as refused
    without being opened."""
    collection = Collection({}, Counter())

    def skip(project: str, reason: str) -> None:
        collection.skipped[reason] += 1
        collection.skipped_at[(project, reason)] += 1

    def listed(store: native.Store, memory: Path) -> list:
        return native.item_names(store, memory) or native.refused_names(store, memory)

    for store in stores:
        for project, memory in native.places(store, names=listed, every_dir=True,
                                             on_error=lambda project: skip(project, "unreadable")):
            try:
                names = native.item_names(store, memory)
                refused = native.refused_names(store, memory)
            except OSError:
                skip(project, "unreadable")
                continue
            for _ in refused:
                skip(project, "refused")
            for name in names:
                try:
                    reason = _add(collection.facts, store, project, memory, name)
                except (OSError, ValueError, OverflowError, RecursionError):
                    reason = "unreadable"
                if reason:
                    skip(project, reason)
    return collection


def _add(facts: Dict[str, Fact], store: native.Store, project: str, memory: Path, name: str) -> Optional[str]:
    """Adds the item ``name`` to ``facts``; the reason it was skipped, or None."""
    if name in MANAGED_ITEMS:
        return "managed"
    if SECRET_NAME.match(name):
        return "secret"
    result = _read_item(store, memory / name)
    if result[0] != "ok":
        return result[0]
    _, data, mtime, digest, found = result
    relative = memory.relative_to(store.path).as_posix()
    source = Source(store.client, store.label, project, name, f"{store.label}/{relative}/{name}", mtime)
    fact = facts.get(digest[:12])
    if fact is None:
        dated = native.timestamp_of(found["modified"])
        facts[digest[:12]] = Fact(digest[:12], digest, data.decode("utf-8"), found["name"], found["description"],
                                  found["type"], dated if dated is not None else mtime, [source], dated is not None)
    else:
        fact.sources.append(source)
        if not fact.dated:
            fact.timestamp = max(fact.timestamp, mtime)
    return None


def discover() -> List[native.Store]:
    """This machine's memory stores; the service state directory records more configuration directories."""
    from src.daemon.state import state_dir
    try:
        directory = state_dir()
        directory = directory if directory.is_dir() else None
    except (OSError, ValueError):
        directory = None
    return native.stores(directory)


def project_keys(root: Optional[Path]) -> set:
    """Claude Code's project directory names for the workspace at ``root``: its own and, for a git
    worktree, its main checkout's, which Claude Code uses for every worktree of a repository."""
    if root is None:
        return set()
    keys = {native.project_name(root)}
    marker = Path(root) / ".git"
    try:
        if marker.is_file():
            line = marker.read_text(encoding="utf-8", errors="replace").strip()
            if line.startswith("gitdir:"):
                gitdir = Path(line[len("gitdir:"):].strip())
                if not gitdir.is_absolute():
                    gitdir = (Path(root) / gitdir).resolve()
                if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                    keys.add(native.project_name(gitdir.parent.parent.parent))
    except OSError:
        pass
    return keys


def select(facts: Iterable[Fact], *, scope: str, projects: set, client: Optional[str] = None,
           memory_type: Optional[str] = None) -> List[Fact]:
    """The facts a caller in ``projects`` may see under ``scope``, newest first, each with only the
    sources it may see: a copy of a fact in another project's memory stays unnamed, and an undated
    fact's time comes from the visible copies only."""
    chosen = []
    for fact in facts:
        sources = [source for source in fact.sources
                   if (client is None or source.client == client) and shows(source.project, scope, projects)]
        if not sources:
            continue
        if memory_type is not None and (fact.type or "").lower() != memory_type.lower():
            continue
        moment = fact.timestamp if fact.dated else max(source.mtime for source in sources)
        chosen.append(Fact(fact.id, fact.sha256, fact.text, fact.name, fact.description, fact.type,
                           moment, sources, fact.dated))
    chosen.sort(key=lambda fact: (-fact.timestamp, fact.id))
    return chosen


# Windows and macOS file systems usually ignore case, and a workspace root can arrive spelled with
# another case than the one Claude Code named its project directory after.
_FOLD_CASE = os.name == "nt" or sys.platform == "darwin"


def shows(project: str, scope: str, projects: set) -> bool:
    """A place of ``project`` ("" for user-level memory) shows under ``scope`` to a caller in ``projects``."""
    if scope == "all" or not project:
        return True
    if scope != "project":
        return False
    if _FOLD_CASE:
        return project.casefold() in {key.casefold() for key in projects}
    return project in projects


# --- chunks and lexical search ----------------------------------------------------------------------

def chunks(fact: Fact) -> List[Tuple[str, str]]:
    """``(chunk id, text)`` of a fact: its body split at Markdown headings, then into pieces of at
    most ``CHUNK_CHARS`` at paragraph ends. Each chunk starts with the fact's name, for context."""
    _, body = split_frontmatter(fact.text)
    body = body if body is not None else fact.text
    sections, current = [], []
    for line in body.splitlines():
        if _HEADING.match(line) and current:
            sections.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("\n".join(current))
    pieces = []
    for section in sections:
        piece = ""
        for paragraph in re.split(r"\n\s*\n", section):
            paragraph = paragraph.strip()
            while len(paragraph) > CHUNK_CHARS:
                if piece:
                    pieces.append(piece)
                    piece = ""
                pieces.append(paragraph[:CHUNK_CHARS])
                paragraph = paragraph[CHUNK_CHARS:]
            if not paragraph:
                continue
            if piece and len(piece) + len(paragraph) + 2 > CHUNK_CHARS:
                pieces.append(piece)
                piece = paragraph
            else:
                piece = f"{piece}\n\n{paragraph}" if piece else paragraph
        if piece:
            pieces.append(piece)
    title = _cut(" — ".join(filter(None, (fact.name, fact.description))), TITLE_CHARS) or ""
    if not pieces:
        pieces = [""]
    return [(f"{fact.id}:{number}", f"{title}\n{piece}".strip() if title else piece)
            for number, piece in enumerate(pieces)]


def tokens(text: str) -> List[str]:
    return [token[:_STEM] for token in _TOKEN.findall(text.casefold()) if len(token) > 1]


def bm25(query: str, documents: Dict[str, List[str]], k1: float = 1.2, b: float = 0.75) -> Dict[str, float]:
    """BM25 scores of the documents that share a term with ``query``."""
    terms = set(tokens(query))
    if not terms or not documents:
        return {}
    average = sum(len(words) for words in documents.values()) / len(documents) or 1.0
    frequency = Counter(term for words in documents.values() for term in set(words) if term in terms)
    scores = {}
    for chunk_id, words in documents.items():
        counts = Counter(word for word in words if word in terms)
        if not counts:
            continue
        score = 0.0
        for term, count in counts.items():
            idf = math.log(1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * count * (k1 + 1) / (count + k1 * (1 - b + b * len(words) / average))
        scores[chunk_id] = score
    return scores


# --- the index ----------------------------------------------------------------------------------------

def _default_embed_texts(texts: List[str]) -> np.ndarray:
    from src.engine.embedder import embed_texts
    return embed_texts(texts)


def _default_embed_query(text: str) -> np.ndarray:
    from src.engine.embedder import embed_query
    return embed_query(text)


def _default_fingerprint() -> str:
    from src.engine.embedder import model_fingerprint
    return model_fingerprint()


def _foreground_busy() -> bool:
    """Another embedding waits for or holds the shared service's one inference worker (persona
    routing, a query). Over stdio there is no such worker, and nothing to wait for."""
    from src.engine import embedder
    executor = getattr(embedder, "_inference", None)
    return executor is not None and getattr(executor, "pending", 0) > 0


YIELD_POLL_SECONDS = 0.25
YIELD_MAX_SECONDS = 30.0


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _retry(action: Callable[[], None], attempts: int = 5) -> None:
    """``action``, retried on ``PermissionError``: on Windows replacing a file that another process
    (or a virus scanner) has open fails for a moment."""
    for attempt in range(attempts):
        try:
            action()
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


class SharedMemoryIndex:
    """Chunk vectors of the shared facts, built in the background; lexical search needs none.

    The store keeps, per chunk, its vector, the SHA-256 of its text (never the text: a deleted note
    leaves nothing readable behind) and the fingerprint of the model that made the vector. The
    fingerprint travels in the same save as the vector, so a reader can never pair vectors with
    another model's marker; vectors of another model are ignored, chunk by chunk."""

    def __init__(self, data_dir: str, *, embed_texts: Callable = _default_embed_texts,
                 embed_query: Callable = _default_embed_query, fingerprint: Callable[[], str] = _default_fingerprint,
                 save_every: float = SAVE_EVERY_SECONDS, busy: Callable[[], bool] = _foreground_busy):
        self.data_dir = data_dir
        self._embed_texts, self._embed_query, self._fingerprint = embed_texts, embed_query, fingerprint
        self._save_every = save_every
        self._busy = busy
        self._meta_path = os.path.join(data_dir, STORE_NAME + ".json")
        self._build_lock = os.path.join(data_dir, LOCK_NAME)
        self._save_lock = os.path.join(data_dir, SAVE_LOCK_NAME)
        self._lock = threading.RLock()
        self._store = None
        self._store_stamp = None
        self._wanted: Dict[str, str] = {}
        self._thread: Optional[threading.Thread] = None
        self._state = {"state": "idle", "covered": 0, "total": 0}
        self._tokens: Dict[str, Tuple[str, List[str]]] = {}

    # ---- storage
    def _vectors(self):
        """The vector store as saved on disk, reloaded when another process saved it since.

        The store writes its two files one after another and discards both when it finds them from
        two different saves. So a save holds the save lock exclusively and a reload holds it shared:
        a reload never sees half a save. A caller keeps the returned object for its whole operation,
        so everything it reads comes from one save."""
        from src.engine.vector_store import NumpyVectorStore
        from src.file_lock import file_lock
        with self._lock:
            os.makedirs(self.data_dir, exist_ok=True)
            with file_lock(self._save_lock, shared=True):
                try:
                    stamp = os.stat(self._meta_path).st_mtime_ns
                except OSError:
                    stamp = None
                if self._store is None or stamp != self._store_stamp:
                    self._store = NumpyVectorStore(STORE_NAME, self.data_dir)
                    self._store_stamp = stamp
            return self._store

    @staticmethod
    def _current(store, documents: Dict[str, str], model: str) -> List[str]:
        """The chunk ids of ``documents`` whose stored vector ``model`` made for their current text."""
        stored = store.get(list(documents))
        return [chunk_id for chunk_id, digest, meta in zip(stored.ids, stored.documents, stored.metadatas)
                if digest == _digest(documents[chunk_id]) and (meta or {}).get("model") == model]

    def _save(self, vectors: Dict[str, np.ndarray], documents: Dict[str, str], model: str) -> None:
        from src.engine.vector_store import NumpyVectorStore
        from src.file_lock import file_lock
        ids = sorted(vectors)
        with self._lock:
            os.makedirs(self.data_dir, exist_ok=True)
            with file_lock(self._save_lock):
                store = NumpyVectorStore(STORE_NAME, self.data_dir)
                if ids:
                    store.replace(ids=ids, embeddings=np.asarray([vectors[i] for i in ids], dtype=np.float32),
                                  documents=[_digest(documents[i]) for i in ids],
                                  metadatas=[{"model": model} for _ in ids])
                else:
                    store.clear()
                _retry(store.save)
            self._store_stamp = None  # read the saved state back on next use

    # ---- building
    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def refresh(self, documents: Dict[str, str]) -> dict:
        """Starts the background build when the stored vectors do not cover ``documents``
        (chunk id -> text); returns the build state."""
        with self._lock:
            if documents != self._wanted:  # a running build restarts only for a real change
                self._wanted = dict(documents)
            if self._thread is not None:
                return dict(self._state)
            covered = self._covered(documents)
            # Stored chunks that are no longer wanted (a deleted or newly skipped note) are pruned by
            # a build that embeds nothing.
            if covered == len(documents) and self._vectors().count() == covered:
                self._state = {"state": "ready", "covered": covered, "total": len(documents)}
                return dict(self._state)
            self._state = {"state": "building", "covered": covered, "total": len(documents)}
            self._thread = threading.Thread(target=self._build, name="shared-memory-index", daemon=True)
            self._thread.start()
            return dict(self._state)

    def coverage(self, documents: Dict[str, str], scoped: bool = False) -> dict:
        """The build state with ``covered`` and ``total`` counted over ``documents`` only.

        ``scoped``: the documents are what one scope shows, and the report says only what that scope
        can see. A complete coverage reads ``ready`` even while the builder embeds another project's
        notes; an incomplete one reads ``building`` or ``building_elsewhere`` while a build runs and
        ``incomplete`` otherwise, never the builder's error, which another project's note may have
        caused."""
        state, covered = self.status(), self._covered(documents)
        if scoped:
            if covered == len(documents):
                state = {"state": "ready"}
            elif state.get("state") in ("building", "building_elsewhere"):
                state = {"state": state["state"]}
            else:
                state = {"state": "incomplete"}
        return {**state, "covered": covered, "total": len(documents)}

    def _covered(self, documents: Dict[str, str]) -> int:
        try:
            model = self._fingerprint()
        except Exception:  # noqa: BLE001 - an unloadable model leaves the index lexical
            return 0
        return len(self._current(self._vectors(), documents, model))

    def join(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _build(self) -> None:
        """Builds until the wanted documents stop changing. The thread stays registered until it has
        released the build lock, so a refresh during the hand-over is built by this thread instead of
        by a new one that would find the lock still held."""
        from src.file_lock import file_lock
        while True:
            built = None
            try:
                os.makedirs(self.data_dir, exist_ok=True)
                with file_lock(self._build_lock, blocking=False):
                    while built is None:
                        with self._lock:
                            wanted = self._wanted
                        if self._build_once(wanted):
                            built = wanted
            except BlockingIOError:
                with self._lock:
                    self._state = {**self._state, "state": "building_elsewhere"}
            except Exception as error:  # noqa: BLE001 - a background thread reports instead of raising
                logger.warning("shared memory index build failed: %s", error, exc_info=True)
                with self._lock:
                    self._state = {**self._state, "state": "failed", "error": f"{type(error).__name__}: {error}"}
            with self._lock:
                if built is None or self._wanted is built:
                    self._thread = None  # a refresh after this starts a new build
                    return

    def _build_once(self, wanted: Dict[str, str]) -> bool:
        """Embeds what ``wanted`` lacks; True when done, False when ``wanted`` changed meanwhile."""
        model = self._fingerprint()
        store = self._vectors()
        vectors: Dict[str, np.ndarray] = store.get_embeddings(self._current(store, wanted, model))
        todo = [chunk_id for chunk_id in sorted(wanted) if chunk_id not in vectors]
        last_save = time.monotonic()
        for start in range(0, len(todo), EMBED_BATCH):
            with self._lock:
                if self._wanted is not wanted:
                    self._save(vectors, wanted, model)
                    return False
            self._yield()
            batch = todo[start:start + EMBED_BATCH]
            embedded = np.asarray(self._embed_texts([wanted[chunk_id] for chunk_id in batch]), dtype=np.float32)
            for chunk_id, vector in zip(batch, embedded):
                vectors[chunk_id] = vector
            with self._lock:
                self._state = {"state": "building", "covered": len(vectors), "total": len(wanted)}
            if time.monotonic() - last_save >= self._save_every:
                self._save(vectors, wanted, model)
                last_save = time.monotonic()
        self._save({chunk_id: vectors[chunk_id] for chunk_id in wanted if chunk_id in vectors}, wanted, model)
        with self._lock:
            if self._wanted is not wanted:
                return False
            self._state = {"state": "ready", "covered": len(vectors), "total": len(wanted)}
        return True

    def _yield(self) -> None:
        """Waits, up to ``YIELD_MAX_SECONDS``, while another embedding is queued or running, so the
        build never queues ahead of persona routing: a foreground call waits for one batch at most."""
        deadline = time.monotonic() + YIELD_MAX_SECONDS
        while self._busy() and time.monotonic() < deadline:
            time.sleep(YIELD_POLL_SECONDS)

    # ---- searching
    def _chunk_tokens(self, chunk_id: str, document: str) -> List[str]:
        cached = self._tokens.get(chunk_id)
        if cached is not None and cached[0] == document:
            return cached[1]
        words = tokens(document)
        if len(self._tokens) > 50_000:
            self._tokens.clear()
        self._tokens[chunk_id] = (document, words)
        return words

    def semantic(self, query: str, documents: Dict[str, str]) -> Tuple[Dict[str, float], Dict[str, np.ndarray]]:
        """Cosine similarity of ``query`` to each chunk with a current vector, and those vectors, all
        from one saved state of the store and the model that embeds the query."""
        model = self._fingerprint()
        store = self._vectors()
        current = self._current(store, documents, model)
        if not current:
            return {}, {}
        query_vector = np.asarray(self._embed_query(query), dtype=np.float32).reshape(-1)
        query_vector = query_vector / (np.linalg.norm(query_vector) or 1.0)
        vectors = {chunk_id: vector for chunk_id, vector in store.get_embeddings(current).items()
                   if vector.shape == query_vector.shape}
        similarity = {}
        for chunk_id, vector in vectors.items():
            similarity[chunk_id] = float(np.dot(vector, query_vector) / (np.linalg.norm(vector) or 1.0))
        return similarity, vectors

    def search(self, query: str, facts: Sequence[Fact],
               use_vectors: bool = True) -> Tuple[List[dict], Dict[str, np.ndarray], Optional[str]]:
        """``facts`` ranked for ``query`` (``{fact, score, chunk, lexical, semantic}``, best first), the
        chunk vectors the semantic ranking used, and why semantic ranking failed, if it did: the
        lexical ranking is returned then.

        Each kind ranks the facts by their best chunk; a fact's score is the sum of ``1/(RRF_K+rank)``
        over the kinds that found it. Its shown chunk is its best lexical one, else its best semantic one."""
        documents, owner = {}, {}
        for fact in facts:
            for chunk_id, text in chunks(fact):
                documents[chunk_id], owner[chunk_id] = text, fact.id
        lexical = bm25(query, {chunk_id: self._chunk_tokens(chunk_id, text) for chunk_id, text in documents.items()})
        semantic, vectors, problem = {}, {}, None
        if use_vectors:
            try:
                semantic, vectors = self.semantic(query, documents)
            except Exception as error:  # noqa: BLE001 - the lexical ranking still answers
                logger.warning("shared memory semantic search failed: %s", error, exc_info=True)
                problem = f"{type(error).__name__}: {error}"
        best: Dict[str, dict] = {}
        for kind, scores in (("lexical", lexical), ("semantic", semantic)):
            per_fact: Dict[str, Tuple[float, str]] = {}
            for chunk_id, score in scores.items():
                fact_id = owner[chunk_id]
                if fact_id not in per_fact or score > per_fact[fact_id][0]:
                    per_fact[fact_id] = (score, chunk_id)
            ordered = sorted(per_fact.items(), key=lambda item: (-item[1][0], item[0]))
            for rank, (fact_id, (score, chunk_id)) in enumerate(ordered, 1):
                entry = best.setdefault(fact_id, {"score": 0.0, "chunk": chunk_id, "lexical": None, "semantic": None})
                entry["score"] += 1.0 / (RRF_K + rank)
                entry[kind] = {"rank": rank, "score": round(score, 4)}
        by_id = {fact.id: fact for fact in facts}
        ranked = sorted(best.items(), key=lambda item: (-item[1]["score"], item[0]))
        return ([{"fact": by_id[fact_id], "score": round(entry["score"], 6), "chunk": documents[entry["chunk"]],
                  "lexical": entry["lexical"], "semantic": entry["semantic"]} for fact_id, entry in ranked],
                vectors, problem)

    def similar(self, fact: Fact, others: Sequence[Fact], vectors: Dict[str, np.ndarray],
                matrices: Optional[Dict[str, Optional[np.ndarray]]] = None) -> List[dict]:
        """Facts of another client or project whose nearest chunk is at least ``SIMILAR_MIN`` similar.

        ``matrices`` caches each fact's normalized chunk vectors across the rows of one query."""
        matrices = {} if matrices is None else matrices

        def matrix_of(item: Fact) -> Optional[np.ndarray]:
            if item.id not in matrices:
                rows = [vectors[chunk_id] for chunk_id, _ in chunks(item) if chunk_id in vectors]
                if rows:
                    matrix = np.asarray(rows, dtype=np.float32)
                    matrices[item.id] = matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
                else:
                    matrices[item.id] = None
            return matrices[item.id]

        mine = matrix_of(fact)
        if mine is None:
            return []
        places = {(source.client, source.project) for source in fact.sources}
        found = []
        for other in others:
            if other.id == fact.id or not ({(s.client, s.project) for s in other.sources} - places):
                continue
            matrix = matrix_of(other)
            if matrix is None:
                continue
            score = float((mine @ matrix.T).max())
            if score >= SIMILAR_MIN:
                found.append({"id": other.id, "name": other.name, "similarity": round(score, 4),
                              "sources": [source.to_dict() for source in other.sources]})
        found.sort(key=lambda item: -item["similarity"])
        return found[:5]


_index: Optional[SharedMemoryIndex] = None
_index_lock = threading.Lock()


def get_index() -> SharedMemoryIndex:
    """This process's index over the installation data directory."""
    global _index
    with _index_lock:
        if _index is None:
            from src.engine import config
            _index = SharedMemoryIndex(os.path.join(config.INSTALL_DATA_DIR, "memory"))
        return _index


def documents_of(facts: Iterable[Fact]) -> Dict[str, str]:
    return {chunk_id: text for fact in facts for chunk_id, text in chunks(fact)}


# --- the tool's answer ------------------------------------------------------------------------------

def _entry(fact: Fact, text: Optional[str], size: int, extra: dict) -> dict:
    out = fact.summary()
    if text is not None:
        if len(text) > size:
            out["text"] = preview(text, size)
            out["truncated"] = {"text": len(text)}
        else:
            out["text"] = text
    out.update(extra)
    return out


def _fits(payload: dict) -> bool:
    return shown_size(to_json(payload)) <= RESULT_BUDGET


def _listing(mode: str, rows: List[Tuple[Fact, Optional[str], dict]], extra: dict, offset: int) -> dict:
    """The payload for ``rows`` under ``RESULT_BUDGET``: shorter previews first, then fewer rows."""
    def render(size: int, count: int) -> dict:
        shown = [_entry(fact, text, size, more) for fact, text, more in rows[:count]]
        payload = {"mode": mode, "total": len(shown), "facts": shown}
        omitted = len(rows) - count
        if omitted:
            payload["omitted"] = omitted
        if omitted or any("truncated" in row for row in shown):
            note = ("read_shared_memory(fact_id=<id>) returns a fact with as much of its text as stays "
                    "inline; its source file holds the rest.")
            if omitted:
                note += (f" {omitted} more facts were left out to keep the result under {INLINE_LIMIT:,} "
                         f"characters: repeat this call with offset={offset + count}.")
            payload["instruction"] = note
        payload.update(extra)
        return payload

    for size in (PREVIEW_CHARS, 300, 200, MIN_PREVIEW_CHARS):
        payload = render(size, len(rows))
        if _fits(payload):
            return payload
    for count in range(len(rows) - 1, 0, -1):
        payload = render(MIN_PREVIEW_CHARS, count)
        if _fits(payload):
            return payload
    if not rows:
        return render(MIN_PREVIEW_CHARS, 0)
    # Not even one row fits with its text: the first comes without it, so offset still moves on.
    payload = {"mode": mode, "total": 1, "facts": [_entry(rows[0][0], None, 0, {})]}
    note = "This fact's text is left out to keep the result inline; read_shared_memory(fact_id=<id>) returns it."
    if len(rows) > 1:
        payload["omitted"] = len(rows) - 1
        note += f" Repeat this call with offset={offset + 1} for the rest."
    payload["instruction"] = note
    payload.update(extra)
    return payload


def read(*, root: Optional[Path], scope: str = "project", client: Optional[str] = None,
         memory_type: Optional[str] = None, query: Optional[str] = None, limit: int = 10, offset: int = 0,
         fact_id: Optional[str] = None, stores: Optional[List[native.Store]] = None,
         index: Optional[SharedMemoryIndex] = None, semantic: bool = True) -> dict:
    """The ``read_shared_memory`` payload; see that tool for the arguments."""
    index = index or get_index()
    collection = collect(discover() if stores is None else stores)
    projects = project_keys(root)
    report: dict = {"scope": scope}
    skipped = Counter()
    for (project, reason), count in collection.skipped_at.items():
        if shows(project, scope, projects):  # another project's skipped files stay uncounted
            skipped[reason] += count
    if skipped:
        report["skipped"] = dict(sorted(skipped.items()))
    if root is None and scope == "project":
        report["note"] = "No project resolved, so only user-level facts are shown."
    elif scope == "project" and not any(
            shows(source.project, "project", projects) and source.project
            for fact in collection.facts.values() for source in fact.sources):
        report["note"] = ("No client keeps memory for this workspace's project; only user-level facts "
                          "are shown.")
    visible = select(collection.facts.values(), scope=scope, projects=projects, client=client,
                     memory_type=memory_type)
    if fact_id is not None:
        fact = next((fact for fact in visible if fact.id == fact_id), None)
        return _whole(fact, report)
    if not query:
        rows = [(fact, _body(fact), {}) for fact in visible[offset:offset + limit]]
        return _listing("recency", rows, report, offset)
    if semantic:
        try:
            index.refresh(documents_of(collection.facts.values()))  # every fact, so a scope change needs no build
            report["index"] = index.coverage(documents_of(visible), scoped=scope != "all")
        except Exception as error:  # noqa: BLE001 - an index that cannot be set up leaves the search lexical
            logger.warning("shared memory index unavailable: %s", error, exc_info=True)
            report["index"] = {"state": "failed", "error": f"{type(error).__name__}: {error}"}
            semantic = False
    else:
        report["index"] = {"state": "off"}
    ranked, vectors, problem = index.search(query, visible, use_vectors=semantic)
    if problem:
        report["index"] = {**report["index"], "semantic_error": problem}
    rows, matrices = [], {}
    for item in ranked[offset:offset + limit]:
        extra = {"score": item["score"], "match": {"lexical": item["lexical"], "semantic": item["semantic"]}}
        alike = index.similar(item["fact"], visible, vectors, matrices) if vectors else []
        if alike:
            extra["similar"] = alike
        rows.append((item["fact"], item["chunk"], extra))
    mode = "hybrid" if vectors else "lexical"
    return _listing(mode, rows, report, offset)


def _body(fact: Fact) -> str:
    """A fact's text without its frontmatter, which ``summary`` already shows."""
    _, body = split_frontmatter(fact.text)
    return (body if body is not None else fact.text).strip()


def _whole(fact: Optional[Fact], report: dict) -> dict:
    """One fact with its whole text, or as much of it as stays inline."""
    if fact is None:
        return {"mode": "fact", "total": 0, "facts": [], **report}
    size = len(fact.text)
    while True:
        payload = {"mode": "fact", "total": 1, "facts": [_entry(fact, fact.text, size, {})], **report}
        if _fits(payload) or size <= MIN_PREVIEW_CHARS:
            break
        size = max(MIN_PREVIEW_CHARS, size * 3 // 4)
    if size < len(fact.text):
        payload["instruction"] = ("The text is cut to keep the result inline; read the file at one of its "
                                  "sources for the rest.")
    return payload


__all__ = [
    "CLIENTS", "Collection", "FACT_ID", "Fact", "SCOPES", "SharedMemoryIndex", "Source", "bm25", "chunks",
    "collect", "discover", "documents_of", "get_index", "project_keys", "read", "select", "tokens",
]
