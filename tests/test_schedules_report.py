"""The run report and the notification (src/schedules/report.py, #347).

Records are written by hand in the documented formats, plus one review by the fake claude worker
(the World of test_schedules_workers) for the real attempt-record format. osascript never runs:
a fake runner records the argv.
"""
from datetime import datetime, timezone
import json
import subprocess

import pytest

from src.daemon.state import write_json
from src.schedules import broker, records, report, run_state, workers
from tests.test_schedules_workers import RUN_ID, finish, head_of, repo, world  # noqa: F401 (fixtures)

SHA, OTHER = "a" * 40, "b" * 40
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
SECRET_TITLE = "The deploy key is logged by the runner"
TOKEN = "fake-report-token-value"


def state(**changes) -> run_state.RunState:
    values = dict(run_id=RUN_ID, mode="run", repository="IEZhu/Agents", pinned=SHA, runner_commit=OTHER,
                  stage="implement", stages_done=["select"], issue=347, branch="claude/autodev-347-run", base=SHA,
                  slice={"this_pr": "Part A", "remaining": ["Part B"]},
                  criteria_refs=["https://github.com/IEZhu/Agents/issues/347"],
                  flows={"flows/autodev-implement.md": "c" * 40, "flows/pr-review.md": None},
                  limits={"max_reviews": 3, "max_bot_rounds": 3, "max_wait_minutes": 120})
    values.update(changes)
    return run_state.RunState(**values)


# --- the report ----------------------------------------------------------------------------------------

def attempt(handle, number, verdict, *, requested, applied, model, effort, fallback=None):
    return {"task_id": f"autodev-{RUN_ID}-{handle}", "attempt_id": str(number), "verdict": verdict,
            "requested": requested, "client": {"client_requested": requested["client"], "client_applied": applied,
                                               "fallback_reason": fallback, "detail": ""},
            "model": model, "effort": effort,
            "package": {"bundle_revision": f"{number}" * 64, "contract_revision": "e" * 64}}


@pytest.fixture
def run_dir(tmp_path):
    directory = records.run_dir(RUN_ID, tmp_path)
    work = directory / workers.WORK_DIR
    codex = {"client": "codex", "model": "gpt-6.1-sol", "effort": "high",
             "fallback": {"model": "opus", "effort": "high"}}
    claude = {"client": "claude", "model": "opus", "effort": "high", "fallback": {"model": "opus", "effort": "high"}}
    write_json(work / "review-1" / "attempt-1.json", attempt("review-1", 1, "retry", requested=codex, applied="claude",
                                                               model="opus", effort="high",
                                                               fallback="nested_sandbox_unavailable"))
    write_json(work / "review-1" / "attempt-2.json", attempt("review-1", 2, "accepted", requested=codex,
                                                               applied="claude", model="opus", effort="high",
                                                               fallback="nested_sandbox_unavailable"))
    write_json(work / "premortem-1" / "attempt-1.json", attempt("premortem-1", 1, "accepted", requested=claude,
                                                                  applied="claude", model="opus", effort="high"))
    write_json(directory / workers.STATE_NAME, {"run_id": RUN_ID, "tasks": [
        {"handle": "review-1", "kind": "review", "status": "accepted", "head": SHA, "attempts": [{}, {}]},
        {"handle": "premortem-1", "kind": "premortem", "status": "accepted", "head": SHA, "attempts": [{}]}]})
    write_json(directory / workers.DECISIONS_NAME, {"run_id": RUN_ID, "decisions": [
        {"key": "k1", "decision": "fixed", "handle": "review-1", "time": "2026-10-14T12:00:00+00:00", "history": []},
        {"key": "k2", "decision": "known_risk", "reason": "later", "handle": "premortem-1", "history": [
            {"decision": "declined", "reason": "no", "handle": "review-1", "time": "2026-10-14T11:00:00+00:00"}]},
        {"key": "k3", "decision": "known_risk", "reason": "kept", "handle": "premortem-1", "history": []}]})
    write_json(directory / broker.PREMORTEM_RECORD, {"validated": True, "findings": [
        {"id": "k3", "level": "high", "title": SECRET_TITLE, "body": "The key is printed.", "security": True},
        {"id": "k2", "level": "low", "title": "A slow test", "body": "It takes long.", "security": False}]})
    return directory


