# Agents-Core Memory Subsystem: `describe` + `history.md`

> Specification and step-by-step implementation plan for the per-repo memory mechanism of the Agents-Core MCP server.
> Status: implemented 2026-04-15. See Appendix C for deviations between the plan and the actual implementation.

> **Update 2026-04-15 (post-implementation):** The standalone `record_history` MCP tool was merged
> into `log_interaction`. Now `log_interaction(...)` always appends to `history.md` (with optional
> `intent`/`action`/`outcome`/`files`/`tags` for curated entries) and, when Langfuse is configured,
> additionally sends a generation trace. The `HistoryWriter` class and `history.md` format are
> unchanged — everything below about deduplication, rotation, format, and `read_history` still
> applies. The separate `record_history` tool was removed to avoid duplicate instructions for the model.

---

## 1. Context & Motivation

The Agents-Core MCP server (`/home/wondermr/repos/Agents`) currently lacks persistent per-repo memory: every new Claude Code session re-explores the codebase from scratch and forgets the meaning of past actions. This wastes tokens, loses architectural decisions, and makes work non-reproducible.

We introduce two complementary mechanisms — file-based (markdown), repo-bound, reusable across LLMs:

1. **`describe` mode** — a one-shot bootstrap that distills the repository into a high-quality compressed overview saved into a managed section of `CLAUDE.md`. Future sessions read it instead of re-exploring.
2. **`history.md` mode** — an append-only *intent + action + outcome* log for each meaningful turn; provides a reproducible action history that can be loaded into context on demand.

The subsystem **reuses existing Agents-Core primitives** (FastMCP server, `NumpyVectorStore`, `CLAUDE.md` marker editor, embedder, hash-based invalidation). No new infrastructure is introduced.

### Design Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Describe generation method | **Prompt + MCP sampling** | The server builds a prompt and context bundle, requests the calling LLM to generate a summary via `ctx.session.create_message(...)`, then writes the result to `CLAUDE.md`. Already used in `route_and_load` (`src/server.py:196`). |
| `history.md` location | **Repo root, gitignored by default** | The file stays as local per-repo memory next to the code, but is gitignored by default to avoid polluting PRs and leaking secrets. Teams can remove it from `.gitignore` to opt into a versioned approach. |
| History write trigger | **`log_interaction(...)`** | The standalone `record_history()` was removed: `log_interaction(...)` always appends an entry to `history.md` and optionally sends a Langfuse generation trace. |
| Semantic search | **Lazy** | `log_interaction(...)` stays fast (append only to `history.md`). `NumpyVectorStore` is built on the first `read_history(query=...)` call and incrementally refreshed by mtime. |

### Prior Art Comparison

| Source | Inspiration |
|---|---|
| **claude-recall** (TS, SQLite, hooks-driven) | Outcome-aware memory; content-hash dedup; JIT injection of active rules. **Not adopted:** SQLite (markdown is simpler and git-friendly for our use case), hooks (Phase 2). |
| **mcp-memory-keeper** (TS, SQLite, explicit tools) | Idea of explicit tools instead of hooks; checkpoint semantics; channel-based scoping. **Adapted:** explicit tool surface, no channels (for now). |
| **mcp-memory-service** (Python, REST+MCP) | Idea of autonomous consolidation (decay + compress) — deferred to Phase 5. **Not adopted:** knowledge graph with typed edges. |
| **Mintlify guidance** | Principle: native CLAUDE.md = persistent context; MCP = real-time queries. Our describe writes to CLAUDE.md (read on startup), history is queried on demand via MCP. |

---

## 2. Goals & Non-Goals

**Goals**
- Two new MCP tools: `describe_repo`, `read_history`; history writing integrated into existing `log_interaction`.
- Idempotent, non-destructive editing of `CLAUDE.md` via a new marker pair (separate from the existing routing-protocol section).
- Append-only `history.md` at the repo root with content-hash dedup, monthly rotation, optional semantic recall.
- Tests in the style of existing ones (`pytest`, `tmp_path`, mock embedder).
- Describe-mode prompt — production-grade (BLUF, MECE, Mega-Prompting structure).

**Non-Goals**
- Hooks for auto-capture (Phase 2; the repo does not yet have `.claude/settings.json` with hooks).
- Knowledge graph with typed edges.
- Memory consolidation/decay (Phase 5).
- Cross-repo memory federation.
- Modification of the existing routing-protocol section in `CLAUDE.md`.

