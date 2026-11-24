"""The first activation in a workspace carries the describe_repo result itself (#459).

Sessions keep working in a repository without calling ``describe_repo`` although a memory
lesson, a client instruction and the managed instruction block each asked for it (eight recorded
repeats in #459), so the signal cannot depend on the model remembering one. These tests pin what
the response carries: the describe_repo result on the first activation in a workspace, nothing on
a repeat in the same unchanged workspace, its own report for another workspace and a changed one,
a reported failure that marks nothing described and never describes the installation checkout in
a workspace's place, and no report at all in a prepared worker's contract.
"""
import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import src.server as server
from src.daemon import workspaces
from src.daemon.workspaces import WorkspaceRegistry, resolve_client_context
from src.engine import config as engine_config
from src.engine import persona
from src.memory.describer import RepoDescriber
from src.runtime_paths import installation_root
from src.schemas.protocol import PersonaDescriptor, PersonaResponse
from tests.test_describer import _seed_repo, _valid_summary
from tests.test_subagent import prepared, worker_tree  # noqa: F401
from tests.test_workspace_mismatch import (assert_narrow_remedy, http_context,  # noqa: F401
                                           linked_worktree, repositories)


@pytest.fixture(autouse=True)
def forget_reports(monkeypatch):
    """Both records are process-global, and what one test reported is not another's state."""
    monkeypatch.setattr(persona, "_reported_workspaces", {})
    monkeypatch.setattr(persona, "_reported_callers", {})


@pytest.fixture
def bundle(monkeypatch):
    """Assemble any role without the retriever; the repository report is what is under test."""
    async def build(agent, query, history, tier=None, selection=None):
        return SimpleNamespace(
            agent=agent, bundle_revision="b" * 64, scope=f"Scope of {agent}",
            persona_block=f"Role {agent}", rules_block="General rules",
            skills_block="Role skills", implants_block="Role implants",
            skills_loaded=["skill-dev-clean-code"], implants_loaded=["IterBudget"],
            rules_loaded=["language-match"], tier="standard",
        )
    monkeypatch.setattr(persona, "build_persona_bundle", AsyncMock(side_effect=build))
    monkeypatch.setattr(server.router, "update_cache", AsyncMock())


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A seeded repository registered as this stdio session's workspace."""
    repo = tmp_path / "project"
    repo.mkdir()
    _seed_repo(repo)
    for name in ("CLAUDE_PROJECT_DIR", "AGENTS_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(repo))
    engine_config._reset_client_repo_root_cache()
    yield repo.resolve()
    engine_config._reset_client_repo_root_cache()


@pytest.fixture
def describers(monkeypatch):
    """The ``repo_path`` of every describer built while the test runs, in order."""
    import src.memory.describer as describer_module
    built = []
    original = describer_module.RepoDescriber

    class Recording(original):
        def __init__(self, *args, **kwargs):
            built.append(kwargs.get("repo_path", args[0] if args else None))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(describer_module, "RepoDescriber", Recording)
    return built


def _write_summary(root):
    describer = RepoDescriber(repo_path=str(root))
    return describer.write_summary(_valid_summary(), describer.compute_repo_hash())


async def activate(agent="software_engineer", query="Fix the failing import", **extra):
    return json.loads(await server.get_agent_context(agent, query, protocol_version=2, **extra))


@pytest.mark.asyncio
async def test_first_activation_in_a_workspace_carries_the_describe_repo_result(bundle, workspace,
                                                                               describers):
    response = await activate()
    assert response["status"] == "SUCCESS"
    report = response["repo_context"]
    assert report["status"] == "needs_summary"
    assert report["workspace"] == str(workspace) and report["repo_path"] == str(workspace)
    assert len(report["repo_hash"]) == 32
    assert "describe_repo" in report["instruction"]
    assert "write_repo_summary" in report["instruction"]
    # The lookup is the read-only half of describe_repo: no summary, no cache, no lock.
    assert describers == [str(workspace)]
    assert not (workspace / "CLAUDE.md").exists()
    assert not (workspace / "data").exists()


@pytest.mark.asyncio
async def test_a_plain_folder_workspace_is_not_asked_for_a_summary(bundle, tmp_path, monkeypatch,
                                                                  describers):
    folder = tmp_path / "task"
    folder.mkdir()
    (folder / "data.csv").write_text("a,b\n1,2\n")
    for name in ("CLAUDE_PROJECT_DIR", "AGENTS_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(folder))
    engine_config._reset_client_repo_root_cache()
    try:
        response = await activate()
    finally:
        engine_config._reset_client_repo_root_cache()
    assert response["status"] == "SUCCESS"
    assert "repo_context" not in response
    assert describers == []
    assert sorted(path.name for path in folder.iterdir()) == ["data.csv"]


@pytest.mark.asyncio
async def test_a_repeated_activation_in_the_same_unchanged_workspace_carries_nothing(bundle,
                                                                                    workspace):
    _write_summary(workspace)
    first = await activate()
    assert first["repo_context"]["status"] == "up-to-date"
    switched = await activate("lawyer", "Read this fictional contract",
                              current_persona=PersonaDescriptor.model_validate(first["persona"]))
    assert switched["status"] == "SUCCESS" and "repo_context" not in switched
    kept = await activate("lawyer", "The same role again",
                          current_persona=PersonaDescriptor.model_validate(switched["persona"]))
    assert kept["status"] == "NO_CHANGE" and "repo_context" not in kept


@pytest.mark.asyncio
async def test_another_workspace_gets_its_own_report(bundle, workspace, tmp_path, monkeypatch):
    _write_summary(workspace)
    opening = await activate()
    first = opening["repo_context"]
    # Seeded identically on purpose: the report follows the workspace, not the repository hash.
    other = tmp_path / "other"
    other.mkdir()
    _seed_repo(other)
    _write_summary(other)
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(other))
    engine_config._reset_client_repo_root_cache()
    active = PersonaDescriptor.model_validate(opening["persona"])
    kept = await activate(current_persona=active)
    assert kept["status"] == "NO_CHANGE", "the role is unchanged; only the workspace moved"
    second = kept["repo_context"]
    assert second["workspace"] == str(other.resolve()) != first["workspace"]
    assert second["hash"] == first["hash"]
    assert "repo_context" not in await activate(current_persona=active)


@pytest.mark.asyncio
async def test_a_stale_repository_context_is_reported_again(bundle, workspace):
    describer = RepoDescriber(repo_path=str(workspace))
    written = describer.write_summary(_valid_summary(), describer.compute_repo_hash())
    assert written["status"] == "refreshed"
    described = (await activate())["repo_context"]
    assert described["status"] == "up-to-date" and described["hash"] == written["hash"]
    assert "repo_context" not in await activate()
    # A new top-level file leaves the written summary describing an older tree.
    (workspace / "Makefile").write_text("all:\n\t@true\n")
    stale = (await activate())["repo_context"]
    assert stale["status"] == "needs_summary"
    assert stale["repo_hash"] != described["hash"]


@pytest.mark.asyncio
async def test_a_described_workspace_carries_the_summary_preview(bundle, workspace):
    describer = RepoDescriber(repo_path=str(workspace))
    describer.write_summary(_valid_summary(), describer.compute_repo_hash())
    report = (await activate())["repo_context"]
    assert report["status"] == "up-to-date"
    assert report["path"] == str(workspace / "CLAUDE.md")
    assert "Project Identity" in report["summary_preview"]
    assert report["word_count"] > 0 and report["in_word_budget"] is False
    assert "describe_repo" in report["instruction"]


@pytest.mark.asyncio
async def test_an_unavailable_workspace_is_reported_and_describes_nothing(bundle, workspace,
                                                                         describers, monkeypatch):
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(Path.home()))
    engine_config._reset_client_repo_root_cache()
    response = await activate()
    assert response["status"] == "SUCCESS"
    report = response["repo_context"]
    assert report["status"] == "unavailable"
    assert report["error"].startswith("workspace_unsafe: ")
    assert "installation checkout is never described" in report["instruction"]
    for absent in ("path", "workspace", "repo_path", "repo_hash", "summary_preview"):
        assert absent not in report
    assert str(installation_root()) not in json.dumps(report)
    # Nothing was described, so nothing is marked described; the decision is recorded for
    # this caller instead, and the same instruction does not ride on every activation.
    assert describers == []
    assert str(workspace) not in persona._reported_workspaces
    assert "repo_context" not in await activate()


