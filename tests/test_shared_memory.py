"""Shared memory across AI clients (#369): reading native memory, scopes, secrets, the index, the tool."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import numpy as np
import pytest

from src.engine import config as engine_config
from src.memory import native, shared
from src.result_size import shown_size

ROUTING = "feedback_agents_core_routing.md"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
TOKEN = "ghp_" + "A" * 36


def note(name: str, description: str, body: str, kind: str = "feedback") -> str:
    return f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: {kind}\n---\n\n{body}\n"


def write(path: Path, text, mode: str = "w") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def world(tmp_path):
    """A Claude configuration directory and a Codex home with memory, and the project roots."""
    root = tmp_path / "work" / "alpha"
    other = tmp_path / "work" / "beta"
    root.mkdir(parents=True)
    other.mkdir(parents=True)
    claude = tmp_path / "home" / ".claude"
    codex = tmp_path / "home" / ".codex"
    own = claude / "projects" / native.project_name(root) / "memory"
    theirs = claude / "projects" / native.project_name(other) / "memory"
    write(own / "feedback_style.md", note("style", "how the owner wants answers",
                                          "Answer in Russian. Put file paths in markdown links."))
    write(own / "MEMORY.md", "- [style](feedback_style.md) — how the owner wants answers\n")
    write(own / "leak.md", note("leak", "a pasted key", KEY))
    write(own / "token.md", note("token", "a pasted token", f"use {TOKEN} for the push"))
    write(own / "huge.md", "x" * (shared.MAX_FACT_BYTES + 1))
    write(theirs / "project_plan.md", note("plan", "beta's release plan", "Ship beta on Friday.", "project"))
    write(claude / "memory" / "user_prefs.md", note("prefs", "the owner's preferences", "Prefers dark themes.", "user"))
    write(claude / "memory" / ROUTING, "Call route_and_load first.\n")
    write(codex / "memories" / "codex_note.md", "The owner deploys with the daemon update command.\n")
    write(codex / "memories_v2" / "summary.txt", "Codex learned: tests run with LANGFUSE_TRACING_ENABLED=false.\n")
    write(codex / "memories_v2" / ".env", f"API={TOKEN}\n")  # never listed: a hidden name is no item
    write(codex / "memories_v2" / "deploy.pem", "named like key material\n")
    write(codex / "memories_v2" / "blob.bin", b"\x00\x01binary")
    stores = [native.Store(claude.resolve(), "claude", "~/.claude"), native.Store(codex.resolve(), "codex", "~/.codex")]
    return {"root": root, "other": other, "claude": claude, "codex": codex, "own": own, "stores": stores}


def by_name(facts):
    return {fact.name or Path(fact.sources[0].file).stem: fact for fact in facts}


# --- reading ------------------------------------------------------------------------------------

def test_collect_reads_every_client_and_skips_what_it_must(world):
    collection = shared.collect(world["stores"])
    texts = "\n".join(fact.text for fact in collection.facts.values())
    names = {source.file for fact in collection.facts.values() for source in fact.sources}
    assert names == {"feedback_style.md", "project_plan.md", "user_prefs.md", "codex_note.md", "summary.txt"}
    assert dict(collection.skipped) == {"managed": 1, "secret": 3, "too_large": 1, "not_text": 1}
    assert "PRIVATE KEY" not in texts and TOKEN not in texts
    codex = [fact for fact in collection.facts.values() if fact.sources[0].client == "codex"]
    assert {source.path for fact in codex for source in fact.sources} == {
        "~/.codex/memories/codex_note.md", "~/.codex/memories_v2/summary.txt"}


def test_a_fact_keeps_its_frontmatter_fields_and_time(world):
    style = by_name(shared.collect(world["stores"]).facts.values())["style"]
    assert (style.description, style.type, style.scope) == ("how the owner wants answers", "feedback", "project")
    assert style.sources[0].project == native.project_name(world["root"])
    assert style.id == hashlib.sha256((world["own"] / "feedback_style.md").read_bytes()).hexdigest()[:12]


def test_identical_content_is_one_fact_with_two_sources(world):
    write(world["codex"] / "memories" / "style_copy.md", (world["own"] / "feedback_style.md").read_text(encoding="utf-8"))
    style = by_name(shared.collect(world["stores"]).facts.values())["style"]
    assert sorted(source.client for source in style.sources) == ["claude", "codex"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks and FIFOs need POSIX here")
def test_links_and_fifos_are_never_opened(world, tmp_path, monkeypatch):
    secret = write(tmp_path / "outside" / "id_rsa.md", KEY)
    os.symlink(secret, world["own"] / "linked.md")
    os.mkfifo(world["own"] / "pipe.md")
    opened = []
    real = native.open_regular
    monkeypatch.setattr(native, "open_regular", lambda path, *a, **k: opened.append(Path(path)) or real(path, *a, **k))
    shared._parsed.clear()
    collection = shared.collect(world["stores"])
    assert secret not in opened and world["own"] / "linked.md" not in opened
    assert world["own"] / "pipe.md" not in opened
    assert "linked.md" not in {source.file for fact in collection.facts.values() for source in fact.sources}


def test_a_linked_memory_directory_is_skipped(world, tmp_path):
    target = tmp_path / "elsewhere"
    write(target / "secret.md", note("elsewhere", "outside", "not a memory"))
    project = world["claude"] / "projects" / "linked-project"
    project.mkdir(parents=True)
    try:
        os.symlink(target, project / "memory", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create directory symlinks")
    files = {source.file for fact in shared.collect(world["stores"]).facts.values() for source in fact.sources}
    assert "secret.md" not in files


# --- scope -----------------------------------------------------------------------------------------

def visible(world, scope, root=None, **kwargs):
    facts = shared.collect(world["stores"]).facts.values()
    keys = shared.project_keys(world["root"] if root is None else root)
    return by_name(shared.select(facts, scope=scope, projects=keys, **kwargs))


def test_project_scope_keeps_this_project_and_user_level_facts(world):
    assert set(visible(world, "project")) == {"style", "prefs", "codex_note", "summary"}


def test_user_scope_keeps_only_user_level_facts(world):
    assert set(visible(world, "user")) == {"prefs", "codex_note", "summary"}


def test_all_scope_includes_other_projects(world):
    assert "plan" in visible(world, "all")


def test_client_and_type_narrow_the_facts(world):
    assert set(visible(world, "project", client="codex")) == {"codex_note", "summary"}
    assert set(visible(world, "all", memory_type="project")) == {"plan"}


def test_a_copy_in_another_project_stays_unnamed(world):
    copy = world["claude"] / "projects" / native.project_name(world["other"]) / "memory" / "feedback_style.md"
    write(copy, (world["own"] / "feedback_style.md").read_text(encoding="utf-8"))
    style = visible(world, "project")["style"]
    assert [source.project for source in style.sources] == [native.project_name(world["root"])]


def test_a_worktree_sees_its_main_checkouts_project(world, tmp_path):
    main = world["root"]
    (main / ".git" / "worktrees" / "feature").mkdir(parents=True)
    worktree = tmp_path / "trees" / "feature"
    write(worktree / ".git", f"gitdir: {main / '.git' / 'worktrees' / 'feature'}\n")
    assert native.project_name(main) in shared.project_keys(worktree)
    assert "style" in visible(world, "project", root=worktree)


# --- chunks and lexical search -----------------------------------------------------------------------

def test_chunks_split_at_headings_and_carry_the_title(world):
    fact = shared.Fact("0" * 12, "0" * 64, note("guide", "a long guide", "# One\n" + "a " * 900 + "\n\n# Two\nshort"),
                       "guide", "a long guide", "reference", 0.0)
    pieces = shared.chunks(fact)
    assert len(pieces) >= 3
    assert all(text.startswith("guide — a long guide") for _, text in pieces)
    assert pieces[-1][1].endswith("# Two\nshort")


def test_tokens_cut_long_words_to_a_stem():
    assert shared.tokens("Зарплаты зарплату searching") == ["зарпла", "зарпла", "search"]


def test_lexical_search_finds_a_word_and_needs_no_model(world, tmp_path):
    index = shared.SharedMemoryIndex(str(tmp_path / "index"), embed_texts=pytest.fail, embed_query=pytest.fail)
    payload = shared.read(root=world["root"], query="markdown links", stores=world["stores"], index=index,
                          semantic=False)
    assert payload["mode"] == "lexical"
    assert payload["facts"][0]["name"] == "style"
    assert payload["facts"][0]["match"]["lexical"]["rank"] == 1


def test_a_fact_id_returns_the_whole_text(world, tmp_path):
    style = visible(world, "project")["style"]
    payload = shared.read(root=world["root"], fact_id=style.id, stores=world["stores"],
                          index=shared.SharedMemoryIndex(str(tmp_path / "index")))
    assert payload["mode"] == "fact" and payload["facts"][0]["text"] == style.text
    hidden = visible(world, "all")["plan"]
    assert shared.read(root=world["root"], fact_id=hidden.id, stores=world["stores"],
                       index=shared.SharedMemoryIndex(str(tmp_path / "index")))["total"] == 0


def test_a_long_listing_stays_inline(world, tmp_path):
    for number in range(60):
        write(world["own"] / f"long_{number}.md", note(f"long {number}", "d" * 300, ("word " * 4000)))
    payload = shared.read(root=world["root"], limit=60, stores=world["stores"],
                          index=shared.SharedMemoryIndex(str(tmp_path / "index")))
    assert shown_size(shared.to_json(payload)) <= shared.RESULT_BUDGET
    assert "instruction" in payload


# --- the index ---------------------------------------------------------------------------------------

class FakeModel:
    """A deterministic bag-of-words embedding: texts that share words point the same way."""

    def __init__(self, dim: int = 64, fingerprint: str = "model-a"):
        self.dim, self.fingerprint_value, self.calls = dim, fingerprint, 0
        self.gate = None

    def vector(self, text: str) -> np.ndarray:
        out = np.zeros(self.dim, dtype=np.float32)
        for word in shared.tokens(text):
            out[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
        return out if out.any() else np.ones(self.dim, dtype=np.float32)

    def embed_texts(self, texts):
        if self.gate is not None:
            self.gate.wait(10)
        self.calls += len(texts)
        return np.asarray([self.vector(text) for text in texts])

    def embed_query(self, text):
        return self.vector(text)

    def fingerprint(self):
        return self.fingerprint_value


def make_index(directory: Path, model: FakeModel, **kwargs) -> shared.SharedMemoryIndex:
    return shared.SharedMemoryIndex(str(directory), embed_texts=model.embed_texts, embed_query=model.embed_query,
                                    fingerprint=model.fingerprint, **kwargs)


def build(world, index) -> dict:
    documents = shared.documents_of(shared.collect(world["stores"]).facts.values())
    index.refresh(documents)
    index.join(30)
    return index.status()


def test_the_index_builds_in_the_background_and_search_turns_hybrid(world, tmp_path):
    model = FakeModel()
    index = make_index(tmp_path / "index", model)
    assert build(world, index)["state"] == "ready"
    payload = shared.read(root=world["root"], query="markdown links answers", stores=world["stores"], index=index)
    assert payload["mode"] == "hybrid" and payload["index"]["state"] == "ready"
    assert payload["facts"][0]["name"] == "style"
    assert payload["facts"][0]["match"]["semantic"] is not None and payload["facts"][0]["match"]["lexical"] is not None


def test_unchanged_chunks_keep_their_vectors_and_a_change_reembeds_only_itself(world, tmp_path):
    model = FakeModel()
    build(world, make_index(tmp_path / "index", model))
    first = model.calls
    assert first > 0
    again = make_index(tmp_path / "index", model)
    assert build(world, again)["state"] == "ready" and model.calls == first
    write(world["codex"] / "memories" / "codex_note.md", "The owner deploys by hand now.\n")
    shared._parsed.clear()
    assert build(world, make_index(tmp_path / "index", model))["state"] == "ready"
    assert model.calls == first + 1


def test_a_model_change_drops_the_stored_vectors(world, tmp_path):
    model = FakeModel()
    build(world, make_index(tmp_path / "index", model))
    first = model.calls
    model.fingerprint_value = "model-b"
    build(world, make_index(tmp_path / "index", model))
    assert model.calls == 2 * first


def test_a_query_during_the_build_answers_lexically_at_once(world, tmp_path):
    model = FakeModel()
    model.gate = threading.Event()
    index = make_index(tmp_path / "index", model)
    try:
        payload = shared.read(root=world["root"], query="markdown links", stores=world["stores"], index=index)
        assert payload["index"]["state"] == "building"
        assert payload["mode"] == "lexical" and payload["facts"][0]["name"] == "style"
    finally:
        model.gate.set()
        index.join(30)
    assert index.status()["state"] == "ready"


def test_a_build_held_by_another_process_reports_it(world, tmp_path):
    from src.file_lock import file_lock
    index = make_index(tmp_path / "index", FakeModel())
    lock = tmp_path / "index" / shared.LOCK_NAME
    holding, release, done = threading.Event(), threading.Event(), threading.Event()

    def hold():
        with file_lock(lock):
            holding.set()
            release.wait(30)
        done.set()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert holding.wait(10)
    try:
        assert build(world, index)["state"] == "building_elsewhere"
    finally:
        release.set()
        done.wait(10)


def test_similar_facts_of_other_clients_are_listed_not_merged(world, tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "SIMILAR_MIN", 0.5)  # the bag-of-words fake has its own similarity scale
    write(world["codex"] / "memories" / "style_codex.md",
          "Answer in Russian. Put file paths in markdown links, please.\n")
    index = make_index(tmp_path / "index", FakeModel())
    build(world, index)
    payload = shared.read(root=world["root"], query="markdown links", stores=world["stores"], index=index)
    files = {fact["sources"][0]["file"] for fact in payload["facts"]}
    style = next(fact for fact in payload["facts"] if fact["name"] == "style")
    assert {"feedback_style.md", "style_codex.md"} <= files  # both facts are listed
    assert any(item["sources"][0]["client"] == "codex" for item in style.get("similar", []))


# --- the tool ------------------------------------------------------------------------------------------

@pytest.fixture
def served(world, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(world["root"]))
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    engine_config._reset_client_repo_root_cache()
    monkeypatch.setattr(shared, "discover", lambda: world["stores"])
    index = make_index(tmp_path / "index", FakeModel())
    monkeypatch.setattr(shared, "get_index", lambda: index)
    yield index
    index.join(30)
    engine_config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_the_tool_serves_claude_memory_to_any_client(served, world):
    import src.server as server
    payload = json.loads(await server.read_shared_memory())
    style = next(fact for fact in payload["facts"] if fact["name"] == "style")
    assert style["sources"][0]["client"] == "claude" and style["sources"][0]["path"].startswith("~/.claude/projects/")
    assert payload["skipped"]["secret"] == 3 and "plan" not in {fact["name"] for fact in payload["facts"]}
    codex = json.loads(await server.read_shared_memory(client="codex"))
    assert {fact["sources"][0]["client"] for fact in codex["facts"]} == {"codex"}


@pytest.mark.asyncio
async def test_the_tool_refuses_bad_arguments(served):
    import src.server as server
    for arguments in ({"scope": "everything"}, {"client": "cursor"}, {"fact_id": "not-an-id"}):
        payload = json.loads(await server.read_shared_memory(**arguments))
        assert payload["status"] == "error", arguments


@pytest.mark.asyncio
async def test_the_tool_searches_and_reads_a_fact(served, world):
    import src.server as server
    found = json.loads(await server.read_shared_memory(query="markdown links"))
    assert found["facts"][0]["name"] == "style"
    whole = json.loads(await server.read_shared_memory(fact_id=found["facts"][0]["id"].upper()))
    assert whole["facts"][0]["text"].startswith("---\nname: style")
