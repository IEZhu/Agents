"""The autodev commands, configuration and run records, with fakes for GitHub, launchd and supervision (#338)."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
from types import SimpleNamespace

from urllib.parse import parse_qs

import pytest

from src.schedules import broker, cli, config as autodev_config, github, heavy, lease, records, runner, sequence
from src.schedules import socket_service
from src.schedules.github import GitHubClient, TokenUnavailable
from src.schedules.supervise import Supervised, SupervisorStopped
from tests.test_schedules_github import FakeGitHub
from tests.test_schedules_launchagent import FakeLaunchd
from tests.test_schedules_socket import leave_endpoint

ZONE = timezone(timedelta(hours=3))
SECRET = "ghp_" + "Q" * 36
BASE = "/repos/IEZhu/Agents"


def empty_github():
    return {f"{BASE}/issues": [[]], f"{BASE}/pulls": [[]]}


class FakeGitHubByLabel(FakeGitHub):
    """`FakeGitHub`, with the select stage's candidate lists (``autodev:ready``, ``autodev:next``)
    answered from ``<path>?labels=<label>`` pages, empty by default."""

    def __call__(self, url, headers):
        path, _, query = url.removeprefix(github.API).partition("?")
        label = (parse_qs(query).get("labels") or [None])[0]
        if label in ("autodev:ready", "autodev:next"):
            self.calls.append((url, dict(headers)))
            return 200, {}, json.dumps(self.pages.get(f"{path}?labels={label}", [[]])[0]).encode()
        return super().__call__(url, headers)


def no_credentials(config):
    """Credentials a test machine does not have: a real run refuses on its preconditions."""
    def token():
        raise TokenUnavailable("no token in the tests")
    missing = Path("/nonexistent/autodev-test")
    return broker.Credentials(missing / "deploy_key", missing / "github_known_hosts", token)


@pytest.fixture
def ctx(tmp_path, service_state_dir):
    clock = {"now": datetime(2026, 10, 12, 2, 20, tzinfo=ZONE)}
    pages = empty_github()
    fake_github = FakeGitHubByLabel(pages)
    supervised = []
    notifications = []

    def notify(argv, **kwargs):
        notifications.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def supervise(argv, **kwargs):
        """Run the child in this process, as the real supervisor would in a new process group."""
        supervised.append((argv, kwargs))
        assert argv[1:6] == ["-P", "-m", "src.schedules", "run", "autodev"]
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(argv[4:], context)
        return Supervised(code, caffeinated=True)

    context = runner.Context(
        installation=tmp_path / "installation", directory=service_state_dir, python="/opt/python",
        clock=lambda: clock["now"], launchctl=FakeLaunchd(), agents_dir=tmp_path / "LaunchAgents",
        client_factory=lambda config: GitHubClient(config["repository"], lambda: SECRET, transport=fake_github),
        drift_check=lambda installation: {"pinned": "c" * 40, "runner_commit": "c" * 40, "changed": []},
        supervise=supervise, heavy_processes=lambda: [], on_ac_power=lambda: True, notify_runner=notify,
        credentials=no_credentials)
    context.test = SimpleNamespace(clock=clock, pages=pages, github=fake_github, supervised=supervised,
                                   notifications=notifications)
    return context


def call(ctx, *argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv), ctx)
    return code, json.loads(out.getvalue())


# --- configuration -------------------------------------------------------------------------------

def test_defaults_follow_the_design(service_state_dir):
    loaded = autodev_config.load(service_state_dir)
    assert loaded == autodev_config.DEFAULTS and loaded is not autodev_config.DEFAULTS
    assert loaded["times"] == ["02:17", "13:47"] and loaded["catch_up_minutes"] == 120
    assert loaded["allowed_hours"] == "08:00-23:00,00:00-06:00" and loaded["enabled"] is False
    assert loaded["mode"] == "dry-run" and loaded["github"]["token"] == "keychain:agents-autodev-github"


def test_configuration_is_saved_privately_and_merged_over_the_defaults(service_state_dir):
    path = autodev_config.save({**autodev_config.DEFAULTS, "times": ["03:00"]}, service_state_dir)
    assert path == service_state_dir / "schedules" / "autodev.json"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.write_text(json.dumps({"times": ["04:00"], "limits": {"max_bot_rounds": 2}}))
    loaded = autodev_config.load(service_state_dir)
    assert loaded["times"] == ["04:00"] and loaded["limits"] == {"max_reviews": 3, "max_bot_rounds": 2,
                                                                 "max_wait_minutes": 120}


@pytest.mark.parametrize("change, problem", [
    ({"mode": "auto"}, "mode must be one of"),
    ({"times": ["25:00"]}, "times:"),
    ({"times": []}, "times must be a non-empty list"),
    ({"catch_up_minutes": 0}, "catch_up_minutes"),
    ({"catch_up_minutes": True}, "catch_up_minutes"),
    ({"allowed_hours": "8-23"}, "allowed_hours"),
    ({"repository": "not a repo"}, "repository"),
    ({"enabled": "yes"}, "enabled must be true or false"),
    ({"stages": {"select": {"client": "gemini"}}}, "stages.select.client"),
    ({"delegates": {"other": {}}}, "delegates may have only"),
    ({"limits": {"max_reviews": -1}}, "limits"),
    ({"github": {"token": "ghp_x"}}, "github.token must name a keychain item"),
    ({"surprise": 1}, "unknown keys: surprise"),
    ({"clone": "~/Documents/autodev-clone"}, "clone must lie outside ~/Documents"),
    ({"clone": "relative/clone"}, "clone must be an absolute path"),
])
def test_invalid_configuration_is_refused(service_state_dir, change, problem):
    path = autodev_config.config_path(service_state_dir)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(change))
    with pytest.raises(autodev_config.ConfigError) as raised:
        autodev_config.load(service_state_dir)
    assert any(problem in item for item in raised.value.problems), raised.value.problems


def test_unreadable_configuration_is_a_config_error(ctx):
    path = autodev_config.config_path(ctx.directory)
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    code, result = call(ctx, "autodev", "status")
    assert code == 1 and result["error"] == "invalid_config"


def test_save_validates_before_writing(service_state_dir):
    with pytest.raises(autodev_config.ConfigError):
        autodev_config.save({**autodev_config.DEFAULTS, "mode": "auto"}, service_state_dir)
    assert not autodev_config.config_path(service_state_dir).exists()


# --- run records ----------------------------------------------------------------------------------

def test_records_mask_token_like_strings(service_state_dir):
    run_id = records.new_run_id(datetime(2026, 10, 12, tzinfo=timezone.utc))
    secrets = {"gh": SECRET, "pat": "github_pat_" + "A" * 30, "anthropic": "sk-ant-api03-" + "b" * 20,
               "router": "sk-or-v1-" + "c" * 20, "aws": "AKIA" + "D" * 16,
               "key": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
               "header": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"}
    written = records.write_record(run_id, {"status": "completed", "nested": [secrets], "known": "x-s3cr3t-x"},
                                   service_state_dir, known=["s3cr3t"])
    text = (service_state_dir / "schedules" / "runs" / run_id / "record.json").read_text()
    for secret in [*secrets.values(), "s3cr3t", "abcdefghijklmnopqrstuvwxyz"]:
        assert secret not in text
    assert written["nested"][0]["gh"] == "ghp_***" and written["known"] == "x-***-x"
    assert stat.S_IMODE((service_state_dir / "schedules" / "runs" / run_id).stat().st_mode) == 0o700


def test_trusted_comment_ids_come_from_the_run_records(service_state_dir):
    for index, comments in enumerate([[{"id": 9001, "kind": "state"}], [{"id": 9002, "kind": "selection"}, {"id": True}]]):
        run_id = records.new_run_id(datetime(2026, 10, 12, 1, index, tzinfo=timezone.utc))
        records.write_record(run_id, {"status": "completed", "comments": comments}, service_state_dir)
    assert records.trusted_comment_ids(service_state_dir) == {9001, 9002}


def test_run_ids_cannot_name_other_paths(service_state_dir):
    for bad in ("../x", "20261012T000000Z-abc", "x"):
        with pytest.raises(ValueError):
            records.run_dir(bad, service_state_dir)
        assert records.read_record(bad, service_state_dir) is None


# --- real modes: run-now, and the automatic mode of #350 (tests/test_schedules_automatic.py) ------------

def test_enabling_run_mode_refuses_while_the_automatic_mode_is_not_ready(ctx):
    code, result = call(ctx, "autodev", "enable", "--mode", "run", "--accept-shared-account")
    assert code == 1 and result["ok"] is False and result["error"] == "not_ready"
    assert "the API token" in result["missing"] and "autodev ready" in result["message"]
    assert ctx.test.supervised == [] and ctx.test.github.calls == []  # without the token nothing is read
    assert autodev_config.load(ctx.directory)["enabled"] is False
    assert not records.runs_dir(ctx.directory).exists()
    assert not (ctx.directory / "schedules" / "acceptance.json").exists()


@pytest.mark.parametrize("argv, mode", [
    (("autodev", "run-now", "--mode", "run"), "run"),
    (("autodev", "run-now", "--mode", "resume", "--pr", "410"), "resume"),
])
def test_run_now_starts_a_supervised_real_run_that_checks_its_preconditions(ctx, argv, mode):
    code, record = call(ctx, *argv)
    assert code == 1 and record["status"] == "refused" and record["mode"] == mode
    assert record["missing"] == ["the deploy key", "the pinned github.com host keys", "the API token"]
    assert "gate" not in record and ctx.test.github.calls == []  # nothing read before the preconditions
    argv_, kwargs = ctx.test.supervised[0]
    assert argv_[6:8] == ["--mode", mode]
    assert kwargs["timeout"] == sequence.run_timeout(autodev_config.DEFAULTS, mode)
    assert kwargs["timeout"] == (330 if mode == "run" else 120) * 60
    [notification] = ctx.test.notifications
    assert notification[-2] == "Autodev run refused"


def test_resume_needs_a_pull_request(ctx, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["autodev", "run-now", "--mode", "resume"], ctx)
    assert raised.value.code == 2


# --- dry-run end to end -------------------------------------------------------------------------------

def test_run_now_dry_run_reaches_the_gate_decision(ctx):
    code, record = call(ctx, "autodev", "run-now", "--mode", "dry-run")
    assert code == 0 and record["status"] == "completed" and record["mode"] == "dry-run"
    assert record["gate"]["decision"] == "select" and record["cleanup_executed"] is False
    assert record["trigger"]["kind"] == "manual" and record["pinned"] == "c" * 40 and record["caffeinated"]
    assert record["selection"]["decision"] == "none" and "autodev:ready" in record["selection"]["rationale"]
    assert "probes" not in record and ctx.test.notifications == []  # no candidate: no session, no notification
    stored = records.read_record(record["run_id"], ctx.directory)
    assert stored["status"] == "completed" and SECRET not in json.dumps(stored)
    argv, kwargs = ctx.test.supervised[0]
    assert argv[:10] == ["/opt/python", "-P", "-m", "src.schedules", "run", "autodev", "--mode", "dry-run",
                         "--run-id", record["run_id"]]
    assert kwargs["cwd"] == ctx.installation and kwargs["timeout"] == sequence.run_timeout({}, "dry-run") == 3600
    environment = kwargs["env"]
    assert environment["PYTHONPATH"] == str(ctx.installation)
    assert environment["AGENTS_SERVICE_DIR"] == str(ctx.directory)
    assert set(environment) <= {"PATH", "PYTHONPATH", "AGENTS_SERVICE_DIR", "PYTHONDONTWRITEBYTECODE",
                                *runner._ENVIRONMENT}
    # Only reads: the client has no other method, and every request was a GET under the repository.
    assert all(url.startswith("https://api.github.com/repos/IEZhu/Agents/") for url, _ in ctx.test.github.calls)


def test_dry_run_reports_cleanup_actions_without_executing_them(ctx):
    old = (ctx.test.clock["now"] - timedelta(hours=8)).astimezone(timezone.utc).isoformat()
    ctx.test.pages[f"{BASE}/issues"] = [[{"number": 401, "labels": [{"name": "autodev:in-progress"}]}]]
    ctx.test.pages[f"{BASE}/issues/401/events"] = [[{"event": "labeled", "label": {"name": "autodev:in-progress"},
                                                     "created_at": old}]]
    code, record = call(ctx, "autodev", "run-now")
    assert code == 0 and record["gate"]["decision"] == "select"
    assert record["gate"]["actions"][0]["op"] == "remove_label" and record["cleanup_executed"] is False
    # Issues, events, open pulls, closed pulls, then the two candidate lists: reads only.
    assert len(ctx.test.github.calls) == 6


@pytest.mark.parametrize("failure, reason", [
    ("drift", "runner drift: src/schedules/gate.py"),
    ("network", "network: GET /repos/IEZhu/Agents/issues: HTTP 502"),
    ("token", "cli: the keychain item"),
    ("git", "cli: git rev-parse failed"),
    ("fetch", "network: fetch of main"),
    ("trusted", "cli: trusted repository: "),
])
def test_infrastructure_failures_are_recorded(ctx, failure, reason):
    if failure == "drift":
        ctx.drift_check = lambda installation: {"pinned": "c" * 40, "changed": ["src/schedules/gate.py"]}
    elif failure == "network":
        ctx.client_factory = lambda config: GitHubClient(
            config["repository"], lambda: SECRET, transport=lambda url, headers: (502, {}, b"{}"))
    elif failure == "token":
        def no_token():
            raise TokenUnavailable("the keychain item agents-autodev-github (account WonderMr) could not be read")
        ctx.client_factory = lambda config: GitHubClient(config["repository"], no_token, transport=ctx.test.github)
    elif failure == "git":
        def broken(installation):
            raise runner.drift.DriftCheckFailed("git rev-parse failed: not a repository")
        ctx.drift_check = broken
    else:
        def unusable(installation):
            if failure == "fetch":
                raise runner.repos.FetchError("fetch of main from https://github.com/IEZhu/Agents.git: failed")
            raise runner.repos.RepositoryError("the trusted repository has info/grafts")
        ctx.drift_check = unusable
    code, record = call(ctx, "autodev", "run-now")
    assert code == 1 and record["status"] == "failed: infra" and record["reason"].startswith(reason)
    assert "gate" not in record
    [notification] = ctx.test.notifications  # a dry-run that failed notifies the owner
    assert notification[-2] == "Autodev dry-run failed"


def test_a_held_lease_is_an_infrastructure_failure(ctx):
    with lease.hold(ctx.directory):
        code, record = call(ctx, "autodev", "run-now")
    assert code == 1 and record["status"] == "failed: infra" and record["reason"].startswith("lease:")


def test_a_run_rechecks_the_update_markers_once_it_holds_the_lease(ctx):
    """An update that began after the tick's check: the run ends before it reads anything."""
    for marker in runner.UPDATE_MARKERS:
        (ctx.directory / marker).write_text("{}")
        code, record = call(ctx, "autodev", "run-now")
        assert code == 1 and record["status"] == "failed: infra"
        assert record["reason"] == f"update: a daemon update or maintenance is in progress ({marker})"
        assert "gate" not in record and ctx.test.github.calls == []
        (ctx.directory / marker).unlink()
    assert call(ctx, "autodev", "run-now")[1]["status"] == "completed"