@pytest.mark.asyncio
async def test_a_failing_lookup_leaves_the_activation_untouched(bundle, workspace, monkeypatch):
    monkeypatch.setattr(persona, "_describe_workspace", Mock(side_effect=OSError("disk gone")))
    response = await activate()
    assert response["status"] == "SUCCESS" and "repo_context" not in response
    assert all(response[f"{block}_block"] for block in ("persona", "rules", "skills", "implants"))
    assert str(workspace) not in persona._reported_workspaces


@pytest.mark.asyncio
async def test_a_workspace_outside_the_registered_root_is_not_described(bundle, repositories,
                                                                       describers):
    """C1's refusal decides what a foreign workspace is told: both roots, never a description."""
    registered, other, _inside = repositories
    ctx = http_context(registered)
    refused = json.loads(await persona.load_persona(
        server.router, "software_engineer", "q", [], None, ctx=ctx, workspace=str(other)))
    report = refused["repo_context"]
    assert report["status"] == "workspace_mismatch" and report["code"] == "workspace_mismatch"
    assert report["registered_workspace"] == str(registered)
    assert report["requested_workspace"] == str(other)
    assert_narrow_remedy(report["remedy"], other)
    assert "summary_preview" not in report and "repo_hash" not in report
    assert describers == [] and str(registered) not in persona._reported_workspaces
    answered = json.loads(await persona.load_persona(
        server.router, "software_engineer", "q", [], None, ctx=ctx, workspace=str(registered)))
    assert answered["repo_context"]["workspace"] == str(registered)


