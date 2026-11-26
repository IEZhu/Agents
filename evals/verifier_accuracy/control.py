"""The control directory and its mandatory attempt recorder.

docs/verifier-accuracy-measurement.md, "Storage" and "Recording path": the set lives in
``claims-control`` under the service-state directory (`src.daemon.state.state_dir`),
mode 0700 with 0600 files, the statements, the labels and the attempt records in
separate files. It must never lie inside the installation root or inside a checkout a
worker receives, because a worker could then read the answers.

The recorder keeps the subagent ledger's privacy shape (no statement text, opaque item
IDs, hashed task IDs) but not its opt-in: it always writes, never falls back to the
ledger's path, and raises on a failed write so the run stops instead of silently
completing a biased subset.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Iterable, Iterator

from src.daemon import state
from src.file_lock import file_lock

DIRECTORY_NAME = "claims-control"
SNAPSHOTS_NAME = "claims-snapshots"
STATEMENTS = "statements.jsonl"
LABELS = "labels.jsonl"
ATTEMPTS = "attempts.jsonl"
PROBES = "probes.jsonl"
OWNER_REVIEW = "owner-review.jsonl"
CANARY = "canary.txt"
MANIFEST = "set.json"
SIMULATION = "simulation.json"
RUN_LOCK = "run.lock"  # held by a run, a separate probe, a discard and a build's writes
DISCARDED = "discarded"
VERSION_CHARS = 16

ARMS = ("V3", "V2")
SLOTS = (1, 2)
STATUSES = ("verdict", "abstained", "invalid", "unfinished")
VERDICTS = ("confirmed", "refuted")
OUTCOMES = ("TP", "FN", "TN", "FP")
# Levels the runner assigns: the arm's own level, V1 for a V2-arm verdict without a recorded
# execution, and "unsupported" for a V3-arm verdict without a recorded snapshot read.
LEVELS = ("V3", "V2", "V1", "unsupported")
FAMILIES = ("model", "execution")
ARM_LEVELS = {"V3": ("V3", "unsupported"), "V2": ("V2", "V1")}
LEVEL_FAMILY = {"V3": "model", "unsupported": "model", "V1": "model", "V2": "execution"}
# Unfinished rows written for a slot that never ran: a resumed run schedules the slot again.
PLACEHOLDER_REASONS = ("probe_failed", "runner_stopped", "budget")
# Every line carries these keys; values may be null where the attempt never got that far.
RECORD_KEYS = ("at", "event", "set", "item", "stratum", "truth", "arm", "slot", "seed", "status", "task",
               "attempt_id", "contract_revision", "client", "tier", "model", "reasoning_effort",
               "bundle_revision")
VERDICT_KEYS = ("level", "family", "verdict", "outcome")


class ControlError(RuntimeError):
    """The control directory is missing, unsafe or inconsistent."""


class RecorderError(RuntimeError):
    """An attempt record could not be written; the run must stop."""


class RunInProgress(ControlError):
    """Another process holds the set's run lock."""


@contextlib.contextmanager
def exclusive(control: Path, action: str) -> Iterator[None]:
    """Hold the set's run lock for ``action``, refusing at once while another process holds it.

    A run holds it from its first record read to its last write; a separate probe, a discard
    and a build's writes hold it too, so none of them can change the records or move the set
    under a run that is going.
    """
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(Path(control) / RUN_LOCK, blocking=False))
        except BlockingIOError:
            raise RunInProgress(f"A run of this set is in progress; {action} is refused until it finishes.") from None
        yield


def default_control_dir(installation=None) -> Path:
    return (state.state_dir(installation) / DIRECTORY_NAME).resolve()


def default_snapshots_dir(installation=None) -> Path:
    return (state.state_dir(installation) / SNAPSHOTS_NAME).resolve()


def _within(path: Path, root: Path) -> bool:
    path, root = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    return path == root or root in path.parents


