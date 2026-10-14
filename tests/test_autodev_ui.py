"""The Autodev page's routes (#348): ``autodev.json`` read and written through the config module,
the switch through the runner, the status and Codex's availability. A test daemon, a temporary
state directory and a fake command runner: no CLI, model, network or scheduler job is started."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime
import hashlib
import json
import os
import stat
import subprocess
import time
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from src.daemon import autodev_ui
from src.daemon.app import create_app
from src.schedules import codex_probes, records, runner
from src.schedules import config as autodev_config

TOKEN = "s" * 48
UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}
NOW = datetime(2026, 10, 12, 2, 20, tzinfo=ZoneInfo("UTC"))
AGENTS = {"sysadmin", "software_engineer", "code_reviewer"}
CLAUDE_HELP = ("Usage: claude [options]\n\nOptions:\n  --effort <level>   Effort level for the session\n"
               "                     (low, medium, high, xhigh, max)\n  --model <model>    Model\n")
GITHUB_TOKEN = "ghp_" + "A1b2C3d4" * 5


class FakeCommands:
    """Answers ``--version`` and ``--help`` the way the CLIs do; anything else fails the test."""

    def __init__(self, codex_version="0.162.1"):
        self.calls, self.codex_version = [], codex_version

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        name, option = os.path.basename(argv[0]), argv[-1]
        if name == "codex" and option == "--version":
            return subprocess.CompletedProcess(argv, 0, f"codex-cli {self.codex_version}\n", "")
        if name == "claude" and option == "--version":
            return subprocess.CompletedProcess(argv, 0, "2.1.296 (Claude Code)\n", "")
        if name == "claude" and option == "--help":
            return subprocess.CompletedProcess(argv, 0, CLAUDE_HELP, "")
        raise AssertionError(f"the page ran {argv}")


@pytest_asyncio.fixture
async def daemon(tmp_path):
    state = tmp_path / "service"
    codex = tmp_path / "codex-package" / "bin" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)
    commands = FakeCommands()

    def no_run(*args, **kwargs):
        raise AssertionError("the page started a run")

    def context():
        return runner.Context(installation=tmp_path / "installation", directory=state, python="/opt/python",
                              clock=lambda: NOW, launchctl=lambda argv: no_run(), agents_dir=tmp_path / "LaunchAgents",
                              supervise=no_run, claude=str(tmp_path / "claude-cli" / "claude"), codex=str(codex),
                              command_runner=commands, platform="darwin")

    def runtime(port):
        from mcp.server.fastmcp import FastMCP
        return FastMCP("test", stateless_http=True, json_response=True), None

    app = create_app(state, TOKEN, runtime_loader=runtime)
    ui = app.state.service.flows_ui.autodev
    ui.context, ui.agents = context, lambda: set(AGENTS)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.service.state != "starting":
                break
            await asyncio.sleep(.01)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://127.0.0.1:8765") as http:
            await login(http)
            yield http, state, commands, str(codex)


async def login(http) -> None:
    code = (await http.post("/admin/ui/code", headers={"Authorization": "Bearer " + TOKEN})).json()["code"]
    assert (await http.post("/ui/api/session", json={"code": code}, headers=UI)).status_code == 200


def config_file(state):
    return autodev_config.config_path(state)


async def read(http):
    response = await http.get("/ui/api/autodev", headers=UI)
    assert response.status_code == 200, response.text
    return response.json()


async def put(http, expected, settings):
    return await http.put("/ui/api/autodev", json={"expected_revision": expected, "settings": settings}, headers=UI)


@pytest.mark.asyncio
async def test_the_routes_need_a_session_and_the_page_header(tmp_path):
    def runtime(port):
        from mcp.server.fastmcp import FastMCP
        return FastMCP("test", stateless_http=True, json_response=True), None

    app = create_app(tmp_path / "service", TOKEN, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            assert (await http.get("/ui/api/autodev", headers=UI)).status_code == 401
            await login(http)
            for headers in ({}, {"Origin": "http://127.0.0.1:8765"},
                            {"X-Agents-UI": "1", "Origin": "https://attacker.example"},
                            {**UI, "Host": "attacker.example"}):
                assert (await http.get("/ui/api/autodev", headers=headers)).status_code == 403, headers
            for path in ("/ui/api/autodev", "/ui/api/autodev/enable", "/ui/api/autodev/disable"):
                method = "PUT" if path == "/ui/api/autodev" else "POST"
                for headers in ({"X-Agents-UI": "1"}, {"Origin": "http://127.0.0.1:8765"},
                                {"X-Agents-UI": "1", "Origin": "http://localhost:9999"}):
                    response = await http.request(method, path, json={}, headers=headers)
                    assert response.status_code == 403, (path, headers)
            assert (await http.post("/ui/api/autodev", json={}, headers=UI)).status_code == 405
            assert (await http.get("/ui/api/autodev/enable", headers=UI)).status_code == 405
            assert (await http.get("/ui/api/autodev/nothing", headers=UI)).status_code == 404
    assert not config_file(tmp_path / "service").exists()


@pytest.mark.asyncio
async def test_the_page_reads_the_defaults_and_writes_nothing(daemon):
    http, state, commands, _ = daemon
    data = await read(http)
    assert data["revision"] == "" and data["problems"] is None
    assert data["settings"] == data["defaults"] == autodev_ui.editable(autodev_config.DEFAULTS)
    settings = data["settings"]
    assert settings["times"] == ["02:17", "13:47"] and settings["catch_up_minutes"] == 120
    assert settings["allowed_hours"] == "08:00-23:00,00:00-06:00" and settings["require_ac_power"] is True
    assert settings["limits"] == {"max_reviews": 3, "max_bot_rounds": 3, "max_wait_minutes": 120}
    assert settings["stages"]["select"] == {"client": "claude", "claude": {"model": "opus", "effort": "high"},
                                            "codex": None}
    assert settings["stages"]["implement"]["claude"] == {"model": "opus", "effort": "xhigh"}
    assert settings["delegates"]["code-review-pass"] == {
        "client": "codex", "claude": None, "codex": {"model": "gpt-6.1-sol", "effort": "high"},
        "effort_followup": "medium"}
    assert settings["delegates"]["change-premortem"]["role"] == "sysadmin"
    assert data["choices"]["efforts"] == {"claude": ["low", "medium", "high", "xhigh", "max"],
                                          "codex": list(autodev_config.CODEX_EFFORTS)}
    assert data["choices"]["agents"] == sorted(AGENTS) and data["choices"]["modes"] == ["dry-run", "run"]
    assert data["capabilities"]["claude"] == {"client": "claude", "efforts": ["low", "medium", "high", "xhigh", "max"],
                                              "version": "2.1.296", "source": "installed"}
    assert data["capabilities"]["codex"]["version"] == "0.162.1"
    status = data["status"]
    assert (status["enabled"], status["mode"], status["runs"]) == (False, "dry-run", [])
    assert status["next_slot"] == "2026-10-12T13:47:00+00:00"
    assert status["scheduler"] == {"installed": False, "supported": True}
    assert status["real_modes"] == {"scheduled": False, "missing": ["#350 automatic mode"]}
    assert not config_file(state).exists() and not (state / "schedules" / "tick.json").exists()
    assert all(call[-1] in ("--version", "--help") for call in commands.calls)  # no model turn, no run


@pytest.mark.asyncio
async def test_the_capability_table_is_kept_between_reads(daemon):
    http, _, commands, _ = daemon
    await read(http)
    seen = len(commands.calls)
    assert seen == 3  # claude --version, claude --help, codex --version
    await read(http)
    assert len(commands.calls) == seen


def changed_settings(settings):
    changed = copy.deepcopy(settings)
    changed["times"] = ["03:05", "15:30"]
    changed["catch_up_minutes"] = 90
    changed["allowed_hours"] = "09:00-22:00"
    changed["require_ac_power"] = False
    changed["limits"]["max_reviews"] = 4
    changed["stages"]["select"]["claude"]["effort"] = "medium"
    changed["stages"]["implement"]["codex"] = {"model": "gpt-6.1-sol", "effort": "ultra"}  # kept for later
    changed["delegates"]["code-review-pass"]["effort_followup"] = "low"
    changed["delegates"]["code-review-pass"]["claude"] = {"model": "sonnet", "effort": "high"}
    changed["delegates"]["change-premortem"]["role"] = "software_engineer"
    return changed


@pytest.mark.asyncio
async def test_valid_settings_are_saved_atomically_into_a_private_file(daemon):
    http, state, _, _ = daemon
    path = config_file(state)
    path.parent.mkdir(parents=True)
    stored = {"github": {"deploy_key": "~/keys/autodev", "token": "keychain:custom-item"},
              "stages": {"implement": {"budget": {"timeout_minutes": 200}}}}
    path.write_text(json.dumps(stored))
    data = await read(http)
    assert data["revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    response = await put(http, data["revision"], changed_settings(data["settings"]))
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["saved"] is True and saved["warnings"] == []
    written = json.loads(path.read_text())
    expected = copy.deepcopy(autodev_config.DEFAULTS)
    expected.update(times=["03:05", "15:30"], catch_up_minutes=90, allowed_hours="09:00-22:00", require_ac_power=False)
    expected["limits"]["max_reviews"] = 4
    expected["github"] = stored["github"]  # never on the page, kept as stored
    expected["stages"]["select"]["claude"]["effort"] = "medium"
    expected["stages"]["implement"]["budget"] = {"timeout_minutes": 200}
    expected["stages"]["implement"]["codex"] = {"model": "gpt-6.1-sol", "effort": "ultra"}
    expected["delegates"]["code-review-pass"].update(effort_followup="low", claude={"model": "sonnet", "effort": "high"})
    expected["delegates"]["change-premortem"]["role"] = "software_engineer"
    assert written == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert sorted(entry.name for entry in path.parent.iterdir()) == ["autodev.json"]  # no temporary file left
    assert saved["revision"] == hashlib.sha256(path.read_bytes()).hexdigest() != data["revision"]
    assert saved["settings"] == autodev_ui.editable(expected)
    assert autodev_config.load(state) == expected  # what the runner reads
    # Keys left out keep their values; an emptied client setting removes that client's object.
    response = await put(http, saved["revision"], {"limits": {"max_bot_rounds": 2},
                                                   "stages": {"implement": {"codex": {"model": "", "effort": ""}}}})
    assert response.status_code == 200, response.text
    written = json.loads(path.read_text())
    assert written["limits"] == {"max_reviews": 4, "max_bot_rounds": 2, "max_wait_minutes": 120}
    assert "codex" not in written["stages"]["implement"] and written["times"] == ["03:05", "15:30"]


@pytest.mark.asyncio
async def test_a_model_warning_does_not_block_the_save(daemon):
    http, state, _, _ = daemon
    data = await read(http)
    settings = {"stages": {"select": {"client": "claude", "claude": {"model": "opus-next", "effort": "high"}}}}
    saved = (await put(http, data["revision"], settings)).json()
    assert saved["saved"] and saved["warnings"] == [
        "stages.select.claude.model: 'opus-next' is neither a known Claude alias (fable, haiku, opus, sonnet) nor a "
        "full claude-* model name; the CLI decides whether it exists"]
    assert autodev_config.load(state)["stages"]["select"]["claude"]["model"] == "opus-next"


@pytest.mark.asyncio
async def test_a_failed_write_leaves_the_file_as_it_was_and_no_partial_file(daemon, monkeypatch):
    http, state, _, _ = daemon
    data = await read(http)
    first = (await put(http, data["revision"], {"catch_up_minutes": 60})).json()
    path = config_file(state)
    before = path.read_bytes()

    def broken_replace(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("src.daemon.state.os.replace", broken_replace)
    response = await put(http, first["revision"], {"catch_up_minutes": 30})
    monkeypatch.undo()
    assert response.status_code == 500 and response.json()["error"] == "storage_error"
    assert path.read_bytes() == before
    assert sorted(entry.name for entry in path.parent.iterdir()) == ["autodev.json"]


INVALID = [
    ({"times": ["25:00"]}, "times: '25:00' is not a time of day HH:MM"),
    ({"times": []}, "times must be a non-empty list of HH:MM"),
    ({"catch_up_minutes": "soon"}, "catch_up_minutes must be an integer from 1 to 1440"),
    ({"allowed_hours": "8-23"}, "allowed_hours: '8' is not a time of day HH:MM"),
    ({"limits": {"max_reviews": 0}}, "limits must have positive integers max_reviews, max_bot_rounds, max_wait_minutes"),
    ({"stages": {"select": {"claude": {"model": "opus", "effort": "ultra"}}}},
     "stages.select.claude.effort: flow_invalid: claude does not take the effort 'ultra' "
     "(allowed: low, medium, high, xhigh, max)"),
    ({"stages": {"submit": {"claude": {"model": "opus; rm -rf ~", "effort": "high"}}}},
     "stages.submit.claude.model: flow_invalid: the model 'opus; rm -rf ~' is not a plain model name"),
    ({"stages": {"select": {"client": "gemini"}}}, "stages.select.client must be one of claude, codex"),
    ({"stages": {"select": {"client": "codex", "codex": None}}}, "stages.select.codex needs a model and an effort"),
    ({"delegates": {"code-review-pass": {"codex": {"model": "gpt-6.1-sol", "effort": "turbo"}}}},
     "delegates.code-review-pass: the Codex effort must be one of " + ", ".join(autodev_config.CODEX_EFFORTS)),
    ({"delegates": {"code-review-pass": {"client": "claude", "claude": {"model": "opus", "effort": "high"},
                                         "effort_followup": "ultra"}}},
     "delegates.code-review-pass.effort_followup: flow_invalid: claude does not take the effort 'ultra' "
     "(allowed: low, medium, high, xhigh, max)"),
    ({"delegates": {"change-premortem": {"role": "nobody"}}},
     "delegates.change-premortem.role: no agent named 'nobody' (agents/<name>/system_prompt.mdc)"),
    ({"delegates": {"change-premortem": {"role": "Sys Admin"}}},
     "delegates.change-premortem.role must be an agent name (lowercase letters, digits, underscores)"),
    ({"delegates": {"code-review-pass": {"role": "sysadmin"}}}, "delegates.code-review-pass has unknown keys: role"),
    ({"stages": {"select": {"claude": "opus"}}}, "stages.select.claude must be an object with a model and an effort"),
    ({"stages": {"review": {"client": "claude"}}}, "stages has no 'review'; it has select, implement, submit"),
    ({"mode": "run"}, "mode change only with the switch (turn on or off)"),
    ({"enabled": True, "mode": "dry-run"}, "enabled and mode change only with the switch (turn on or off)"),
    ({"github": {"token": "keychain:x"}}, "settings has unknown keys: github"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("settings, problem", INVALID)
async def test_invalid_settings_are_refused_with_a_message_and_the_file_unchanged(daemon, settings, problem):
    http, state, _, _ = daemon
    data = await read(http)
    saved = (await put(http, data["revision"], {"catch_up_minutes": 100})).json()
    before = config_file(state).read_bytes()
    response = await put(http, saved["revision"], settings)
    assert response.status_code == 400, response.text
    answer = response.json()
    assert answer["error"] == "invalid_settings" and problem in answer["problems"], answer
    assert answer["message"].startswith("not saved: ")
    assert config_file(state).read_bytes() == before


@pytest.mark.asyncio
async def test_without_a_file_an_invalid_save_creates_none(daemon):
    http, state, _, _ = daemon
    response = await put(http, "", {"times": ["7am"]})
    assert response.status_code == 400 and not config_file(state).exists()
    for body in ({"settings": {}}, {"expected_revision": "", "settings": {}, "force": True}):
        response = await http.put("/ui/api/autodev", json=body, headers=UI)
        assert response.status_code == 400 and response.json()["error"] == "invalid_request"
    assert not config_file(state).exists()


@pytest.mark.asyncio
async def test_a_file_changed_since_the_page_read_it_is_not_overwritten(daemon):
    http, state, _, _ = daemon
    data = await read(http)
    path = config_file(state)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"times": ["04:00"]}))  # the CLI or an editor, meanwhile
    response = await put(http, data["revision"], {"catch_up_minutes": 30})
    assert response.status_code == 409 and response.json()["error"] == "config_changed"
    assert json.loads(path.read_text()) == {"times": ["04:00"]}


@pytest.mark.asyncio
async def test_run_mode_is_refused_and_dry_run_turns_the_schedule_on_and_off(daemon):
    http, state, _, _ = daemon
    response = await http.post("/ui/api/autodev/enable", json={"mode": "run"}, headers=UI)
    assert response.status_code == 409
    answer = response.json()
    assert answer["error"] == "mode_unavailable" and answer["missing"] == ["#350 automatic mode"]
    assert "automatic mode (#350)" in answer["message"]
    assert not config_file(state).exists() and not (state / "schedules" / "tick.json").exists()
    response = await http.post("/ui/api/autodev/enable", json={"mode": "scheduled"}, headers=UI)
    assert response.status_code == 400 and response.json()["error"] == "invalid_request"

    response = await http.post("/ui/api/autodev/enable", json={"mode": "dry-run"}, headers=UI)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["result"]["enabled"] is True and answer["result"]["scheduler_installed"] is False
    assert "autodev install" in answer["result"]["note"]
    assert (answer["status"]["enabled"], answer["status"]["mode"]) == (True, "dry-run")
    stored = autodev_config.load(state)
    assert (stored["enabled"], stored["mode"]) == (True, "dry-run")
    assert stat.S_IMODE(config_file(state).stat().st_mode) == 0o600
    tick = json.loads((state / "schedules" / "tick.json").read_text())
    assert tick["enabled_at"] == "2026-10-12T02:20:00+00:00"
    # A run asked for while dry-run is on changes nothing either.
    before = config_file(state).read_bytes()
    assert (await http.post("/ui/api/autodev/enable", json={"mode": "run"}, headers=UI)).status_code == 409
    assert config_file(state).read_bytes() == before

    response = await http.post("/ui/api/autodev/disable", json={}, headers=UI)
    assert response.status_code == 200 and response.json()["status"]["enabled"] is False
    assert autodev_config.load(state)["enabled"] is False


@pytest.mark.asyncio
async def test_codex_is_shown_unavailable_until_a_stored_availability_says_available(daemon):
    http, state, _, codex = daemon
    found = (await read(http))["codex"]
    assert found["installed"] is True and found["version"] == "0.162.1"
    assert found["stages"]["select"] == {"available": False, "reason": "not_checked",
                                         "message": autodev_ui.REASONS["not_checked"], "checked": None}
    for kind in ("implement", "submit"):
        assert found["stages"][kind]["reason"] == "codex_stage_commit_unsupported"
        assert "commits" in found["stages"][kind]["message"]

    def store(entry):
        path = codex_probes.availability_path(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stage": entry}))

    shape = codex_probes.stage_shape(codex, autodev_config.load(state), "select")
    store({"available": False, "reason": "nested_sandbox_unavailable", "checked": "2026-10-11T10:00:00+00:00",
           "checked_at": time.time(), "version": "0.162.1", "shape": shape})
    select = (await read(http))["codex"]["stages"]["select"]
    assert (select["available"], select["reason"]) == (False, "nested_sandbox_unavailable")
    assert select["message"] == "a nested sandbox cannot apply on this macOS"
    assert select["checked"] == "2026-10-11T10:00:00+00:00"

    store({"available": True, "reason": None, "checked": "2026-10-11T11:00:00+00:00", "checked_at": time.time(),
           "version": "0.162.1", "shape": shape, "digest": "d" * 64, "installed": {"sandbox_runs": True}})
    found = (await read(http))["codex"]["stages"]
    assert found["select"] == {"available": True, "reason": None, "message": None,
                               "checked": "2026-10-11T11:00:00+00:00"}
    assert found["implement"]["available"] is False  # a stage that commits never runs with Codex

    store({"available": True, "reason": None, "checked": "x", "checked_at": time.time() - 8 * 24 * 3600,
           "version": "0.162.1", "shape": shape})
    assert (await read(http))["codex"]["stages"]["select"]["reason"] == "stale"


@pytest.mark.asyncio
async def test_the_status_lists_the_newest_runs_and_never_a_secret(daemon):
    http, state, _, codex = daemon
    key = os.path.expanduser("~/.config/agents-autodev/deploy_key")
    records.write_record("20261011T021700Z-aaaaaa", {"mode": "dry-run", "trigger": {"kind": "slot", "at": "x"},
                                                     "status": "completed", "gate": {"decision": "select"},
                                                     "started": "2026-10-11T02:17:05+00:00"}, state)
    records.write_record("20261011T134700Z-bbbbbb", {"mode": "dry-run", "trigger": {"kind": "slot", "at": "y"},
                                                     "status": "failed: infra",
                                                     "reason": f"cli: cannot read {key} with {GITHUB_TOKEN}",
                                                     "started": "2026-10-11T13:47:05+00:00"}, state)
    data = await read(http)
    runs = data["status"]["runs"]
    assert [run["run_id"] for run in runs] == ["20261011T134700Z-bbbbbb", "20261011T021700Z-aaaaaa"]
    assert runs[0]["status"] == "failed: infra" and runs[1]["decision"] == "select"
    assert runs[0]["reason"] == "cli: cannot read *** with ghp_***"
    text = json.dumps(data)
    for secret in (key, "deploy_key", "agents-autodev", GITHUB_TOKEN, "keychain:", codex, "claude-cli"):
        assert secret not in text, secret


@pytest.mark.asyncio
async def test_an_invalid_file_shows_its_problems_and_refuses_every_write(daemon):
    http, state, _, _ = daemon
    path = config_file(state)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"times": "02:17"}))
    data = await read(http)
    assert data["settings"] is None and data["codex"] is None
    assert data["problems"] == ["times must be a non-empty list of HH:MM"]
    assert data["status"]["enabled"] is None and data["status"]["next_slot"] is None
    for method, route, body in (("PUT", "/ui/api/autodev", {"expected_revision": data["revision"], "settings": {}}),
                                ("POST", "/ui/api/autodev/enable", {"mode": "dry-run"}),
                                ("POST", "/ui/api/autodev/disable", {})):
        response = await http.request(method, route, json=body, headers=UI)
        assert response.status_code == 409 and response.json()["error"] == "invalid_config", route
        assert response.json()["problems"] == ["times must be a non-empty list of HH:MM"]
    assert json.loads(path.read_text()) == {"times": "02:17"}


def test_the_role_list_is_the_agent_catalog():
    assert "sysadmin" in autodev_ui._agents()


@pytest.mark.asyncio
async def test_a_damaged_state_file_or_a_runner_that_cannot_load_is_an_error_not_a_crash(daemon, monkeypatch):
    http, state, _, _ = daemon
    tools = state / "schedules" / "tools.json"
    tools.parent.mkdir(parents=True)
    tools.write_text("{")
    response = await http.get("/ui/api/autodev", headers=UI)
    assert response.status_code == 500 and response.json()["error"] == "state_error"
    tools.unlink()
    import src.schedules
    monkeypatch.delattr(src.schedules, "runner")
    monkeypatch.setitem(__import__("sys").modules, "src.schedules.runner", None)
    response = await http.get("/ui/api/autodev", headers=UI)
    assert response.status_code == 501 and response.json()["error"] == "platform_unsupported"