---

## 3. Architecture

### 3.1 Data Flow

```
describe_repo(repo_path?, force_refresh=False)
  ├─ RepoDescriber.compute_repo_hash()       # MD5 of pyproject/package.json/top-level dirs/README head
  ├─ if hash unchanged and not force → return {status:"up-to-date"}
  ├─ tree walk (depth ≤ 3, excluding vendor) + reading key files → CONTEXT_BUNDLE
  ├─ render DESCRIBE_PROMPT (template) with CONTEXT_BUNDLE
  ├─ ctx.session.create_message(prompt)      # MCP sampling — Claude generates summary
  ├─ managed_section.upsert(CLAUDE.md, DESCRIBE_MARKER_BEGIN/END, summary)
  ├─ save hash → DESCRIBE_HASH_FILE
  └─ return {status, path, hash, word_count, summary_preview}

log_interaction(..., intent, action, outcome, files?, tags?)
  ├─ HistoryWriter.compute_entry_hash()      # SHA256(intent+action+outcome)[:12]
  ├─ scan last 50 entries for duplicate → return {status:"duplicate"} on match
  ├─ format markdown entry
  ├─ fcntl.flock + atomic append to history.md
  ├─ maybe_rotate() if file > 512 KB → archive to history/YYYY-MM.md
  └─ return {status:"recorded", entry_id, path}

read_history(limit=20, since?, query?)
  ├─ if query:
  │    ├─ HistoryStore.ensure_index()        # lazy: rebuild if file mtime > store mtime
  │    └─ semantic search via NumpyVectorStore + embedder
  └─ else:
       └─ HistoryReader.read_recent()        # parse bottom-up, filter by since
```

### 3.2 Module Layout

| New file | Role |
|---|---|
| `src/memory/__init__.py` | Package marker |
| `src/memory/config.py` | Constants: paths, markers, thresholds; imports `REPO_ROOT`/`DATA_DIR` from `src/engine/config.py:7-8` |
| `src/memory/managed_section.py` | Pure Python port of the marker editor from `scripts/init_repo.sh:636-672`. Functions: `upsert_section`, `read_section`, `remove_section`. Atomic writes via `tempfile` + `os.replace`. |
| `src/memory/describer.py` | `RepoDescriber`: hash → bundle → prompt → sampling → upsert |
| `src/memory/history.py` | `HistoryWriter` (append, dedup, rotate) + `HistoryReader` (recent + lazy semantic) + `HistoryStore` (wrapper around NumpyVectorStore) |
| `tests/test_managed_section.py` | Marker editor tests (style of `tests/test_vector_store.py`) |
| `tests/test_describer.py` | Hash, refresh logic, mock sampling, upsert verification |
| `tests/test_history.py` | Append, dedup, rotation, recent read, semantic search (mock embedder) |

| Modified file | What changes |
|---|---|
| `src/server.py` | Add `@mcp.tool()` for `describe_repo`, `read_history`; extend `log_interaction` with history append |
| `CLAUDE.md` (project root) | Add short instruction to routing protocol: after protocol steps the agent should call `log_interaction()` at the end of meaningful turns; on first session in an unfamiliar repo — call `describe_repo()` first |
| `.gitignore` | Entries for `history.md` and `history/` |

### 3.3 Reuse Map (do NOT rewrite from scratch)

| Existing | Location | Where reused |
|---|---|---|
| FastMCP `@mcp.tool()` decorator + JSON-string returns | `src/server.py:70-608` | All new tools — same registration pattern |
| MCP sampling via `ctx.session.create_message(...)` | `src/server.py:196` | `describe_repo` uses the same sampling call |
| Marker editor for CLAUDE.md (inline Python) | `scripts/init_repo.sh:636-672` | Ported literally to `src/memory/managed_section.py` — bash installer and MCP tool share one implementation |
| `SkillRetriever._compute_dir_hash` + `_needs_reindex` | `src/engine/skills.py:32-51` | `RepoDescriber._compute_repo_hash` + `_needs_refresh` |
| `NumpyVectorStore` (atomic .npz+.json, thread-safe) | `src/engine/vector_store.py:39` | `HistoryStore` for semantic recall |
| `embed_texts` / `embed_query` (FastEmbed) | `src/engine/embedder.py:83,89` | Vectorization of entries and queries |
| `LanguageDetector` | `src/engine/language.py:39` | Language tagging of entries |
| `debug_log` | `src/utils/debug_logger.py:18` | Instrumentation of all tools |
| `@observe` from `langfuse_compat` | `src/utils/langfuse_compat.py` | Optional observability |
| `REPO_ROOT`, `DATA_DIR` | `src/engine/config.py:7-8` | Base paths |
| pytest fixtures (`tmp_path`, `populated_store`) | `tests/test_vector_store.py:12-31` | Mirrored for new tests |

