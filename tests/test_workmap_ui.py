"""The Work map tab's reads: registered workspaces' work maps, behind the UI session and the page
header, read-only. No model and no network."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time

import httpx
import pytest
import pytest_asyncio

from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceRegistry
from src.workmap import web
from src.workmap.store import WorkmapStore, _iso, map_key

TOKEN = "s" * 48
UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}
TASK = "release-2026:demo"
CONTRACT = "c" * 64
RESULT = ("task: release-2026:demo\nstatus: completed\n\n## summary\n<script>alert(1)</script> done\n\n"
          "## artifacts\n- artifact://m1/" + "d" * 64 + "/a1 — the report\n- docs/out.md — the page\n\n"
          "## evidence\n[^e1]: see artifact://m2/" + "e" * 64 + "/a2 — a log\n")


def build_map(root, *, clock=1_764_000_000.0, task=TASK):
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
            http.roots = {"project": project, "other": other, "empty": empty}
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
    assert first["updated"] == "2025-11-24T16:00:00Z"


@pytest.mark.asyncio
async def test_one_map_is_a_graph_of_its_nodes_and_edges(daemon):
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
    # What the page shows without repeating the store's rules: settled, and the plan beside the result.
    assert build["settled"] is True and build["needs"] == "V2" and build["ready"] is False
    assert build["summary"] == "Built it" and build["planned_summary"] == ""
    review = next(node for node in graph["nodes"] if node["id"] == "review")
    assert review["ready"] is True and review["settled"] is False and review["attempts"] == 0
    assert "current" not in graph  # every running card is marked, not one chosen node


@pytest.mark.asyncio
async def test_a_node_carries_the_time_of_its_status_and_counts_its_attempts(daemon):
    """A running card says how long it has run: the graph dates each node's current status, and a
    second preparation of a prepared node is a new attempt, not a repeated step."""
    from src.workmap.store import _iso
    http, ids, project, _ = daemon
    now = [1_764_100_000.0]
    store = WorkmapStore(project, clock=lambda: now[0])
    store.record_prepared(TASK, "review", contract_revision="f" * 64, attempt_id="1", complexity="high")
    now[0] += 600
    store.record_prepared(TASK, "review", contract_revision="e" * 64, attempt_id="2", complexity="high")
    await login(http)
    graph = (await http.get(f"/ui/api/workmap/map?workspace={ids['project']}&task_id={TASK}", headers=UI)).json()
    review = next(node for node in graph["nodes"] if node["id"] == "review")
    assert graph["running"] == ["review"] and review["status"] == "prepared" and review["needs"] == "V3"
    assert review["attempts"] == 2 and review["since"] == _iso(1_764_100_600.0)
    build = next(node for node in graph["nodes"] if node["id"] == "build")
    assert build["attempts"] == 1 and build["since"] == "2025-11-24T16:00:00Z"
    record = (await http.get(f"/ui/api/workmap/node?workspace={ids['project']}&task_id={TASK}&node=review",
                             headers=UI)).json()
    assert [(step["status"], step["op"], step["at"]) for step in record["history"]] == [
        ("planned", "add_node", "2025-11-24T16:00:00Z"), ("prepared", "prepared", _iso(1_764_100_000.0)),
        ("prepared", "prepared", _iso(1_764_100_600.0))]
    assert record["node"]["attempts"] == 2


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


def test_artifacts_follow_the_result_format_and_cost_linear_time():
    """An item is a line that starts with '- ' (an indented line continues it), its separator stands
    outside code spans, a footnote citation is not part of a reference, and a heading is found
    without a pattern that backtracked quadratically on a long run of spaces."""
    from src.workmap.web import artifacts
    m1, m2 = "artifact://m1/" + "d" * 64 + "/a1", "artifact://m2/" + "e" * 64 + "/art_x"
    text = (f"## artifacts\n- {m1}[^e1] — the report\n  - a sub-bullet of its purpose\n"
            "- `docs/a — b.md` — a dash inside the code span\n"
            f"## evidence\n[^e1]: {m2}. — a log, and artifact://not/a/reference\n")
    assert artifacts(text) == [{"ref": m1, "purpose": "the report - a sub-bullet of its purpose"},
                               {"ref": "docs/a — b.md", "purpose": "a dash inside the code span"},
                               {"ref": m2, "purpose": ""}]
    started = time.perf_counter()
    assert artifacts("# a" + " " * 150_000 + "b\n## artifacts\n- x — y\n" + "# " * 300_000) == [
        {"ref": "x", "purpose": "y"}]
    assert time.perf_counter() - started < 1  # was 4.5 s for 32,000 spaces, quadratic in the run


def test_the_artifact_scan_is_linear_in_every_crafted_shape():
    """A separator is found without a pattern, so a run of spaces before it costs one pass: the scan's
    own cap of 200,000 characters in one item took 188 s with the patterns, and a result that
    workmap_submit accepts (30 items of 3,940 spaces) 2.3 s. Continuation lines and citations are
    joined and cut once, not rebuilt per line or per citation."""
    from src.workmap.web import ARTIFACT_SCAN_CHARS, artifacts
    started = time.perf_counter()
    for separator, found in (("", {"ref": "x yz", "purpose": ""}), (" -- ", {"ref": "x y", "purpose": "z"}),
                             (" — ", {"ref": "x y", "purpose": "z"})):
        text = "## artifacts\n- x" + " " * (ARTIFACT_SCAN_CHARS - 40) + "y" + separator + "z\n"
        assert len(text) < ARTIFACT_SCAN_CHARS and artifacts(text) == [found]
    accepted = "## artifacts\n" + "".join(f"- out/f{index}.md" + " " * 3_940 + f"x -- purpose {index}\n" for index in range(30))
    assert artifacts(accepted) == [{"ref": f"out/f{index}.md x", "purpose": f"purpose {index}"} for index in range(30)]
    assert artifacts("## artifacts\n- a\n" + "  b\n" * 60_000) == [{"ref": "a" + " b" * 249 + "…", "purpose": ""}]
    assert artifacts("## artifacts\n- out/a.md" + "[^e1]" * 39_000 + " — cited\n") == [{"ref": "out/a.md", "purpose": "cited"}]
    assert time.perf_counter() - started < 1  # milliseconds each; a quadratic step takes seconds to minutes


@pytest.mark.asyncio
async def test_a_hostile_result_never_stalls_the_node_route(daemon):
    """The security review's planted result, with runs of 30,000 spaces in a heading-like line and in
    its artifacts items, read through the node route while the event loop keeps time: the route
    answers at once (it took 4.2 s, and stopped the loop as long, while a pattern backtracked over
    the run), and every reference stays plain text."""
    http, ids, project, _ = daemon
    store = WorkmapStore(project, clock=lambda: 1_764_100_000.0)
    store.apply(TASK, [{"op": "add_node", "id": "hostile", "title": "A planted result"}])
    run = " " * 30_000
    text = ("task: x\nstatus: completed\n\n## summary\n# a" + run + "b\n\n## artifacts\n"
            "- javascript:window.__xss=7 — <b onclick=window.__xss=71>purpose</b>\n"
            "- `file:///etc/passwd` — a local path\n"
            "- out/report.md" + run + "x -- a run before the double hyphen\n"
            "- out/log.md" + run + "x — a run before the em dash\n")
    store.record_prepared(TASK, "hostile", contract_revision="a" * 64, attempt_id="1", complexity=None)
    store.record_result(TASK, "hostile", contract_revision="a" * 64, attempt_id="1", text=text,
                        sha256=hashlib.sha256(text.encode()).hexdigest(), handback="line",
                        outcome={"status": "validated", "level": "V1", "summary": "planted", "checks": None,
                                 "findings": None, "limitations": 0, "result_status": "completed",
                                 "question": None, "escalation": None})
    await login(http)

    async def sleep_measured():
        started = time.perf_counter()
        await asyncio.sleep(0.05)
        return time.perf_counter() - started

    started = time.perf_counter()
    response, slept = await asyncio.gather(
        http.get(f"/ui/api/workmap/node?workspace={ids['project']}&task_id={TASK}&node=hostile", headers=UI),
        sleep_measured())
    assert response.status_code == 200
    assert slept < 1 and time.perf_counter() - started < 2  # the loop kept running while the record was read
    assert response.json()["artifacts"] == [
        {"ref": "javascript:window.__xss=7", "purpose": "<b onclick=window.__xss=71>purpose</b>"},
        {"ref": "file:///etc/passwd", "purpose": "a local path"},
        {"ref": "out/report.md x", "purpose": "a run before the double hyphen"},
        {"ref": "out/log.md x", "purpose": "a run before the em dash"}]


def _append(root, task, event):
    """Append one committed event to a map's log as a hand-written or planted line would be."""
    path = root / "data" / "workmap" / f"{map_key(task)}.jsonl"
    revision = WorkmapStore(root).load(task).revision + 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**event, "rev": revision, "commit": revision, "v": 1}) + "\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("at, by", [(float("nan"), "parent"), ("\ud800", "parent"), ({"a": 1}, "parent"),
                                    ("2025-11-24T16:00:00Z", "\ud800"), ("x" * 100_000, ["parent"])])