def record(**changes):
    found = {"run_id": RUN_ID, "mode": "run", "status": "completed", "pinned": SHA, "runner_commit": OTHER,
             "gate": {"decision": "select", "reason": "nothing stands in the way", "actions": []},
             "selection": {"decision": "task", "issue": 347, "scores": [{"issue": 347}, {"issue": 348}],
                           "rationale": "first"},
             "issue": 347, "pr": 501, "pr_url": "https://github.com/IEZhu/Agents/pull/501",
             "review_status": "waiting_bots", "next_attempt": "2026-10-14T12:15:00Z",
             "stages": {kind: {"status": "completed", "reason": None, "cost_usd": cost, "execution": {
                 "requested": {"client": "claude", "model": "opus", "effort": effort, "method": "session"},
                 "applied": {"client": "claude", "model": "opus", "effort": effort, "method": "session",
                             "model_reported": "claude-opus-4-5", "fallback_reason": None}}}
                 for kind, cost, effort in (("select", 0.5, "high"), ("implement", 4.25, "xhigh"),
                                            ("submit", 1.0, "high"))},
             "checks": [{"name": "regular suite", "command": "pytest tests/ -q", "status": "passed", "detail": "ok"}],
             "risk_issues": [{"number": 610, "finding": "k9"}], "exports": None, "blockers": []}
    found.update(changes)
    return found


def test_the_report_has_requested_and_applied_execution_per_stage_and_step(run_dir):
    built = report.build_report(record(), state().to_dict(NOW), run_dir)
    assert [stage["kind"] for stage in built["stages"]] == ["select", "implement", "submit"]
    for stage in built["stages"]:
        assert set(stage["execution"]) == {"requested", "applied"}
        assert stage["execution"]["requested"]["method"] == stage["execution"]["applied"]["method"] == "session"
    review, premortem = built["steps"]
    assert (review["handle"], review["attempts"], review["status"]) == ("review-1", 2, "accepted")
    assert review["execution"] == {
        "requested": {"client": "codex", "model": "gpt-6.1-sol", "effort": "high", "method": "delegate"},
        "applied": {"client": "claude", "model": "opus", "effort": "high", "method": "delegate",
                    "fallback_reason": "nested_sandbox_unavailable"}}
    assert premortem["execution"]["requested"]["client"] == premortem["execution"]["applied"]["client"] == "claude"
    assert review["decisions"] == {"fixed": 1, "declined": 1, "mitigated": 0, "known_risk": 0}
    assert premortem["decisions"] == {"fixed": 0, "declined": 0, "mitigated": 0, "known_risk": 2}
    assert [package["handle"] for package in built["packages"]] == ["review-1", "review-1", "premortem-1"]
    assert built["packages"][0]["bundle_revision"] == "1" * 64
    assert built["flows"] == {"flows/autodev-implement.md": "c" * 40, "flows/pr-review.md": None}
    assert built["gate"] == {"decision": "select", "reason": "nothing stands in the way"}
    assert built["selection"] == {"decision": "task", "issue": 347, "top_five": [347, 348]}
    assert built["checks"] == [{"name": "regular suite", "status": "passed"}]
    assert built["cost_usd_total"] == 5.75 and built["risk_issues"] == {"created": [610], "count": 1}
    assert built["security"] == {"kept_local": 1, "open": 1}
    assert SECRET_TITLE not in json.dumps(built)  # security findings appear as counts only


def test_the_report_without_a_run_directory_or_stages(tmp_path):
    built = report.build_report({"run_id": RUN_ID, "mode": "dry-run", "status": "completed",
                                 "selection": "not started"})
    assert built["steps"] == built["packages"] == built["stages"] == [] and built["selection"] == "not started"
    assert built["cost_usd_total"] is None and built["security"] == {"kept_local": 0, "open": 0}


def test_the_report_masks_known_secrets(run_dir):
    built = report.build_report(record(blockers=[f"the token {TOKEN} leaked"]), None, run_dir, known=[TOKEN])
    assert TOKEN not in json.dumps(built)


def test_the_report_of_a_real_review(world):
    service = world.service()
    finish(service, service.review({"head": head_of(world)}))
    built = report.build_report({"run_id": RUN_ID, "mode": "run", "status": "completed"}, None, world.run_dir)
    [step] = built["steps"]
    assert step["execution"]["requested"] == {"client": "claude", "model": "opus", "effort": "high",
                                              "method": "delegate"}
    assert step["execution"]["applied"] == {"client": "claude", "model": "opus", "effort": "high",
                                            "method": "delegate", "fallback_reason": None}
    assert built["packages"][0]["bundle_revision"] == world.packages[-1].contract.bundle_revision


