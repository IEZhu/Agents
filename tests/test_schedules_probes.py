"""Sandbox probes (#343) against a fake ``claude`` in a temporary home: pass and fail detection.

The fake (tests/fixtures/fake_claude.py) behaves like a working sandbox by default and is told to
open one hole at a time. Nothing here starts a model, touches the keychain or launchd, or plants
anything in the real home directory.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from src.schedules import cli, config as autodev_config, decoys, probes, records, runner, sandbox
from src.schedules.lease import schedules_dir
from tests.test_schedules_sandbox import RUN_ID, SESSION_PATH, fake_claude, make_paths

READ_TOOLS = {"Bash", "Read", "Grep", "Glob"}
WRITE_TOOLS = {"Bash", "Write", "Edit"}


class FakeCommands:
    """``launchctl list``/``remove`` and ``security`` answers; everything else runs for real
    (the fake claude's ``auth status``)."""

    def __init__(self, job_exists=False):
        self.job_exists = job_exists
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == probes.LAUNCHCTL:
            return subprocess.CompletedProcess(argv, 0 if self.job_exists else 113)
        if argv[0] == "/usr/bin/security":
            return subprocess.CompletedProcess(argv, 44, "", "")
        return subprocess.run(argv, **kwargs)


@pytest.fixture
def world(tmp_path_factory, service_state_dir):
    base = tmp_path_factory.mktemp("probe")
    paths = make_paths(base, service_state_dir)
    for directory in ("installation", "trusted.git/hooks", "LaunchAgents", "bin"):
        (base / directory).mkdir(parents=True)
    plist = base / "LaunchAgents" / "local.agents-core.0123456789abcdef.scheduler.plist"
    plist.write_text("<plist/>\n")
    autodev_config.save(dict(autodev_config.DEFAULTS), service_state_dir)
    records.run_dir(RUN_ID, service_state_dir)
    targets = probes.Targets(installation=base / "installation", runs=records.runs_dir(service_state_dir),
                             schedules=schedules_dir(service_state_dir),
                             config_file=autodev_config.config_path(service_state_dir),
                             trusted=base / "trusted.git", plist=plist)
    planted = decoys.ensure(service_state_dir, paths.home)
    return SimpleNamespace(base=base, paths=paths, state=service_state_dir, targets=targets, planted=planted,
                           commands=FakeCommands())


def build(world, scenario, kind="implement", paths=None, **options):
    scenario = {"seen": str(world.base / "seen.json"), **scenario}
    claude = fake_claude(world.base / "bin", scenario)
    return sandbox.build(claude, kind, paths or world.paths, path=SESSION_PATH,
                         run_variables={"AUTODEV_RUN_ID": RUN_ID}, **options)


def guard(world, spec, **options):
    options = {"directory": world.state, "targets": world.targets, "persistent": world.planted, "mode": "dry-run",
               "run_id": RUN_ID, "platform": "darwin", "managed": lambda: [], "command_runner": world.commands,
               **options}
    return probes.guard(spec, **options)


def probe(world, scenario=None, **options):
    spec = build(world, scenario or {})
    return guard(world, spec, **options), spec


def leftovers(world):
    found = []
    for directory in (world.paths.exchange, world.paths.repository, world.paths.state_copy):
        found += [path.name for path in directory.iterdir() if "probe-" in path.name]
    return found


# --- a working sandbox ------------------------------------------------------------------------------

def test_a_working_sandbox_passes_every_probe(world):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, world.state)
    report, spec = probe(world)
    assert report.ok, report.failures
    assert report.ran_session and report.digest == spec.digest() and not report.tripped
    assert all(check["ok"] for check in report.checks)
    assert report.init == {"tools": sorted(sandbox.STAGE_TOOLS), "mcp_servers": 0, "permission_mode": "dontAsk"}
    assert report.observed["tmpdir_inside_exchange"] is True and report.observed["fds"] <= probes.MAX_FDS
    groups = {check["group"] for check in report.checks}
    assert {"C0", "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C13", "C15", "C18"} <= groups
    by_tool = lambda prefix: {check["tool"] for check in report.checks if check["id"].startswith(prefix)}
    assert by_tool("read.credentials.") == READ_TOOLS and by_tool("read.owner-temp.") >= {"Bash", "Read"}
    assert by_tool("write.") == WRITE_TOOLS
    assert {"control.exchange.bash", "control.exchange.write", "control.repo.write", "control.repo.edit",
            "control.exchange.read", "control.exchange.grep", "control.exchange.glob", "control.tmpdir.bash",
            "control.sandbox-loaded.bash"} <= {check["id"] for check in report.checks}
    assert autodev_config.load(world.state)["enabled"] is True


