"""The automatic mode (#350, src/schedules/automatic.py and runner.py): `autodev ready`, the owner's
acceptance, `enable --mode run --accept-shared-account`, the tick's refusal of a scheduled real run,
the child's check before the sequence and the schedule-off notification.

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
    assert {kind: found["status"] for kind, found in result["probe_freshness"].items()} == {
        "implement": "passed", "submit": "passed"}  # select has no Bash: no fresh record asked for
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


@pytest.mark.parametrize("state", ["missing", "failed", "future"])
def test_ready_names_a_missing_failed_or_future_probe_record(ctx, state):
    ready_now(ctx)
    run_id = None
    if state == "missing":
        for record in records.all_records(ctx.directory):
            if record["probes"][0]["kind"] == "implement" and record["probes"][0]["mode"] == "scheduled":
                (records.run_dir(record["run_id"], ctx.directory) / records.RECORD_NAME).unlink()
    elif state == "failed":
        run_id = record_probe(ctx, "implement", "scheduled", ok=False)
    else:
        run_id = record_probe(ctx, "implement", "scheduled", age=-timedelta(hours=1))
    code, result = call(ctx, "autodev", "ready")
    command = "`python -m src.schedules probe --mode scheduled --kind implement --confirm-live-probe`"
    expected = {"missing": f"sandbox probe implement in scheduled mode: no record; run {command}",
                "failed": f"sandbox probe implement in scheduled mode: failed in run {run_id}; run {command} once it "
                          "is fixed",
                "future": f"sandbox probe implement in scheduled mode: the passing record (run {run_id}) is dated in "
                          f"the future or has no time; run {command}"}[state]
    assert code == 1 and result["missing"] == [expected]  # a missing or failed pair hides the freshness item
    assert result["probes"]["implement"]["scheduled"]["status"] == ("stale" if state == "future" else state)
    assert result["probes"]["implement"]["resume"]["status"] == "passed"


def test_every_mode_once_but_only_the_kinds_with_bash_must_be_fresh(ctx):
    """The freshness rule (#350 review): each (kind, mode) needs a pass once (A7), at any age;
    implement and submit, which every run probes again, need a pass in the last 7 days in any mode;
    select (no Bash) does not."""
    accept(ctx)
    for kind in stages.KINDS:
        for mode in MODES:
            record_probe(ctx, kind, mode, age=timedelta(days=30))
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["missing"] == [
        f"sandbox probe {kind}: the newest passing record (run {result['probe_freshness'][kind]['run_id']}, resume "
        f"mode) is not from the last 7 days; a run's guarded {kind} session refreshes it, or run `python -m "
        f"src.schedules probe --mode scheduled --kind {kind} --confirm-live-probe`" for kind in ("implement", "submit")]
    assert all(result["probes"][kind][mode]["status"] == "passed" for kind, mode in PAIRS)  # no age for A7
    # A scheduled run's guarded implement and submit sessions refresh them; a resume's refreshes submit too.
    record_probe(ctx, "implement", "scheduled", guarded=True)
    passed = record_probe(ctx, "submit", "resume", guarded=True)
    code, result = call(ctx, "autodev", "ready")
    assert code == 0, result["missing"]
    assert result["probe_freshness"]["submit"] == {"status": "passed", "run_id": passed, "mode": "resume",
                                                   "finished": utc(NOW - timedelta(hours=2))}
    # A week without such a session names exactly what to probe again.
    record_probe(ctx, "submit", "run-now", age=timedelta(days=8))
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and len(result["missing"]) == 1
    assert result["missing"][0].startswith("sandbox probe submit: the newest passing record (run ")
    assert "run-now mode) is not from the last 7 days" in result["missing"][0]
    assert result["probes"]["submit"]["run-now"]["status"] == "passed"


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


def test_a_static_only_pass_neither_replaces_a_failure_nor_refreshes_an_old_probe(ctx):
    """Kills mutant M03 (every report counts): the newer static-only passes must change nothing."""
    ready_now(ctx)
    failed = record_probe(ctx, "select", "scheduled", ok=False)
    record_probe(ctx, "select", "scheduled", ran_session=False, guarded=True)
    record_probe(ctx, "implement", "dry-run", age=timedelta(days=8))  # implement's newest probe session: old
    record_probe(ctx, "implement", "scheduled", ran_session=False, guarded=True)
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["probes"]["select"]["scheduled"]["status"] == "failed"
    assert result["probes"]["select"]["scheduled"]["run_id"] == failed
    assert result["probe_freshness"]["implement"]["status"] == "stale"
    assert result["probe_freshness"]["implement"]["mode"] == "dry-run"


def test_a_codex_select_stage_needs_claude_probes_only_while_a_run_falls_back_to_claude(ctx):
    """A run starts a Claude select session when the stored Codex availability says unavailable
    (`stages.stage_profile`), so ready asks for that session's probes then (#350 review)."""
    settings = copy.deepcopy(autodev_config.DEFAULTS)
    settings["stages"]["select"] = {"client": "codex", "codex": {"model": "gpt-6.1-sol", "effort": "high"}}
    autodev_config.save(settings, ctx.directory)
    record_every_probe(ctx, kinds=("implement", "submit"))
    accept(ctx)
    ctx.world["codex"]["select"] = {"available": True, "reason": None, "checked": "2026-10-13T10:00:00+00:00"}
    code, result = call(ctx, "autodev", "ready")
    assert code == 0 and "select" not in result["probes"]
    assert result["codex"] == {"select": {"available": True, "reason": None, "checked": "2026-10-13T10:00:00+00:00",
                                          "client": "codex"}}
    ctx.world["codex"]["select"] = {"available": False, "reason": "nested_sandbox_unavailable", "checked": "x"}
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["missing"] == [
        f"sandbox probe select in {mode} mode: no record; run `python -m src.schedules probe --mode {mode} --kind "
        f"select --confirm-live-probe`" for mode in MODES]
    assert result["codex"]["select"] == {"available": False, "reason": "nested_sandbox_unavailable", "checked": "x",
                                         "client": "claude"}
    record_every_probe(ctx, kinds=("select",))
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


def test_the_probe_command_probes_the_claude_select_session_a_run_falls_back_to(ctx, monkeypatch):
    settings = copy.deepcopy(autodev_config.DEFAULTS)
    settings["stages"]["select"] = {"client": "codex", "codex": {"model": "gpt-6.1-sol", "effort": "high"}}
    autodev_config.save(settings, ctx.directory)
    config = autodev_config.load(ctx.directory)
    ctx.world["codex"]["select"] = {"available": True, "reason": None, "checked": "2026-10-13T10:00:00+00:00"}
    refused = runner._probe(ctx, config, records.new_run_id(NOW), "scheduled", "select", [])
    assert refused["status"] == "refused" and "`codex-probe --kind stage` checks it" in refused["reason"]
    # Codex unavailable: a run starts a Claude select session, which the command probes (here it
    # stops at the missing claude executable, past the client check).
    ctx.world["codex"]["select"] = {"available": False, "reason": "nested_sandbox_unavailable", "checked": "x"}
    monkeypatch.setattr(runner, "claude_executable", lambda context: None)
    assert runner._probe(ctx, config, records.new_run_id(NOW), "scheduled", "select", []) == runner.infra(
        "cli: the claude executable was not found (tools.json, PATH)")


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
    record_every_probe(ctx)
    decoys.trip(ctx.directory, "an earlier probe", now=NOW - timedelta(hours=3))
    accept(ctx)  # given after that trip: current
    assert automatic.read_acceptance(ctx.directory)["tripwire_count"] == 1
    assert call(ctx, "autodev", "ready")[0] == 0
    decoys.trip(ctx.directory, "stage session: tool call 1 (Read) names decoy token", now=NOW - timedelta(minutes=5))
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and len(result["missing"]) == 1
    assert result["missing"][0].startswith("the owner's acceptance of the residual risks (plan section 11, rows 1, 2 "
                                           "and 4): a tripwire turned the schedule off after it (")


def test_a_trip_since_the_acceptance_counts_whatever_its_date(ctx):
    """Clock-independent (#350 review): the count changed, though the entry is dated before it."""
    ready_now(ctx)
    decoys.trip(ctx.directory, "a trip written with a clock behind", now=NOW - timedelta(days=2))
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and "a tripwire turned the schedule off after it" in result["missing"][0]


def test_a_trip_in_the_same_second_as_the_acceptance_makes_it_stale(ctx):
    """Kills mutant M02 (``>=`` to ``>``): the count is the same (the trip came first), the times equal."""
    record_every_probe(ctx)
    decoys.trip(ctx.directory, "a trip racing with enable", now=NOW - timedelta(hours=1))
    accept(ctx, NOW - timedelta(hours=1))
    assert automatic.read_acceptance(ctx.directory)["tripwire_count"] == 1
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["missing"] == [
        f"{automatic.ACCEPTANCE_ITEM}: a tripwire turned the schedule off after it ({utc(NOW - timedelta(hours=1))}); "
        f"check the run records, then accept again with `{automatic.ENABLE_COMMAND}`"]


@pytest.mark.parametrize("damage", ["not json", "no count", "a list", "a directory"])
def test_an_unreadable_tripwire_file_makes_the_acceptance_stale(ctx, damage):
    """Fails closed (#350 review): a tripwire that cannot be ruled out counts."""
    ready_now(ctx)
    path = ctx.directory / "schedules" / decoys.TRIPWIRE_NAME
    if damage == "a directory":
        path.mkdir()
    else:
        path.write_text({"not json": "{", "no count": json.dumps({"last": {"time": utc(NOW)}}),
                         "a list": "[1]"}[damage])
    assert decoys.trip_count(ctx.directory) is None
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["missing"] == [
        f"{automatic.ACCEPTANCE_ITEM}: tripwire.json cannot be read, so a tripwire since it cannot be ruled out; "
        f"check it and the run records, then accept again with `{automatic.ENABLE_COMMAND}`"]
    code, result = call(ctx, "autodev", "enable", "--mode", "run", "--accept-shared-account")
    assert code == 1 and result["error"] == "not_ready"
    assert result["missing"] == ["tripwire.json cannot be read; check it and the run records"]


def test_an_acceptance_dated_in_the_future_is_stale(ctx):
    record_every_probe(ctx)
    accept(ctx, NOW + timedelta(hours=2))  # the clock was ahead when it was given
    decoys.trip(ctx.directory, "a later trip, dated before the bad acceptance", now=NOW)
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["missing"][0].startswith(f"{automatic.ACCEPTANCE_ITEM}: dated in the future (")
    automatic.record_acceptance(ctx.directory, NOW + timedelta(hours=2), source="cli")  # no trip since: still future
    assert automatic.acceptance_problems(ctx.directory, NOW)[0].startswith(f"{automatic.ACCEPTANCE_ITEM}: dated in the "
                                                                          "future (")


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
    assert stored["risks"] == automatic.risks() and stored["version"] == 2 and stored["tripwire_count"] == 0
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


def test_a_trip_while_enable_checks_the_conditions_refuses_it(ctx):
    """security-1: a concurrent run trips during the GitHub reads; enable must not write run mode over it."""
    record_every_probe(ctx)
    transport = ctx.world["transport"]

    def tripping(url, headers):
        if not transport.requests:  # the first GitHub read: a concurrent run reads a decoy meanwhile
            decoys.trip(ctx.directory, "stage session: tool call 1 (Read) names decoy token",
                        run_id=records.new_run_id(NOW - timedelta(minutes=1)))
        return transport(url, headers)
    ctx.client_factory = lambda config: GitHubClient(config["repository"], lambda: TOKEN, transport=tripping)
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, ctx.directory)  # dry-run, then the trip
    code, result = call(ctx, "autodev", "enable", "--mode", "run", "--accept-shared-account")
    assert code == 1 and result["error"] == "tripped_during_enable"
    settings = autodev_config.load(ctx.directory)
    assert (settings["enabled"], settings["mode"]) == (False, "dry-run")
    assert automatic.read_acceptance(ctx.directory) is None
    assert transport.requests == [RULES, WORKFLOW, SECRETS]
    # Enabling again once the owner has looked: the trip is counted into the new acceptance.
    ctx.client_factory = lambda config: GitHubClient(config["repository"], lambda: TOKEN, transport=transport)
    assert call(ctx, "autodev", "enable", "--mode", "run", "--accept-shared-account")[0] == 0
    assert automatic.read_acceptance(ctx.directory)["tripwire_count"] == 1