async def test_a_log_whose_times_or_authors_are_not_the_writers_is_still_listed_and_opened(daemon, at, by):
    http, ids, project, _ = daemon
    _append(project, TASK, {"op": "update_node", "id": "build", "title": "Built again", "at": at, "by": by})
    await login(http)
    ws = ids["project"]
    listed = await http.get("/ui/api/workmap/maps", headers=UI)
    assert listed.status_code == 200 and listed.json()["workspaces"][0]["maps"][0]["updated"] == "2025-11-24T16:00:00Z"
    graph = await http.get(f"/ui/api/workmap/map?workspace={ws}&task_id={TASK}", headers=UI)
    assert graph.status_code == 200 and graph.json()["revision"] == 11
    record = (await http.get(f"/ui/api/workmap/node?workspace={ws}&task_id={TASK}&node=build", headers=UI)).json()
    assert record["node"]["title"] == "Built again"
    assert all(step["at"] in (None, "2025-11-24T16:00:00Z") and step["by"] in (None, "parent", "server")
               for step in record["history"])


@pytest.mark.asyncio
async def test_a_time_in_the_future_never_makes_a_map_the_latest(daemon):
    """A planted log stamped 9999 must not stay the map #workmap and a task-only link open."""
    http, ids, _, _ = daemon
    planted = http.roots["empty"]
    build_map(planted, clock=253_402_300_799.0)  # 9999-12-31T23:59:59Z
    await login(http)
    listed = (await http.get("/ui/api/workmap/maps", headers=UI)).json()["workspaces"]
    assert [repo["workspace"] for repo in listed][:1] == [ids["project"]]
    assert [repo["maps"][0]["updated"] for repo in listed if repo["workspace"] == ids["empty"]] == [None]
    graph = (await http.get(f"/ui/api/workmap/map?task_id={TASK}", headers=UI)).json()
    assert graph["workspace"] == ids["project"] and graph["candidates"] == 2


