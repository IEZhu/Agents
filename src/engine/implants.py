import logging
import os
import glob
import hashlib
import yaml
from src.utils.langfuse_compat import observe
from typing import List, Dict, Any, Optional
from src.utils.prompt_loader import split_frontmatter

from src.engine.config import IMPLANTS_DIR, IMPLANTS_RELEVANCE_THRESHOLD, CHROMA_PATH
from src.engine.chroma import get_chroma_client, get_embedding_fn

logger = logging.getLogger(__name__)

class ImplantRetriever:
    HASH_FILE = os.path.join(CHROMA_PATH, ".implants_hash")

    def __init__(self):
        self.chroma_client = get_chroma_client()
        self.embedding_fn = get_embedding_fn()

        self.collection = self.chroma_client.get_or_create_collection(
            name="implants_store",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

        changed, self._current_hash = self._needs_reindex()
        if changed:
            logger.info("Implants changed on disk. Re-indexing...")
            self.index_implants()

    @staticmethod
    def _compute_dir_hash() -> str:
        h = hashlib.md5()
        for path in sorted(glob.glob(os.path.join(IMPLANTS_DIR, "*.mdc"))):
            h.update(path.encode())
            with open(path, "rb") as f:
                h.update(f.read())
        return h.hexdigest()

    def _needs_reindex(self) -> tuple[bool, str]:
        current = self._compute_dir_hash()
        if not os.path.exists(self.HASH_FILE):
            return True, current
        try:
            with open(self.HASH_FILE, "r") as f:
                return f.read().strip() != current, current
        except Exception:
            return True, current

    def _save_hash(self, digest: str = None):
        os.makedirs(os.path.dirname(self.HASH_FILE), exist_ok=True)
        with open(self.HASH_FILE, "w") as f:
            f.write(digest or self._compute_dir_hash())

    def index_implants(self):
        """
        Reads all .mdc files in IMPLANTS_DIR and indexes them.
        """
        implant_files = glob.glob(os.path.join(IMPLANTS_DIR, "*.mdc"))

        if not implant_files:
            logger.warning(f"No implant files found in {IMPLANTS_DIR}")
            return

        documents = []
        metadatas = []
        ids = []

        for file_path in implant_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # Parse frontmatter
                frontmatter = {}
                body = content

                fm_str, parsed_body = split_frontmatter(content)
                if fm_str is not None:
                    try:
                        frontmatter = yaml.safe_load(fm_str)
                        body = parsed_body
                    except Exception as e:
                        logger.error(f"Failed to parse frontmatter for {file_path}: {e}")

                # Prepare for indexing
                description = frontmatter.get("description", "")
                full_text = f"{description}\n\n{body}"

                filename = os.path.basename(file_path)

                short_name = frontmatter.get("short_name", "")
                one_liner = frontmatter.get("one_liner", "")

                documents.append(full_text)
                metadatas.append({
                    "filename": filename,
                    "description": description,
                    "path": file_path,
                    "body": body,
                    "short_name": short_name,
                    "one_liner": one_liner,
                })
                ids.append(filename)

                logger.info(f"Indexed implant: {filename}")

            except Exception as e:
                logger.error(f"Error processing implant file {file_path}: {e}")

        if documents:
            self.collection.upsert(
                documents=documents,
                metadatas=metadatas,
                ids=ids
            )
            self._save_hash(getattr(self, "_current_hash", None))
            logger.info(f"Successfully indexed {len(documents)} implants.")

    @observe(name="retrieve_implants")
    def retrieve(self, query: str, n_results: int = 3, context: Dict[str, Any] = None, role: Optional[str] = None, agent_context: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieves relevant implants for a given query.
        """
        if self.collection.count() == 0:
            return []

        # Handle legacy agent_context param (treat as role)
        if agent_context and not role:
            role = agent_context

        search_parts = [f"Query: {query}"]

        if role:
            search_parts.append(f"Role: {role}")

        if context:
            history_text = context.get("history_text", "")
            if history_text:
                # Truncate history to avoid too long query
                search_parts.append(f"Context: {history_text[-300:]}")

        search_query = "\n".join(search_parts)
        logger.info(f"Retrieving implants with query: {search_query}")

        candidates = self.collection.query(
            query_texts=[search_query],
            n_results=min(n_results * 3, self.collection.count()),
        )

        implants = []

        if candidates['ids'] and candidates['distances']:
            for i, distance in enumerate(candidates['distances'][0]):
                if distance < IMPLANTS_RELEVANCE_THRESHOLD:
                    meta = candidates['metadatas'][0][i] or {}
                    content = meta.get('body', candidates['documents'][0][i])
                    implants.append({
                        "filename": candidates['ids'][0][i],
                        "content": content,
                        "metadata": meta,
                        "distance": distance,
                    })

        implants.sort(key=lambda x: x["distance"])
        implants = implants[:n_results]

        if implants:
            names = [(imp["metadata"].get("short_name", imp["filename"]), f"{imp['distance']:.3f}") for imp in implants]
            logger.info(f"Selected implants: {names}")

        return implants

    def get_catalog(self) -> str:
        """Return a compact catalog of all implants (short_name + one_liner).
        Designed for JIT injection: the model sees what's available and can
        request specific implants via load_implants(query=...) or load_implants(task_type=...).
        """
        if self.collection.count() == 0:
            return ""

        all_data = self.collection.get(include=["metadatas"])
        entries: list[str] = []
        for meta in (all_data.get("metadatas") or []):
            short = meta.get("short_name") or meta.get("filename", "?")
            liner = meta.get("one_liner") or meta.get("description", "")
            entries.append(f"{short}({liner})")

        header = (
            "## Available Reasoning Implants (call load_implants to load)\n"
        )
        return header + ", ".join(sorted(entries))

    def format_implants_for_prompt(self, implants: List[Dict[str, Any]]) -> str:
        """Formats retrieved implants for injection into the system prompt."""
        if not implants:
            return ""

        formatted = "## Dynamic Implants (Contextually Loaded)\n"
        formatted += "The following cognitive implants have been loaded to augment reasoning:\n\n"

        for implant in implants:
            meta = implant.get("metadata", {})
            desc = meta.get("description", "No description")
            content = implant.get("content", "")

            formatted += f"### Implant: {meta.get('filename')}\n"
            formatted += f"**Description**: {desc}\n"
            formatted += f"{content}\n\n"

        return formatted