@pytest.mark.asyncio
async def test_a_prepared_worker_runs_no_conversation_bootstrap(bundle, worker_tree, workspace,
                                                               describers):
    before = prepared()
    assert describers == [], "preparing a package looks no repository up"
    reported = (await activate())["repo_context"]
    assert reported["workspace"] == str(workspace), "the conversation still gets the signal"
    after = prepared()
    assert after.contract.model_dump() == before.contract.model_dump()
    assert after.contract_revision == before.contract_revision
    package = json.dumps(after.model_dump(mode="json"), ensure_ascii=False)
    assert persona.REPO_CONTEXT_FIELD not in package
    assert "describe_repo" not in package
    assert describers == [str(workspace)], "only the conversation activation built a describer"


@pytest.mark.asyncio
async def test_an_intermediate_or_failed_activation_carries_no_report(workspace, describers):
    for status in ("ROUTE_REQUIRED", "ERROR"):
        response = json.dumps({"protocol_version": 2, "status": status, "request_id": "r"})
        assert await persona.with_repo_context(response) == response
    assert describers == []


@pytest.mark.asyncio
async def test_a_caller_that_ignores_the_field_applies_the_same_bundle(bundle, workspace):
    _write_summary(workspace)
    reporting = await activate()
    plain = await activate()
    assert "repo_context" in reporting and "repo_context" not in plain
    volatile = {"request_id", "persona", "repo_context"}
    assert ({key: value for key, value in reporting.items() if key not in volatile}
            == {key: value for key, value in plain.items() if key not in volatile})
    assert reporting["persona"] == {**plain["persona"],
                                    "activation_id": reporting["persona"]["activation_id"]}


@pytest.mark.asyncio
async def test_a_pending_needs_summary_keeps_being_reported_until_the_summary_is_written(
        bundle, workspace):
    opening = await activate()
    assert opening["repo_context"]["status"] == "needs_summary"
    active = PersonaDescriptor.model_validate(opening["persona"])
    for _ in range(2):
        later = await activate(current_persona=active)
        assert later["status"] == "NO_CHANGE"
        assert later["repo_context"]["status"] == "needs_summary"
        assert later["repo_context"]["repo_hash"] == opening["repo_context"]["repo_hash"]
    written = _write_summary(workspace)
    assert written["status"] == "refreshed"
    done = await activate(current_persona=active)
    assert done["repo_context"]["status"] == "up-to-date"
    assert "repo_context" not in await activate(current_persona=active)


@pytest.mark.asyncio
async def test_a_lookup_that_failed_is_reported_again_once_it_works(bundle, workspace,
                                                                   monkeypatch):
    real = persona._describe_workspace
    monkeypatch.setattr(persona, "_describe_workspace", Mock(side_effect=OSError("disk gone")))
    assert "repo_context" not in await activate()
    monkeypatch.setattr(persona, "_describe_workspace", real)
    assert (await activate())["repo_context"]["status"] == "needs_summary"


@pytest.mark.asyncio
async def test_an_http_session_gets_the_report_on_its_first_activation_and_not_the_second(
        bundle, repositories, describers, monkeypatch):
    """The tool bodies forward the request context: HTTP reads the workspace only from it."""
    registered, _other, _inside = repositories
    _write_summary(registered)
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    ctx = http_context(registered)
    first = json.loads(await server.get_agent_context(
        "software_engineer", "Fix the failing import", protocol_version=2, ctx=ctx))
    assert first["repo_context"]["status"] == "up-to-date"
    assert first["repo_context"]["workspace"] == str(registered)
    second = json.loads(await server.get_agent_context(
        "software_engineer", "Fix it again", protocol_version=2, ctx=ctx,
        current_persona=PersonaDescriptor.model_validate(first["persona"])))
    assert second["status"] == "NO_CHANGE" and "repo_context" not in second


@pytest.mark.asyncio
async def test_route_and_load_forwards_the_context_and_the_workspace(bundle, repositories,
                                                                    monkeypatch):
    registered, _other, _inside = repositories
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    ctx = http_context(registered)
    forwarded = AsyncMock(return_value=json.dumps({"status": "NO_CHANGE"}))
    monkeypatch.setattr(server, "route_persona", forwarded)
    await server.route_and_load("Fix the failing import", protocol_version=2, ctx=ctx,
                                workspace=str(registered))
    assert forwarded.await_args.kwargs["ctx"] is ctx
    assert forwarded.await_args.kwargs["workspace"] == str(registered)


@pytest.mark.asyncio
async def test_a_tool_call_naming_an_uncovered_workspace_gets_the_c1_refusal(
        bundle, repositories, describers, monkeypatch):
    registered, other, _inside = repositories
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    response = json.loads(await server.get_agent_context(
        "software_engineer", "q", protocol_version=2, ctx=http_context(registered),
        workspace=str(other)))
    assert response["status"] == "SUCCESS", "the activation itself is never refused"
    report = response["repo_context"]
    assert report["status"] == "workspace_mismatch" and report["code"] == "workspace_mismatch"
    assert report["registered_workspace"] == str(registered)
    assert report["requested_workspace"] == str(other)
    assert_narrow_remedy(report["remedy"], other)
    assert "summary_preview" not in report and describers == []


def _hash_counter(monkeypatch):
    """Every repository whose tree the activation hashed, in order."""
    hashed = []
    original = RepoDescriber.compute_repo_hash

    def counting(self):
        hashed.append(self.repo_path)
        return original(self)

    monkeypatch.setattr(RepoDescriber, "compute_repo_hash", counting)
    return hashed