@pytest.mark.asyncio
async def test_a_package_issued_again_is_no_new_attempt(daemon):
    """The server records every preparation; the same contract again is the same attempt and keeps
    the time the attempt started."""
    http, ids, project, _ = daemon
    now = [1_764_100_000.0]
    store = WorkmapStore(project, clock=lambda: now[0])
    store.record_prepared(TASK, "review", contract_revision="f" * 64, attempt_id="1", complexity=None)
    now[0] += 600
    store.record_prepared(TASK, "review", contract_revision="f" * 64, attempt_id="1", complexity=None)
    await login(http)
    record = (await http.get(f"/ui/api/workmap/node?workspace={ids['project']}&task_id={TASK}&node=review",
                             headers=UI)).json()
    assert record["node"]["attempts"] == 1 and record["node"]["since"] == _iso(1_764_100_000.0)
    assert [step["op"] for step in record["history"]] == ["add_node", "prepared"]


@pytest.mark.asyncio
async def test_the_map_names_the_last_commit_and_the_verdicts_the_store_recorded(daemon):
    """The page shows recorded verdicts, never ones it derives, and names every node of the last
    commit: a review's result and its verdict are one commit."""
    http, ids, project, sha = daemon
    store = WorkmapStore(project, clock=lambda: 1_764_100_000.0)
    review = ("task: x\nstatus: completed\n\n## summary\nclean\n")
    review_sha = hashlib.sha256(review.encode()).hexdigest()
    store.record_prepared(TASK, "review", contract_revision="9" * 64, attempt_id="1", complexity=None,
                          inputs={"build": sha})
    outcome = {"status": "validated", "level": "V2", "summary": "No issue", "checks": {"passed": 1, "failed": 0,
               "not_run": 0}, "findings": {}, "limitations": 0, "result_status": "completed", "question": None,
               "escalation": None}
    store.record_result(TASK, "review", contract_revision="9" * 64, attempt_id="1", text=review, sha256=review_sha,
                        handback="line", outcome=outcome)
    await login(http)
    url = f"/ui/api/workmap/map?workspace={ids['project']}&task_id={TASK}"
    graph = (await http.get(url, headers=UI)).json()
    assert graph["last_change"] == ["review", "build"]
    assert graph["verdicts"] == [{"verifier": "review", "target": "build", "verdict": "clean", "result_sha256": sha}]
    # A new attempt of the verifier has given no verdict yet: the old one no longer counts.
    store.apply(TASK, [{"op": "update_node", "id": "review", "status": "planned"}])
    store.record_prepared(TASK, "review", contract_revision="8" * 64, attempt_id="2", complexity=None,
                          inputs={"build": sha})
    graph = (await http.get(url, headers=UI)).json()
    assert graph["verdicts"] == [] and graph["last_change"] == ["review"]


