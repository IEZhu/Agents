"""The Autodev page's routes (#348): ``autodev.json`` read and written through the config module,
the switch through the runner, the status and Codex's availability. A test daemon, a temporary
state directory and a fake command runner: no CLI, model, network or scheduler job is started."""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import os
import stat
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from src.daemon import autodev_ui
from src.daemon.app import create_app
from src.schedules import automatic, codex_probes, decoys, records, runner
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


def no_credentials(*args, **kwargs):
    """The daemon reads no credential and nothing from GitHub (#350 review): the broker's
    credentials, the GitHub client and the deploy-key reader fail the test, and so does any command
    but the CLIs' ``--version`` and ``--help`` (`FakeCommands`; ``security`` among them)."""
    raise AssertionError("the page read a credential or GitHub")


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
                              command_runner=commands, platform="darwin", notify_runner=no_run,
                              credentials=no_credentials, client_factory=no_credentials, key_reader=no_credentials)

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
    assert data["revision"] == autodev_ui.revision(autodev_config.DEFAULTS) and data["problems"] is None
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
    automatic_mode = status["automatic"]
    assert (automatic_mode["can_enable"], automatic_mode["checked"], automatic_mode["passed"]) == (False, None, None)
    assert automatic_mode["accepted"] is None and automatic_mode["fresh_minutes"] == 15
    assert automatic_mode["missing"] == [NO_READY, automatic.acceptance_problems(state, NOW)[0]]
    assert [risk["row"] for risk in automatic_mode["risks"]] == [1, 2, 4]
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


@pytest.mark.asyncio
async def test_the_capability_table_expires_and_follows_the_executables(daemon, monkeypatch):
    http, _, commands, _ = daemon
    monkeypatch.setattr(autodev_ui, "CAPABILITY_TTL", -1.0)  # kept until a moment already past
    await read(http)
    await read(http)
    assert len(commands.calls) == 6
    monkeypatch.setattr(autodev_ui, "CAPABILITY_TTL", 600.0)
    await read(http)
    assert len(commands.calls) == 9
    monkeypatch.setattr(runner, "claude_executable", lambda ctx: "/other/claude")  # another executable
    await read(http)
    assert len(commands.calls) == 12 and commands.calls[-3][0] == "/other/claude"


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
    assert data["revision"] == autodev_ui.revision(autodev_config.load(state))
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
    # No temporary file left; the writers' lock (config.writing) stays.
    assert sorted(entry.name for entry in path.parent.iterdir()) == ["autodev.json", "autodev.lock"]
    assert saved["revision"] == autodev_ui.revision(expected) != data["revision"]
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
async def test_same_as_the_first_review_effort_is_kept_as_null(daemon):
    """'Same as the first' (no effort_followup) is stored as null, which the merge with the defaults
    keeps and the run reads as the first review's effort (`workers.client`: a falsy follow-up)."""
    http, state, _, _ = daemon
    data = await read(http)
    response = await put(http, data["revision"], {"delegates": {"code-review-pass": {"effort_followup": None}}})
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["delegates"]["code-review-pass"]["effort_followup"] is None
    assert json.loads(config_file(state).read_text())["delegates"]["code-review-pass"]["effort_followup"] is None
    assert autodev_config.load(state)["delegates"]["code-review-pass"]["effort_followup"] is None
    assert (await read(http))["settings"]["delegates"]["code-review-pass"]["effort_followup"] is None
    again = await put(http, response.json()["revision"], {"delegates": {"code-review-pass": {"effort_followup": ""}}})
    assert again.status_code == 200 and autodev_config.load(state)["delegates"]["code-review-pass"]["effort_followup"] is None
    later = await put(http, again.json()["revision"], {"delegates": {"code-review-pass": {"effort_followup": "low"}}})
    assert later.status_code == 200 and autodev_config.load(state)["delegates"]["code-review-pass"]["effort_followup"] == "low"


