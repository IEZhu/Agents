"""The automatic mode (#350, src/schedules/automatic.py and runner.py): `autodev ready`, the owner's
acceptance, `enable --mode run --accept-shared-account`, the tick's refusal of a scheduled real run
and the schedule-off notification.

GitHub answers come from tests/fixtures/autodev_preconditions_synthetic.json (synthetic, in the
shape GitHub's REST reference gives) through a fake transport; probe results are run records the
tests write. No probe session, model turn, network, keychain or osascript runs: the command runner
fails the test if anything calls it, and notifications go to a list.
"""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import subprocess

import pytest

from src.schedules import automatic, cli, decoys, records, runner, stages
from src.schedules import config as autodev_config
from src.schedules import report as run_report
from src.schedules.broker import Credentials
from src.schedules.github import GitHubClient, TokenUnavailable
from src.schedules.supervise import Supervised
from tests.test_schedules_launchagent import FakeLaunchd
from tests.test_schedules_preconditions import FIXTURE, KEY, RULES, SECRETS, TOKEN, WORKFLOW, Transport

ZONE = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 14, 2, 20, tzinfo=ZONE)  # three minutes after the 02:17 slot
PLAN = Path(__file__).resolve().parents[1] / "docs" / "autodev-plan.md"
MODES = automatic.PROBE_MODES
PAIRS = [(kind, mode) for kind in stages.KINDS for mode in MODES]


def nothing_runs(argv, **kwargs):
    raise AssertionError(f"ready ran a command: {argv}")


@pytest.fixture
def ctx(tmp_path, service_state_dir):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "deploy_key").write_text(KEY + "\n")
    (keys / "github_known_hosts").write_text("github.com ssh-ed25519 placeholder\n")
    world = {"transport": Transport(), "token": lambda: TOKEN, "codex": {}, "notifications": [], "supervised": [],
             "runs": 0}

    def credentials(config):
        return Credentials(keys / "deploy_key", keys / "github_known_hosts", world["token"])

    def notify(argv, **kwargs):
        world["notifications"].append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def supervise(argv, **kwargs):  # the run's child is not started: the tests look at the argv
        world["supervised"].append(argv)
        return Supervised(0, caffeinated=True)

    context = runner.Context(
        installation=tmp_path / "installation", directory=service_state_dir, python="/opt/python",
        clock=lambda: NOW, launchctl=FakeLaunchd(), agents_dir=tmp_path / "LaunchAgents",
        client_factory=lambda config: GitHubClient(config["repository"], lambda: TOKEN, transport=world["transport"]),
        credentials=credentials, supervise=supervise, heavy_processes=lambda: [], on_ac_power=lambda: True,
        notify_runner=notify, command_runner=nothing_runs,
        codex_availability=lambda kind: world["codex"].get(kind, {"available": False, "reason": "not_checked"}))
    context.world = world
    return context


def call(ctx, *argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv), ctx)
    return code, json.loads(out.getvalue())


def utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def record_probe(ctx, kind, mode, *, ok=True, ran_session=True, age=timedelta(hours=2), finished=None,
                 guarded=False) -> str:
    """A run record with one probe report, newer than every one written before it: the owner's
    ``probe`` command's shape, or (``guarded``) a run's guarded stage session."""
    ctx.world["runs"] += 1
    started = NOW - timedelta(days=6) + timedelta(minutes=ctx.world["runs"])
    run_id = records.new_run_id(started)
    report = {"kind": kind, "mode": mode, "digest": "d" * 64, "ok": ok, "ran_session": ran_session,
              "failures": [] if ok else ["read.token.bash: expected denied, got allowed"],
              "finished": finished if finished is not None else utc(NOW - age)}
    record = {"trigger": {"kind": "slot" if guarded else "probe"}, "mode": "run" if guarded else mode,
              "status": "completed" if ok else "refused", "probes": [report]}
    if not guarded:
        record["session_kind"] = kind
    records.write_record(run_id, record, ctx.directory)
    return run_id


