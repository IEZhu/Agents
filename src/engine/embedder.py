"""Lightweight embedding engine based on FastEmbed (ONNX Runtime).

Replaces sentence-transformers + PyTorch with a much lighter dependency
footprint (~100 MB installed vs ~2 GB for torch + transformers).
Model is selected via EMBEDDING_MODEL env var (set during setup).

Uses query_embed() for queries and passage_embed() for documents
to apply model-specific instruction prefixes (e.g. "query: " / "passage: ").
"""

import glob
import logging
import os
import shutil
import tempfile
import threading
import warnings
from typing import List

import numpy as np

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_model = None


def clear_model_cache(model_name: str) -> None:
    """Remove fastembed's cached files for *model_name* so the next load re-downloads."""
    cache_dir = os.path.join(tempfile.gettempdir(), "fastembed_cache")
    if not os.path.isdir(cache_dir):
        return
    suffix = model_name.split("/")[-1]
    for d in glob.glob(os.path.join(cache_dir, f"models--*{suffix}*")):
        logger.warning("Removing corrupted model cache: %s", d)
        shutil.rmtree(d, ignore_errors=True)


_MAX_LOAD_RETRIES = 2


def _get_model():
    """Lazy-init singleton TextEmbedding instance.

    On first failure (e.g. corrupted/incomplete cache) the model cache is
    cleared and one retry is attempted, so the server can self-heal without
    manual intervention.
    """
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding
                from src.engine.config import EMBEDDING_MODEL

                for attempt in range(_MAX_LOAD_RETRIES):
                    try:
                        logger.info("Loading embedding model: %s (attempt %d)", EMBEDDING_MODEL, attempt + 1)
                        with warnings.catch_warnings():
                            warnings.filterwarnings("ignore", message=".*now uses mean pooling.*")
                            _model = TextEmbedding(model_name=EMBEDDING_MODEL)
                        logger.info("Embedding model loaded")
                        break
                    except Exception:
                        if attempt < _MAX_LOAD_RETRIES - 1:
                            logger.warning(
                                "Model load failed, clearing cache and retrying",
                                exc_info=True,
                            )
                            clear_model_cache(EMBEDDING_MODEL)
                        else:
                            raise
    return _model


def reset_model():
    """Discard the cached model so the next call re-initializes it."""
    global _model
    with _lock:
        _model = None


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
