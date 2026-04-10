"""Pre-download embedding model and pre-index ChromaDB.

Usage: python chroma_preindex.py <repo_root>

Without this, the first MCP server startup takes 30-60s for embedding
generation, causing Claude Desktop to time out.
"""
import sys
import os


def main():
    repo_root = sys.argv[1]
    sys.path.insert(0, repo_root)

    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("BAAI/bge-m3")
        print("Embedding model ready", flush=True)

        print("Indexing skills...", flush=True)
        from src.engine.skills import SkillRetriever

        sr = SkillRetriever()
        print(f"Skills indexed: {sr.collection.count()} entries", flush=True)

        print("Indexing implants...", flush=True)
        from src.engine.implants import ImplantRetriever

        ir = ImplantRetriever()
        print(f"Implants indexed: {ir.collection.count()} entries", flush=True)
    except Exception as e:
        print(f"Warning: Pre-indexing failed: {e}", flush=True)
        print("It will run on first MCP server start", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