@pytest.mark.asyncio
async def test_a_repeat_activation_answers_from_the_record_without_hashing(bundle, workspace,
                                                                          monkeypatch):
    """The record is consulted before the tree is hashed, on every activation (#459 cost)."""
    _write_summary(workspace)
    hashed = _hash_counter(monkeypatch)
    assert (await activate())["repo_context"]["status"] == "up-to-date"
    assert hashed == [str(workspace)], "the first activation looks the repository up"
    for _ in range(3):
        assert "repo_context" not in await activate()
    assert hashed == [str(workspace)], "a repeat activation hashed the tree again"


@pytest.mark.asyncio
async def test_a_pending_report_is_repeated_without_hashing_the_tree_again(bundle, workspace,
                                                                          monkeypatch):
    """An undescribed repository must not pay the walk on every activation either (#459)."""
    hashed = _hash_counter(monkeypatch)
    opening = (await activate())["repo_context"]
    assert opening["status"] == "needs_summary"
    assert hashed == [str(workspace)]
    for _ in range(3):
        assert (await activate())["repo_context"] == opening, "the pending report keeps coming"
    assert hashed == [str(workspace)], "a repeat activation hashed the tree again"


@pytest.mark.asyncio
async def test_deleting_only_the_managed_section_is_reported_again(bundle, workspace):
    """The case plan() exists for: the section is gone and the tree hash has not moved (#600)."""
    written = _write_summary(workspace)
    assert (await activate())["repo_context"]["status"] == "up-to-date"
    assert "repo_context" not in await activate()
    (workspace / "CLAUDE.md").write_text("# Project\n\nNotes a human keeps.\n", encoding="utf-8")
    describer = RepoDescriber(repo_path=str(workspace))
    assert describer.compute_repo_hash() == written["hash"], "only the section was deleted"
    assert describer.plan().needs_refresh is True
    report = (await activate())["repo_context"]
    assert report["status"] == "needs_summary" and report["repo_hash"] == written["hash"]
    assert report["workspace"] == str(workspace)


@pytest.mark.asyncio
async def test_a_needs_summary_report_can_be_followed_exactly_as_it_says(bundle, repositories,
                                                                        monkeypatch):
    """Its instruction names write_repo_summary, which over HTTP needs the workspace_id (C-H3)."""
    registered, _other, _inside = repositories
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    ctx = http_context(registered, identity="workspace-a")
    report = json.loads(await server.get_agent_context(
        "software_engineer", "q", protocol_version=2, ctx=ctx))["repo_context"]
    assert report["status"] == "needs_summary"
    assert report["workspace_id"] == "workspace-a"
    assert "write_repo_summary" in report["instruction"]
    written = json.loads(await server.write_repo_summary(
        summary=_valid_summary(), repo_hash=report["repo_hash"], repo_path=report["repo_path"],
        workspace_id=report["workspace_id"], ctx=ctx))
    assert written["status"] == "refreshed", written
    assert written["path"] == str(registered / "CLAUDE.md")


@pytest.mark.asyncio
async def test_a_workspace_that_can_never_be_written_is_not_told_to_persist(bundle, monkeypatch):
    """write_repo_summary always refuses the live installation, so the advice must not stand (C-H3)."""
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    report = json.loads(await server.get_agent_context(
        "software_engineer", "q", protocol_version=2,
        ctx=http_context(installation_root())))["repo_context"]
    assert report["status"] == "needs_summary"
    assert "live_installation" in report["instruction"]
    assert "write_repo_summary" not in report["instruction"]
    assert "describe_repo" in report["instruction"]


@pytest.mark.asyncio
async def test_the_report_caps_the_preview_describe_repo_returns_whole(bundle, workspace):
    """A six-paragraph summary must not put 10,000 characters into every activation (C-H4)."""
    paragraph = "Agents-Core is one local server that supplies the context of a project. " * 40
    describer = RepoDescriber(repo_path=str(workspace))
    describer.write_summary("## Project Identity\n\n" + "\n".join([paragraph] * 6),
                            describer.compute_repo_hash())
    report = (await activate())["repo_context"]
    assert report["status"] == "up-to-date"
    assert len(report["summary_preview"]) <= persona.MAX_REPORT_PREVIEW_CHARS
    assert report["summary_preview"].endswith("[…]")
    assert len(json.dumps(report, ensure_ascii=False)) < 2000
    whole = describer.up_to_date_response(describer.plan())["summary_preview"]
    assert len(whole) > persona.MAX_REPORT_PREVIEW_CHARS, "describe_repo keeps the stored head"


@pytest.mark.asyncio
async def test_a_resolution_only_the_installation_fallback_answers_is_unavailable(
        bundle, workspace, describers, monkeypatch):
    """The slice's own criterion: the installation is never handed over as a project (C-T1).

    The legacy fallback is what decides here, which is the only way to reach the flag the
    activation passes; a cwd that is merely gone raises before it.
    """
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
    fallback = installation_root()

    def resolved():
        return str(fallback), True, "install"

    resolved.cache_clear = lambda: None
    monkeypatch.setattr(engine_config, "_resolve_client_repo_root", resolved)
    response = await activate()
    assert response["status"] == "SUCCESS"
    report = response["repo_context"]
    assert report["status"] == "unavailable"
    assert "cwd unavailable" in report["error"]
    assert "installation checkout is never described" in report["instruction"]
    assert str(fallback) not in json.dumps(report)
    assert describers == []