def test_a_run_cannot_start_while_an_update_holds_the_lease(ctx, monkeypatch):
    monkeypatch.setattr(lease, "HOLD_WAIT", 0.2)
    with lease.hold(ctx.directory):  # as `src.daemon.autoupdate` holds it for its transaction
        (ctx.directory / "maintenance.json").write_text("{}")
        code, record = call(ctx, "autodev", "run-now")
    assert code == 1 and record["reason"].startswith("lease: ") and ctx.test.github.calls == []


def test_timeout_and_crash_of_the_run_are_infrastructure_failures(ctx):
    ctx.supervise = lambda argv, **kwargs: Supervised(-9, timed_out=True, signalled=[11, 12])
    code, record = call(ctx, "autodev", "run-now")
    assert code == 1 and record["status"] == "failed: infra" and record["reason"].startswith("timeout:")
    assert record["signalled"] == [11, 12]
    ctx.supervise = lambda argv, **kwargs: Supervised(1)
    code, record = call(ctx, "autodev", "run-now")
    assert code == 1 and record["reason"] == "cli: the run exited with code 1 without a result"

    def cannot_start(argv, **kwargs):
        raise FileNotFoundError(2, "No such file", argv[0])
    ctx.supervise = cannot_start
    code, record = call(ctx, "autodev", "run-now")
    assert code == 1 and record["reason"].startswith("cli: the run could not start")


