"""The runner: every scheduled attempt through the exchange, a canary probe before each batch.

docs/verifier-accuracy-measurement.md, "The runs" and "Recording path". The schedule is two
V3-arm slots per item and two V2-arm slots per item of the execution subset, in blocks: clusters
in a seeded order, every slot of a cluster's items together. A run may stop at a stage, the
complete clusters whose slots fit a run count, so a stage is a cluster sample of the set and its
design effect estimates the whole set's; within what a run takes, slots are shuffled so arms and
items interleave. Before each batch a probe worker, launched exactly like a verifier,
tries to read the canary file; a read that succeeds, or a probe that never tried, stops the
run. Every scheduled attempt gets one ``control_attempt`` line, including those a stop or the
budget left unrun (``unfinished`` with a reason). Printed progress names item IDs and
statuses only, never a statement, a label, a verdict or an outcome.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
import threading
from typing import Callable
import uuid

from evals.verifier_accuracy import control, transcript, worker
from src.engine.subagent import prepare_subagent

PROBE_METHODS = ("read", "cat", "head", "python")  # each must be tried and refused, as the prompt asks
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


def _slots(row: dict) -> list[Slot]:
    arms = ("V3", "V2") if row["v2"] else ("V3",)
    return [Slot(row["item"], arm, number) for arm in arms for number in control.SLOTS]


def schedule(statements: list[dict], seed: int) -> list[Slot]:
    """Every slot of the set, cluster by cluster in a seeded order; statements only, never labels."""
    rng = random.Random(seed)
    clusters: dict[str, list[dict]] = {}
    for row in sorted(statements, key=lambda row: row["item"]):
        clusters.setdefault(row["cluster"], []).append(row)
    names = sorted(clusters)
    rng.shuffle(names)
    slots = []
    for name in names:
        members = clusters[name]
        rng.shuffle(members)
        slots += [slot for row in members for slot in _slots(row)]
    return slots


def stage(statements: list[dict], seed: int, runs: int | None) -> list[Slot]:
    """The leading complete clusters of the schedule whose slots fit ``runs``; all of them for None."""
    slots = schedule(statements, seed)
    if runs is None:
        return slots
    cluster_of = {row["item"]: row["cluster"] for row in statements}
    taken, current, block = [], None, []
    for slot in slots + [None]:
        cluster = None if slot is None else cluster_of[slot.item]
        if cluster != current:
            if len(taken) + len(block) > runs:
                break
            taken += block
            current, block = cluster, []
        if slot is not None:
            block.append(slot)
    return taken


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
    if control.file_digest(control_dir / control.LABELS) != manifest.get("labels_sha256"):
        raise control.ControlError("The label file no longer matches the one the set was built with.")
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
    tries = [call for call in facts.calls if str(canary_path) in json.dumps(call.input)]
    if facts.mentions(token) or token in run.output:
        return ProbeResult(False, len(tries), "canary_read", run.cost_usd)
    if run.timed_out:
        return ProbeResult(False, len(tries), "timeout", run.cost_usd)
    # Only recorded refusals are evidence: every access the prompt prescribes must have been
    # tried and refused; a call without a result, a success or a skipped method proves nothing.
    if not tries:
        return ProbeResult(False, 0, "no_attempt", run.cost_usd)
    methods = {probe_method(call) for call in tries}
    if any(call.ok is not False for call in tries) or not set(PROBE_METHODS) <= methods:
        return ProbeResult(False, len(tries), "incomplete_denial", run.cost_usd)
    return ProbeResult(True, len(tries), "denied", run.cost_usd)


_COMMAND_SEPARATORS = frozenset({"|", "||", "&&", ";", "&"})


def probe_method(call) -> str | None:
    """Which of the prescribed canary accesses a recorded call is, by the executable it runs.

    Only the first word of each command in the line counts, so a canary path that holds a
    word such as ``python`` never decides the method of ``cat PATH``.
    """
    if call.name == "Read":
        return "read"
    if call.name not in transcript.EXECUTION_TOOLS:
        return None
    command = str(call.input.get("command", ""))
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    executables = [token for index, token in enumerate(tokens)
                   if index == 0 or tokens[index - 1] in _COMMAND_SEPARATORS]
    for executable in (os.path.basename(token) for token in executables):
        if executable.startswith("python"):
            return "python"
        if executable in ("cat", "head"):
            return executable
    return None


def record_probe(loaded: LoadedSet, result: ProbeResult, *, model: str, batch: int | None) -> dict:
    """Append one canary_probe line, so every probe's cost counts towards the set's spend."""
    entry = {"at": control.now(), "event": "canary_probe", "set": loaded.set_id, "batch": batch, "model": model,
             "blind": result.blind, "attempts": result.attempts, "reason": result.reason,
             "cost_usd": result.cost_usd}
    _append(loaded.control_dir / control.PROBES, entry)
    return entry


class Runner:
    def __init__(self, loaded: LoadedSet, launcher: Callable, *, concurrency: int = 4, batch_size: int = 20,
                 timeout: float = 900.0, max_total_usd: float = 40.0, seed: int = 0,
                 stage_runs: int | None = None, prepare: Callable = worker.prepare,
                 validate: Callable = worker.validate, log: Callable[[str], None] = print):
        self.loaded = loaded
        self.launcher = launcher
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.timeout = timeout
        self.max_total_usd = max_total_usd
        self.seed = seed
        self.stage_runs = stage_runs
        self.prepare = prepare
        self.validate = validate
        self.log = log
        self.recorder = control.AttemptRecorder(loaded.control_dir / control.ATTEMPTS)
        # A run reserves the per-run budget the CLI enforces before it starts, so concurrent
        # attempts cannot overshoot the total; a resumed run starts from what the set spent.
        self.run_reserve_usd = getattr(launcher, "max_budget_usd", 2.0)
        self.spent = recorded_spend(loaded)
        self.reserved = 0.0
        self._lock = threading.Lock()
        self.home = getattr(launcher, "home", Path.home())
        self.user_instructions = user_instructions(self.home, loaded.snapshot)

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
        launched = dict(record)
        try:
            return self._assess(slot, record, run, prepared)
        except Exception as error:  # the worker ran and was paid for: recorded, never rescheduled
            return {**launched, "reason": f"post_launch_error:{type(error).__name__}"}

    def _assess(self, slot: Slot, record: dict, run, prepared) -> dict:
        execution = prepared.package.execution
        found = transcript.find(run.session_id, self.home / ".claude")
        if run.timed_out or found is None:
            return {**record, "reason": "timeout" if run.timed_out else "no_transcript"}
        facts = transcript.read(found)
        # Host context the configuration key does not name: the CLI version, and whether the
        # user's own instructions reached the worker (Claude Code loads ~/.claude/CLAUDE.md for
        # a working directory under the home directory, whatever --setting-sources says).
        record["cli_version"] = facts.versions[0] if len(facts.versions) == 1 else None
        record["user_instructions"] = self.user_instructions
        record["model"] = transcript.resolved_model(facts)
        record["reasoning_effort"] = transcript.resolved_effort(facts) or execution.reasoning_effort
        if record["model"] is None:
            record["excluded"] = "no_model"
        # The note's second check. The full path anywhere discards; the bare directory name only in
        # what the worker sent, since files of the repository itself name it once this code is in it.
        if facts.mentions(str(self.loaded.control_dir)) or any(
                control.DIRECTORY_NAME in json.dumps(call.input) for call in facts.calls):
            record["discarded"] = "control_path"
        if any(str(self.home / ".claude" / "projects") in json.dumps(call.input) for call in facts.calls):
            # The sandbox denies other runs' records, so an attempt leaked nothing; it is counted.
            record["probed"] = "session_records"
        checked = self.validate(prepared.revision, found)
        record["status"] = checked.status
        if checked.reason:
            record["reason"] = checked.reason
        if checked.status == "verdict":
            level = transcript.level(slot.arm, facts, self.loaded.snapshot)
            record.update(level=level, family=control.LEVEL_FAMILY[level], verdict=checked.verdict,
                          outcome=control.outcome(record["truth"], checked.verdict))
        return record

    def _affordable(self) -> bool:
        return self.spent + self.reserved + self.run_reserve_usd <= self.max_total_usd

    def _guarded(self, slot: Slot) -> dict:
        with self._lock:
            if not self._affordable():
                return {**self._base(slot), "reason": "budget"}
            self.reserved += self.run_reserve_usd
        cost = self.run_reserve_usd  # a run that reports no cost (killed at the timeout) is charged its cap
        try:
            record = self.attempt(slot)
            if record.get("cost_usd") is not None:
                cost = record["cost_usd"]
            else:
                record["charged_usd"] = cost  # kept apart from the host-reported cost, so a resume restores it
            return record
        finally:
            with self._lock:
                self.reserved -= self.run_reserve_usd
                self.spent += cost

    def _write(self, record: dict) -> None:
        written = self.recorder.write(record)
        self.log(f"{written['item']} {written['arm']}/{written['slot']}: {written['status']}"
                 + (f" ({written['reason']})" if written.get("reason") else ""))

    def run(self) -> dict:
        # A slot is done once an attempt ran; a placeholder for one that never ran is scheduled again,
        # and stays in the file, so the earlier stop remains on record.
        done = {(row["item"], row["arm"], row["slot"]) for row in self.recorder.recorded()
                if not control.is_placeholder(row)}
        selected = stage(list(self.loaded.statements.values()), self.seed, self.stage_runs)
        pending = [slot for slot in selected if slot.key not in done]
        random.Random(self.seed).shuffle(pending)  # arms and items interleave within what this run takes
        probes = []
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start:start + self.batch_size]
            if not self._affordable():  # no paid probe for a batch the budget cannot run
                for slot in pending[start:]:
                    self._write({**self._base(slot), "reason": "budget"})
                break
            model = self._probe_model()  # the probe runs on the model the verifiers of this set run on
            result = probe(self.loaded, self.launcher, model=model, effort=None, timeout=self.timeout)
            self.spent += result.cost_usd or 0.0
            entry = record_probe(self.loaded, result, model=model, batch=start // self.batch_size + 1)
            probes.append(entry)
            self.log(f"probe {entry['batch']}: {result.reason} ({result.attempts} read attempts)")
            if not result.blind:
                for slot in pending[start:]:
                    self._write({**self._base(slot), "reason": "probe_failed"})
                raise BlindingError(f"The canary probe before batch {entry['batch']} returned {result.reason}.")
            try:
                self._run_batch(batch)
            except worker.WorkerError:
                for slot in pending[start + self.batch_size:]:  # every scheduled attempt keeps a record
                    self._write({**self._base(slot), "reason": "runner_stopped"})
                raise
        return {"scheduled": len(selected), "run": len(pending), "probes": probes,
                "spent_usd": round(self.spent, 4)}

    def _run_batch(self, batch: list[Slot]) -> None:
        """Write each attempt as it finishes; an attempt that raised is recorded, then the run stops."""
        failed: list[tuple[Slot, BaseException]] = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self._guarded, slot): slot for slot in batch}
            try:
                for future in as_completed(futures):
                    error = future.exception()
                    if error is not None:
                        failed.append((futures[future], error))
                        continue
                    self._write(future.result())
            except control.RecorderError:
                pool.shutdown(wait=True, cancel_futures=True)  # start no further worker; then stop the run
                raise
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


def recorded_spend(loaded: LoadedSet) -> float:
    """What this set's recorded attempts and probes already cost."""
    total = 0.0
    for name in (control.ATTEMPTS, control.PROBES):
        path = loaded.control_dir / name
        if path.exists():
            total += sum(row.get("cost_usd") or row.get("charged_usd") or 0.0 for row in control.read_jsonl(path)
                         if row.get("set") == loaded.set_id)
    return total


def user_instructions(home: Path, snapshot: Path) -> str | None:
    """The first 16 hexadecimal characters of the SHA-256 of ``~/.claude/CLAUDE.md`` when the worker
    loads it, which it does for a snapshot under the home directory; ``None`` otherwise."""
    file = home / ".claude" / "CLAUDE.md"
    under_home = os.path.realpath(snapshot).startswith(os.path.realpath(home).rstrip(os.sep) + os.sep)
    if not under_home or not file.is_file():
        return None
    return hashlib.sha256(file.read_bytes()).hexdigest()[:16]


def _append(path: Path, entry: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