def record_every_probe(ctx, kinds=stages.KINDS) -> None:
    for kind in kinds:
        for mode in MODES:
            record_probe(ctx, kind, mode)


def accept(ctx, when=NOW - timedelta(hours=1)) -> dict:
    return automatic.record_acceptance(ctx.directory, when, source="cli")


def ready_now(ctx) -> dict:
    record_every_probe(ctx)
    return accept(ctx)


# --- ready -------------------------------------------------------------------------------------

def test_ready_passes_when_every_condition_holds(ctx):
    ready_now(ctx)
    code, result = call(ctx, "autodev", "ready")
    assert code == 0 and result["ok"] is True and result["ready"] is True, result["missing"]
    assert result["missing"] == [] and result["conditions"] == [] and result["codex"] == {}
    assert ctx.world["transport"].requests == [RULES, WORKFLOW, SECRETS] and FIXTURE["synthetic"] is True
    assert all(result["probes"][kind][mode]["status"] == "passed" for kind, mode in PAIRS)
    acceptance = result["acceptance"]
    assert acceptance["current"] is True and acceptance["accepted"] == utc(NOW - timedelta(hours=1))
    assert acceptance["rows"] == [1, 2, 4] and acceptance["risk_list_sha256"] == automatic.risk_list_version()
    assert [risk["row"] for risk in result["risks"]] == [1, 2, 4]
    text = json.dumps(result)
    assert TOKEN not in text and KEY not in text


def change_transport(ctx, what):
    responses = copy.deepcopy(FIXTURE["responses"])
    if what == "no required approval":
        for rule in responses[RULES]:
            if rule["type"] == "pull_request":
                rule["parameters"]["required_approving_review_count"] = 0
    elif what == "no deletion rule":
        responses[RULES] = [rule for rule in responses[RULES] if rule["type"] != "deletion"]
    elif what == "workflows may approve":
        responses[WORKFLOW] = {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True}
    elif what == "routine token as a repository secret":
        responses[SECRETS] = {"total_count": 1, "secrets": [{"name": "CLAUDE_ROUTINE_TOKEN"}]}
    ctx.world["transport"] = Transport(responses)


@pytest.mark.parametrize("what, item", [
    ("no required approval", "main protection: a pull_request rule with at least one required approving review"),
    ("no deletion rule", "main protection: the deletion rule"),
    ("workflows may approve", "workflows may not approve pull requests (can_approve_pull_request_reviews false)"),
    ("routine token as a repository secret",
     "CLAUDE_ROUTINE_TOKEN only as an environment secret, not a repository secret"),
])
def test_ready_names_each_missing_protection_value(ctx, what, item):
    ready_now(ctx)
    change_transport(ctx, what)
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["ready"] is False and result["missing"] == [item]


def test_ready_names_each_missing_credential(ctx, tmp_path):
    ready_now(ctx)
    (tmp_path / "keys" / "deploy_key").unlink()
    assert call(ctx, "autodev", "ready")[1]["missing"] == ["the deploy key"]

    def unavailable():
        raise TokenUnavailable("no token in the tests")
    ctx.world["token"] = unavailable
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["missing"] == ["the deploy key", "the API token"]


@pytest.mark.parametrize("state", ["missing", "failed", "stale", "future"])
def test_ready_names_a_missing_failed_or_stale_probe_record(ctx, state):
    ready_now(ctx)
    run_id = None
    if state == "missing":
        for record in records.all_records(ctx.directory):
            if record["probes"][0]["kind"] == "implement" and record["probes"][0]["mode"] == "scheduled":
                (records.run_dir(record["run_id"], ctx.directory) / records.RECORD_NAME).unlink()
    elif state == "failed":
        run_id = record_probe(ctx, "implement", "scheduled", ok=False)
    elif state == "stale":
        run_id = record_probe(ctx, "implement", "scheduled", age=timedelta(days=8))
    else:
        run_id = record_probe(ctx, "implement", "scheduled", age=-timedelta(hours=1))
    code, result = call(ctx, "autodev", "ready")
    command = "`python -m src.schedules probe --mode scheduled --kind implement --confirm-live-probe`"
    expected = {"missing": f"sandbox probe implement in scheduled mode: no record; run {command}",
                "failed": f"sandbox probe implement in scheduled mode: failed in run {run_id}; run {command} once it "
                          "is fixed",
                "stale": f"sandbox probe implement in scheduled mode: the passing record (run {run_id}) is older than "
                         f"7 days; run {command}",
                "future": f"sandbox probe implement in scheduled mode: the passing record (run {run_id}) is older "
                          f"than 7 days; run {command}"}[state]
    assert code == 1 and result["missing"] == [expected]
    assert result["probes"]["implement"]["scheduled"]["status"] == ("stale" if state == "future" else state)
    assert result["probes"]["implement"]["resume"]["status"] == "passed"