@pytest.mark.asyncio
async def test_a_padded_role_and_follow_up_effort_are_saved_trimmed(daemon):
    http, state, _, _ = daemon
    data = await read(http)
    response = await put(http, data["revision"], {"delegates": {
        "code-review-pass": {"effort_followup": " low "}, "change-premortem": {"role": " software_engineer "}}})
    assert response.status_code == 200, response.text
    stored = json.loads(config_file(state).read_text())["delegates"]
    assert stored["code-review-pass"]["effort_followup"] == "low"
    assert stored["change-premortem"]["role"] == "software_engineer"


@pytest.mark.asyncio
async def test_a_hidden_pre_mortem_follow_up_effort_does_not_block_a_client_change(daemon):
    """The page edits effort_followup of the review only; a pre-mortem runs once, so a stored
    pre-mortem effort_followup is never used and is not checked against a new client."""
    http, state, _, _ = daemon
    path = config_file(state)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"delegates": {"change-premortem": {
        "client": "codex", "codex": {"model": "gpt-6.1-sol", "effort": "high"}, "effort_followup": "minimal"}}}))
    data = await read(http)
    response = await put(http, data["revision"], {"delegates": {"change-premortem": {
        "client": "claude", "claude": {"model": "opus", "effort": "high"}}}})
    assert response.status_code == 200, response.text
    stored = autodev_config.load(state)["delegates"]["change-premortem"]
    assert stored["client"] == "claude" and stored["effort_followup"] == "minimal"  # kept, as every hidden key


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
    assert sorted(entry.name for entry in path.parent.iterdir()) == ["autodev.json", "autodev.lock"]


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
    ({"stages": {"select": {"claude": {"model": "opus", "effort": "high", "x": "y"}}}},
     "stages.select.claude must be an object with a model and an effort"),
    ({"stages": {"select": {"codex": {"model": 5, "effort": "high"}}}},
     "stages.select.codex must be an object with a model and an effort"),
    ({"stages": {"select": {"budget": {"timeout_minutes": 5}}}}, "stages.select has unknown keys: budget"),
    ({"stages": {"select": {"modle": "opus"}}}, "stages.select has unknown keys: modle"),
    # The fallback client of a Codex profile is checked too: its worker would start with that effort.
    ({"delegates": {"code-review-pass": {"claude": {"model": "opus", "effort": "ultra"}}}},
     "delegates.code-review-pass.claude.effort: flow_invalid: claude does not take the effort 'ultra' "
     "(allowed: low, medium, high, xhigh, max)"),
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
    response = await put(http, (await read(http))["revision"], {"times": ["7am"]})
    assert response.status_code == 400 and response.json()["error"] == "invalid_settings"
    assert not config_file(state).exists()
    for body in ({"settings": {}}, {"expected_revision": "", "settings": {}, "force": True}):
        response = await http.put("/ui/api/autodev", json=body, headers=UI)
        assert response.status_code == 400 and response.json()["error"] == "invalid_request"
    assert not config_file(state).exists()


