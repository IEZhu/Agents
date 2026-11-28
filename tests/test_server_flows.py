"""Exercise the registered tools over MCP and the daemon's real workspace binding."""
import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest

import src.server as server
from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceError, WorkspaceRegistry
from src.memory import fallback
from src.engine import config
from src.flows import FlowCatalog, FlowError


TOKEN = "f" * 48


@pytest.fixture
def environment(tmp_path, monkeypatch):
    install = tmp_path / "install" / "flows"
    install.mkdir(parents=True)
    (install / "README.md").write_text("# Catalog\n", encoding="utf-8")
    (install / "check.md").write_text("# Check repository\n\nInspect README.md.\n", encoding="utf-8")
    target = tmp_path / "client"
    target.mkdir()
    monkeypatch.setattr(server, "FlowCatalog", lambda: FlowCatalog(install))
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(target))
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    config._reset_client_repo_root_cache()
    yield install, target
    config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_registered_tool_schema_and_stdio_execution(environment):
    install, target = environment
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert "list_flows" in tools
    schema = tools["run_flow"].inputSchema
    assert schema["required"] == ["flow"]
    assert set(schema["properties"]) == {"flow", "request", "repo_path", "current_persona", "workspace",
                                         "force_reload"}
    content, _ = await server.mcp.call_tool("run_flow", {"flow": "check", "request": "no-merge"})
    result = json.loads(content[0].text)
    assert result["status"] == "needs_execution"
    assert result["repo_path"] == str(target)
    assert result["workspace_id"] is None
    assert result["flow"]["source_path"] == str(install / "check.md")
    assert result["request"] == "no-merge"
    assert not list(target.iterdir())  # Loading does not execute the flow or write memory.


@pytest.mark.asyncio
async def test_target_subdirectory_and_escape(environment):
    _, target = environment
    child = target / "nested"
    child.mkdir()
    result = json.loads(await server.run_flow("check", repo_path="nested"))
    assert result["repo_path"] == str(child)
    outside = target.parent / "outside"
    outside.mkdir()
    (target / "escape").symlink_to(outside)
    for requested in ("..", str(outside), "escape", "absent", "\x00"):
        error = json.loads(await server.run_flow("check", repo_path=requested))
        assert error["status"] == "error"
        assert "within workspace" in error["error"]


@pytest.mark.asyncio
async def test_reading_a_flow_does_not_require_writable_memory_paths(environment):
    _, target = environment
    (target / "CLAUDE.md").symlink_to(target.parent / "shared-instructions.md")
    result = json.loads(await server.run_flow("check"))
    assert result["status"] == "needs_execution"


@pytest.mark.asyncio
async def test_stdio_cwd_uses_caller_not_installation(environment, monkeypatch):
    _, target = environment
    (target / ".git").mkdir()
    nested = target / "sub"
    nested.mkdir()
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT")
    monkeypatch.chdir(nested)
    result = json.loads(await server.run_flow("check"))
    assert result["repo_path"] == str(target)


@pytest.mark.asyncio
async def test_no_install_fallback_when_stdio_cwd_is_lost(environment, monkeypatch):
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT")
    def missing_cwd():
        raise FileNotFoundError("cwd unavailable")
    monkeypatch.setattr(config.os, "getcwd", missing_cwd)
    with pytest.raises(config.ClientRootError, match="cwd unavailable"):
        config.get_client_repo_root()
    result = json.loads(await server.run_flow("check"))
    assert result["status"] == "error"
    assert "cwd unavailable" in result["error"]


