"""Lightweight embedding engine based on FastEmbed (ONNX Runtime).

Replaces sentence-transformers + PyTorch with a ~10 MB dependency.
Model is selected via EMBEDDING_MODEL env var (set during setup).
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
    """Embed a list of texts. Returns (N, D) numpy array."""
    model = _get_model()
    return np.array(list(model.embed(texts)))


def embed_query(text: str) -> np.ndarray:
    """Embed a single query. Returns (D,) numpy array."""
    return embed_texts([text])[0]