@pytest.mark.asyncio
async def test_a_body_over_the_limit_is_refused(daemon):
    http, state, _, _ = daemon
    data = await read(http)
    body = json.dumps({"expected_revision": data["revision"], "settings": {"catch_up_minutes": 60}})
    response = await http.put("/ui/api/autodev", content=body + " " * autodev_ui.MAX_BODY,
                              headers={**UI, "Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["message"] == "the body must be a small JSON object"
    assert not config_file(state).exists()
    response = await http.put("/ui/api/autodev", content=body, headers={**UI, "Content-Type": "application/json"})
    assert response.status_code == 200, response.text


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
async def test_the_revision_leaves_out_the_switch_but_not_a_change_elsewhere(daemon):
    """The switch, the CLI and the tripwire change only enabled (and mode): a form read before
    still saves. Any other change elsewhere gives a new revision, and the form's save is refused."""
    http, state, _, _ = daemon
    data = await read(http)
    turned_on = (await http.post("/ui/api/autodev/enable", json={"mode": "dry-run"}, headers=UI)).json()
    assert turned_on["revision"] == data["revision"]
    decoys.trip(state, "probe: decoy seen")
    assert (await read(http))["revision"] == data["revision"]
    response = await put(http, data["revision"], {"catch_up_minutes": 30})
    assert response.status_code == 200, response.text
    stored = autodev_config.load(state)
    assert (stored["enabled"], stored["catch_up_minutes"]) == (False, 30)  # the tripwire's off is kept
    # A change made elsewhere (the CLI, an editor) is in the switch's write, but the form's revision
    # is still the one it read: its save is refused.
    revision = response.json()["revision"]
    changed = autodev_config.load(state)
    changed["times"] = ["04:00"]
    autodev_config.save(changed, state)
    assert (await http.post("/ui/api/autodev/enable", json={"mode": "dry-run"}, headers=UI)).json()["revision"] != revision
    response = await put(http, revision, {"catch_up_minutes": 45})
    assert response.status_code == 409 and response.json()["error"] == "config_changed"
    assert autodev_config.load(state)["times"] == ["04:00"]


@pytest.mark.asyncio
async def test_the_revision_describes_the_settings_read_not_a_newer_file(daemon, monkeypatch):
    http, state, _, _ = daemon
    path = config_file(state)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"catch_up_minutes": 100}))
    real_load = autodev_config.load

    def load_then_replaced(directory=None):
        loaded = real_load(directory)
        path.write_text(json.dumps({"catch_up_minutes": 50}))  # the CLI, between the read and the hash
        return loaded

    monkeypatch.setattr(autodev_config, "load", load_then_replaced)
    data = await read(http)
    monkeypatch.setattr(autodev_config, "load", real_load)
    assert data["settings"]["catch_up_minutes"] == 100
    # The revision is the one of the settings shown, so a save of them would undo the newer file: refused.
    response = await put(http, data["revision"], {"times": ["05:00"]})
    assert response.status_code == 409 and response.json()["error"] == "config_changed"
    assert json.loads(path.read_text()) == {"catch_up_minutes": 50}
    assert data["revision"] == autodev_ui.revision({**copy.deepcopy(autodev_config.DEFAULTS), "catch_up_minutes": 100})


@pytest.mark.asyncio
async def test_a_save_response_describes_its_write_and_refuses_a_later_stale_form(daemon, monkeypatch):
    http, state, _, _ = daemon
    data = await read(http)
    settings = copy.deepcopy(data["settings"])
    settings["times"] = ["03:00"]
    settings["stages"]["select"]["claude"]["model"] = "opus-next"
    real_writing = autodev_config.writing
    written = []

    @contextmanager
    def writing_then_changed(directory=None):
        with real_writing(directory):
            yield
            written.append(autodev_config.load(directory))
        # Another writer changes a different form field after this PUT releases its lock,
        # before its response is built. No sleep or thread scheduling decides the interleaving.
        with real_writing(directory):
            newer = autodev_config.load(directory)
            newer["allowed_hours"] = "07:00-20:00"
            autodev_config.save(newer, directory)

    with monkeypatch.context() as patch:
        patch.setattr(autodev_config, "writing", writing_then_changed)
        response = await put(http, data["revision"], settings)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert len(written) == 1
    assert saved["settings"] == autodev_ui.editable(written[0])
    assert saved["revision"] == autodev_ui.revision(written[0])
    assert saved["status"]["next_slot"] == "2026-10-12T03:00:00+00:00"
    assert (saved["status"]["enabled"], saved["status"]["mode"]) == (False, "dry-run")
    assert saved["saved"] is True and len(saved["warnings"]) == 1 and "opus-next" in saved["warnings"][0]
    assert autodev_config.load(state)["allowed_hours"] == "07:00-20:00"
    before = config_file(state).read_bytes()
    # The page keeps its form when a later edit arrives during the PUT. Saving that full form
    # with the first PUT's revision must refuse to overwrite the other writer's allowed hours.
    settings["catch_up_minutes"] = 30
    response = await put(http, saved["revision"], settings)
    assert response.status_code == 409 and response.json()["error"] == "config_changed"
    assert config_file(state).read_bytes() == before


