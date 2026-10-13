"""Sandbox probes (#343) against a fake ``claude`` in a temporary home: pass and fail detection.

The fake (tests/fixtures/fake_claude.py) behaves like a working sandbox by default and is told to
open one hole at a time. Nothing here starts a model, touches the keychain or launchd, or plants
anything in the real home directory; the run's temporary root is a fresh /private/tmp/ad-<hex>.
"""
import dataclasses
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest

from src.schedules import cli, config as autodev_config, decoys, probes, records, runner, sandbox
from src.schedules.lease import schedules_dir
from src.schedules.scan import KnownSecret
from tests.test_schedules_decoys import RawOnlyScanner
from tests.test_schedules_sandbox import RUN_ID, SESSION_PATH, fake_claude, make_paths, temp_root  # noqa: F401

READ_TOOLS = {"Bash", "Read", "Grep", "Glob"}
NONCE = "0123456789abcdef"


class FakeCommands:
    """``launchctl list``/``remove`` and ``security`` answers; everything else runs for real
    (the fake claude's ``auth status``). ``existing`` names the keychain items that exist
    (``internet:<server>`` for internet passwords); a ``-w`` read gives ``secret`` or nothing."""

    def __init__(self, job_exists=False, existing=("gh:github.com",), secret=None):
        self.job_exists = job_exists
        self.existing = set(existing)
        self.secret = secret
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == probes.LAUNCHCTL:
            return subprocess.CompletedProcess(argv, 0 if self.job_exists else 113)
        if argv[0] == "/usr/bin/security":
            if "-w" in argv:
                return subprocess.CompletedProcess(argv, 0 if self.secret else 44, self.secret or "", "")
            name = argv[argv.index("-s") + 1]
            if argv[1] == "find-internet-password":
                name = f"internet:{name}"
            return subprocess.CompletedProcess(argv, 0 if name in self.existing else 44, "", "")
        return subprocess.run(argv, **kwargs)


@pytest.fixture
def world(tmp_path_factory, service_state_dir, temp_root, monkeypatch):  # noqa: F811
    base = tmp_path_factory.mktemp("probe")
    paths = make_paths(base, service_state_dir, temp_root)
    # The runner builds its SessionPaths with the machine's shared temporary roots by default
    # (the owner's $TMPDIR, /private/tmp/claude-<uid>, /private/tmp/claude); the CLI tests use the
    # world's, so no probe test plants, reads or hashes anything there.
    real_paths = sandbox.SessionPaths
    temps = {"owner_temp": paths.owner_temp, "claude_temp": paths.claude_temp, "claude_shared": paths.claude_shared}
    monkeypatch.setattr(sandbox, "SessionPaths", lambda **fields: real_paths(**{**temps, **fields}))
    (paths.home / "Library/Keychains").mkdir(parents=True)
    (paths.repository / ".git/config").write_text("[core]\n\trepositoryformatversion = 0\n")  # no hooks (--template=)
    for directory in ("installation", "trusted.git/hooks", "LaunchAgents", "bin"):
        (base / directory).mkdir(parents=True)
    (base / "trusted.git/config").write_text("[core]\n\tbare = true\n")
    (base / "trusted.git/hooks/pre-push.sample").write_text("#!/bin/sh\n")
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
                           commands=FakeCommands(), folder=planted[1].path.parent)


def build(world, scenario, kind="implement", paths=None, **options):
    paths = paths or world.paths
    scenario = {"seen": str(world.base / "seen.json"), "fallback_tmpdir": str(paths.claude_temp), **scenario}
    claude = fake_claude(world.base / "bin", scenario)
    return sandbox.build(claude, kind, paths, path=SESSION_PATH, run_variables={"AUTODEV_RUN_ID": RUN_ID}, **options)


def guard(world, spec, **options):
    options = {"directory": world.state, "targets": world.targets, "persistent": world.planted, "mode": "dry-run",
               "run_id": RUN_ID, "platform": "darwin", "managed": lambda: [], "command_runner": world.commands,
               "resolver": lambda host: True, **options}
    return probes.guard(spec, **options)


def probe(world, scenario=None, **options):
    spec = build(world, scenario or {})
    return guard(world, spec, **options), spec


def failed(report, text):
    return any(text in failure for failure in report.failures)


def leftovers(world):
    found = []
    for directory in (world.paths.exchange, world.paths.repository, world.paths.state_copy, world.paths.tmp_root):
        found += [path.name for path in directory.rglob("*") if "probe-" in path.name]
    return found


@pytest.fixture
def fixed_nonce(monkeypatch):
    real = probes.secrets.token_hex
    monkeypatch.setattr(probes.secrets, "token_hex", lambda size: NONCE if size == 8 else real(size))
    return NONCE


# --- a working sandbox ------------------------------------------------------------------------------

def test_a_working_sandbox_passes_every_probe(world):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, world.state)
    report, spec = probe(world)
    assert report.ok, report.failures
    assert report.ran_session and report.digest == spec.digest() and not report.tripped
    assert all(check["ok"] for check in report.checks)
    assert report.init == {"tools": sorted(sandbox.STAGE_TOOLS), "mcp_servers": 0, "permission_mode": "dontAsk"}
    assert report.observed["tmpdir_inside_temp_root"] is True and report.observed["fds"] <= probes.MAX_FDS
    assert report.observed["keychain"]["probed"] == ["gh:github.com"]
    assert set(report.observed["exit_codes"]) == {"git.config-global.script", "prefs.read.script"}
    assert {"CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID"} <= set(report.observed["env_names"])
    groups = {check["group"] for check in report.checks}
    assert {"C0", "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C13", "C15", "C18"} <= groups
    by_tool = lambda prefix: {check["tool"] for check in report.checks if check["id"].startswith(prefix)}
    assert by_tool("read.credentials.") == READ_TOOLS and by_tool("read.owner-temp-decoy.") == READ_TOOLS
    assert by_tool("write.") == {"Bash", "Write", "Edit"}
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
    assert sorted(path.name for path in world.folder.iterdir()) == sorted(d.path.name for d in world.planted[1:])


def test_recognised_forms_refused_by_the_permission_layer_count_as_denied(world):
    report, _ = probe(world, {"refuse_inline": True})
    assert report.ok, report.failures
    assert {check["outcome"] for check in report.checks if check["id"].endswith(".bash")
            and not check["id"].startswith("control.")} == {"denied"}


def test_the_record_holds_verdicts_but_no_marker(world):
    report, _ = probe(world, {"allow": ["read.credentials.bash"]})
    record = probes.record_report(RUN_ID, world.state, report, world.planted)
    text = (records.run_dir(RUN_ID, world.state) / records.RECORD_NAME).read_text()
    assert record["probes"][0]["ok"] is False and record["probes"][0]["tripped"] is True
    for decoy in world.planted:
        assert decoy.marker not in text
    assert "decoy credentials" in text  # the label, never the value


# --- the plan: every probe, pinned (decision 17) ------------------------------------------------------

def _plan_calls(world, kind, fixed_nonce, paths=None):
    spec = build(world, {}, kind=kind, paths=paths)
    run = probes.ProbeRun(fixed_nonce, list(world.planted))
    try:
        probes._prepare(spec, run, world.targets, None)
        run.keychain = probes.keychain_inventory(world.targets, run.keychain_decoy, world.commands)
        return probes.plan(spec, run, world.targets), run
    finally:
        probes._cleanup(spec, run)


def _plan_ids(world, kind, fixed_nonce, paths=None):
    calls, _ = _plan_calls(world, kind, fixed_nonce, paths)
    return sorted((check.id, call.tool) for call in calls for check in call.checks)


def _body(calls, check_id):
    """The shell body of a Bash sub-probe, from its inline command or its script."""
    for call in calls:
        if call.tool == "Bash" and any(check.id == check_id for check in call.checks):
            text = call.script[1] if call.script else call.input["command"]
            suffix = f"; echo {probes.MARK} {check_id} rc=$?"
            return next(line[:-len(suffix)] for line in text.splitlines() if line.endswith(suffix))
    raise KeyError(check_id)