def test_the_acceptance_is_dated_when_the_checks_began(ctx):
    record_every_probe(ctx)
    ticks = iter(range(1000))
    ctx.clock = lambda: NOW + timedelta(seconds=next(ticks))
    code, result = call(ctx, "autodev", "enable", "--mode", "run", "--accept-shared-account")
    assert code == 0 and result["acceptance"]["accepted"] == utc(NOW)  # the first reading, before readiness


def test_disable_and_dry_run_withdraw_the_acceptance_and_a_hand_edit_does_not_revive_it(ctx):
    """security-3: the acceptance is bound to the act of enabling run mode."""
    record_every_probe(ctx)
    assert call(ctx, "autodev", "enable", "--mode", "run", "--accept-shared-account")[0] == 0
    assert call(ctx, "autodev", "disable")[0] == 0
    stored = automatic.read_acceptance(ctx.directory)
    assert (stored["revoked"], stored["revoked_by"]) == (utc(NOW), "disable")
    run_mode(ctx)  # autodev.json edited back to run mode by hand
    code, result = call(ctx, "tick")
    assert ctx.world["supervised"] == [] and result["run"]["status"] == "refused"
    assert result["run"]["missing"] == [
        f"{automatic.ACCEPTANCE_ITEM}: withdrawn when the schedule was turned off or set to dry-run ({utc(NOW)}); "
        f"accept again with `{automatic.ENABLE_COMMAND}`"]
    assert call(ctx, "autodev", "enable", "--mode", "run", "--accept-shared-account")[0] == 0
    assert call(ctx, "autodev", "ready")[0] == 0
    assert call(ctx, "autodev", "enable", "--mode", "dry-run")[0] == 0
    assert automatic.read_acceptance(ctx.directory)["revoked_by"] == "dry-run"
    assert "withdrawn" in call(ctx, "autodev", "ready")[1]["missing"][0]
    assert automatic.revoke_acceptance(ctx.directory, NOW, by="disable") is False  # once only