@pytest.mark.asyncio
async def test_stdio_cwd_in_windows_directory_is_refused(environment, tmp_path, monkeypatch):
    # The Claude desktop app starts stdio servers in C:\Windows\System32.
    windows = tmp_path / "Windows"
    (windows / "System32").mkdir(parents=True)
    (windows / "CLAUDE.md").write_text("")  # keeps the walk-up inside tmp_path
    monkeypatch.setattr(config, "_windows_directory", lambda: windows.resolve())
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT")
    monkeypatch.chdir(windows / "System32")
    result = json.loads(await server.run_flow("check"))
    assert result["status"] == "error"
    assert result["error"].startswith("workspace_unsafe: refusing")
    listing = json.loads(await server.list_flows())
    assert listing["status"] == "success" and listing["repo"]["status"] == "unavailable"
    # Workspace errors say which process refused and what it saw (#231); no client named the desktop app.
    repo_flow = json.loads(await server.get_flow("repo:check"))
    assert repo_flow["error"].startswith("repo_scope_unavailable: workspace_unsafe: refusing")
    for refused in (result, repo_flow):
        assert refused["pid"] == os.getpid() and refused["workspace_inputs"]["cwd"] == os.getcwd()
        assert "hint" not in refused


def test_flow_workspace_errors_from_a_desktop_started_server_carry_a_hint(monkeypatch):
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    session = SimpleNamespace(check_client_capability=lambda _: True, client_params=SimpleNamespace(
        clientInfo=SimpleNamespace(name="local-agent-mode-Agents-Core")))
    ctx = SimpleNamespace(session=session)
    for error in (WorkspaceError("workspace_unsafe: refusing"),
                  FlowError("repo_scope_unavailable: workspace_unsafe: refusing")):
        result = json.loads(server._flow_error(error, ctx))
        assert result["pid"] == os.getpid() and result["workspace_inputs"]["client"] == session.client_params.clientInfo.name
        assert "claude_desktop_config.json" in result["hint"]
    # Any other flow error stays as it was.
    assert json.loads(server._flow_error(FlowError("flow_not_found: x"), ctx)) == {
        "status": "error", "error": "flow_not_found: x"}


@pytest.mark.asyncio
async def test_error_results_and_listing_without_workspace(environment, monkeypatch):
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    catalog = json.loads(await server.list_flows())
    assert catalog["status"] == "success"
    assert [flow["id"] for flow in catalog["flows"]] == ["check"]
    result = json.loads(await server.run_flow("check"))
    assert result == {"status": "error", "error": "workspace_required", "remedy": fallback.REMEDY}
    monkeypatch.delenv("AGENTS_TRANSPORT")
    for name, code in [("absent", "flow_not_found"), ("../check", "flow_invalid")]:
        result = json.loads(await server.run_flow(name))
        assert result["status"] == "error" and code in result["error"]


@pytest.mark.asyncio
async def test_http_calls_are_isolated_and_cannot_override_identity(environment, tmp_path, monkeypatch):
    install, first = environment
    second = tmp_path / "second"
    second.mkdir()
    registry = WorkspaceRegistry(tmp_path / "service")
    first_id, second_id = registry.register(first), registry.register(second)
    # Use the actual registered server tools, with the production middleware.
    # Restore transport settings after the test to avoid affecting other tests.
    monkeypatch.setattr(server.mcp.settings, "stateless_http", True)
    monkeypatch.setattr(server.mcp.settings, "json_response", True)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = create_app(registry.directory, TOKEN, runtime_loader=lambda port: (server.mcp, None))
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"}
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if app.state.service.state != "starting":
                break
            await asyncio.sleep(.01)
        assert app.state.service.state == "ready"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                    base_url="http://127.0.0.1:8765", headers=headers) as http:
            async def call(identity=None, **arguments):
                response = await http.post("/mcp", headers={"X-Agents-Workspace": identity} if identity else {},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "run_flow", "arguments": {"flow": "check", **arguments}}})
                assert response.status_code == 200
                return json.loads(response.json()["result"]["content"][0]["text"])

            identities = [first_id, second_id] * 5
            results = await asyncio.gather(*(call(identity) for identity in identities))
            for identity, result in zip(identities, results):
                assert result["status"] == "needs_execution"
                assert result["workspace_id"] == identity
                assert result["repo_path"] == str(first if identity == first_id else second)
                assert result["flow"]["source_path"] == str(install / "check.md")
            for identity, code in [(None, "workspace_required"), ("invalid", "workspace_invalid")]:
                result = await call(identity, repo_path=str(first))
                assert result == {"status": "error", "error": code, "remedy": fallback.REMEDY}
            rejected = await call(first_id, repo_path=str(second))
            assert rejected["status"] == "error"
            assert "within workspace" in rejected["error"]
    assert not list(first.iterdir()) and not list(second.iterdir())


