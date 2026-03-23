import threading

import chromadb
from chromadb.utils import embedding_functions

from src.engine.config import CHROMA_PATH, EMBEDDING_MODEL

_lock = threading.Lock()
_client = None
_embedding_fn = None

def get_chroma_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client

def get_embedding_fn():
    global _embedding_fn
    if _embedding_fn is None:
        with _lock:
            if _embedding_fn is None:
                _embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=EMBEDDING_MODEL
                )
    return _embedding_fn