def test_a_stopped_supervisor_completes_the_record_before_it_exits(ctx):
    def stopped(argv, **kwargs):
        raise SupervisorStopped(signal.SIGTERM)
    ctx.supervise = stopped
    with pytest.raises(SupervisorStopped):
        call(ctx, "autodev", "run-now")
    record = records.all_records(ctx.directory)[0]
    assert record["status"] == "failed: infra" and record["finished"]
    assert record["reason"] == "stopped: the supervisor received SIGTERM; the run's process tree was ended"


def test_a_run_the_supervisor_ended_still_notifies_the_owner(ctx):
    """Correctness review #347: a run ended by a timeout or a stopped supervisor that could not
    notify the owner itself is notified by the launching process, once."""
    ctx.supervise = lambda argv, **kwargs: Supervised(-9, timed_out=True)
    code, record = call(ctx, "autodev", "run-now")
    assert code == 1 and record["reason"].startswith("timeout:") and record["notification"]["sent"] is True
    [argv] = ctx.test.notifications
    assert argv[-2] == "Autodev dry-run failed" and record["run_id"] in argv[-1]
    assert record["report"]["status"] == "failed: infra"

    def notified_itself(argv, **kwargs):
        run_id = argv[argv.index("--run-id") + 1]
        records.update_record(run_id, lambda current: {**current, "notification": {"sent": True, "title": "x"}},
                              ctx.directory)
        return Supervised(-9, timed_out=True)
    ctx.supervise = notified_itself
    call(ctx, "autodev", "run-now")
    assert len(ctx.test.notifications) == 1

    def stopped(argv, **kwargs):
        raise SupervisorStopped(signal.SIGTERM)
    ctx.supervise = stopped
    with pytest.raises(SupervisorStopped):
        call(ctx, "autodev", "run-now")
    assert len(ctx.test.notifications) == 2 and ctx.test.notifications[-1][-2] == "Autodev dry-run failed"