@pytest.mark.asyncio
async def test_run_flow_activates_the_flow_persona(environment, tmp_path, monkeypatch,
                                                  persona_components):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.engine import persona

    install, _ = environment
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "lib"))
    calls = []

    async def build(agent, query, history, tier=None, selection=None):
        calls.append((agent, query, selection))
        return SimpleNamespace(
            agent=agent, bundle_revision="c" * 64, scope="Review code",
            persona_block="Role", rules_block="Rules", skills_block="Skills",
            implants_block="", skills_loaded=["skill-a"], implants_loaded=[],
            rules_loaded=["truth"], tier="standard")
    monkeypatch.setattr(persona, "build_persona_bundle", build)
    monkeypatch.setattr(server.router, "update_cache", AsyncMock())
    from src import client_instructions
    monkeypatch.setattr(client_instructions, "drift_warnings", lambda context=None: ["outdated section"])

    plain = json.loads(await server.run_flow("check"))
    assert "persona_activation" not in plain and plain["flow"]["persona"] is None

    saved = json.loads(await server.set_flow_persona(
        "check", agent="code_reviewer", skills=["skill-a"], implants=[], rules=["truth"]))
    assert saved["flow"]["persona_source"] == "overlay"
    result = json.loads(await server.run_flow("check", request="PR 7"))
    assert "take precedence over the persona" in result["instruction"]
    activation = result["persona_activation"]
    assert activation["status"] == "SUCCESS"
    assert activation["persona"]["agent"] == "code_reviewer"
    assert activation["replaces_activation_id"] is None
    # A first activation: the full instruction and the warnings about managed sections (#433).
    assert activation["instruction"] == persona.APPLY_INSTRUCTION
    assert activation["warnings"] == ["outdated section"]
    agent, query, selection = calls[-1]
    assert (agent, query) == ("code_reviewer", "Check repository\nPR 7")
    assert (selection.skills, selection.implants, selection.rules) == (("skill-a",), (), ("truth",))
    server.router.update_cache.assert_not_awaited()  # A flow's choice is not routing feedback.

    again = json.loads(await server.run_flow("check", current_persona=activation["persona"]))
    assert again["persona_activation"]["status"] == "NO_CHANGE"

    # Same agent, other components: compared by revision, so not NO_CHANGE.
    default_only = json.loads(await server.set_flow_persona("check", agent="code_reviewer"))
    assert default_only["flow"]["persona"] == {"agent": "code_reviewer"}
    revisions = iter(["d" * 64])

    async def build_default(agent, query, history, tier=None, selection=None):
        calls.append((agent, query, selection))
        bundle = await build(agent, query, history, tier, selection)
        bundle.bundle_revision = next(revisions)
        return bundle
    monkeypatch.setattr(persona, "build_persona_bundle", build_default)
    switched = json.loads(await server.run_flow("check", current_persona=activation["persona"]))
    assert switched["persona_activation"]["status"] == "SUCCESS"
    assert calls[-1][2].skills is None  # The agent's default selection.
    # Later in the conversation: a pointer to the full instruction, and no warnings.
    assert switched["persona_activation"]["instruction"] == persona.INSTRUCTION_POINTER
    assert "warnings" not in switched["persona_activation"]
    # A lost instruction of a flow's persona comes back with force_reload, replacing the active
    # one even though its bundle revision is unchanged.
    monkeypatch.setattr(persona, "build_persona_bundle", build)
    active = {**switched["persona_activation"]["persona"], "bundle_revision": "c" * 64}
    unchanged = json.loads(await server.run_flow("check", current_persona=active))
    assert unchanged["persona_activation"]["status"] == "NO_CHANGE"
    restored = json.loads(await server.run_flow("check", current_persona=active, force_reload=True))
    assert restored["persona_activation"]["status"] == "SUCCESS"
    assert restored["persona_activation"]["instruction"] == persona.APPLY_INSTRUCTION
    assert restored["persona_activation"]["replaces_activation_id"] == active["activation_id"]
    assert "warnings" not in restored["persona_activation"]
    server.router.update_cache.assert_not_awaited()

    # A component removed after the choice was saved: refused, not an ERROR activation.
    json.loads(await server.set_flow_persona("check", agent="code_reviewer", skills=["skill-a"]))
    (persona_components / "skills" / "skill-a.mdc").unlink()
    refused = json.loads(await server.run_flow("check"))
    assert refused["status"] == "error" and "unknown skills: skill-a" in refused["error"]

    both = json.loads(await server.set_flow_persona("check", agent="code_reviewer", reset=True))
    assert both["status"] == "error" and "reset=true" in both["error"]
    error = json.loads(await server.set_flow_persona("check", skills=["skill-a"]))
    assert error["status"] == "error" and "need an agent" in error["error"]
    reset = json.loads(await server.set_flow_persona("check", reset=True))
    assert reset["flow"]["persona"] is None


