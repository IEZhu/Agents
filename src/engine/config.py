import os

ENGINE_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(ENGINE_DIR, "../.."))

CHROMA_PATH = os.path.join(REPO_ROOT, "chroma_db")
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

SKILLS_DIR = os.path.join(REPO_ROOT, ".cursor", "skills")
IMPLANTS_DIR = os.path.join(REPO_ROOT, ".cursor", "implants")

ROUTER_SIMILARITY_THRESHOLD = 0.95
SKILLS_RELEVANCE_THRESHOLD = 0.45
IMPLANTS_RELEVANCE_THRESHOLD = 0.73

SESSION_CACHE_MAX_SIZE = 128
SESSION_CACHE_TTL_SECONDS = 600
