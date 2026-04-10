"""Lightweight NumPy-based vector store.

Replaces ChromaDB for datasets of ~100-1000 items.
Stores embeddings as .npz and metadata as .json on disk.
Cosine similarity via numpy — O(N) brute-force, <0.1 ms at N=200.
"""

import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class QueryResult:
    ids: List[str]
    distances: List[float]
    documents: List[str]
    metadatas: List[Dict[str, Any]]


@dataclass
class GetResult:
    ids: List[str]
    documents: List[str]
    metadatas: List[Dict[str, Any]]


class NumpyVectorStore:
    """Thread-safe in-memory vector store backed by .npz + .json files."""

    def __init__(self, name: str, data_dir: str):
        self.name = name
        self._data_dir = data_dir
        self._npz_path = os.path.join(data_dir, f"{name}.npz")
        self._meta_path = os.path.join(data_dir, f"{name}.json")
        self._lock = threading.RLock()

        # In-memory state (guarded by _lock)
        self._embeddings: Optional[np.ndarray] = None  # (N, D) float32
        self._normed: Optional[np.ndarray] = None  # (N, D) L2-normalized cache
        self._ids: List[str] = []
        self._documents: List[str] = []
        self._metadatas: List[Dict[str, Any]] = []
        self._id_to_idx: Dict[str, int] = {}

        self._load()

    def _load(self):
        """Load from disk if files exist."""
        if os.path.exists(self._npz_path) and os.path.exists(self._meta_path):
            try:
                with np.load(self._npz_path) as data:
                    self._embeddings = data["embeddings"]
                    npz_version = str(data["save_version"]) if "save_version" in data.files else ""

                with open(self._meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)

                self._ids = meta["ids"]
                self._documents = meta["documents"]
                self._metadatas = meta["metadatas"]

                # Verify both files were written in the same save() call.
                # Both empty ("" == "") is fine (pre-migration stores); any other
                # inequality — including one side missing — indicates a mid-save crash.
                meta_version = meta.get("save_version", "")
                if npz_version != meta_version:
                    raise ValueError(
                        f"Save version mismatch: npz={npz_version!r}, json={meta_version!r}"
                    )

                # Validate embeddings shape (must be 2-D)
                if self._embeddings.ndim != 2:
                    raise ValueError(
                        f"Expected 2-D embeddings, got ndim={self._embeddings.ndim}"
                    )

                # Validate consistency between embeddings and metadata
                n_emb = self._embeddings.shape[0]
                n_ids = len(self._ids)
                n_docs = len(self._documents)
                n_meta = len(self._metadatas)
                if not (n_emb == n_ids == n_docs == n_meta):
                    raise ValueError(
                        f"Length mismatch: embeddings={n_emb}, "
                        f"ids={n_ids}, documents={n_docs}, metadatas={n_meta}"
                    )

                self._id_to_idx = {id_: i for i, id_ in enumerate(self._ids)}
                self._recompute_norms()

                logger.info(f"[{self.name}] Loaded {len(self._ids)} entries from disk")
            except Exception as e:
                logger.warning(
                    "[%s] Failed to load from disk, removing corrupted files: %s",
                    self.name, e, exc_info=True,
                )
                self._reset()
                # Remove corrupted files so next startup doesn't retry the same failure
                for path in (self._npz_path, self._meta_path):
                    try:
                        if os.path.exists(path):
                            os.unlink(path)
                    except OSError:
                        pass
        elif os.path.exists(self._npz_path) or os.path.exists(self._meta_path):
            # Only one of the two files exists — treat as corruption from a
            # mid-save crash and remove the stray file so we don't silently
            # reset on every startup.
            logger.warning(
                "[%s] Partial store files detected (npz=%s, json=%s), "
                "removing stray file",
                self.name,
                os.path.exists(self._npz_path),
                os.path.exists(self._meta_path),
            )
            for path in (self._npz_path, self._meta_path):
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except OSError:
                    pass
            self._reset()
        else:
            self._reset()

    def _reset(self):
        self._embeddings = None
        self._normed = None
        self._ids = []
        self._documents = []
        self._metadatas = []
        self._id_to_idx = {}

    def _recompute_norms(self):
        """Precompute L2-normalized embeddings for fast cosine queries."""
        if self._embeddings is not None and len(self._ids) > 0:
            norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True) + 1e-10
            self._normed = self._embeddings / norms
        else:
            self._normed = None

    def clear(self):
        """Public API: reset in-memory state under lock."""
        with self._lock:
            self._reset()

    def save(self):
        """Persist to disk using atomic write (temp file + rename).

        Both files share a save_version UUID so _load() can detect a
        crash between the two os.replace() calls.
        """
        with self._lock:
            os.makedirs(self._data_dir, exist_ok=True)
            version = uuid.uuid4().hex

            # Save embeddings
            if self._embeddings is not None and len(self._ids) > 0:
                fd, tmp_npz_path = tempfile.mkstemp(
                    dir=self._data_dir, suffix=".npz"
                )
                os.close(fd)
                try:
                    np.savez(tmp_npz_path, embeddings=self._embeddings,
                             save_version=np.array(version))
                    os.replace(tmp_npz_path, self._npz_path)
                except Exception:
                    if os.path.exists(tmp_npz_path):
                        os.unlink(tmp_npz_path)
                    raise
            else:
                # Remove both files for empty store so _load() starts clean
                for path in (self._npz_path, self._meta_path):
                    if os.path.exists(path):
                        os.unlink(path)
                logger.debug(f"[{self.name}] Saved empty store (files removed)")
                return

            # Save metadata
            meta = {
                "ids": self._ids,
                "documents": self._documents,
                "metadatas": self._metadatas,
                "save_version": version,
            }
            fd, tmp_meta_path = tempfile.mkstemp(
                dir=self._data_dir, suffix=".json"
            )
            os.close(fd)
            try:
                with open(tmp_meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False)
                os.replace(tmp_meta_path, self._meta_path)
            except Exception:
                if os.path.exists(tmp_meta_path):
                    os.unlink(tmp_meta_path)
                raise

            logger.debug(f"[{self.name}] Saved {len(self._ids)} entries to disk")

    def count(self) -> int:
        with self._lock:
            return len(self._ids)

    @staticmethod
    def _validate_inputs(
        ids: List[str],
        embeddings: np.ndarray,
        documents: List[str],
        metadatas: List[Dict[str, Any]],
    ):
        """Validate shape/length consistency before mutating state."""
        if embeddings.ndim != 2:
            raise ValueError(f"embeddings must be 2-D, got ndim={embeddings.ndim}")
        n = len(ids)
        if embeddings.shape[0] != n:
            raise ValueError(
                f"Length mismatch: ids={n}, embeddings={embeddings.shape[0]}"
            )
        if len(documents) != n:
            raise ValueError(
                f"Length mismatch: ids={n}, documents={len(documents)}"
            )
        if len(metadatas) != n:
            raise ValueError(
                f"Length mismatch: ids={n}, metadatas={len(metadatas)}"
            )

    def replace(
        self,
        ids: List[str],
        embeddings: np.ndarray,
        documents: List[str],
        metadatas: List[Dict[str, Any]],
    ):
        """Replace the store contents with exactly the provided entries.

        Callers (SkillRetriever, ImplantRetriever) always supply the full
        authoritative set, so a full replace is the correct semantic —
        any old entry not in ``ids`` is stale and must be dropped.
        """
        if len(ids) == 0:
            with self._lock:
                self._reset()
            return

        embeddings = np.asarray(embeddings, dtype=np.float32)
        self._validate_inputs(ids, embeddings, documents, metadatas)

        with self._lock:
            self._embeddings = embeddings.copy()
            self._ids = list(ids)
            self._documents = list(documents)
            self._metadatas = list(metadatas)
            self._id_to_idx = {id_: i for i, id_ in enumerate(self._ids)}
            self._recompute_norms()

    def add(
        self,
        ids: List[str],
        embeddings: np.ndarray,
        documents: List[str],
        metadatas: List[Dict[str, Any]],
    ):
        """Append-only insert (for router cache). Skips existing IDs."""
        embeddings = np.asarray(embeddings, dtype=np.float32)
        self._validate_inputs(ids, embeddings, documents, metadatas)

        new_ids = []
        new_embs = []
        new_docs = []
        new_metas = []

        with self._lock:
            for i, id_ in enumerate(ids):
                if id_ not in self._id_to_idx:
                    new_ids.append(id_)
                    new_embs.append(embeddings[i])
                    new_docs.append(documents[i])
                    new_metas.append(metadatas[i])

            if not new_ids:
                return

            new_emb_array = np.asarray(new_embs, dtype=np.float32)

            if self._embeddings is None or len(self._ids) == 0:
                self._embeddings = new_emb_array
            else:
                if new_emb_array.shape[1] != self._embeddings.shape[1]:
                    raise ValueError(
                        f"Dimension mismatch: new embeddings have "
                        f"{new_emb_array.shape[1]} dims, store has "
                        f"{self._embeddings.shape[1]} dims"
                    )
                self._embeddings = np.vstack([self._embeddings, new_emb_array])

            base_idx = len(self._ids)
            self._ids.extend(new_ids)
            self._documents.extend(new_docs)
            self._metadatas.extend(new_metas)
            for i, id_ in enumerate(new_ids):
                self._id_to_idx[id_] = base_idx + i
            self._recompute_norms()

    def query(
        self,
        query_embedding: np.ndarray,
        n_results: int = 1,
    ) -> QueryResult:
        """Cosine similarity search. Returns closest n_results."""
        with self._lock:
            if self._embeddings is None or len(self._ids) == 0:
                return QueryResult(ids=[], distances=[], documents=[], metadatas=[])

            query_vec = np.asarray(query_embedding, dtype=np.float32).squeeze()
            if query_vec.ndim != 1:
                raise ValueError(
                    f"query_embedding must be 1-D (or squeezable to 1-D), "
                    f"got shape {np.asarray(query_embedding).shape}"
                )
            if query_vec.shape[0] != self._embeddings.shape[1]:
                raise ValueError(
                    f"Dimension mismatch: query has {query_vec.shape[0]} dims, "
                    f"store has {self._embeddings.shape[1]} dims"
                )

            # Cosine distance = 1 - cosine_similarity
            # _normed is precomputed on replace/add/load to avoid per-query allocation
            query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
            if self._normed is None:
                self._recompute_norms()
            similarities = self._normed @ query_norm
            distances = 1.0 - similarities

            # Get top-n indices (use argsort for correctness at small N)
            n = min(n_results, len(self._ids))
            if n <= 0:
                return QueryResult(ids=[], distances=[], documents=[], metadatas=[])
            top_indices = np.argsort(distances)[:n]

            return QueryResult(
                ids=[self._ids[i] for i in top_indices],
                distances=[float(distances[i]) for i in top_indices],
                documents=[self._documents[i] for i in top_indices],
                metadatas=[self._metadatas[i] for i in top_indices],
            )

    def get(self, ids: List[str]) -> GetResult:
        """Direct lookup by IDs. Returns only found entries."""
        with self._lock:
            found_ids = []
            found_docs = []
            found_metas = []

            for id_ in ids:
                if id_ in self._id_to_idx:
                    idx = self._id_to_idx[id_]
                    found_ids.append(id_)
                    found_docs.append(self._documents[idx])
                    found_metas.append(self._metadatas[idx])

            return GetResult(ids=found_ids, documents=found_docs, metadatas=found_metas)

    def trim(self, max_size: int):
        """Keep only the most recent max_size entries (by insertion order)."""
        with self._lock:
            if len(self._ids) <= max_size:
                return
            keep_from = len(self._ids) - max_size
            self._embeddings = self._embeddings[keep_from:]
            self._ids = self._ids[keep_from:]
            self._documents = self._documents[keep_from:]
            self._metadatas = self._metadatas[keep_from:]
            self._id_to_idx = {id_: i for i, id_ in enumerate(self._ids)}
            self._recompute_norms()
            logger.debug(f"[{self.name}] Trimmed to {max_size} entries")

    def get_all_metadatas(self) -> List[Dict[str, Any]]:
        """Return all metadatas (for catalog generation)."""
        with self._lock:
            return list(self._metadatas)