@pytest.mark.asyncio
async def test_run_flow_persona_waits_for_readiness(environment, tmp_path, monkeypatch, persona_components):
    from src.engine import persona

    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "lib"))
    json.loads(await server.set_flow_persona("check", agent="code_reviewer"))
    built = []

    async def build(*args, **kwargs):
        built.append(args)

    async def warming(tool):
        return "warming_up"
    monkeypatch.setattr(persona, "build_persona_bundle", build)
    monkeypatch.setattr(server, "_readiness_problem", warming)

    result = json.loads(await server.run_flow("check"))
    assert result["status"] == "needs_execution"
    assert result["persona_activation"]["status"] == "ERROR"
    assert result["persona_activation"]["message"].startswith("warming_up")
    assert not built


ENGINEER = {"agent": "software_engineer", "activation_id": "before-flow", "bundle_revision": "e" * 64,
            "scope": "Write code", "skills_loaded": ["skill-a"], "implants_loaded": [], "rules_loaded": ["truth"]}


@pytest.fixture
def switch_back(environment, tmp_path, monkeypatch, persona_components):
    """Two flows with personas over fake bundles (#456): check runs as code_reviewer with the exact
    skill list ["skill-a"], close as investigative_analyst. A bundle with an exact skill list has
    revision c..c and any other d..d, so a flow's lists and the agent's own bundle differ."""
    from unittest.mock import AsyncMock

    from src import client_instructions
    from src.engine import persona

    install, _ = environment
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "lib"))
    (install / "close.md").write_text("# Close thread\n\nReport what happened.\n", encoding="utf-8")
    analyst = persona_components / "agents" / "investigative_analyst" / "system_prompt.mdc"
    analyst.parent.mkdir(parents=True)
    analyst.write_text("---\nidentity: {name: investigative_analyst, role: Role}\n---\nBody\n", encoding="utf-8")
    builds = []

    async def build(agent, query, history, tier=None, selection=None):
        builds.append(agent)
        exact = selection is not None and selection.skills is not None
        return SimpleNamespace(
            agent=agent, bundle_revision=("c" if exact else "d") * 64, scope="Role scope",
            persona_block="Role", rules_block="Rules", skills_block="Skills", implants_block="",
            skills_loaded=list(selection.skills) if exact else [], implants_loaded=[], rules_loaded=[],
            tier="standard")
    monkeypatch.setattr(persona, "build_persona_bundle", build)
    monkeypatch.setattr(server.router, "update_cache", AsyncMock())
    monkeypatch.setattr(client_instructions, "drift_warnings", lambda context=None: [])
    return builds


