"""The first activation in a workspace carries the describe_repo result itself (#459).

Sessions keep working in a repository without calling ``describe_repo`` although a memory
lesson, a client instruction and the managed instruction block each asked for it (eight recorded
repeats in #459), so the signal cannot depend on the model remembering one. These tests pin what
the response carries: the describe_repo result on the first activation in a workspace, nothing on
a repeat in the same unchanged workspace, its own report for another workspace and a changed one,
a reported failure that marks nothing described and never describes the installation checkout in
a workspace's place, and no report at all in a prepared worker's contract.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import src.server as server
from src.engine import config as engine_config
from src.engine import persona
from src.memory.describer import RepoDescriber
from src.runtime_paths import installation_root
from src.schemas.protocol import PersonaDescriptor, PersonaResponse
from tests.test_describer import _seed_repo, _valid_summary
from tests.test_subagent import prepared, worker_tree  # noqa: F401
from tests.test_workspace_mismatch import http_context, repositories  # noqa: F401


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
    # Nothing was described, so nothing is marked described and the report keeps coming.
    assert describers == []
    assert str(workspace) not in persona._reported_workspaces
    assert (await activate())["repo_context"]["status"] == "unavailable"


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
    assert f"migrate --workspace {other}" in report["remedy"]
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
    assert f"migrate --workspace {other}" in report["remedy"]
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
    monkeypatch.setattr(persona, "_reported_workspaces", {})
    assert 0 < persona._MAX_REPORTED_WORKSPACES <= 1024, "the shipped bound must stay small"
    monkeypatch.setattr(persona, "_MAX_REPORTED_WORKSPACES", 2)
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
