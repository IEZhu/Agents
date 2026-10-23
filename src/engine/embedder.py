"""Lightweight embedding engine based on direct ONNX Runtime inference.

Replaces sentence-transformers + PyTorch with a much lighter dependency
footprint (~100 MB installed vs ~2 GB for torch + transformers).
Model is selected via EMBEDDING_MODEL env var (set during setup).

Queries go through query_embed() and documents through passage_embed(), after
the model's prompt template from src/engine/embedding_prompts.py: the dense adapter itself
adds no "query: " / "passage: " prefix or task instruction for these models.
"""

import logging
import os
import shutil
import stat
import threading
import time
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
_HUB_BLOB_LOCK_BUDGET_SECONDS = 0.25


def _hub_blob_targets(directory: Path, blobs: Path) -> set[Path] | None:
    """Read every snapshot link; ambiguous traversal preserves all blob files."""
    def scan_error(error):
        raise error

    targets = set()
    try:
        root = directory.resolve(strict=True)
        for parent, folders, filenames in os.walk(directory, onerror=scan_error, followlinks=False):
            parent = Path(parent)
            for name in folders:
                folder = parent / name
                if folder.is_symlink() or folder.resolve(strict=True) != root / folder.relative_to(directory):
                    return None  # Includes directory junctions and cache aliases.
            for name in filenames:
                entry = parent / name
                if stat.S_ISLNK(entry.lstat().st_mode):
                    target = entry.resolve(strict=True)
                    if target.parent == blobs and stat.S_ISREG(target.stat().st_mode):
                        targets.add(target)
    except (OSError, RuntimeError):
        return None
    return targets


def _hub_blob_lock_path(repository: Path, blob: Path) -> Path | None:
    """Use Hub's canonical blob lock without following aliased lock paths."""
    cache = repository.parent
    lock_root = cache / ".locks"
    lock_directory = lock_root / repository.name
    lock_path = lock_directory / (blob.name + ".lock")
    try:
        root = cache.resolve(strict=True)
        if blob.parent != repository.resolve(strict=True) / "blobs":
            return None
        for entry in (lock_root, lock_directory, lock_path):
            if entry.is_symlink() or entry.resolve(strict=False) != root / entry.relative_to(cache):
                return None
    except (OSError, RuntimeError, ValueError):
        return None
    return lock_path


def _remove_corrupt_hub_snapshot(snapshot: Path, repository: Path) -> None:
    """Coordinate unreferenced blob removal with the Hub publication protocol."""
    blobs = repository / "blobs"
    try:
        expected = repository.resolve(strict=True) / "blobs"
        targets = (_hub_blob_targets(snapshot, expected)
                   if blobs.resolve(strict=True) == expected and not blobs.is_symlink() else None)
    except (OSError, RuntimeError):
        targets = None
    shutil.rmtree(snapshot, ignore_errors=True)
    if targets is None:
        return
    try:
        snapshot.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return
    else:
        return  # The snapshot or an alias survived the attempted removal.
    from huggingface_hub.utils import WeakFileLock

    remaining = _HUB_BLOB_LOCK_BUDGET_SECONDS
    for target in sorted(targets):
        lock_path = _hub_blob_lock_path(repository, target)
        if lock_path is None:
            return
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            if _hub_blob_lock_path(repository, target) != lock_path:
                return
            if remaining <= 0:
                return
            # Hub's own existing-blob pointer creation holds this same lock.
            # Re-scan under it so a publisher cannot add a link after the scan.
            started = time.monotonic()
            with WeakFileLock(lock_path, timeout=remaining):
                remaining -= max(0.0, time.monotonic() - started)
                if (_hub_blob_lock_path(repository, target) != lock_path
                        or blobs.is_symlink() or blobs.resolve(strict=True) != expected):
                    return
                references = _hub_blob_targets(repository / "snapshots", expected)
                if references is None:
                    return
                if target not in references:
                    target.unlink(missing_ok=True)
        except Exception:
            # Cleanup is optional: lock contention or any unknown maintenance
            # failure preserves blobs and leaves the original load error visible.
            logger.warning("Could not coordinate damaged blob cleanup: %s", target, exc_info=True)
            return


