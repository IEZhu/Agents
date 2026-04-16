"""One-shot repository bootstrap: build a context bundle, ask the calling LLM
to summarize it via MCP sampling, and persist the summary into the managed
Repository Memory section of CLAUDE.md.

The describer never calls an LLM directly. It builds the prompt, the caller
performs ``ctx.session.create_message(...)`` (mirroring the pattern in
``src/server.py:196``), and the result is fed back into ``write_summary``.
This split keeps the module unit-testable without an MCP context.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from src.engine.config import REPO_ROOT
from src.memory import managed_section
from src.memory.config import (
    CLAUDE_MD_FILE,
    DESCRIBE_EXCLUDED_DIRS,
    DESCRIBE_HASH_FILE,
    DESCRIBE_MARKER_BEGIN,
    DESCRIBE_MARKER_END,
    DESCRIBE_README_HEAD_LINES,
    DESCRIBE_TREE_MAX_DEPTH,
    DESCRIBE_WORD_MAX,
    DESCRIBE_WORD_MIN,
)

logger = logging.getLogger(__name__)


# Files we always sample into the context bundle if present at the repo root.
_KEY_MANIFEST_FILES = (
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "requirements.txt",
    "Pipfile",
    "Gemfile",
    "composer.json",
)

# Files written *by* the memory subsystem itself — must not feed into the hash
# or each refresh would invalidate its own cache.
_HASH_EXCLUDED_FILES = frozenset({
    "CLAUDE.md",
    "history.md",
    ".describe_hash",
})


DESCRIBE_PROMPT_TEMPLATE = """\
You are a **Repository Analyst** performing a one-time deep study of a codebase.
Your output will be saved into CLAUDE.md as the project memory and read by every
future Claude session, so future sessions can work effectively without re-exploring
the codebase.

## Task
Produce a compressed, LLM-consumable repository overview for `{REPO_NAME}`.

## Input (CONTEXT_BUNDLE)
{CONTEXT_BUNDLE}

## Output Format
Produce exactly these sections in this order. Use compressed markdown
(BLUF, atomic paragraphs, no filler).

### Project Identity
One paragraph: name, purpose, primary language, framework, package manager.

### Tech Stack
Bulleted list of key dependencies with version and purpose. Max 15 items.

### Entry Points
Table: path | purpose. Include: main, tests, config, CI/CD, scripts.

### Module Map
Depth-2 directory listing. One line per directory: `path/ — purpose`.
Skip generated/vendor dirs.

### Conventions
Bulleted list: naming, imports, error handling, logging, frontmatter format,
code style.

### Key Workflows
For each script / Make target / npm script: `name — what it does`. Max 10.

### Architecture Patterns
2–3 paragraphs: data flow, key abstractions, dependency injection style,
async patterns.

### Test Strategy
Runner, fixture patterns, mock strategy, coverage config. One paragraph.

### Gotchas
Bulleted list of non-obvious things: env vars needed, startup order,
known limitations, common mistakes.

### Glossary
Table: term | definition. Domain-specific terms only. Max 15.

