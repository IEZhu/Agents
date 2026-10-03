"""Derived-index compatibility, independent of source history and dialogue state."""
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
import hashlib
import json
import os

INDEX_SCHEMA = 2
# v2: model prompt templates applied before embedding (src/engine/embedding_prompts.py).
PREPROCESSING = "prompt-templates-v2"


@lru_cache(maxsize=8)
def fingerprint(model=None):
    from src.engine.config import EMBEDDING_MODEL, FASTEMBED_CACHE_DIR
    from src.engine.embedding_prompts import MAX_INPUT_TOKENS, pinned_revision, templates
    model = model or EMBEDDING_MODEL
    # A plain-file copy holds exactly its pinned export revision (embedding_prompts.materialize).
    revision = os.environ.get("AGENTS_MODEL_ARTIFACT") or pinned_revision(model)
    if not revision:
        # HF snapshot names are immutable commit IDs. Non-HF artifacts must be
        # pinned by AGENTS_MODEL_ARTIFACT in the installed service configuration.
        refs = []
        for cache in sorted(Path(FASTEMBED_CACHE_DIR).glob("models--*" + model.split("/")[-1] + "*")):
            ref = cache / "refs/main"
            if ref.is_file(): refs.append(cache.name + ":" + ref.read_text().strip())
        revision = ",".join(refs) or "unresolved"
    payload = {"model": model, "revision": revision, "schema": INDEX_SCHEMA,
               "preprocessing": PREPROCESSING, "prompts": list(templates(model)),
               "max_input_tokens": MAX_INPUT_TOKENS, "fastembed": version("fastembed")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@lru_cache(maxsize=1)
def configuration_revision():
    from src.engine.config import INSTALL_ROOT
    root = Path(INSTALL_ROOT)
    digest = hashlib.sha256()
    for folder in ("agents", "skills", "implants", "rules"):
        for path in sorted((root / folder).rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
    digest.update(fingerprint().encode())
    return digest.hexdigest()