IMPLEMENT_PLAN = sorted([
    ("control.exchange.bash", "Bash"), ("control.tmpdir.bash", "Bash"),
    ("control.exchange.write", "Write"), ("control.repo.write", "Write"), ("control.exchange.read", "Read"),
    ("control.exchange.grep", "Grep"), ("control.exchange.glob", "Glob"), ("control.repo.read", "Read"),
    ("control.repo.edit", "Edit"), ("control.sandbox-loaded.script", "Bash"),
    *[(f"read.{label}.{suffix}", "Bash") for label in ("ssh-key", "credentials", "token", "owner-temp-decoy",
                                                         "claude-temp-decoy") for suffix in ("bash", "script")],
    *[(f"read.{label}.{tool.lower()}", tool) for label in ("ssh-key", "credentials", "token", "owner-temp-decoy",
                                                             "claude-temp-decoy") for tool in ("Read", "Grep", "Glob")],
    *[(f"list.{name}.{suffix}", "Bash") for name in ("ssh-dir", "keychains-dir", "config-dir", "home", "owner-temp",
                                                     "claude-temp", "claude-shared", "state-dir")
      for suffix in ("bash", "script")],
    *[(f"list.{name}.glob", "Glob") for name in ("ssh-dir", "keychains-dir", "config-dir", "home", "owner-temp",
                                                 "claude-temp", "claude-shared", "state-dir")],
    *[(f"list.{name}.grep", "Grep") for name in ("ssh-dir", "keychains-dir", "config-dir", "owner-temp",
                                                 "claude-temp", "claude-shared", "state-dir")],
    ("read.decoy-registry.bash", "Bash"), ("read.decoy-registry.script", "Bash"), ("read.decoy-registry.glob", "Glob"),
    ("git.config-global.script", "Bash"), ("prefs.read.script", "Bash"),
    ("keychain.gh-github-com.script", "Bash"),
    ("gh.token.script", "Bash"), ("network.external.script", "Bash"), ("network.external-noproxy.script", "Bash"),
    ("network.metadata.script", "Bash"), ("network.dns.script", "Bash"), ("network.udp.script", "Bash"),
    ("network.listen.script", "Bash"), ("network.local-listener.script", "Bash"),
    ("socket.foreign.script", "Bash"), ("launchd.submit.script", "Bash"), ("apple-events.osascript.script", "Bash"),
    ("launch-services.open.script", "Bash"),
    ("bypass.sandbox-loaded.script", "Bash"), ("bypass.read.script", "Bash"),
    *[(f"write.{name}.{suffix}", tool) for name in (
        "home", "runs", "installation", "schedules", "state-dir", "venv", "state-copy", "config-dir", "gh-config",
        "settings-dir", "owner-temp", "claude-temp", "claude-shared", "trusted", "trusted-hooks", "plist-dir",
        "repo-git-hooks", "repo-claude-settings")
      for suffix, tool in (("bash", "Bash"), ("script", "Bash"), ("write", "Write"))],
    # The one-off repository's .git/config is a file: appended to, and read, then written.
    ("write.repo-git-config.bash", "Bash"), ("write.repo-git-config.script", "Bash"),
    ("read.repo-git-config.read", "Read"), ("write.repo-git-config.write", "Write"),
    *[(f"append.{name}.{suffix}", "Bash") for name in ("config-file", "plist", "settings-file")
      for suffix in ("bash", "script")],
    *[(f"link.{name}.{suffix}", "Bash") for name in ("symlink-read", "hardlink", "dir-symlink")
      for suffix in ("bash", "script")],
    ("read.state-copy.read", "Read"), ("write.state-copy.edit", "Edit"),
    ("link.dir-symlink.write", "Write"), ("link.symlink-read.read", "Read"),
    ("observe.env.script", "Bash"),
])

SELECT_PLAN = sorted([
    ("control.exchange.read", "Read"), ("control.exchange.grep", "Grep"), ("control.exchange.glob", "Glob"),
    *[(f"read.{label}.{tool.lower()}", tool) for label in ("ssh-key", "credentials", "token", "owner-temp-decoy",
                                                             "claude-temp-decoy") for tool in ("Read", "Grep", "Glob")],
    *[(f"list.{name}.glob", "Glob") for name in ("ssh-dir", "keychains-dir", "config-dir", "home", "owner-temp",
                                                 "claude-temp", "claude-shared", "state-dir")],
    *[(f"list.{name}.grep", "Grep") for name in ("ssh-dir", "keychains-dir", "config-dir", "owner-temp",
                                                 "claude-temp", "claude-shared", "state-dir")],
    ("read.decoy-registry.glob", "Glob"), ("link.symlink-read.read", "Read"),
])


@pytest.mark.parametrize("kind, expected", [("implement", IMPLEMENT_PLAN), ("submit", IMPLEMENT_PLAN),
                                            ("select", SELECT_PLAN)])
def test_the_plan_holds_exactly_the_pinned_probes(world, fixed_nonce, kind, expected):
    assert _plan_ids(world, kind, fixed_nonce) == expected


def test_the_plan_refuses_duplicate_check_ids():
    check = probes.Check("read.x.bash", "C1", probes.DENIED)
    calls = [probes.Call("Bash", {"command": "a"}, (check,)), probes.Call("Bash", {"command": "b"}, (check,))]
    with pytest.raises(ValueError, match="same ID: read.x.bash"):
        probes._require_unique(calls)


def test_a_session_with_the_daemon_port_and_extra_sockets_probes_them(world, fixed_nonce):
    targets = dataclasses.replace(world.targets, daemon_port=4567, extra_sockets=(Path("/var/run/x.sock"),))
    world.targets = targets
    ids = {check for check, _ in _plan_ids(world, "implement", fixed_nonce)}
    assert {"network.daemon.script", "socket.extra-0.script"} <= ids


# --- holes, one at a time -----------------------------------------------------------------------------

@pytest.mark.parametrize("scenario, failing", [
    (lambda w: {"allow": ["read.credentials.bash"]}, "read.credentials.bash"),
    (lambda w: {"allow": ["read.credentials.script"]}, "read.credentials.script"),
    (lambda w: {"allow_paths": [w.folder.name]}, "read.credentials.read"),
    (lambda w: {"allow_calls": [["Grep", w.planted[2].path.name]]}, "read.token.grep"),
    (lambda w: {"allow_calls": [["Glob", w.planted[0].path.name]]}, "read.ssh-key.glob"),
    (lambda w: {"honour_bypass": True}, "bypass.read.script"),
    (lambda w: {"allow": ["link.symlink-read.bash"]}, "link.symlink-read.bash"),
    (lambda w: {"allow": ["link.symlink-read.script"]}, "link.symlink-read.script"),
    (lambda w: {"allow_paths": ["-link"]}, "link.symlink-read.read"),
    (lambda w: {"allow": ["read.owner-temp-decoy.bash"]}, "read.owner-temp-decoy.bash"),
    (lambda w: {"allow": ["read.claude-temp-decoy.script"]}, "read.claude-temp-decoy.script"),
])
def test_a_readable_decoy_fails_and_turns_the_schedule_off(world, scenario, failing):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, world.state)
    report, _ = probe(world, scenario(world))
    assert not report.ok and report.tripped
    assert failed(report, failing), report.failures
    assert autodev_config.load(world.state)["enabled"] is False
    assert json.loads((schedules_dir(world.state) / decoys.TRIPWIRE_NAME).read_text())["count"] == 1


def test_a_marker_in_the_models_text_is_a_leak(world):
    report, _ = probe(world, {"say": "here it is: " + world.planted[2].marker})
    assert not report.ok and report.tripped
    assert any(hit["label"] == "decoy token" and hit["kind"] == "known" for hit in report.hits)
    assert all(set(hit) == {"stream", "kind", "label", "start", "end"} for hit in report.hits)


@pytest.mark.parametrize("probe_id, created", [
    ("write.home.bash", lambda w, n: w.paths.home / f"probe-{n}-bash"),
    ("write.trusted-hooks.script", lambda w, n: w.base / "trusted.git/hooks" / f"probe-{n}-script"),
    ("write.claude-shared.script", lambda w, n: w.paths.claude_shared / f"probe-{n}-script"),
    ("write.owner-temp.bash", lambda w, n: w.paths.owner_temp / f"probe-{n}-bash"),
    ("control.sandbox-loaded.script", lambda w, n: w.folder / f"probe-{n}-script"),
    ("bypass.sandbox-loaded.script", lambda w, n: w.folder / f"probe-{n}-bypass"),
    ("link.hardlink.bash", lambda w, n: w.paths.exchange / f"probe-{n}-hard-bash"),
    ("link.dir-symlink.script", lambda w, n: w.folder / f"probe-{n}-via-link-script"),
])
def test_a_bash_write_that_succeeded_fails_and_is_removed(world, fixed_nonce, probe_id, created):
    report, _ = probe(world, {"allow": [probe_id]})
    assert not report.ok
    assert any(failure.startswith(f"{probe_id}: ") for failure in report.failures), report.failures
    assert not created(world, fixed_nonce).exists()