---

## 4. Format Contracts

### 4.1 `CLAUDE.md` (managed section)

Two marker pairs coexist. The new pair is placed **below** the existing routing-protocol section so that reruns of `init_repo.sh` and `describe_repo` never overlap.

```
# >>> Agents-Core Routing Protocol (managed by init_repo) >>>
… existing content …
# <<< Agents-Core Routing Protocol (managed by init_repo) <<<

# >>> Agents-Core Repository Memory (managed by describe_repo) >>>

# Repository: agents-core
> Auto-generated by `describe_repo` on 2026-04-15T10:00:00Z. Hash: a1b2c3.
> Re-run with `describe_repo(force_refresh=True)` to update.

## Project Identity
…
## Tech Stack
…
## Entry Points
…
## Module Map
…
## Conventions
…
## Key Workflows
…
## Architecture Patterns
…
## Test Strategy
…
## Gotchas
…
## Glossary
…

# <<< Agents-Core Repository Memory (managed by describe_repo) <<<
```

**Word budget:** 800–1500 (enforced by tests). Hard cap to prevent `CLAUDE.md` from bloating the context window.

### 4.2 `history.md` (append-only, repo root)

File header (written once on first append):
```yaml
---
repo: agents-core
created: 2026-04-15T09:30:00Z
format_version: 1
---
```

Entry template:
```markdown
## 2026-04-15T14:32:00Z | a1b2c3d4e5f6
**Intent:** Enable semantic recall of past actions for context loading.
**Action:** Added `HistoryStore` on top of `NumpyVectorStore` in `src/memory/history.py`; entries are embedded lazily.
**Outcome:** `pytest tests/test_history.py::test_semantic_search_returns_relevant` passes; index rebuilds when history.md mtime grows.
**Files:** src/memory/history.py, tests/test_history.py
**Tags:** #feature #memory
```