def test_the_probe_runs_exactly_the_sessions_argv_environment_and_directory(world):
    report, spec = probe(world)
    seen = json.loads((world.base / "seen.json").read_text())
    assert seen["argv"] == list(spec.argv[1:]) and seen["cwd"] == os.path.realpath(spec.cwd)
    assert {name: seen["env"][name] for name in spec.env} == dict(spec.env)
    assert set(seen["env"]) - set(spec.env) <= {"PWD", "SHLVL", "OLDPWD", "_", "__CF_USER_TEXT_ENCODING",
                                                 "PYTHONNOUSERSITE"}  # added by the shell and the fake's interpreter


def test_the_probe_cleans_up_after_itself(world):
    report, _ = probe(world)
    assert report.ok and leftovers(world) == []
    assert list(world.paths.owner_temp.iterdir()) == [] and list(world.paths.claude_temp.iterdir()) == []
    assert all(decoy.path.exists() for decoy in world.planted)


def test_permission_denied_events_count_as_denied(world):
    report, _ = probe(world, {"denial_events": True})
    assert report.ok, report.failures


def test_the_record_holds_verdicts_but_no_marker(world):
    report, _ = probe(world, {"allow": ["read.credentials.bash"]})
    record = probes.record_report(RUN_ID, world.state, report, world.planted)
    text = (records.run_dir(RUN_ID, world.state) / records.RECORD_NAME).read_text()
    assert record["probes"][0]["ok"] is False and record["probes"][0]["tripped"] is True
    for decoy in world.planted:
        assert decoy.marker not in text
    assert "decoy credentials" in text  # the label, never the value


# --- holes, one at a time -----------------------------------------------------------------------------

@pytest.mark.parametrize("scenario, failing", [
    ({"allow": ["read.credentials.bash"]}, "read.credentials.bash"),
    ({"allow_paths": [decoys.DECOY_DIR]}, "read.credentials.read"),
    ({"allow": ["bypass.dangerously-disable-sandbox.bash"]}, "bypass.dangerously-disable-sandbox.bash"),
    ({"allow": ["link.symlink-read.bash"]}, "link.symlink-read.bash"),
    ({"allow": ["read.owner-temp.bash"]}, "read.owner-temp.bash"),
])
def test_a_readable_decoy_fails_and_turns_the_schedule_off(world, scenario, failing):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, world.state)
    report, _ = probe(world, scenario)
    assert not report.ok and report.tripped
    assert any(failing in failure for failure in report.failures), report.failures
    assert autodev_config.load(world.state)["enabled"] is False
    assert json.loads((schedules_dir(world.state) / decoys.TRIPWIRE_NAME).read_text())["count"] == 1


def test_a_marker_in_the_models_text_is_a_leak(world):
    report, _ = probe(world, {"say": "here it is: " + world.planted[2].marker})
    assert not report.ok and report.tripped
    assert any(hit["label"] == "decoy token" and hit["kind"] == "known" for hit in report.hits)
    assert all(set(hit) == {"stream", "kind", "label", "start", "end"} for hit in report.hits)


@pytest.mark.parametrize("probe_id, created", [
    ("write.home.bash", lambda w, n: w.paths.home / f"probe-{n}-bash"),
    ("write.trusted-hooks.bash", lambda w, n: w.base / "trusted.git/hooks" / f"probe-{n}-bash"),
    ("control.sandbox-loaded.bash", lambda w, n: w.paths.home / decoys.DECOY_DIR / f"probe-{n}-bash"),
    ("link.hardlink.bash", lambda w, n: w.paths.exchange / f"probe-{n}-hard"),
])
def test_a_write_that_succeeded_fails_and_is_removed(world, probe_id, created, monkeypatch):
    nonce = "0123456789abcdef"
    monkeypatch.setattr(probes.secrets, "token_hex", lambda size: nonce)
    report, _ = probe(world, {"allow": [probe_id]})
    assert not report.ok
    assert any(failure.startswith(f"{probe_id}: ") for failure in report.failures), report.failures
    assert not created(world, nonce).exists()


def test_an_append_that_succeeded_fails(world):
    report, _ = probe(world, {"allow": ["append.plist.bash"]})
    assert not report.ok and any("append.plist.bash" in failure for failure in report.failures)