@pytest.mark.parametrize("kind, mode", PAIRS)
def test_every_kind_and_mode_needs_its_own_record(ctx, kind, mode):
    accept(ctx)
    for other_kind, other_mode in PAIRS:
        if (other_kind, other_mode) != (kind, mode):
            record_probe(ctx, other_kind, other_mode)
    record_probe(ctx, kind, "dry-run" if mode != "dry-run" else "run-now")  # another mode's record does not count
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["missing"] == [
        f"sandbox probe {kind} in {mode} mode: no record; run `python -m src.schedules probe --mode {mode} --kind "
        f"{kind} --confirm-live-probe`"]


def test_guarded_session_reports_count_but_static_checks_alone_do_not_replace_a_probe(ctx):
    ready_now(ctx)
    # A run's select session gets the static checks only: a passing one neither replaces nor refreshes.
    record_probe(ctx, "select", "scheduled", ran_session=False, guarded=True)
    assert call(ctx, "autodev", "ready")[0] == 0
    # A failing one counts as a failed probe.
    failed = record_probe(ctx, "select", "scheduled", ok=False, ran_session=False, guarded=True)
    assert call(ctx, "autodev", "ready")[1]["probes"]["select"]["scheduled"] == {
        "status": "failed", "run_id": failed, "finished": utc(NOW - timedelta(hours=2))}
    # A guarded implement session ran its probe session: a newer pass than an old failure.
    record_probe(ctx, "implement", "resume", ok=False)
    passed = record_probe(ctx, "implement", "resume", guarded=True)
    assert call(ctx, "autodev", "ready")[1]["probes"]["implement"]["resume"]["run_id"] == passed


def test_a_codex_select_stage_needs_its_stored_availability_instead_of_claude_probes(ctx):
    settings = copy.deepcopy(autodev_config.DEFAULTS)
    settings["stages"]["select"] = {"client": "codex", "codex": {"model": "gpt-6.1-sol", "effort": "high"}}
    autodev_config.save(settings, ctx.directory)
    record_every_probe(ctx, kinds=("implement", "submit"))
    accept(ctx)
    ctx.world["codex"]["select"] = {"available": False, "reason": "nested_sandbox_unavailable", "checked": "x"}
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["missing"] == [
        "Codex stage availability for select: nested_sandbox_unavailable; run "
        "`python -m src.schedules codex-probe --kind stage --confirm-live-probe`"]
    assert result["codex"] == {"select": {"available": False, "reason": "nested_sandbox_unavailable", "checked": "x"}}
    assert "select" not in result["probes"]
    ctx.world["codex"]["select"] = {"available": True, "reason": None, "checked": "2026-10-13T10:00:00+00:00"}
    assert call(ctx, "autodev", "ready")[0] == 0


def test_a_committing_stage_configured_for_codex_still_needs_its_claude_probes(ctx):
    settings = copy.deepcopy(autodev_config.DEFAULTS)
    settings["stages"]["implement"] = {"client": "codex", "codex": {"model": "gpt-6.1-sol", "effort": "high"}}
    autodev_config.save(settings, ctx.directory)
    accept(ctx)
    record_every_probe(ctx, kinds=("select", "submit"))
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["codex"] == {}  # implement runs with Claude (stages.stage_profile)
    assert [item.split(":")[0] for item in result["missing"]] == [f"sandbox probe implement in {mode} mode"
                                                                   for mode in MODES]