WRITE_TARGETS = ["home", "runs", "installation", "schedules", "state-dir", "venv", "state-copy", "config-dir",
                 "gh-config", "settings-dir", "owner-temp", "claude-temp", "claude-shared", "trusted", "trusted-hooks",
                 "plist-dir"]


def _write_dir(world, name):
    paths, targets = world.paths, world.targets
    return {"home": paths.home, "runs": targets.runs, "installation": targets.installation,
            "schedules": targets.schedules, "state-dir": paths.state_dir, "venv": paths.venv,
            "state-copy": paths.state_copy, "config-dir": paths.config_dir, "gh-config": paths.gh_config,
            "settings-dir": paths.settings_file.parent, "owner-temp": paths.owner_temp,
            "claude-temp": paths.claude_temp, "claude-shared": paths.claude_shared, "trusted": targets.trusted,
            "trusted-hooks": targets.trusted / "hooks", "plist-dir": targets.plist.parent}[name]


@pytest.mark.parametrize("name", WRITE_TARGETS)
def test_every_write_tool_target_that_succeeded_fails(world, fixed_nonce, name):
    target = _write_dir(world, name) / f"probe-{fixed_nonce}-write"
    report, _ = probe(world, {"allow_calls": [["Write", str(target)]]})
    assert not report.ok
    assert failed(report, f"write.{name}.write: {target} was created"), report.failures
    entry = next(check for check in report.checks if check["id"] == f"write.{name}.write")
    assert entry["outcome"] == "allowed" and entry["ok"] is False
    assert not target.exists()


def test_a_write_through_a_directory_link_that_succeeded_fails(world, fixed_nonce):
    report, _ = probe(world, {"allow_paths": ["-dirlink/"]})
    assert not report.ok and failed(report, "link.dir-symlink.write: ")
    assert not (world.folder / f"probe-{fixed_nonce}-via-link-write").exists()


def test_an_edit_of_the_state_copy_that_succeeded_fails(world, fixed_nonce):
    report, _ = probe(world, {"allow_calls": [["Edit", f"probe-{fixed_nonce}-edit.txt"]]})
    assert not report.ok and failed(report, "write.state-copy.edit: ") and failed(report, "changed")


@pytest.mark.parametrize("name", ["config-file", "plist", "settings-file"])
def test_an_append_that_succeeded_fails(world, name):
    report, _ = probe(world, {"allow": [f"append.{name}.script"]})
    assert not report.ok and failed(report, f"append.{name}.script: expected denied, got allowed")


@pytest.mark.parametrize("scenario, failing", [
    ({"deny": ["control.exchange.bash"]}, "control.exchange.bash"),
    ({"deny": ["control.tmpdir.bash"]}, "control.tmpdir.bash"),
    ({"deny_calls": [["Write", "/exchange/probe-"]]}, "control.exchange.write"),
    ({"deny_calls": [["Write", "/repo/.autodev-probe-"]]}, "control.repo.write"),
    ({"deny_calls": [["Read", "-seed.txt"]]}, "control.exchange.read"),
    ({"deny_calls": [["Grep", "/exchange"]]}, "control.exchange.grep"),
    ({"deny_calls": [["Glob", "-seed*"]]}, "control.exchange.glob"),
    ({"deny_calls": [["Read", "/repo/.autodev-probe-"]]}, "control.repo.read"),
    ({"deny_calls": [["Edit", "/repo/.autodev-probe-"]]}, "control.repo.edit"),
    ({"error_paths": ["/exchange/probe-"]}, "control.exchange.write"),
])
def test_a_failed_positive_control_aborts(world, scenario, failing):
    report, _ = probe(world, scenario)
    assert not report.ok and not report.tripped
    assert any(failure.startswith(failing) for failure in report.failures), report.failures


@pytest.mark.parametrize("scenario, failure", [
    ({"skip": [0]}, "got missing"),
    ({"skip": [5, 9]}, "got missing"),
    ({"extra_call": {"tool": "Bash", "input": {"command": "ls ~"}}}, "an unexpected Bash call"),
    ({"init": {"tools": list(sandbox.STAGE_TOOLS) + ["WebFetch"]}}, "init tools differ from the session's (extra: WebFetch)"),
    ({"init": {"mcp_servers": [{"name": "claude.ai Gmail", "status": "connected"}]}}, "init lists 1 MCP server(s)"),
    ({"init": {"permissionMode": "default"}}, "init permission mode is default, not dontAsk"),
    ({"init": {"plugins": [{"name": "x", "path": "/x"}]}}, "plugins that sandbox.allowed_plugins does not allow: x"),
    ({"garbage": True}, "line(s) of the stream are not JSON events"),
    ({"allow": ["keychain.gh-github-com.script"]}, "keychain.gh-github-com.script"),
    ({"allow": ["gh.token.script"]}, "gh.token.script"),
    ({"allow": ["network.external.script"]}, "network.external.script"),
    ({"allow": ["network.dns.script"]}, "network.dns.script"),
    ({"allow": ["network.udp.script"]}, "network.udp.script"),
    ({"allow": ["network.metadata.script"]}, "network.metadata.script"),
    ({"allow": ["network.listen.script"]}, "network.listen.script"),
    ({"allow": ["apple-events.osascript.script"]}, "apple-events.osascript.script"),
    ({"allow": ["launch-services.open.script"]}, "launch-services.open.script"),
    ({"allow": ["read.git-credentials.script"]}, "read.git-credentials.script"),
    ({"allow": ["read.decoy-registry.bash"]}, "read.decoy-registry.bash"),
    ({"allow": ["list.claude-shared.script"]}, "list.claude-shared.script"),
    ({"allow": ["list.state-dir.bash"]}, "list.state-dir.bash"),
    ({"allow_paths": [".ssh"]}, "list.ssh-dir.glob"),
    ({"allow_calls": [["Grep", "/.ssh"]]}, "list.ssh-dir.grep"),
    ({"allow_calls": [["Grep", "owner-tmp"]]}, "list.owner-temp.grep"),
])
def test_a_mismatch_aborts(world, scenario, failure):
    # A missing file also gives a non-zero exit code; the owner's may exist, so the probe must see one.
    (world.paths.home / ".git-credentials").write_text("https://owner:not-a-secret@example.com\n")
    report, _ = probe(world, scenario)
    assert not report.ok
    assert failed(report, failure), report.failures


# --- denials need proof (decision 1) -----------------------------------------------------------------

def test_a_denial_forged_with_an_invalid_extra_parameter_fails(world):
    """F1: the model adds an invalid parameter to the decoy Read and Grep, the CLI rejects the input
    before any permission check, and the decoy stays open; this must never pass as denied."""
    name = world.planted[1].path.name
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, world.state)
    open_hole = {"allow_paths": [name]}
    report, _ = probe(world, {**open_hole, "forge": [["Read", name], ["Grep", name]]})
    assert not report.ok
    assert failed(report, "read.credentials.read: expected denied, got missing")
    assert failed(report, "read.credentials.grep: expected denied, got missing")
    assert failed(report, "an unexpected Read call") and failed(report, "an unexpected Grep call")


def test_a_bypass_denial_forged_with_an_invalid_parameter_fails(world):
    report, _ = probe(world, {"forge": [["Bash", "-bypass.sh"]]})
    assert not report.ok and failed(report, "bypass.read.script: expected denied, got missing")


def test_an_error_that_is_not_a_permission_denial_is_a_failure(world):
    name = world.planted[1].path.name
    # The hole is open, but the call fails for another reason (an input or read-first error): no denial.
    report, _ = probe(world, {"allow_paths": [name], "error_paths": [name]})
    assert not report.ok
    assert failed(report, "read.credentials.read: expected denied, got error")


