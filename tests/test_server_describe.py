"""Tool-level tests for ``describe_repo``'s needs_summary fallback and
``write_repo_summary``.

``tests/test_describer.py`` covers ``RepoDescriber`` itself; these tests drive
the MCP tools without sampling, the way a client without sampling support
sees them: describe_repo hands back the prompt, and the client persists its
summary through write_repo_summary. Sampling must never cause describe_repo
to write, including when a refresh is forced.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.server as server
from src.daemon.workspaces import ClientContext
from src.engine import config as engine_config
from src.memory import managed_section
from src.memory import describer as describer_module
from src.memory.config import DESCRIBE_MARKER_BEGIN, DESCRIBE_MARKER_END
from src.memory.describer import RepoDescriber
from src.result_size import INLINE_LIMIT, shown_size
from tests.test_describer import _seed_large_repo, _seed_repo, _valid_summary


NEEDS_SUMMARY_KEYS = {"status", "workspace_id", "repo_hash", "repo_path", "prompt", "instruction"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A seeded repo that stdio-mode tools resolve as the client root."""
    _seed_repo(tmp_path)
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    engine_config._reset_client_repo_root_cache()
    yield tmp_path.resolve()
    engine_config._reset_client_repo_root_cache()


async def _describe(**kwargs) -> dict:
    """Decode a repository-description tool response for behavior assertions."""
    return json.loads(await server.describe_repo(**kwargs))


async def _write(needs: dict, summary: str, **kwargs) -> dict:
    """Call write_repo_summary with the describe_repo values passed back unchanged."""
    return json.loads(await server.write_repo_summary(
        summary=summary,
        repo_hash=needs["repo_hash"],
        repo_path=needs["repo_path"],
        workspace_id=needs["workspace_id"],
        **kwargs,
    ))


def _section(root) -> str | None:
    """Read only the managed repository-memory section of a test checkout."""
    return managed_section.read_section(
        str(root / "CLAUDE.md"), DESCRIBE_MARKER_BEGIN, DESCRIBE_MARKER_END
    )


@pytest.mark.asyncio
async def test_needs_summary_shape_without_sampling(repo):
    needs = await _describe()
    assert needs["status"] == "needs_summary"
    assert set(needs) == NEEDS_SUMMARY_KEYS
    assert needs["workspace_id"] is None  # stdio has no workspace identity
    assert needs["repo_path"] == str(repo)
    assert needs["repo_hash"] == RepoDescriber(str(repo)).compute_repo_hash()
    assert "# Demo" in needs["prompt"]
    # The instruction spells out the exact write_repo_summary arguments.
    assert "write_repo_summary(" in needs["instruction"]
    assert f'repo_hash="{needs["repo_hash"]}"' in needs["instruction"]
    assert f"repo_path={json.dumps(needs['repo_path'])}" in needs["instruction"]