**Rules:**
- `entry_id` (12-hex suffix in the heading) = `sha256(intent+action+outcome)[:12]`. Stable, deduplicated.
- Dedup: scan the last 50 entries by id before append. Duplicates short-circuit.
- Append-only. Past entries are never edited or deleted.
- `fcntl.flock(LOCK_EX)` for writes (protects against concurrent sessions).
- Rotation: when `os.path.getsize > 512 KB` — move file to `history/YYYY-MM.md` (month from the last entry's timestamp), create a fresh `history.md` with a header pointing to the archive.
- UTF-8, `\n` line endings.
- `tags` — free-form `#hashtags`; `metadata` — flat JSON, if provided, serialized inline as `**Meta:** {...}`.

### 4.3 MCP Tool Signatures (added to `src/server.py`)

```python
@mcp.tool()
async def describe_repo(
    ctx: Context,
    repo_path: str | None = None,
    force_refresh: bool = False,
) -> str:
    """One-shot repo bootstrap. Generates a structured summary via MCP sampling
    and writes it into the managed Repository Memory section of CLAUDE.md.

    Returns JSON: {status, path, hash, word_count, summary_preview}.
    status ∈ {"refreshed", "up-to-date", "rejected", "error"}.
    """

@mcp.tool()
async def log_interaction(
    agent_name: str,
    query: str,
    response_content: str,
    intent: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    files: list[str] | None = None,
    tags: list[str] | None = None,
) -> str:
    """End-of-turn logger. Always appends to history.md (append-only, deduped by
    content hash) and optionally sends a Langfuse generation trace.

    Returns JSON: {request_id, langfuse: {status}, history: {status, entry_id, path}}.
    """

@mcp.tool()
async def read_history(
    limit: int = 20,
    since: str | None = None,
    query: str | None = None,
) -> str:
    """Read recent entries (limit/since) or run a lazy semantic search (query).

    Returns JSON: {entries: [...], total, mode}.
    mode ∈ {"recency", "semantic"}.

    Entry shape depends on mode:
    - recency: {id, timestamp, intent, action, outcome, files, tags, metadata}.
    - semantic: {id, distance, document, timestamp, intent, tags}.
    """
```

All tools return JSON strings (following the existing pattern in `src/server.py`).

---

## 5. Describe Prompt (central artifact)

`RepoDescriber` builds this prompt and sends it via `ctx.session.create_message(...)`. The `{{CONTEXT_BUNDLE}}` placeholder is filled deterministically: file tree (depth ≤ 3), `pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod`, README.md (first 200 lines), entry-point file headers, sample `.mdc` frontmatter, test list, scripts.

> The prompt text is kept in English intentionally — future Claude sessions in any language context will be able to execute it, and the output structure matches the English sections of `CLAUDE.md`.

```
You are a **Repository Analyst** performing a one-time deep study of a codebase.
Your output will be saved into CLAUDE.md as the project memory and read by every
future Claude session, so future sessions can work effectively without re-exploring
the codebase.

## Task
Produce a compressed, LLM-consumable repository overview for `{{REPO_NAME}}`.

## Input (CONTEXT_BUNDLE)
{{CONTEXT_BUNDLE}}

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
- Total output: 800–1500 words. Exceeding wastes context; under-shooting loses info.
- Output ONLY the markdown sections above. No preamble, no closing remarks.
```

---

## 6. Step-by-Step Implementation

### Phase 1 — Foundation (1 PR)

1. Create `src/memory/__init__.py` (empty).
2. Create `src/memory/config.py` with constants: `MEMORY_DATA_DIR`, `HISTORY_FILE`, `DESCRIBE_HASH_FILE`, `DESCRIBE_MARKER_BEGIN/END`, `HISTORY_VECTOR_STORE_NAME`, `HISTORY_ROTATION_THRESHOLD_KB = 512`. All paths bound to `REPO_ROOT`/`DATA_DIR` from `src/engine/config.py`.
3. Create `src/memory/managed_section.py` with `upsert_section`, `read_section`, `remove_section`. Port inline Python from `scripts/init_repo.sh:636-672` line-by-line, add atomic writes via `tempfile.NamedTemporaryFile` + `os.replace`. Validate marker uniqueness; raise on partial markers.
4. Write `tests/test_managed_section.py`: create, replace, append, partial-marker rejection, content outside markers preserved, atomic write on failure.

### Phase 2 — Describe (1 PR)

5. Create `src/memory/describer.py`:
   - `RepoDescriber.__init__(repo_path)`.
   - `_compute_repo_hash()` — MD5 of: sorted top-level file names, `pyproject.toml` contents, `package.json` contents, depth-1 + depth-2 directory names, first 200 lines of README.md.
   - `_needs_refresh(force) → (bool, hash)` — pattern from `src/engine/skills.py:43-51`.
   - `_build_context_bundle() → str` — renders the `{{CONTEXT_BUNDLE}}` block: tree (`Path.rglob` with filters, depth ≤ 3, excluding `node_modules`, `.venv`, `__pycache__`, `.git`, `data/`), key file contents, sample `.mdc` frontmatter.
   - `_render_prompt(bundle, repo_name) → str` — substitutes placeholders in the describe prompt template (stored as a multiline constant in the module).
   - `async describe(ctx, force=False) → dict` — orchestrator: if no refresh needed — return cached summary read via `managed_section.read_section`; otherwise build prompt, call `ctx.session.create_message(...)` (sampling), `managed_section.upsert_section(CLAUDE.md, …, generated)`, save hash, return status.
6. Add `describe_repo` tool to `src/server.py`. Wrap with `@observe` if Langfuse is loaded. Return JSON.
7. Write `tests/test_describer.py`: deterministic hash, refresh-on-change, refresh-skipped-when-unchanged, mocked `ctx.session.create_message` with a pre-built summary, upsert verification, word-count assertion (800–1500).

### Phase 3 — History (1 PR)

8. Create `src/memory/history.py`:
   - `HistoryWriter`: `__init__(history_path)`, `_compute_entry_hash`, `_is_duplicate` (tail-scan last 50), `append_entry` (format → `flock` → atomic append → `maybe_rotate`), `maybe_rotate`.
   - `HistoryReader`: `read_recent(limit, since)` — bottom-up parsing via regex `## {ts} | {id}`.
   - `HistoryStore`: lazy wrapper around `NumpyVectorStore`. `ensure_index()` compares store and file mtime; if stale — re-embeds all entries (or incrementally: only entries whose id is not in the store). `search(query, limit)` embeds the query, returns top-N with distance.
9. Extend `log_interaction` in `src/server.py` to append history entries. Add `read_history`. Both return JSON.
10. Write `tests/test_history.py`: append creates file with header, format roundtrip, dedup, recent read order, `since` filter, rotation triggers + creates archive, lazy semantic index rebuilds on file change (mock embedder).

### Phase 4 — Wiring & Documentation (1 PR)

11. Update `CLAUDE.md` (project root) — short post-flight instruction inside the routing protocol section: *"After completing a meaningful turn, call `log_interaction(intent, action, outcome, files)`. On first session in an unfamiliar repo, call `describe_repo()` first."* Propagated to `~/.claude/CLAUDE.md` on next `init_repo.sh` run.
12. Update `.gitignore`:
    ```
    # history.md and history/ are gitignored by default for privacy.
    history.md
    history/
    ```
13. Update root `README.md` — short "Repository Memory" section with a link to this specification.

### Phase 5 — Optional Follow-ups (separate PRs, out of current scope)

14. **Hooks:** add a `.claude/settings.json` template with a `PostToolUse` hook that reminds Claude to include curated fields in `log_interaction`. Document in the spec, disabled by default.
15. **Memory consolidation:** nightly task that compresses entries older than N days into monthly summaries.
16. **Integration with `init_repo.sh`:** optional `--describe` flag that runs `describe_repo` during first install.

---

## 7. Test Plan

### 7.1 Unit Tests (pytest)

| Test | What it verifies |
|---|---|
| `test_managed_section_create` | File creation with markers and content |
| `test_managed_section_replace` | Replacing existing section, content outside markers untouched |
| `test_managed_section_append` | Append when no markers exist |
| `test_managed_section_partial_markers` | Error on partial markers (only begin or only end) |
| `test_describer_hash_deterministic` | Same repo → same hash |
| `test_describer_hash_changes_on_file_add` | New top-level file → hash changes |
| `test_describer_skips_when_unchanged` | Second call without force → `status:"up-to-date"` |
| `test_describer_force_refresh_overwrites` | `force_refresh=True` → regeneration even with same hash |
| `test_describer_word_count_in_range` | Generated summary fits within 800–1500 words |
| `test_history_append_creates_file` | First call creates `history.md` with frontmatter |
| `test_history_format_roundtrip` | Append → read → all fields match |
| `test_history_dedup_by_hash` | Repeat call with same intent/action/outcome → `status:"duplicate"` |
| `test_history_read_recent_order` | Returns entries in reverse chronological order |
| `test_history_read_since_filter` | Timestamp filtering works |
| `test_history_rotation_triggers` | File > 512 KB → moves to `history/YYYY-MM.md` |
| `test_history_semantic_lazy_index` | Vector store does not exist until first `read_history(query=...)` |
| `test_history_semantic_returns_relevant` | Mock embedder: query returns the most relevant entry |

### 7.2 Live MCP Checks (manual)

1. Start server: `bash scripts/run_tests.sh && python -m src.server`.
2. In a Claude Code session:
   - `describe_repo()` → confirm that `CLAUDE.md` was updated; routing section untouched.
   - Repeat `describe_repo()` → `status:"up-to-date"`.
   - `describe_repo(force_refresh=True)` → regeneration.
   - `log_interaction(...)` with curated `intent="Verify integration", action="Manual test", outcome="passed"` → entry in `history.md`.
   - Repeat same `log_interaction` → `status:"duplicate"`.
   - `read_history(limit=5)` → entry visible.
   - `read_history(query="verify integration")` → semantic match.
3. Regressions: `pytest tests/test_routing.py` — `route_and_load` correctly routes existing test queries.

### 7.3 Quality Metrics

| Metric | Target |
|---|---|
| Word count of describe summary | 800–1500 |
| Size of `CLAUDE.md` after describe | < 15 KB |
| Latency of `log_interaction` (history append) | < 20 ms (no vectorization) |
| Latency of `read_history(query=...)` (cold) | < 500 ms (including initial indexing of ≤ 100 entries) |
| Semantic search accuracy | top-1 matches expected in ≥ 90% of tests |

---

## 8. Open Questions (non-blocking for v1)

- **Categories in `log_interaction`?** Accept an enum `category` (decision/refactor/fix/feature) for filtered reading? **Recommendation:** skip in v1; tags cover this with zero schema rigidity.
- **Git metadata in describe?** Include current branch and last commit? **Recommendation:** yes — append a `## Git State` block at the end of the managed section, regenerated on each refresh.
- **Phase 2 hooks:** opt-in PostToolUse for auto-reminder to include curated fields in `log_interaction`. **Not in scope for v1;** hook contract to be specified in this doc (Phase 5 section).

---

## Appendix A. Affected Files (quick navigation)

**Created:**
- `src/memory/__init__.py`
- `src/memory/config.py` — constants
- `src/memory/managed_section.py` — marker editor (port from `scripts/init_repo.sh:636-672`)
- `src/memory/describer.py` — `RepoDescriber` + describe prompt template
- `src/memory/history.py` — `HistoryWriter`, `HistoryReader`, `HistoryStore`
- `tests/test_managed_section.py`
- `tests/test_describer.py`
- `tests/test_history.py`

**Modified:**
- `src/server.py` — new `@mcp.tool()` registrations + extended `log_interaction`
- `CLAUDE.md` — added post-flight instruction (one paragraph) inside routing protocol
- `.gitignore` — `history.md` and `history/` gitignored by default
- `README.md` — one paragraph about the memory subsystem with a link to this spec

## Appendix C. Implementation Notes (what actually shipped)

The implementation as of 2026-04-15 matches the spec. Clarifications that emerged during coding:

1. **Module `src/memory/config.py`** adds constants not explicitly mentioned in the plan: `CLAUDE_MD_FILE`, `HISTORY_ARCHIVE_DIR`, `DESCRIBE_TREE_MAX_DEPTH`, `DESCRIBE_README_HEAD_LINES`, `DESCRIBE_EXCLUDED_DIRS`, `DESCRIBE_WORD_MIN/MAX`. All are derived from values mentioned in the spec (512 KB, 800–1500 words, depth ≤ 3).

2. **CLAUDE.md/history.md hashes are excluded from the repo hash** (`_HASH_EXCLUDED_FILES` in `describer.py`). Without this, `describe_repo` would invalidate its own cache on every run — the side effect of writing to CLAUDE.md changes the top-level filenames.

3. **`RepoDescriber` is split into `plan() / build_prompt() / write_summary()`**, and the `ctx.session.create_message(...)` call stays in `src/server.py`. This allows writing unit tests without an MCP context — tests do not use sampling.

4. **`HistoryWriter`/`HistoryReader` are pure stdlib**, without numpy. `HistoryStore` (semantic recall) imports `NumpyVectorStore` and the embedder only on the first `search()`. This is critical for NixOS environments: writer/reader work even when numpy cannot load (semantic-store tests are marked `skipif` in that case).

5. **`HistoryStore.search` accepts `embed_query` / `embed_texts` as arguments** (DI). By default they load `src.engine.embedder.*`; in tests a `FakeEmbedder` with deterministic vectors is injected, requiring no real model download.

6. **fcntl wrapper** `_lock_exclusive` / `_unlock` in `history.py` — no-op on Windows so the module imports cross-platform.

7. **Tag normalization:** hashtags without a `#` prefix are automatically prefixed (`"feature"` → `"#feature"`).

8. **Rotation merge:** if `history/YYYY-MM.md` already exists at rotation time (multiple rotations in one month), the new snapshot is appended with a `<!-- merged on rotation -->` separator rather than overwriting the existing archive.

9. **CLI instruction in `CLAUDE.md`** added as item `4. Repository memory (first session per repo)` — a separate section within the Routing Flow to avoid confusion with post-flight step 3.

10. **README.md** contains a `Repository Memory` section with a link to this spec and a mention of the `.gitignore` opt-out.

---

## Appendix B. References (research)

- `https://github.com/raoulbia-ai/claude-recall` — outcome-aware memory, hooks, JIT injection
- `https://github.com/mkreyman/mcp-memory-keeper` — explicit tool surface, checkpoints, channels
- `https://github.com/doobidoo/mcp-memory-service` — autonomous consolidation, knowledge graph
- `https://www.mintlify.com/blog/how-claudes-memory-and-mcp-work` — split native CLAUDE.md vs MCP
