"""Offline run observations: wait in the process, never weaken the CI gate."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("watch_github_run", ROOT / "scripts/dev/watch_github_run.py")
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
SHA, REPO, RUN = "a" * 40, "owner/repo", 42


def run(status="completed", conclusion="success", **changes):
    return {"id": RUN, "head_sha": SHA, "run_attempt": 1, "repository": {"full_name": REPO},
            "status": status, "conclusion": conclusion, **changes}


def job(number=1, **changes):
    return {"id": number, "run_id": RUN, "head_sha": SHA, "status": "completed", "conclusion": "success", **changes}


class Clock:
    now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def execute(tmp_path, responses, **kwargs):
    calls, clock = [], Clock()
    items = iter(responses)

    def api(endpoint, timeout):
        calls.append((endpoint, timeout))
        value = next(items)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    result = watch.observe(REPO, RUN, SHA, 1, tmp_path, api=api, clock=clock, sleep=clock.sleep, **kwargs)
    assert json.loads((tmp_path / "result.json").read_text()) == result
    return result, calls


def test_repeated_waits_emit_nothing_and_preserve_final_jobs(tmp_path, capsys):
    waiting = [run("in_progress", None) for _ in range(100)]
    result, calls = execute(tmp_path, [*waiting, run(), {"total_count": 1, "jobs": [job()]}, run()], seconds=55, interval=0.5)
    assert result["outcome"] == "success" and result["polls"] == 101
    assert capsys.readouterr().out == ""
    assert len(calls) == 103
    assert json.loads((tmp_path / "jobs.json").read_text())[0]["jobs"] == [job()]


def test_pending_timeout_never_becomes_success(tmp_path):
    result, calls = execute(tmp_path, [run("queued", None)] * 3, seconds=3, interval=1)
    assert result["outcome"] == "pending" and len(calls) == 3


@pytest.mark.parametrize("changes", [{"head_sha": "b" * 40}, {"run_attempt": 2}, {"id": 43},
                                    {"repository": {"full_name": "another/repo"}}])
def test_identity_change_is_not_passing_evidence(tmp_path, changes):
    result, calls = execute(tmp_path, [run(**changes)])
    assert result["outcome"] == "unverified" and len(calls) == 1


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "neutral", "skipped", "timed_out"])
def test_non_success_jobs_cannot_clear_the_gate(tmp_path, conclusion):
    result, _ = execute(tmp_path, [run(), {"total_count": 1, "jobs": [job(conclusion=conclusion)]}, run()])
    assert result["outcome"] == "attention" and result["attention"][0]["conclusion"] == conclusion


def test_all_job_pages_are_read(tmp_path):
    result, calls = execute(tmp_path, [run(), {"total_count": 2, "jobs": [job()]},
                                      {"total_count": 2, "jobs": [job(2)]}, run()], expected_jobs=2)
    assert result["outcome"] == "success" and result["jobs"] == 2
    assert "attempts/1/jobs?per_page=100&page=2" in calls[2][0]


@pytest.mark.parametrize("pages", [
    [{"total_count": 0, "jobs": []}],
    [{"total_count": 2, "jobs": [job()]}, {"total_count": 2, "jobs": []}],
    [{"total_count": 2, "jobs": [job(), job()]}],
    [{"total_count": 1, "jobs": [job(head_sha="b" * 40)]}],
    [{"total_count": 1, "jobs": [job(run_id=43)]}],
    [{"total_count": 2, "jobs": [job()]}, {"total_count": 3, "jobs": [job(2)]}],
])
def test_missing_duplicate_or_mismatched_jobs_fail_closed(tmp_path, pages):
    result, _ = execute(tmp_path, [run(), *pages])
    assert result["outcome"] == "unverified"


def test_rerun_during_page_read_requires_new_attempt(tmp_path):
    result, _ = execute(tmp_path, [run(), {"total_count": 1, "jobs": [job()]}, run(run_attempt=2)])
    assert result["outcome"] == "unverified"


def test_wrong_expected_count_and_api_errors_are_not_success(tmp_path):
    result, _ = execute(tmp_path, [run(), {"total_count": 1, "jobs": [job()]}, run()], expected_jobs=2)
    assert result["outcome"] == "unverified"
    result, _ = execute(tmp_path, [watch.ObservationError("API unavailable")])
    assert result["outcome"] == "unverified"


def test_previous_success_is_invalidated_before_the_first_api_read(tmp_path):
    watch.write_json(tmp_path / "result.json", {"outcome": "success"})

    def unavailable(*args):
        assert json.loads((tmp_path / "result.json").read_text())["outcome"] == "pending"
        raise watch.ObservationError("API unavailable")

    result = watch.observe(REPO, RUN, SHA, 1, tmp_path, api=unavailable)
    assert result["outcome"] == "unverified"


def test_transport_is_get_only_and_does_not_echo_remote_errors(monkeypatch):
    calls = []

    def refused(argv, **kwargs):
        calls.append(argv)
        return watch.subprocess.CompletedProcess(argv, 1, stdout="", stderr="sensitive remote text")

    monkeypatch.setattr(watch.subprocess, "run", refused)
    with pytest.raises(watch.ObservationError, match="exit 1") as error:
        watch.gh_json("repos/owner/repo/actions/runs/42", 5)
    assert calls == [["gh", "api", "--method", "GET", "repos/owner/repo/actions/runs/42"]]
    assert "sensitive" not in str(error.value)


def test_cli_prints_only_one_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(watch, "observe", lambda *a, **k: {"outcome": "pending", "polls": 5})
    code = watch.main(["--repo", REPO, "--run-id", str(RUN), "--sha", SHA, "--attempt", "1", "--output", str(tmp_path)])
    assert code == 3 and json.loads(capsys.readouterr().out) == {"outcome": "pending", "polls": 5}


@pytest.mark.parametrize("option,value", [("--seconds", "60"), ("--seconds", "nan"),
                                        ("--interval", "0"), ("--expected-jobs", "0")])
def test_cli_refuses_unbounded_waits_and_invalid_counts(tmp_path, option, value):
    with pytest.raises(SystemExit) as error:
        watch.main(["--repo", REPO, "--run-id", str(RUN), "--sha", SHA, "--attempt", "1", "--output", str(tmp_path), option, value])
    assert error.value.code == 2