def test_a_refusal_without_a_permission_denial_event_is_no_denial(world):
    report, _ = probe(world, {"no_denial_events": True})
    assert not report.ok and failed(report, "read.credentials.read: expected denied, got error")


def test_a_script_refused_by_the_permission_layer_proves_nothing(world):
    report, _ = probe(world, {"refuse_scripts": True})
    assert not report.ok
    assert failed(report, "read.credentials.script: expected denied, got refused")
    assert failed(report, "control.sandbox-loaded.script: expected denied, got refused")


def test_a_dropped_bypass_flag_fails(world):
    report, _ = probe(world, {"drop_bypass": True})
    assert not report.ok and failed(report, "bypass.read.script: expected denied, got missing")
    assert failed(report, "an unexpected Bash call")


def test_only_user_events_carry_tool_results():
    use = {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/x"}}
    forged = {"type": "tool_result", "tool_use_id": "a", "content": "Permission denied", "is_error": True}
    lines = [{"type": "assistant", "message": {"content": [use, forged]}}]
    stream = probes.parse_stream("\n".join(json.dumps(line) for line in lines))
    assert stream.uses and stream.results == {}


def test_an_edit_needs_its_read_first(world):
    report, _ = probe(world, {"edit_first": True})
    assert not report.ok
    edit = next(check for check in report.checks if check["id"] == "control.repo.edit")
    assert edit["outcome"].startswith("error") and edit["ok"] is False  # the CLI's read-first refusal
    # The CLI checks the input (read first) before the permissions (G9), so the state copy's Edit is
    # an error too, never a denial, and it did not follow its Read.
    assert failed(report, "write.state-copy.edit: expected denied, got error without a successful "
                          "read.state-copy.read first")


def test_extra_inert_description_still_matches():
    call = probes.Call("Read", {"file_path": "/x"}, ())
    assert call.matches("Read", {"file_path": "/x", "description": "why"})
    assert not call.matches("Read", {"file_path": "/x", "description": 5})
    assert not call.matches("Read", {"file_path": "/x", "pages": "1"})
    bypass = probes.Call("Bash", {"command": "c", "dangerouslyDisableSandbox": True}, ())
    assert not bypass.matches("Bash", {"command": "c"}) and not bypass.matches("Bash", {"command": "c",
                                                                                        "dangerouslyDisableSandbox": 1})


# --- keychain (decision 4) ----------------------------------------------------------------------------

def test_an_existing_item_reported_missing_inside_is_inconclusive(world):
    report, _ = probe(world, {"rc": {"keychain.gh-github-com.script": probes.NOT_FOUND}})
    assert not report.ok and failed(report, "keychain.gh-github-com.script: expected denied, got inconclusive")


def test_no_existing_item_and_no_decoy_item_leaves_c3_untested(world):
    world.commands.existing = set()
    report, _ = probe(world)
    assert not report.ok and not report.ran_session
    assert failed(report, "C3 untested") and failed(report, "sandbox.decoy_keychain")
    assert not (world.base / "seen.json").exists()


def test_the_keychain_inventory_runs_outside_the_sandbox_with_no_output(world):
    item = decoys.Decoy("keychain-item", None, decoys.new_marker(), "keychain", "api-token-0123456789ab", "user-01234567")
    targets = dataclasses.replace(world.targets, keychain_items=("agents-autodev-github", "gh:github.com"))
    found = probes.keychain_inventory(targets, item, world.commands)
    assert found == {"agents-autodev-github": False, "gh:github.com": True, "internet:github.com": False,
                     "decoy": False}
    assert ["/usr/bin/security", "find-internet-password", "-s", "github.com"] in world.commands.calls
    assert not any("-w" in call for call in world.commands.calls)


def test_the_internet_item_and_git_credential_helper_are_probed_when_they_exist(world):
    world.commands.existing.add("internet:github.com")
    report, _ = probe(world)
    assert report.ok, report.failures
    ids = {check["id"] for check in report.checks}
    assert {"keychain.internet-github-com.script", "keychain.git-credential-github-com.script"} <= ids
    report, _ = probe(world, {"allow": ["keychain.git-credential-github-com.script"]})
    assert not report.ok


def test_the_decoy_keychain_item_is_probed_with_its_value(world):
    item = decoys.Decoy("keychain-item", None, decoys.new_marker(), "keychain", "api-token-0123456789ab", "user-01234567")
    world.commands.existing = {item.service}
    report = guard(world, build(world, {}), persistent=[*world.planted, item])
    assert report.ok, report.failures
    assert "keychain.decoy.script" in {check["id"] for check in report.checks}
    report = guard(world, build(world, {"allow": ["keychain.decoy.script"]}), persistent=[*world.planted, item])
    assert not report.ok  # an allowed lookup prints nothing here, but its exit code is 0


# --- network and IPC (decision 11) --------------------------------------------------------------------

def test_a_resolved_name_without_a_connection_fails_the_noproxy_probe(world):
    report, _ = probe(world, {"rc": {"network.external-noproxy.script": 7}})
    assert not report.ok and failed(report, "network.external-noproxy.script: expected denied, got inconclusive")


def test_a_listen_probe_that_failed_for_another_reason_is_inconclusive(world):
    report, _ = probe(world, {"rc": {"network.listen.script": 127}})
    assert not report.ok and failed(report, "network.listen.script: expected denied, got inconclusive")


def test_an_offline_runner_leaves_the_network_untested(world):
    report, _ = probe(world, resolver=lambda host: False)
    assert not report.ok and not report.ran_session and failed(report, "C5 untested")


def test_the_daemon_port_must_be_unreachable(world):
    daemon = probes.Listener.tcp()
    try:
        world.targets = dataclasses.replace(world.targets, daemon_port=daemon.port)
        report, _ = probe(world)
        assert report.ok, report.failures
        assert daemon.accepted == 0
        report, _ = probe(world, {"allow": ["network.daemon.script"]})
        assert not report.ok and failed(report, "network.daemon.script: expected denied, got allowed")
        assert daemon.accepted == 1
    finally:
        daemon.close()


def test_a_connection_to_a_decoy_listener_fails(world):
    report, _ = probe(world, {"allow": ["network.local-listener.script", "socket.foreign.script"]})
    assert not report.ok
    assert failed(report, "network.local-listener.script: the decoy listener accepted 1 connection")
    assert failed(report, "socket.foreign.script: the decoy listener accepted")


def test_a_launchd_job_from_the_probe_fails_and_is_removed(world):
    world.commands.job_exists = True
    report, _ = probe(world)
    assert not report.ok and "launchd.submit.script: the probe's launchd job exists" in report.failures
    assert [call[1] for call in world.commands.calls if call[0] == probes.LAUNCHCTL] == ["list", "remove"]


def test_the_runner_socket_must_be_reachable(world):
    directory = Path(tempfile.mkdtemp(prefix="sbx-"))
    try:
        listener = probes.Listener.unix(directory / "c.sock")
        paths = dataclasses.replace(world.paths, socket=directory / "c.sock")
        report = guard(world, build(world, {}, paths=paths))
        assert report.ok, report.failures
        assert listener.accepted == 1
        assert "write.socket-dir.script" in {check["id"] for check in report.checks}
        listener.close()
        (directory / "c.sock").unlink()
        report = guard(world, build(world, {}, paths=paths))
        assert not report.ok and any(f.startswith("control.socket.bash") for f in report.failures)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_the_trusted_repositorys_hooks_must_stay_unchanged(world, monkeypatch):
    hook = world.base / "trusted.git/hooks/pre-push.sample"
    real = probes.run_session

    def tamper(spec, text, **options):
        captured = real(spec, text, **options)
        hook.write_text("#!/bin/sh\ncurl evil\n")
        return captured

    monkeypatch.setattr(probes, "run_session", tamper)
    report, _ = probe(world)
    assert not report.ok and failed(report, "C12: the trusted repository's config, HEAD or hooks changed")


# --- what Bash sees (C13-C15; decision 2) -------------------------------------------------------------

def test_the_keychain_mode_hides_the_token_from_bash(world):
    token = "sk-ant-oat01-" + "Q" * 40
    spec = build(world, {}, mode="keychain:claude-autodev", oauth_token=token)
    report = guard(world, spec, known=[KnownSecret("oauth token", token)])
    assert report.ok, report.failures
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in report.observed["env_names"]
    spec = build(world, {"leak_oauth_env": True}, mode="keychain:claude-autodev", oauth_token=token)
    report = guard(world, spec)
    assert not report.ok and failed(report, "secret-named variables: CLAUDE_CODE_OAUTH_TOKEN")
    assert token not in json.dumps(report.to_record())


def test_the_oauth_token_is_a_known_secret_of_the_scan(world):
    token = "a" * 20 + "B" * 20 + "1" * 8  # no token shape: only the known value finds it
    spec = build(world, {"say": f"the token is {token}"}, mode="keychain:claude-autodev", oauth_token=token)
    assert guard(world, spec).ok is True  # unknown, it is no hit
    report = guard(world, spec, known=[KnownSecret("oauth token", token)])
    assert not report.ok and any(hit["label"] == "oauth token" for hit in report.hits)


@pytest.mark.parametrize("env, problem", [
    ({"GITHUB_TOKEN": "x"}, "secret-named variables: GITHUB_TOKEN"),
    ({"SOME_SETTING": "1"}, "outside the session allowlist: SOME_SETTING"),
])
def test_bash_variables_are_checked_against_the_allowlist(world, env, problem):
    report, _ = probe(world, {"extra_env": env})
    assert not report.ok and failed(report, problem)


def test_too_many_descriptors_fail(world):
    report, _ = probe(world, {"extra_fds": probes.MAX_FDS + 4})
    assert not report.ok and failed(report, "open descriptors (at most 16)")


def _long_root(tmp_path):
    base = tmp_path / ("x" * 40)
    base.mkdir()
    return sandbox.make_temp_root(base)


def test_an_over_long_temporary_root_fails_before_the_session(world, tmp_path):
    long_root = _long_root(tmp_path)
    paths = dataclasses.replace(world.paths, tmp_root=long_root)
    with pytest.raises(sandbox.SandboxError, match="temporary root is too long"):
        build(world, {}, paths=paths)
    spec = dataclasses.replace(build(world, {}), paths=paths)
    report = guard(world, spec)
    assert not report.ok and not report.ran_session and failed(report, "temporary root is too long")


def test_the_shared_fallback_tmpdir_fails_the_probe(world, tmp_path, monkeypatch):
    """The fake models the CLI: an override longer than 44 bytes gives Bash the shared fallback."""
    monkeypatch.setattr(sandbox, "TMP_PATH_LIMIT", 4096)  # let the static check pass to reach the probe
    long_root = _long_root(tmp_path)
    paths = dataclasses.replace(world.paths, tmp_root=long_root)
    report = guard(world, build(world, {}, paths=paths))
    assert not report.ok and report.ran_session
    assert failed(report, "$TMPDIR does not resolve inside the run's temporary root")
    assert report.observed["tmpdir_inside_temp_root"] is False


# --- names the stream chose (decision 7) --------------------------------------------------------------

@pytest.mark.parametrize("name", ["ghp_" + "a" * 36, "Read Write", "x" * 65])
def test_an_unexpected_calls_name_never_reaches_the_record_unchecked(world, name):
    report, _ = probe(world, {"extra_call": {"tool": name, "input": {}}})
    assert not report.ok and failed(report, "an unexpected <invalid> call")
    assert name not in json.dumps(report.to_record())


def test_init_names_are_masked(world):
    secret = "ghp_" + "b" * 36
    report, _ = probe(world, {"init": {"tools": list(sandbox.STAGE_TOOLS) + [secret],
                                       "plugins": [{"name": secret}], "permissionMode": secret}})
    assert not report.ok and secret not in json.dumps(report.to_record())


def test_an_allowed_plugin_passes(world):
    report, _ = probe(world, {"init": {"plugins": [{"name": "builtin-x", "path": "/x"}]}},
                      allowed_plugins=["builtin-x"])
    assert report.ok, report.failures


# --- loosened settings and static checks abort before any session --------------------------------------

def _rewrite(spec, change):
    settings = json.loads(json.dumps(spec.settings))
    change(settings)
    spec.paths.settings_file.write_text(json.dumps(settings))
    spec.paths.settings_file.chmod(0o600)
    return dataclasses.replace(spec, settings=settings)


@pytest.mark.parametrize("change, problem", [
    (lambda s: s["sandbox"].update(failIfUnavailable=False), "failIfUnavailable must be true"),
    (lambda s: s["sandbox"].update(excludedCommands=["*"]), "excludedCommands must not be set"),
    (lambda s: s["sandbox"]["network"].update(allowLocalBinding=True), "allowLocalBinding must not be set"),
    (lambda s: s["sandbox"]["filesystem"]["denyRead"].remove("~/"), "denyRead lacks ~/"),
    (lambda s: s["sandbox"]["filesystem"]["denyWrite"].clear(), "denyWrite lacks"),
])
def test_a_loosened_setting_aborts_before_the_probe_session(world, change, problem):
    spec = _rewrite(build(world, {}), change)
    report = guard(world, spec)
    assert not report.ok and not report.ran_session
    assert failed(report, problem), report.failures
    assert not (world.base / "seen.json").exists()


def test_an_extra_tool_aborts_before_the_probe_session(world):
    spec = build(world, {})
    argv = list(spec.argv)
    argv[argv.index("--tools") + 1] += ",WebFetch"
    report = guard(world, dataclasses.replace(spec, argv=tuple(argv)))
    assert not report.ok and not report.ran_session and "spec: the argv differs from the launcher's" in report.failures


@pytest.mark.parametrize("options, problem", [
    ({"managed": lambda: ["/Library/Application Support/ClaudeCode/managed-settings.json"]}, "managed settings present"),
    ({"platform": "linux"}, "macOS-only"),
    ({"persistent": []}, "no decoys are planted"),
])
def test_static_refusals(world, options, problem):
    report = guard(world, build(world, {}), **options)
    assert not report.ok and not report.ran_session
    assert failed(report, problem), report.failures


def test_a_decoy_self_test_miss_aborts_before_the_session(world, monkeypatch):
    """C19 at guard level: a scan that finds the markers only raw misses every encoded form."""
    monkeypatch.setattr(decoys, "Scanner", RawOnlyScanner)
    missed = decoys.self_test(world.planted)
    assert {"credentials: base64", "token: hex", "ssh-key: split"} <= set(missed)
    assert not any(item.endswith(": raw") for item in missed)
    report = guard(world, build(world, {}))
    assert not report.ok and not report.ran_session
    assert failed(report, "decoy self-test missed credentials: base64")


TOKEN_LINE = "[http]\n\textraHeader = Authorization: token ghp_" + "a" * 36 + "\n"


@pytest.mark.parametrize("layout", [
    lambda home: (home / ".gitconfig").write_text(TOKEN_LINE),
    lambda home: (_symlinked(home, TOKEN_LINE)),
    lambda home: ((home / "inc.cfg").write_text(TOKEN_LINE), (home / ".gitconfig").write_text(
        "[include]\n\tpath = ~/inc.cfg\n")),
    lambda home: ((home / "work").mkdir(), (home / "work/conf").write_text(TOKEN_LINE),
                  (home / ".gitconfig").write_text('[includeIf "gitdir:~/work/"]\n\tpath = work/conf ; a comment\n')),
    lambda home: ((home / "ignore-me").write_text("Zq8pL2mX9vB4nR7tK1wY5cH3jG6dS0gNuQoIiPrMsWzTyVh\n"),
                  (home / ".gitconfig").write_text('[core]\n\texcludesFile = "~/ignore-me"\n')),
])
def test_a_secret_in_the_reopened_git_configuration_aborts(world, layout):
    layout(world.paths.home)
    report = guard(world, build(world, {}))
    assert not report.ok and not report.ran_session
    assert failed(report, "(re-opened to the sandbox) holds"), report.failures
    assert "ghp_" not in json.dumps(report.to_record())


def _symlinked(home, text):
    target = home.parent / "nix-store-config"
    target.write_text(text)
    (home / ".config/git").mkdir(parents=True)
    (home / ".config/git/config").symlink_to(target)


def test_an_unreadable_reopened_git_file_is_a_problem(world):
    (world.paths.home / "inc").mkdir()
    (world.paths.home / ".gitconfig").write_text("[include]\n\tpath = ~/inc\n")
    report = guard(world, build(world, {}))
    assert failed(report, "cannot be checked")
    (world.paths.home / ".gitconfig").write_text("[include]\n\tpath = ~/absent\n[user]\n\tname = Owner\n")
    assert guard(world, build(world, {})).ok


def test_a_hard_link_in_the_writable_trees_aborts(world):
    os.link(world.planted[1].path, world.paths.exchange / "hard")
    report = guard(world, build(world, {}))
    assert failed(report, "hard links")


# --- the capture, the process group and the session gate ------------------------------------------------

def test_the_capture_is_bounded_and_timed(world):
    spec = build(world, {"oversize": 300_000})
    captured = probes.run_session(spec, "Calls:\n", max_capture=100_000)
    assert captured.truncated and len(captured.stdout) <= 100_000
    spec = build(world, {"sleep": 30})
    captured = probes.run_session(spec, "Calls:\n", timeout=1)
    assert captured.timed_out


def test_an_oversized_capture_fails_the_guard(world):
    report, _ = probe(world, {"oversize": 300_000}, max_capture=100_000)
    assert not report.ok and report.truncated and failed(report, "output exceeded")


def test_the_probe_sessions_children_are_killed_when_it_ends(world):
    pid_file = world.base / "child.pid"
    report, _ = probe(world, {"spawn_child": str(pid_file)})
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail("the probe session's child survived it")


def test_start_session_needs_a_passing_report_for_exactly_this_spec(world):
    failing, spec = probe(world, {"allow": ["gh.token.script"]})
    with pytest.raises(probes.ProbeFailed):
        probes.require(failing)
    with pytest.raises(probes.ProbeFailed):
        probes.start_session(spec, failing, "work", platform="darwin", managed=lambda: [])
    passing, spec = probe(world)
    changed = dataclasses.replace(spec, env={**spec.env, "LANG": "C"})
    with pytest.raises(sandbox.SandboxError, match="does not cover this session"):
        probes.start_session(changed, passing, "work", platform="darwin", managed=lambda: [])
    static_only = guard(world, spec, force_session=False)
    static_only.ran_session = False
    with pytest.raises(sandbox.SandboxError):
        probes.start_session(spec, static_only, "work", platform="darwin", managed=lambda: [])
    process = probes.start_session(spec, probes.require(passing), "Calls:\n", stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, platform="darwin", managed=lambda: [])
    assert process.wait(timeout=60) == 0


def test_start_session_refuses_an_old_report(world):
    passing, spec = probe(world)
    later = passing.finished_at + probes.MAX_REPORT_AGE + 1
    with pytest.raises(sandbox.SandboxError, match="older than"):
        probes.start_session(spec, passing, "work", platform="darwin", managed=lambda: [], now=lambda: later)


@pytest.mark.parametrize("change, problem", [
    (lambda w, spec: _rewrite(spec, lambda s: s["sandbox"].update(failIfUnavailable=False)) and None,
     "settings file on disk differs"),
    (lambda w, spec: Path(spec.argv[0]).write_text(Path(spec.argv[0]).read_text() + "# upgraded\n"),
     "claude executable differs"),
    (lambda w, spec: os.link(w.planted[1].path, w.paths.repository / "hard"), "hard links"),
])
def test_start_session_rechecks_the_session(world, change, problem):
    passing, spec = probe(world)
    change(world, spec)
    with pytest.raises(sandbox.SandboxError, match=problem):
        probes.start_session(spec, passing, "work", platform="darwin", managed=lambda: [])


def test_start_session_refuses_new_managed_settings_and_other_popen_options(world):
    passing, spec = probe(world)
    with pytest.raises(sandbox.SandboxError, match="managed settings present"):
        probes.start_session(spec, passing, "work", platform="darwin", managed=lambda: ["/Library/x.json"])
    with pytest.raises(TypeError):
        probes.start_session(spec, passing, "work", executable="/bin/sh", platform="darwin", managed=lambda: [])
    with pytest.raises(TypeError):
        probes.start_session(spec, passing, "work", pass_fds=(3,), platform="darwin", managed=lambda: [])


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


def test_git_references_follow_includes_and_core_files():
    text = ('[include]\n\tpath = a.cfg\n[includeIf "gitdir:~/w/"]\n\tpath = "~/b.cfg" # note\n'
            "[core]\n\texcludesFile = ~/ignore\n\tattributesfile = /etc/attrs\n[user]\n\tname = x\n")
    assert probes.git_references(text) == [("include", "a.cfg"), ("include", "~/b.cfg"), ("core", "~/ignore"),
                                           ("core", "/etc/attrs")]


# --- the stage-session tripwire (decision 14) ------------------------------------------------------------

def _stream(*uses, results=(), text=None):
    events = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"u{i}", "name": name,
                                                             "input": tool_input}]}}
              for i, (name, tool_input) in enumerate(uses)]
    events += [{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"u{i}",
                                                         "content": content, "is_error": True}]}}
               for i, content in enumerate(results)]
    if text:
        events.append({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    return "\n".join(json.dumps(event) for event in events)


@pytest.mark.parametrize("stream, label", [
    (lambda w: _stream(("Bash", {"command": f"cat ~/{w.folder.name}/*"}), results=["Operation not permitted"]),
     "credentials"),
    (lambda w: _stream(("Read", {"file_path": str(w.planted[0].path)}), results=["denied"]), "ssh-key"),
    (lambda w: _stream(("Grep", {"pattern": w.planted[2].path.name, "path": "/"})), "token"),
    (lambda w: _stream(text=f"found {w.planted[2].marker}"), "token"),
])
def test_a_stage_session_naming_a_decoy_trips_the_schedule(world, stream, label):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, world.state)
    findings = probes.stage_tripwire(stream(world), world.planted, directory=world.state, run_id=RUN_ID)
    assert findings and any(label in finding for finding in findings)
    assert autodev_config.load(world.state)["enabled"] is False
    stored = json.loads((schedules_dir(world.state) / decoys.TRIPWIRE_NAME).read_text())
    assert stored["last"]["reason"].startswith("stage session:") and stored["last"]["run_id"] == RUN_ID
    assert all(decoy.marker not in json.dumps(stored) for decoy in world.planted)


def test_a_clean_stage_session_does_not_trip(world):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, world.state)
    stream = _stream(("Bash", {"command": "pytest -q"}), ("Read", {"file_path": "/repo/README.md"}))
    assert probes.stage_tripwire(stream, world.planted, directory=world.state) == []
    assert autodev_config.load(world.state)["enabled"] is True


