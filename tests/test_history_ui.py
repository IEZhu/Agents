"""The History tab's reads (#189): registered workspaces' history.md and archives, behind the UI
session and the page header. No model and no network."""
from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import threading
import time

import httpx
import pytest
import pytest_asyncio

from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceRegistry
from src.memory.history import EntryChanged, HistoryReader, HistoryWriter, delete_entry

TOKEN = "s" * 48
UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def block(number: int, when: dt.datetime, intent: str, action: str = "Agent: lawyer", outcome: str = "an answer",
          tags=None) -> str:
    return HistoryWriter._render_entry(f"{number:012x}", when.isoformat(timespec="seconds"), intent, action,
                                       outcome, None, tags, {"answer_timestamp": "x"})


def persona(agent: str, action: str = "keep") -> str:
    return f"Agent: {agent}\nPersona (client-reported): {agent}; activation=a-1; revision={'b' * 64}; action={action}"


def write(path, *blocks: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nrepo: x\n---\n" + "".join(blocks), encoding="utf-8")


@pytest_asyncio.fixture
async def daemon(tmp_path):
    project, other, gone, quiet, archived = (tmp_path / name for name in ("project", "other", "gone", "quiet", "archived"))
    write(project / "history.md",
          block(1, NOW - dt.timedelta(hours=3), "first today", persona("lawyer", "switch")),
          block(2, NOW - dt.timedelta(minutes=2), "newest question", persona("ux_designer")),
          block(3, NOW - dt.timedelta(hours=1), "middle", "Agent: software_engineer", tags=["#t"]))
    write(project / "history" / "2026-09.md",
          block(4, dt.datetime(2026, 9, 30, 10, tzinfo=dt.timezone.utc), "september one"),
          block(5, dt.datetime(2026, 9, 30, 11, tzinfo=dt.timezone.utc), "september two holds a NEEDLE"))
    write(other / "history.md", block(6, NOW - dt.timedelta(days=2), "the needle in another repo", outcome="needle x"))
    # Only archives, one of them with a name that is no month: it is left out, not fatal.
    write(archived / "history" / "2026-08.md", block(7, dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc), "august"))
    write(archived / "history" / "2026-13.md", block(8, dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc), "no month"))
    gone.mkdir()
    quiet.mkdir()
    registry = WorkspaceRegistry(tmp_path / "service")
    ids = {name: registry.register(path) for name, path in
           (("project", project), ("other", other), ("gone", gone), ("quiet", quiet), ("archived", archived))}
    shutil.rmtree(gone)

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
            yield http, ids


async def login(http) -> None:
    code = (await http.post("/admin/ui/code", headers={"Authorization": "Bearer " + TOKEN})).json()["code"]
    assert (await http.post("/ui/api/session", json={"code": code}, headers=UI)).status_code == 200


@pytest.mark.asyncio
async def test_history_reads_need_a_session_and_the_page_header_and_count_as_activity(daemon):
    http, ids = daemon
    service = http._transport.app.state.service
    paths = ["/ui/api/history/repos", f"/ui/api/history?workspace={ids['project']}",
             f"/ui/api/history/source?workspace={ids['project']}", "/ui/api/history/search?q=needle"]
    for path in paths:
        assert (await http.get(path, headers=UI)).status_code == 401
    await login(http)
    for path in paths:
        for headers in ({}, {"Origin": "http://127.0.0.1:8765"},
                        {"X-Agents-UI": "1", "Origin": "https://attacker.example"},
                        {**UI, "Host": "attacker.example"}):
            assert (await http.get(path, headers=headers)).status_code == 403, (path, headers)
    service.last_activity = idle = time.monotonic() - 100
    response = await http.get(paths[0], headers=UI)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert service.last_activity > idle  # reading a history is the user's activity, unlike the stats poll
    assert (await http.post(paths[0], headers=UI)).status_code == 405


@pytest.mark.asyncio
async def test_the_list_shows_repositories_with_a_history_newest_first(daemon):
    http, ids = daemon
    await login(http)
    repos = (await http.get("/ui/api/history/repos", headers=UI)).json()["repos"]
    assert [repo["name"] for repo in repos] == ["project", "other", "archived", "gone"]  # "quiet" has no history
    project, other, archived, gone = repos
    assert [(f["file"], f["entries"]) for f in archived["files"]] == [("2026-08", 1)]
    assert project["workspace"] == ids["project"] and project["available"] and project["entries"] == 5
    assert [(f["file"], f["entries"]) for f in project["files"]] == [("current", 3), ("2026-09", 2)]
    assert project["newest"] == (NOW - dt.timedelta(minutes=2)).isoformat(timespec="seconds")
    assert other["entries"] == 1 and gone == {**gone, "available": False, "files": [], "newest": None}


