"""Lightweight embedding engine based on FastEmbed (ONNX Runtime).

Replaces sentence-transformers + PyTorch with a ~10 MB dependency.
Model is selected via EMBEDDING_MODEL env var (set during setup).

Uses query_embed() for queries and passage_embed() for documents
to apply model-specific instruction prefixes (e.g. "query: " / "passage: ").
"""

import logging
import threading
from typing import List

import numpy as np

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_model = None


def _get_model():
    """Lazy-init singleton TextEmbedding instance."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding
                from src.engine.config import EMBEDDING_MODEL

                logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
                _model = TextEmbedding(model_name=EMBEDDING_MODEL)
                logger.info("Embedding model loaded")
    return _model


def embed_texts(texts: List[str]) -> np.ndarray:
    """Embed documents/passages. Returns (N, D) numpy array."""
    model = _get_model()
    return np.array(list(model.passage_embed(texts)))


def embed_query(text: str) -> np.ndarray:
    """Embed a single query. Returns (D,) numpy array.

    Uses query_embed() which adds model-specific query prefixes
    for better retrieval quality.
    """
    model = _get_model()
    return np.array(list(model.query_embed([text])))[0]
