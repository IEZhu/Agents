"""Tool-level tests for ``describe_repo``'s needs_summary fallback and
``write_repo_summary``.

``tests/test_describer.py`` covers ``RepoDescriber`` itself; these tests drive
the MCP tools without sampling, the way a client without sampling support
sees them: describe_repo hands back the prompt, and the client persists its
summary through write_repo_summary. Two tests stub sampling to cover the
direct write and the fallback when sampling fails.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import src.server as server
from src.daemon.workspaces import ClientContext
from src.engine import config as engine_config
from src.memory import managed_section
from src.memory.config import DESCRIBE_MARKER_BEGIN, DESCRIBE_MARKER_END
from src.memory.describer import RepoDescriber
from tests.test_describer import _seed_repo, _valid_summary


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
    return json.loads(await server.describe_repo(**kwargs))


async def _write(needs: dict, summary: str) -> dict:
    """Call write_repo_summary with the describe_repo values passed back unchanged."""
    return json.loads(await server.write_repo_summary(
        summary=summary,
        repo_hash=needs["repo_hash"],
        repo_path=needs["repo_path"],
        workspace_id=needs["workspace_id"],
    ))


def _section(root) -> str | None:
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
async def test_sampled_summary_is_written_directly(repo, monkeypatch):
    async def sample(ctx, prompt, query):
        return _valid_summary("sampled")

    monkeypatch.setattr(server, "_supports_sampling", lambda ctx: True)
    monkeypatch.setattr(server, "_sample_with_agent", sample)
    result = await _describe()
    assert result["status"] == "refreshed"
    section = _section(repo)
    assert section is not None and "sampled" in section


@pytest.mark.asyncio
async def test_failed_sampling_falls_back_to_needs_summary_and_writes_nothing(repo, monkeypatch):
    async def sample(ctx, prompt, query):
        raise RuntimeError("client refused the sampling request")

    monkeypatch.setattr(server, "_supports_sampling", lambda ctx: True)
    monkeypatch.setattr(server, "_sample_with_agent", sample)
    needs = await _describe()
    assert needs["status"] == "needs_summary" and set(needs) == NEEDS_SUMMARY_KEYS
    assert _section(repo) is None
    # The fallback completes as it does without sampling.
    assert (await _write(needs, _valid_summary("after failure")))["status"] == "refreshed"


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
    result = await server.mcp.get_prompt("describe_repo", {"force": "yes"})
    text = result.messages[0].content.text
    assert "describe_repo(force_refresh=True)" in text
    assert "needs_summary" in text and "write_repo_summary" in text
    for key in ("repo_hash", "repo_path", "workspace_id"):
        assert key in text
