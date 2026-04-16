"""Constants for the repository memory subsystem.

All paths are derived from REPO_ROOT / DATA_DIR exposed by the engine config,
so the memory tools pick up overrides made elsewhere automatically.
"""

import os

from src.engine.config import DATA_DIR, REPO_ROOT

# --- Filesystem layout -------------------------------------------------------

# Vector store + hash file live under data/memory so they share the same
# git-ignored data root as the routing/skills/implants stores.
MEMORY_DATA_DIR = os.path.join(DATA_DIR, "memory")

# Repo-root markdown artifacts (git-ignored by default — see .gitignore).
HISTORY_FILE = os.path.join(REPO_ROOT, "history.md")
HISTORY_ARCHIVE_DIR = os.path.join(REPO_ROOT, "history")
CLAUDE_MD_FILE = os.path.join(REPO_ROOT, "CLAUDE.md")

# Hash file used by RepoDescriber to skip work when the repo hasn't changed.
DESCRIBE_HASH_FILE = os.path.join(MEMORY_DATA_DIR, ".describe_hash")

# Vector store for lazy semantic recall over history entries.
HISTORY_VECTOR_STORE_NAME = "history_store"

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
