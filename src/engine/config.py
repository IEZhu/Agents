import logging
import os

logger = logging.getLogger(__name__)

ENGINE_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(ENGINE_DIR, "../.."))

# Embedding model (set via .env or init_repo.sh)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

# Persistent storage for vector stores
DATA_DIR = os.path.join(REPO_ROOT, "data")

AGENTS_DIR = os.path.join(REPO_ROOT, "agents")
SKILLS_DIR = os.path.join(REPO_ROOT, "skills")
IMPLANTS_DIR = os.path.join(REPO_ROOT, "implants")
CAPABILITIES_FILE = os.path.join(REPO_ROOT, "agents", "capabilities", "registry.yaml")


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
DEBUG_LOG_DIR = os.path.join(REPO_ROOT, "logs")
