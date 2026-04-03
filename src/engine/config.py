import os
import logging

logger = logging.getLogger(__name__)

ENGINE_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(ENGINE_DIR, "../.."))

CHROMA_PATH = os.path.join(REPO_ROOT, "chroma_db")
EMBEDDING_MODEL = "BAAI/bge-m3"


def _resolve_path(primary: str, fallback: str) -> str:
    """Use the new path if it exists, fall back to legacy .cursor/ path."""
    if os.path.exists(primary):
        return primary
    if os.path.exists(fallback):
        logger.warning("Using legacy path %s — migrate to %s", fallback, primary)
        return fallback
    return primary


AGENTS_DIR = _resolve_path(
    os.path.join(REPO_ROOT, "agents"),
    os.path.join(REPO_ROOT, ".cursor", "agents"),
)
SKILLS_DIR = _resolve_path(
    os.path.join(REPO_ROOT, "skills"),
    os.path.join(REPO_ROOT, ".cursor", "skills"),
)
IMPLANTS_DIR = _resolve_path(
    os.path.join(REPO_ROOT, "implants"),
    os.path.join(REPO_ROOT, ".cursor", "implants"),
)
CAPABILITIES_FILE = _resolve_path(
    os.path.join(REPO_ROOT, "agents", "capabilities", "registry.yaml"),
    os.path.join(REPO_ROOT, ".cursor", "capabilities", "registry.yaml"),
)

ROUTER_SIMILARITY_THRESHOLD = 0.95
SKILLS_RELEVANCE_THRESHOLD = 0.55
IMPLANTS_RELEVANCE_THRESHOLD = 0.73

SESSION_CACHE_MAX_SIZE = 128
SESSION_CACHE_TTL_SECONDS = 600

# Debug logging — set AGENTS_DEBUG=1 in .env to write per-call JSON files to logs/
AGENTS_DEBUG = os.getenv("AGENTS_DEBUG", "").lower() in ("1", "true")
DEBUG_LOG_DIR = os.path.join(REPO_ROOT, "logs")