def _write_during_the_save(monkeypatch, write):
    """Run ``write`` (another writer of autodev.json: the tripwire, the CLI) in a thread while the
    page's save is between its load and its write; return that thread."""
    real = autodev_ui.execution_problems
    started: list[threading.Thread] = []

    def checking(*args, **kwargs):
        thread = threading.Thread(target=write)
        thread.start()
        thread.join(0.3)  # the other writer waits for the page's lock, else it is done by now
        started.append(thread)
        return real(*args, **kwargs)

    monkeypatch.setattr(autodev_ui, "execution_problems", checking)
    return started


def _context(state):
    return runner.Context(installation=state.parent / "installation", directory=state, python="/opt/python",
                          clock=lambda: NOW, launchctl=lambda argv: None, agents_dir=state.parent / "LaunchAgents",
                          platform="darwin")


@pytest.mark.asyncio
@pytest.mark.parametrize("writer, enabled_before, enabled_after", [
    ("tripwire", True, False), ("cli disable", True, False), ("cli enable", False, True)])
async def test_a_save_never_undoes_a_concurrent_tripwire_or_cli_switch(daemon, monkeypatch, writer, enabled_before,
                                                                       enabled_after):
    http, state, _, _ = daemon
    if enabled_before:
        assert (await http.post("/ui/api/autodev/enable", json={"mode": "dry-run"}, headers=UI)).status_code == 200
    else:
        await put(http, (await read(http))["revision"], {"catch_up_minutes": 100})
    data = await read(http)
    write = {"tripwire": lambda: decoys.trip(state, "probe: decoy seen"),
             "cli disable": lambda: runner.disable(_context(state)),
             "cli enable": lambda: runner.enable(_context(state), "dry-run")}[writer]
    threads = _write_during_the_save(monkeypatch, write)
    response = await put(http, data["revision"], {"times": ["03:00"]})
    assert response.status_code == 200, response.text
    [thread] = threads
    thread.join(10)
    assert not thread.is_alive()
    stored = autodev_config.load(state)
    assert (stored["enabled"], stored["mode"], stored["times"]) == (enabled_after, "dry-run", ["03:00"])


@pytest.mark.asyncio
async def test_run_mode_is_refused_and_dry_run_turns_the_schedule_on_and_off(daemon):
    http, state, _, _ = daemon
    response = await http.post("/ui/api/autodev/enable", json={"mode": "run"}, headers=UI)
    assert response.status_code == 409
    answer = response.json()
    assert answer["error"] == "acceptance_required" and "accept_shared_account: true" in answer["message"]
    assert [risk["row"] for risk in answer["risks"]] == [1, 2, 4]
    response = await http.post("/ui/api/autodev/enable", json={"mode": "run", "accept_shared_account": True},
                               headers=UI)
    assert response.status_code == 409
    answer = response.json()
    assert answer["error"] == "not_ready" and answer["missing"][0] == NO_READY
    assert not config_file(state).exists() and not (state / "schedules" / "tick.json").exists()
    assert not automatic.acceptance_path(state).exists()
    for body in ({"mode": "scheduled"}, {"mode": "run", "accept_shared_account": "yes"},
                 {"mode": "dry-run", "accept": True}):
        response = await http.post("/ui/api/autodev/enable", json=body, headers=UI)
        assert response.status_code == 400 and response.json()["error"] == "invalid_request", body

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
    assert (await http.post("/ui/api/autodev/enable", json={"mode": "run", "accept_shared_account": True},
                            headers=UI)).status_code == 409
    assert config_file(state).read_bytes() == before

    response = await http.post("/ui/api/autodev/disable", json={}, headers=UI)
    assert response.status_code == 200 and response.json()["status"]["enabled"] is False
    assert autodev_config.load(state)["enabled"] is False


