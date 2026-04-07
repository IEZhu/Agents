import os

ENGINE_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(ENGINE_DIR, "../.."))

CHROMA_PATH = os.path.join(REPO_ROOT, "chroma_db")
EMBEDDING_MODEL = "BAAI/bge-m3"


AGENTS_DIR = os.path.join(REPO_ROOT, "agents")
SKILLS_DIR = os.path.join(REPO_ROOT, "skills")
IMPLANTS_DIR = os.path.join(REPO_ROOT, "implants")
CAPABILITIES_FILE = os.path.join(REPO_ROOT, "agents", "capabilities", "registry.yaml")

ROUTER_SIMILARITY_THRESHOLD = 0.95
# Sticky agent: auto-switch to a different agent without LLM if cosine distance
# is below this value. Intentionally tighter than the router's distance cutoff
# (1 - ROUTER_SIMILARITY_THRESHOLD) — only near-duplicate queries trigger an
# auto-switch; ambiguous cases keep the current agent for stability.
# Tune empirically if switches are too rare.
STICKY_SWITCH_THRESHOLD = 0.02
SKILLS_RELEVANCE_THRESHOLD = 0.55
IMPLANTS_RELEVANCE_THRESHOLD = 0.73
MAX_PREFERRED_IMPLANTS = 5
IMPLANTS_DEEP_TIER_DEFAULT = 3

SESSION_CACHE_MAX_SIZE = 128
SESSION_CACHE_TTL_SECONDS = 600

# Debug logging — set AGENTS_DEBUG=1 in .env to write per-call JSON files to logs/
AGENTS_DEBUG = os.getenv("AGENTS_DEBUG", "").lower() in ("1", "true")
DEBUG_LOG_DIR = os.path.join(REPO_ROOT, "logs")
