"""Build read-only repository context and explicitly persist finished summaries.

The calling model summarizes the prompt. Only ``write_summary`` writes the
managed Repository Memory section of CLAUDE.md; the running installation and
runtime source root are protected from summary writes.
"""

from __future__ import annotations

import datetime as _dt
import functools
import hashlib
import itertools
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from src.engine.config import _main_checkout, get_client_repo_root
from src.runtime_paths import installation_root, runtime_root
from src.memory import config as _memory_config
from src.memory import managed_section
from src.memory.config import (
    DESCRIBE_EXCLUDED_DIRS,
    DESCRIBE_LINE_MAX_CHARS,
    DESCRIBE_MANIFEST_MAX_CHARS,
    DESCRIBE_MANIFESTS_MAX_CHARS,
    DESCRIBE_MARKER_BEGIN,
    DESCRIBE_MARKER_END,
    DESCRIBE_README_HEAD_LINES,
    DESCRIBE_README_MAX_CHARS,
    DESCRIBE_TREE_MAX_CHARS,
    DESCRIBE_TREE_MAX_DEPTH,
    DESCRIBE_TREE_MAX_ENTRIES_PER_DIR,
    DESCRIBE_WORD_MAX,
    DESCRIBE_WORD_MIN,
)
from src.result_size import shown_size

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


# Context bundle layout: sections joined by a blank line, bodies in code fences.
_SECTION_SEPARATOR = "\n\n"
_FENCE_OPEN = "```\n"
_FENCE_CLOSE = "\n```"
_ELLIPSIS = "…"

# Bundle sizes are shown units (#434): what a text adds to the describe_repo
# response as Claude Code counts it (src.result_size.shown_size). The response
# JSON-encodes the prompt and Claude Code escapes it once more, in UTF-16 code
# units, so a quote or backslash costs 4, a newline or tab 3, another control
# character 7, an astral character 2 and any other character 1. Escaping goes
# character by character, so the cost of joined texts is the sum of their costs.
_EMPTY_SHOWN_SIZE = shown_size("")


def _cost(text: str) -> int:
    """Shown units ``text`` adds to the describe_repo response; at least ``len(text)``."""
    return shown_size(json.dumps(text, ensure_ascii=False)[1:-1]) - _EMPTY_SHOWN_SIZE


@functools.lru_cache(maxsize=1024)
def _char_cost(char: str) -> int:
    return _cost(char)


_NEWLINE_COST = _cost("\n")


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def _fenced(heading: str, body: str) -> str:
    """Render one bundle section: a heading, a blank line and a fenced body."""
    return f"{heading}{_SECTION_SEPARATOR}{_FENCE_OPEN}{body}{_FENCE_CLOSE}"


def _body_room(heading: str, max_chars: int) -> int:
    """Shown units left for a fenced body once its heading and fences are counted."""
    return max_chars - _cost(_fenced(heading, ""))


def _shorten_line(line: str, max_chars: int) -> str:
    """Return ``line`` within ``max_chars`` shown units, ending in an ellipsis when shortened.

    Python strings index code points, so a cut never splits the UTF-8 or UTF-16
    sequence of a character.
    """
    if _cost(line) <= max_chars:
        return line
    room = max_chars - _cost(_ELLIPSIS)
    if room < 0:
        return ""
    # The whole line costs more than room, so some prefix runs over it.
    end = next(end for end, size in enumerate(itertools.accumulate(map(_char_cost, line))) if size > room)
    return line[:end] + _ELLIPSIS


def _more_lines_note(number: int, tail: str = "") -> str:
    return f"{_ELLIPSIS} {_count(number, 'more line', 'more lines')}{tail}"