def test_selection_view_for_the_dry_run_printout():
    scores = [{"issue": number, "value": 3, "urgency": 2, "unblocking": 1, "size": 2, "readiness": 1, "next": False,
               "score": 3.0 - number / 10, "rationale": f"r{number}"} for number in range(1, 7)]
    view = report.selection_view({"decision": "task", "issue": 1, "slice": {"this_pr": "x", "remaining": []},
                                  "rationale": "best", "scores": scores}, "IEZhu/Agents")
    assert len(view["top_five"]) == 5 and view["issue"] == 1 and view["rationale"] == "best"
    assert len(view["table"].splitlines()) == 7 and "[#1](https://github.com/IEZhu/Agents/issues/1)" in view["table"]


# --- the notification ------------------------------------------------------------------------------------

class Runner:
    def __init__(self, code=0, error=None):
        self.code, self.error, self.calls = code, error, []

    def __call__(self, argv, **options):
        self.calls.append((argv, options))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.code, "", "")


def test_notification_is_a_fixed_vocabulary_without_security_details(run_dir):
    built = report.build_report(record(blockers=["an open pre-mortem risk needs the owner"],
                                       reason=f"tripwire: {SECRET_TITLE}"), None, run_dir)
    title, message = report.notification(built)
    assert title == "Autodev run completed"
    assert message == ("issue #347; PR https://github.com/IEZhu/Agents/pull/501; review waiting_bots; "
                       "continue with: run-now --mode resume --pr 501; 1 blocker(s) on the issue; "
                       f"details in run {RUN_ID}.")
    for detail in (SECRET_TITLE, "pre-mortem", "tripwire", "kept_local", "security"):
        assert detail not in title + message


def test_notification_names_the_exports_and_the_outcome():
    exports = {"patch": f"/state/runs/{RUN_ID}/export/{RUN_ID}.patch",
               "bundle": f"/state/runs/{RUN_ID}/export/{RUN_ID}.bundle", "sha256": {}}
    title, message = report.notification({"run_id": RUN_ID, "mode": "run", "status": "blocked", "issue": 347,
                                          "exports": exports, "blockers": ["needs a control path"]})
    assert title == "Autodev run blocked" and exports["patch"] in message and exports["bundle"] in message
    title, _ = report.notification({"run_id": RUN_ID, "mode": "dry-run", "status": "failed: infra"})
    assert title == "Autodev dry-run failed"
    _, masked = report.notification({"run_id": RUN_ID, "mode": "run", "status": "completed",
                                     "pr_url": f"https://github.com/IEZhu/Agents/pull/{TOKEN}"}, known=[TOKEN])
    assert TOKEN not in masked


def test_notify_passes_the_data_as_arguments_only():
    runner = Runner()
    title, message = 'Autodev "run" done', "line one\nline two \"quoted\" & 'single' -- end\x07"
    assert report.notify(title, message, runner=runner, platform="darwin") is True
    [(argv, options)] = runner.calls
    assert argv[:7] == ["/usr/bin/osascript", "-e", "on run argv",
                        "-e", "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run"]
    assert argv[7:] == ['Autodev "run" done', "line one line two \"quoted\" & 'single' -- end"]
    script = " ".join(argv[1:7])
    assert "quoted" not in script and "line one" not in script and "Autodev" not in script
    assert options["stdin"] == subprocess.DEVNULL and options["env"]["PATH"] == "/usr/bin:/bin"
    assert "shell" not in options


def test_notify_never_lets_data_look_like_an_option():
    argv = report.notify_argv("-e do shell script", "--help")
    assert argv[7:] == ["e do shell script", "help"] and argv.count("-e") == 3


def test_notify_failures_return_false_and_other_platforms_do_nothing():
    assert report.notify("t", "m", runner=Runner(code=1), platform="darwin") is False
    assert report.notify("t", "m", runner=Runner(error=OSError("no osascript")), platform="darwin") is False
    assert report.notify("t", "m", runner=Runner(error=subprocess.TimeoutExpired("osascript", 30)),
                         platform="darwin") is False
    idle = Runner()
    assert report.notify("t", "m", runner=idle, platform="linux") is False and idle.calls == []


@pytest.mark.parametrize("found, expected", [
    ({"mode": "run", "status": "completed"}, True), ({"mode": "resume", "status": "completed"}, True),
    ({"mode": "dry-run", "status": "completed"}, False), ({"mode": "dry-run", "status": "failed: infra"}, True),
    ({"mode": "dry-run", "status": "completed", "tripwire": {"reason": "x"}}, True),
])
def test_should_notify(found, expected):
    assert report.should_notify(found) is expected