def ensure_separate(control: Path, *, installation_roots: Iterable[Path], checkouts: Iterable[Path]) -> None:
    """Refuse a control directory a worker could reach through its own inputs."""
    for root in installation_roots:
        if _within(control, root):
            raise ControlError(f"The control directory {control} lies inside the installation root {root}; "
                               "set AGENTS_SERVICE_DIR outside it.")
    for checkout in checkouts:
        if _within(control, checkout):
            raise ControlError(f"The control directory {control} lies inside {checkout}, a checkout workers receive.")
        if _within(checkout, control):
            raise ControlError(f"The checkout {checkout} lies inside the control directory {control}, "
                               "which workers are denied.")


def open_private(control: Path) -> Path:
    """The directory, created if needed, 0700 and owned by this user."""
    return state.private_dir(control)


def write_private(path: Path, text: str) -> None:
    state.atomic_private(path, text)  # mkstemp creates the file 0600
    if os.name == "posix":
        os.chmod(path, 0o600)


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    write_private(path, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))


def read_jsonl(path: Path) -> list[dict]:
    """Every line of a control file; a malformed line is an error, never skipped."""
    rows = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError as error:
                raise ControlError(f"{path.name} line {number} is not JSON: {error}") from None
            if not isinstance(value, dict):
                raise ControlError(f"{path.name} line {number} is not an object")
            rows.append(value)
    return rows


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def set_version(control: Path) -> str:
    """The first 16 hexadecimal characters of the statement file's SHA-256."""
    return hashlib.sha256((control / STATEMENTS).read_bytes()).hexdigest()[:VERSION_CHARS]


def task_hash(task_id: str) -> str:
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def check_record(record: dict) -> None:
    """A control_attempt line as the note specifies it; a malformed one is a runner bug."""
    missing = [key for key in RECORD_KEYS if key not in record]
    if missing:
        raise ValueError(f"control_attempt lacks {missing}")
    if record["event"] != "control_attempt":
        raise ValueError("event must be control_attempt")
    if record["arm"] not in ARMS or record["slot"] not in SLOTS:
        raise ValueError(f"unknown arm or slot: {record['arm']!r} {record['slot']!r}")
    if not isinstance(record["truth"], bool):
        raise ValueError("truth must be a boolean")
    if not isinstance(record["seed"], int) or isinstance(record["seed"], bool):
        raise ValueError("seed must be the schedule's integer seed")
    if record["status"] not in STATUSES:
        raise ValueError(f"unknown status {record['status']!r}")
    if record["status"] == "verdict":
        if record.get("level") not in ARM_LEVELS[record["arm"]]:
            raise ValueError(f"a {record['arm']}-arm verdict cannot have level {record.get('level')!r}")
        if record.get("family") != LEVEL_FAMILY[record["level"]]:
            raise ValueError(f"level {record['level']} belongs to family {LEVEL_FAMILY[record['level']]}")
        if record.get("verdict") not in VERDICTS:
            raise ValueError("a verdict line needs a verdict")
        if record.get("outcome") != outcome(record["truth"], record["verdict"]):
            raise ValueError("the outcome must follow from the truth and the verdict")
    elif any(key in record for key in VERDICT_KEYS):
        raise ValueError("only verdict lines carry level, family, verdict and outcome")


def is_placeholder(row: dict) -> bool:
    """A row for a slot that never ran, which a later attempt of the same slot supersedes."""
    reason = str(row.get("reason") or "")
    return row.get("status") == "unfinished" and (reason in PLACEHOLDER_REASONS or reason.startswith("runner_error"))


def outcome(truth: bool, verdict: str) -> str:
    """The positive class is "the verifier says the claim is false"."""
    refuted = verdict == "refuted"
    if not truth:
        return "TP" if refuted else "FN"
    return "FP" if refuted else "TN"


class AttemptRecorder:
    """Appends one ``control_attempt`` line per scheduled attempt and fsyncs it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, record: dict) -> dict:
        record = {"at": now(), "event": "control_attempt", **record}
        check_record(record)
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError as error:
                raise RecorderError(f"The attempt record could not be written to {self.path}: {error}") from error
        return record

    def recorded(self) -> list[dict]:
        return read_jsonl(self.path) if self.path.exists() else []
