"""Exercise the registered tools over MCP and the daemon's real workspace binding."""
import asyncio
import json

import httpx
import pytest

import src.server as server
from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceRegistry
from src.engine import config
from src.flows import FlowCatalog


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
    assert set(schema["properties"]) == {"flow", "request", "repo_path", "current_persona"}
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
    # A previous legacy lookup may legitimately have cached its fallback.
    assert config.get_client_repo_root() == config.INSTALL_ROOT
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
    assert result["error"].startswith("workspace_required: refusing")
    listing = json.loads(await server.list_flows())
    assert listing["status"] == "success" and listing["repo"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_error_results_and_listing_without_workspace(environment, monkeypatch):
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    catalog = json.loads(await server.list_flows())
    assert catalog["status"] == "success"
    assert [flow["id"] for flow in catalog["flows"]] == ["check"]
    result = json.loads(await server.run_flow("check"))
    assert result == {"status": "error", "error": "workspace_required"}
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
                assert result == {"status": "error", "error": code}
            rejected = await call(first_id, repo_path=str(second))
            assert rejected["status"] == "error"
            assert "within workspace" in rejected["error"]
    assert not list(first.iterdir()) and not list(second.iterdir())


@pytest.mark.asyncio
async def test_run_flow_activates_the_flow_persona(environment, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import src.component_catalog as catalog
    from src.engine import persona

    install, _ = environment
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "lib"))
    monkeypatch.setattr(catalog, "known_agents", lambda: {"code_reviewer"})
    monkeypatch.setattr(catalog, "known_ids", lambda kind: {"skill-a", "truth"})
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
    server.router.update_cache.assert_not_awaited()

    # A component removed after the choice was saved: refused, not an ERROR activation.
    monkeypatch.setattr(catalog, "known_ids", lambda kind: set())
    json.loads(await server.set_flow_persona("check", agent="code_reviewer", skills=[]))
    monkeypatch.setattr(catalog, "known_agents", lambda: set())
    refused = json.loads(await server.run_flow("check"))
    assert refused["status"] == "error" and "unknown agent" in refused["error"]
    monkeypatch.setattr(catalog, "known_agents", lambda: {"code_reviewer"})

    both = json.loads(await server.set_flow_persona("check", agent="code_reviewer", reset=True))
    assert both["status"] == "error" and "reset=true" in both["error"]
    error = json.loads(await server.set_flow_persona("check", skills=["skill-a"]))
    assert error["status"] == "error" and "need an agent" in error["error"]
    reset = json.loads(await server.set_flow_persona("check", reset=True))
    assert reset["flow"]["persona"] is None