def _fit_lines(lines: list[str], max_chars: int, note_tail: str = "") -> str:
    """Join ``lines`` within ``max_chars`` shown units, cutting at a line boundary.

    Lines that fit are returned unchanged. Otherwise lines over
    ``DESCRIBE_LINE_MAX_CHARS`` are shortened first, and a cut ends with a
    "… N more lines" note followed by ``note_tail``; when not even the first
    line fits beside that note, that single line is shortened into the room
    left, so the section keeps some content.
    """
    text = "\n".join(lines)
    if _cost(text) <= max_chars:
        return text
    lines = [_shorten_line(line, DESCRIBE_LINE_MAX_CHARS) for line in lines]
    text = "\n".join(lines)
    if _cost(text) <= max_chars:
        return text

    def note(number: int) -> str:
        return _more_lines_note(number, note_tail)

    kept: list[str] = []
    size = 0  # cost of the kept lines, each followed by its newline
    for index, line in enumerate(lines):
        # The whole text does not fit, so a note always follows a kept line.
        if size + _cost(line) + _NEWLINE_COST + _cost(note(len(lines) - index - 1)) > max_chars:
            break
        kept.append(line)
        size += _cost(line) + _NEWLINE_COST
    left = len(lines) - len(kept)
    if not kept:
        after = left - 1
        room = max_chars - (_cost(note(after)) + _NEWLINE_COST if after else 0)
        if room > _cost(_ELLIPSIS):
            kept.append(_shorten_line(lines[0], room))
            left = after
    result = "\n".join(kept + ([note(left)] if left else []))
    return result if _cost(result) <= max_chars else _shorten_line(note(left), max_chars)


@dataclass(frozen=True)
class _TreeLine:
    """One directory-tree line, its depth and the number of entries it stands for."""
    text: str
    depth: int
    entries: int = 1


def _tree_note(dropped: dict[int, int]) -> str:
    total = sum(dropped.values())
    by_depth = ", ".join(
        f"{number} at depth {depth}" for depth, number in sorted(dropped.items(), reverse=True)
    )
    return (
        f"{_ELLIPSIS} {_count(total, 'more entry', 'more entries')} ({by_depth}; "
        "the deepest lines were cut first, evenly across directories)"
    )


def _sibling_ranks(lines: list[_TreeLine]) -> list[tuple[int, ...]]:
    """Each line's position among its siblings, then its parent's among theirs, up to the top.

    The tree walk lists a directory's entries right after its own line, so a
    line is at most one level deeper than the line before it.
    """
    branch: list[int] = []  # positions along the current branch, the top level first
    ranks: list[tuple[int, ...]] = []
    for line in lines:
        del branch[line.depth:]
        if len(branch) == line.depth:
            branch[-1] += 1
        else:
            branch.append(0)
        ranks.append(tuple(reversed(branch)))
    return ranks


def _fit_tree(lines: list[_TreeLine], max_chars: int) -> str:
    """Join tree lines within ``max_chars`` shown units, dropping the deepest lines first.

    At each depth, from the deepest up, the directories give up their last
    entries in turn: a line goes before every line that stands earlier in its
    own directory, and among lines that stand equally far down their
    directories, the one under a later parent goes first. So each directory
    keeps as many of its first entries as any other, give or take one, and
    the top levels stay complete while the room allows; a final note counts
    the entries left out at each depth.
    """
    sizes = [_cost(line.text) + _NEWLINE_COST for line in lines]  # each line with its newline
    size = sum(sizes)
    if size - _NEWLINE_COST <= max_chars:
        return "\n".join(line.text for line in lines)
    ranks = _sibling_ranks(lines)
    order = sorted(range(len(lines)), key=lambda i: (-lines[i].depth, tuple(-rank for rank in ranks[i])))
    kept = [True] * len(lines)
    dropped: dict[int, int] = {}
    for index in order:
        if dropped and size + _cost(_tree_note(dropped)) <= max_chars:
            break
        line = lines[index]
        kept[index] = False
        size -= sizes[index]
        dropped[line.depth] = dropped.get(line.depth, 0) + line.entries
    note = _tree_note(dropped)
    result = "\n".join([line.text for line, keep in zip(lines, kept) if keep] + [note])
    return result if _cost(result) <= max_chars else _shorten_line(note, max_chars)


