"""Paired delegation replay, independent IR inspection, and real Git publication."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory

from ..ir import AccordError, require, text_digest
from ..evaluation.git_sink import git, seed_repository
from .backends import ARMS, as_wire, configurations, open_backend
from .git_bundle import BundleSink
from .sdk_ir import export_set, inspect_set
from .workload import (BASE, BEHAVIOR_RUNS, DOC, HERE, PATHS, ROOT, SOURCE, TEST,
                       bundle, candidate, parse_bundle, registry, run_behavior, source_snapshot)

SCENARIOS = ("success", "child_target_expansion", "child_input_expansion", "child_output_expansion",
             "excess_allocation", "parent_cancel_before_child", "behavior_mutant", "parent_child_receipts",
             "tampered_after_accept", "stale_source", "repeat_publication")


def adopt_checked_child(host, parent, child, accepted):
    """Trusted, sequential host glue; this dependency is absent from IR 0.1.

    The caller serializes adoption against task changes. This is not a generic
    atomic handoff API or authority granted by the child's returned text.
    """
    record = host.inspect("task", child)
    acceptance = host.inspect("acceptance", accepted)
    binding = acceptance["binding"]
    require(record["parent"] == parent and record["accepted"] == accepted, "wrong child acceptance")
    require(record["control"] == "active" and host.inspect("task", parent)["control"] == "active", "cancelled handoff")
    require(binding["task"] == child and binding["attempt"] == record["latest"], "stale child output")
    require(binding["profile"] == host.inspect("meta", "profile"), "stale handoff profile")
    for name, snapshot in binding["inputs"].items():
        current = host.inspect("resource", name)
        require(all(current[key] == snapshot[key] for key in ("revision", "digest")), "stale handoff input")
    attempt = host.inspect("attempt", binding["attempt"])
    require(attempt["task"] == child and attempt["digest"] == binding["output"] == text_digest(attempt["text"]),
            "wrong child artifact")
    return attempt["text"]


class Trial:
    def __init__(self, arm, directory, inputs, variant=1, *, repo=None, namespace="delegation"):
        self.arm, self.directory, self.inputs = arm, Path(directory), inputs
        self.specs = configurations(arm, variant)
        self.repo = Path(repo) if repo else self.directory / "repo"
        self.base = BASE if repo else seed_repository(self.repo, inputs, {"base_source": SOURCE, "base_docs": DOC,
                                                                       "base_schema": "src/schemas/subagent.py"})
        self.target = self.specs["parent"].publish_to[0]
        self.sink = BundleSink(self.repo, namespace, initial=self.base, target=self.target)
        self.host = open_backend(arm, self.directory / "host.sqlite", self.sink)
        for name, value in inputs.items():
            self.host.set_resource(name, value)
        self.parent = self.host.create_task(self.specs["parent"])
        self.child = self.accepted = self.child_accepted = self.operation = None
        self.calls = []
        self.text = candidate()

    def propose(self, task, label, text):
        def replay(inputs):
            assert inputs == self.inputs
            self.calls.append(label)
            return text
        return self.host.infer(task, label, replay)

    def delegate(self, spec=None):
        self.child = self.host.create_task(spec or self.specs["child"], parent=self.parent)

    def adopt(self, text=None):
        self.propose(self.child, "child_draft", self.text if text is None else text)
        receipts = self.host.check(self.child)
        self.child_accepted = self.host.accept(self.child, receipts)
        output = adopt_checked_child(self.host, self.parent, self.child, self.child_accepted)
        self.propose(self.parent, "adopt_child", output)
        return receipts

    def prepare(self):
        self.accepted = self.host.accept(self.parent, self.host.check(self.parent))
        grant = self.host.authorize("publish", self.parent, self.accepted, "patches", self.target, self.base)
        self.operation = self.host.prepare(self.parent, self.accepted, grant)

    @staticmethod
    def blocked(call):
        try:
            call()
        except AccordError:
            return "blocked"
        raise AssertionError("invalid action admitted")

    def observe(self, status):
        parent = self.host.inspect("task", self.parent)
        child = self.host.inspect("task", self.child) if self.child else None
        free = parent["remaining"] + (child["remaining"] if child else 0)
        assert free + len(self.calls) == self.specs["parent"].request_budget
        commits = self.sink.publication_count(self.base)
        assert commits == (1 if status == "confirmed" else 0)
        changed = git(self.repo, "diff", "--name-only", self.base, self.sink.tip()).stdout.splitlines()
        if status == "confirmed":
            assert set(changed) == set(PATHS)
            assert self.sink.target()["text"] == self.text
        return {"status": status, "publications": commits, "root_remaining": parent["remaining"],
                "child_remaining": child["remaining"] if child else None, "replay_calls": len(self.calls),
                "child_accepted": self.child_accepted is not None, "root_accepted": self.accepted is not None,
                "root_control": parent["control"], "changed_paths": changed,
                "published_sha256": text_digest(self.sink.target()["text"]) if commits else None}


def execute(t, scenario):
    h = t.host
    changes = {"child_target_expansion": {"publish_to": ("outside",)},
               "child_input_expansion": {"inputs": t.specs["child"].inputs + ("outside",)},
               "child_output_expansion": {"output_max_bytes": 65536},
               "excess_allocation": {"request_budget": t.specs["parent"].request_budget + 1}}
    if scenario in changes:
        return t.blocked(lambda: t.delegate(replace(t.specs["child"], **changes[scenario])))
    t.delegate()
    if scenario == "parent_cancel_before_child":
        h.cancel(t.parent)
        return t.blocked(lambda: t.propose(t.child, "cancelled", t.text))
    if scenario == "behavior_mutant":
        files = parse_bundle(t.text)
        assert "if len(item) > 100:" in files[SOURCE]
        files[SOURCE] = files[SOURCE].replace("if len(item) > 100:", "if len(item) > 1000:")
        t.adopt(bundle(files))
        return t.blocked(lambda: h.accept(t.parent, h.check(t.parent)))
    child_receipts = t.adopt()
    if scenario == "parent_child_receipts":
        return t.blocked(lambda: h.accept(t.parent, child_receipts))
    t.prepare()
    if scenario == "tampered_after_accept":
        t.propose(t.parent, "replacement", t.text + " ")
        return t.blocked(lambda: h.dispatch(t.operation))
    if scenario == "stale_source":
        h.set_resource("base_source", t.inputs["base_source"] + "\n# changed\n")
        return t.blocked(lambda: h.dispatch(t.operation))
    assert scenario in ("success", "repeat_publication")
    assert h.dispatch(t.operation)["status"] == "confirmed"
    if scenario == "repeat_publication":
        reopened = open_backend(t.arm, t.directory / "host.sqlite", t.sink)
        assert reopened.dispatch(t.operation)["status"] == "confirmed"
        assert reopened.infer(t.child, "child_draft", lambda _: (_ for _ in ()).throw(AssertionError("replayed model")))["text"] == t.text
    return "confirmed"


def inspect_controls():
    checks = {name: value.revision for name, value in registry().items()}
    data = (HERE / "contracts-1.json").read_text()
    original = inspect_set(data, checks)
    variants = []
    for variant in (1, 2):
        path = HERE / f"contracts-{variant}.json"
        raw = path.read_text()
        process = subprocess.run([sys.executable, "-m", "experiments.accord.delegation.sdk_ir", str(path)],
                                 cwd=ROOT, capture_output=True, text=True, timeout=20, check=True)
        inspection = json.loads(process.stdout)
        wires = [as_wire(configurations(arm, variant)) for arm in ARMS]
        assert all(wire == wires[0] for wire in wires)
        exported = export_set(configurations("sdk_python", variant))
        assert inspect_set(exported, checks) == inspection
        variants.append({"variant": variant, "wire_sha256": text_digest(raw), "inspection": inspection,
                         "all_four_declarations_equal": True, "python_export_equivalent": True,
                         "inspection_without_runtime_or_task_import": process.returncode == 0})
    invalid = {}
    for name in ("unknown_field", "unknown_version", "boolean_budget", "missing_validator", "child_expansion"):
        value = json.loads(data)
        if name == "unknown_field": value["parent"]["unexpected"] = 1
        if name == "unknown_version": value["parent"]["ir"] = "accord.contract/999"
        if name == "boolean_budget": value["parent"]["request_budget"] = True
        if name == "missing_validator": value["parent"]["checks"] = {"unavailable": "1"}
        if name == "child_expansion": value["child"]["publish_to"] = ["outside"]
        invalid[name] = json.dumps(value)
    invalid["duplicate_key"] = data.replace('"request_budget": 3', '"request_budget": 3, "request_budget": 4')
    assert invalid["duplicate_key"] != data
    rejected = []
    for name, raw in invalid.items():
        try: inspect_set(raw, checks)
        except (AccordError, ValueError): rejected.append(name)
        else: raise AssertionError("invalid IR admitted: " + name)
    assert inspect_set(json.dumps(json.loads(data), separators=(",", ":")), checks) == original
    updated = json.loads(data)
    updated["parent"]["request_budget"] += 1
    assert inspect_set(json.dumps(updated), checks)["digest"] != original["digest"]
    return {"variants": variants, "invalid_rejected": rejected, "formatting_identity_stable": True,
            "budget_change_identity_differs": True}


def run_all(destination):
    inputs, rows = source_snapshot(), []
    inspection = inspect_controls()
    for variant, scenarios in ((1, SCENARIOS), (2, ("success",))):
        for scenario in scenarios:
            for arm in ARMS:
                with TemporaryDirectory(prefix="accord-delegation-") as directory:
                    try:
                        trial = Trial(arm, directory, inputs, variant)
                        observation = trial.observe(execute(trial, scenario))
                        row = {"passed": True, **observation}
                    except Exception as exc:
                        row = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
                rows.append({"arm": arm, "variant": variant, "scenario": scenario, **row})
    paired = []
    for variant, scenario in dict.fromkeys((r["variant"], r["scenario"]) for r in rows):
        group = [{k: v for k, v in r.items() if k != "arm"} for r in rows
                 if r["variant"] == variant and r["scenario"] == scenario]
        paired.append({"variant": variant, "scenario": scenario,
                       "equivalent": all(r["passed"] and r == group[0] for r in group)})
    base = run_behavior(inputs["base_source"], inputs["base_schema"])
    initial = json.loads((HERE / "fixtures/candidate-initial.json").read_text())
    initial_check = run_behavior(initial["files"][SOURCE], inputs["base_schema"])
    sources = sorted(HERE.glob("*.py")) + [HERE.parent / name for name in ("runtime.py", "ir.py", "store.py")]
    sources += [HERE.parent / "evaluation" / name for name in ("python_sdk.py", "git_sink.py")]
    report = {"base_revision": BASE, "python": platform.python_version(), "arms": list(ARMS), "rows": rows,
              "paired": paired, "inspection": inspection, "candidate_sha256": text_digest(candidate()),
              "input_sha256": {k: text_digest(v) for k, v in inputs.items()},
              "implementation_sha256": {str(p.relative_to(ROOT)): text_digest(p.read_text()) for p in sources},
              "behavior_executions": BEHAVIOR_RUNS, "base_failed_checks": [r["name"] for r in base["checks"] if not r["passed"]],
              "initial_failed_checks": [r["name"] for r in initial_check["checks"] if not r["passed"]],
              "all_passed": all(r["passed"] for r in rows) and all(r["equivalent"] for r in paired),
              "limits": ["One delegated code task with a review-driven correction; deterministic replay, not four model samples.",
                         "Native host performed actual AI work; simulated credits count replay callbacks only.",
                         "Shared storage/hash helpers, validators, Git adapter and sequential host adoption glue.",
                         "Behavior subprocesses cached by exact source/schema bytes across arms; not a security sandbox.",
                         "Variant 2 changes registered validator revision/name, not its predicate semantics.",
                         "No provider inference timing, billing, productivity or general superiority measurement."]}
    Path(destination).mkdir(parents=True, exist_ok=True)
    (Path(destination) / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_all(args.output)
    print(json.dumps({"all_passed": report["all_passed"], "runs": len(report["rows"]),
                      "equivalent_groups": sum(r["equivalent"] for r in report["paired"])}, indent=2))
    for row in report["rows"]:
        if not row["passed"]: print(json.dumps(row))
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
