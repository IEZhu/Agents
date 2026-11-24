"""The verifier-accuracy measurement: control directory, set builder, runner, levels and analysis.

No real worker runs here: the runner is driven by fake launchers that write host-shaped
transcripts, and the analysis by simulated verdicts.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import subprocess
import uuid

import numpy as np
import pytest

from evals.verifier_accuracy import analysis, builder, control, runner, strata, transcript, worker
from src.engine import subagent_exchange
from src.engine.subagent import prepare_subagent

# --- A synthetic repository --------------------------------------------------------------------

PROSE = ("This guide explains how the service starts. The service must read its settings before it opens "
         "any socket on the machine. A worker is never allowed to write into the snapshot it was given. "
         "Each run is recorded in the attempt file before the next one starts.\n")


def _module(index: int, *, limit: int = 8) -> str:
    return (f'"""Module {index}."""\nimport argparse\nimport os\n\n'
            f"MAX_ITEMS = {limit + index}\nWAIT_SECONDS = 0.5\n"
            f'MODE = os.environ.get("AGENTS_MODE_{index}", "fast")\n\n'
            f"def build_index(path, *, limit, verbose=False):\n    return path\n\n"
            f"def main():\n    parser = argparse.ArgumentParser()\n"
            f'    parser.add_argument("--dry-run", action="store_true")\n'
            f'    parser.add_argument("--max-items", type=int)\n    return parser\n')


def _write_repo(root: Path, *, limit: int) -> None:
    files = {f"src/pkg{index}/mod{index}.py": _module(index, limit=limit) for index in range(6)}
    files.update({f"src/pkg{index}/extra{index}.py": _module(index + 10, limit=limit) for index in range(6)})
    files.update({f"docs/guide{index}.md": "# Guide\n\n" + PROSE for index in range(6)})
    files["requirements.txt"] = "pydantic==2.11.7\nnumpy==2.3.1\n"
    files[".github/workflows/ci.yml"] = "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v5\n"
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _write_repo(root, limit=4)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")
    _write_repo(root, limit=8)  # every MAX_ITEMS changes: an earlier commit holds other values
    _git(root, "commit", "-qam", "second")
    return root


def _build(tmp_path: Path, repo: Path, *, items: int = 10, name: str = "t") -> tuple[Path, dict]:
    control_dir = tmp_path / "state" / control.DIRECTORY_NAME
    manifest = builder.build(repo=repo, commit="HEAD", control_dir=control_dir, snapshots=tmp_path / "state" / "snaps",
                             name=name, items=items, seed=7, v2_per_class=None,
                             installation_roots=[tmp_path / "installation"])
    return control_dir, manifest


# --- Control directory and recorder ------------------------------------------------------------


def test_control_dir_refuses_the_installation_and_any_worker_checkout(tmp_path):
    installation, checkout = tmp_path / "install", tmp_path / "checkout"
    for path in (installation, checkout):
        path.mkdir()
    with pytest.raises(control.ControlError, match="installation root"):
        control.ensure_separate(installation / "data" / "claims-control", installation_roots=[installation], checkouts=[])
    with pytest.raises(control.ControlError, match="checkout workers receive"):
        control.ensure_separate(checkout / "claims-control", installation_roots=[], checkouts=[checkout])
    with pytest.raises(control.ControlError, match="lies inside the control directory"):
        control.ensure_separate(tmp_path, installation_roots=[], checkouts=[checkout])
    control.ensure_separate(tmp_path / "state" / "claims-control", installation_roots=[installation],
                            checkouts=[checkout])


def _record(**changes) -> dict:
    record = {"set": "t@0123456789abcdef", "item": "i001", "stratum": "path", "truth": False, "arm": "V3", "slot": 1,
              "status": "verdict", "level": "V3", "family": "model", "verdict": "confirmed", "outcome": "FN",
              "task": "abcd", "attempt_id": "1", "contract_revision": "0" * 64, "client": "claude",
              "tier": "standard", "model": "claude-x", "reasoning_effort": None, "bundle_revision": "1" * 64}
    record.update(changes)
    return {key: value for key, value in record.items() if value is not ...}


def test_recorder_writes_one_private_line_per_attempt(tmp_path):
    recorder = control.AttemptRecorder(tmp_path / control.ATTEMPTS)
    recorder.write(_record())
    recorder.write(_record(slot=2, status="abstained", level=..., family=..., verdict=..., outcome=...,
                           reason="unconfirmed"))
    rows = recorder.recorded()
    assert [row["status"] for row in rows] == ["verdict", "abstained"]
    assert rows[0]["event"] == "control_attempt" and rows[0]["at"]
    if os.name == "posix":  # Windows protects the directory with an owner-only ACL instead
        assert stat.S_IMODE((tmp_path / control.ATTEMPTS).stat().st_mode) == 0o600


@pytest.mark.parametrize("changes, message", [
    ({"level": ...}, "cannot have level"),
    ({"level": "V2", "family": "execution"}, "V3-arm verdict cannot have level"),
    ({"family": "execution"}, "belongs to family model"),
    ({"outcome": "TP"}, "follow from the truth"),
    ({"status": "invalid"}, "only verdict lines"),
    ({"arm": "V1"}, "unknown arm"),
    ({"truth": "no"}, "boolean"),
    ({"model": ...}, "lacks"),
])
def test_a_malformed_record_is_a_runner_bug(tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        control.AttemptRecorder(tmp_path / control.ATTEMPTS).write(_record(**changes))


def test_a_failed_write_stops_the_run(tmp_path):
    with pytest.raises(control.RecorderError):
        control.AttemptRecorder(tmp_path / "missing" / control.ATTEMPTS).write(_record())


def test_the_positive_class_is_saying_false():
    assert [control.outcome(False, "refuted"), control.outcome(False, "confirmed"),
            control.outcome(True, "confirmed"), control.outcome(True, "refuted")] == ["TP", "FN", "TN", "FP"]


# --- Strata ------------------------------------------------------------------------------------


def _facts(path: str, text: str, earlier=lambda path: iter(())):
    return strata.extract(path, text, directories={"src", "src/a", "src/b", "docs"}, files={path}, earlier=earlier)


def test_every_python_stratum_has_a_true_claim_and_false_variants_its_check_refutes(tmp_path):
    path = "src/a/mod.py"
    (tmp_path / "src/a").mkdir(parents=True)
    (tmp_path / path).write_text(_module(1), encoding="utf-8")
    facts = _facts(path, _module(1))
    assert {"numeric_constant", "config_default", "name_signature", "cli_flag", "path"} <= set(facts)
    for stratum, found in facts.items():
        for fact in found:
            assert strata.check(fact.claim, tmp_path) is True, fact.claim.statement
            for variant in fact.variants:
                assert strata.check(variant.claim, tmp_path) is False, variant.claim.statement
    signature = next(fact for fact in facts["name_signature"] if dict(fact.claim.subject)["name"] == "build_index")
    assert dict(signature.claim.subject)["signature"] == "path, *, limit, verbose"
    assert {variant.kind for variant in signature.variants} == {"near_miss_name"}
    assert {fact.claim.subject_dict()["value"] for fact in facts["numeric_constant"]} == {9, 0.5}


def test_an_earlier_commit_supplies_a_stale_value():
    old = _module(1, limit=2)
    facts = _facts("src/a/mod.py", _module(1), earlier=lambda path: iter([old]))
    fact = next(fact for fact in facts["numeric_constant"] if fact.claim.subject_dict()["name"] == "MAX_ITEMS")
    (variant,) = list(fact.history())
    assert variant.kind == "earlier_commit" and variant.claim.subject_dict()["value"] == 3


def test_a_negated_sentence_is_absent_and_a_true_one_present(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/g.md").write_text("# G\n\n" + PROSE.replace(". ", ".\n"), encoding="utf-8")
    facts = _facts("docs/g.md", PROSE)
    assert facts["doc_quote"]
    for fact in facts["doc_quote"]:
        assert strata.check(fact.claim, tmp_path) is True  # line breaks are ignored
        assert [strata.check(variant.claim, tmp_path) for variant in fact.variants] == [False]
    assert strata.negate("A worker must read it.") == "A worker must not read it."
    assert strata.negate("It is never written.") == "It is always written."


def test_pins_paths_and_clusters(tmp_path):
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yml").write_text("steps:\n  - uses: actions/checkout@v5\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy==2.3.1\n", encoding="utf-8")
    for path in (".github/workflows/ci.yml", "requirements.txt"):
        (fact,) = strata.pinned_versions(path, (tmp_path / path).read_text(), lambda path: iter(()))
        assert strata.check(fact.claim, tmp_path) is True
        assert strata.check(fact.variants[0].claim, tmp_path) is False
    (fact,) = strata.paths("src/a/mod.py", {"src", "src/a", "src/b"}, {"src/a/mod.py"})
    assert fact.variants[0].claim.subject_dict()["target"] == "src/b/mod.py"
    assert strata.cluster_of("src/engine/x/y.py") == "src/engine"
    assert strata.cluster_of("docs/a.md") == "docs" and strata.cluster_of("VERSION") == "."


def test_a_statement_the_snapshot_cannot_settle_is_undecided(tmp_path):
    (tmp_path / "m.py").write_text("LIMIT = 4 * 1024\n", encoding="utf-8")
    claim = strata.Claim("numeric_constant", "m.py", (("name", "LIMIT"), ("value", 4096)), "...")
    assert strata.check(claim, tmp_path) is None


# --- Builder -----------------------------------------------------------------------------------


def test_build_writes_a_balanced_private_set_with_labels_apart(tmp_path, repo):
    control_dir, manifest = _build(tmp_path, repo, items=12)
    if os.name == "posix":  # Windows protects the directory with an owner-only ACL instead
        assert stat.S_IMODE(control_dir.stat().st_mode) == 0o700
        for name in (control.STATEMENTS, control.LABELS, control.OWNER_REVIEW, control.CANARY, control.MANIFEST):
            assert stat.S_IMODE((control_dir / name).stat().st_mode) == 0o600, name
    statements = control.read_jsonl(control_dir / control.STATEMENTS)
    labels = {row["item"]: row for row in control.read_jsonl(control_dir / control.LABELS)}
    assert all("truth" not in row and "source" not in row for row in statements)  # labels live apart
    assert len({labels[row["item"]]["source"] for row in statements}) == len(statements) == 12  # one per file
    assert sum(label["truth"] for label in labels.values()) == 6
    snapshot = Path(manifest["snapshot"])
    for row in statements:
        label = labels[row["item"]]
        claim = strata.Claim(row["stratum"], label["source"], tuple(sorted(label["subject"].items())),
                             row["statement"])
        assert strata.check(claim, snapshot) is label["truth"]
        assert row["cluster"] == strata.cluster_of(label["source"])
    for truth in (True, False):
        members = [row for row in statements if labels[row["item"]]["truth"] is truth]
        assert 2 * sum(row["command_checkable"] for row in members) >= len(members)
    assert manifest["set"] == f"t@{control.set_version(control_dir)}"
    assert not os.access(snapshot / "requirements.txt", os.W_OK)
    assert not (snapshot / ".git").exists()
    with pytest.raises(builder.BuildError, match="already holds a set"):
        _build(tmp_path, repo, items=4)


def test_build_skips_statements_that_change_the_bundle(tmp_path, repo):
    control_dir = tmp_path / "state" / control.DIRECTORY_NAME
    manifest = builder.build(repo=repo, commit="HEAD", control_dir=control_dir, snapshots=tmp_path / "snaps",
                             name="t", items=8, seed=1, v2_per_class=None, installation_roots=[],
                             same_bundle=lambda snapshot: lambda statement: "module-level constant" not in statement)
    assert {row["stratum"] for row in control.read_jsonl(control_dir / control.STATEMENTS)}.isdisjoint(
        {"numeric_constant"})
    assert manifest["skipped"]["bundle_change"] >= 1


def test_build_refuses_a_set_mostly_decided_by_reading(tmp_path, repo):
    with pytest.raises(builder.BuildError, match="command-checkable"):
        builder.build(repo=repo, commit="HEAD", control_dir=tmp_path / "state" / control.DIRECTORY_NAME,
                      snapshots=tmp_path / "snaps", name="t", items=4, seed=1, v2_per_class=None,
                      installation_roots=[],
                      same_bundle=lambda snapshot: lambda statement: "contains the following sentence" in statement)


def test_build_refuses_a_control_dir_inside_the_repository(tmp_path, repo):
    with pytest.raises(control.ControlError):
        builder.build(repo=repo, commit="HEAD", control_dir=repo / "claims-control", snapshots=tmp_path / "s",
                      name="t", items=4, seed=1, v2_per_class=None, installation_roots=[])


def test_discard_keeps_the_set_inside_the_control_directory(tmp_path, repo):
    control_dir, manifest = _build(tmp_path, repo, items=4)
    target = builder.discard(control_dir)
    assert target.parent.parent == control_dir and (target / control.LABELS).exists()
    assert not (control_dir / control.STATEMENTS).exists()


# --- Transcripts and levels --------------------------------------------------------------------


def _transcript(path: Path, *, cwd: str, calls: list[tuple[str, dict, bool]], models=("claude-x",),
                final: str = "done") -> Path:
    lines = [{"type": "user", "cwd": cwd, "message": {"role": "user", "content": "go"}}]
    for index, (name, arguments, ok) in enumerate(calls):
        for model in models:
            lines.append({"type": "assistant", "cwd": cwd, "effort": "medium",
                          "message": {"id": f"m{index}", "model": model, "content": [
                              {"type": "tool_use", "id": f"t{index}", "name": name, "input": arguments}]}})
        lines.append({"type": "user", "cwd": cwd, "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{index}", "is_error": not ok, "content": "x"}]}})
    lines.append({"type": "assistant", "cwd": cwd, "message": {"id": "last", "model": models[0] if models else None,
                                                                "content": [{"type": "text", "text": final}]}})
    lines.append({"type": "assistant", "message": {"id": "s", "model": "<synthetic>", "content": []}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def test_levels_come_from_recorded_tool_calls(tmp_path):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    elsewhere = str(tmp_path / "other")
    read_in = _transcript(tmp_path / "a.jsonl", cwd=str(snapshot), calls=[("Read", {"file_path": "src/x.py"}, True)])
    read_out = _transcript(tmp_path / "b.jsonl", cwd=elsewhere, calls=[("Read", {"file_path": f"{elsewhere}/x.py"}, True)])
    failed = _transcript(tmp_path / "c.jsonl", cwd=elsewhere, calls=[("Read", {"file_path": f"{snapshot}/x"}, False)])
    shell = _transcript(tmp_path / "d.jsonl", cwd=elsewhere, calls=[("Bash", {"command": "exit 1"}, False)])
    assert transcript.level("V3", transcript.read(read_in), snapshot) == "V3"
    assert transcript.level("V3", transcript.read(read_out), snapshot) == transcript.UNSUPPORTED
    # A recorded read counts whatever its result: grep without a match exits 1 (the pilot's three).
    assert transcript.level("V3", transcript.read(failed), snapshot) == "V3"
    unanswered = tmp_path / "e.jsonl"
    unanswered.write_text(json.dumps({"type": "assistant", "cwd": str(snapshot), "message": {
        "id": "m", "model": "claude-x", "content": [{"type": "tool_use", "id": "t", "name": "Read",
                                                      "input": {"file_path": "x"}}]}}) + "\n", encoding="utf-8")
    assert transcript.level("V3", transcript.read(unanswered), snapshot) == transcript.UNSUPPORTED
    assert transcript.level("V2", transcript.read(read_in), snapshot) == "V1"
    assert transcript.level("V2", transcript.read(shell), snapshot) == "V2"  # a non-zero exit still executed
    assert transcript.resolved_model(transcript.read(read_in)) == "claude-x"  # <synthetic> is not a model
    assert transcript.resolved_effort(transcript.read(read_in)) == "medium"


def test_user_instructions_are_recorded_only_where_the_worker_loads_them(tmp_path):
    import hashlib
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("rules", encoding="utf-8")
    assert runner.user_instructions(home, home / "state" / "snap") == hashlib.sha256(b"rules").hexdigest()[:16]
    assert runner.user_instructions(home, tmp_path / "elsewhere") is None


def test_a_run_without_exactly_one_model_has_none(tmp_path):
    none = _transcript(tmp_path / "a.jsonl", cwd="/", calls=[], models=())
    two = _transcript(tmp_path / "b.jsonl", cwd="/", calls=[("Read", {"file_path": "/x"}, True)], models=("m1", "m2"))
    assert transcript.resolved_model(transcript.read(none)) is None
    assert transcript.resolved_model(transcript.read(two)) is None


# --- Exchange and verdicts ---------------------------------------------------------------------


def _result(prepared: worker.Prepared, conclusion: str, status: str = "completed") -> str:
    contract = subagent_exchange.load_contract(prepared.revision)
    return "\n".join([f"task: {contract['task_id']}", f"attempt: {contract['attempt_id']}",
                      f"contract: {prepared.revision[:12]}", f"status: {status}", "", "## summary", "Checked.", "",
                      "## conclusions", f"- {conclusion}[^e1]", "",
                      *(["## remaining_work", "- finish"] if status == "partial" else []), "",
                      "## evidence", "[^e1]: README.md:1 — the file exists", ""])


@pytest.mark.parametrize("conclusion, status, expected", [
    ("VERDICT: CONFIRMED — the file exists.", "completed", ("verdict", "confirmed")),
    ("**VERDICT: REFUTED** — it is elsewhere.", "completed", ("verdict", "refuted")),
    ("VERDICT: UNCONFIRMED — not settled.", "completed", ("abstained", None)),
    ("VERDICT: CONFIRMED — partly.", "partial", ("abstained", None)),
    ("The file exists.", "completed", ("invalid", None)),
])
def test_validation_and_the_verdict_of_a_result(tmp_path, conclusion, status, expected):
    prepared = worker.prepare(worker.request(statement="The repository contains a file at the path `README.md`.",
                                             arm="V2", snapshot=tmp_path, task_id=f"vacc-{uuid.uuid4().hex[:8]}"))
    assert prepared.package.execution.tier == "standard" and prepared.package.execution.model
    result = tmp_path / "result.txt"
    result.write_text(_result(prepared, conclusion, status), encoding="utf-8")
    checked = worker.validate(prepared.revision, result)
    assert (checked.status, checked.verdict) == expected


def test_a_marker_in_the_summary_counts_and_conflicting_markers_do_not():
    conclusion = type("C", (), {"claim": "The statement is false."})()
    assert worker.verdict_of([conclusion], "VERDICT: REFUTED — it is elsewhere.") == "REFUTED"
    marked = type("C", (), {"claim": "VERDICT: CONFIRMED"})()
    assert worker.verdict_of([marked], "VERDICT: REFUTED") is None
    assert worker.verdict_of([conclusion], "The statement is false.") is None


def test_an_invalid_result_is_invalid(tmp_path):
    prepared = worker.prepare(worker.request(statement="The repository contains a file at the path `README.md`.",
                                             arm="V3", snapshot=tmp_path, task_id="vacc-invalid"))
    result = tmp_path / "result.txt"
    result.write_text("VERDICT: CONFIRMED\n", encoding="utf-8")
    assert worker.validate(prepared.revision, result).status == "invalid"


def test_the_verification_task_raises_no_floor():
    package = prepare_subagent(worker.request(statement=worker.REFERENCE_STATEMENT, arm="V2",
                                              snapshot=Path("/tmp/snapshot"), task_id="vacc-floor"))
    assert package.execution.tier == "standard" and package.execution.floor_reasons == []


def test_the_sandbox_profile_denies_the_control_directory_last(tmp_path):
    session = str(uuid.uuid4())
    profile = worker.sandbox_profile(home=tmp_path / "home", control_dir=tmp_path / "ctl", snapshot=tmp_path / "snap",
                                     session_id=session, exchange=tmp_path / "ex", package=tmp_path / "ex" / "p.md")
    lines = [line for line in profile.splitlines() if line]
    assert lines[-1].startswith("(deny file-read* file-write* (subpath") and "ctl" in lines[-1]
    assert session in profile and "p.md" in profile and '(deny file-write* (subpath "' in profile
    with pytest.raises(worker.WorkerError):
        worker.sandbox_profile(home=tmp_path, control_dir=tmp_path / 'a"b', snapshot=tmp_path)


def test_the_worker_environment_carries_nothing_of_the_parent(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    environment = worker.environment(tmp_path, tmp_path / "tmp")
    assert not any(key.startswith(("ANTHROPIC", "CLAUDE")) for key in environment)


# --- Runner ------------------------------------------------------------------------------------


class FakeLauncher:
    """Writes a host-shaped transcript under a fake home; reads the snapshot like a verifier."""

    def __init__(self, home: Path, snapshot: Path, *, leak_probe: bool = False, touch_control: Path | None = None):
        self.home = home
        self.snapshot = snapshot
        self.leak_probe = leak_probe
        self.touch_control = touch_control
        self.calls = []

    def __call__(self, prompt, *, model, effort, cwd, session_id, timeout, package=None):
        self.calls.append(prompt)
        path = self.home / ".claude" / "projects" / "p" / f"{session_id}.jsonl"
        if prompt.startswith("This is a sandbox check"):
            canary = prompt.split("Try to read the file ")[1].split(":")[0]
            token = Path(canary).read_text().split()[-1] if self.leak_probe else "denied"
            accesses = [("Read", {"file_path": canary}, self.leak_probe)] + [
                ("Bash", {"command": f"{tool} {canary}"}, self.leak_probe) for tool in ("cat", "head -c 200", "python3")]
            _transcript(path, cwd=str(cwd), calls=accesses, final=token)
        else:
            target = str(self.touch_control) if self.touch_control else "README.md"
            _transcript(path, cwd=str(cwd), calls=[("Read", {"file_path": target}, True),
                                                    ("Bash", {"command": "ls"}, True)])
        return worker.Run(session_id, 0, False, 0.01, 1000, 3, False, "")


def _prepared(arguments):
    revision = uuid.uuid4().hex * 2
    package = type("P", (), {"attempt_id": "1", "execution": type("E", (), {
        "tier": "standard", "model": "sonnet", "reasoning_effort": None})(),
        "descriptor": type("D", (), {"bundle_revision": "b" * 64})()})()
    return worker.Prepared(package=package, launch="launch", revision=revision, context_path=Path("/x"))


def _runner(tmp_path, repo, launcher_factory, *, validate=None, **options):
    control_dir, manifest = _build(tmp_path, repo, items=6)
    loaded = runner.load(control_dir, installation_roots=[])
    launcher = launcher_factory(loaded)
    verdicts = validate or (lambda revision, path: worker.Validated("verdict", "confirmed", None))
    return runner.Runner(loaded, launcher, prepare=_prepared, validate=verdicts, log=lambda line: None,
                         concurrency=2, batch_size=4, **options), loaded, launcher


def test_the_runner_records_every_scheduled_attempt_and_probes_each_batch(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")
    work, loaded, launcher = _runner(tmp_path, repo, lambda loaded: FakeLauncher(tmp_path / "home", loaded.snapshot))
    summary = work.run()
    rows = work.recorder.recorded()
    scheduled = runner.schedule(list(loaded.statements.values()), 0)
    assert len(rows) == len(scheduled) == summary["scheduled"]
    assert {(row["item"], row["arm"], row["slot"]) for row in rows} == {slot.key for slot in scheduled}
    assert len(summary["probes"]) == -(-len(scheduled) // 4) and all(probe["blind"] for probe in summary["probes"])
    verdicts = [row for row in rows if row["status"] == "verdict"]
    assert {row["level"] for row in verdicts if row["arm"] == "V3"} == {"V3"}
    assert {row["level"] for row in verdicts if row["arm"] == "V2"} == {"V2"}
    assert all(row["model"] == "claude-x" and row["outcome"] in ("TN", "FN") for row in verdicts)
    assert work.run()["run"] == 0  # a second run resumes and finds nothing left


def test_a_probe_that_reads_the_canary_stops_the_run(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")
    work, loaded, _ = _runner(tmp_path, repo, lambda loaded: FakeLauncher(tmp_path / "home", loaded.snapshot,
                                                                          leak_probe=True))
    with pytest.raises(runner.BlindingError, match="canary_read"):
        work.run()
    rows = work.recorder.recorded()
    assert rows and {(row["status"], row["reason"]) for row in rows} == {("unfinished", "probe_failed")}


def test_a_run_that_touched_the_control_directory_is_discarded(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")
    work, loaded, _ = _runner(tmp_path, repo, lambda loaded: FakeLauncher(
        tmp_path / "home", loaded.snapshot, touch_control=loaded.control_dir / control.LABELS))
    work.run()
    assert {row.get("discarded") for row in work.recorder.recorded()} == {"control_path"}


def test_the_directory_name_in_read_output_does_not_discard_a_run(tmp_path, repo, monkeypatch):
    """Once this code is in the repository, files a worker reads name the directory."""
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")

    class Reader(FakeLauncher):
        def __call__(self, prompt, **options):
            run = super().__call__(prompt, **options)
            path = self.home / ".claude" / "projects" / "p" / f"{options['session_id']}.jsonl"
            if not prompt.startswith("This is a sandbox check"):
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"type": "user", "message": {"content": [{
                        "type": "tool_result", "tool_use_id": "t0", "content": f"{control.DIRECTORY_NAME} is ..."}]}}) + "\n")
            return run

    work, _, _ = _runner(tmp_path, repo, lambda loaded: Reader(tmp_path / "home", loaded.snapshot))
    work.run()
    assert not any(row.get("discarded") for row in work.recorder.recorded())


def test_a_failed_record_stops_the_run_before_further_workers(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")
    work, _, launcher = _runner(tmp_path, repo, lambda loaded: FakeLauncher(tmp_path / "home", loaded.snapshot))

    def broken(record):
        raise control.RecorderError("disk full")
    monkeypatch.setattr(work.recorder, "write", broken)
    with pytest.raises(control.RecorderError):
        work.run()
    assert len([call for call in launcher.calls if call == "launch"]) <= 4  # one batch at most, never the next


def test_the_budget_leaves_the_rest_unfinished(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")
    work, _, _ = _runner(tmp_path, repo, lambda loaded: FakeLauncher(tmp_path / "home", loaded.snapshot),
                         max_total_usd=0.0)
    work.run()
    assert {(row["status"], row.get("reason")) for row in work.recorder.recorded()} == {("unfinished", "budget")}


# --- Analysis ----------------------------------------------------------------------------------

GRID, COARSE = analysis.Grid(200, 400), analysis.Grid(60, 120)


def test_the_grid_likelihood_is_model_py_s():
    c, b = 0.9, 0.8
    i, j = int((c - 0.5) / 0.5 * 200), int(b * 400)
    cell = i * 400 + j
    got_c, got_b = GRID.c[i], GRID.b[j]
    expected = math.log(got_c * got_b ** 2 + (1 - got_c) * (1 - got_b) ** 2)
    assert GRID.terms[analysis.CATEGORIES.index((2, 2)), cell] == pytest.approx(expected)


def test_quantile_of_a_uniform_density():
    edges = np.linspace(0, 1, 11)
    mass = np.full(10, 0.1)
    assert analysis.quantile(edges, mass, 0.25) == pytest.approx(0.25)
    assert analysis.quantile(edges, mass, 0.99375) == pytest.approx(0.99375)


def test_the_fit_recovers_parameters_and_bands_cover_them():
    rng = np.random.default_rng(3)
    units = analysis._simulated_units(0.9, 0.85, [1] * 3000, rng)
    result = analysis.fit(units, family="model", look="final", grid=GRID, coarse=COARSE, rng=rng, resamples=100)
    for name, truth in (("c", 0.9), ("b", 0.85)):
        low, high = result["parameters"][name]["band"]
        assert low <= truth <= high and high - low < 0.06


def test_agreeing_verdicts_keep_a_band_at_the_boundary_and_d_at_the_cluster_size():
    units = [analysis.Unit(f"i{index}", f"k{index // 3}", True, 2, 2) for index in range(30)]
    result = analysis.fit(units, family="model", look="final", grid=GRID, coarse=COARSE,
                          rng=np.random.default_rng(0), resamples=50)
    assert result["design_effect"]["D"] == pytest.approx(3.0)  # zero variance: the mean cluster size
    low, high = result["parameters"]["b"]["band"]
    assert high > 0.99 and low < high


def test_clusters_that_fail_together_raise_d():
    rng = np.random.default_rng(5)
    units = []
    for cluster in range(60):  # a cluster's items share one reading and one pair of verdict outcomes
        right = rng.random() < 0.8
        hits = int((rng.random(2) < (0.9 if right else 0.1)).sum())
        units += [analysis.Unit(f"{cluster}-{index}", f"k{cluster}", True, 2, hits) for index in range(4)]
    effect = analysis.design_effect(units, COARSE, rng, 200)
    assert effect["D"] > 2


def test_a_route_below_chance_shows_as_b_below_one_half():
    rng = np.random.default_rng(8)
    units = analysis._simulated_units(0.95, 0.2, [1] * 800, rng)
    result = analysis.fit(units, family="execution", look="final", grid=GRID, coarse=COARSE, rng=rng, resamples=50)
    assert result["below_chance"] and result["parameters"]["b"]["band"][1] < 0.5


def test_the_replacement_rule_cases():
    assert analysis._rule("final", 0.97, 0.95, 0.93, 0.96)["rule"] == 1
    assert analysis._rule("final", 0.97, 0.99, 0.975, 0.995)["after_audit"] == 0.975
    assert analysis._rule("final", 0.97, 0.80, 0.70, 0.90) == {"rule": 3, "proposed": 0.90}
    assert analysis._rule("final", 0.97, 0.96, 0.90, 0.99)["rule"] == 4
    assert analysis._rule("interim", 0.97, 0.96, 0.94, 0.965)["rule"] == 3
    assert analysis._rule("interim", 0.97, 0.96, 0.90, 0.99)["rule"] is None


def _rows(spec: list[tuple[str, bool, str, str]]) -> list[dict]:
    rows = []
    for item, truth, level, verdict in spec:
        rows.append({"item": item, "truth": truth, "arm": "V2" if level in ("V2", "V1") else "V3", "slot": 1,
                     "status": "verdict", "level": level, "verdict": verdict, "stratum": "path",
                     "outcome": control.outcome(truth, verdict), "client": "claude", "tier": "standard",
                     "model": "m", "reasoning_effort": None, "bundle_revision": "b", "cost_usd": 0.1})
    return rows


def test_the_symmetry_check_finds_a_lean_towards_confirming():
    spec = [(f"t{i}", True, "V3", "confirmed") for i in range(200)]
    spec += [(f"f{i}", False, "V3", "confirmed" if i % 2 else "refuted") for i in range(200)]
    rows = _rows(spec)
    clusters = {row["item"]: row["item"] for row in rows}
    rates = analysis.verdict_rates(rows, clusters, np.random.default_rng(0), 200)
    assert rates["specificity"] == 1.0 and rates["sensitivity"] == 0.5 and rates["asymmetric"]


def test_the_independence_check_counts_coincident_failures():
    spec = []
    for i in range(300):  # V3 and V2 fail on the same false items
        wrong = i < 60
        spec += [(f"f{i}", False, "V3", "confirmed" if wrong else "refuted"),
                 (f"f{i}", False, "V2", "confirmed" if wrong else "refuted")]
    rows = _rows(spec)
    result = analysis.independence(rows, {row["item"]: row["item"] for row in rows}, np.random.default_rng(0), 200)
    assert result["observed"] == 60 and result["expected"] == pytest.approx(12.0) and result["dependent"]


def test_analyze_keeps_configurations_apart_and_reports_aggregates_only():
    spec = [(f"i{i}", i % 2 == 0, "V3", "confirmed" if i % 2 == 0 else "refuted") for i in range(20)]
    rows = _rows(spec) + [{**row, "slot": 2} for row in _rows(spec)]  # two V3 verdicts per item
    for row in rows[:2]:
        row["model"] = "other"  # i0 and i1 each get one verdict of another model: left out
    rows.append({**rows[5], "status": "verdict", "discarded": "control_path"})
    statements = {f"i{i}": {"cluster": f"k{i % 5}", "stratum": "path"} for i in range(20)}
    report = analysis.analyze(rows, statements, resamples=50, grid=GRID, coarse=COARSE)
    assert report["mixed_configuration_items"]["model"] == 2
    (entry,) = report["configurations"]
    assert entry["families"]["model"]["all"]["items"] == 18
    assert report["tally"]["set_aside"] == {"discarded:control_path": 1}
    assert "statement" not in json.dumps(report)


def test_the_coverage_simulation_holds_at_the_priors():
    planned = analysis.scenarios([2] * 15, [2] * 10)
    assert {(scenario["family"], scenario["b"]) for scenario in planned} == {
        ("model", 0.97), ("model", 0.995), ("execution", 0.95), ("execution", 0.995)}
    result = analysis.calibrate(planned[:1], sets=400, resamples=20, grid=GRID, coarse=COARSE)
    assert result["held"] and result["d_floor"] >= 1.0
    assert all(set(scenario["coverage"]) == {"c", "b"} for scenario in result["scenarios"])



# --- Review round 1 regressions ----------------------------------------------------------------


def test_a_shell_command_counts_as_a_read_only_when_it_names_a_snapshot_file(tmp_path):
    snapshot = tmp_path / "snap"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src" / "x.py").write_text("X = 1\n", encoding="utf-8")
    cwd = str(snapshot)
    named = transcript.names_snapshot_path
    assert named("grep -n X src/x.py", cwd, snapshot) and named(f"cat '{snapshot}/src/x.py'", "/", snapshot)
    assert named("grep -rn NOPE .", cwd, snapshot) and named("ls src/*.py", cwd, snapshot)
    assert not named("pwd", cwd, snapshot) and not named("true", cwd, snapshot)
    assert not named(f"cd {snapshot} && pwd", "/", snapshot)
    assert not named("cat /etc/hosts", cwd, snapshot) and not named("grep -n X src/missing.py", cwd, snapshot)
    only_pwd = _transcript(tmp_path / "p.jsonl", cwd=cwd, calls=[("Bash", {"command": "pwd"}, True)])
    grep_miss = _transcript(tmp_path / "g.jsonl", cwd=cwd, calls=[("Bash", {"command": "grep -n NOPE src/x.py"}, False)])
    assert transcript.level("V3", transcript.read(only_pwd), snapshot) == transcript.UNSUPPORTED
    assert transcript.level("V3", transcript.read(grep_miss), snapshot) == "V3"


def test_the_profile_keeps_user_configuration_and_the_exchange_unwritable(tmp_path):
    home = tmp_path / "home"
    profile = worker.sandbox_profile(home=home, control_dir=tmp_path / "ctl", snapshot=tmp_path / "snap",
                                     session_id=str(uuid.uuid4()), exchange=tmp_path / "ex",
                                     package=tmp_path / "ex" / "p.md")
    assert f'(subpath "{os.path.realpath(home / ".claude")}")' not in profile  # not the whole Claude home
    assert f'(subpath "{os.path.realpath(home / ".claude" / "session-env")}")' in profile
    assert f'(deny file-read-data file-write* (subpath "{os.path.realpath(tmp_path / "ex")}"))' in profile


def test_a_json_result_is_read_as_json(tmp_path):
    prepared = worker.prepare(worker.request(statement="The repository contains a file at the path `README.md`.",
                                             arm="V3", snapshot=tmp_path, task_id="vacc-json"))
    contract = subagent_exchange.load_contract(prepared.revision)
    result = {"protocol": "agents-core/subagent/1", "task_id": contract["task_id"],
              "attempt_id": contract["attempt_id"], "bundle_revision": contract["bundle_revision"],
              "contract_revision": prepared.revision, "status": "completed",
              "evidence": [{"id": "e1", "reference": "README.md:1", "description": "present"}], "artifacts": [],
              "checks": [], "limitations": [], "remaining_work": [], "question": None,
              "result": {"summary": "Checked.", "conclusions": [{"claim": "VERDICT: CONFIRMED.", "evidence_ids": ["e1"]}]}}
    path = tmp_path / "result.txt"
    path.write_text(json.dumps(result), encoding="utf-8")
    assert worker.validate(prepared.revision, path) == worker.Validated("verdict", "confirmed", None)


def test_a_probe_without_a_recorded_refusal_is_not_blind(tmp_path, repo):
    control_dir, _ = _build(tmp_path, repo, items=4)
    loaded = runner.load(control_dir, installation_roots=[])

    class Unanswered(FakeLauncher):
        def __call__(self, prompt, **options):
            path = self.home / ".claude" / "projects" / "p" / f"{options['session_id']}.jsonl"
            canary = prompt.split("Try to read the file ")[1].split(":")[0]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"type": "assistant", "cwd": "/", "message": {"id": "m", "model": "x", "content": [
                {"type": "tool_use", "id": "t", "name": "Read", "input": {"file_path": canary}}]}}) + "\n",
                encoding="utf-8")
            return worker.Run(options["session_id"], None, True, None, 1, None, None, "")

    result = runner.probe(loaded, Unanswered(tmp_path / "home", loaded.snapshot), model="m", effort=None, timeout=1)
    assert not result.blind and result.reason == "timeout"


def test_concurrent_attempts_reserve_their_budget(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")
    launcher_home = tmp_path / "home"

    class Capped(FakeLauncher):
        max_budget_usd = 1.0

        def __call__(self, prompt, **options):
            run = super().__call__(prompt, **options)
            return run if prompt.startswith("This is a sandbox check") else worker.Run(
                run.session_id, 0, False, 1.0, 1000, 3, False, "")  # each verifier run costs its whole cap

    work, _, launcher = _runner(tmp_path, repo, lambda loaded: Capped(launcher_home, loaded.snapshot), max_total_usd=2.5)
    work.run()
    rows = work.recorder.recorded()
    started = [row for row in rows if row.get("reason") != "budget"]
    # 2.5 dollars hold two reservations of 1: two concurrent workers start, and none after them.
    # Checking only the spend so far, both would pass again once they finished, overshooting the cap.
    assert len(started) == 2 and work.spent <= 2.5


def test_a_resumed_run_starts_from_the_recorded_spend(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")
    work, loaded, launcher = _runner(tmp_path, repo, lambda loaded: FakeLauncher(tmp_path / "home", loaded.snapshot))
    work.run()
    again = runner.Runner(loaded, launcher, prepare=_prepared, validate=work.validate, log=lambda line: None)
    assert again.spent == pytest.approx(runner.recorded_spend(loaded)) and again.spent > 0


def test_a_runner_error_records_the_later_batches_too(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")

    def failing(arguments):
        raise worker.WorkerError("preparation failed")
    work, loaded, _ = _runner(tmp_path, repo, lambda loaded: FakeLauncher(tmp_path / "home", loaded.snapshot))
    work.prepare = failing
    with pytest.raises(worker.WorkerError):
        work.run()
    rows = work.recorder.recorded()
    assert len(rows) == len(runner.schedule(list(loaded.statements.values()), 0))
    assert {row["reason"] for row in rows} == {"runner_error:WorkerError", "runner_stopped"}


def test_a_below_chance_route_gets_no_proposed_value():
    rng = np.random.default_rng(8)
    units = analysis._simulated_units(0.95, 0.2, [1] * 800, rng)
    result = analysis.fit(units, family="execution", look="final", grid=GRID, coarse=COARSE, rng=rng, resamples=50)
    assert result["below_chance"]
    assert result["parameters"]["b"]["rule"] is None and result["parameters"]["b"]["proposed"] is None


def test_a_cluster_drawn_twice_stays_two_copies():
    rows = _rows([("a", False, "V3", "confirmed"), ("a", False, "V2", "confirmed"),
                  ("b", False, "V3", "refuted"), ("b", False, "V2", "refuted")])
    clusters = {"a": "A", "b": "B"}

    class Fixed:
        def integers(self, low, high, size):
            return np.array([0, 0])  # cluster A twice
    (sample,) = list(analysis._cluster_resamples(rows, clusters, Fixed(), 1))
    assert analysis._independence_once(sample)[2] == 2  # two copies, one V3-V2 pair each, not four pairs


def test_strata_are_compared_within_one_configuration():
    spec = [(f"p{i}", True, "V3", "confirmed") for i in range(100)] + [(f"d{i}", True, "V3", "confirmed") for i in range(10)]
    other = [(f"q{i}", True, "V3", "refuted") for i in range(10)] + [(f"e{i}", True, "V3", "refuted") for i in range(100)]
    rows = _rows(spec) + _rows(other)
    for row in rows[:110]:
        row["stratum"] = "path" if row["item"].startswith("p") else "doc_quote"
    for row in rows[110:]:
        row["stratum"] = "path" if row["item"].startswith("q") else "doc_quote"
        row["model"] = "other"
    statements = {row["item"]: {"cluster": row["item"], "stratum": row["stratum"]} for row in rows}
    report = analysis.analyze(rows, statements, resamples=20, grid=GRID, coarse=COARSE)
    assert not [finding for finding in report["findings"] if "transport" in finding]


def test_discarding_twice_never_overwrites_earlier_records(tmp_path, repo):
    control_dir, _ = _build(tmp_path, repo, items=4)
    first = builder.discard(control_dir)
    _build(tmp_path, repo, items=4)
    second = builder.discard(control_dir)
    assert first != second and (first / control.LABELS).exists() and (second / control.LABELS).exists()
    assert first.name.split("-")[0] == second.name.split("-")[0]  # the same set version, two targets
    (control_dir / control.LABELS).write_text("{}\n", encoding="utf-8")  # a build that stopped before statements
    assert builder.discard(control_dir).name.startswith("incomplete-")


def test_a_constant_bound_again_anywhere_is_undecidable(tmp_path):
    for source in ("LIMIT = 5\nif X:\n    LIMIT = 10\n", "LIMIT = 5\nLIMIT += 1\n", "LIMIT = 5\na, LIMIT = 1, 2\n",
                   "LIMIT = 5\ndef f():\n    global LIMIT\n    LIMIT = 6\n", "LIMIT = 5\nfor LIMIT in range(3):\n    pass\n"):
        (tmp_path / "m.py").write_text(source, encoding="utf-8")
        assert not _facts("m.py", source).get("numeric_constant") or all(
            fact.claim.subject_dict()["name"] != "LIMIT" for fact in _facts("m.py", source)["numeric_constant"])
        claim = strata.Claim("numeric_constant", "m.py", (("name", "LIMIT"), ("value", 5)), "...")
        assert strata.check(claim, tmp_path) is None, source
    (tmp_path / "m.py").write_text("LIMIT = 5\n", encoding="utf-8")
    assert strata.check(strata.Claim("numeric_constant", "m.py", (("name", "LIMIT"), ("value", 5)), "..."), tmp_path)



# --- Review round 2 regressions ----------------------------------------------------------------


def test_a_resumed_run_reschedules_placeholders_and_counts_each_slot_once(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")
    work, loaded, launcher = _runner(tmp_path, repo, lambda loaded: FakeLauncher(tmp_path / "home", loaded.snapshot,
                                                                                 leak_probe=True))
    with pytest.raises(runner.BlindingError):
        work.run()
    scheduled = len(runner.schedule(list(loaded.statements.values()), 0))
    launcher.leak_probe = False  # the blinding is repaired; the same command resumes
    again = runner.Runner(loaded, launcher, prepare=_prepared, validate=work.validate, log=lambda line: None,
                          concurrency=2, batch_size=4)
    assert again.run()["run"] == scheduled
    rows = again.recorder.recorded()
    assert len(rows) == 2 * scheduled  # the placeholders stay on record
    counted = analysis.tally(rows)
    assert counted["attempts"] == scheduled and counted["superseded_placeholders"] == scheduled


def test_a_fallback_charge_survives_a_resume(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")

    class Silent(FakeLauncher):
        max_budget_usd = 0.5

        def __call__(self, prompt, **options):
            run = super().__call__(prompt, **options)
            return run if prompt.startswith("This is a sandbox check") else worker.Run(
                run.session_id, None, False, None, None, None, None, "")  # no cost reported
    work, loaded, _ = _runner(tmp_path, repo, lambda loaded: Silent(tmp_path / "home", loaded.snapshot))
    work.run()
    charged = [row["charged_usd"] for row in work.recorder.recorded() if "charged_usd" in row]
    assert charged and set(charged) == {0.5}
    assert runner.recorded_spend(loaded) >= sum(charged)


def test_a_changed_label_file_is_refused(tmp_path, repo):
    control_dir, _ = _build(tmp_path, repo, items=4)
    labels = control.read_jsonl(control_dir / control.LABELS)
    labels[0]["truth"] = not labels[0]["truth"]
    control.write_jsonl(control_dir / control.LABELS, labels)
    with pytest.raises(control.ControlError, match="label file"):
        runner.load(control_dir, installation_roots=[])


def test_the_snapshot_holds_no_links(tmp_path, repo):
    os.symlink("/etc/hosts", repo / "hosts-link")
    _git(repo, "add", "hosts-link")
    _git(repo, "commit", "-qm", "link")
    commit = builder.resolve_commit(repo, "HEAD")
    snapshot = builder.export_snapshot(repo, commit, tmp_path / "snaps")
    assert not (snapshot / "hosts-link").exists() and not (snapshot / "hosts-link").is_symlink()
    assert (snapshot / "requirements.txt").exists()


@pytest.mark.parametrize("source", [
    "LIMIT = 5\nmatch 6:\n    case LIMIT:\n        pass\n",
    "LIMIT = 5\ndef f(value=(LIMIT := 6)):\n    pass\n",
    "LIMIT = 5\n@decorate(LIMIT := 6)\nclass C:\n    pass\n",
])
def test_captures_and_definition_time_walrus_rebind_a_constant(tmp_path, source):
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    claim = strata.Claim("numeric_constant", "m.py", (("name", "LIMIT"), ("value", 5)), "...")
    assert strata.check(claim, tmp_path) is None


def test_shell_reads_after_cd_and_inside_quoted_code_count(tmp_path):
    snapshot = tmp_path / "snap"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src" / "x.py").write_text("X = 1\n", encoding="utf-8")
    cwd = str(snapshot)
    named = transcript.names_snapshot_path
    assert named("cd src && cat x.py", cwd, snapshot)
    assert named("python3 -c \"print(open('src/x.py').read())\"", cwd, snapshot)
    assert named("cat src/x.py", cwd, snapshot)
    assert not named("cd src && pwd", cwd, snapshot) and not named("pwd", cwd, snapshot)



# --- Review round 3 regressions ----------------------------------------------------------------


def test_coverage_must_be_shown_not_merely_unrefuted():
    assert not analysis.holds_level(30, 30, 0.9875)  # 30 of 30 cannot show 98.75 %
    assert analysis.holds_level(400, 400, 0.9875) and not analysis.holds_level(990, 1000, 0.9875)


def test_a_probe_that_skipped_an_access_is_not_blind(tmp_path, repo):
    control_dir, _ = _build(tmp_path, repo, items=4)
    loaded = runner.load(control_dir, installation_roots=[])

    class ReadOnly(FakeLauncher):
        def __call__(self, prompt, **options):
            path = self.home / ".claude" / "projects" / "p" / f"{options['session_id']}.jsonl"
            canary = prompt.split("Try to read the file ")[1].split(":")[0]
            _transcript(path, cwd="/", calls=[("Read", {"file_path": canary}, False)], final="denied")
            return worker.Run(options["session_id"], 0, False, 0.01, 1, 1, False, "")

    result = runner.probe(loaded, ReadOnly(tmp_path / "home", loaded.snapshot), model="m", effort=None, timeout=1)
    assert not result.blind and result.reason == "incomplete_denial"


def test_an_error_after_launch_is_a_paid_attempt_never_rescheduled(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")

    def broken(revision, path):
        raise RuntimeError("transcript processing failed")
    work, loaded, _ = _runner(tmp_path, repo, lambda loaded: FakeLauncher(tmp_path / "home", loaded.snapshot),
                              validate=broken)
    work.run()
    rows = work.recorder.recorded()
    assert {row["reason"] for row in rows} == {"post_launch_error:RuntimeError"}
    assert all(row["cost_usd"] == 0.01 for row in rows) and not any(control.is_placeholder(row) for row in rows)
    assert work.run()["run"] == 0


def test_a_shell_comment_names_no_file(tmp_path):
    snapshot = tmp_path / "snap"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src" / "x.py").write_text("X = 1\n", encoding="utf-8")
    assert not transcript.names_snapshot_path("true # src/x.py", str(snapshot), snapshot)


def test_run_refuses_an_interim_simulation_and_probe_cost_is_recorded(tmp_path, repo):
    from evals.scripts import verifier_accuracy as cli
    control_dir, manifest = _build(tmp_path, repo, items=4)
    (control_dir / control.SIMULATION).write_text(json.dumps({"set": manifest["set"], "held": True, "look": "interim"}),
                                                  encoding="utf-8")
    assert cli.main(["--installation", str(tmp_path / "installation"), "--control-dir", str(control_dir), "run"]) == 1
    loaded = runner.load(control_dir, installation_roots=[])
    runner.record_probe(loaded, runner.ProbeResult(True, 4, "denied", 0.25), model="m", batch=None)
    assert runner.recorded_spend(loaded) == pytest.approx(0.25)



# --- Review round 4 regressions ----------------------------------------------------------------


def test_four_refused_reads_do_not_stand_for_the_shell_methods(tmp_path, repo):
    control_dir, _ = _build(tmp_path, repo, items=4)
    loaded = runner.load(control_dir, installation_roots=[])

    class ReadsOnly(FakeLauncher):
        def __call__(self, prompt, **options):
            path = self.home / ".claude" / "projects" / "p" / f"{options['session_id']}.jsonl"
            canary = prompt.split("Try to read the file ")[1].split(":")[0]
            _transcript(path, cwd="/", calls=[("Read", {"file_path": canary}, False)] * 4, final="denied")
            return worker.Run(options["session_id"], 0, False, 0.01, 1, 1, False, "")

    result = runner.probe(loaded, ReadsOnly(tmp_path / "home", loaded.snapshot), model="m", effort=None, timeout=1)
    assert not result.blind and result.reason == "incomplete_denial"
    blind = runner.probe(loaded, FakeLauncher(tmp_path / "home", loaded.snapshot), model="m", effort=None, timeout=1)
    assert blind.blind and blind.reason == "denied"  # the four methods, each refused


def test_each_look_simulates_enough_sets_to_show_its_level():
    assert analysis.minimum_sets("final") < 1000 <= analysis.default_sets("final")
    assert analysis.default_sets("interim") >= analysis.minimum_sets("interim") > 1000
    with pytest.raises(ValueError, match="cannot show the interim"):
        analysis.calibrate([], sets=1000, look="interim")



def test_the_probe_method_is_the_executable_not_a_word_in_the_path():
    canary = "/tmp/python-controls/claims-control/canary.txt"
    call = lambda command: transcript.ToolCall("Bash", {"command": command}, "/", False)
    assert runner.probe_method(call(f"cat {canary}")) == "cat"
    assert runner.probe_method(call(f"head -c 200 {canary}")) == "head"
    assert runner.probe_method(call(f"python3 -c \"print(open('{canary}').read())\"")) == "python"
    assert runner.probe_method(call(f"cd /tmp && /usr/bin/cat {canary}")) == "cat"
    assert runner.probe_method(transcript.ToolCall("Read", {"file_path": canary}, "/", False)) == "read"


# --- Staged run --------------------------------------------------------------------------------


def _statements(sizes: dict[str, int]) -> list[dict]:
    rows, index = [], 0
    for cluster, size in sizes.items():
        for _ in range(size):
            rows.append({"item": f"i{index:03d}", "cluster": cluster, "v2": index % 3 != 0, "stratum": "path"})
            index += 1
    return rows


def test_the_schedule_keeps_clusters_together_and_a_stage_takes_whole_ones():
    statements = _statements({"a": 3, "b": 1, "c": 4, "d": 2, "e": 5})
    cluster_of = {row["item"]: row["cluster"] for row in statements}
    slots = runner.schedule(statements, 3)
    assert sorted(slot.key for slot in slots) == sorted(slot.key for row in statements for slot in runner._slots(row))
    order = [cluster_of[slot.item] for slot in slots]
    assert all(order.index(name) + order.count(name) - 1 == len(order) - 1 - order[::-1].index(name)
               for name in set(order))  # every cluster is one contiguous block
    assert runner.schedule(list(reversed(statements)), 3) == slots  # the input order does not matter
    orders = {tuple(dict.fromkeys(cluster_of[slot.item] for slot in runner.schedule(statements, seed)))
              for seed in range(8)}
    assert len(orders) > 1 and orders != {tuple(sorted(set(order)))}  # a stage is a seeded cluster sample
    assert runner.stage(statements, 3, None) == slots
    for runs in (0, 5, 17, 30, len(slots)):
        taken = runner.stage(statements, 3, runs)
        assert len(taken) <= runs and taken == slots[:len(taken)]
        clusters = {cluster_of[slot.item] for slot in taken}
        assert all(sum(cluster_of[slot.item] == name for slot in taken)
                   == sum(cluster_of[slot.item] == name for slot in slots) for name in clusters)  # whole clusters
        following = slots[len(taken):]
        assert not following or len(taken) + sum(
            cluster_of[slot.item] == cluster_of[following[0].item] for slot in following) > runs  # nothing more fits


def test_a_staged_run_takes_its_clusters_and_a_later_run_continues(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(runner.Runner, "_probe_model", lambda self: "sonnet")
    work, loaded, launcher = _runner(tmp_path, repo, lambda loaded: FakeLauncher(tmp_path / "home", loaded.snapshot),
                                     stage_runs=10)
    statements = list(loaded.statements.values())
    expected = runner.stage(statements, 0, 10)
    summary = work.run()
    assert 0 < summary["run"] == summary["scheduled"] == len(expected) <= 10
    assert {(row["item"], row["arm"], row["slot"]) for row in work.recorder.recorded()} == {
        slot.key for slot in expected}
    rest = runner.Runner(loaded, launcher, prepare=_prepared, validate=work.validate, log=lambda line: None,
                         concurrency=2, batch_size=4)
    assert rest.run()["run"] == len(runner.schedule(statements, 0)) - len(expected)
    assert len(rest.recorder.recorded()) == len(runner.schedule(statements, 0))  # each slot once


def test_the_planning_look_applies_no_replacement_rule():
    rng = np.random.default_rng(5)
    units = analysis._simulated_units(0.9, 0.85, [1] * 600, rng)
    final, planned = (analysis.fit(units, family="model", look=look, grid=GRID, coarse=COARSE,
                                   rng=np.random.default_rng(6), resamples=50) for look in ("final", "planning"))
    assert final["parameters"]["c"]["rule"] == 3  # the prior 0.97 lies above the band
    assert {planned["parameters"][name]["rule"] for name in ("c", "b")} == {None}
    assert {planned["parameters"][name]["proposed"] for name in ("c", "b")} == {None}
    assert planned["parameters"]["c"]["band"] == final["parameters"]["c"]["band"]  # the final look's quantiles
    assert "planning" not in analysis.SIMULATED_LOOKS


def test_a_lost_verdict_leaves_one_and_an_item_without_any_is_dropped():
    rng = np.random.default_rng(1)
    whole = analysis._simulated_units(0.9, 0.9, [2] * 500, rng)
    assert {unit.n for unit in whole} == {2} and len(whole) == 1000
    thinned = analysis._simulated_units(0.9, 0.9, [2] * 500, rng, verdict_yield=0.5)
    assert {unit.n for unit in thinned} == {1, 2} and 650 < len(thinned) < 850  # 3/4 keep at least one


def test_the_yield_counts_launched_slots_of_the_arm_only():
    rows = _rows([(f"i{i}", True, "V3", "confirmed") for i in range(6)] + [("j0", True, "unsupported", "confirmed")])
    rows += [{**rows[0], "item": "x", "status": "invalid", "level": None},
             {**rows[0], "item": "y", "status": "unfinished", "reason": "budget"}]  # a placeholder never ran
    assert analysis.stage_yield(rows, "model") == pytest.approx(6 / 8)
    assert analysis.stage_yield(rows, "execution") is None


def test_the_projection_reports_the_whole_set_and_never_a_rule_case_of_the_stage():
    spec = []
    for i in range(60):
        wrong = i % 10 == 0
        spec += [(f"i{i}", True, "V3", "refuted" if wrong else "confirmed")] * 2
    rows = _rows(spec)
    for index, row in enumerate(rows):
        row["slot"] = 1 + index % 2
    statements = {f"i{i}": {"cluster": f"k{i // 2}", "stratum": "path", "v2": False} for i in range(240)}
    report = analysis.analyze(rows, statements, look="planning", resamples=30, grid=GRID, coarse=COARSE)
    plan = analysis.planning(rows, statements, report, d_floor=1.0, sets=20, resamples=20, grid=GRID, coarse=COARSE)
    (projection,) = plan["projections"]
    model = projection["families"]["model"]
    assert model["items"] == 240 and model["clusters"] == 120 and model["verdict_yield"] == 1.0
    stage_width = report["configurations"][0]["families"]["model"]["all"]["parameters"]["b"]["half_width"]
    assert model["b"]["half_width"]["median"] < stage_width  # four times the items narrow the band
    assert sum(model["b"]["rule_shares"].values()) == pytest.approx(1.0)
    assert plan["slots"] == {"set": 480, "launched": 120, "remaining": 360}
    assert plan["remaining_usd"] == pytest.approx(36.0)
    assert "execution" not in projection["families"]


def test_counts_that_would_run_or_simulate_nothing_are_refused(capsys):
    from evals.scripts import verifier_accuracy as cli
    for argv in (["run", "--stage-runs", "0"], ["run", "--stage-runs", "-1"], ["analyze", "--projection-sets", "0"]):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(argv)
        assert "must be at least 1" in capsys.readouterr().err
    assert cli.parser().parse_args(["run", "--stage-runs", "220"]).stage_runs == 220