def test_ready_records_its_outcome_for_the_settings_page(ctx):
    code, result = call(ctx, "autodev", "ready")
    assert code == 1 and result["recorded"] == utc(NOW)
    assert automatic.read_ready(ctx.directory) == {"version": 1, "checked": utc(NOW), "ready": False,
                                                   "missing": result["missing"]}
    assert stat.S_IMODE(automatic.ready_path(ctx.directory).stat().st_mode) == 0o600
    assert automatic.page_problems(ctx.directory, NOW)[:2] == [
        "the last `python -m src.schedules autodev ready` (2026-10-13T23:20:00+00:00) did not pass", result["missing"][0]]
    ready_now(ctx)
    assert call(ctx, "autodev", "ready")[0] == 0
    assert automatic.page_problems(ctx.directory, NOW) == []
    assert automatic.page_problems(ctx.directory, NOW + timedelta(minutes=15)) == []
    old = ["the last passing `python -m src.schedules autodev ready` (2026-10-13T23:20:00+00:00) is not from the last "
           "15 minutes; run it again"]
    assert automatic.page_problems(ctx.directory, NOW + timedelta(minutes=16)) == old
    assert automatic.page_problems(ctx.directory, NOW - timedelta(minutes=1)) == old  # from the future
    decoys.trip(ctx.directory, "a decoy read", now=NOW)
    assert automatic.page_problems(ctx.directory, NOW)[0].startswith(
        f"{automatic.ACCEPTANCE_ITEM}: a tripwire turned the schedule off after it")
    automatic.ready_path(ctx.directory).unlink()
    assert automatic.page_problems(ctx.directory, NOW)[0] == ("`python -m src.schedules autodev ready` has no "
                                                             "recorded result; run it in a terminal")