NO_READY = "`python -m src.schedules autodev ready` has no recorded result; run it in a terminal"


def cli_ready(state, minutes_ago=5):
    """What `autodev ready` in a terminal leaves for the page: a passing result, and the CLI's acceptance."""
    automatic.record_acceptance(state, NOW - timedelta(hours=1), source="cli")
    automatic.record_ready(state, automatic.Readiness([], []), NOW - timedelta(minutes=minutes_ago))


@pytest.mark.asyncio
async def test_the_page_offers_run_mode_on_a_recent_passing_ready_and_records_the_acceptance(daemon):
    http, state, commands, _ = daemon
    cli_ready(state)
    found = (await read(http))["status"]["automatic"]
    assert found == {"can_enable": True, "missing": [], "checked": "2026-10-12T02:15:00+00:00", "passed": True,
                     "fresh_minutes": 15, "accepted": "2026-10-12T01:20:00+00:00", "risks": automatic.risks(),
                     "risk_list_sha256": automatic.risk_list_version()}

    response = await http.post("/ui/api/autodev/enable", json={"mode": "run"}, headers=UI)
    assert response.status_code == 409 and response.json()["error"] == "acceptance_required"
    response = await http.post("/ui/api/autodev/enable", json={"mode": "run", "accept_shared_account": True},
                               headers=UI)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert (answer["result"]["mode"], answer["result"]["acceptance"]["source"]) == ("run", "page")
    assert (answer["status"]["enabled"], answer["status"]["mode"]) == (True, "run")
    stored = automatic.read_acceptance(state)
    assert stored["source"] == "page" and stored["accepted"] == "2026-10-12T02:20:00+00:00"  # given now
    assert stat.S_IMODE(automatic.acceptance_path(state).stat().st_mode) == 0o600
    assert all(call[-1] in ("--version", "--help") for call in commands.calls)  # no keychain read
    # Turning off from the page withdraws the acceptance: run mode needs the CLI's flag again.
    response = await http.post("/ui/api/autodev/disable", json={}, headers=UI)
    found = response.json()["status"]["automatic"]
    assert found["can_enable"] is False and found["accepted"] is None
    assert found["missing"] == [f"{automatic.ACCEPTANCE_ITEM}: withdrawn when the schedule was turned off or set to "
                                f"dry-run (2026-10-12T02:20:00+00:00); accept again with `{automatic.ENABLE_COMMAND}`"]


@pytest.mark.asyncio
@pytest.mark.parametrize("what", ["old", "failed", "tripwire"])
async def test_the_page_refuses_run_mode_without_a_recent_passing_ready_or_after_a_trip(daemon, what):
    http, state, _, _ = daemon
    cli_ready(state, minutes_ago=16 if what == "old" else 5)
    if what == "failed":
        automatic.record_ready(state, automatic.Readiness(["the deploy key"], []), NOW - timedelta(minutes=5))
    elif what == "tripwire":
        decoys.trip(state, "stage session: the stream carries the marker of decoy token")
    found = (await read(http))["status"]["automatic"]
    assert found["can_enable"] is False
    expected = {"old": ["the last passing `python -m src.schedules autodev ready` (2026-10-12T02:04:00+00:00) is not "
                        "from the last 15 minutes; run it again"],
                "failed": ["the last `python -m src.schedules autodev ready` (2026-10-12T02:15:00+00:00) did not pass",
                           "the deploy key"]}.get(what)
    if expected:
        assert found["missing"] == expected and found["accepted"] == "2026-10-12T01:20:00+00:00"
    else:
        assert found["accepted"] is None and "a tripwire turned the schedule off after it" in found["missing"][0]
    response = await http.post("/ui/api/autodev/enable", json={"mode": "run", "accept_shared_account": True},
                               headers=UI)
    assert response.status_code == 409 and response.json()["error"] == "not_ready"
    assert response.json()["missing"] == found["missing"]
    assert autodev_config.load(state)["enabled"] is False