## Rules
- BLUF: lead every section with the most important fact.
- Compress: no filler ("this project is", "in order to", "it is worth noting").
- Atomic: one paragraph = one idea.
- Concrete: cite file paths and line numbers, not vague references.
- Skip empty sections rather than writing "N/A".
- Total output: {WORD_MIN}–{WORD_MAX} words. Exceeding wastes context; under-shooting loses info.
- Output ONLY the markdown sections above. No preamble, no closing remarks.
"""


@dataclass
class DescribeDecision:
    """Outcome of ``RepoDescriber.plan()``."""
    needs_refresh: bool
    current_hash: str
    cached_hash: Optional[str]
    cached_summary: Optional[str]


class RepoDescriber:
    """Build the describe prompt, write the resulting summary back to CLAUDE.md.

    Splitting the work into ``plan()`` / ``build_prompt()`` / ``write_summary()``
    keeps the LLM-facing call (``ctx.session.create_message``) outside the class
    so unit tests don't need to fake an MCP context.
    """

    def __init__(
        self,
        repo_path: Optional[str] = None,
        claude_md_path: Optional[str] = None,
        hash_file: Optional[str] = None,
    ):
        self.repo_path = os.path.abspath(repo_path) if repo_path else REPO_ROOT
        # When the caller overrides repo_path, default the CLAUDE.md and hash
        # file to live inside that repo unless explicit paths are given —
        # otherwise tests would clobber the real CLAUDE.md.
        if claude_md_path is not None:
            self.claude_md_path = claude_md_path
        elif repo_path:
            self.claude_md_path = os.path.join(self.repo_path, "CLAUDE.md")
        else:
            self.claude_md_path = CLAUDE_MD_FILE

        if hash_file is not None:
            self.hash_file = hash_file
        elif repo_path:
            self.hash_file = os.path.join(
                self.repo_path, "data", "memory", ".describe_hash"
            )
        else:
            self.hash_file = DESCRIBE_HASH_FILE

    # ------------------------------------------------------------------ hash
    def compute_repo_hash(self) -> str:
        """Stable hash that changes whenever a *meaningful* repo property
        changes: top-level files, depth-1/2 directory names, manifest contents,
        and the head of README.md.

        Patterned after ``SkillRetriever._compute_dir_hash`` in
        ``src/engine/skills.py:32``.
        """
        h = hashlib.md5()
        repo = Path(self.repo_path)

        # Top-level filenames (sorted for determinism).
        try:
            top_names = sorted(p.name for p in repo.iterdir())
        except FileNotFoundError:
            return h.hexdigest()
        for name in top_names:
            if name in DESCRIBE_EXCLUDED_DIRS or name in _HASH_EXCLUDED_FILES:
                continue
            h.update(name.encode("utf-8"))

        # Depth-1 + depth-2 directory names.
        for depth in (1, 2):
            for d in sorted(self._iter_dirs_at_depth(repo, depth)):
                h.update(d.encode("utf-8"))

        # Manifest contents.
        for manifest in _KEY_MANIFEST_FILES:
            mpath = repo / manifest
            if mpath.is_file():
                try:
                    h.update(mpath.read_bytes())
                except OSError:
                    continue

        # Head of README.md (if present).
        readme = repo / "README.md"
        if readme.is_file():
            try:
                with readme.open("r", encoding="utf-8", errors="replace") as f:
                    head = "".join(f.readline() for _ in range(DESCRIBE_README_HEAD_LINES))
                h.update(head.encode("utf-8"))
            except OSError:
                pass

        return h.hexdigest()

    def _iter_dirs_at_depth(self, root: Path, depth: int) -> Iterable[str]:
        """Yield directory paths *exactly* ``depth`` levels below ``root``.

        Excludes directories listed in ``DESCRIBE_EXCLUDED_DIRS``. Uses a
        pruning ``os.walk`` so vendored trees (``.git``, ``node_modules``,
        ``data``) are never descended into.
        """
        if depth < 1:
            return
        root_str = str(root)
        for current, dirs, _files in os.walk(root_str):
            # Prune in-place so os.walk skips excluded subtrees entirely.
            # Also skip hidden dirs (matching _render_tree's dotfile filter)
            # so the hash and context bundle agree on which dirs matter.
            dirs[:] = sorted(
                d for d in dirs
                if d not in DESCRIBE_EXCLUDED_DIRS and not d.startswith(".")
            )
            rel = os.path.relpath(current, root_str)
            if rel == ".":
                # Root itself — emit its depth-1 children on the next iteration.
                continue
            parts = rel.split(os.sep)
            if len(parts) == depth:
                yield "/".join(parts)
                # No need to descend further — prune children.
                dirs[:] = []
            elif len(parts) > depth:
                dirs[:] = []

    # ------------------------------------------------------------------ plan
    def plan(self, force_refresh: bool = False) -> DescribeDecision:
        current = self.compute_repo_hash()
        cached_hash = self._read_cached_hash()
        cached_summary = managed_section.read_section(
            self.claude_md_path, DESCRIBE_MARKER_BEGIN, DESCRIBE_MARKER_END
        )
        # Refresh if forced, hash changed, or the managed section is missing
        # despite a stored hash (someone hand-deleted it).
        needs = (
            force_refresh
            or cached_hash != current
            or cached_summary is None
        )
        return DescribeDecision(
            needs_refresh=needs,
            current_hash=current,
            cached_hash=cached_hash,
            cached_summary=cached_summary,
        )

    # ----------------------------------------------------------------- prompt
    def build_context_bundle(self) -> str:
        """Render the deterministic ``{CONTEXT_BUNDLE}`` block."""
        repo = Path(self.repo_path)
        sections: list[str] = []

        # Tree (depth ≤ DESCRIBE_TREE_MAX_DEPTH)
        sections.append("### Directory Tree (depth ≤ %d)" % DESCRIBE_TREE_MAX_DEPTH)
        sections.append("```\n" + self._render_tree(repo) + "```")

        # Manifests
        for manifest in _KEY_MANIFEST_FILES:
            mpath = repo / manifest
            if mpath.is_file():
                sections.append(f"### {manifest}")
                try:
                    text = mpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                sections.append("```\n" + text.strip() + "\n```")

        # README head
        readme = repo / "README.md"
        if readme.is_file():
            try:
                with readme.open("r", encoding="utf-8", errors="replace") as f:
                    head = "".join(f.readline() for _ in range(DESCRIBE_README_HEAD_LINES))
                sections.append("### README.md (head)")
                sections.append("```\n" + head.rstrip() + "\n```")
            except OSError:
                pass

        return "\n\n".join(sections)

    def _render_tree(self, root: Path) -> str:
        """Indented directory listing, depth-bounded, vendor-pruned."""
        lines: list[str] = []
        max_depth = DESCRIBE_TREE_MAX_DEPTH

        def walk(path: Path, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            except (PermissionError, OSError):
                return
            for child in children:
                if child.name in DESCRIBE_EXCLUDED_DIRS:
                    continue
                if child.name.startswith(".") and child.name not in (".env.example",):
                    continue
                indent = "  " * (depth - 1)
                marker = "/" if child.is_dir() else ""
                lines.append(f"{indent}{child.name}{marker}")
                if child.is_dir() and not child.is_symlink():
                    walk(child, depth + 1)

        walk(root, 1)
        return "\n".join(lines) + "\n"

    def build_prompt(self) -> str:
        bundle = self.build_context_bundle()
        repo_name = os.path.basename(self.repo_path) or self.repo_path
        return DESCRIBE_PROMPT_TEMPLATE.format(
            REPO_NAME=repo_name,
            CONTEXT_BUNDLE=bundle,
            WORD_MIN=DESCRIBE_WORD_MIN,
            WORD_MAX=DESCRIBE_WORD_MAX,
        )

    # ----------------------------------------------------------------- write
    # Minimum usable summary length — below this we refuse to persist so a
    # bad sampling response (empty string, refusal, truncation) cannot wipe
    # out a good cached summary.
    MIN_PERSIST_WORD_COUNT = 200
    # Minimum structural marker — a valid summary has at least one ``##`` or
    # ``###`` heading from the template (Project Identity, Tech Stack, ...).
    _HEADING_MARKERS = ("## ", "### ")

    def write_summary(self, summary: str, repo_hash: str) -> dict:
        """Persist a finished summary to CLAUDE.md and update the hash file.

        Refuses to persist summaries shorter than ``MIN_PERSIST_WORD_COUNT``
        words or lacking any Markdown section headings (``##`` or ``###``) —
        such outputs usually indicate a failed or refused sampling call and
        overwriting a good cached summary with them would be worse than
        leaving the old one in place.

        Returns a JSON-friendly dict with status, path, hash, word_count,
        and a short preview of the stored summary.
        """
        word_count = len(summary.split())
        has_heading = any(m in summary for m in self._HEADING_MARKERS)

        if word_count < self.MIN_PERSIST_WORD_COUNT or not has_heading:
            return {
                "status": "rejected",
                "reason": (
                    f"sampled summary failed sanity check "
                    f"(word_count={word_count}, has_heading={has_heading}); "
                    f"refusing to overwrite CLAUDE.md"
                ),
                "word_count": word_count,
                "has_heading": has_heading,
                "summary_preview": "\n".join(summary.splitlines()[:6]),
            }

        repo_name = os.path.basename(self.repo_path) or self.repo_path
        timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        wrapped = self._wrap_summary(summary, repo_name, timestamp, repo_hash)

        action = managed_section.upsert_section(
            self.claude_md_path,
            DESCRIBE_MARKER_BEGIN,
            DESCRIBE_MARKER_END,
            wrapped,
        )
        self._save_hash(repo_hash)

        preview = "\n".join(summary.splitlines()[:6])

        return {
            "status": "refreshed",
            "action": action,
            "path": self.claude_md_path,
            "hash": repo_hash,
            "word_count": word_count,
            "in_word_budget": DESCRIBE_WORD_MIN <= word_count <= DESCRIBE_WORD_MAX,
            "summary_preview": preview,
        }

    @staticmethod
    def _wrap_summary(summary: str, repo_name: str, timestamp: str, repo_hash: str) -> str:
        header = (
            f"# Repository: {repo_name}\n"
            f"> Auto-generated by `describe_repo` on {timestamp}. Hash: {repo_hash[:8]}.\n"
            f"> Re-run with `describe_repo(force_refresh=True)` to update.\n"
        )
        return header + "\n" + summary.strip("\n") + "\n"

    # ----------------------------------------------------------------- helpers
    def _read_cached_hash(self) -> Optional[str]:
        try:
            with open(self.hash_file, "r", encoding="utf-8") as f:
                value = f.read().strip()
                return value or None
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def _save_hash(self, digest: str) -> None:
        target_dir = os.path.dirname(self.hash_file) or "."
        os.makedirs(target_dir, exist_ok=True)
        # Atomic write — same pattern as managed_section
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".describe_hash.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(digest)
            os.replace(tmp, self.hash_file)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise

    # ----------------------------------------------------------------- helpers exposed for the MCP tool
    def up_to_date_response(self, decision: DescribeDecision) -> dict:
        preview = ""
        if decision.cached_summary:
            preview = "\n".join(decision.cached_summary.splitlines()[:6])
        return {
            "status": "up-to-date",
            "path": self.claude_md_path,
            "hash": decision.current_hash,
            "summary_preview": preview,
        }


__all__ = [
    "DESCRIBE_PROMPT_TEMPLATE",
    "DescribeDecision",
    "RepoDescriber",
]