# --- the CLI ---------------------------------------------------------------------------------------------

def _ctx(world, scenario, **changes):
    autodev_config.save({**autodev_config.DEFAULTS, "clone": str(world.base / "runner" / "trusted")}, world.state)
    claude = fake_claude(world.base / "bin", {"fallback_tmpdir": str(world.paths.claude_temp), **scenario})

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
                          agents_dir=world.base / "LaunchAgents", resolver=lambda host: True, **changes)


def test_the_cli_probe_refuses_without_confirmation(world, capsys):
    ctx = _ctx(world, {})
    assert cli.main(["probe", "--mode", "dry-run"], ctx=ctx) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "confirmation_required" and runner.PROBE_CONFIRMATION in output["message"]
    assert not (world.base / "sessions").exists()


@pytest.mark.parametrize("mode", ["dry-run", "run-now", "scheduled", "resume"])
def test_the_cli_probe_runs_a_guarded_session_in_every_mode(world, capsys, mode, monkeypatch):
    created = []
    make = sandbox.make_temp_root
    monkeypatch.setattr(sandbox, "make_temp_root", lambda base: created.append(make(base)) or created[-1])
    ctx = _ctx(world, {})
    code = cli.main(["probe", "--mode", mode, "--kind", "implement", runner.PROBE_CONFIRMATION], ctx=ctx)
    output = json.loads(capsys.readouterr().out)
    assert code == 0, output
    assert output["status"] == "completed" and output["mode"] == mode and output["trigger"] == {"kind": "probe"}
    assert output["probes"][0]["ok"] and output["probes"][0]["ran_session"]
    types = output["probes"][0]["observed"]["target_types"]  # the world's temporary roots are directories
    assert not {"owner-temp", "claude-temp", "claude-shared"} & set(types)
    assert list(world.paths.claude_temp.iterdir()) == [] and list(world.paths.owner_temp.iterdir()) == []
    stored = (records.run_dir(output["run_id"], world.state) / records.RECORD_NAME).read_text()
    assert all(decoy.marker not in stored for decoy in decoys.registered(world.state))
    assert not list((world.base / "sessions").glob("*/probe"))
    assert len(created) == 1 and not created[0].exists()  # the run's temporary root is gone


