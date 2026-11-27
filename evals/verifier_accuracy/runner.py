"""The runner: every scheduled attempt through the exchange, a canary probe before each batch.

docs/verifier-accuracy-measurement.md, "The runs" and "Recording path". The schedule is two
V3-arm slots per item and two V2-arm slots per item of the execution subset, in blocks: clusters
in a seeded order, every slot of a cluster's items together. One seed orders a set: every record
names it, and a run with another seed is refused. A run may stop at a stage, the leading complete
clusters, as many as a run count holds on average, so a stage is a simple random sample of the
set's clusters and its design effect estimates the whole set's; within what a run takes, slots
are shuffled so arms and items interleave. Before each batch a probe worker, launched exactly
like a verifier, tries to read the canary file; a read that succeeds, or a probe that never
tried, stops the run. Every scheduled attempt gets one ``control_attempt`` line, including those a stop or the
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


class ScheduleError(control.ControlError):
    """A run or a look whose schedule is not the set's own: another seed, or a stage that holds no
    cluster."""


# Records from before the seed field had no way to name their seed; every one was scheduled with
# the default, 0 (stage 1 of the 240-item set ran with --seed 0), so only that seed continues them.
LEGACY_SEED = 0


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


def _blocks(statements: list[dict], seed: int) -> list[list[Slot]]:
    """Each cluster's slots, the clusters in the seeded order; statements only, never labels."""
    rng = random.Random(seed)
    clusters: dict[str, list[dict]] = {}
    for row in sorted(statements, key=lambda row: row["item"]):
        clusters.setdefault(row["cluster"], []).append(row)
    names = sorted(clusters)
    rng.shuffle(names)
    blocks = []
    for name in names:
        members = clusters[name]
        rng.shuffle(members)
        blocks.append([slot for row in members for slot in _slots(row)])
    return blocks


def schedule(statements: list[dict], seed: int) -> list[Slot]:
    """Every slot of the set, cluster by cluster in a seeded order; statements only, never labels."""
    return [slot for block in _blocks(statements, seed) for slot in block]