@pytest.mark.parametrize("scenario, failing", [
    ({"deny": ["control.exchange.bash"]}, "control.exchange.bash"),
    ({"deny": ["control.tmpdir.bash"]}, "control.tmpdir.bash"),
    ({"deny_paths": ["/exchange/probe-"]}, "control.exchange.write"),
    ({"deny_paths": ["/repo/.autodev-probe-"]}, "control.repo.edit"),
])
def test_a_failed_positive_control_aborts(world, scenario, failing):
    report, _ = probe(world, scenario)
    assert not report.ok and not report.tripped
    assert any(failure.startswith(failing) for failure in report.failures), report.failures


@pytest.mark.parametrize("scenario, failure", [
    ({"skip": [0]}, "got missing"),
    ({"extra_call": {"tool": "Bash", "input": {"command": "ls ~"}}}, "an unexpected Bash call"),
    ({"init": {"tools": list(sandbox.STAGE_TOOLS) + ["WebFetch"]}}, "init tools differ from the session's (extra: WebFetch)"),
    ({"init": {"mcp_servers": [{"name": "claude.ai Gmail", "status": "connected"}]}}, "init lists 1 MCP server(s)"),
    ({"init": {"permissionMode": "default"}}, "init permission mode is 'default'"),
    ({"init": {"plugins": [{"name": "x", "path": "/x"}]}}, "init lists plugins"),
    ({"garbage": True}, "line(s) of the stream are not JSON events"),
    ({"allow": ["keychain.claude-code-credentials.bash"]}, "keychain.claude-code-credentials.bash"),
    ({"allow": ["gh.token.bash"]}, "gh.token.bash"),
    ({"allow": ["network.external.bash"]}, "network.external.bash"),
    ({"allow": ["apple-events.osascript.bash"]}, "apple-events.osascript.bash"),
    ({"allow": ["read.git-credentials.bash"]}, "read.git-credentials.bash"),
    ({"allow_paths": [".ssh"]}, "read.ssh-dir.glob"),
])
def test_a_mismatch_aborts(world, scenario, failure):
    # A missing file also gives a non-zero exit code; the owner's may exist, so the probe must see one.
    (world.paths.home / ".git-credentials").write_text("https://owner:not-a-secret@example.com\n")
    report, _ = probe(world, scenario)
    assert not report.ok
    assert any(failure in item for item in report.failures), report.failures


def test_a_connection_to_a_decoy_listener_fails(world):
    report, _ = probe(world, {"allow": ["network.local-listener.bash", "socket.foreign.bash"]})
    assert not report.ok
    assert any("decoy listener accepted 1 connection" in failure and "network.local-listener" in failure
               for failure in report.failures)
    assert any("socket.foreign.bash: the decoy listener accepted" in failure for failure in report.failures)


def test_a_launchd_job_from_the_probe_fails_and_is_removed(world):
    world.commands.job_exists = True
    report, _ = probe(world)
    assert not report.ok and "launchd.submit.bash: the probe's launchd job exists" in report.failures
    assert [call[1] for call in world.commands.calls if call[0] == probes.LAUNCHCTL] == ["list", "remove"]


def test_the_runner_socket_must_be_reachable(world):
    directory = Path(tempfile.mkdtemp(prefix="sbx-"))
    try:
        listener = probes.Listener.unix(directory / "c.sock")
        paths = sandbox.SessionPaths(**{**world.paths.__dict__, "socket": directory / "c.sock"})
        report = guard(world, build(world, {}, paths=paths))
        assert report.ok, report.failures
        assert listener.accepted == 1
        listener.close()
        (directory / "c.sock").unlink()
        report = guard(world, build(world, {}, paths=paths))
        assert not report.ok and any(f.startswith("control.socket.bash") for f in report.failures)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_the_keychain_mode_hides_the_token_from_bash(world):
    token = "sk-ant-oat01-" + "Q" * 40
    spec = build(world, {}, mode="keychain:claude-autodev", oauth_token=token)
    report = guard(world, spec, known=[decoys.Decoy("oauth", None, token).known])
    assert report.ok, report.failures
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in report.observed["env_names"]
    spec = build(world, {"leak_oauth_env": True}, mode="keychain:claude-autodev", oauth_token=token)
    report = guard(world, spec)
    assert not report.ok and any("secret-named variables: CLAUDE_CODE_OAUTH_TOKEN" in f for f in report.failures)
    assert token not in json.dumps(report.to_record())