def test_ready_reads_stored_state_and_starts_no_probe(ctx):
    def no_probe_session(*args, **kwargs):
        raise AssertionError("ready started a probe session")
    ctx.probe_session = no_probe_session
    ctx.claude = "/nonexistent/claude"
    call(ctx, "autodev", "ready")
    ready_now(ctx)
    assert call(ctx, "autodev", "ready")[0] == 0
    assert ctx.world["supervised"] == [] and ctx.world["notifications"] == []


# --- the acceptance -----------------------------------------------------------------------------

def test_ready_names_a_missing_or_outdated_acceptance(ctx, monkeypatch):
    record_every_probe(ctx)
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["missing"] == [
        "the owner's acceptance of the residual risks (plan section 11, rows 1, 2 and 4): not recorded; give it "
        "with `python -m src.schedules autodev enable --mode run --accept-shared-account`"]
    accept(ctx)
    assert call(ctx, "autodev", "ready")[0] == 0
    changed = (*automatic.RESIDUAL_RISKS[:2], (4, automatic.RESIDUAL_RISKS[2][1], "a new residual risk"))
    monkeypatch.setattr(automatic, "RESIDUAL_RISKS", changed)
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["missing"] == [
        "the owner's acceptance of the residual risks (plan section 11, rows 1, 2 and 4): given for another version "
        "of the risk list; accept again with `python -m src.schedules autodev enable --mode run "
        "--accept-shared-account`"]
    assert result["acceptance"]["current"] is False


def test_a_tripwire_after_the_acceptance_asks_for_a_new_one(ctx):
    ready_now(ctx)
    decoys.trip(ctx.directory, "an earlier probe", now=NOW - timedelta(hours=3))  # before: still current
    assert call(ctx, "autodev", "ready")[0] == 0
    decoys.trip(ctx.directory, "stage session: tool call 1 (Read) names decoy token", now=NOW - timedelta(minutes=5))
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and len(result["missing"]) == 1
    assert result["missing"][0].startswith("the owner's acceptance of the residual risks (plan section 11, rows 1, 2 "
                                           "and 4): a tripwire turned the schedule off after it (")


def test_the_risk_list_is_the_plans_rows_one_two_and_four():
    section = PLAN.read_text(encoding="utf-8").split("## 11. Design pre-mortem", 1)[1].split("\n## ", 1)[0]
    rows = {int(match[1]): match[0] for match in re.finditer(r"^\| (\d+) \|.*$", section, re.MULTILINE)}
    for row, risk, residual in automatic.RESIDUAL_RISKS:
        assert risk in rows[row], row
        assert f"**Residual risk:** {residual}" in rows[row], row
    assert automatic.RISK_ROWS == (1, 2, 4)
    assert automatic.risk_list_version() == hashlib.sha256(automatic.risk_list_text().encode()).hexdigest()


# --- enable --mode run -----------------------------------------------------------------------------

def test_run_mode_without_the_flag_is_refused_naming_it(ctx):
    ready_now(ctx)
    code, result = call(ctx, "autodev", "enable", "--mode", "run")
    assert code == 1 and result["ok"] is False and result["error"] == "acceptance_required"
    assert result["flag"] == "--accept-shared-account" and "--accept-shared-account" in result["message"]
    assert [risk["row"] for risk in result["risks"]] == [1, 2, 4]
    assert ctx.world["transport"].requests == []  # refused before anything is read
    assert autodev_config.load(ctx.directory)["enabled"] is False


