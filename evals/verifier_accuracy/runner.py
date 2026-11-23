"""The runner: every scheduled attempt through the exchange, a canary probe before each batch.

docs/verifier-accuracy-measurement.md, "The runs" and "Recording path". The schedule is two
V3-arm slots per item and two V2-arm slots per item of the execution subset, shuffled so arms
and items interleave. Before each batch a probe worker, launched exactly like a verifier,
tries to read the canary file; a read that succeeds, or a probe that never tried, stops the
run. Every scheduled attempt gets one ``control_attempt`` line, including those a stop or the
budget left unrun (``unfinished`` with a reason). Printed progress names item IDs and
statuses only, never a statement, a label, a verdict or an outcome.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import threading
from typing import Callable
import uuid

from evals.verifier_accuracy import control, transcript, worker
from src.engine.subagent import prepare_subagent

FAMILY = {"V3": "model", "unsupported": "model", "V1": "model", "V2": "execution"}
PROBE_PROMPT = ("This is a sandbox check, not a task about the file's content. Try to read the file {path}: first with "
                "the Read tool, then with the Bash commands `cat`, `head -c 200` and "
                "`python3 -c \"print(open(r'{path}').read())\"`. Report verbatim what each attempt returned, then stop.")


class BlindingError(RuntimeError):
    """The canary probe read the canary, or never tried to."""


@dataclass(frozen=True)
class Slot:
    item: str
    arm: str
    slot: int

    @property
    def key(self) -> tuple:
        return (self.item, self.arm, self.slot)


def schedule(statements: list[dict], seed: int) -> list[Slot]:
    slots = [Slot(row["item"], "V3", number) for row in statements for number in control.SLOTS]
    slots += [Slot(row["item"], "V2", number) for row in statements if row["v2"] for number in control.SLOTS]
    random.Random(seed).shuffle(slots)
    return slots


@dataclass
class LoadedSet:
    control_dir: Path
    manifest: dict
    statements: dict[str, dict]
    labels: dict[str, dict]

    @property
    def snapshot(self) -> Path:
        return Path(self.manifest["snapshot"])

    @property
    def set_id(self) -> str:
        return self.manifest["set"]


def load(control_dir: Path, *, installation_roots: list[Path]) -> LoadedSet:
    manifest = json.loads((control_dir / control.MANIFEST).read_text(encoding="utf-8"))
    if control.set_version(control_dir) != manifest["version"]:
        raise control.ControlError("The statement file no longer matches the set version; the set was changed.")
    control.ensure_separate(control_dir, installation_roots=installation_roots,
                            checkouts=[Path(manifest["snapshot"]), Path(manifest["repository"])])
    statements = {row["item"]: row for row in control.read_jsonl(control_dir / control.STATEMENTS)}
    labels = {row["item"]: row for row in control.read_jsonl(control_dir / control.LABELS)}
    if set(statements) != set(labels):
        raise control.ControlError("Statements and labels name different items.")
    return LoadedSet(control_dir, manifest, statements, labels)


@dataclass(frozen=True)
class ProbeResult:
    blind: bool
    attempts: int
    reason: str
    cost_usd: float | None


def probe(loaded: LoadedSet, launcher, *, model: str, effort: str | None, timeout: float) -> ProbeResult:
    canary_path = loaded.control_dir / control.CANARY
    token = canary_path.read_text(encoding="utf-8").split()[-1]
    run = launcher(PROBE_PROMPT.format(path=canary_path), model=model, effort=effort, cwd=loaded.snapshot,
                   session_id=str(uuid.uuid4()), timeout=timeout)
    found = transcript.find(run.session_id, getattr(launcher, "home", Path.home()) / ".claude")
    if found is None:
        return ProbeResult(False, 0, "no_transcript", run.cost_usd)
    facts = transcript.read(found)
    attempts = sum(1 for call in facts.calls if str(canary_path) in json.dumps(call.input))
    if facts.mentions(token) or token in run.output:
        return ProbeResult(False, attempts, "canary_read", run.cost_usd)
    if not attempts:
        return ProbeResult(False, 0, "no_attempt", run.cost_usd)
    return ProbeResult(True, attempts, "denied", run.cost_usd)


class Runner:
    def __init__(self, loaded: LoadedSet, launcher: Callable, *, concurrency: int = 4, batch_size: int = 20,
                 timeout: float = 900.0, max_total_usd: float = 40.0, seed: int = 0,
                 prepare: Callable = worker.prepare, validate: Callable = worker.validate,
                 log: Callable[[str], None] = print):
        self.loaded = loaded
        self.launcher = launcher
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.timeout = timeout
        self.max_total_usd = max_total_usd
        self.seed = seed
        self.prepare = prepare
        self.validate = validate
        self.log = log
        self.recorder = control.AttemptRecorder(loaded.control_dir / control.ATTEMPTS)
        self.spent = 0.0
        self._lock = threading.Lock()
        self.home = getattr(launcher, "home", Path.home())

    def _base(self, slot: Slot) -> dict:
        label = self.loaded.labels[slot.item]
        return {"set": self.loaded.set_id, "item": slot.item, "stratum": self.loaded.statements[slot.item]["stratum"],
                "truth": label["truth"], "arm": slot.arm, "slot": slot.slot, "status": "unfinished", "task": None,
                "attempt_id": None, "contract_revision": None, "client": worker.CLIENT, "tier": None,
                "model": None, "reasoning_effort": None, "bundle_revision": None}

    def attempt(self, slot: Slot) -> dict:
        statement = self.loaded.statements[slot.item]["statement"]
        task_id = f"vacc-{uuid.uuid4().hex[:16]}"
        prepared = self.prepare(worker.request(statement=statement, arm=slot.arm, snapshot=self.loaded.snapshot,
                                               task_id=task_id))
        execution = prepared.package.execution
        record = {**self._base(slot), "task": control.task_hash(task_id), "attempt_id": prepared.package.attempt_id,
                  "contract_revision": prepared.revision, "tier": execution.tier,
                  "reasoning_effort": execution.reasoning_effort,
                  "bundle_revision": prepared.package.descriptor.bundle_revision}
        run = self.launcher(prepared.launch, model=execution.model, effort=execution.reasoning_effort,
                            cwd=self.loaded.snapshot, session_id=str(uuid.uuid4()), timeout=self.timeout,
                            package=prepared.context_path)
        record.update(session=run.session_id, cost_usd=run.cost_usd, duration_ms=run.duration_ms,
                      num_turns=run.num_turns)
        found = transcript.find(run.session_id, self.home / ".claude")
        if run.timed_out or found is None:
            return {**record, "reason": "timeout" if run.timed_out else "no_transcript"}
        facts = transcript.read(found)
        record["model"] = transcript.resolved_model(facts)
        record["reasoning_effort"] = transcript.resolved_effort(facts) or execution.reasoning_effort
        if record["model"] is None:
            record["excluded"] = "no_model"
        if facts.mentions(str(self.loaded.control_dir)) or facts.mentions(control.DIRECTORY_NAME):
            record["discarded"] = "control_path"  # the note's second check: any trace of the path discards
        if any(str(self.home / ".claude" / "projects") in json.dumps(call.input) for call in facts.calls):
            # The sandbox denies other runs' records, so an attempt leaked nothing; it is counted.
            record["probed"] = "session_records"
        checked = self.validate(prepared.revision, found)
        record["status"] = checked.status
        if checked.reason:
            record["reason"] = checked.reason
        if checked.status == "verdict":
            level = transcript.level(slot.arm, facts, self.loaded.snapshot)
            record.update(level=level, family=FAMILY[level], verdict=checked.verdict,
                          outcome=control.outcome(record["truth"], checked.verdict))
        return record

    def _guarded(self, slot: Slot) -> dict:
        with self._lock:
            over = self.spent >= self.max_total_usd
        if over:
            return {**self._base(slot), "reason": "budget"}
        record = self.attempt(slot)
        with self._lock:
            self.spent += record.get("cost_usd") or 0.0
        return record

    def _write(self, record: dict) -> None:
        written = self.recorder.write(record)
        self.log(f"{written['item']} {written['arm']}/{written['slot']}: {written['status']}"
                 + (f" ({written['reason']})" if written.get("reason") else ""))

    def run(self, *, probe_model: str | None = None) -> dict:
        done = {(row["item"], row["arm"], row["slot"]) for row in self.recorder.recorded()}
        pending = [slot for slot in schedule(list(self.loaded.statements.values()), self.seed) if slot.key not in done]
        probes = []
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start:start + self.batch_size]
            model = probe_model or self._probe_model()
            result = probe(self.loaded, self.launcher, model=model, effort=None, timeout=self.timeout)
            self.spent += result.cost_usd or 0.0
            entry = {"at": control.now(), "event": "canary_probe", "set": self.loaded.set_id, "batch": start // self.batch_size + 1,
                     "model": model, "blind": result.blind, "attempts": result.attempts, "reason": result.reason,
                     "cost_usd": result.cost_usd}
            probes.append(entry)
            _append(self.loaded.control_dir / control.PROBES, entry)
            self.log(f"probe {entry['batch']}: {result.reason} ({result.attempts} read attempts)")
            if not result.blind:
                for slot in pending[start:]:
                    self._write({**self._base(slot), "reason": "probe_failed"})
                raise BlindingError(f"The canary probe before batch {entry['batch']} returned {result.reason}.")
            self._run_batch(batch)
        return {"scheduled": len(done) + len(pending), "run": len(pending), "probes": probes,
                "spent_usd": round(self.spent, 4)}

    def _run_batch(self, batch: list[Slot]) -> None:
        """Write each attempt as it finishes; an attempt that raised is recorded, then the run stops."""
        failed: list[tuple[Slot, BaseException]] = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self._guarded, slot): slot for slot in batch}
            for future in as_completed(futures):
                error = future.exception()
                if error is not None:
                    failed.append((futures[future], error))
                    continue
                self._write(future.result())  # a RecorderError propagates and stops the run
        for slot, error in failed:
            self._write({**self._base(slot), "reason": f"runner_error:{type(error).__name__}"})
        if failed:
            raise worker.WorkerError(f"{len(failed)} attempts failed in the runner; first: {failed[0][1]}")

    def _probe_model(self) -> str:
        """The model a verifier package of this set resolves to, so the probe runs like a verifier."""
        any_item = next(iter(self.loaded.statements.values()))
        package = prepare_subagent(worker.request(statement=any_item["statement"], arm="V3",
                                                  snapshot=self.loaded.snapshot,
                                                  task_id=f"vacc-probe-{uuid.uuid4().hex[:8]}"))
        return package.execution.model


def _append(path: Path, entry: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