@pytest.mark.asyncio
async def test_the_record_of_reported_workspaces_stays_bounded(bundle, tmp_path, monkeypatch):
    """Past the bound the oldest workspace is evicted and reports again (C-T3)."""
    for bound in (persona._MAX_REPORTED_WORKSPACES, persona._MAX_REPORTED_CALLERS):
        assert 0 < bound <= 1024, "the shipped bounds must stay small"
    monkeypatch.setattr(persona, "_MAX_REPORTED_WORKSPACES", 2)
    monkeypatch.setattr(persona, "_MAX_REPORTED_CALLERS", 2)
    for name in ("CLAUDE_PROJECT_DIR", "AGENTS_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    roots = []
    for name in ("one", "two", "three"):
        root = tmp_path / name
        root.mkdir()
        _seed_repo(root)
        _write_summary(root)
        roots.append(root.resolve())
    for root in roots:
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(root))
        engine_config._reset_client_repo_root_cache()
        assert (await activate())["repo_context"]["workspace"] == str(root)
    assert len(persona._reported_workspaces) == 2
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(roots[0]))
    engine_config._reset_client_repo_root_cache()
    assert (await activate())["repo_context"]["workspace"] == str(roots[0])
    engine_config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_a_reporting_response_still_validates_as_a_persona_response(bundle, workspace):
    """The declared field and the injected key cannot drift apart (C-T6)."""
    _write_summary(workspace)
    raw = await server.get_agent_context("software_engineer", "q", protocol_version=2)
    assert PersonaResponse.model_validate_json(raw).repo_context == json.loads(raw)["repo_context"]


async def _http_activate(ctx, query="Fix the failing import", workspace=None):
    return json.loads(await server.get_agent_context(
        "software_engineer", query, protocol_version=2, ctx=ctx, workspace=workspace))


@pytest.mark.asyncio
async def test_every_session_of_one_server_process_is_told_not_only_the_first(
        bundle, repositories, monkeypatch):
    """"Already reported" is decided per caller, never per process (P-C1).

    One process serves several sessions in both deployments that ship: the Claude desktop app
    starts a single stdio server for all of its Code sessions, and the HTTP daemon serves
    every session on the machine. Keyed by the root alone, the report fired for the first
    session and for nobody else, and a second session was handed the first one's workspace_id
    while the report was pending.
    """
    registered, _other, _inside = repositories
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    for identity in ("session-a", "session-b"):
        report = (await _http_activate(http_context(registered, identity=identity)))["repo_context"]
        assert report["status"] == "needs_summary", identity
        assert report["workspace_id"] == identity, "each caller gets its own workspace_id"
    _write_summary(registered)
    described = (await _http_activate(http_context(registered, identity="session-a")))["repo_context"]
    assert described["status"] == "up-to-date"
    # The same caller is still told once, and the other session is told as well.
    assert "repo_context" not in await _http_activate(http_context(registered, identity="session-a"))
    second = (await _http_activate(http_context(registered, identity="session-b")))["repo_context"]
    assert second["status"] == "up-to-date", "a second session of the same process is told too"
    assert second["hash"] == described["hash"]


def test_a_stdio_call_carries_no_identity_that_separates_two_sessions():
    """Why the stdio twin of the shared caller identity is a limitation, not a fix (CR1 F3).

    Two sessions of one shared stdio server get one caller identity, as two HTTP sessions
    registered for the same workspace do, so the second is not told about a described
    repository. The honest reason is that nothing stable identifies them: a stdio process runs
    one `ServerSession` over its single pipe pair, which is why the Code sessions the Claude
    desktop app multiplexes over one server are not separate MCP sessions and two stdio servers
    are separate processes sharing no record. This pins what the session and the request
    actually carry, so a session identity a later SDK gains is noticed here instead of staying
    an assumption in a comment.
    """
    from mcp.server.session import ServerSession
    from mcp.shared.context import RequestContext
    from mcp.types import Implementation, InitializeRequestParams, RequestParams

    def identifiers(names):
        """Names that would carry the identity of a calling session, if one existed."""
        flattened = {name: name.lower().replace("_", "") for name in names}
        return sorted(name for name, flat in flattened.items()
                      if "sessionid" in flat or flat in ("id", "clientid", "callerid"))

    surfaces = {
        "ServerSession": [name for name in dir(ServerSession) if not name.startswith("_")],
        "InitializeRequestParams": list(InitializeRequestParams.model_fields),
        "clientInfo": list(Implementation.model_fields),
        "_meta": list(RequestParams.Meta.model_fields),
        "RequestContext": [field.name for field in dataclasses.fields(RequestContext)],
    }
    for name, names in surfaces.items():
        assert identifiers(names) == [], f"{name} gained a session identifier: {names}"
    # What they do carry: one initialize handshake, and per request an id and a progress token.
    assert "client_params" in surfaces["ServerSession"]
    assert set(surfaces["InitializeRequestParams"]) >= {"protocolVersion", "capabilities",
                                                        "clientInfo"}
    assert set(surfaces["clientInfo"]) >= {"name", "version"}
    assert surfaces["_meta"] == ["progressToken"]
    assert set(surfaces["RequestContext"]) >= {"request_id", "meta", "session", "request"}
    # `request` is the HTTP request, None over stdio, which is what `_http_request` reads.
    assert workspaces._http_request(SimpleNamespace(
        request_context=RequestContext(request_id="r", meta=None, session=None,
                                       lifespan_context=None))) is None


