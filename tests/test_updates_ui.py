"""The overview's Updates section (#381): ``/ui/api/updates`` shows the update channel and saves
main or dev through ``update_channel.set_branch``, with the protections of ``/ui/api/autodev``.
Temporary git repositories only; no service is started and nothing is updated."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import pytest_asyncio

from src import update_channel
from src.daemon import overview
from src.daemon.app import create_app
from src.daemon.state import write_json
from src.file_lock import file_lock
from tests.test_daemon_update import git, installation  # noqa: F401  (fixture)
from tests.test_update_channel import push_dev

TOKEN = "u" * 48
UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}
PATH = "/ui/api/updates"


def runtime(port):
    from mcp.server.fastmcp import FastMCP
    return FastMCP("test", stateless_http=True, json_response=True), None


async def login(http) -> None:
    code = (await http.post("/admin/ui/code", headers={"Authorization": "Bearer " + TOKEN})).json()["code"]
    assert (await http.post("/ui/api/session", json={"code": code}, headers=UI)).status_code == 200


@pytest_asyncio.fixture
async def daemon(installation, tmp_path):  # noqa: F811
    controller, root, old, target = installation
    state = tmp_path / "service"
    app = create_app(state, TOKEN, runtime_loader=runtime)
    service = app.state.service
    service.flows_ui.updates.root = service.overview.root = root
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if service.state != "starting":
                break
            await asyncio.sleep(.01)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            yield http, root, state, old


async def read(http):
    response = await http.get(PATH, headers=UI)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def put(http, **body):
    return await http.put(PATH, json=body, headers=UI)


@pytest.mark.asyncio
async def test_the_route_needs_a_session_the_page_header_and_the_same_origin(daemon):
    http, root, state, old = daemon
    assert (await http.get(PATH, headers=UI)).status_code == 401
    assert (await put(http, expected_revision="", branch="dev")).status_code == 401
    await login(http)
    for headers in ({}, {"Origin": "http://127.0.0.1:8765"}, {"X-Agents-UI": "1", "Origin": "https://attacker.example"},
                    {**UI, "Host": "attacker.example"}):
        assert (await http.get(PATH, headers=headers)).status_code == 403, headers
    revision = (await read(http))["updates"]["revision"]
    for headers in ({"X-Agents-UI": "1"}, {"Origin": "http://127.0.0.1:8765"},
                    {"X-Agents-UI": "1", "Origin": "http://localhost:9999"},
                    {"X-Agents-UI": "1", "Origin": "https://attacker.example"}):
        response = await http.put(PATH, json={"expected_revision": revision, "branch": "dev"}, headers=headers)
        assert response.status_code == 403, headers
    assert (await http.post(PATH, json={}, headers=UI)).status_code == 405
    assert not (root / ".env").exists()


@pytest.mark.asyncio
async def test_the_page_reads_the_channel_without_writing_or_a_secret(daemon):
    http, root, state, old = daemon
    git(root, "remote", "set-url", "origin", "https://owner:secret-token@example.invalid/agents.git")
    write_json(state / "service.json", {"auto_update": {"enabled": True, "interval": 900}})
    write_json(state / "auto-update.json", {"time": "2026-10-14T10:00:00+00:00", "state": "skipped",
                                            "reason": "fetch failed: https://owner:secret-token@example.invalid/x",
                                            "unrelated": "dropped"})
    await login(http)
    body = await read(http)
    updates = body["updates"]
    assert body["version"]
    assert (updates["configured_branch"], updates["source"], updates["checked_out_branch"]) == ("main", "default", "main")
    assert updates["action"] == "follow" and updates["auto_update"] is True and updates["choices"] == ["main", "dev"]
    assert updates["update_command"].endswith("-m src.daemon update")
    assert "next scheduled run (every 15 min" in updates["when"]
    assert updates["last_run"] == {"time": "2026-10-14T10:00:00+00:00", "state": "skipped",
                                   "reason": "fetch failed: https://example.invalid/x"}
    assert "secret-token" not in json.dumps(body) and not (root / ".env").exists()


@pytest.mark.asyncio
async def test_saving_dev_writes_the_setting_and_names_when_it_applies(daemon, monkeypatch):
    http, root, state, old = daemon
    monkeypatch.setenv("AGENTS_AUTO_UPDATE_BRANCH", "main")  # the service's environment does not count
    push_dev(root)
    await login(http)
    revision = (await read(http))["updates"]["revision"]
    response = await put(http, expected_revision=revision, branch="dev")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"]["branch"] == "dev" and body["saved"]["previous_setting"] == "main"
    assert not body["saved"]["downgrade"] and "warnings" not in body["saved"]
    updates = body["updates"]
    assert (updates["configured_branch"], updates["source"], updates["action"]) == ("dev", ".env", "switch")
    assert updates["revision"] != revision
    assert updates["when"].startswith("The switch from main to dev happens when you run `")
    assert "scheduled updates are off" in updates["when"]
    assert update_channel.file_values(root)[update_channel.BRANCH_KEY] == "dev"
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main"  # saving changes no code


@pytest.mark.asyncio
async def test_a_stale_revision_is_refused_and_leaves_the_file(daemon):
    http, root, state, old = daemon
    push_dev(root)
    await login(http)
    revision = (await read(http))["updates"]["revision"]
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=main  # set elsewhere\n")
    response = await put(http, expected_revision=revision, branch="dev")
    assert response.status_code == 409 and response.json()["error"] == "conflict"
    assert (root / ".env").read_text() == "AGENTS_AUTO_UPDATE_BRANCH=main  # set elsewhere\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("body, code", [
    ({"branch": "-x"}, "invalid_branch"), ({"branch": "a b"}, "invalid_branch"),
    ({"branch": "feature/x"}, "invalid_branch"), ({"branch": None}, "invalid_branch"),
    ({"branch": "dev"}, "unknown_branch"),  # the remote has no dev
    ({"branch": "dev", "allow_downgrade": "yes"}, "invalid_request"),
    ({"branch": "dev", "extra": 1}, "invalid_request"),
])
async def test_an_unsafe_offered_or_missing_branch_is_refused(daemon, body, code):
    http, root, state, old = daemon
    await login(http)
    revision = (await read(http))["updates"]["revision"]
    response = await put(http, expected_revision=revision, **body)
    assert response.status_code == 400 and response.json()["error"] == code, response.text
    assert not (root / ".env").exists()


@pytest.mark.asyncio
async def test_a_body_without_its_revision_or_not_an_object_is_refused(daemon):
    http, root, state, old = daemon
    await login(http)
    assert (await put(http, branch="dev")).json()["error"] == "invalid_request"
    response = await http.put(PATH, content=b"[1]", headers={**UI, "Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_a_downgrade_needs_the_confirmation(daemon):
    http, root, state, old = daemon
    tip = push_dev(root)
    git(root, "checkout", "--quiet", "-B", "dev", tip)  # the installation runs dev, ahead of main
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=dev\n")
    await login(http)
    revision = (await read(http))["updates"]["revision"]
    refused = await put(http, expected_revision=revision, branch="main")
    assert refused.status_code == 409 and refused.json()["error"] == "downgrade"
    assert "older code" in refused.json()["message"]
    assert (root / ".env").read_text() == "AGENTS_AUTO_UPDATE_BRANCH=dev\n"
    confirmed = await put(http, expected_revision=revision, branch="main", allow_downgrade=True)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["saved"]["downgrade"] is True
    assert update_channel.read(root, environ={}).allow_downgrade == f"main@{tip}"


@pytest.mark.asyncio
async def test_an_unreachable_remote_is_a_bad_gateway_without_its_credentials(daemon):
    http, root, state, old = daemon
    git(root, "remote", "set-url", "origin", "https://owner:secret-token@127.0.0.1:9/agents.git")
    await login(http)
    revision = (await read(http))["updates"]["revision"]
    response = await put(http, expected_revision=revision, branch="dev")
    assert response.status_code == 502 and response.json()["error"] == "unreachable", response.text
    assert "secret-token" not in response.text and not (root / ".env").exists()


@pytest.mark.asyncio
async def test_a_running_control_command_makes_the_save_wait(daemon):
    http, root, state, old = daemon
    push_dev(root)
    await login(http)
    revision = (await read(http))["updates"]["revision"]
    with file_lock(state / "control.lock"):
        response = await put(http, expected_revision=revision, branch="dev")
    assert response.status_code == 409 and response.json()["error"] == "busy"
    assert not (root / ".env").exists()


def test_the_set_branch_call_runs_off_the_event_loop(installation, tmp_path, monkeypatch):  # noqa: F811
    """`set_branch` reads the remote: the page's save must not block the daemon's loop."""
    controller, root, old, target = installation
    from src.daemon import updates_ui
    threads = []

    def fake(*args, **kwargs):
        import threading
        threads.append(threading.current_thread() is threading.main_thread())
        return {"branch": "dev"}

    monkeypatch.setattr(update_channel, "set_branch", fake)
    ui = updates_ui.UpdatesUI(type("Service", (), {"directory": tmp_path / "service"})(), root=root)

    class Request:
        method = "PUT"

        async def body(self):
            return json.dumps({"expected_revision": "", "branch": "dev"}).encode()

    response = asyncio.run(ui.handle(Request(), updates_ui.PATH))
    assert response.status_code == 200 and threads == [False]


@pytest.mark.parametrize("action, scheduled, expected", [
    ("switch", True, "The switch from main to dev happens at the next scheduled run (every 15 min, when the service "
                     "is idle), or now with `.venv/bin/python -m src.daemon update`."),
    ("switch", False, "The switch from main to dev happens when you run `.venv/bin/python -m src.daemon update`; "
                      "scheduled updates are off."),
    ("follow", True, "Updates fast-forward dev at the next scheduled run"),
    ("local", True, "The checkout is on feature/x, not main, dev or the previous setting"),
])
def test_the_section_names_when_the_setting_applies(action, scheduled, expected):
    current = "feature/x" if action == "local" else "main"
    text = overview.when_text({"configured_branch": "dev", "checked_out_branch": current, "action": action},
                              scheduled, 900, overview.update_command("darwin"))
    assert text.startswith(expected)


def test_windows_names_its_own_update_command():
    assert overview.update_command("win32") == r".venv\Scripts\python -m src.daemon update"