def test_the_page_source_enables_run_mode_from_stored_state_only(ctx):
    """security-4: ``enable`` from the settings page reads no credential and nothing from GitHub."""
    def no_credentials(config):
        raise AssertionError("the page's enable read the broker credentials")
    ready_now(ctx)
    assert call(ctx, "autodev", "ready")[0] == 0
    requests = list(ctx.world["transport"].requests)
    ctx.credentials = no_credentials
    ctx.client_factory = no_credentials
    code, result = runner.enable(ctx, "run", accept_shared_account=True, source="page")
    assert code == 0 and result["acceptance"]["source"] == "page"
    assert ctx.world["transport"].requests == requests
    automatic.record_acceptance(ctx.directory, NOW - timedelta(hours=1), source="cli")
    ctx.clock = lambda: NOW + timedelta(minutes=16)  # the recorded ready is too old now
    code, result = runner.enable(ctx, "run", accept_shared_account=True, source="page")
    assert code == 1 and result["error"] == "not_ready" and "is not from the last 15 minutes" in result["missing"][0]


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


def test_a_continuation_is_refused_and_recorded_while_ready_fails_naming_its_pr(ctx, monkeypatch):
    """Kills mutant M09 (only ``run`` checked): a scheduled resume must pass ready too; the refusal
    names the PR and the command that resumes it (correctness-12)."""
    ready_now(ctx)
    run_mode(ctx, last_slot="2026-10-14T02:17:00+03:00",
             next_attempt={"at": "2026-10-14T02:00:00+03:00", "pr": 410})
    monkeypatch.setattr(automatic, "RESIDUAL_RISKS", automatic.RESIDUAL_RISKS[:2])
    code, result = call(ctx, "tick")
    assert result["tick"]["trigger"]["kind"] == "continuation" and ctx.world["supervised"] == []
    run = result["run"]
    assert (run["mode"], run["status"], run["pr"]) == ("resume", "refused", 410)
    assert run["pr_url"] == "https://github.com/IEZhu/Agents/pull/410"
    [argv] = ctx.world["notifications"]
    assert argv[-2:] == ["Autodev run refused", "PR https://github.com/IEZhu/Agents/pull/410; continue with: run-now "
                                                f"--mode resume --pr 410; details in run {run['run_id']}."]
    state = json.loads((ctx.directory / "schedules" / "tick.json").read_text())
    assert state["last_continuation"] == "2026-10-14T02:00:00+03:00"  # handled once, not every five minutes


