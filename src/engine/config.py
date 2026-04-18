import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ENGINE_DIR = os.path.dirname(__file__)

# --- Install root ------------------------------------------------------------
# Where the Agents-Core source ships: agents/, skills/, implants/, capabilities/
# registry, and vector indexes keyed against those files (router_cache,
# skills_store, implants_store, .router_cache_model). Shared across all client
# repos that reach this install through a global MCP registration.
INSTALL_ROOT = os.path.abspath(os.path.join(ENGINE_DIR, "../.."))
INSTALL_DATA_DIR = os.path.join(INSTALL_ROOT, "data")
AGENTS_DIR = os.path.join(INSTALL_ROOT, "agents")
SKILLS_DIR = os.path.join(INSTALL_ROOT, "skills")
IMPLANTS_DIR = os.path.join(INSTALL_ROOT, "implants")
CAPABILITIES_FILE = os.path.join(INSTALL_ROOT, "agents", "capabilities", "registry.yaml")

# --- Client repo root (per-session, per-repo memory artifacts) ---------------
# Where the serving MCP session's journal lives: history.md, history/ archive,
# managed CLAUDE.md section, describe_hash, history_store vector index, and
# AGENTS_DEBUG logs. Resolved lazily so a single install serving many client
# repos keeps their memory isolated (issue #36).

_CLIENT_ROOT_MARKERS = (".git", "CLAUDE.md")


def _find_marker_upwards(start: Path) -> Optional[Path]:
    """Walk up from *start* until a directory containing any of
    `_CLIENT_ROOT_MARKERS` is found. Returns that directory, or None."""
    try:
        current = start.resolve(strict=False)
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in _CLIENT_ROOT_MARKERS):
            return candidate
    return None


@lru_cache(maxsize=1)
def get_client_repo_root() -> str:
    """Resolve the client repo that owns this MCP session's per-repo memory.

    Resolution order:
      1. `AGENTS_CLIENT_REPO_ROOT` env var — authoritative override.
      2. Walk up from `os.getcwd()` to the nearest directory containing
         `.git` or `CLAUDE.md`.
      3. Fallback: `os.getcwd()`.

    Memoized for the process lifetime. Tests reset via
    `_reset_client_repo_root_cache()`.
    """
    override = os.environ.get("AGENTS_CLIENT_REPO_ROOT")
    if override:
        resolved = os.path.realpath(os.path.expanduser(override))
        # Debug-level so the server's INFO-configured root logger stays quiet
        # on normal startup; AGENTS_DEBUG=1 surfaces these when diagnosing.
        logger.debug("client-repo-root: env override -> %s", resolved)
        return resolved

    # `os.getcwd()` raises FileNotFoundError when the process' cwd has been
    # deleted (long-running daemons started from ephemeral dirs). Without
    # this guard the first memory-tool call would crash the whole session.
    # Fall back to INSTALL_ROOT and warn loudly so the anomaly is visible.
    try:
        cwd = Path(os.getcwd())
    except (FileNotFoundError, OSError) as err:
        logger.warning(
            "client-repo-root: cwd unavailable (%s); falling back to INSTALL_ROOT. "
            "Set AGENTS_CLIENT_REPO_ROOT to pin the per-session memory target.",
            err,
        )
        return INSTALL_ROOT

    marker = _find_marker_upwards(cwd)
    if marker is not None:
        logger.debug("client-repo-root: walk-up marker -> %s", marker)
        return str(marker)

    try:
        fallback = str(cwd.resolve())
    except OSError as err:
        logger.warning(
            "client-repo-root: cwd resolve failed (%s); falling back to INSTALL_ROOT.",
            err,
        )
        return INSTALL_ROOT
    logger.debug("client-repo-root: fallback cwd -> %s", fallback)
    return fallback


def _reset_client_repo_root_cache() -> None:
    """Clear the memoization of `get_client_repo_root()`. Test-only."""
    get_client_repo_root.cache_clear()


def get_client_data_dir() -> str:
    """`{client_repo_root}/data` — per-repo `history_store` and `.describe_hash`."""
    return os.path.join(get_client_repo_root(), "data")


def get_debug_log_dir() -> str:
    """`{client_repo_root}/logs` — per-call JSON debug logs (when `AGENTS_DEBUG=1`)."""
    return os.path.join(get_client_repo_root(), "logs")


# --- Embedding / model config ------------------------------------------------

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

# fastembed cache — persistent by default (macOS launchd wipes /tmp during long downloads).
FASTEMBED_CACHE_DIR = os.path.expanduser(os.getenv("FASTEMBED_CACHE_DIR", "~/.cache/fastembed"))


def _float_env(name: str, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Parse a float from an env var, falling back to *default* on bad values or out-of-range."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid value for %s=%r, using default %.2f", name, raw, default)
        return default
    if not (lo <= value <= hi):
        logger.warning("Out-of-range value for %s=%.4f (expected %.1f–%.1f), using default %.2f", name, value, lo, hi, default)
        return default
    return value


ROUTER_SIMILARITY_THRESHOLD = _float_env("ROUTER_SIMILARITY_THRESHOLD", 0.95)
# Sticky agent: auto-switch to a different agent without LLM if cosine distance
# is below this value. Intentionally tighter than the router's distance cutoff
# (1 - ROUTER_SIMILARITY_THRESHOLD) — only near-duplicate queries trigger an
# auto-switch; ambiguous cases keep the current agent for stability.
# Tune empirically if switches are too rare.
STICKY_SWITCH_THRESHOLD = 0.02

# Keyword boosting: minimum keyword hits to consider overriding a cache decision
KEYWORD_OVERRIDE_MIN_HITS = 1
# Top agent must have >= this ratio vs second-best to auto-override
# (otherwise falls through to ROUTE_REQUIRED for LLM re-evaluation)
KEYWORD_UNIQUENESS_RATIO = 2.0
# Cosine distance thresholds (1 - similarity). Calibrated for
# paraphrase-multilingual-MiniLM-L12-v2; typical distances:
#   skills  0.39–0.63  → threshold 0.75 (comfortable margin)
#   implants 0.52–0.72 → threshold 0.85 (implant descriptions are more
#                         abstract, so distances run ~0.1 higher)
SKILLS_RELEVANCE_THRESHOLD = _float_env("SKILLS_RELEVANCE_THRESHOLD", 0.75)
IMPLANTS_RELEVANCE_THRESHOLD = _float_env("IMPLANTS_RELEVANCE_THRESHOLD", 0.85)
MAX_PREFERRED_IMPLANTS = 5
IMPLANTS_DEEP_TIER_DEFAULT = 3

SESSION_CACHE_MAX_SIZE = 128
SESSION_CACHE_TTL_SECONDS = 600

# Debug logging — set AGENTS_DEBUG=1 in .env to write per-call JSON files to logs/
AGENTS_DEBUG = os.getenv("AGENTS_DEBUG", "").lower() in ("1", "true")


# --- Deprecated name aliases (PEP 562) ---------------------------------------
# Issue #36: `REPO_ROOT`/`DATA_DIR`/`DEBUG_LOG_DIR` used to conflate the Agents
# install directory with the client repo whose memory we write to. They now
# split into INSTALL_ROOT + get_client_repo_root()/get_debug_log_dir(). These
# aliases keep external `from src.engine.config import REPO_ROOT` callsites
# working without churn; prefer the explicit names in new code.
def __getattr__(name: str):
    if name == "REPO_ROOT":
        return INSTALL_ROOT
    if name == "DATA_DIR":
        return INSTALL_DATA_DIR
    if name == "DEBUG_LOG_DIR":
        return get_debug_log_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