def stage(statements: list[dict], seed: int, runs: int | None) -> list[Slot]:
    """The leading complete clusters of the schedule, as many as ``runs`` slots hold on average;
    all of them for None.

    The count comes from the budget alone, ``runs × clusters // slots``, never from the sizes of
    the clusters the seed put first, so every cluster has the same chance to be in a stage and a
    stage is a simple random sample of the set's clusters. Cutting where the leading clusters stop
    fitting would leave out the cluster that crosses the limit, more often a large one, and would
    never take a cluster with more slots than the limit. A stage's slots average at most ``runs``
    and vary with the clusters drawn, so they can exceed it.
    """
    blocks = _blocks(statements, seed)
    if runs is not None and blocks:
        total = sum(map(len, blocks))
        count = min(len(blocks), runs * len(blocks) // total)
        if not count:
            raise ScheduleError(f"{runs} runs hold no cluster: this set's {len(blocks)} clusters average "
                                f"{total / len(blocks):.1f} slots.")
        blocks = blocks[:count]
    return [slot for block in blocks for slot in block]


def schedule_seed(rows: list[dict]) -> int | None:
    """The schedule seed the attempt records name; None when none does (records older than the field)."""
    seeds = {row["seed"] for row in rows if "seed" in row}
    if len(seeds) > 1:
        raise ScheduleError(f"The attempt records name {len(seeds)} schedule seeds; a set has one.")
    return next(iter(seeds), None)


def check_seed(rows: list[dict], statements: list[dict], seed: int) -> None:
    """Refuse a seed other than the one the set's records were scheduled with.

    Records written before the seed was recorded name none. They continue only with
    ``LEGACY_SEED``, the seed they were run with, and only when the clusters they hold are exactly
    the leading clusters of its order, as every stage's are.
    """
    recorded = schedule_seed(rows)
    if recorded is not None:
        if recorded != seed:
            raise ScheduleError(f"This set is scheduled with seed {recorded}; seed {seed} orders its clusters "
                                "differently, so its stages would not be the leading clusters of one order.")
        return
    if rows and seed != LEGACY_SEED:
        raise ScheduleError(f"This set's records predate the seed field and were scheduled with seed "
                            f"{LEGACY_SEED}; seed {seed} is refused.")
    cluster_of = {row["item"]: row["cluster"] for row in statements}
    held = {cluster_of[row["item"]] for row in rows}
    leading = {cluster_of[block[0].item] for block in _blocks(statements, seed)[:len(held)]}
    if held != leading:
        raise ScheduleError(f"The set's {len(held)} recorded clusters are not the leading clusters of seed "
                            f"{seed}'s order: run with the seed of the first stage.")


def first_half(statements: list[dict], rows: list[dict]) -> list[Slot]:
    """The interim look's slots: the stage of half the set's slots under the seed the records name."""
    seed = schedule_seed(rows)
    if seed is None:
        raise ScheduleError("No attempt record names a schedule seed, so the first half has no definition; "
                            "a run of the current runner records it.")
    return stage(statements, seed, len(schedule(statements, seed)) // 2)


@dataclass(frozen=True, eq=False)
class LoadedSet:
    """A set as ``load`` read it; frozen, so the hold it was loaded under stays its hold."""
    control_dir: Path
    manifest: dict
    statements: dict[str, dict]
    labels: dict[str, dict]
    lock: control.RunLock | None = None  # the run lock it was loaded under; a run needs it still held

    @property
    def snapshot(self) -> Path:
        return Path(self.manifest["snapshot"])

    @property
    def set_id(self) -> str:
        return self.manifest["set"]


def _checkouts(manifest: dict) -> list[Path]:
    """The directories a worker of this set receives, which the control directory must lie apart from."""
    return [Path(manifest["snapshot"]), Path(manifest["repository"])]


def preflight(control_dir: Path, *, installation_roots: list[Path]) -> None:
    """Refuse a directory that holds no set, or that a worker could reach, before the run lock's
    file is created in it. It writes nothing and decides nothing: ``load`` checks both again under
    the lock."""
    path = control_dir / control.MANIFEST
    if not path.is_file():
        raise control.ControlError(f"{control_dir} holds no set; build one first.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    control.ensure_separate(control_dir, installation_roots=installation_roots, checkouts=_checkouts(manifest))


def load(control_dir: Path, *, installation_roots: list[Path], lock: control.RunLock | None = None) -> LoadedSet:
    """The set in ``control_dir``. A set a run will use is loaded with ``lock``, the hold
    ``control.exclusive`` yields for this directory, and ``Runner.run`` refuses one loaded without
    it; commands that only read the set load it without the lock."""
    if lock is not None:
        control.ensure_held(lock, control_dir)
    manifest = json.loads((control_dir / control.MANIFEST).read_text(encoding="utf-8"))
    if control.set_version(control_dir) != manifest["version"]:
        raise control.ControlError("The statement file no longer matches the set version; the set was changed.")
    control.ensure_separate(control_dir, installation_roots=installation_roots, checkouts=_checkouts(manifest))
    if control.file_digest(control_dir / control.LABELS) != manifest.get("labels_sha256"):
        raise control.ControlError("The label file no longer matches the one the set was built with.")
    statements = {row["item"]: row for row in control.read_jsonl(control_dir / control.STATEMENTS)}
    labels = {row["item"]: row for row in control.read_jsonl(control_dir / control.LABELS)}
    if set(statements) != set(labels):
        raise control.ControlError("Statements and labels name different items.")
    loaded = LoadedSet(control_dir, manifest, statements, labels, lock)
    if lock is not None:
        lock.loaded.add(loaded)  # the hold now vouches for this set, and for no copy of it
    return loaded


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
                "truth": label["truth"], "arm": slot.arm, "slot": slot.slot, "seed": self.seed,
                "status": "unfinished", "task": None,
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
        """Run what the stage leaves.

        The caller holds the set's run lock from before it loaded the set until this returns, as the
        ``run`` command does: ``with control.exclusive(directory, action) as lock:`` and then
        ``load(directory, ..., lock=lock)``. A set loaded without the lock, or under one released
        since, is refused: a discard or a rebuild could have replaced it on disk after the load, and
        a second run could schedule the same slots or record another seed. The set must be the very
        object ``load`` read under that hold: a copy carrying a later hold of the same directory is
        refused, since the set on disk may have changed between the two holds. A second Runner under
        the same hold is refused while this one is going.
        """
        lock = self.loaded.lock
        control.ensure_held(lock, self.loaded.control_dir)
        if self.loaded not in lock.loaded:
            raise control.ControlError("This set was not loaded under the run lock it carries: load it again "
                                       "under that lock.")
        if not lock.running.acquire(blocking=False):
            raise control.RunInProgress("Another Runner of this set is going under the same run lock; a second "
                                        "run is refused until it finishes.")
        try:
            self.spent = recorded_spend(self.loaded)  # another Runner of this set may have run since __init__
            return self._run()
        finally:
            lock.running.release()

    def _run(self) -> dict:
        # A slot is done once an attempt of this set ran; a placeholder for one that never ran is
        # scheduled again, and stays in the file, so the earlier stop remains on record. Rows of
        # another set say nothing about this one's slots, as the seed check and the spend take them.
        own = [row for row in self.recorder.recorded() if row.get("set") == self.loaded.set_id]
        statements = list(self.loaded.statements.values())
        check_seed(own, statements, self.seed)
        done = {(row["item"], row["arm"], row["slot"]) for row in own if not control.is_placeholder(row)}
        selected = stage(statements, self.seed, self.stage_runs)
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
        return {"seed": self.seed, "scheduled": len(selected), "run": len(pending), "probes": probes,
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