async def _flow(name, **arguments):
    return json.loads(await server.run_flow(name, **arguments))


async def _load(agent, current, **arguments):
    return json.loads(await server.get_agent_context(agent, "Tick the checklist item and comment on it.",
                                                     current_persona=current, **arguments))


@pytest.mark.asyncio
async def test_run_flow_returns_the_persona_to_switch_back_to(switch_back):
    """#456: nothing told a session to leave a flow's persona, so the answers after thread-close ran
    under it. A flow's persona_activation comes with previous_persona, the current_persona run_flow
    received, null without one; a flow without a persona returns neither."""
    plain = await _flow("check", current_persona=ENGINEER)
    assert "persona_activation" not in plain and "previous_persona" not in plain

    json.loads(await server.set_flow_persona("check", agent="code_reviewer", skills=["skill-a"]))
    switched = await _flow("check", current_persona=ENGINEER)
    assert switched["persona_activation"]["status"] == "SUCCESS"
    assert switched["persona_activation"]["replaces_activation_id"] == "before-flow"
    assert switched["persona_activation"]["persona"]["activation_id"].startswith("flow-")
    assert switched["previous_persona"] == ENGINEER

    fresh = await _flow("check")
    assert fresh["persona_activation"]["status"] == "SUCCESS" and fresh["previous_persona"] is None

    async def warming(tool):
        return "warming_up"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(server, "_readiness_problem", warming)
        waiting = await _flow("check", current_persona=ENGINEER)
    assert waiting["persona_activation"]["status"] == "ERROR"
    assert waiting["previous_persona"] == ENGINEER

    tool = {t.name: t for t in await server.mcp.list_tools()}["run_flow"]
    assert "previous_persona" in " ".join(tool.description.split())


@pytest.mark.asyncio
async def test_run_flow_under_a_flow_activation_returns_no_previous_persona(switch_back):
    """#456 review: a re-call under the flow's own persona (NO_CHANGE, or a force_reload restore after
    a compaction) echoed that persona as the role to go back to. It now returns none, so the last
    previous_persona the caller received still names the role from before the flow."""
    json.loads(await server.set_flow_persona("check", agent="code_reviewer", skills=["skill-a"]))
    reviewer = (await _flow("check", current_persona=ENGINEER))["persona_activation"]["persona"]

    again = await _flow("check", current_persona=reviewer)
    assert again["persona_activation"]["status"] == "NO_CHANGE"
    assert "previous_persona" not in again

    restored = await _flow("check", current_persona=reviewer, force_reload=True)
    assert restored["persona_activation"]["status"] == "SUCCESS"
    assert restored["persona_activation"]["replaces_activation_id"] == reviewer["activation_id"]
    assert restored["persona_activation"]["persona"]["activation_id"].startswith("flow-")
    assert "previous_persona" not in restored


@pytest.mark.asyncio
async def test_a_second_flow_leaves_the_role_from_before_the_first(switch_back):
    """#456 review: pr-review then thread-close under code_reviewer handed code_reviewer back. The
    second flow returns no previous_persona, and switching back with the active descriptor lands on
    the role from before the first flow."""
    json.loads(await server.set_flow_persona("check", agent="code_reviewer", skills=["skill-a"]))
    json.loads(await server.set_flow_persona("close", agent="investigative_analyst"))
    first = await _flow("check", current_persona=ENGINEER)
    assert first["previous_persona"] == ENGINEER

    second = await _flow("close", current_persona=first["persona_activation"]["persona"])
    analyst = second["persona_activation"]["persona"]
    assert second["persona_activation"]["status"] == "SUCCESS" and analyst["agent"] == "investigative_analyst"
    assert "previous_persona" not in second

    back = await _load(first["previous_persona"]["agent"], analyst)
    assert back["status"] == "SUCCESS" and back["persona"]["agent"] == "software_engineer"
    assert back["replaces_activation_id"] == analyst["activation_id"]
    assert not back["persona"]["activation_id"].startswith("flow-")