def test_a_trip_after_the_ticks_check_stops_the_scheduled_run_in_the_child(ctx, monkeypatch):
    """security-2: a concurrent probe fails between the tick's check and the child's lease."""
    from src.schedules import sequence
    ready_now(ctx)
    run_mode(ctx)

    def no_sequence(*args, **kwargs):
        raise AssertionError("the sequence started")
    monkeypatch.setattr(sequence, "run_sequence", no_sequence)
    children = []

    def supervise(argv, **kwargs):
        decoys.trip(ctx.directory, "sandbox probes failed: the select probe session in dry-run mode",
                    run_id=records.new_run_id(NOW - timedelta(minutes=1)), cause=decoys.PROBE)
        children.append(runner.run_autodev(ctx, argv[argv.index("--mode") + 1],
                                           run_id=argv[argv.index("--run-id") + 1]))
        return Supervised(children[-1][0], caffeinated=True)
    ctx.supervise = supervise
    code, result = call(ctx, "tick")
    [(child_code, record)] = children
    assert child_code == 1 and record["status"] == "refused" and result["run"]["status"] == "refused"
    assert record["reason"].startswith("automatic mode no longer ready at the start: the schedule was turned off; "
                                       f"{automatic.ACCEPTANCE_ITEM}: a tripwire turned the schedule off after it (")
    assert record["missing"][0] == "the schedule was turned off" and len(record["missing"]) == 2
    [argv] = ctx.world["notifications"]
    assert argv[-2:] == ["Autodev run refused", f"details in run {record['run_id']}."]


def test_a_supervised_run_now_is_not_checked_by_the_automatic_mode(ctx, monkeypatch):
    from src.schedules import sequence
    monkeypatch.setattr(sequence, "run_sequence", lambda *args, **kwargs: {"status": "completed"})
    code, record = runner.run_autodev(ctx, "run")  # no acceptance, schedule off: a manual run is the owner's
    assert code == 0 and record["status"] == "completed"


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


def test_a_run_ended_by_the_supervisor_after_it_tripped_says_schedule_off(ctx):
    """Kills mutant M27: notify_ended must carry the trip's cause into the notification."""
    run_id = records.new_run_id(NOW)
    records.write_record(run_id, {"mode": "run", "status": "running"}, ctx.directory)
    decoys.trip(ctx.directory, "stage session: tool call 3 (Bash) names decoy token", run_id=run_id)
    record = {"run_id": run_id, "mode": "run", "status": "failed: infra", "reason": "timeout: the run exceeded"}
    runner.notify_ended(ctx, record, records.run_dir(run_id, ctx.directory))
    assert record["schedule_off"] == "tripwire"
    assert record["notification"] == {"sent": True, "title": "Autodev schedule off", "by": "supervisor"}
    [argv] = ctx.world["notifications"]
    assert argv[-2:] == ["Autodev schedule off", f"a tripwire fired and turned the schedule off; details in run {run_id}."]


def test_a_command_that_notified_already_is_not_notified_again(ctx):
    """Kills mutant M26: note_schedule_off leaves a record that carries its notification alone."""
    run_id = records.new_run_id(NOW)
    decoys.trip(ctx.directory, "the probe failed", run_id=run_id, cause=decoys.PROBE)
    record = {"status": "refused", "mode": "run", "notification": {"sent": True, "title": "Autodev run refused"}}
    runner.note_schedule_off(ctx, run_id, record)
    assert ctx.world["notifications"] == [] and "schedule_off" not in record


def test_a_trip_records_its_entry_under_the_writers_lock(ctx, monkeypatch):
    """The schedule off and the tripwire entry are written together under the lock that enable
    holds while it compares the count (security-1)."""
    real = decoys._record_trip
    seen = []

    @contextlib.contextmanager
    def writing(directory=None):
        seen.append("enter")
        yield
        seen.append("exit")
    monkeypatch.setattr(autodev_config, "writing", writing)
    monkeypatch.setattr(decoys, "_record_trip", lambda directory, entry: (seen.append("record"), real(directory, entry)))
    decoys.trip(ctx.directory, "a decoy read")
    assert seen == ["enter", "record", "exit"] and decoys.trip_count(ctx.directory) == 1