@pytest.mark.asyncio
async def test_the_daemon_never_reads_the_credentials_or_github_for_the_automatic_mode(daemon, monkeypatch):
    """security-4: the page shows the CLI's recorded result; it never runs the readiness checks."""
    http, state, commands, _ = daemon
    from src.schedules import broker, github as github_module
    for module, name in ((runner, "readiness"), (runner, "github_conditions"), (broker, "credential_problems"),
                         (broker.Credentials, "from_config"), (github_module, "keychain_token")):
        monkeypatch.setattr(module, name, no_credentials)
    assert (await read(http))["status"]["automatic"]["can_enable"] is False
    cli_ready(state)
    assert (await read(http))["status"]["automatic"]["can_enable"] is True
    response = await http.post("/ui/api/autodev/enable", json={"mode": "run", "accept_shared_account": True},
                               headers=UI)
    assert response.status_code == 200, response.text
    assert (await http.post("/ui/api/autodev/disable", json={}, headers=UI)).status_code == 200
    assert all(call[-1] in ("--version", "--help") for call in commands.calls)


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


def test_every_reason_codex_can_be_unavailable_for_has_words():
    from src.schedules import stages
    stored = {*codex_probes.REASONS, *codex_probes.ABORT_REASONS, stages.CODEX_COMMIT_UNSUPPORTED,
              "not_checked", "version_changed", "shape_changed", "stale"}  # the last four: read_availability
    assert stored <= set(autodev_ui.REASONS), stored - set(autodev_ui.REASONS)
    assert all(text and text != reason for reason, text in autodev_ui.REASONS.items())


@pytest.mark.asyncio
async def test_a_stored_probe_failure_is_shown_in_words(daemon):
    http, state, _, _ = daemon
    path = codex_probes.availability_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stage": {"available": False, "reason": "login_missing", "checked": "x"}}))
    select = (await read(http))["codex"]["stages"]["select"]
    assert (select["reason"], select["message"]) == ("login_missing", autodev_ui.REASONS["login_missing"])


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
    sandbox_dir = os.path.expanduser("~/.config/agents-autodev/sandbox-config")  # another file of the secrets folder
    records.write_record("20261011T150000Z-cccccc", {"mode": "dry-run", "status": "failed: infra",
                                                     "gate": {"reason": f"no login in {sandbox_dir}"}}, state)
    data = await read(http)
    runs = data["status"]["runs"]
    assert [run["run_id"] for run in runs] == ["20261011T150000Z-cccccc", "20261011T134700Z-bbbbbb",
                                               "20261011T021700Z-aaaaaa"]
    assert runs[1]["status"] == "failed: infra" and runs[2]["decision"] == "select"
    assert runs[1]["reason"] == "cli: cannot read *** with ghp_***"
    assert runs[0]["decision_reason"] == "no login in ***/sandbox-config"
    text = json.dumps(data)
    for secret in (key, "deploy_key", "agents-autodev", GITHUB_TOKEN, "keychain:", codex, "claude-cli"):
        assert secret not in text, secret


@pytest.mark.asyncio
async def test_the_status_lists_the_ten_newest_runs_newest_first(daemon):
    http, state, _, _ = daemon
    ids = [f"20261011T{hour:02d}0000Z-{hour:06d}" for hour in range(12)]
    for run_id in ids:
        records.write_record(run_id, {"mode": "dry-run", "status": "completed"}, state)
    runs = (await read(http))["status"]["runs"]
    assert [run["run_id"] for run in runs] == ids[::-1][:autodev_ui.RUNS_SHOWN] and autodev_ui.RUNS_SHOWN == 10


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
