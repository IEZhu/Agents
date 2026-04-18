"""Constants for the repository memory subsystem.

Path constants (``HISTORY_FILE``, ``HISTORY_ARCHIVE_DIR``, ``CLAUDE_MD_FILE``,
``DESCRIBE_HASH_FILE``, ``MEMORY_DATA_DIR``) are **client-repo-scoped** — each
MCP session resolves them against ``src.engine.config.get_client_repo_root()``,
not against the Agents install directory. One global Agents-Core install can
then serve many client repos with isolated per-repo memory (issue #36).

The path names are exposed via PEP 562 ``__getattr__`` so tests that swap the
client root via ``_reset_client_repo_root_cache()`` see the updated value on
the next attribute access.
"""

import os

from src.engine.config import get_client_data_dir, get_client_repo_root

# --- Managed section markers (CLAUDE.md) -------------------------------------

# Kept distinct from the Routing-Protocol markers managed by init_repo.sh
# so the two managed sections never collide.
DESCRIBE_MARKER_BEGIN = "# >>> Agents-Core Repository Memory (managed by describe_repo) >>>"
DESCRIBE_MARKER_END = "# <<< Agents-Core Repository Memory (managed by describe_repo) <<<"

# --- Tunables ----------------------------------------------------------------

# Rotation threshold for history.md before it is moved to history/YYYY-MM.md.
# Override via env for testing: HISTORY_ROTATION_THRESHOLD_KB=1
HISTORY_ROTATION_THRESHOLD_KB = int(os.environ.get("HISTORY_ROTATION_THRESHOLD_KB", "512"))

# Tail-scan window for content-hash deduplication on append.
HISTORY_DEDUP_TAIL_SIZE = 50

# Schema version stamped into the history.md frontmatter.
HISTORY_FORMAT_VERSION = 1

# Acceptable describe-summary word count window — enforced by tests, surfaced
# in the response so callers can detect drift.
DESCRIBE_WORD_MIN = 800
DESCRIBE_WORD_MAX = 1500

# Directory tree depth used to build the describe context bundle.
DESCRIBE_TREE_MAX_DEPTH = 3

# Top of README sampled into the describe context bundle.
DESCRIBE_README_HEAD_LINES = 200

# Directories excluded from the describe tree walk and the repo hash.
DESCRIBE_EXCLUDED_DIRS = frozenset({
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "data",
    "logs",
    "dist",
    "build",
    ".obsidian",
    ".idea",
    ".vscode",
    "history",  # archived history files — describe doesn't care
})

# --- Vector store name (not a path) ------------------------------------------

HISTORY_VECTOR_STORE_NAME = "history_store"


# --- Lazy, client-scoped paths (PEP 562) -------------------------------------

def __getattr__(name: str):
    if name == "MEMORY_DATA_DIR":
        return os.path.join(get_client_data_dir(), "memory")
    if name == "HISTORY_FILE":
        return os.path.join(get_client_repo_root(), "history.md")
    if name == "HISTORY_ARCHIVE_DIR":
        return os.path.join(get_client_repo_root(), "history")
    if name == "CLAUDE_MD_FILE":
        return os.path.join(get_client_repo_root(), "CLAUDE.md")
    if name == "DESCRIBE_HASH_FILE":
        return os.path.join(get_client_data_dir(), "memory", ".describe_hash")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
