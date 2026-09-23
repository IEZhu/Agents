"""Run an offline eval script against a throwaway copy of the install's data/.

Constructing ``SkillRetriever``/``ImplantRetriever`` reindexes into ``DATA_DIR``
whenever the stored hash differs, and the hash covers ``EMBEDDING_MODEL`` and
``IMPLANT_INDEX_MODE``. Without this, a script that switches the index mode, or
runs without ``.env`` loaded (so the model falls back to MiniLM), rewrites the
``implants_store``/``skills_store`` files and their hashes that the daemon and
stdio servers read. Mirrors ``tests/conftest.py``.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
_SEEDED_FILES = (
    "skills_store.npz", "skills_store.json", ".skills_hash",
    "implants_store.npz", "implants_store.json", ".implants_hash",
)
_BINDS_AT_IMPORT = ("src.engine.config", "src.engine.skills", "src.engine.implants", "src.engine.enrichment")
_DATA: str | None = None


def isolate_data_dir() -> str:
    """Point config at a temporary data/ seeded from the install; idempotent.

    Must run before anything imports ``src.engine``: config reads
    ``EMBEDDING_MODEL`` at import, and the retrievers bind ``DATA_DIR`` then.
    """
    global _DATA
    if _DATA is not None:
        return _DATA
    bound = [m for m in _BINDS_AT_IMPORT if m in sys.modules]
    if bound:
        raise RuntimeError(f"isolate_data_dir() must run before importing {bound}")
    if not os.environ.get("EMBEDDING_MODEL"):
        model = dotenv_values(REPO_ROOT / ".env").get("EMBEDDING_MODEL")
        if model:
            os.environ["EMBEDDING_MODEL"] = model
    import src.engine.config as cfg  # after the model pin

    root = tempfile.mkdtemp(prefix="agents-evals-")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    data = os.path.join(root, "data")
    os.mkdir(data)
    for name in _SEEDED_FILES:
        source = os.path.join(cfg.INSTALL_DATA_DIR, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(data, name))
    os.environ["AGENTS_ROUTER_DATA_DIR"] = os.path.join(data, "router")
    cfg.DATA_DIR = cfg.INSTALL_DATA_DIR = data
    cfg.AUTO_UPDATE_STAGING_DIR = os.path.join(data, ".prepared")
    _DATA = data
    return data