@pytest.mark.asyncio
async def test_a_file_reads_newest_first_in_portions_with_a_full_index(daemon, tmp_path):
    http, ids = daemon
    await login(http)
    base = f"/ui/api/history?workspace={ids['project']}"
    first = (await http.get(base + "&limit=2", headers=UI)).json()
    assert (first["file"], first["files"], first["total"]) == ("current", ["current", "2026-09"], 3)
    assert [entry["intent"] for entry in first["entries"]] == ["newest question", "middle"]
    newest = first["entries"][0]
    assert (newest["agent"], newest["persona_action"], newest["id"]) == ("ux_designer", "keep", f"{2:012x}")
    assert first["entries"][1]["persona_action"] is None and first["entries"][1]["tags"] == ["#t"]
    assert [(item["id"], item["agent"]) for item in first["index"]] == [
        (f"{2:012x}", "ux_designer"), (f"{3:012x}", "software_engineer"), (f"{1:012x}", "lawyer")]
    # The next portion counts from the oldest entry: an answer logged meanwhile shifts nothing.
    (tmp_path / "project" / "history.md").write_text(
        (tmp_path / "project" / "history.md").read_text(encoding="utf-8") + block(9, NOW, "logged meanwhile"),
        encoding="utf-8")
    rest = (await http.get(base + "&before=1&limit=2", headers=UI)).json()
    assert [entry["intent"] for entry in rest["entries"]] == ["first today"] and "index" not in rest
    assert rest["total"] == 4
    archive = (await http.get(base + "&file=2026-09", headers=UI)).json()
    assert [entry["intent"] for entry in archive["entries"]] == ["september two holds a NEEDLE", "september one"]


@pytest.mark.asyncio
async def test_an_entry_opens_the_file_that_holds_it_and_is_reached(daemon):
    http, ids = daemon
    await login(http)
    base = f"/ui/api/history?workspace={ids['project']}"
    found = (await http.get(base + f"&entry={4:012x}&limit=1", headers=UI)).json()
    assert (found["file"], found["focus"]) == ("2026-09", 1)
    assert [entry["id"] for entry in found["entries"]] == [f"{5:012x}", f"{4:012x}"]  # the portion reaches it
    missing = await http.get(base + "&entry=0123456789ab", headers=UI)
    assert missing.status_code == 404 and missing.json()["error"].startswith("entry_not_found")
    wrong_time = await http.get(base + f"&entry={4:012x}&time=2026-01-01T00:00:00%2B00:00", headers=UI)
    assert wrong_time.status_code == 404  # ids repeat: the time picks the occurrence
    timed = (await http.get(base + f"&entry={4:012x}&time=2026-09-30T10:00:00%2B00:00", headers=UI)).json()
    assert timed["focus"] == 1
    # Without a file, a workspace that has only archives opens its newest one.
    archived = (await http.get(f"/ui/api/history?workspace={ids['archived']}", headers=UI)).json()
    assert (archived["file"], archived["files"], archived["total"]) == ("2026-08", ["2026-08"], 1)


@pytest.mark.asyncio
async def test_only_registered_workspaces_and_history_files_are_read(daemon, tmp_path):
    http, ids = daemon
    await login(http)
    for query, status, code in [
        ("workspace=not-registered", 400, "workspace_invalid"),
        (f"workspace={ids['gone']}", 400, "workspace_invalid"),
        (f"workspace={ids['project']}&file=../../etc/passwd", 400, "file_invalid"),
        (f"workspace={ids['project']}&file=2026-13", 400, "file_invalid"),
        (f"workspace={ids['project']}&file=history", 400, "file_invalid"),
        (f"workspace={ids['project']}&file=2026-08", 404, "file_not_found"),
        (f"workspace={ids['project']}&before=-1", 400, "invalid_request"),
        (f"workspace={ids['project']}&limit=x", 400, "invalid_request"),
    ]:
        for path in ("/ui/api/history?", "/ui/api/history/source?"):
            if path.endswith("source?") and ("before" in query or "limit" in query):
                continue
            response = await http.get(path + query, headers=UI)
            assert response.status_code == status and response.json()["error"].startswith(code), (path, query)


