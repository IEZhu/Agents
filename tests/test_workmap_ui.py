"""The Work map tab's reads: registered workspaces' work maps, behind the UI session and the page
header, read-only. No model and no network."""
from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest
import pytest_asyncio

from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceRegistry
from src.workmap.store import WorkmapStore

TOKEN = "s" * 48
UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}
TASK = "release-2026:demo"
CONTRACT = "c" * 64
RESULT = ("task: release-2026:demo\nstatus: completed\n\n## summary\n<script>alert(1)</script> done\n\n"
          "## artifacts\n- artifact://m1/" + "d" * 64 + "/a1 — the report\n- docs/out.md — the page\n\n"
          "## evidence\n[^e1]: see artifact://m2/" + "e" * 64 + "/a2 — a log\n")


def build_map(root, *, clock=1_800_000_000.0, task=TASK):
    store = WorkmapStore(root, clock=lambda: clock)
    store.apply(task, [
        {"op": "add_node", "id": "goal", "kind": "goal", "title": "Ship the demo"},
        {"op": "add_node", "id": "build", "title": "Build the page", "risk": "routine"},
        {"op": "add_node", "id": "review", "title": "Review the page"},
        {"op": "add_node", "id": "ask", "kind": "question", "title": "Which port?"},
        {"op": "add_edge", "from": "review", "to": "build", "type": "depends_on"},
        {"op": "add_edge", "from": "review", "to": "build", "type": "verifies"},
        {"op": "add_edge", "from": "ask", "to": "goal", "type": "contradicts"}])
    return store


def run_stage(store, task=TASK):
    store.record_prepared(task, "build", contract_revision=CONTRACT, attempt_id="1", complexity="standard")
    sha = hashlib.sha256(RESULT.encode()).hexdigest()
    store.record_result(task, "build", contract_revision=CONTRACT, attempt_id="1", text=RESULT, sha256=sha,
                        handback="line", outcome={"status": "validated", "level": "V2", "summary": "Built it",
                                                  "checks": {"passed": 3, "failed": 0, "not_run": 1},
                                                  "findings": None, "limitations": 2, "result_status": "completed",
                                                  "question": None, "escalation": None})
    return sha


