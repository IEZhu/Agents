"""Keep the test suite away from the live install's derived data (issue #68).

The checkout this suite runs from is usually also the live install: the shared
daemon and stdio servers read ``data/skills_store.*``, ``data/implants_store.*``
and their hash files from here. Two things used to let tests rewrite them:

* ``src.engine.enrichment`` builds ``SkillRetriever()``/``ImplantRetriever()`` at
  import time, and they reindex into ``DATA_DIR`` whenever the stored hash does
  not match. pytest does not load ``.env``, so ``EMBEDDING_MODEL`` fell back to
  MiniLM, the hash never matched, and the live stores were rebuilt under the
  wrong model.
* ``src.self_update`` derives ``STATE_FILE``/``CHECK_STAMP``/``LOCK_FILE`` from
  ``INSTALL_DATA_DIR`` at import time, so tests that do not redirect them write
  the live ``data/.last_update.json``.

This runs at module level because pytest imports ``conftest.py`` before it
collects test modules, and the retrievers bind their paths at import. It
patches ``src.engine.config`` attributes instead of adding an environment hook:
an env var would be inherited by the staged-update ``src.reindex`` subprocess
(``src/self_update.py``) and redirect its stores out of the worktree.
"""
import atexit
import os
import shutil
import tempfile

from dotenv import dotenv_values

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIVE_DATA_DIR = os.path.join(_REPO_ROOT, "data")

# Stores and their hash files: copying them lets the retrievers see a matching
# hash and skip re-embedding. On a host without the same fastembed snapshot the
# fingerprint differs and they re-embed, but only into the temporary copy.
_SEEDED_FILES = (
    "skills_store.npz",
    "skills_store.json",
    ".skills_hash",
    "implants_store.npz",
    "implants_store.json",
    ".implants_hash",
)


def _pin_embedding_model() -> None:
    """Use the install's model so the copied stores' hashes can match."""
    if os.environ.get("EMBEDDING_MODEL"):
        return
    model = dotenv_values(os.path.join(_REPO_ROOT, ".env")).get("EMBEDDING_MODEL")
    if model:
        os.environ["EMBEDDING_MODEL"] = model


def _isolated_data_dir() -> str:
    """A temporary ``data`` directory seeded with copies of the live stores."""
    root = tempfile.mkdtemp(prefix="agents-tests-")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    data = os.path.join(root, "data")  # Tests assert the directory is named "data".
    os.mkdir(data)
    for name in _SEEDED_FILES:
        source = os.path.join(_LIVE_DATA_DIR, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(data, name))
    return data


_pin_embedding_model()
TEST_DATA_DIR = _isolated_data_dir()
# The router prefers this variable over config.DATA_DIR; a value inherited from
# a stdio/daemon environment would point it at live router state.
os.environ["AGENTS_ROUTER_DATA_DIR"] = os.path.join(TEST_DATA_DIR, "router")

from src.engine import config as _config  # noqa: E402  (must follow the env pins)

# Same override as evals/runners/persona_server.py. DATA_DIR is otherwise a
# PEP 562 alias; setting it explicitly keeps both names in step.
_config.DATA_DIR = _config.INSTALL_DATA_DIR = TEST_DATA_DIR
_config.AUTO_UPDATE_STAGING_DIR = os.path.join(TEST_DATA_DIR, ".prepared")