@pytest.fixture
def temp_root():
    """A short temporary directory, as the runner's own, for private socket directories."""
    path = Path(tempfile.mkdtemp(prefix="sock-"))
    yield path
    shutil.rmtree(path)


def test_a_runner_socket_left_by_a_killed_run_is_removed(ctx, temp_root):
    """A SIGKILL ends `serve` without its cleanup. Once the run's tree is gone, after a timeout or
    a stopped supervisor, the runner removes the socket, clients and private directory that the
    run record's ``endpoint`` names."""
    run_ids = []

    def leaving_an_endpoint(outcome):
        def supervise(argv, **kwargs):
            run_id = argv[argv.index("--run-id") + 1]
            run_ids.append(run_id)
            private = Path(tempfile.mkdtemp(prefix=socket_service.PRIVATE_PREFIX, dir=temp_root))
            endpoint = leave_endpoint(private, private=True)
            records.write_record(run_id, {**records.read_record(run_id, ctx.directory), "endpoint": endpoint},
                                 ctx.directory)
            return outcome()
        return supervise

    ctx.supervise = leaving_an_endpoint(lambda: Supervised(-9, timed_out=True))
    code, record = call(ctx, "autodev", "run-now")
    assert code == 1 and record["reason"].startswith("timeout:")
    assert record["endpoint_cleanup"]["kept"] == []
    assert {record["endpoint"]["socket"], *record["endpoint"]["clients"].values()} < \
        set(record["endpoint_cleanup"]["removed"])
    assert list(temp_root.iterdir()) == []

    def stopped():
        raise SupervisorStopped(signal.SIGTERM)
    ctx.supervise = leaving_an_endpoint(stopped)
    with pytest.raises(SupervisorStopped):
        call(ctx, "autodev", "run-now")
    record = records.read_record(run_ids[-1], ctx.directory)
    assert record["reason"].startswith("stopped:") and record["endpoint_cleanup"]["kept"] == []
    assert list(temp_root.iterdir()) == []

    ctx.supervise = lambda argv, **kwargs: Supervised(1)  # a run that names no endpoint
    assert "endpoint_cleanup" not in call(ctx, "autodev", "run-now")[1]


