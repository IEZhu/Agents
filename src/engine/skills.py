import logging
import os
import glob
import hashlib
import yaml
from src.utils.langfuse_compat import observe
from typing import List, Dict, Any
from src.utils.prompt_loader import split_frontmatter

from src.engine.config import SKILLS_DIR, SKILLS_RELEVANCE_THRESHOLD, DATA_DIR
from src.engine.vector_store import NumpyVectorStore
from src.engine.embedder import embed_texts, embed_query

logger = logging.getLogger(__name__)

class SkillRetriever:
    HASH_FILE = os.path.join(DATA_DIR, ".skills_hash")

    def __init__(self):
        self.store = NumpyVectorStore(name="skills_store", data_dir=DATA_DIR)

        changed, self._current_hash = self._needs_reindex()
        if changed:
            logger.info("Skills changed on disk. Re-indexing...")
            self.index_skills()

    @staticmethod
    def _compute_dir_hash() -> str:
        h = hashlib.md5()
        for path in sorted(glob.glob(os.path.join(SKILLS_DIR, "*.mdc"))):
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

    def index_skills(self):
        """
        Reads all .mdc files in SKILLS_DIR and indexes them.
        """
        skill_files = glob.glob(os.path.join(SKILLS_DIR, "*.mdc"))

        if not skill_files:
            logger.warning(f"No skill files found in {SKILLS_DIR}")
            return

        documents = []
        metadatas = []
        ids = []

        for file_path in skill_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # Parse frontmatter
                frontmatter = {}
                body = content

                fm_str, parsed_body = split_frontmatter(content)
                if fm_str is not None:
                    try:
                        frontmatter = yaml.safe_load(fm_str) or {}
                        body = parsed_body
                    except Exception as e:
                        logger.error(f"Failed to parse frontmatter for {file_path}: {e}")

                # Prepare for indexing
                # We index the full content (description + body) for better retrieval
                description = frontmatter.get("description", "")
                full_text = f"{description}\n\n{body}"

                filename = os.path.basename(file_path)

                compiled = frontmatter.get("compiled", "")

                documents.append(full_text)
                metadatas.append({
                    "filename": filename,
                    "description": description,
                    "path": file_path,
                    "body": body,
                    "compiled": compiled,
                })
                ids.append(filename)

                logger.info(f"Indexed skill: {filename}")

            except Exception as e:
                logger.error(f"Error processing skill file {file_path}: {e}")

        if documents:
            embeddings = embed_texts(documents)
            self.store.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )
            self.store.save()
            self._save_hash(getattr(self, "_current_hash", None))
            logger.info(f"Successfully indexed {len(documents)} skills.")

    @observe(name="retrieve_skills")
    def retrieve(self, query: str, n_results: int = 2, preferred_skills: List[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieves relevant skills for a given query.
        If preferred_skills are provided, loads them instead of vector search.
        """
        if self.store.count() == 0:
            return []

        skills = []

        if preferred_skills:
            target_ids = [
                f"{ps}.mdc" if not ps.endswith(".mdc") else ps
                for ps in preferred_skills
            ]
            try:
                results = self.store.get(ids=target_ids)
                if results.ids:
                    for i, skill_id in enumerate(results.ids):
                        meta = results.metadatas[i] or {}
                        content = meta.get('body', results.documents[i])
                        skills.append({
                            "filename": skill_id,
                            "content": content,
                            "metadata": meta,
                            "distance": 0.0,
                        })
                logger.info(f"Loaded {len(skills)} preferred skills: {target_ids}")
                return skills
            except Exception as e:
                logger.warning(f"Failed to retrieve preferred skills {target_ids}: {e}")

        query_emb = embed_query(query)
        results = self.store.query(query_embedding=query_emb, n_results=n_results)

        if results.ids and results.distances:
            for i, distance in enumerate(results.distances):
                if distance < SKILLS_RELEVANCE_THRESHOLD:
                    meta = results.metadatas[i] or {}
                    content = meta.get('body', results.documents[i])
                    skills.append({
                        "filename": results.ids[i],
                        "content": content,
                        "metadata": meta,
                        "distance": distance,
                    })

        return skills

    def format_skills_for_prompt(self, skills: List[Dict[str, Any]], compiled: bool = False) -> str:
        """Formats retrieved skills for injection into the system prompt.
        When compiled=True, uses the compressed one-liner version if available.
        """
        if not skills:
            return ""

        if compiled:
            lines = ["## Skills (compiled)"]
            for skill in skills:
                meta = skill.get("metadata", {})
                c = meta.get("compiled", "")
                if c:
                    lines.append(f"- **{meta.get('filename', '?')}**: {c}")
                else:
                    lines.append(f"- **{meta.get('filename', '?')}**: {meta.get('description', '')}")
            return "\n".join(lines)

        formatted = "## Dynamic Skills (Contextually Loaded)\n"
        formatted += "The following specialized skills have been loaded to help with the request:\n\n"
        for skill in skills:
            meta = skill.get("metadata", {})
            desc = meta.get("description", "No description")
            content = skill.get("content", "")
            formatted += f"### Skill: {meta.get('filename')}\n"
            formatted += f"**Description**: {desc}\n"
            formatted += f"{content}\n\n"
        return formatted