@pytest.mark.asyncio
async def test_a_worktree_session_is_described_from_the_checkout_it_runs_in(bundle, repositories,
                                                                           tmp_path, monkeypatch):
    """A worktree session must not be handed another branch's description (P-C2).

    Registration maps the named directory to its main checkout (`config._main_checkout`) and
    keeps the worktree as a physical grant, so the memory root of a worktree session is the
    main checkout. Reported from there, the session received the main checkout's managed
    summary, written for a different branch, with the instruction to read it instead of
    exploring the tree, and a `repo_hash` that belongs to the main checkout.
    """
    main, _other, _inside = repositories
    worktree = linked_worktree(main, tmp_path / "worktree")
    (worktree / "BRANCH.md").write_text("# Only on this branch\n", encoding="utf-8")
    assert engine_config._main_checkout(worktree) == main, "the normalization that causes this"
    main_summary = _write_summary(main)
    kept = (main / "CLAUDE.md").read_text(encoding="utf-8")
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    ctx = http_context(main, grants=(worktree,))
    report = (await _http_activate(ctx))["repo_context"]
    assert report["status"] == "needs_summary", "the worktree has no description of its own"
    assert report["workspace"] == str(worktree) and report["repo_path"] == str(worktree)
    assert report["registered_workspace"] == str(main), "the report says which root it describes"
    assert report["repo_hash"] == RepoDescriber(repo_path=str(worktree)).compute_repo_hash()
    assert report["repo_hash"] != main_summary["hash"], "the hash belongs to the described root"
    assert "Project Identity" not in json.dumps(report), "not the other branch's summary"
    # A needs_summary acted on from here must not persist into the main checkout.
    assert "to persist the overview" not in report["instruction"], "no write is advised"
    assert "instead of writing a summary" in report["instruction"]
    assert "other_checkout" in report["instruction"]
    refused = json.loads(await server.write_repo_summary(
        summary=_valid_summary(), repo_hash=report["repo_hash"], repo_path=report["repo_path"],
        workspace_id=report["workspace_id"], ctx=ctx, workspace=str(worktree)))
    assert refused["status"] == "error" and "within workspace" in refused["error"]
    assert (main / "CLAUDE.md").read_text(encoding="utf-8") == kept, "the main checkout is intact"
    assert not (worktree / "CLAUDE.md").exists()
    # A call that names the worktree, as the parameter asks it to, reads the same way.
    named = json.loads(await server.get_agent_context(
        "software_engineer", "q", protocol_version=2, ctx=http_context(
            main, identity="named", grants=(worktree, main)), workspace=str(worktree)))
    assert named["repo_context"]["repo_path"] == str(worktree)
    assert named["repo_context"]["registered_workspace"] == str(main)
    # Once the worktree is described, the preview is its own file.
    _write_summary(worktree)
    described = (await _http_activate(http_context(main, identity="later", grants=(worktree,))))["repo_context"]
    assert described["status"] == "up-to-date" and described["path"] == str(worktree / "CLAUDE.md")
    assert described["registered_workspace"] == str(main)
    assert str(main) in described["instruction"] and str(worktree) in described["instruction"]


@pytest.mark.asyncio
async def test_a_call_that_names_the_memory_root_is_described_from_the_memory_root(
        bundle, repositories, describers, tmp_path, monkeypatch):
    """An explicit choice must beat the fallback that stands in for no choice (CR1 F1).

    The single-grant fallback exists for a call that names no workspace: the one physical grant
    beside the memory root is then the directory the registration was made from. A call that
    names the memory root itself was covered by `context.root`, which recorded no
    `covering_root`, and fell into that same fallback — so the session that asked about the main
    checkout was handed the worktree's hash, path and summary instead.
    """
    main, _other, inside = repositories
    worktree = linked_worktree(main, tmp_path / "worktree")
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    ctx = http_context(main, grants=(worktree,))
    for named in (str(main), str(main) + os.sep, str(inside), "nested"):
        context = await resolve_client_context(ctx, named)
        assert context.root == main, named
        assert context.report_root() == main, f"{named} names the memory root"
    for identity, named in (("named-root", str(main)), ("named-inside", str(inside))):
        report = (await _http_activate(http_context(main, identity=identity, grants=(worktree,)),
                                       workspace=named))["repo_context"]
        assert report["status"] == "needs_summary", named
        assert report["workspace"] == str(main) and report["repo_path"] == str(main), named
        assert "registered_workspace" not in report, "the described root is the memory root"
        assert "to persist the overview" in report["instruction"], "a write is possible here"
    assert describers == [str(main)], "the second caller answers from the main checkout's record"
    # A call that names nothing still follows the single grant: that case is unchanged.
    silent = (await _http_activate(http_context(main, identity="silent",
                                                grants=(worktree,))))["repo_context"]
    assert silent["workspace"] == str(worktree)
    assert silent["registered_workspace"] == str(main)
    # With no grant to override the choice the match records nothing, so a request whose
    # argument names its own registered root is still answered with the context it carried.
    plain = http_context(main)
    carried = plain.request_context.request.state.client_context
    assert await resolve_client_context(plain, str(main)) is carried
    assert carried.report_root() == main

