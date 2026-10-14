"""Run paired repository scenarios with recorded candidate bytes and real Git effects."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import difflib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch

from ..ir import AccordError, digest, text_digest
from .clients import ARMS, open_client
from .git_sink import GitSink, git, seed_repository
from .workload import BASE, Check, HERE, PATHS, ROOT, candidate, checks, source_snapshot

SCENARIOS = ("success", "bad_scope", "wrong_command", "commented_command", "echoed_command", "missing_receipt", "forged_receipt",
             "wrong_attempt_receipt", "source_aba", "resample_after_accept", "profile_change",
             "revoked", "cancelled", "budget_race", "grant_reissue", "target_conflict",
             "lost_reply", "ref_reply_lost", "crash_before_send", "crash_after_send", "recovery_race",
             "cancel_after_admit", "replay", "opaque_sink")


class Trial:
    def __init__(self, arm, directory, inputs):
        self.arm, self.directory, self.inputs = arm, Path(directory), inputs
        self.repo = self.directory / "repo"
        self.base = seed_repository(self.repo, inputs, PATHS)
        self.sink = GitSink(self.repo, "trial", initial=self.base)
        self.model_calls, self.validator_calls = [], []
        self.registry = {name: Check(check.revision, self._count_check(name, check.run)) for name, check in checks().items()}
        self.host, spec = open_client(arm, self.directory / "host.sqlite", self.sink, self.registry)
        for name, value in inputs.items():
            self.host.set_resource(name, value)
        self.task = self.host.create_task(spec)
        self.text = candidate(inputs)
        self.acceptance = self.grant = self.operation = None

    def _count_check(self, name, function):
        def counted(text, inputs):
            self.validator_calls.append(name)
            return function(text, inputs)
        return counted

    def propose(self, label="draft", text=None):
        def model(inputs):
            self.model_calls.append(label)
            return self.text if text is None else text
        return self.host.infer(self.task, label, model)

    def accepted(self):
        self.propose()
        receipts = self.host.check(self.task)
        self.acceptance = self.host.accept(self.task, receipts)
        return receipts

    def ready(self):
        self.accepted()
        self.grant = self.host.authorize("publish", self.task, self.acceptance, "docs", "test_guide", self.base)
        self.operation = self.host.prepare(self.task, self.acceptance, self.grant)

    def reopen(self):
        return open_client(self.arm, self.directory / "host.sqlite", GitSink(self.repo, "trial"), self.registry)[0]

    def crash(self, phase):
        script = '''
import os, sys
from pathlib import Path
from experiments.accord.evaluation.clients import open_client
from experiments.accord.evaluation.git_sink import GitSink
root=Path(sys.argv[1])
host,_=open_client(sys.argv[2],root/'host.sqlite',GitSink(root/'repo','trial'))
def hook(phase):
    if phase==sys.argv[4]:
        os._exit(73)
host.dispatch(sys.argv[3],crash_hook=hook)
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.directory), self.arm, self.operation, phase],
                                cwd=ROOT, capture_output=True, text=True, timeout=20)
        assert result.returncode == 73, result.stderr

    def blocked(self, function):
        try:
            function()
        except AccordError:
            return "blocked"
        raise AssertionError("invalid action was admitted")

    def observe(self, status):
        task = self.host.inspect("task", self.task)
        grant = self.host.inspect("grant", self.grant) if self.grant else None
        return {"status": status, "destination_commits": self.sink.publication_count(self.base),
                "remaining": task["remaining"], "model_calls": len(self.model_calls),
                "validator_calls": len(self.validator_calls), "grant_state": grant["state"] if grant else None,
                "task_control": task["control"], "artifact_sha256": text_digest(self.sink.target()["text"])}


def execute(trial, scenario):
    t, h = trial, trial.host
    if scenario in ("bad_scope", "wrong_command", "commented_command", "echoed_command"):
        bad = t.text.replace("# Tests\n", "# Invented guide\n", 1) if scenario == "bad_scope" else t.text.replace("tests/test_subagent_mcp.py -q", "tests/not_a_real_test.py -q", 1)
        if scenario == "commented_command":
            bad = t.text.replace("LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_subagent.py", "# LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_subagent.py", 1)
        if scenario == "echoed_command":
            from .workload import COMMAND
            bad = t.text.replace(COMMAND, 'echo "' + COMMAND + '"', 1)
        t.propose(text=bad)
        return t.blocked(lambda: h.accept(t.task, h.check(t.task)))
    if scenario in ("missing_receipt", "forged_receipt", "wrong_attempt_receipt"):
        t.propose()
        receipts = h.check(t.task)
        if scenario == "missing_receipt":
            receipts = receipts[:-1]
        elif scenario == "forged_receipt":
            receipts[0] = "worker_claims_passed"
        else:
            t.propose("new_attempt")
        return t.blocked(lambda: h.accept(t.task, receipts))
    if scenario == "budget_race":
        t.propose()
        barrier = threading.Barrier(2)
        def request(label):
            barrier.wait(timeout=5)
            try:
                return t.propose(label)
            except AccordError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(request, ("racing_one", "racing_two")))
        assert sum(value is not None for value in values) == 1
        assert len(t.model_calls) == 2
        return "one_admitted"
    t.ready()
    if scenario == "source_aba":
        h.set_resource("core_tests", t.inputs["core_tests"] + "\n# changed\n")
        h.set_resource("core_tests", t.inputs["core_tests"])
        return t.blocked(lambda: h.dispatch(t.operation))
    if scenario == "resample_after_accept":
        t.propose("new_attempt")
        return t.blocked(lambda: h.dispatch(t.operation))
    if scenario == "profile_change":
        updated = dict(t.registry)
        updated["source_alignment"] = Check("2", updated["source_alignment"].run)
        changed, _ = open_client(t.arm, t.directory / "host.sqlite", t.sink, updated)
        changed.activate_profile()
        return t.blocked(lambda: h.dispatch(t.operation))
    if scenario in ("revoked", "cancelled"):
        h.revoke(t.grant) if scenario == "revoked" else h.cancel(t.task)
        return t.blocked(lambda: h.dispatch(t.operation))
    if scenario == "grant_reissue":
        return t.blocked(lambda: h.authorize("publish", t.task, t.acceptance, "docs", "test_guide", "0" * 40))
    if scenario == "target_conflict":
        original = h.inspect("operation", t.operation)["intent"]
        text = t.inputs["test_guide"] + "\nConcurrent documentation update.\n"
        competing = original | {"request": "other", "output": text_digest(text)}
        t.sink.apply(digest("competing", competing), competing, text)
        result = h.dispatch(t.operation)
        assert result["status"] == "denied" and t.sink.target()["text"] == text
        return result["status"]
    if scenario in ("crash_before_send", "crash_after_send"):
        t.crash("before_send" if scenario == "crash_before_send" else "after_send")
        assert h.inspect("operation", t.operation)["status"] == "dispatched"
        result = t.reopen().recover(t.operation)
    elif scenario == "ref_reply_lost":
        from . import git_sink
        original_git, injected = git_sink.git, []
        def lost_after_cas(repo, *args, **kwargs):
            response = original_git(repo, *args, **kwargs)
            if args[0] == "update-ref" and response.returncode == 0 and not injected:
                assert "--stdin" not in args
                injected.append(True)
                return subprocess.CompletedProcess(response.args, 128, "", "lost CAS acknowledgement")
            return response
        with patch.object(git_sink, "git", lost_after_cas):
            result = h.dispatch(t.operation)
        assert injected
    elif scenario == "lost_reply":
        def lose(phase):
            if phase == "after_send":
                raise ConnectionError("injected lost Git receipt")
        assert h.dispatch(t.operation, crash_hook=lose)["status"] == "outcome_unknown"
        result = t.reopen().recover(t.operation)
    elif scenario == "cancel_after_admit":
        def cancel(phase):
            if phase == "before_send":
                h.cancel(t.task)
                h.revoke(t.grant)
            else:
                raise ConnectionError("response lost after cancellation")
        assert h.dispatch(t.operation, crash_hook=cancel)["status"] == "outcome_unknown"
        result = t.reopen().recover(t.operation)
        assert h.inspect("task", t.task)["control"] == "cancelled"
    elif scenario == "recovery_race":
        entered, release = threading.Event(), threading.Event()
        def pause(phase):
            if phase == "before_send":
                entered.set()
                assert release.wait(timeout=10)
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(h.dispatch, t.operation, crash_hook=pause)
            try:
                assert entered.wait(timeout=10)
                result = t.reopen().recover(t.operation)
            finally:
                release.set()
            assert pending.result(timeout=10)["status"] == "confirmed"
    elif scenario == "opaque_sink":
        class Opaque:
            identity, idempotent = t.sink.identity, False
            calls = 0
            def apply(self, operation, intent, text):
                self.calls += 1
                t.sink.apply(operation, intent, text)
                raise ConnectionError("cannot recover outcome")
            def lookup(self, operation):
                return None
        opaque = Opaque()
        blind, _ = open_client(t.arm, t.directory / "host.sqlite", opaque, t.registry)
        assert blind.dispatch(t.operation)["status"] == "outcome_unknown"
        assert blind.recover(t.operation)["status"] == "outcome_unknown" and opaque.calls == 1
        assert t.sink.publication_count(t.base) == 1
        return "outcome_unknown"
    elif scenario in ("success", "replay"):
        result = h.dispatch(t.operation)
        if scenario == "replay":
            old = t.propose()
            assert t.reopen().infer(t.task, "draft", lambda _: (_ for _ in ()).throw(AssertionError("model reran"))) == old
            assert t.reopen().dispatch(t.operation)["status"] == "confirmed"
            assert len(t.model_calls) == 1
    else:
        raise ValueError(scenario)
    assert result["status"] == "confirmed"
    assert t.sink.target()["text"] == t.text
    assert t.sink.publication_count(t.base) == 1
    assert h.inspect("grant", t.grant)["operation"] == t.operation
    assert h.inspect("grant", t.grant)["state"] == "spent"
    return "confirmed"


def run_all(output_directory, *, arms=ARMS, scenarios=SCENARIOS):
    inputs, rows = source_snapshot(), []
    for scenario in scenarios:
        for arm in arms:
            started = time.perf_counter()
            with TemporaryDirectory(prefix="accord-comparison-") as directory:
                trial = Trial(arm, directory, inputs)
                try:
                    status = execute(trial, scenario)
                    observation = trial.observe(status)
                    if status in ("blocked", "one_admitted"):
                        assert observation["destination_commits"] == 0
                    row = {"arm": arm, "scenario": scenario, "passed": True, **observation}
                except Exception as exc:
                    row = {"arm": arm, "scenario": scenario, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
            row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            rows.append(row)
    semantic_keys = ("passed", "status", "destination_commits", "remaining", "model_calls", "validator_calls",
                     "grant_state", "task_control", "artifact_sha256")
    paired = []
    for scenario in scenarios:
        group = [row for row in rows if row["scenario"] == scenario]
        observations = [{key: row.get(key) for key in semantic_keys} for row in group]
        paired.append({"scenario": scenario, "equivalent": all(value == observations[0] for value in observations)
                       and all(row["passed"] for row in group)})
    report = {"source_revision": BASE, "python": platform.python_version(), "git": git(ROOT, "version").stdout.strip(),
              "implementation_sha256": {str(path.relative_to(ROOT)): text_digest(path.read_text()) for path in
                                        [HERE / name for name in ("clients.py", "workload.py", "git_sink.py", "python_sdk.py", "run.py")]
                                        + [HERE.parent / name for name in ("runtime.py", "ir.py", "store.py")]},
              "candidate_sha256": text_digest(candidate(inputs)), "input_sha256": {k: text_digest(v) for k, v in inputs.items()},
              "arms": list(arms), "scenarios": list(scenarios), "rows": rows, "paired": paired,
              "all_passed": all(row["passed"] for row in rows) and all(row["equivalent"] for row in paired),
              "limits": ["One recorded agent-authored documentation task, not independently sampled model runs.",
                         "Independent SDK shares storage, hashing, predicates and Git sink; single-task subset only.",
                         "Elapsed times include temporary repository setup and are descriptive, not a performance benchmark."]}
    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    patch = "".join(difflib.unified_diff(inputs["test_guide"].splitlines(keepends=True), candidate(inputs).splitlines(keepends=True),
                                       fromfile="a/tests/README.md", tofile="b/tests/README.md", n=0))
    (destination / "accepted.patch").write_text(patch)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Directory for JSON observations and candidate patch")
    args = parser.parse_args()
    report = run_all(args.output)
    print(json.dumps({"all_passed": report["all_passed"], "runs": len(report["rows"]),
                      "equivalent_scenarios": sum(row["equivalent"] for row in report["paired"])}, indent=2))
    if not report["all_passed"]:
        for row in report["rows"]:
            if not row["passed"]:
                print(json.dumps(row))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