def test_processes_swept_after_a_normal_exit_are_recorded(ctx):
    supervise = ctx.supervise
    ctx.supervise = lambda argv, **kwargs: Supervised(supervise(argv, **kwargs).returncode, signalled=[4242])
    code, record = call(ctx, "autodev", "run-now")
    assert code == 0 and record["status"] == "completed" and record["swept"] == [4242]


def test_the_gate_trusts_state_comments_from_earlier_run_records(ctx):
    run_id = records.new_run_id(datetime(2026, 10, 11, tzinfo=timezone.utc))
    records.write_record(run_id, {"status": "completed", "comments": [{"id": 9001, "kind": "state", "pr": 410}]},
                         ctx.directory)
    pull = {"number": 410, "state": "open", "labels": [{"name": "autodev"}],
            "head": {"ref": "claude/autodev-401-x", "sha": "a" * 40, "repo": {"full_name": "IEZhu/Agents"}}}
    state = {"head": "a" * 40, "next_attempt": "2026-10-11T00:00:00Z", "rounds": 1, "review_status": "waiting_bots"}
    body = "<!-- autodev -->\n<!-- autodev:state " + json.dumps(state) + " -->"
    ctx.test.pages.update({
        f"{BASE}/pulls": [[pull]], f"{BASE}/pulls/410": {**pull, "mergeable": True},
        f"{BASE}/issues/410/comments": [[{"id": 9001, "body": body, "updated_at": "2026-10-11T00:00:00Z"}]],
        f"{BASE}/pulls/410/reviews": [[]], f"{BASE}/pulls/410/comments": [[]]})
    code, record = call(ctx, "autodev", "run-now")
    assert record["gate"]["decision"] == "resume" and record["gate"]["pr"] == 410


