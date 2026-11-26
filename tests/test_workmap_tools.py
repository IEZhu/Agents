"""The two work-map tools over the registered workspace."""
import json

import pytest

import src.server as server
from src.engine import config as engine_config
from src.result_size import RESULT_SIZE_CEILING
from src.schemas.subagent import MAX_RESULT_CHARS
from src.workmap.store import WorkmapStore
from src.workmap.view import DEFAULT_BUDGET

TASK = "task-1"


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    engine_config._reset_client_repo_root_cache()
    yield tmp_path
    engine_config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_the_tools_write_and_read_a_map(served):
    applied = json.loads(await server.workmap_update(task_id=TASK, ops=[
        {"op": "add_node", "id": "goal", "kind": "goal", "title": "Ship it"},
        {"op": "add_node", "id": "map-code", "title": "Map the code"}]))
    assert applied["status"] == "applied" and applied["revision"] == 3
    assert applied["workspace"]["root"] == str(served)
    viewed = json.loads(await server.workmap_view(task_id=TASK))
    assert viewed["mode"] == "map" and "map-code: Map the code" in viewed["view"]
    node = json.loads(await server.workmap_view(task_id=TASK, node="map-code"))
    assert node["mode"] == "node" and node["view"].startswith("node map-code")
    listed = json.loads(await server.workmap_view())
    assert listed["mode"] == "list" and listed["total"] == 1


@pytest.mark.asyncio
async def test_a_refused_operation_is_an_error_payload(served):
    payload = json.loads(await server.workmap_update(task_id=TASK, ops=[
        {"op": "add_node", "id": "x", "title": "X"},
        {"op": "update_node", "id": "x", "status": "validated"}]))
    assert payload == {"status": "error", "code": "status_reserved", "error": "ops[1]: only the server sets validated"}


@pytest.mark.asyncio
async def test_viewing_an_unknown_map_or_missing_result_is_reported(served):
    assert json.loads(await server.workmap_view(task_id=TASK))["code"] == "unknown_map"
    await server.workmap_update(task_id=TASK, ops=[{"op": "add_node", "id": "x", "title": "X"}])
    assert json.loads(await server.workmap_view(task_id=TASK, node="x", detail="result"))["code"] == "no_result"


@pytest.mark.asyncio
async def test_the_view_declares_the_result_ceiling():
    """detail="result" returns a whole stored result, up to 128,000 characters before escaping."""
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert tools["workmap_view"].meta == {"anthropic/maxResultSizeChars": RESULT_SIZE_CEILING}
    assert MAX_RESULT_CHARS * 2 < RESULT_SIZE_CEILING


@pytest.mark.asyncio
async def test_the_list_keeps_the_newest_maps_that_fit_the_default_budget(served):
    for index in range(60):
        store = WorkmapStore(served, clock=lambda index=index: 1_800_000_000.0 + index)
        store.apply(f"task-{index:02d}", [{"op": "add_node", "id": "goal", "kind": "goal",
                                           "title": f"Goal {index:02d} " + "g" * 100}])
    listed = json.loads(await server.workmap_view())
    lines = listed["view"].splitlines()
    assert listed["total"] == 60 and len(listed["view"]) <= DEFAULT_BUDGET
    assert lines[0].startswith("task-59 ") and lines[1].startswith("task-58 ")
    shown = len(lines) - 1
    assert lines[-1] == f"… {60 - shown} more maps: name a task_id to read one" and 10 < shown < 60


def test_the_tool_texts_state_the_goal_dependency_refusal_and_the_registered_workspace():
    import asyncio

    import src.server as server

    docstring = " ".join(server.workmap_update.__doc__.split())
    assert "and so is a depends_on edge onto a goal" in docstring
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    node = tools["get_subagent_context"].inputSchema["properties"]["workmap_node"]["description"]
    assert "registered workspace" in node and "no workspace argument" in node
