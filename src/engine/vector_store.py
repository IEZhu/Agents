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
        self._ids: List[str] = []
        self._documents: List[str] = []
        self._metadatas: List[Dict[str, Any]] = []
        self._id_to_idx: Dict[str, int] = {}

        self._load()

    def _load(self):
        """Load from disk if files exist."""
        if os.path.exists(self._npz_path) and os.path.exists(self._meta_path):
            try:
                data = np.load(self._npz_path)
                self._embeddings = data["embeddings"]

                with open(self._meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)

                self._ids = meta["ids"]
                self._documents = meta["documents"]
                self._metadatas = meta["metadatas"]
                self._id_to_idx = {id_: i for i, id_ in enumerate(self._ids)}

                logger.info(f"[{self.name}] Loaded {len(self._ids)} entries from disk")
            except Exception as e:
                logger.warning(f"[{self.name}] Failed to load from disk: {e}. Starting empty.")
                self._reset()
        else:
            self._reset()

    def _reset(self):
        self._embeddings = None
        self._ids = []
        self._documents = []
        self._metadatas = []
        self._id_to_idx = {}

    def save(self):
        """Persist to disk using atomic write (temp file + rename)."""
        with self._lock:
            os.makedirs(self._data_dir, exist_ok=True)

            # Save embeddings
            if self._embeddings is not None and len(self._ids) > 0:
                fd, tmp_npz_path = tempfile.mkstemp(
                    dir=self._data_dir, suffix=".npz"
                )
                os.close(fd)
                try:
                    np.savez(tmp_npz_path, embeddings=self._embeddings)
                    os.replace(tmp_npz_path, self._npz_path)
                except Exception:
                    if os.path.exists(tmp_npz_path):
                        os.unlink(tmp_npz_path)
                    raise

            # Save metadata
            meta = {
                "ids": self._ids,
                "documents": self._documents,
                "metadatas": self._metadatas,
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

            logger.info(f"[{self.name}] Saved {len(self._ids)} entries to disk")

    def count(self) -> int:
        with self._lock:
            return len(self._ids)

    def upsert(
        self,
        ids: List[str],
        embeddings: np.ndarray,
        documents: List[str],
        metadatas: List[Dict[str, Any]],
    ):
        """Insert or update entries. Full replace if all IDs are new (common case)."""
        if len(ids) == 0:
            return

        with self._lock:
            # Fast path: full reindex (all new IDs or replacing everything)
            existing = [id_ for id_ in ids if id_ in self._id_to_idx]
            if not existing or len(existing) == len(self._ids):
                # Full replace
                self._embeddings = np.asarray(embeddings, dtype=np.float32)
                self._ids = list(ids)
                self._documents = list(documents)
                self._metadatas = list(metadatas)
                self._id_to_idx = {id_: i for i, id_ in enumerate(self._ids)}
            else:
                # Incremental upsert
                for i, id_ in enumerate(ids):
                    if id_ in self._id_to_idx:
                        idx = self._id_to_idx[id_]
                        self._embeddings[idx] = embeddings[i]
                        self._documents[idx] = documents[i]
                        self._metadatas[idx] = metadatas[i]
                    else:
                        idx = len(self._ids)
                        self._ids.append(id_)
                        self._documents.append(documents[i])
                        self._metadatas.append(metadatas[i])
                        self._id_to_idx[id_] = idx
                        if self._embeddings is None:
                            self._embeddings = np.asarray(
                                embeddings[i : i + 1], dtype=np.float32
                            )
                        else:
                            self._embeddings = np.vstack(
                                [self._embeddings, embeddings[i : i + 1]]
                            )

    def add(
        self,
        ids: List[str],
        embeddings: np.ndarray,
        documents: List[str],
        metadatas: List[Dict[str, Any]],
    ):
        """Append-only insert (for router cache). Skips existing IDs."""
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
                self._embeddings = np.vstack([self._embeddings, new_emb_array])

            base_idx = len(self._ids)
            self._ids.extend(new_ids)
            self._documents.extend(new_docs)
            self._metadatas.extend(new_metas)
            for i, id_ in enumerate(new_ids):
                self._id_to_idx[id_] = base_idx + i

    def query(
        self,
        query_embedding: np.ndarray,
        n_results: int = 1,
    ) -> QueryResult:
        """Cosine similarity search. Returns closest n_results."""
        with self._lock:
            if self._embeddings is None or len(self._ids) == 0:
                return QueryResult(ids=[], distances=[], documents=[], metadatas=[])

            query_vec = np.asarray(query_embedding, dtype=np.float32)

            # Cosine distance = 1 - cosine_similarity
            query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
            norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True) + 1e-10
            normed = self._embeddings / norms
            similarities = normed @ query_norm
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
            logger.info(f"[{self.name}] Trimmed to {max_size} entries")

    def get_all_metadatas(self) -> List[Dict[str, Any]]:
        """Return all metadatas (for catalog generation)."""
        with self._lock:
            return list(self._metadatas)