@pytest.mark.asyncio
async def test_a_named_worktree_inside_the_memory_root_is_described_from_the_worktree(
        bundle, repositories, describers, monkeypatch):
    """Both authorized roots cover a nested grant, and the deeper one was named (CR2 F1).

    A linked worktree is usually registered inside its own main checkout -- this repository
    keeps them under `.worktrees/` -- so `context.root` covers the grant as well as the grant
    does and matched first. The report then carried the main checkout's path, hash and summary
    for a call that named the worktree, which is the very mismatch this parameter exists to
    correct. The memory-root test above uses a worktree beside the main checkout, where the
    first match happens to be the right one, so it passed throughout.
    """
    main, _other, _inside = repositories
    (main / ".worktrees").mkdir()
    nested = linked_worktree(main, main / ".worktrees" / "feature", name="nested")
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    context = await resolve_client_context(http_context(main, grants=(nested,)), str(nested))
    assert context.root == main, "the memory root is untouched"
    assert context.mismatch is None, "a grant of this session is not another repository"
    assert context.report_root() == nested, "the report follows the worktree the call named"
    report = (await _http_activate(http_context(main, identity="nested", grants=(nested,)),
                                   workspace=str(nested)))["repo_context"]
    assert report["repo_path"] == str(nested), report
    assert report["registered_workspace"] == str(main)
    assert report["repo_hash"] == RepoDescriber(repo_path=str(nested)).compute_repo_hash()
    assert describers == [str(nested)], "nothing described the main checkout in its place"
    # Naming the memory root itself is still answered from the memory root (CR1 F1).
    carried = await resolve_client_context(
        http_context(main, identity="root", grants=(nested,)), str(main))
    assert carried.report_root() == main, "the explicit memory-root choice is unchanged"


@pytest.mark.asyncio
async def test_a_summary_file_that_leaves_the_checkout_is_refused_before_it_is_read(
        bundle, repositories, describers, tmp_path, monkeypatch):
    """A report embeds a managed section, so the file it reads has to be in the checkout (CR1 F2).

    `plan()` reads `CLAUDE.md` through `managed_section.read_section`, which opens the path it is
    handed. A symlink there pointed the activation at a file outside the grant, and that file's
    managed section came back to the caller in `summary_preview`. `ClientContext.require_root`
    holds a session's memory root to exactly this rule, which is why `describe_repo` refuses such
    a workspace; a report follows the granted checkout the call named, which that method never
    sees, so the rule is applied to the root actually read.
    """
    registered, outside, _inside = repositories
    assert RepoDescriber(repo_path=str(registered)).claude_md_path == str(
        registered / persona.SUMMARY_FILE), "the checked path is the one the describer reads"
    _write_summary(registered)  # described, so its stored hash agrees with its tree
    elsewhere = RepoDescriber(repo_path=str(outside))
    elsewhere.write_summary(_valid_summary("outside-the-grant"), elsewhere.compute_repo_hash())
    kept = (outside / "CLAUDE.md").read_text(encoding="utf-8")
    assert "outside-the-grant" in kept, "the section a report would embed"
    (registered / "CLAUDE.md").unlink()
    (registered / "CLAUDE.md").symlink_to(outside / "CLAUDE.md")
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    report = (await _http_activate(http_context(registered)))["repo_context"]
    assert "outside-the-grant" not in json.dumps(report), report.get("summary_preview")
    assert str(outside) not in json.dumps(report), "and not the path it would be read from"
    assert report["status"] == "workspace_unsafe", report
    assert report["code"] == "memory_path_escapes_workspace"
    assert report["error"].startswith("workspace_unsafe: ")
    assert str(registered / "CLAUDE.md") in report["error"]
    for absent in ("summary_preview", "hash", "repo_hash", "repo_path", "registered_workspace"):
        assert absent not in report, absent
    # Nothing was described in its place, the installation checkout least of all.
    assert describers == [] and str(installation_root()) not in json.dumps(report)
    assert str(registered) not in persona._reported_workspaces
    assert (outside / "CLAUDE.md").read_text(encoding="utf-8") == kept, "and nothing was written"
    # The decision is recorded like an unavailable workspace: told once, and to each caller.
    assert "repo_context" not in await _http_activate(http_context(registered))
    second = (await _http_activate(http_context(registered, identity="b")))["repo_context"]
    assert second["status"] == "workspace_unsafe"
    # The deliberate tool already refused this workspace; the report no longer disagrees.
    refused = json.loads(await server.describe_repo(ctx=http_context(registered)))
    assert refused["status"] == "error" and "escapes workspace" in refused["error"]