def test_the_decoy_keychain_item_is_probed_with_its_value(world):
    item = decoys.Decoy("keychain-item", None, decoys.new_marker(), "keychain")
    report = guard(world, build(world, {}), persistent=[*world.planted, item])
    assert report.ok, report.failures
    assert "keychain.decoy.bash" in {check["id"] for check in report.checks}
    report = guard(world, build(world, {"allow": ["keychain.decoy.bash"]}), persistent=[*world.planted, item])
    assert not report.ok  # an allowed lookup prints nothing here, but its exit code is 0


def test_the_selection_session_gets_the_file_tool_subset(world):
    spec = build(world, {}, kind="select")
    report = guard(world, spec)
    assert report.ok and not report.ran_session  # no Bash: static checks only
    report = guard(world, spec, force_session=True)
    assert report.ok, report.failures
    assert {check["tool"] for check in report.checks} == {"Read", "Grep", "Glob"}
    assert report.init["tools"] == sorted(sandbox.READ_TOOLS)


# --- loosened settings abort before any session -----------------------------------------------------

def _rewrite(spec, change):
    settings = json.loads(json.dumps(spec.settings))
    change(settings)
    spec.paths.settings_file.write_text(json.dumps(settings))
    spec.paths.settings_file.chmod(0o600)
    return sandbox.SessionSpec(spec.kind, spec.argv, spec.env, spec.paths, settings, spec.mode)


@pytest.mark.parametrize("change, problem", [
    (lambda s: s["sandbox"].update(failIfUnavailable=False), "failIfUnavailable must be true"),
    (lambda s: s["sandbox"].update(excludedCommands=["*"]), "excludedCommands must not be set"),
    (lambda s: s["sandbox"]["network"].update(allowLocalBinding=True), "allowLocalBinding must not be set"),
    (lambda s: s["sandbox"]["filesystem"]["denyRead"].remove("~/"), "denyRead lacks ~/"),
])
def test_a_loosened_setting_aborts_before_the_probe_session(world, change, problem):
    spec = _rewrite(build(world, {}), change)
    report = guard(world, spec)
    assert not report.ok and not report.ran_session
    assert any(problem in failure for failure in report.failures), report.failures
    assert not (world.base / "seen.json").exists()


def test_an_extra_tool_aborts_before_the_probe_session(world):
    spec = build(world, {})
    argv = list(spec.argv)
    argv[argv.index("--tools") + 1] += ",WebFetch"
    report = guard(world, sandbox.SessionSpec(spec.kind, tuple(argv), spec.env, spec.paths, spec.settings))
    assert not report.ok and not report.ran_session and "spec: the argv differs from the launcher's" in report.failures


@pytest.mark.parametrize("options, problem", [
    ({"managed": lambda: ["/Library/Application Support/ClaudeCode/managed-settings.json"]}, "managed settings present"),
    ({"platform": "linux"}, "macOS-only"),
    ({"persistent": []}, "no decoys are planted"),
])
def test_static_refusals(world, options, problem):
    report = guard(world, build(world, {}), **options)
    assert not report.ok and not report.ran_session
    assert any(problem in failure for failure in report.failures), report.failures


def test_a_hard_link_or_a_secret_in_the_git_config_aborts(world):
    os.link(world.planted[1].path, world.paths.exchange / "hard")
    report = guard(world, build(world, {}))
    assert any("hard links" in failure for failure in report.failures)
    (world.paths.exchange / "hard").unlink()
    (world.paths.home / ".gitconfig").write_text("[http]\n\textraHeader = Authorization: token ghp_"
                                                  + "a" * 36 + "\n")
    report = guard(world, build(world, {}))
    assert any(".gitconfig (re-opened to the sandbox)" in failure for failure in report.failures)
    assert "ghp_" not in json.dumps(report.to_record())


# --- the capture and the session gate -------------------------------------------------------------------

def test_the_capture_is_bounded_and_timed(world):
    spec = build(world, {"oversize": 300_000})
    captured = probes.run_session(spec, "Calls:\n", max_capture=100_000)
    assert captured.truncated and len(captured.stdout) <= 100_000
    spec = build(world, {"sleep": 30})
    captured = probes.run_session(spec, "Calls:\n", timeout=1)
    assert captured.timed_out


