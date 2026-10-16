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

# Target describe-summary word window: requested by the prompt and reported as
# in_word_budget so callers can detect drift; not enforced (write_summary only
# requires RepoDescriber.MIN_PERSIST_WORD_COUNT words and a heading).
DESCRIBE_WORD_MIN = 800
DESCRIBE_WORD_MAX = 1500

# Directory tree depth used to build the describe context bundle.
DESCRIBE_TREE_MAX_DEPTH = 3

# Top of README sampled into the describe context bundle.
DESCRIBE_README_HEAD_LINES = 200

# Size budget of the describe context bundle (#434), in shown units: what a
# text adds to the describe_repo response as Claude Code counts it with
# src.result_size.shown_size. The response JSON-encodes the prompt that holds
# the bundle, and Claude Code escapes it once more and counts UTF-16 code
# units: a quote or backslash costs 4, a newline or tab 3, another control
# character 7, an astral character such as an emoji 2, any other character 1.
# Claude Code saves a result over INLINE_LIMIT (50,000) to a file; 24,000 leave
# room for the prompt template and the instruction.
DESCRIBE_BUNDLE_MAX_CHARS = 24_000
# Section caps cover a section's heading, its code fence and the blank line
# before it, so tree + manifests + README never exceed the total above.
DESCRIBE_TREE_MAX_CHARS = 10_000
# All manifests share one cap; each takes at most DESCRIBE_MANIFEST_MAX_CHARS.
DESCRIBE_MANIFESTS_MAX_CHARS = 6_000
DESCRIBE_MANIFEST_MAX_CHARS = 3_000
DESCRIBE_README_MAX_CHARS = 8_000
# Entries listed per directory before a "… N more entries" note, when the full
# tree does not fit its cap.
DESCRIBE_TREE_MAX_ENTRIES_PER_DIR = 40
# A line that costs more is shortened with an ellipsis when its part is cut.
DESCRIBE_LINE_MAX_CHARS = 1_000

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