def test_run_mode_with_the_flag_is_refused_while_a_condition_is_missing(ctx):
    record_every_probe(ctx, kinds=("select", "implement"))  # submit never probed
    code, result = call(ctx, "autodev", "enable", "--mode", "run", "--accept-shared-account")
    assert code == 1 and result["error"] == "not_ready"
    assert result["missing"] == [f"sandbox probe submit in {mode} mode: no record; run `python -m src.schedules "
                                 f"probe --mode {mode} --kind submit --confirm-live-probe`" for mode in MODES]
    assert not automatic.acceptance_path(ctx.directory).exists()  # the acceptance is no condition of enabling
    assert autodev_config.load(ctx.directory)["enabled"] is False


def test_run_mode_records_the_acceptance_once_ready_passes(ctx, monkeypatch):
    record_every_probe(ctx)
    code, result = call(ctx, "autodev", "enable", "--mode", "run", "--accept-shared-account")
    assert code == 0 and result["ok"] is True and (result["enabled"], result["mode"]) == (True, "run")
    assert result["acceptance"] == {"accepted": utc(NOW), "rows": [1, 2, 4],
                                    "risk_list_sha256": automatic.risk_list_version(), "source": "cli"}
    stored = automatic.read_acceptance(ctx.directory)
    assert stored["risks"] == automatic.risks() and stored["version"] == 1
    assert stat.S_IMODE(automatic.acceptance_path(ctx.directory).stat().st_mode) == 0o600
    settings = autodev_config.load(ctx.directory)
    assert (settings["enabled"], settings["mode"]) == (True, "run")
    assert call(ctx, "autodev", "ready")[0] == 0
    # A changed risk list asks for a new acceptance, which enabling again records.
    changed = ((1, "A model read the owner's keys", automatic.RESIDUAL_RISKS[0][2]), *automatic.RESIDUAL_RISKS[1:])
    monkeypatch.setattr(automatic, "RESIDUAL_RISKS", changed)
    assert call(ctx, "autodev", "ready")[0] == 1
    assert call(ctx, "autodev", "enable", "--mode", "run", "--accept-shared-account")[0] == 0
    assert call(ctx, "autodev", "ready")[0] == 0
    assert automatic.read_acceptance(ctx.directory)["risk_list_sha256"] == automatic.risk_list_version()


def test_dry_run_is_enabled_as_before_without_any_check(ctx):
    code, result = call(ctx, "autodev", "enable", "--mode", "dry-run")
    assert code == 0 and result["mode"] == "dry-run" and "acceptance" not in result
    assert ctx.world["transport"].requests == [] and not automatic.acceptance_path(ctx.directory).exists()


# --- the tick -----------------------------------------------------------------------------------

def run_mode(ctx, **state) -> None:
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True, "mode": "run"}, ctx.directory)
    path = ctx.directory / "schedules" / "tick.json"
    path.write_text(json.dumps({"enabled_at": "2026-10-14T00:00:00+03:00", **state}))


def test_a_scheduled_run_starts_while_ready_passes(ctx):
    ready_now(ctx)
    run_mode(ctx)
    code, result = call(ctx, "tick")
    assert code == 0 and result["tick"]["action"] == "run"
    [argv] = ctx.world["supervised"]
    assert argv[argv.index("--mode") + 1] == "run" and "--pr" not in argv
    assert ctx.world["transport"].requests == [RULES, WORKFLOW, SECRETS]  # checked at tick time


def test_a_continuation_resumes_while_ready_passes(ctx):
    ready_now(ctx)
    run_mode(ctx, last_slot="2026-10-14T02:17:00+03:00",
             next_attempt={"at": "2026-10-14T02:00:00+03:00", "pr": 410})
    code, result = call(ctx, "tick")
    assert result["tick"]["trigger"]["kind"] == "continuation"
    [argv] = ctx.world["supervised"]
    assert argv[argv.index("--mode") + 1] == "resume" and argv[argv.index("--pr") + 1] == "410"


