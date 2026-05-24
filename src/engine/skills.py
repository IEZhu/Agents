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
        # Also reindex if store is empty but .mdc files exist (corrupted/missing store)
        if not changed and self.store.count() == 0:
            if glob.glob(os.path.join(SKILLS_DIR, "*.mdc")):
                changed = True
                logger.info("Skills store empty despite hash match — forcing reindex")
        if changed:
            logger.info("Skills changed on disk. Re-indexing...")
            self.index_skills()

    @staticmethod
    def _compute_dir_hash() -> str:
        from src.engine.config import EMBEDDING_MODEL
        h = hashlib.md5()
        h.update(EMBEDDING_MODEL.encode())
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
            self.store.clear()
            self.store.save()
            self._save_hash(getattr(self, "_current_hash", None))
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

                # Prepare for indexing — concat description + keywords + body
                # so keywords contribute to the embedding similarity, alongside
                # being available verbatim in metadata for keyword-bonus scoring.
                description = frontmatter.get("description", "")
                compiled = frontmatter.get("compiled", "")
                keywords = frontmatter.get("keywords", []) or []
                filename = os.path.basename(file_path)
                full_text = f"{description}\n{' '.join(keywords)}\n\n{body}"

                documents.append(full_text)
                metadatas.append({
                    "filename": filename,
                    "description": description,
                    "path": file_path,
                    "body": body,
                    "compiled": compiled,
                    "keywords": keywords,
                })
                ids.append(filename)

                logger.info(f"Indexed skill: {filename}")

            except Exception as e:
                logger.error(f"Error processing skill file {file_path}: {e}")

        if not documents:
            # All files failed to parse — clear stale store
            self.store.clear()
            self.store.save()
            self._save_hash(getattr(self, "_current_hash", None))
            logger.warning("No skills could be parsed — store cleared")
            return

        embeddings = embed_texts(documents)
        self.store.replace(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        self.store.save()
        self._save_hash(getattr(self, "_current_hash", None))
        logger.info(f"Successfully indexed {len(documents)} skills.")

    @staticmethod
    def _to_id(name: str) -> str:
        """Normalize skill name to its storage ID (filename with .mdc)."""
        return name if name.endswith(".mdc") else f"{name}.mdc"

    @observe(name="retrieve_skills")
    def retrieve(
        self,
        query: str,
        *,
        mandatory: List[str] | None = None,
        preferred: List[str] | None = None,
        capable: List[str] | None = None,
        n_results: int = 2,
        boost_factor: float = 0.7,
        keyword_boost: float = 0.85,
    ) -> List[Dict[str, Any]]:
        """3-tier retrieval for the per-agent skill model.

        Args:
            query: User query for semantic ranking.
            mandatory: Skills loaded unconditionally (core_skills for the agent).
            preferred: Skills participating in semantic search with a distance
                boost — ``distance × boost_factor`` (smaller distance = closer
                match). Use for skills the agent uses often.
            capable: Skills participating in semantic search with base distance.
                Use for the broader pool of skills that might apply to specific
                sub-queries.
            n_results: Max additional skills from the semantic pool, on top of
                ``mandatory``.
            boost_factor: Multiplier applied to distance of preferred skills.
                Values < 1.0 increase their effective rank. Default 0.7.
            keyword_boost: Multiplier applied when any skill ``keywords`` entry
                literally appears in the query (case-insensitive). Default 0.85.

        Returns:
            List of skill dicts with ``filename``, ``content``, ``metadata``,
            ``distance``, and ``tier`` ("mandatory" / "preferred" / "capable").
            Order: mandatory first (declaration order), then top-N semantic
            results sorted by adjusted distance.
        """
        if self.store.count() == 0:
            return []

        mandatory = mandatory or []
        preferred = preferred or []
        capable = capable or []

        skills: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # --- 1) Mandatory: load by ID, no scoring -----------------------------
        if mandatory:
            target_ids = [self._to_id(s) for s in mandatory]
            try:
                results = self.store.get(ids=target_ids)
                for i, sid in enumerate(results.ids):
                    if sid in seen_ids:
                        continue
                    meta = results.metadatas[i] or {}
                    skills.append({
                        "filename": sid,
                        "content": meta.get("body", results.documents[i]),
                        "metadata": meta,
                        "distance": 0.0,
                        "tier": "mandatory",
                    })
                    seen_ids.add(sid)
                if len(results.ids) < len(target_ids):
                    missing = [tid for tid in target_ids if tid not in set(results.ids)]
                    logger.warning(f"Mandatory skills not found in store: {missing}")
            except Exception as e:
                logger.warning(f"Failed to load mandatory skills {target_ids}: {e}")

        # --- 2) Semantic pool (preferred ∪ capable), filter, score, rank ------
        if n_results <= 0:
            return skills

        preferred_ids = {self._to_id(s) for s in preferred}
        capable_ids = {self._to_id(s) for s in capable}
        pool_ids = (preferred_ids | capable_ids) - seen_ids
        if not pool_ids:
            return skills

        query_emb = embed_query(query)
        query_lower = query.lower()
        # Query a wide window then filter to pool — store may have more skills.
        results = self.store.query(query_embedding=query_emb, n_results=self.store.count())
        if not results.ids or not results.distances:
            return skills

        scored: list[tuple[float, str, dict, str]] = []
        for i, sid in enumerate(results.ids):
            if sid not in pool_ids:
                continue
            d = results.distances[i]
            meta = results.metadatas[i] or {}
            tier_label = "preferred" if sid in preferred_ids else "capable"
            if tier_label == "preferred":
                d *= boost_factor
            # Keyword boost: any literal keyword present in query lowers distance further.
            for kw in meta.get("keywords", []) or []:
                if kw and isinstance(kw, str) and kw.lower() in query_lower:
                    d *= keyword_boost
                    break
            if d < SKILLS_RELEVANCE_THRESHOLD:
                scored.append((d, sid, meta, results.documents[i]))

        scored.sort(key=lambda x: x[0])
        for d, sid, meta, doc in scored[:n_results]:
            if sid in seen_ids:
                continue
            tier_label = "preferred" if sid in {self._to_id(s) for s in preferred} else "capable"
            skills.append({
                "filename": sid,
                "content": meta.get("body", doc),
                "metadata": meta,
                "distance": d,
                "tier": tier_label,
            })
            seen_ids.add(sid)

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