@pytest_asyncio.fixture
async def daemon(tmp_path):
    project, other, empty = (tmp_path / name for name in ("project", "other", "empty"))
    for path in (project, other, empty):
        path.mkdir()
    sha = run_stage(build_map(project))
    build_map(other, clock=1_700_000_000.0, task="older-task")
    registry = WorkspaceRegistry(tmp_path / "service")
    ids = {name: registry.register(path) for name, path in
           (("project", project), ("other", other), ("empty", empty))}

    def runtime(port):
        from mcp.server.fastmcp import FastMCP
        return FastMCP("test", stateless_http=True, json_response=True), None

    app = create_app(registry.directory, TOKEN, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.service.state != "starting":
                break
            await asyncio.sleep(.01)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://127.0.0.1:8765") as http:
            yield http, ids, project, sha


async def login(http) -> None:
    code = (await http.post("/admin/ui/code", headers={"Authorization": "Bearer " + TOKEN})).json()["code"]
    assert (await http.post("/ui/api/session", json={"code": code}, headers=UI)).status_code == 200


def paths(ids):
    ws = ids["project"]
    return ["/ui/api/workmap/maps", f"/ui/api/workmap/map?workspace={ws}&task_id={TASK}",
            f"/ui/api/workmap/node?workspace={ws}&task_id={TASK}&node=build",
            f"/ui/api/workmap/result?workspace={ws}&task_id={TASK}&node=build"]


@pytest.mark.asyncio
async def test_the_reads_need_a_session_and_the_page_header_and_are_not_activity(daemon):
    http, ids, _, _ = daemon
    service = http._transport.app.state.service
    for path in paths(ids):
        assert (await http.get(path, headers=UI)).status_code == 401
        assert (await http.get(path, headers=UI)).json() == {"error": "session_required"}
    await login(http)
    for path in paths(ids):
        # Without the header another origin could read the maps with the cookie.
        assert (await http.get(path)).status_code == 403
        assert (await http.get(path, headers={"X-Agents-UI": "1", "Origin": "http://evil.test"})).status_code == 403
        service.last_activity = 0.0
        assert (await http.get(path, headers=UI)).status_code == 200, path
        assert service.last_activity == 0.0  # an open map's polls never hold back an update


@pytest.mark.asyncio
async def test_no_route_writes(daemon):
    http, ids, project, _ = daemon
    await login(http)
    before = {path.name: path.read_bytes() for path in (project / "data" / "workmap").iterdir()}
    for path in paths(ids):
        for method in ("POST", "PUT", "DELETE"):
            response = await http.request(method, path, headers=UI, json={})
            assert response.status_code == 405 and response.headers["allow"] == "GET", (method, path)
    assert {path.name: path.read_bytes() for path in (project / "data" / "workmap").iterdir()} == before


@pytest.mark.asyncio
async def test_the_list_shows_each_workspaces_maps_newest_first(daemon):
    http, ids, _, _ = daemon
    await login(http)
    listed = (await http.get("/ui/api/workmap/maps", headers=UI)).json()["workspaces"]
    assert [item["workspace"] for item in listed] == [ids["project"], ids["other"]]  # the empty one is left out
    first = listed[0]["maps"][0]
    assert first["task_id"] == TASK and first["goal"] == "Ship the demo" and first["closed"] is False
    assert first["counts"] == {"planned": 3, "validated": 1} and first["nodes"] == 4 and first["running"] == []
    assert first["updated"] == "2027-01-15T08:00:00Z"


@pytest.mark.asyncio
async def test_one_map_is_a_graph_with_its_current_stage(daemon):
    http, ids, _, sha = daemon
    await login(http)
    response = await http.get(f"/ui/api/workmap/map?workspace={ids['project']}&task_id={TASK}", headers=UI)
    graph = response.json()
    assert response.status_code == 200 and graph["workspace"] == ids["project"] and graph["name"] == "project"
    assert [node["id"] for node in graph["nodes"]] == ["goal", "build", "ask", "review"]
    assert {(edge["from"], edge["to"], edge["type"]) for edge in graph["edges"]} == {
        ("review", "build", "depends_on"), ("review", "build", "verifies"), ("ask", "goal", "contradicts")}
    build = next(node for node in graph["nodes"] if node["id"] == "build")
    assert (build["status"], build["level"], build["result_status"]) == ("validated", "V2", "completed")
    assert build["checks"] == {"passed": 3, "failed": 0, "not_run": 1} and build["limitations"] == 2
    assert build["contract_revision"] == CONTRACT and build["result_sha256"] == sha and build["attempt_id"] == "1"
    assert graph["current"] == "build"  # nothing runs, so the node that changed last


@pytest.mark.asyncio
async def test_a_prepared_stage_is_the_current_one(daemon):
    http, ids, project, _ = daemon
    WorkmapStore(project).record_prepared(TASK, "review", contract_revision="f" * 64, attempt_id="1",
                                          complexity="high")
    await login(http)
    graph = (await http.get(f"/ui/api/workmap/map?workspace={ids['project']}&task_id={TASK}", headers=UI)).json()
    assert graph["current"] == "review" and graph["running"] == ["review"]


@pytest.mark.asyncio
async def test_a_link_without_a_workspace_finds_the_map(daemon):
    http, ids, _, _ = daemon
    await login(http)
    graph = (await http.get("/ui/api/workmap/map?task_id=older-task", headers=UI)).json()
    assert graph["workspace"] == ids["other"] and graph["task_id"] == "older-task"


@pytest.mark.asyncio
async def test_the_node_record_holds_history_relations_and_artifacts_but_not_the_result(daemon):
    http, ids, _, sha = daemon
    await login(http)
    record = (await http.get(f"/ui/api/workmap/node?workspace={ids['project']}&task_id={TASK}&node=build",
                             headers=UI)).json()
    assert [(step["status"], step["level"], step["op"]) for step in record["history"]] == [
        ("planned", "V0", "add_node"), ("prepared", "V0", "prepared"), ("validated", "V2", "result")]
    assert record["relations"]["needed_by"] == ["review"] and record["relations"]["verified_by"] == ["review"]
    assert record["artifacts"] == [
        {"ref": "artifact://m1/" + "d" * 64 + "/a1", "purpose": "the report"},
        {"ref": "docs/out.md", "purpose": "the page"},
        {"ref": "artifact://m2/" + "e" * 64 + "/a2", "purpose": ""}]
    assert record["has_result"] is True and record["result_chars"] == len(RESULT)
    assert "<script>" not in str(record)  # the text comes only from the result route
    assert record["node"]["result_sha256"] == sha


@pytest.mark.asyncio
async def test_the_result_is_served_as_json_text_only_on_its_own_route(daemon):
    http, ids, _, _ = daemon
    await login(http)
    response = await http.get(f"/ui/api/workmap/result?workspace={ids['project']}&task_id={TASK}&node=build",
                              headers=UI)
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/json")
    assert response.json()["text"] == RESULT and response.json()["truncated"] is False


@pytest.mark.asyncio
async def test_unknown_maps_nodes_results_and_workspaces_are_refused(daemon):
    http, ids, _, _ = daemon
    await login(http)
    ws = ids["project"]
    cases = {f"/ui/api/workmap/map?workspace={ws}&task_id=nope": (404, "unknown_map"),
             "/ui/api/workmap/map?task_id=nope": (404, "unknown_map"),
             f"/ui/api/workmap/node?workspace={ws}&task_id={TASK}&node=nope": (404, "unknown_node"),
             f"/ui/api/workmap/node?workspace={ws}&task_id=nope&node=build": (404, "unknown_map"),
             f"/ui/api/workmap/result?workspace={ws}&task_id={TASK}&node=review": (404, "no_result"),
             f"/ui/api/workmap/map?workspace={ws}&task_id=../x": (400, "invalid_request"),
             f"/ui/api/workmap/map?workspace=not-a-uuid&task_id={TASK}": (400, "workspace_invalid"),
             "/ui/api/workmap/other": (404, "not_found")}
    for path, (status, code) in cases.items():
        response = await http.get(path, headers=UI)
        assert response.status_code == status, (path, response.json())
        assert code in str(response.json()), (path, response.json())


def test_artifacts_come_from_the_section_and_from_references_elsewhere():
    from src.workmap.web import artifacts
    assert artifacts(None) == [] and artifacts("") == []
    text = "## Artifacts\n- `out/a.png` -- a screenshot\n- plain\n## changes\n- not/an/artifact.py — x\n"
    assert artifacts(text) == [{"ref": "out/a.png", "purpose": "a screenshot"}, {"ref": "plain", "purpose": ""}]
