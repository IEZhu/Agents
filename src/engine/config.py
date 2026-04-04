import os
import logging

logger = logging.getLogger(__name__)

ENGINE_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(ENGINE_DIR, "../.."))

CHROMA_PATH = os.path.join(REPO_ROOT, "chroma_db")
EMBEDDING_MODEL = "BAAI/bge-m3"


AGENTS_DIR = os.path.join(REPO_ROOT, "agents")
SKILLS_DIR = os.path.join(REPO_ROOT, "skills")
IMPLANTS_DIR = os.path.join(REPO_ROOT, "implants")
CAPABILITIES_FILE = os.path.join(REPO_ROOT, "agents", "capabilities", "registry.yaml")

ROUTER_SIMILARITY_THRESHOLD = 0.95
SKILLS_RELEVANCE_THRESHOLD = 0.55
IMPLANTS_RELEVANCE_THRESHOLD = 0.73

SESSION_CACHE_MAX_SIZE = 128
SESSION_CACHE_TTL_SECONDS = 600

# Debug logging — set AGENTS_DEBUG=1 in .env to write per-call JSON files to logs/
AGENTS_DEBUG = os.getenv("AGENTS_DEBUG", "").lower() in ("1", "true")
DEBUG_LOG_DIR = os.path.join(REPO_ROOT, "logs")