@pytest.mark.asyncio
async def test_large_repo_description_stays_inline(repo):
    """A large repository's needs_summary response fits Claude Code's inline result size."""
    _seed_large_repo(repo)
    text = await server.describe_repo(force_refresh=True)
    needs = json.loads(text)
    assert needs["status"] == "needs_summary"
    assert shown_size(text) <= INLINE_LIMIT
    assert "more entries" in needs["prompt"] and "more lines" in needs["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("force_refresh", [False, True])
async def test_describe_never_samples_or_writes(repo, force_refresh):
    """Sampling-capable clients still receive a read-only description on every refresh."""
    sample = AsyncMock(side_effect=AssertionError("describe_repo must not request sampling"))
    ctx = SimpleNamespace(session=SimpleNamespace(
        create_message=sample, check_client_capability=lambda _: True,
    ))
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    result = await _describe(ctx=ctx, force_refresh=force_refresh)
    assert result["status"] == "needs_summary"
    sample.assert_not_awaited()
    assert before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


@pytest.mark.asyncio
async def test_describe_live_installation_is_read_only_and_write_is_rejected(repo, monkeypatch):
    """Protected descriptions remain readable and never invite a forbidden write."""
    monkeypatch.setattr(describer_module, "installation_root", lambda: repo)
    (repo / "CLAUDE.md").write_text("# Installation instructions\n")
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    needs = await _describe(force_refresh=True)
    assert needs["status"] == "needs_summary"
    assert "live_installation" in needs["instruction"]
    assert "write_repo_summary(" not in needs["instruction"]
    written = await _write(needs, _valid_summary())
    assert written["status"] == "rejected"
    assert "live_installation" in written["reason"]
    assert before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


@pytest.mark.asyncio
async def test_write_persists_and_next_describe_is_up_to_date(repo):
    needs = await _describe()
    result = await _write(needs, _valid_summary("persisted"))
    assert result["status"] == "refreshed"
    assert result["path"] == str(repo / "CLAUDE.md")
    assert result["hash"] == needs["repo_hash"]
    section = _section(repo)
    assert section is not None and "persisted" in section

    again = await _describe()
    assert again["status"] == "up-to-date"
    assert again["hash"] == needs["repo_hash"]


@pytest.mark.asyncio
async def test_forced_describe_leaves_existing_summary_and_hash_unchanged(repo):
    """A forced refresh returns context without replacing existing summary or cache files."""
    needs = await _describe()
    assert (await _write(needs, _valid_summary("original")))["status"] == "refreshed"
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert (await _describe(force_refresh=True))["status"] == "needs_summary"
    assert before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


@pytest.mark.asyncio
async def test_worktree_normalization_protects_installation_but_explicit_target_is_writable(repo, monkeypatch):
    """Normalized roots stay protected while an authorized nested worktree can persist."""
    monkeypatch.setattr(describer_module, "installation_root", lambda: repo)
    (repo / "CLAUDE.md").write_text("# Live instructions\n")
    gitdir = repo / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    worktree = repo / ".worktrees" / "feature"
    worktree.mkdir(parents=True)
    _seed_repo(worktree)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT")
    ctx = SimpleNamespace(session=SimpleNamespace(
        check_client_capability=lambda _: True,
        list_roots=AsyncMock(return_value=SimpleNamespace(roots=[SimpleNamespace(uri=repo.as_uri())])),
    ))
    needs = await _describe(ctx=ctx, workspace=str(worktree))
    assert needs["repo_path"] == str(repo)
    assert "live_installation" in needs["instruction"]
    assert (await _write(needs, _valid_summary(), ctx=ctx, workspace=str(worktree)))["status"] == "rejected"
    assert not (repo / ".agents-description.lock").exists()
    assert not (repo / "data").exists()

    physical = await _describe(ctx=ctx, workspace=str(worktree), repo_path=str(worktree))
    assert physical["repo_path"] == str(worktree)
    written = await _write(physical, _valid_summary(), ctx=ctx, workspace=str(worktree))
    assert written["status"] == "refreshed"
    assert written["path"] == str(worktree / "CLAUDE.md")
    assert (await _describe(ctx=ctx, workspace=str(worktree), repo_path=str(worktree)))["status"] == "up-to-date"
    assert (repo / "CLAUDE.md").read_text() == "# Live instructions\n"


@pytest.mark.asyncio
async def test_http_direct_write_to_live_installation_is_rejected(repo, monkeypatch):
    """HTTP cannot bypass installation protection by skipping the description call."""
    monkeypatch.setattr(describer_module, "installation_root", lambda: repo)
    ctx = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(
            client_context=ClientContext("request", "http", workspace_id="workspace-a", root=repo),
        )),
    ))
    result = json.loads(await server.write_repo_summary(
        summary=_valid_summary(), repo_hash=RepoDescriber(str(repo)).compute_repo_hash(),
        repo_path=str(repo), workspace_id="workspace-a", ctx=ctx,
    ))
    assert result["status"] == "rejected"
    assert "live_installation" in result["reason"]
    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".agents-description.lock").exists()
    assert not (repo / "data").exists()


@pytest.mark.asyncio
async def test_external_worktree_needs_physical_memory_root_override(repo, monkeypatch):
    """An external worktree needs an explicit physical memory root before persistence."""
    monkeypatch.setattr(describer_module, "installation_root", lambda: repo)
    gitdir = repo / ".git" / "worktrees" / "external"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    worktree = repo.parent / "external-worktree"
    worktree.mkdir()
    _seed_repo(worktree)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT")
    ctx = SimpleNamespace(session=SimpleNamespace(
        check_client_capability=lambda _: True,
        list_roots=AsyncMock(return_value=SimpleNamespace(roots=[SimpleNamespace(uri=repo.parent.as_uri())])),
    ))
    normalized = await _describe(ctx=ctx, workspace=str(worktree))
    assert normalized["repo_path"] == str(repo)
    assert "AGENTS_CLIENT_REPO_ROOT" in normalized["instruction"]
    refused = await _describe(ctx=ctx, workspace=str(worktree), repo_path=str(worktree))
    assert refused["status"] == "error" and "within workspace" in refused["error"]

    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(worktree))
    engine_config._reset_client_repo_root_cache()
    physical = await _describe(ctx=ctx, workspace=str(worktree), repo_path=str(worktree))
    assert physical["status"] == "needs_summary" and physical["repo_path"] == str(worktree)
    written = await _write(physical, _valid_summary(), ctx=ctx, workspace=str(worktree))
    assert written["status"] == "refreshed" and written["path"] == str(worktree / "CLAUDE.md")
    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".agents-description.lock").exists()
    assert not (repo / "data").exists()


