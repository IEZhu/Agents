"""Lightweight embedding engine based on FastEmbed (ONNX Runtime).

Replaces sentence-transformers + PyTorch with a much lighter dependency
footprint (~100 MB installed vs ~2 GB for torch + transformers).
Model is selected via EMBEDDING_MODEL env var (set during setup).

Queries go through query_embed() and documents through passage_embed(), after
the model's prompt template from src/engine/embedding_prompts.py: fastembed itself
adds no "query: " / "passage: " prefix or task instruction for these models.
"""

import glob
import logging
import os
import shutil
import threading
import warnings
from pathlib import Path
from typing import List

import numpy as np

from src.engine.config import EMBEDDING_BATCH_SIZE, EMBEDDING_MODEL, FASTEMBED_CACHE_DIR
from src.engine.embedding_prompts import (
    as_passage, as_query, cap_tokens, local_copy, materialize, register_custom,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_model = None
# Embedding fingerprint of the files _model was loaded from (see model_fingerprint).
_model_fingerprint = None


def clear_model_cache(model_name: str) -> None:
    """Remove fastembed's cached files and the plain-file copy of *model_name* so the next load re-downloads."""
    if not os.path.isdir(FASTEMBED_CACHE_DIR):
        return
    suffix = model_name.split("/")[-1]
    stale = glob.glob(os.path.join(FASTEMBED_CACHE_DIR, f"models--*{suffix}*"))
    copy = local_copy(model_name, FASTEMBED_CACHE_DIR)
    if copy and os.path.isdir(copy):
        stale.append(copy)
    for d in stale:
        logger.warning("Removing corrupted model cache: %s", d)
        shutil.rmtree(d, ignore_errors=True)


_MAX_LOAD_RETRIES = 1 if os.environ.get("AGENTS_TRANSPORT") == "http" else 2


def _get_model():
    """Lazy-init singleton TextEmbedding instance.

    On first failure (e.g. corrupted/incomplete cache) the model cache is
    cleared and one retry is attempted, so the server can self-heal without
    manual intervention.
    """
    global _model, _model_fingerprint
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding

                os.makedirs(FASTEMBED_CACHE_DIR, exist_ok=True)
                register_custom(EMBEDDING_MODEL)

                for attempt in range(_MAX_LOAD_RETRIES):
                    try:
                        logger.info("Loading embedding model: %s (attempt %d)", EMBEDDING_MODEL, attempt + 1)
                        with warnings.catch_warnings():
                            warnings.filterwarnings("ignore", message=".*now uses mean pooling.*")
                            options = {}
                            if os.environ.get("AGENTS_MODEL_PATH"):
                                options["specific_model_path"] = os.environ["AGENTS_MODEL_PATH"]
                                options["local_files_only"] = True
                            elif local := materialize(EMBEDDING_MODEL, FASTEMBED_CACHE_DIR):
                                options["specific_model_path"] = local
                            model = TextEmbedding(model_name=EMBEDDING_MODEL, cache_dir=FASTEMBED_CACHE_DIR, **options)
                            cap_tokens(model)
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
                # Set before the model is published.
                _model_fingerprint = _loaded_fingerprint(model)
                _model = model
    return _model


def _loaded_fingerprint(model) -> str:
    """Fingerprint of the snapshot *model* was loaded from.

    fastembed picks the Hugging Face snapshot when the load starts, and another
    process can move the cache's refs/main before it ends, so the revision is
    taken from the directory fastembed opened (``<cache dir>:<commit>``, which
    equals what ``fingerprint()`` reads from refs when one cache directory
    matches the model). A plain-file copy has its pinned revision
    (``compute_fingerprint``); other directories fall back to refs.
    """
    from src.engine.fingerprint import compute_fingerprint
    revision = None
    model_dir = getattr(getattr(model, "model", None), "_model_dir", None)
    if model_dir is None:
        logger.warning("fastembed exposes no model directory; the history index "
                       "fingerprint falls back to the model cache refs")
    elif isinstance(model_dir, (str, os.PathLike)):
        path = Path(model_dir)
        if path.parent.name == "snapshots" and path.parent.parent.name.startswith("models--"):
            revision = f"{path.parent.parent.name}:{path.name}"
    return compute_fingerprint(EMBEDDING_MODEL, revision=revision)


def reset_model():
    """Discard the cached model so the next call re-initializes it."""
    global _model, _model_fingerprint
    with _lock:
        _model = None
        _model_fingerprint = None


def model_fingerprint() -> str:
    """Embedding fingerprint of the snapshot the loaded model came from.

    ``fingerprint()`` reads the model cache when first called, so in a process
    that loaded its model earlier it can describe a snapshot another process
    downloaded since. Indexes that label their vectors use this value instead;
    before the first load it falls back to ``fingerprint()``.
    """
    if _model_fingerprint is not None:
        return _model_fingerprint
    from src.engine.fingerprint import fingerprint
    return fingerprint(EMBEDDING_MODEL)


def _embed_texts(texts: List[str]) -> np.ndarray:
    """Embed documents/passages in bounded batches. Returns (N, D) numpy array."""
    model = _get_model()
    return np.array(list(model.passage_embed([as_passage(EMBEDDING_MODEL, t) for t in texts],
                                             batch_size=EMBEDDING_BATCH_SIZE)))


def _embed_query(text: str) -> np.ndarray:
    """Embed a single query. Returns (D,) numpy array.

    The model's query template (a prefix or task instruction) is applied first.
    """
    model = _get_model()
    return np.array(list(model.query_embed([as_query(EMBEDDING_MODEL, text)])))[0]


# One inference worker for every embedding entry point in the shared runtime.
_inference = None
if os.environ.get("AGENTS_TRANSPORT") == "http":
    from src.daemon.execution import InferenceExecutor
    _inference = InferenceExecutor()


def embed_texts(texts: List[str]) -> np.ndarray:
    return _inference.run(_embed_texts, texts) if _inference else _embed_texts(texts)


def embed_query(text: str) -> np.ndarray:
    return _inference.run(_embed_query, text) if _inference else _embed_query(text)