# --- tick, enable, disable, status, ready -----------------------------------------------------------------

def test_enable_disable_and_a_tick_that_runs_the_due_slot(ctx):
    code, result = call(ctx, "tick")
    assert code == 0 and result["tick"]["action"] == "idle" and "run" not in result

    ctx.test.clock["now"] = datetime(2026, 10, 12, 1, 0, tzinfo=ZONE)
    code, result = call(ctx, "autodev", "enable", "--mode", "dry-run")
    assert code == 0 and result["enabled"] and result["mode"] == "dry-run"
    assert result["next_slot"] == "2026-10-12T02:17:00+03:00" and result["scheduler_installed"] is False
    tick_state = json.loads((ctx.directory / "schedules" / "tick.json").read_text())
    assert tick_state["enabled_at"] == "2026-10-12T01:00:00+03:00"

    ctx.test.clock["now"] = datetime(2026, 10, 12, 2, 20, tzinfo=ZONE)
    code, result = call(ctx, "tick")
    assert result["tick"]["action"] == "run" and result["run"]["status"] == "completed"
    assert result["run"]["trigger"] == {"kind": "slot", "at": "2026-10-12T02:17:00+03:00"}
    ctx.test.clock["now"] = datetime(2026, 10, 12, 2, 25, tzinfo=ZONE)
    assert call(ctx, "tick")[1]["tick"]["action"] == "idle"
    assert len(ctx.test.supervised) == 1

    code, result = call(ctx, "autodev", "disable")
    assert code == 0 and autodev_config.load(ctx.directory)["enabled"] is False


def test_tick_in_run_mode_refuses_without_starting_anything_while_not_ready(ctx):
    config = {**autodev_config.DEFAULTS, "enabled": True, "mode": "run"}
    autodev_config.save(config, ctx.directory)
    (ctx.directory / "schedules" / "tick.json").write_text(json.dumps({"enabled_at": "2026-10-12T00:00:00+03:00"}))
    code, result = call(ctx, "tick")
    assert result["tick"]["action"] == "run" and result["run"]["status"] == "refused"
    assert result["run"]["reason"].startswith("automatic mode not ready: the deploy key; ")
    assert "the API token" in result["run"]["reason"] and result["run"]["reason"].endswith(" more)")
    assert ctx.test.supervised == []
    [record] = records.all_records(ctx.directory)  # recorded, and the owner is told
    assert record["run_id"] == result["run"]["run_id"] and record["mode"] == "run" and "the API token" in record["missing"]
    [argv] = ctx.test.notifications
    assert argv[-2] == "Autodev run refused" and argv[-1] == f"details in run {record['run_id']}."
    state = json.loads((ctx.directory / "schedules" / "tick.json").read_text())
    assert state["last_slot"] == "2026-10-12T02:17:00+03:00"  # handled, not retried every five minutes