@pytest.mark.asyncio
async def test_a_summary_file_symlinked_inside_the_checkout_is_still_read(bundle, tmp_path,
                                                                         monkeypatch):
    """The rule is containment, not the file type: a link that stays inside is a normal file."""
    root = tmp_path / "linked"
    root.mkdir()
    _seed_repo(root)
    root = root.resolve()
    written = _write_summary(root)
    section = (root / "CLAUDE.md").read_text(encoding="utf-8")
    # A file below the root does not feed the repository hash, so the description stays current.
    (root / "src" / "memory.md").write_text(section, encoding="utf-8")
    (root / "CLAUDE.md").unlink()
    (root / "CLAUDE.md").symlink_to(Path("src") / "memory.md")
    assert (root / "CLAUDE.md").is_symlink()
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    report = (await _http_activate(http_context(root)))["repo_context"]
    assert report["status"] == "up-to-date" and report["hash"] == written["hash"]
    assert "Project Identity" in report["summary_preview"]


@pytest.mark.asyncio
async def test_the_lookup_record_holds_no_advice_that_belongs_to_one_caller(bundle, repositories,
                                                                           tmp_path, monkeypatch):
    """The record says what the checkout is; what differs per caller is added after it (CR1 F4).

    One installation can hold both registrations of the same checkout: `workspace register
    <worktree>` records it as its own memory root, which is the action every refusal's remedy
    names, and setup registers the main checkout with that worktree as its physical grant. One
    daemon serves both, so both reach one lookup record. The record carried the first caller's
    `registered_workspace` and its persistence advice, so whichever called first decided what
    the other was told: a write advised where the caller's own workspace guard refuses it, or an
    `other_checkout` restriction imposed on a caller that has no other checkout.
    """
    main, _other, _inside = repositories
    worktree = linked_worktree(main, tmp_path / "worktree")
    registry = WorkspaceRegistry(tmp_path / "service")
    rooted_id = registry.register(main, physical_root=worktree)
    direct_id = registry.register(worktree)
    assert registry.resolve(rooted_id) == main and registry.resolve(direct_id) == worktree
    assert registry.cooperation_roots(rooted_id) == registry.cooperation_roots(direct_id) == (
        worktree,), "both registrations describe the same checkout"
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")

    def direct(identity):
        return http_context(worktree, identity=identity, grants=(worktree,))

    def rooted(identity):
        return http_context(main, identity=identity, grants=(worktree,))

    def assert_direct(report):
        assert report["repo_path"] == str(worktree), "its own memory root is the checkout"
        assert "registered_workspace" not in report
        assert "to persist the overview" in report["instruction"], "this caller can write here"
        assert "other_checkout" not in report["instruction"]

    def assert_rooted(report):
        assert report["repo_path"] == str(worktree)
        assert report["registered_workspace"] == str(main), "the root its write would resolve in"
        assert "other_checkout" in report["instruction"]
        assert "to persist the overview" not in report["instruction"]

    for order in ((direct, assert_direct, rooted, assert_rooted),
                  (rooted, assert_rooted, direct, assert_direct)):
        persona._reported_workspaces.clear()
        persona._reported_callers.clear()
        first_context, assert_first, second_context, assert_second = order
        first = (await _http_activate(first_context("first")))["repo_context"]
        assert first["status"] == "needs_summary"
        assert_first(first)
        second = (await _http_activate(second_context("second")))["repo_context"]
        assert second["status"] == "needs_summary"
        assert second["repo_hash"] == first["repo_hash"], "the checkout data is the same"
        assert_second(second)
    # A described checkout reads the same way: the note belongs to the caller, not to the record.
    _write_summary(worktree)
    described = (await _http_activate(rooted("described-rooted")))["repo_context"]
    assert described["status"] == "up-to-date"
    assert described["registered_workspace"] == str(main)
    assert "This report describes" in described["instruction"]
    own = (await _http_activate(direct("described-direct")))["repo_context"]
    assert own["status"] == "up-to-date" and own["hash"] == described["hash"]
    assert "registered_workspace" not in own
    assert "This report describes" not in own["instruction"]


@pytest.mark.asyncio
async def test_an_unavailable_workspace_is_recorded_like_a_described_one(bundle, workspace,
                                                                        tmp_path, monkeypatch):
    """P-C3's record half: the unavailable branch recorded nothing, so it repeated forever.

    `with_repo_context` runs on every SUCCESS and NO_CHANGE, and the unavailable branch
    returns before `_describe_workspace`, so nothing suppressed a repeat: every activation of
    a workspace-less session carried the same instruction again.
    """
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(Path.home()))
    engine_config._reset_client_repo_root_cache()
    first = (await activate(workspace=str(tmp_path / "one")))["repo_context"]
    assert first["status"] == "unavailable"
    assert "repo_context" not in await activate(workspace=str(tmp_path / "one")), \
        "the same caller is told once"
    later = (await activate(workspace=str(tmp_path / "two")))["repo_context"]
    assert later["status"] == "unavailable", "another caller is still told once"