def clear_model_cache(model_name: str, *, failed_model_dir=None) -> None:
    """Remove selected managed caches, or only a proven-corrupt model directory.

    Automatic recovery supplies failed_model_dir and preserves other snapshots
    and fallback sources. An explicit call without it retains the manual reset
    behavior. Caller-owned directories and symlink paths are never removed.
    """
    if os.environ.get("AGENTS_MODEL_PATH"):
        return
    cache = Path(FASTEMBED_CACHE_DIR).expanduser().absolute()
    if not cache.is_dir():
        return
    from src.engine.onnx_embedding import TextEmbedding

    # A pinned plain copy can exist before register_custom has run.
    stale = []
    copy = local_copy(model_name, str(cache))
    if copy:
        stale.append(Path(copy))
    try:
        spec = TextEmbedding._spec(model_name)
    except ValueError:
        spec = None  # Unknown registration cannot identify a Hub/archive cache.
    if spec is not None:
        source = spec["sources"]
        if source.get("hf"):
            name = "models--" + source["hf"].replace("/", "--")
            path = cache / name
            if path.parent == cache:
                stale.append(path)
        if source.get("url"):
            name = ("fast-" if source.get("_deprecated_tar_struct") else "") + spec["model"].split("/")[-1]
            path = cache / name
            if name not in {"", ".", ".."} and path.parent == cache:
                stale.append(path)
    root = cache.resolve()
    for managed in stale:
        path = managed
        if failed_model_dir is not None:
            path = Path(failed_model_dir).expanduser().absolute()
            try:
                path.relative_to(managed)
            except ValueError:
                continue
        try:
            relative = path.relative_to(cache)
        except ValueError:
            continue
        if not relative.parts or ".." in relative.parts:
            continue
        if any(cache.joinpath(*relative.parts[:i]).is_symlink()
               for i in range(1, len(relative.parts) + 1)):
            continue
        if not path.is_dir():
            continue
        resolved = path.resolve()
        if resolved == root or root not in resolved.parents:
            continue
        logger.warning("Removing corrupted model cache: %s", path)
        if (failed_model_dir is not None and managed.name.startswith("models--")
                and path.parent == managed / "snapshots"):
            _remove_corrupt_hub_snapshot(path, managed)
        else:
            shutil.rmtree(path, ignore_errors=True)


_MAX_LOAD_RETRIES = 1 if os.environ.get("AGENTS_TRANSPORT") == "http" else 2


def _get_model():
    """Lazy-init singleton TextEmbedding instance.

    Only positively identified corruption removes the affected managed model
    directory, including on the last attempt. Resource, configuration and unknown
    failures preserve offline caches. Retries remain bounded per transport, and
    explicit AGENTS_MODEL_PATH directories are never removed.
    """
    global _model, _model_fingerprint
    if _model is None:
        with _lock:
            if _model is None:
                from src.engine.onnx_embedding import InvalidModelCache, TextEmbedding

                os.makedirs(FASTEMBED_CACHE_DIR, exist_ok=True)
                register_custom(EMBEDDING_MODEL)

                for attempt in range(_MAX_LOAD_RETRIES):
                    local = None
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
                    except Exception as error:
                        if isinstance(error, InvalidModelCache):
                            try:
                                clear_model_cache(EMBEDDING_MODEL, failed_model_dir=error.model_dir)
                            except OSError:
                                logger.warning("Could not remove the damaged model directory", exc_info=True)
                        if attempt < _MAX_LOAD_RETRIES - 1:
                            logger.warning(
                                "Model load failed, retrying within the transport budget",
                                exc_info=True,
                            )
                        else:
                            raise
                # Set before the model is published.
                _model_fingerprint = _loaded_fingerprint(model)
                _model = model
    return _model


def _loaded_fingerprint(model) -> str:
    """Fingerprint of the snapshot *model* was loaded from.

    The dense adapter picks the Hugging Face snapshot when the load starts, and another
    process can move the cache's refs/main before it ends, so the revision is
    taken from the directory the dense adapter opened (``<cache dir>:<commit>``, which
    equals what ``fingerprint()`` reads from refs when one cache directory
    matches the model). A plain-file copy, which is no snapshot directory, gets
    its pinned revision in ``compute_fingerprint``; other directories fall back to
    refs.
    """
    from src.engine.fingerprint import compute_fingerprint
    revision = None
    model_dir = getattr(getattr(model, "model", None), "_model_dir", None)
    if model_dir is None:
        logger.warning("The dense adapter exposes no model directory; the history index "
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