@pytest.mark.asyncio
async def test_the_source_is_the_file_as_it_is(daemon, tmp_path):
    http, ids = daemon
    await login(http)
    source = (await http.get(f"/ui/api/history/source?workspace={ids['project']}&file=2026-09", headers=UI)).json()
    assert source["text"] == (tmp_path / "project" / "history" / "2026-09.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_the_search_counts_entries_that_hold_every_term_per_repository(daemon):
    http, ids = daemon
    await login(http)
    found = (await http.get("/ui/api/history/search?q=needle", headers=UI)).json()
    assert found["repos"] == [{"workspace": ids["project"], "matches": 1}, {"workspace": ids["other"], "matches": 1}]
    both = (await http.get("/ui/api/history/search?q=NEEDLE%20another", headers=UI)).json()
    assert both["repos"] == [{"workspace": ids["other"], "matches": 1}]
    assert (await http.get("/ui/api/history/search?q=%20", headers=UI)).json()["repos"] == []
    # The repository's name counts as part of each entry, as on the other tabs.
    named = (await http.get("/ui/api/history/search?q=other%20needle", headers=UI)).json()
    assert named["repos"] == [{"workspace": ids["other"], "matches": 1}]


@pytest.mark.asyncio
async def test_an_unreadable_registry_is_reported(daemon):
    http, _ = daemon
    await login(http)
    service = http._transport.app.state.service
    service.registry.path.write_text("{not json", encoding="utf-8")
    response = await http.get("/ui/api/history/repos", headers=UI)
    assert response.status_code == 500 and response.json()["error"].startswith("registry_unreadable")


# --- Delete entry ---------------------------------------------------------------------------------

def entries_of(path) -> list[str]:
    return [entry.intent for entry in HistoryReader._parse(path.read_text(encoding="utf-8"))]


def test_delete_entry_removes_exactly_that_entry(tmp_path):
    history = tmp_path / "history.md"
    first = NOW - dt.timedelta(hours=2)
    write(history, block(1, first, "one"), block(2, first, "two"), block(3, NOW, "three"))
    marker = "\n<!-- merged on rotation -->\n"
    archive = tmp_path / "history" / "2026-09.md"
    write(archive, block(4, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc), "september"), marker,
          block(5, dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc), "later"))
    delete_entry(str(history), None, f"{2:012x}", first.isoformat(timespec="seconds"))
    assert entries_of(history) == ["one", "three"]
    delete_entry(str(history), "2026-09", f"{4:012x}", "2026-09-01T00:00:00+00:00")
    assert entries_of(archive) == ["later"] and marker in archive.read_text(encoding="utf-8")
    with pytest.raises(EntryChanged):  # a stale page: the entry is gone already
        delete_entry(str(history), None, f"{2:012x}", first.isoformat(timespec="seconds"))
    with pytest.raises(EntryChanged):  # the same id at another time is another entry
        delete_entry(str(history), None, f"{1:012x}", NOW.isoformat(timespec="seconds"))
    (tmp_path / "history.md.rotating").write_text("", encoding="utf-8")
    with pytest.raises(EntryChanged, match="rotation"):
        delete_entry(str(history), None, f"{1:012x}", first.isoformat(timespec="seconds"))


def test_appends_that_run_meanwhile_survive_a_deletion(tmp_path):
    history = tmp_path / "history.md"
    writer = HistoryWriter(str(history), str(tmp_path / "history"))
    target = writer.append_entry("to delete", "Agent: lawyer", "x")
    timestamp = HistoryReader(str(history)).read_all()[0].timestamp
    appended = []

    def append():
        for number in range(40):
            appended.append(writer.append_entry(f"meanwhile {number}", "Agent: lawyer", "y")["entry_id"])

    thread = threading.Thread(target=append)
    thread.start()
    delete_entry(str(history), None, target["entry_id"], timestamp)
    thread.join()
    intents = entries_of(history)
    assert "to delete" not in intents and intents == [f"meanwhile {number}" for number in range(40)]


@pytest.mark.asyncio
async def test_the_page_deletes_an_entry_with_its_header_and_a_stale_one_is_refused(daemon, tmp_path):
    http, ids = daemon
    await login(http)
    body = {"workspace": ids["project"], "file": "current", "entry": f"{3:012x}",
            "time": (NOW - dt.timedelta(hours=1)).isoformat(timespec="seconds")}
    assert (await http.post("/ui/api/history/delete", json=body)).status_code == 403  # no page header
    assert (await http.get("/ui/api/history/delete", headers=UI)).status_code == 405
    done = await http.post("/ui/api/history/delete", json=body, headers=UI)
    assert done.status_code == 200 and done.json()["status"] == "deleted"
    assert "middle" not in entries_of(tmp_path / "project" / "history.md")
    stale = await http.post("/ui/api/history/delete", json=body, headers=UI)
    assert stale.status_code == 409 and stale.json()["error"].startswith("entry_changed")
    for wrong, code in (({**body, "file": "../x"}, "file_invalid"), ({**body, "entry": "x"}, "invalid_request"),
                        ({**body, "workspace": "not-registered"}, "workspace_invalid")):
        response = await http.post("/ui/api/history/delete", json=wrong, headers=UI)
        assert response.status_code == 400 and response.json()["error"].startswith(code), wrong