@pytest.mark.asyncio
async def test_write_rejects_stale_repo_hash(repo):
    needs = await _describe()
    # README head is part of the hash: the repo changed while the client
    # was writing its summary.
    (repo / "README.md").write_text("# Demo, renamed\n\nChanged after describe.\n")
    result = await _write(needs, _valid_summary())
    assert set(result) == {"status", "reason"}  # the documented shape
    assert result["status"] == "rejected"
    assert result["reason"].startswith("repo_hash changed")
    assert not (repo / "CLAUDE.md").exists()


@pytest.mark.asyncio
async def test_write_rejects_short_summary(repo):
    needs = await _describe()
    result = await _write(needs, "## Heading\n\nonly a few words")
    assert set(result) == {"status", "reason", "word_count", "has_heading", "summary_preview"}  # the documented shape
    assert result["status"] == "rejected"
    assert result["word_count"] < RepoDescriber.MIN_PERSIST_WORD_COUNT
    assert not (repo / "CLAUDE.md").exists()


@pytest.mark.asyncio
async def test_repo_path_outside_client_root_is_an_error(repo):
    outside = str(repo.parent)
    described = await _describe(repo_path=outside)
    assert described["status"] == "error"
    assert "within workspace" in described["error"]

    written = json.loads(await server.write_repo_summary(
        summary=_valid_summary(),
        repo_hash=RepoDescriber(str(repo)).compute_repo_hash(),
        repo_path=outside,
    ))
    assert written["status"] == "error"
    assert not (repo.parent / "CLAUDE.md").exists()


@pytest.mark.asyncio
async def test_http_write_requires_original_workspace_id(repo):
    """Over HTTP the workspace comes from the request, and write_repo_summary
    accepts only the workspace_id that describe_repo returned."""
    identity = ClientContext("request", "http", workspace_id="workspace-a", root=repo)
    ctx = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(client_context=identity)),
    ))

    needs = await _describe(ctx=ctx)
    assert needs["status"] == "needs_summary"
    assert needs["workspace_id"] == "workspace-a"
    assert 'workspace_id="workspace-a"' in needs["instruction"]

    wrong = json.loads(await server.write_repo_summary(
        summary=_valid_summary(), repo_hash=needs["repo_hash"],
        repo_path=needs["repo_path"], workspace_id="workspace-b", ctx=ctx,
    ))
    assert wrong["status"] == "error"
    assert wrong["error"].startswith("workspace_invalid")
    assert not (repo / "CLAUDE.md").exists()

    right = json.loads(await server.write_repo_summary(
        summary=_valid_summary(), repo_hash=needs["repo_hash"],
        repo_path=needs["repo_path"], workspace_id=needs["workspace_id"], ctx=ctx,
    ))
    assert right["status"] == "refreshed"


@pytest.mark.asyncio
async def test_slash_prompt_directs_the_needs_summary_fallback():
    """The slash prompt preserves the handoff and obeys protected-write instructions."""
    result = await server.mcp.get_prompt("describe_repo", {"force": "yes"})
    text = result.messages[0].content.text
    assert "describe_repo(force_refresh=True)" in text
    assert "needs_summary" in text and "write_repo_summary" in text
    for key in ("repo_hash", "repo_path", "workspace_id"):
        assert key in text
    assert "instruction" in text


@pytest.mark.asyncio
async def test_memory_tools_use_an_empty_session_folder(tmp_path, monkeypatch):
    """History and repository memory work without a marker or manual registration."""
    for name in ("AGENTS_CLIENT_REPO_ROOT", "CLAUDE_PROJECT_DIR", "AGENTS_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(engine_config, "_CLIENT_ROOT_MARKERS", ("no-such-marker",))
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    engine_config._reset_client_repo_root_cache()
    try:
        logged = json.loads(await server.log_interaction("software_engineer", "q", "r"))
        assert logged["timestamp"] and logged["workspace"]["root"] == str(plain.resolve())
        assert logged["workspace"]["source"] != "fallback"
        assert server.drain_pending_logs(5)
        history = json.loads(await server.read_history())
        assert [entry["intent"] for entry in history["entries"]] == ["q"]
        assert (plain / "history.md").is_file()
        described = await _describe()
        assert described["status"] == "needs_summary"
        assert described["repo_path"] == str(plain.resolve())
        written = await _write(described, _valid_summary())
        assert written["status"] in ("written", "refreshed")
        assert _section(plain) is not None
        assert not (plain / ".git").exists()
    finally:
        engine_config._reset_client_repo_root_cache()