@pytest.mark.asyncio
async def test_a_refresh_or_restore_under_a_flow_stays_the_flows_activation(switch_back):
    """#456 re-verification: refresh_persona_context and a force_reload restore, which a session
    readiness reminder prescribes after a compaction, rebuilt the flow persona's own agent without
    the flow- mark, so the next flow named the flow's agent as previous_persona. Both keep the mark,
    and the next flow returns none; a force_reload load of another agent is a plain switch."""
    json.loads(await server.set_flow_persona("check", agent="code_reviewer", skills=["skill-a"]))
    json.loads(await server.set_flow_persona("close", agent="investigative_analyst"))
    reviewer = (await _flow("check", current_persona=ENGINEER))["persona_activation"]["persona"]

    refreshed = json.loads(await server.refresh_persona_context("Also check the tests.", current_persona=reviewer))
    restored = await _load("code_reviewer", reviewer, force_reload=True)
    for rebuilt in (refreshed, restored):
        assert rebuilt["status"] == "SUCCESS" and rebuilt["replaces_activation_id"] == reviewer["activation_id"]
        assert rebuilt["persona"]["agent"] == "code_reviewer"
        assert rebuilt["persona"]["activation_id"].startswith("flow-")
        chained = await _flow("close", current_persona=rebuilt["persona"])
        assert chained["persona_activation"]["status"] == "SUCCESS" and "previous_persona" not in chained

    elsewhere = await _load("software_engineer", reviewer, force_reload=True)
    assert elsewhere["status"] == "SUCCESS" and not elsewhere["persona"]["activation_id"].startswith("flow-")


@pytest.mark.asyncio
async def test_switching_back_to_the_flows_own_agent_replaces_the_flows_lists(switch_back):
    """#456 review: get_agent_context answers NO_CHANGE by agent name, so a code_reviewer session that
    ran a code_reviewer flow with its own skill list kept that list after switching back. From a
    flow's activation the agent's bundle is rebuilt and compared by revision; from any other
    activation of the same agent it is still NO_CHANGE without a rebuild."""
    reviewer = (await _load("code_reviewer", None))["persona"]
    assert reviewer["bundle_revision"] == "d" * 64
    json.loads(await server.set_flow_persona("check", agent="code_reviewer", skills=["skill-a"]))
    flow = await _flow("check", current_persona=reviewer)
    assert flow["previous_persona"] == reviewer
    listed = flow["persona_activation"]["persona"]
    assert listed["bundle_revision"] == "c" * 64 and listed["skills_loaded"] == ["skill-a"]

    back = await _load("code_reviewer", listed)
    assert back["status"] == "SUCCESS" and back["replaces_activation_id"] == listed["activation_id"]
    assert back["persona"]["bundle_revision"] == "d" * 64 and back["persona"]["skills_loaded"] == []
    assert not back["persona"]["activation_id"].startswith("flow-")

    builds = len(switch_back)
    kept = await _load("code_reviewer", back["persona"])
    assert kept["status"] == "NO_CHANGE" and len(switch_back) == builds


@pytest.mark.asyncio
async def test_stdio_empty_cwd_works_but_program_files_are_refused(environment, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT")
    monkeypatch.setattr(config, "_CLIENT_ROOT_MARKERS", ("no-such-marker",))
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    config._reset_client_repo_root_cache()
    result = json.loads(await server.run_flow("check"))
    assert result["status"] == "needs_execution" and result["repo_path"] == str(plain.resolve())

    programs = tmp_path / "Program Files"
    (programs / "tool").mkdir(parents=True)
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    monkeypatch.setattr(config, "_windows_directory", lambda: None)
    monkeypatch.setenv("ProgramFiles", str(programs))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(programs / "tool"))
    config._reset_client_repo_root_cache()
    result = json.loads(await server.run_flow("check"))
    assert result["status"] == "error" and result["error"].startswith("workspace_unsafe: refusing")
    assert "repo_path" not in result