DESCRIBE_PROMPT_TEMPLATE = """\
You are a **Repository Analyst** performing a one-time deep study of a codebase.
Produce reusable project memory so future sessions can work effectively without
re-exploring the codebase. Persistence is a separate action governed by the
describe_repo response's instruction and the caller's repository instructions.

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

    ``plan()`` and ``build_prompt()`` only read; ``write_summary()`` is a separate
    explicit write, so inspection never changes the repository.
    """

    def __init__(
        self,
        repo_path: Optional[str] = None,
        claude_md_path: Optional[str] = None,
        hash_file: Optional[str] = None,
    ):
        self.repo_path = os.path.abspath(repo_path) if repo_path else get_client_repo_root()
        # When the caller overrides repo_path, default the CLAUDE.md and hash
        # file to live inside that repo unless explicit paths are given —
        # otherwise tests would clobber the real CLAUDE.md.
        if claude_md_path is not None:
            self.claude_md_path = claude_md_path
        elif repo_path:
            self.claude_md_path = os.path.join(self.repo_path, "CLAUDE.md")
        else:
            self.claude_md_path = _memory_config.CLAUDE_MD_FILE

        if hash_file is not None:
            self.hash_file = hash_file
        elif repo_path:
            self.hash_file = os.path.join(
                self.repo_path, "data", "memory", ".describe_hash"
            )
        else:
            self.hash_file = _memory_config.DESCRIBE_HASH_FILE

    # ------------------------------------------------------------------ hash
    def compute_repo_hash(self) -> str:
        """Stable hash that changes whenever a *meaningful* repo property
        changes: top-level files, depth-1/2 directory names, manifest contents,
        and the head of README.md.

        Patterned after ``SkillRetriever._compute_dir_hash`` in
        ``src/engine/skills.py``.
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
            # Skip hidden files/dirs to match _render_tree's dotfile filter.
            if name.startswith(".") and name not in (".env.example",):
                continue
            h.update(name.encode("utf-8"))

        # Depth-1 + depth-2 directory names.
        for depth in (1, 2):
            for d in sorted(self._iter_dirs_at_depth(repo, depth)):
                h.update(d.encode("utf-8"))

        # Manifest contents (skip symlinks to stay within the sandbox).
        for manifest in _KEY_MANIFEST_FILES:
            mpath = repo / manifest
            if mpath.is_file() and not mpath.is_symlink():
                try:
                    h.update(mpath.read_bytes())
                except OSError:
                    continue

        # Head of README.md (if present, skip if symlinked).
        readme = repo / "README.md"
        if readme.is_file() and not readme.is_symlink():
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
        """Render the deterministic ``{CONTEXT_BUNDLE}`` block.

        The tree, the manifests and the README head each stay within their own
        cap in shown units, so the bundle never exceeds
        ``DESCRIBE_BUNDLE_MAX_CHARS``. A part that fits its cap is rendered
        whole; a cut happens at a line boundary and leaves a note of what it
        left out.
        """
        repo = Path(self.repo_path)

        # Tree (depth ≤ DESCRIBE_TREE_MAX_DEPTH); the first section has no separator.
        heading = "### Directory Tree (depth ≤ %d)" % DESCRIBE_TREE_MAX_DEPTH
        sections = [_fenced(heading, self._render_tree(repo, _body_room(heading, DESCRIBE_TREE_MAX_CHARS)))]

        sections.extend(self._manifest_sections(repo))

        # README head (skip if symlinked)
        readme = repo / "README.md"
        if readme.is_file() and not readme.is_symlink():
            try:
                with readme.open("r", encoding="utf-8", errors="replace") as f:
                    head = "".join(f.readline() for _ in range(DESCRIBE_README_HEAD_LINES))
                    continues = bool(f.read(1))
                heading = "### README.md (head)"
                room = _body_room(heading, DESCRIBE_README_MAX_CHARS - _cost(_SECTION_SEPARATOR))
                # A cut note counts the lines left out of the head, so it says when the file goes on.
                tail = f" of the first {DESCRIBE_README_HEAD_LINES}; README.md continues" if continues else ""
                sections.append(_fenced(heading, _fit_lines(head.rstrip().split("\n"), room, tail)))
            except OSError:
                pass

        return _SECTION_SEPARATOR.join(sections)

    def _manifest_sections(self, repo: Path) -> list[str]:
        """Render the root manifests within ``DESCRIBE_MANIFESTS_MAX_CHARS`` together.

        Skips symlinks to stay within the sandbox. The smallest manifest goes
        first and takes at most an equal share of what is left, capped at
        ``DESCRIBE_MANIFEST_MAX_CHARS``, so small manifests stay whole and the
        larger ones split the rest. Each share includes the section separator.
        """
        manifests: list[tuple[str, str]] = []
        for manifest in _KEY_MANIFEST_FILES:
            mpath = repo / manifest
            if mpath.is_file() and not mpath.is_symlink():
                try:
                    manifests.append((manifest, mpath.read_text(encoding="utf-8", errors="replace").strip()))
                except OSError:
                    continue
        rendered: dict[str, str] = {}
        budget = DESCRIBE_MANIFESTS_MAX_CHARS
        by_size = sorted(manifests, key=lambda item: _cost(item[1]))
        for position, (manifest, text) in enumerate(by_size):
            share = min(DESCRIBE_MANIFEST_MAX_CHARS, budget // (len(by_size) - position))
            heading = f"### {manifest}"
            room = _body_room(heading, share - _cost(_SECTION_SEPARATOR))
            rendered[manifest] = _fenced(heading, _fit_lines(text.split("\n"), room))
            budget -= _cost(rendered[manifest]) + _cost(_SECTION_SEPARATOR)
        return [rendered[manifest] for manifest, _text in manifests]

    def _render_tree(self, root: Path, max_chars: int) -> str:
        """Indented directory listing, depth-bounded, vendor-pruned, within ``max_chars`` shown units.

        The full listing is returned unchanged when it fits. Otherwise a
        directory lists at most ``DESCRIBE_TREE_MAX_ENTRIES_PER_DIR`` entries
        and notes how many more it has, an overlong line is shortened, and
        ``_fit_tree`` drops the deepest lines if the listing is still too long.
        Once the full listing has grown too long, the walk skips the entries
        past a directory's cap.
        """
        full: Optional[list[str]] = []  # the listing without caps, while it may still fit
        full_size = -_NEWLINE_COST  # cost of the full listing joined by newlines
        capped: list[_TreeLine] = []
        max_depth = DESCRIBE_TREE_MAX_DEPTH
        per_dir = DESCRIBE_TREE_MAX_ENTRIES_PER_DIR

        def walk(path: Path, depth: int, listed: bool) -> None:
            """List ``path``'s entries; ``listed`` says whether ``path`` is in the capped listing."""
            nonlocal full, full_size
            if depth > max_depth:
                return
            try:
                children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            except (PermissionError, OSError):
                return
            children = [
                child for child in children
                if child.name not in DESCRIBE_EXCLUDED_DIRS
                and not (child.name.startswith(".") and child.name not in (".env.example",))
            ]
            indent = "  " * (depth - 1)
            for position, child in enumerate(children):
                within = listed and position < per_dir
                if not within and full is None:
                    break  # the rest of this directory is past the cap
                marker = "/" if child.is_dir() else ""
                text = f"{indent}{child.name}{marker}"
                if full is not None:
                    full.append(text)
                    full_size += _cost(text) + _NEWLINE_COST
                    if full_size > max_chars:
                        full = None
                if within:
                    capped.append(_TreeLine(_shorten_line(text, DESCRIBE_LINE_MAX_CHARS), depth))
                if child.is_dir() and not child.is_symlink():
                    walk(child, depth + 1, within)
            more = len(children) - per_dir
            if listed and more > 0:
                capped.append(_TreeLine(
                    f"{indent}{_ELLIPSIS} {_count(more, 'more entry', 'more entries')}", depth, more,
                ))

        walk(root, 1, True)
        if full is not None:
            return "\n".join(full)
        return _fit_tree(capped, max_chars)

    def build_prompt(self) -> str:
        bundle = self.build_context_bundle()
        repo_name = os.path.basename(self.repo_path) or self.repo_path
        # Use str.replace instead of str.format — the bundle contains raw file
        # contents (JSON, TOML) with curly braces that would crash .format().
        # Replace simple placeholders first, then CONTEXT_BUNDLE last so any
        # literal "{WORD_MIN}" / "{WORD_MAX}" in file contents isn't corrupted.
        return (
            DESCRIBE_PROMPT_TEMPLATE
            .replace("{REPO_NAME}", repo_name)
            .replace("{WORD_MIN}", str(DESCRIBE_WORD_MIN))
            .replace("{WORD_MAX}", str(DESCRIBE_WORD_MAX))
            .replace("{CONTEXT_BUNDLE}", bundle)
        )

    # ----------------------------------------------------------------- write
    # Minimum usable summary length — below this we refuse to persist so a
    # bad sampling response (empty string, refusal, truncation) cannot wipe
    # out a good cached summary.
    MIN_PERSIST_WORD_COUNT = 200
    # Minimum structural marker — a valid summary has at least one ``##`` or
    # ``###`` heading from the template (Project Identity, Tech Stack, ...).
    _HEADING_MARKERS = ("## ", "### ")

    def write_rejection_reason(self) -> Optional[str]:
        """Protect every write destination; separate linked worktrees are allowed."""
        paths = (
            Path(self.repo_path) / ".agents-description.lock",
            Path(self.claude_md_path),
            Path(self.claude_md_path).with_name(f".{Path(self.claude_md_path).name}.lock"),
            Path(self.hash_file),
        )
        # Atomic replacement changes the final directory entry even when it
        # is a symlink pointing outside the installation.
        targets = {
            target for path in paths
            for target in (path.resolve(), path.parent.resolve() / path.name)
        }
        for root in (installation_root(), runtime_root()):
            protected = root.resolve()
            status = protected.stat()
            protected_identity = (status.st_dev, status.st_ino)
            for target in targets:
                for parent in (target, *target.parents):
                    try:
                        status = parent.stat()
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    # Resolved spellings can still differ for the same macOS
                    # directory (case variants and /System/Volumes/Data).
                    if (status.st_dev, status.st_ino) == protected_identity:
                        return (
                            "live_installation: repository summaries cannot modify the running "
                            "Agents-Core installation or runtime source checkout; use a separate worktree"
                        )
                    # A nested linked worktree is a separate checkout. Plain
                    # subdirectories must not bypass installation protection.
                    if _main_checkout(parent) != parent:
                        break
        return None

    def write_summary(self, summary: str, repo_hash: str) -> dict:
        """Validate protected destinations and freshness before explicitly persisting a summary."""
        reason = self.write_rejection_reason()
        if reason:
            return {"status": "rejected", "reason": reason}
        from src.file_lock import file_lock
        with file_lock(os.path.join(self.repo_path, ".agents-description.lock")):
            # A caller can wait on the lock while an ancestor is retargeted.
            if reason := self.write_rejection_reason():
                return {"status": "rejected", "reason": reason}
            if self.compute_repo_hash() != repo_hash:
                return {"status": "rejected", "reason": "repo_hash changed; call describe_repo again"}
            return self._write_summary_locked(summary, repo_hash)

    def _write_summary_locked(self, summary: str, repo_hash: str) -> dict:
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
        os.makedirs(os.path.dirname(self.hash_file) or ".", exist_ok=True)
        managed_section.atomic_write(self.hash_file, digest)

    # ----------------------------------------------------------------- helpers exposed for the MCP tool
    @staticmethod
    def _strip_wrapper_header(text: str) -> str:
        """Remove the _wrap_summary header so word count matches write_summary."""
        # Header and body are separated by a blank line.
        parts = text.split("\n\n", 1)
        return parts[1] if len(parts) > 1 else text

    def up_to_date_response(self, decision: DescribeDecision) -> dict:
        cached = decision.cached_summary or ""
        raw = self._strip_wrapper_header(cached)
        preview = "\n".join(raw.splitlines()[:6])
        word_count = len(raw.split())
        return {
            "status": "up-to-date",
            "path": self.claude_md_path,
            "hash": decision.current_hash,
            "word_count": word_count,
            "in_word_budget": DESCRIBE_WORD_MIN <= word_count <= DESCRIBE_WORD_MAX,
            "summary_preview": preview,
        }


__all__ = [
    "DESCRIBE_PROMPT_TEMPLATE",
    "DescribeDecision",
    "RepoDescriber",
]