@pytest.mark.asyncio
async def test_answers_name_no_path_and_never_repeat_an_unchecked_value(daemon):
    http, ids, _, _ = daemon
    other = http.roots["other"]
    await login(http)
    listed = await http.get("/ui/api/workmap/maps", headers=UI)
    assert "root" not in listed.text and '"key"' not in listed.text and str(other) not in listed.text
    junk = "<b>" + "x" * 300
    for path in (f"/ui/api/workmap/map?task_id={junk}",
                 f"/ui/api/workmap/node?workspace={ids['project']}&task_id={TASK}&node={junk}",
                 f"/ui/api/workmap/result?workspace={ids['project']}&task_id={TASK}&node={junk}"):
        response = await http.get(path, headers=UI)
        assert response.status_code == 400 and junk not in response.text and "invalid_request" in response.text
    # A store the reads refuse (its data directory is a link) is named, not its path.
    (other / "data").rename(other / "elsewhere")
    os.symlink(other / "elsewhere", other / "data")
    response = await http.get(f"/ui/api/workmap/map?workspace={ids['other']}&task_id=older-task", headers=UI)
    assert response.status_code == 500 and "unsafe_store" in response.text and str(other) not in response.text


@pytest.mark.asyncio
async def test_an_unchanged_map_is_folded_once_and_a_large_one_not_at_all(daemon, monkeypatch):
    http, ids, project, _ = daemon
    folds = []
    original = WorkmapStore.fold_file
    monkeypatch.setattr(WorkmapStore, "fold_file", lambda self, name: folds.append(name) or original(self, name))
    await login(http)
    url = f"/ui/api/workmap/map?workspace={ids['project']}&task_id={TASK}"
    for _ in range(3):
        assert (await http.get(url, headers=UI)).status_code == 200
    assert len(folds) == 1  # polls of an unchanged map cost a stat each
    WorkmapStore(project, clock=lambda: 1_764_100_000.0).apply(TASK, [{"op": "update_node", "id": "ask", "title": "Port?"}])
    assert (await http.get(url, headers=UI)).json()["revision"] == 11 and len(folds) == 2
    # A log over the page's limit is refused before it is read, and counted in the list.
    monkeypatch.setattr(web, "WEB_MAX_BYTES", 100)
    response = await http.get(url, headers=UI)
    assert response.status_code == 413 and "map_too_large" in response.text and len(folds) == 2
    project_list = (await http.get("/ui/api/workmap/maps", headers=UI)).json()["workspaces"]
    assert [(repo["too_large"], repo["maps"]) for repo in project_list if repo["workspace"] == ids["project"]] == [(1, [])]


@pytest.mark.asyncio
async def test_a_map_file_the_store_refuses_is_counted_and_the_others_still_listed(daemon):
    http, ids, project, _ = daemon
    os.symlink(project / "data" / "workmap" / f"{map_key(TASK)}.jsonl",
               project / "data" / "workmap" / ("0" * 16 + ".jsonl"))
    await login(http)
    repo = next(repo for repo in (await http.get("/ui/api/workmap/maps", headers=UI)).json()["workspaces"]
                if repo["workspace"] == ids["project"])
    assert [item["task_id"] for item in repo["maps"]] == [TASK] and repo["skipped"] == 1 and repo["total"] == 2