def test_the_cli_probe_passes_the_broker_and_oauth_secrets_to_the_scan(world, capsys, monkeypatch):
    token = "github_pat_" + "Z" * 30
    key = world.paths.home / ".config/agents-autodev/deploy_key"
    key.parent.mkdir(parents=True)
    key.write_text("-----BEGIN KEY-----\n" + "k" * 60 + "\n-----END KEY-----\n")
    seen = {}
    real = probes.guard

    def spy(spec, **options):
        seen.update(options)
        return real(spec, **options)

    monkeypatch.setattr(probes, "guard", spy)
    world.commands.secret = token
    ctx = _ctx(world, {})
    autodev_config.save({**autodev_config.load(world.state), "sandbox": {
        "login": "keychain:claude-autodev", "decoy_keychain": False, "allowed_plugins": ["builtin-x"]}}, world.state)
    cli.main(["probe", runner.PROBE_CONFIRMATION], ctx=ctx)
    output = capsys.readouterr().out
    assert sorted(secret.label for secret in seen["known"]) == ["broker token", "deploy key", "oauth token"]
    assert seen["allowed_plugins"] == ["builtin-x"]
    assert token not in output and "k" * 60 not in output
    keychain = seen["targets"].keychain_items
    assert "claude-autodev" in keychain and any(item.startswith("Claude Code-credentials-") for item in keychain)


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