def test_tick_skips_while_a_daemon_update_runs(ctx):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, ctx.directory)
    (ctx.directory / "schedules" / "tick.json").write_text(json.dumps({"enabled_at": "2026-10-12T00:00:00+03:00"}))
    (ctx.directory / "transaction.json").write_text("{}")
    code, result = call(ctx, "tick")
    assert result["tick"]["action"] == "skip" and "daemon update" in result["tick"]["reason"]
    assert ctx.test.supervised == []


def test_tick_skips_while_a_session_holds_the_heavy_process_lease(ctx):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, ctx.directory)
    (ctx.directory / "schedules" / "tick.json").write_text(json.dumps({"enabled_at": "2026-10-12T00:00:00+03:00"}))
    with lease.acquire(heavy.lease_path(ctx.directory)):
        code, result = call(ctx, "tick")
        assert result["tick"]["action"] == "skip" and result["tick"]["reason"] == "the heavy-process lease is held"
        assert call(ctx, "autodev", "status")[1]["heavy_lease_held"] is True
    assert ctx.test.supervised == []
    assert call(ctx, "tick")[1]["tick"]["action"] == "run"


def test_status_reports_state_runs_and_the_next_slot(ctx):
    call(ctx, "autodev", "run-now")
    code, result = call(ctx, "autodev", "status")
    assert code == 0 and result["next_slot"] == "2026-10-12T13:47:00+03:00"
    assert result["scheduler"]["label"].endswith(".scheduler") and result["scheduler"]["installed"] is False
    assert result["recent_runs"][0]["status"] == "completed" and result["recent_runs"][0]["decision"] == "select"
    assert result["lease_held"] is False and result["automatic"]["on"] is False
    assert result["automatic"]["check"] == "python -m src.schedules autodev ready"
    acceptance = result["automatic"]["acceptance"]
    assert (acceptance["current"], acceptance["accepted"]) == (False, None) and "not recorded" in acceptance["problems"][0]
    assert result["heavy_lease_held"] is False
    assert result["config_file"] == str(ctx.directory / "schedules" / "autodev.json")


def test_ready_lists_what_is_missing_and_exits_with_one(ctx):
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["ok"] is True and result["ready"] is False
    assert "the API token" in result["missing"] and result["acceptance"]["current"] is False
    assert sum(item.startswith("sandbox probe ") for item in result["missing"]) == 12  # 3 kinds in 4 modes
    assert ctx.test.supervised == [] and ctx.test.github.calls == []


def test_install_and_uninstall_through_the_cli(ctx, tmp_path):
    ctx.tool_search = str(tmp_path / "no-tools")
    code, result = call(ctx, "autodev", "install", "--python", "/opt/other-python")
    assert code == 0 and result["changed"] and result["loaded"]
    assert result["label"] == f"local.agents-core.{ctx.directory.name}.scheduler"
    assert call(ctx, "autodev", "install", "--python", "/opt/other-python")[1]["changed"] is False
    assert call(ctx, "autodev", "uninstall")[1]["installed"] is False


def test_default_state_directory_comes_from_state_dir(service_state_dir):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["autodev", "enable", "--mode", "dry-run"]) == 0
    assert json.loads((service_state_dir / "schedules" / "autodev.json").read_text())["enabled"] is True
    assert os.environ["AGENTS_SERVICE_DIR"] == str(service_state_dir)


def test_run_environment_keeps_secrets_out(ctx, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    environment = runner.run_environment(ctx)
    assert "GITHUB_TOKEN" not in environment and "ANTHROPIC_API_KEY" not in environment
    assert environment["PATH"].split(os.pathsep)[0] == "/usr/bin"


def test_conftest_isolates_the_service_state_directory(service_state_dir):
    from src.daemon.state import state_dir
    assert state_dir() == Path(service_state_dir).resolve()
    assert "Application Support" not in str(state_dir())


def test_a_blind_process_scan_is_recorded(ctx):
    supervise = ctx.supervise
    ctx.supervise = lambda argv, **kwargs: Supervised(supervise(argv, **kwargs).returncode, failed_scans=2)
    code, record = call(ctx, "autodev", "run-now")
    assert code == 0 and record["status"] == "completed" and record["failed_process_scans"] == 2
