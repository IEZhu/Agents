import logging
import os
import glob
import hashlib
import re
import statistics
import yaml
from src.utils.langfuse_compat import observe
from typing import List, Dict, Any, Optional
from src.utils.prompt_loader import split_frontmatter

import src.engine.config as _cfg
from src.engine.config import IMPLANTS_DIR, IMPLANTS_RELEVANCE_THRESHOLD, DATA_DIR
from src.engine.vector_store import NumpyVectorStore
from src.engine.embedder import embed_texts, embed_query

logger = logging.getLogger(__name__)


def _normalize_triggers(raw: Any) -> List[str]:
    """Coerce a frontmatter ``triggers`` value to a clean lowercase ``list[str]``."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [t.strip().lower() for t in raw if isinstance(t, str) and t.strip()]


def _when_to_use(body: str) -> str:
    """Return the body's "## When to Use" section (without the heading), or ""."""
    m = re.search(r"^## When to Use\s*\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _index_text(description: str, body: str, triggers: List[str]) -> str:
    """Text embedded for an implant, per ``IMPLANT_INDEX_MODE``.

    ``legacy`` embeds the technique body, which describes the method itself and
    so matches conversations *about* the method. ``triggers`` embeds user-side
    cues and the "When to Use" list, which describe the task shape instead.
    """
    if _cfg.IMPLANT_INDEX_MODE == "triggers":
        return f"{description}\n{'; '.join(triggers)}\n\n{_when_to_use(body)}"
    return f"{description}\n\n{body}"


def _trigger_hit(triggers: List[str], query_lower: str) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", query_lower) for t in triggers)

class ImplantRetriever:
    HASH_FILE = os.path.join(DATA_DIR, ".implants_hash")

    def __init__(self):
        self.store = NumpyVectorStore(name="implants_store", data_dir=DATA_DIR)

        changed, self._current_hash = self._needs_reindex()
        # Also reindex if store is empty but .mdc files exist (corrupted/missing store)
        if not changed and self.store.count() == 0:
            if glob.glob(os.path.join(IMPLANTS_DIR, "*.mdc")):
                changed = True
                logger.info("Implants store empty despite hash match — forcing reindex")
        if changed:
            logger.info("Implants changed on disk. Re-indexing...")
            self.index_implants()

    @staticmethod
    def _compute_dir_hash() -> str:
        from src.engine.config import EMBEDDING_MODEL
        h = hashlib.md5()
        from src.engine.fingerprint import fingerprint
        h.update(fingerprint(EMBEDDING_MODEL).encode())
        # The index text depends on the mode, so switching it must reindex.
        h.update(_cfg.IMPLANT_INDEX_MODE.encode())
        for path in sorted(glob.glob(os.path.join(IMPLANTS_DIR, "*.mdc"))):
            # Prepared stores move from a worktree into the live install.
            h.update(os.path.basename(path).encode())
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
            self.store.clear()
            self.store.save()
            self._save_hash(getattr(self, "_current_hash", None))
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
                        frontmatter = yaml.safe_load(fm_str) or {}
                        body = parsed_body
                    except Exception as e:
                        logger.error(f"Failed to parse frontmatter for {file_path}: {e}")

                # Prepare for indexing
                description = frontmatter.get("description", "")
                triggers = _normalize_triggers(frontmatter.get("triggers"))
                full_text = _index_text(description, body, triggers)

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
                    "triggers": triggers,
                })
                ids.append(filename)

                logger.info(f"Indexed implant: {filename}")

            except Exception as e:
                logger.error(f"Error processing implant file {file_path}: {e}")

        if not documents:
            # All files failed to parse — clear stale store
            self.store.clear()
            self.store.save()
            self._save_hash(getattr(self, "_current_hash", None))
            logger.warning("No implants could be parsed — store cleared")
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
        logger.info(f"Successfully indexed {len(documents)} implants.")

    @observe(name="retrieve_implants")
    def retrieve(self, query: str, n_results: int = 3, context: Optional[Dict[str, Any]] = None, role: Optional[str] = None, agent_context: Optional[str] = None, preferred_implants: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Retrieves relevant implants for a given query.
        If preferred_implants are provided, loads them directly (like preferred_skills).
        """
        if self.store.count() == 0:
            return []

        # --- Preferred implants fast-path with semantic top-up ---
        preferred_loaded: list[dict] = []
        preferred_ids_loaded: set[str] = set()
        if preferred_implants:
            # Apply n_results cap before fetching to avoid unnecessary work
            all_target_ids = [
                f"{pi}.mdc" if not pi.endswith(".mdc") else pi
                for pi in preferred_implants
            ]
            target_ids = all_target_ids[:n_results]
            try:
                results = self.store.get(ids=target_ids)
                if results.ids:
                    # Build lookup map: id -> (meta, content) for deterministic ordering
                    lookup = {}
                    for i, implant_id in enumerate(results.ids):
                        meta = results.metadatas[i] or {}
                        content = meta.get('body', results.documents[i])
                        lookup[implant_id] = (meta, content)
                    missing = [tid for tid in target_ids if tid not in lookup]
                    if missing:
                        logger.warning(f"Preferred implants not found in index: {missing}")
                    for tid in target_ids:
                        if tid in lookup:
                            meta, content = lookup[tid]
                            preferred_loaded.append({
                                "filename": tid,
                                "content": content,
                                "metadata": meta,
                                "distance": 0.0,
                            })
                            preferred_ids_loaded.add(tid)
                logger.info(f"Loaded {len(preferred_loaded)}/{len(target_ids)} preferred implants (cap={n_results})")
            except Exception as e:
                logger.warning(f"Failed to retrieve preferred implants {target_ids}: {e}")

            # If all slots filled, return early without semantic search
            if len(preferred_loaded) >= n_results:
                return preferred_loaded

            # Otherwise fall through to semantic search for remaining slots.
            n_results = n_results - len(preferred_loaded)

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
        logger.debug(
            "Retrieving implants: search_len=%d, has_role=%s, has_context=%s",
            len(search_query), bool(role), bool(context),
        )

        query_emb = embed_query(search_query)
        if _cfg.IMPLANT_GATING == "zscore":
            semantic_implants = self._zscore_candidates(
                query, query_emb, n_results, preferred_ids_loaded,
            )
        else:
            semantic_implants = self._legacy_candidates(
                query_emb, n_results, preferred_ids_loaded,
            )

        if semantic_implants:
            names = [(imp["metadata"].get("short_name", imp["filename"]), f"{imp['distance']:.3f}") for imp in semantic_implants]
            logger.info(f"Selected implants: {names}")

        # Prepend preferred implants (if any) before semantic results
        return preferred_loaded + semantic_implants

    def _legacy_candidates(self, query_emb, n_results: int, skip: set[str]) -> List[Dict[str, Any]]:
        """Top-N under the absolute ``IMPLANTS_RELEVANCE_THRESHOLD``."""
        candidates = self.store.query(
            query_embedding=query_emb,
            n_results=min(n_results * 3, self.store.count()),
        )
        out: List[Dict[str, Any]] = []
        if candidates.ids and candidates.distances:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Implant candidates (threshold=%.2f): %s",
                    IMPLANTS_RELEVANCE_THRESHOLD,
                    [(cid, f"{d:.4f}") for cid, d in zip(candidates.ids, candidates.distances)],
                )
            for i, distance in enumerate(candidates.distances):
                cid = candidates.ids[i]
                if distance < IMPLANTS_RELEVANCE_THRESHOLD and cid not in skip:
                    meta = candidates.metadatas[i] or {}
                    out.append({
                        "filename": cid,
                        "content": meta.get("body", candidates.documents[i]),
                        "metadata": meta,
                        "distance": distance,
                    })
        out.sort(key=lambda x: x["distance"])
        return out[:n_results]

    def _zscore_candidates(self, query: str, query_emb, n_results: int, skip: set[str]) -> List[Dict[str, Any]]:
        """Keep implants that stand out from this query's own distance distribution.

        Distances on the current embedder are compressed into a narrow band, so an
        absolute threshold either keeps everything or nothing. Scoring every
        implant and keeping only those ``IMPLANT_GATE_Z`` standard deviations
        closer than the mean lets an ordinary query load no implant at all.
        A literal ``triggers`` hit in the user's query scales the distance by
        ``IMPLANT_TRIGGER_BOOST`` before the comparison.
        """
        total = self.store.count()
        results = self.store.query(query_embedding=query_emb, n_results=total)
        if not results.ids or len(results.distances) < 3:
            return []
        query_lower = query.lower()
        scored = []
        for i, cid in enumerate(results.ids):
            meta = results.metadatas[i] or {}
            d = results.distances[i]
            if _trigger_hit(meta.get("triggers") or [], query_lower):
                d *= _cfg.IMPLANT_TRIGGER_BOOST
            scored.append((d, cid, meta, results.documents[i]))
        dists = [d for d, *_ in scored]
        mean, sd = statistics.fmean(dists), statistics.pstdev(dists)
        if sd == 0:
            return []
        cutoff = mean - _cfg.IMPLANT_GATE_Z * sd
        logger.debug("Implant z-gate: mean=%.4f sd=%.4f cutoff=%.4f", mean, sd, cutoff)
        kept = sorted((x for x in scored if x[0] <= cutoff and x[1] not in skip), key=lambda x: x[0])
        return [
            {"filename": cid, "content": meta.get("body", doc), "metadata": meta, "distance": d}
            for d, cid, meta, doc in kept[:n_results]
        ]

    def get_catalog(self) -> str:
        """Return a compact catalog of all implants (short_name + one_liner).
        Designed for JIT injection: the model sees what's available and can
        request specific implants via load_implants(query=...) or load_implants(task_type=...).
        """
        if self.store.count() == 0:
            return ""

        all_metas = self.store.get_all_metadatas()
        entries: list[str] = []
        for meta in all_metas:
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