def test_a_scheduled_run_is_refused_and_recorded_when_the_acceptance_is_no_longer_current(ctx, monkeypatch):
    ready_now(ctx)
    run_mode(ctx)
    monkeypatch.setattr(automatic, "RESIDUAL_RISKS", automatic.RESIDUAL_RISKS[:2])
    code, result = call(ctx, "tick")
    assert code == 0 and result["tick"]["action"] == "run" and ctx.world["supervised"] == []
    run = result["run"]
    assert run["status"] == "refused" and run["mode"] == "run" and run["trigger"]["kind"] == "slot"
    assert run["reason"].startswith("automatic mode not ready: the owner's acceptance of the residual risks")
    assert records.read_record(run["run_id"], ctx.directory)["missing"] == run["missing"]
    [argv] = ctx.world["notifications"]
    assert argv[-2:] == ["Autodev run refused", f"details in run {run['run_id']}."]
    assert run["notification"] == {"sent": True, "title": "Autodev run refused"}
    state = json.loads((ctx.directory / "schedules" / "tick.json").read_text())
    assert state["last_slot"] == "2026-10-14T02:17:00+03:00"  # handled once, not every five minutes


def test_a_scheduled_dry_run_is_unchanged(ctx):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, ctx.directory)
    (ctx.directory / "schedules" / "tick.json").write_text(json.dumps({"enabled_at": "2026-10-14T00:00:00+03:00"}))
    code, result = call(ctx, "tick")
    [argv] = ctx.world["supervised"]
    assert argv[argv.index("--mode") + 1] == "dry-run" and ctx.world["transport"].requests == []


# --- the schedule off: tripwire and failed probe --------------------------------------------------

@pytest.mark.parametrize("cause, phrase", [
    (decoys.TRIPWIRE, "a tripwire fired and turned the schedule off"),
    (decoys.PROBE, "a sandbox probe failed and turned the schedule off"),
])
def test_a_command_that_turned_the_schedule_off_notifies_with_fixed_phrases(ctx, cause, phrase):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, ctx.directory)
    run_id = records.new_run_id(NOW)
    entry = decoys.trip(ctx.directory, "the reason stays in tripwire.json", run_id=run_id, cause=cause)
    assert entry["cause"] == cause and autodev_config.load(ctx.directory)["enabled"] is False
    record = {"status": "refused", "mode": "dry-run", "reason": "sandbox probes failed"}
    runner.note_schedule_off(ctx, run_id, record)
    [argv] = ctx.world["notifications"]
    assert argv[:7] == ["/usr/bin/osascript", "-e", "on run argv", "-e",
                        "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run"]
    assert argv[7:] == ["Autodev schedule off", f"{phrase}; details in run {run_id}."]
    assert record["schedule_off"] == cause and record["notification"] == {"sent": True, "title": "Autodev schedule off"}
    assert "reason stays" not in json.dumps(argv)


def test_a_trip_by_another_run_is_not_this_commands(ctx):
    decoys.trip(ctx.directory, "another run", run_id=records.new_run_id(NOW - timedelta(hours=1)))
    record = {"status": "completed"}
    runner.note_schedule_off(ctx, records.new_run_id(NOW), record)
    assert record == {"status": "completed"} and ctx.world["notifications"] == []
    assert decoys.tripped_by(ctx.directory, None) is None


def test_an_old_tripwire_entry_without_a_cause_counts_as_a_tripwire(ctx):
    path = ctx.directory / "schedules" / decoys.TRIPWIRE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"count": 1, "last": {"time": utc(NOW), "reason": "x", "run_id": "R"}}))
    assert decoys.tripped_by(ctx.directory, "R")["cause"] == decoys.TRIPWIRE
    with pytest.raises(ValueError):
        decoys.trip(ctx.directory, "x", cause="something else")


def test_the_report_and_should_notify_carry_the_schedule_off():
    record = {"mode": "dry-run", "status": "completed", "run_id": "R", "schedule_off": decoys.PROBE}
    assert run_report.should_notify(record) is True
    found = run_report.build_report(record)
    assert found["schedule_off"] == "probe"
    assert run_report.build_report({**record, "schedule_off": "anything"})["schedule_off"] is None
    assert run_report.notification(found) == ("Autodev schedule off",
                                              "a sandbox probe failed and turned the schedule off; details in run R.")
