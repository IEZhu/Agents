"""Usage statistics for the settings page (#187): answer counts from history files, the AI apps
that use the daemon, and the protected ``GET /ui/api/stats``. No model and no network."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio

import src.server as server
from src.daemon.app import create_app
from src.daemon.usage import CONNECTED_SECONDS, MAX_APPS, MAX_OBSERVED, AppActivity, app_name, observing
from src.daemon.workspaces import ClientContext, WorkspaceRegistry
from src.memory.history import HistoryWriter
from src.memory.history_stats import FileSummaries, summarize

NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)
DAY = dt.timedelta(days=1)
QUERY = "query text that must stay in the history file"
ANSWER = "answer text that must stay in the history file"
TOKEN = "s" * 48
UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}


def block(number: int, when: dt.datetime, action: str, meta: dict | None = None) -> str:
    return HistoryWriter._render_entry(f"{number:012x}", when.isoformat(timespec="seconds"), QUERY, action,
                                       ANSWER, None, None, meta)


def persona(agent: str, action: str = "keep") -> str:
    return f"Persona (client-reported): {agent}; activation=a-1; revision={'b' * 64}; action={action}"


def write(path, *blocks: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nrepo: x\n---\n" + "".join(blocks), encoding="utf-8")


# --- Answer counts --------------------------------------------------------------------------------

def test_counts_per_day_agent_repository_action_and_app(tmp_path):
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    write(alpha / "history.md",
          block(1, NOW - dt.timedelta(hours=1), "Agent: lawyer\n" + persona("lawyer"),
                {"answer_timestamp": "2026.10.06 14:00:00", "client": "codex"}),
          # A curated action counts under the agent of its persona line.
          block(2, NOW - DAY, "Reviewed the contract\n" + persona("lawyer", "switch")),
          block(3, NOW - 3 * DAY, "Agent: software_engineer"),
          block(4, NOW - 10 * DAY, "Agent: x\nPersona (unverified): ux_designer; missing=scope; action=refresh"))
    write(alpha / "history" / "2026-09.md",
          block(5, NOW - 20 * DAY, "Agent: lawyer\n" + persona("lawyer", "restore")),
          block(6, NOW - 40 * DAY, "Agent: lawyer\n" + persona("lawyer")))
    write(beta / "history.md", block(7, NOW - 2 * DAY, "Free text\n" + persona("software_engineer", "nonsense")))

    result = summarize([("w-a", "alpha", alpha), ("w-b", "beta", beta)], now=NOW, files=FileSummaries())

    assert (result["today"], result["last_7_days"], result["last_30_days"]) == (1, 4, 6)
    assert len(result["per_day"]) == 30 and result["per_day"][0]["date"] == "2026-09-07"
    assert result["per_day"][-1] == {"date": "2026-10-06", "answers": 1}
    assert result["per_agent"] == [{"agent": "lawyer", "answers": 3}, {"agent": "software_engineer", "answers": 2},
                                   {"agent": "ux_designer", "answers": 1}]
    assert result["per_repository"] == [{"workspace": "w-a", "name": "alpha", "answers": 5},
                                        {"workspace": "w-b", "name": "beta", "answers": 1}]
    assert result["per_action"] == {"keep": 1, "switch": 1, "refresh": 1, "restore": 1, "other": 1, "none": 1}
    assert result["per_app"] == [{"app": "unknown", "answers": 5}, {"app": "codex", "answers": 1}]
    assert [event["id"] for event in result["events"]] == [f"{n:012x}" for n in (1, 2, 7, 3, 4, 5, 6)]
    assert result["skipped"] == []
    text = json.dumps(result)
    assert QUERY not in text and ANSWER not in text and "Reviewed the contract" not in text


def test_the_same_content_logged_again_later_counts_again_and_a_rotation_copy_once(tmp_path):
    root = tmp_path / "repo"
    again = block(1, NOW - DAY, "Agent: lawyer")  # the writer's tail no longer held the first one
    write(root / "history" / "2026-10.md", block(1, NOW - 3 * DAY, "Agent: lawyer"), again)
    write(root / "history.md", again, block(2, NOW, "Agent: lawyer"))  # moved by a rotation in progress
    assert summarize([("w", "repo", root)], now=NOW, files=FileSummaries())["last_7_days"] == 3


def test_days_are_counted_in_the_local_zone(tmp_path):
    root = tmp_path / "repo"
    write(root / "history.md", block(1, dt.datetime(2026, 10, 5, 21, 30, tzinfo=dt.timezone.utc), "Agent: lawyer"))
    now = dt.datetime(2026, 10, 6, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    result = summarize([("w", "repo", root)], now=now, files=FileSummaries())
    assert result["today"] == 1 and result["per_day"][-1] == {"date": "2026-10-06", "answers": 1}


def test_a_missing_workspace_is_skipped_and_the_others_still_count(tmp_path):
    alive = tmp_path / "alive"
    write(alive / "history.md", block(1, NOW, "Agent: lawyer"))
    result = summarize([("w-gone", "gone", tmp_path / "gone"), ("w-alive", "alive", alive)],
                       now=NOW, files=FileSummaries())
    assert result["skipped"] == [{"workspace": "w-gone", "name": "gone", "reason": "missing"}]
    assert result["today"] == 1


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions and a non-root user")
def test_an_unreadable_history_skips_its_workspace(tmp_path):
    locked, alive = tmp_path / "locked", tmp_path / "alive"
    write(locked / "history.md", block(1, NOW, "Agent: lawyer"))
    write(alive / "history.md", block(2, NOW, "Agent: lawyer"))
    (locked / "history.md").chmod(0)
    try:
        result = summarize([("w-l", "locked", locked), ("w-a", "alive", alive)], now=NOW, files=FileSummaries())
    finally:
        (locked / "history.md").chmod(0o600)
    assert result["skipped"] == [{"workspace": "w-l", "name": "locked", "reason": "unreadable"}]
    assert result["today"] == 1


def test_unchanged_files_come_from_the_cache_and_older_archives_are_not_read(tmp_path):
    root = tmp_path / "repo"
    write(root / "history.md", block(1, NOW, "Agent: lawyer"))
    write(root / "history" / "2026-09.md", block(2, NOW - 5 * DAY, "Agent: lawyer"))
    write(root / "history" / "2026-05.md", block(3, NOW - 140 * DAY, "Agent: lawyer"))
    files, workspaces = FileSummaries(), [("w", "repo", root)]
    first = summarize(workspaces, now=NOW, files=files)
    assert files.reads == 2  # history.md and September; May ended before the window
    assert summarize(workspaces, now=NOW, files=files) == first and files.reads == 2
    with (root / "history.md").open("a", encoding="utf-8") as stream:
        stream.write(block(4, NOW, "Agent: lawyer"))
    assert summarize(workspaces, now=NOW, files=files)["today"] == 2 and files.reads == 3


def test_events_are_the_fifty_newest_answers_without_their_text(tmp_path):
    root = tmp_path / "repo"
    write(root / "history.md", *(block(n, NOW - dt.timedelta(minutes=n), "Agent: lawyer\n" + persona("lawyer"))
                                 for n in range(1, 61)))
    events = summarize([("w", "repo", root)], now=NOW, files=FileSummaries())["events"]
    assert len(events) == 50 and events[-1]["id"] == f"{50:012x}"
    assert events[0] == {"id": f"{1:012x}", "time": (NOW - dt.timedelta(minutes=1)).isoformat(timespec="seconds"),
                         "agent": "lawyer", "action": "keep", "workspace": "w", "repository": "repo", "app": None}


# --- Apps -------------------------------------------------------------------------------------------

def initialize(name: str = "codex-mcp-client", version: str = "0.50.0") -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": name, "version": version}}}).encode()


def test_an_initialize_makes_its_app_connected_until_the_window_passes():
    now = [1_800_000_000.0]
    apps = AppActivity(clock=lambda: now[0])
    apps.request("codex")
    apps.observe(initialize(), "codex", "w-1")
    (codex,) = apps.snapshot()
    assert codex["connected"] and codex["requests_today"] == 1 and codex["workspace"] == "w-1"
    assert (codex["client"], codex["version"]) == ("codex-mcp-client", "0.50.0")
    now[0] += CONNECTED_SECONDS + 1
    assert not apps.snapshot()[0]["connected"]
    apps.stream_opened("codex")  # an open notification stream is a connection, however old
    now[0] += 10 * CONNECTED_SECONDS
    assert apps.snapshot()[0]["connected"] and apps.snapshot()[0]["streams"] == 1
    apps.stream_closed("codex")  # the app was there until now, but a closed stream is not a request
    (codex,) = apps.snapshot()
    assert not codex["connected"] and codex["streams"] == 0
    assert codex["last_seen"] == dt.datetime.fromtimestamp(now[0], dt.timezone.utc).isoformat(timespec="seconds")


def test_requests_without_a_valid_app_count_as_unknown_and_apps_are_bounded():
    apps = AppActivity()
    for value in (None, "", "Cursor IDE", "x" * 40, "../etc"):
        apps.request(app_name(value))
    assert [(app["app"], app["requests_today"]) for app in apps.snapshot()] == [("unknown", 5)]
    assert app_name(" Cursor ") == "cursor"
    for number in range(MAX_APPS + 3):
        apps.request(f"app-{number}")
    names = [app["app"] for app in apps.snapshot()]
    assert len(names) == MAX_APPS and "unknown" in names and "app-17" not in names


@pytest.mark.asyncio
async def test_observing_passes_every_message_and_reads_only_a_whole_small_body():
    body = initialize("claude-code", "2.0.0")
    messages = [{"type": "http.request", "body": body[:10], "more_body": True},
                {"type": "http.request", "body": body[10:], "more_body": False}]

    async def receive():
        return messages.pop(0)

    apps = AppActivity()
    observed = observing(receive, apps, "claude-code", None)
    assert (await observed())["body"] == body[:10] and apps.snapshot() == []
    assert (await observed())["body"] == body[10:]
    assert apps.snapshot()[0]["client"] == "claude-code"

    huge = b'{"method": "initialize", "params": {"clientInfo": {"name": "big"}}, "pad": "' + b"x" * MAX_OBSERVED + b'"}'
    large = [{"type": "http.request", "body": huge, "more_body": False}]

    async def receive_large():
        return large.pop(0)

    other = AppActivity()
    assert (await observing(receive_large, other, "codex", None)())["body"] == huge
    assert other.snapshot() == []


# --- log_interaction ----------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("app", ["codex", None])
async def test_log_interaction_names_a_known_app_in_meta(tmp_path, monkeypatch, app):
    writer = Mock()
    writer.return_value.append_entry.return_value = {"status": "recorded"}
    monkeypatch.setattr(server, "HistoryWriter", writer)
    monkeypatch.setattr(server, "_drain_abandoned", False)
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)
    monkeypatch.setattr(server, "resolve_client_context",
                        AsyncMock(return_value=ClientContext("r", "http", "w", tmp_path, client=app)))
    await server.mcp.call_tool("log_interaction", {"agent_name": "lawyer", "query": "q", "response_content": "r"})
    assert server.drain_pending_logs(5)
    metadata = writer.return_value.append_entry.call_args.args[5]
    assert "answer_timestamp" in metadata and metadata.get("client") == app and ("client" in metadata) == bool(app)


# --- The daemon ---------------------------------------------------------------------------------------

@pytest_asyncio.fixture
async def daemon(tmp_path):
    repo = tmp_path / "project"
    write(repo / "history.md", block(1, dt.datetime.now(dt.timezone.utc), "Agent: lawyer\n" + persona("lawyer"),
                                     {"answer_timestamp": "x", "client": "cursor"}))
    registry = WorkspaceRegistry(tmp_path / "service")
    workspace = registry.register(repo)

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
            yield http, workspace


async def login(http) -> None:
    code = (await http.post("/admin/ui/code", headers={"Authorization": "Bearer " + TOKEN})).json()["code"]
    assert (await http.post("/ui/api/session", json={"code": code}, headers=UI)).status_code == 200


@pytest.mark.asyncio
async def test_stats_need_a_session_and_the_page_header_and_carry_no_text(daemon):
    http, workspace = daemon
    service = http._transport.app.state.service
    assert (await http.get("/ui/api/stats", headers=UI)).status_code == 401
    await login(http)
    for headers in ({}, {"Origin": "http://127.0.0.1:8765"},
                    {"X-Agents-UI": "1", "Origin": "https://attacker.example"},
                    {**UI, "Host": "attacker.example"}):
        assert (await http.get("/ui/api/stats", headers=headers)).status_code == 403, headers
    service.last_activity = idle = time.monotonic() - 100
    response = await http.get("/ui/api/stats", headers=UI)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert service.last_activity == idle  # a poll is not activity: it never holds back an update
    stats = response.json()
    assert stats["answers"]["today"] == 1 and stats["skipped"] == []
    assert stats["answers"]["per_repository"] == [{"workspace": workspace, "name": "project", "answers": 1}]
    assert stats["answers"]["per_app"] == [{"app": "cursor", "answers": 1}]
    assert stats["events"][0]["repository"] == "project" and stats["uptime_seconds"] >= 0
    assert QUERY not in response.text and ANSWER not in response.text and TOKEN not in response.text


@pytest.mark.asyncio
async def test_an_initialize_over_http_shows_its_app_connected(daemon):
    http, workspace = daemon
    service = http._transport.app.state.service
    now = [time.time()]
    service.usage.apps.clock = lambda: now[0]
    response = await http.post("/mcp", content=initialize(), headers={
        "Authorization": "Bearer " + TOKEN, "X-Agents-Client": "codex", "X-Agents-Workspace": workspace,
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    assert response.status_code == 200 and response.json()["result"]["serverInfo"]
    await login(http)

    async def codex():
        apps = (await http.get("/ui/api/stats", headers=UI)).json()["apps"]
        return next(app for app in apps if app["app"] == "codex")

    app = await codex()
    assert app["connected"] and app["requests_today"] == 1 and app["workspace"] == "project"
    assert (app["client"], app["version"]) == ("codex-mcp-client", "0.50.0")
    now[0] += CONNECTED_SECONDS + 1
    assert not (await codex())["connected"]