def test_start_session_needs_a_passing_report_for_exactly_this_spec(world):
    failing, spec = probe(world, {"allow": ["gh.token.bash"]})
    with pytest.raises(probes.ProbeFailed):
        probes.require(failing)
    with pytest.raises(probes.ProbeFailed):
        probes.start_session(spec, failing, "work")
    passing, spec = probe(world)
    changed = sandbox.SessionSpec(spec.kind, spec.argv, {**spec.env, "LANG": "C"}, spec.paths, spec.settings)
    with pytest.raises(sandbox.SandboxError, match="does not cover this session"):
        probes.start_session(changed, passing, "work")
    static_only = guard(world, spec, force_session=False)
    static_only.ran_session = False
    with pytest.raises(sandbox.SandboxError):
        probes.start_session(spec, static_only, "work")
    process = probes.start_session(spec, probes.require(passing), "Calls:\n", stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
    assert process.wait(timeout=60) == 0


def test_parse_stream_reads_tool_calls_results_and_denials():
    lines = [{"type": "system", "subtype": "init", "tools": ["Read"]},
             {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Read",
                                                            "input": {"file_path": "/x"}}]}},
             {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a",
                                                       "content": [{"type": "text", "text": "denied"}],
                                                       "is_error": True}]}},
             {"type": "result", "permission_denials": [{"tool_name": "Read", "tool_use_id": "a"}]}]
    stream = probes.parse_stream("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
    assert stream.init["tools"] == ["Read"] and stream.uses[0]["name"] == "Read"
    assert stream.results["a"] == {"text": "denied", "is_error": True}
    assert stream.denied == {"a"} and stream.unparsed == 1


# --- the CLI ---------------------------------------------------------------------------------------------

def _ctx(world, scenario, **changes):
    autodev_config.save({**autodev_config.DEFAULTS, "clone": str(world.base / "runner" / "trusted")}, world.state)
    claude = fake_claude(world.base / "bin", scenario)

    def session(ctx, config, run_id):
        layout = sandbox.session_layout(world.base / "sessions", run_id, "probe")
        sandbox.prepare_layout(layout)
        (layout["repository"] / ".git").mkdir(parents=True)
        venv = world.base / "venv-ro"
        venv.mkdir(exist_ok=True)
        return runner.ProbeSession(layout, venv, lambda: shutil.rmtree(layout["base"].parent, ignore_errors=True))

    return runner.Context(installation=world.base / "installation", directory=world.state, claude=claude,
                          home=world.paths.home, platform="darwin", probe_session=session,
                          command_runner=world.commands, managed_sources=lambda: [],
                          agents_dir=world.base / "LaunchAgents", **changes)


def test_the_cli_probe_refuses_without_confirmation(world, capsys):
    ctx = _ctx(world, {})
    assert cli.main(["probe", "--mode", "dry-run"], ctx=ctx) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "confirmation_required" and runner.PROBE_CONFIRMATION in output["message"]
    assert not (world.base / "sessions").exists()


@pytest.mark.parametrize("mode", ["dry-run", "run-now", "scheduled", "resume"])
def test_the_cli_probe_runs_a_guarded_session_in_every_mode(world, capsys, mode):
    ctx = _ctx(world, {})
    code = cli.main(["probe", "--mode", mode, "--kind", "implement", runner.PROBE_CONFIRMATION], ctx=ctx)
    output = json.loads(capsys.readouterr().out)
    assert code == 0, output
    assert output["status"] == "completed" and output["mode"] == mode and output["trigger"] == {"kind": "probe"}
    assert output["probes"][0]["ok"] and output["probes"][0]["ran_session"]
    stored = (records.run_dir(output["run_id"], world.state) / records.RECORD_NAME).read_text()
    assert all(decoy.marker not in stored for decoy in decoys.registered(world.state))
    assert not list((world.base / "sessions").glob("*/probe"))


def test_the_cli_probe_refuses_without_a_login(world, capsys):
    ctx = _ctx(world, {"auth": {"loggedIn": False}, "auth_code": 1})
    assert cli.main(["probe", runner.PROBE_CONFIRMATION], ctx=ctx) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "refused" and output["login_command"].endswith("claude auth login")
    assert "probes" not in output


def test_the_cli_probe_records_a_failure(world, capsys):
    ctx = _ctx(world, {"allow": ["read.token.bash"]})
    assert cli.main(["probe", runner.PROBE_CONFIRMATION], ctx=ctx) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "refused" and "tripwire" in output["reason"]
    assert autodev_config.load(world.state)["enabled"] is False
    assert all(decoy.marker not in json.dumps(output) for decoy in decoys.registered(world.state))
