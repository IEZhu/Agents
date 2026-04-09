import os

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

ROUTER_SIMILARITY_THRESHOLD = float(os.getenv("ROUTER_SIMILARITY_THRESHOLD", "0.95"))
# Sticky agent: auto-switch to a different agent without LLM if cosine distance
# is below this value. Intentionally tighter than the router's distance cutoff
# (1 - ROUTER_SIMILARITY_THRESHOLD) — only near-duplicate queries trigger an
# auto-switch; ambiguous cases keep the current agent for stability.
# Tune empirically if switches are too rare.
STICKY_SWITCH_THRESHOLD = 0.02
SKILLS_RELEVANCE_THRESHOLD = float(os.getenv("SKILLS_RELEVANCE_THRESHOLD", "0.75"))
IMPLANTS_RELEVANCE_THRESHOLD = float(os.getenv("IMPLANTS_RELEVANCE_THRESHOLD", "0.80"))
MAX_PREFERRED_IMPLANTS = 5
IMPLANTS_DEEP_TIER_DEFAULT = 3

SESSION_CACHE_MAX_SIZE = 128
SESSION_CACHE_TTL_SECONDS = 600

# Debug logging — set AGENTS_DEBUG=1 in .env to write per-call JSON files to logs/
AGENTS_DEBUG = os.getenv("AGENTS_DEBUG", "").lower() in ("1", "true")
DEBUG_LOG_DIR = os.path.join(REPO_ROOT, "logs")