# --- review round 2 ---------------------------------------------------------------------------------------

def test_claude_pid_is_a_documented_cli_variable(world, monkeypatch):
    """G5: the CLI sets CLAUDE_PID in Bash (env-vars docs); the allowlist must accept it."""
    assert "CLAUDE_PID" in probes.CLI_ENV_NAMES
    monkeypatch.setattr(probes, "CLI_ENV_NAMES", probes.CLI_ENV_NAMES - {"CLAUDE_PID"})
    report, _ = probe(world)
    assert not report.ok and failed(report, "outside the session allowlist: CLAUDE_PID")


def test_the_pinned_plan_with_a_socket_and_a_read_root(world, fixed_nonce):
    """G8: the socket control and the socket directory and read-root write targets are pinned."""
    socket_dir = Path(tempfile.mkdtemp(prefix="sbx-"))
    try:
        clients = world.base / "clients"
        clients.mkdir()
        paths = dataclasses.replace(world.paths, socket=socket_dir / "c.sock", read_roots=(clients,))
        expected = sorted([*IMPLEMENT_PLAN, ("control.socket.bash", "Bash"),
                           *[(f"write.{name}.{suffix}", tool) for name in ("socket-dir", "read-root-0")
                             for suffix, tool in (("bash", "Bash"), ("script", "Bash"), ("write", "Write"))]])
        assert _plan_ids(world, "implement", fixed_nonce, paths) == expected
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_a_rewritten_probe_script_fails(world, fixed_nonce):
    """G7: a script rewritten to print forged denials passes every exit-code check, so only the
    runner's snapshot of its own script catches it."""
    report, _ = probe(world, {"tamper_scripts": ["-ipc.sh"]})
    assert not report.ok
    script = f"probe-{fixed_nonce}-ipc.sh"
    assert failed(report, f"apple-events.osascript.script: the probe script {script} changed"), report.failures
    assert all(f"the probe script {script} changed" in failure for failure in report.failures)  # nothing else saw it
    ipc = [check for check in report.checks if check["id"].startswith(("socket.", "launchd.", "apple-events.",
                                                                       "launch-services."))]
    assert ipc and all(check["outcome"] == "denied" for check in ipc)


# Network probes against a local stand-in for the sandbox proxy, which answers a blocked host with
# HTTP 403 (sandboxing docs). Only the loopback interface is used.

class _Proxy:
    def __init__(self, status):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                body = b"blocked by the sandbox proxy\n" if status >= 400 else b"ok\n"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "CONNECT":
                    self.wfile.write(body)

            do_GET = do_CONNECT = _answer

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _run_through(proxy, body):
    env = {"PATH": "/usr/bin:/bin", "http_proxy": proxy.url, "HTTP_PROXY": proxy.url, "https_proxy": proxy.url,
           "HTTPS_PROXY": proxy.url}
    return subprocess.run(["/bin/bash", "-c", body], env=env, capture_output=True, timeout=30).returncode


@pytest.mark.skipif(not os.path.exists("/usr/bin/curl"), reason="needs curl")
def test_a_proxy_error_page_never_reads_as_allowed(world, fixed_nonce):
    """G6: the proxy's 403 page makes every curl probe exit non-zero (denied); only a real
    response (an HTTP status below 400) exits 0. Without --fail the metadata probe read the
    error page as allowed."""
    calls, _ = _plan_calls(world, "implement", fixed_nonce)
    metadata, external = _body(calls, "network.metadata.script"), _body(calls, "network.external.script")
    assert "--fail" in metadata and "--max-time" in metadata
    blocked = _Proxy(403)
    try:
        assert _run_through(blocked, metadata) == 22  # curl: HTTP error with --fail
        assert _run_through(blocked, external) not in (0, *probes.INCONCLUSIVE_CODES)  # the CONNECT was refused
        assert _run_through(blocked, metadata.replace(" --fail", "")) == 0  # the round-1 form: a false "allowed"
    finally:
        blocked.close()
    answering = _Proxy(200)
    try:
        assert _run_through(answering, metadata) == 0  # a real response is allowed, a failure of the probe
    finally:
        answering.close()


def test_a_proxy_403_exit_code_counts_as_denied(world):
    report, _ = probe(world, {"rc": {"network.metadata.script": 22}})
    assert report.ok, report.failures


@pytest.mark.parametrize("probe_id, code", [("network.dns.script", probes.GUARD_FAILED), ("gh.token.script", 127),
                                            ("network.udp.script", 126), ("list.home.script", 127),
                                            ("write.home.bash", 126), ("keychain.gh-github-com.script", 127)])
def test_a_probe_that_did_not_run_is_inconclusive(world, probe_id, code):
    """G1: a missing tool (127), an unrunnable one (126) or a failed guard is no denial."""
    report, _ = probe(world, {"rc": {probe_id: code}})
    assert not report.ok and failed(report, f"{probe_id}: expected denied, got inconclusive")


def test_probes_that_hide_their_tools_exit_code_are_guarded(world, fixed_nonce):
    world.commands.existing.add("internet:github.com")
    calls, _ = _plan_calls(world, "implement", fixed_nonce)
    for check_id in ("network.dns.script", "keychain.git-credential-github-com.script"):
        assert f"|| exit {probes.GUARD_FAILED};" in _body(calls, check_id)
    missing_tool = _body(calls, "network.dns.script").replace("/usr/bin/dscacheutil", "/nonexistent/dscacheutil")
    assert subprocess.run(["/bin/bash", "-c", missing_tool], capture_output=True).returncode == probes.GUARD_FAILED


# Targets are typed before the session (G1, decision 3).

def _shared_as(world, kind):
    shared = world.paths.claude_shared
    shutil.rmtree(shared)
    if kind == "file":
        shared.write_text("the owner's own file\n")
    elif kind == "other":
        shared.symlink_to(world.base / "nowhere")
    return shared


def test_a_regular_file_target_gets_an_append_and_a_read(world, fixed_nonce):
    shared = _shared_as(world, "file")
    calls, run = _plan_calls(world, "implement", fixed_nonce)
    ids = {check.id for call in calls for check in call.checks}
    assert {"write.claude-shared.bash", "write.claude-shared.script", "list.claude-shared.bash",
            "list.claude-shared.script", "list.claude-shared.glob", "list.claude-shared.grep"} <= ids
    assert "write.claude-shared.write" not in ids  # overwriting needs a Read the read block refuses
    assert _body(calls, "write.claude-shared.script") == f": >> {shared}"
    assert _body(calls, "list.claude-shared.bash") == f"cat {shared} >/dev/null 2>&1"
    assert run.types["claude-shared"] == probes.FILE
    report, _ = probe(world)
    assert report.ok, report.failures
    assert report.observed["target_types"]["claude-shared"] == "file"
    assert shared.read_text() == "the owner's own file\n"


@pytest.mark.parametrize("scenario, failure", [
    ({"allow": ["write.claude-shared.script"]}, "write.claude-shared.script: expected denied, got allowed"),
    ({"allow": ["list.claude-shared.bash"]}, "list.claude-shared.bash: expected denied, got allowed"),
    (lambda w: {"allow_calls": [["Grep", str(w.paths.claude_shared)]]}, "list.claude-shared.grep"),
])
def test_a_regular_file_target_that_is_reachable_fails(world, scenario, failure):
    _shared_as(world, "file")
    report, _ = probe(world, scenario(world) if callable(scenario) else scenario)
    assert not report.ok and failed(report, failure), report.failures


def test_a_missing_target_is_created_by_the_probe(world, fixed_nonce):
    shared = _shared_as(world, "missing")
    calls, run = _plan_calls(world, "implement", fixed_nonce)
    ids = {check.id for call in calls for check in call.checks}
    assert {"write.claude-shared.bash", "write.claude-shared.script", "write.claude-shared.write"} <= ids
    assert not any(check_id.startswith("list.claude-shared.") for check_id in ids)  # nothing to read
    assert _body(calls, "write.claude-shared.bash") == f"mkdir {shared}"
    write = next(call for call in calls if call.checks[0].id == "write.claude-shared.write")
    assert write.input["file_path"] == str(shared) and write.checks[0].absent == (shared,)
    report, _ = probe(world)
    assert report.ok, report.failures
    assert report.observed["target_types"]["claude-shared"] == "missing" and not shared.exists()


@pytest.mark.parametrize("scenario, check_id", [
    ({"allow": ["write.claude-shared.script"]}, "write.claude-shared.script"),
    (lambda w: {"allow_calls": [["Write", str(w.paths.claude_shared)]]}, "write.claude-shared.write"),
])
def test_a_missing_target_the_session_could_create_fails_and_is_removed(world, scenario, check_id):
    shared = _shared_as(world, "missing")
    report, _ = probe(world, scenario(world) if callable(scenario) else scenario)
    assert not report.ok and failed(report, f"{check_id}: {shared} was created"), report.failures
    assert not os.path.lexists(shared)


def test_a_target_that_cannot_be_probed_refuses_the_probe(world):
    _shared_as(world, "other")  # a dangling link: mkdir would fail on it whatever the sandbox does
    report, _ = probe(world)
    assert not report.ok and not report.ran_session
    assert failed(report, "the probe target claude-shared is neither a directory, a regular file nor missing")
    assert report.observed["target_types"]["claude-shared"] == "other"


# The one-off repository (G3, decision 5).

def test_the_one_off_repositorys_git_and_claude_settings_are_probed(world, fixed_nonce):
    repo = world.paths.repository
    calls, run = _plan_calls(world, "implement", fixed_nonce)
    assert run.types["repo-git-config"] == probes.FILE and run.types["repo-git-hooks"] == probes.MISSING
    assert _body(calls, "write.repo-git-config.bash") == f": >> {repo}/.git/config"
    assert _body(calls, "write.repo-git-hooks.script") == f"mkdir {repo}/.git/hooks"
    assert _body(calls, "write.repo-claude-settings.script") == (
        f"mkdir -p {repo}/.claude && printf '{{}}' > {repo}/.claude/settings.local.json")
    (repo / ".git/hooks").mkdir()
    calls, _ = _plan_calls(world, "implement", fixed_nonce)
    assert _body(calls, "write.repo-git-hooks.bash") == f"printf x > {repo}/.git/hooks/probe-{fixed_nonce}-bash"
    report, _ = probe(world)
    assert report.ok, report.failures
    assert not (repo / ".claude").exists()


@pytest.mark.parametrize("scenario, failure", [
    ({"allow": ["write.repo-git-config.script"]}, "write.repo-git-config.script: expected denied, got allowed"),
    ({"allow": ["write.repo-git-hooks.bash"]}, "/.git/hooks was created"),
    ({"allow": ["write.repo-claude-settings.script"]}, "/.claude/settings.local.json was created"),
    ({"allow_calls": [["Write", "/.git/config"]]}, "write.repo-git-config.write: "),
    ({"allow_calls": [["Write", "/.git/hooks"]]}, "write.repo-git-hooks.write: "),
    ({"allow_calls": [["Write", "settings.local.json"]]}, "write.repo-claude-settings.write: "),
    ({"deny_calls": [["Read", "/.git/config"]]}, "read.repo-git-config.read: expected allowed, got denied"),
])
def test_a_one_off_repository_write_that_succeeded_fails(world, scenario, failure):
    repo = world.paths.repository
    report, _ = probe(world, scenario)
    assert not report.ok and failed(report, failure), report.failures
    assert not (repo / ".git/hooks").exists() and not (repo / ".claude").exists()


def test_the_git_config_write_counts_only_after_its_read(world):
    report, _ = probe(world, {"skip": [_call_index(world, "Read", "/.git/config")]})
    assert not report.ok
    assert failed(report, "write.repo-git-config.write: expected denied, got error without a successful "
                          "read.repo-git-config.read first")


def _call_index(world, tool, part):
    spec = build(world, {})
    run = probes.ProbeRun("f" * 16, list(world.planted))
    try:
        probes._prepare(spec, run, world.targets, None)
        run.keychain = probes.keychain_inventory(world.targets, run.keychain_decoy, world.commands)
        calls = probes.plan(spec, run, world.targets)
    finally:
        probes._cleanup(spec, run)
    return next(index for index, call in enumerate(calls) if call.tool == tool and part in json.dumps(call.input))


@pytest.mark.parametrize("plant, problem", [
    (lambda repo: (repo / ".claude").mkdir() or (repo / ".claude/settings.json").write_text("{}"),
     "holds project settings"),
    (lambda repo: (repo / ".claude").mkdir() or (repo / ".claude/settings.local.json").write_text("{}"),
     "holds project settings"),
    (lambda repo: (repo / ".mcp.json").write_text("{}"), "holds a project MCP configuration"),
    (lambda repo: (repo / ".claude").write_text("x"), ".claude is not a directory"),
])
def test_a_one_off_repository_with_project_settings_is_refused(world, plant, problem):
    passing, spec = probe(world)
    plant(world.paths.repository)
    report = guard(world, spec)
    assert not report.ok and not report.ran_session and failed(report, problem), report.failures
    with pytest.raises(sandbox.SandboxError, match=problem):
        probes.start_session(spec, passing, "work", platform="darwin", managed=lambda: [])


def test_a_plain_claude_directory_is_allowed(world):
    (world.paths.repository / ".claude").mkdir()
    (world.paths.repository / ".claude/commands").mkdir()
    assert probes.repository_problems(world.paths.repository) == []


# Names the stream chose (G2, decision 4).

def test_secret_chunks_as_tool_names_never_reach_the_record(world):
    token = "ghp_Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z" + "Q1w2E3"
    chunks = [token[index:index + 12] for index in range(0, 36, 12)]
    calls = [{"tool": chunk, "input": {}} for chunk in chunks] + [{"tool": "WebFetch", "input": {"url": "x"}}]
    report = guard(world, build(world, {"extra_calls": calls}), known=[KnownSecret("broker token", token)])
    assert not report.ok
    assert failed(report, "3 unexpected <invalid> calls (not in the probe plan)")
    assert failed(report, "an unexpected WebFetch call (not in the probe plan)")  # a documented built-in name
    assert report.observed["unexpected_calls"] == 4
    record = probes.record_report(RUN_ID, world.state, report, world.planted, known=[token])
    text = json.dumps(record)
    assert all(chunk not in text for chunk in chunks) and token not in text


def test_many_unexpected_calls_stay_one_line_and_failures_are_capped(world):
    report, _ = probe(world, {"extra_calls": [{"tool": f"x{index}", "input": {}} for index in range(300)]})
    assert [failure for failure in report.failures if "unexpected" in failure] == [
        "300 unexpected <invalid> calls (not in the probe plan)"]
    many = probes.ProbeReport("implement", "dry-run", "d", failures=[f"f{index}" for index in range(500)])
    probes._finish(many, time.monotonic())
    assert not many.ok and len(many.failures) == probes.MAX_FAILURES + 1
    assert many.failures[-1] == "300 more failure(s) not listed"
