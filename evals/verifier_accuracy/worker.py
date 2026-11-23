"""One verification through the normal exchange, with the worker blinded by the operating system.

Preparation and validation call the same functions as the ``get_subagent_context`` and
``validate_subagent_result`` tools (`prepare_subagent`, `subagent_exchange.store`,
`load_contract`, `read_result`, `assess_result`), so the package and the checks are the
production ones; the opt-in subagent ledger is not written.

The worker is a fresh headless Claude Code session (``claude -p``) started under macOS
``sandbox-exec`` with a profile that denies every read and write of the control directory,
denies writes to the snapshot and to the home directory except Claude Code's own state, and
passes no ``ANTHROPIC_*``/``CLAUDE*`` variables. A package's scope is an instruction, not a
sandbox; the profile applies to the file tools and the shell alike, and to every process the
worker starts. An Agent-tool worker cannot be blinded this way: it runs inside the parent's
own process, with the parent's permissions, and the parent's runner must read the labels.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

from evals.verifier_accuracy import control
from src.engine import subagent_exchange
from src.engine.subagent import prepare_subagent
from src.engine.subagent_text import parse_result
from src.engine.subagent_validation import ContractError, assess_result
from src.schemas.subagent import RESULT_MODELS, PreparedPackage, TaskContract

VERIFIER_AGENT = "code_reviewer"
CLIENT = "claude"
COMPLEXITY = "standard"
RESULT_TYPE = "research"
WORKER_TOOLS = ("Read", "Grep", "Glob", "Bash")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_VERDICT = re.compile(r"^\s*\**\s*VERDICT\s*:\s*\**\s*(CONFIRMED|REFUTED|UNCONFIRMED)\b", re.IGNORECASE)
# Writable for the worker: Claude Code's own state and caches; the system temporary
# directories lie outside the home directory and stay writable.
WRITABLE_HOME = (".claude", "Library/Caches")


class WorkerError(RuntimeError):
    """The exchange or the launch failed in a way that stops the run."""


def request(*, statement: str, arm: str, snapshot: Path, task_id: str, attempt_id: str = "1") -> dict:
    """The arguments of a verification package: one statement as a neutral question."""
    criteria = [
        "Classify the statement as CONFIRMED when the files in the snapshot directory show it is true, "
        "REFUTED when they show it is false, or UNCONFIRMED when they do not settle it.",
        "Give exactly one conclusion and begin its text with VERDICT: CONFIRMED, VERDICT: REFUTED or "
        "VERDICT: UNCONFIRMED, followed by one sentence of reasoning.",
        "Cite the file and line, or the command and its output, that decide the verdict as evidence.",
    ]
    if arm == "V2":
        criteria.append("Decide it by running at least one shell command against the snapshot directory, "
                        "and record that command and its output as evidence.")
    return {
        "agent_name": VERIFIER_AGENT,
        "task": f"Is this statement about the repository snapshot in {snapshot} true or false?\n\n{statement}",
        "acceptance_criteria": criteria,
        "task_id": task_id, "attempt_id": attempt_id,
        "context": ["The snapshot directory is a read-only export of one commit of this repository, without "
                    "git history. Other copies of the repository on this machine may be at other commits "
                    "and do not decide the statement."],
        "scope": [f"Read files under {snapshot}.", f"Run read-only shell commands in {snapshot}."],
        "non_goals": ["Modifying, creating or deleting any file.",
                      "Using git history, other checkouts or the network to decide the statement."],
        "result_type": RESULT_TYPE, "complexity": COMPLEXITY, "client": CLIENT,
        "max_skills": 3, "max_implants": 0, "max_context_chars": 16000,
    }


REFERENCE_STATEMENT = "The repository contains a file at the path `README.md`."


def bundle_guard(snapshot: Path):
    """A check that a statement's V3 and V2 packages keep the reference bundle and the standard tier.

    Skills are selected by word overlap with the task, so a statement can change the bundle,
    and with it the configuration its verdicts belong to; the builder skips such statements.
    """
    def revision(statement: str, arm: str) -> str | None:
        package = prepare_subagent(request(statement=statement, arm=arm, snapshot=snapshot,
                                           task_id="vacc-bundle-check"))
        if not isinstance(package, PreparedPackage):
            return None
        execution = package.execution
        if execution is None or execution.tier != "standard" or execution.floor_reasons:
            return None
        return package.descriptor.bundle_revision

    reference = {arm: revision(REFERENCE_STATEMENT, arm) for arm in ("V3", "V2")}
    if None in reference.values():
        raise WorkerError("The reference verification package could not be prepared at the standard tier.")
    return lambda statement: all(revision(statement, arm) == reference[arm] for arm in reference)


@dataclass(frozen=True)
class Prepared:
    package: PreparedPackage
    launch: str
    revision: str
    context_path: Path


def prepare(arguments: dict) -> Prepared:
    package = prepare_subagent(arguments)
    if not isinstance(package, PreparedPackage):
        raise WorkerError(f"Preparation failed: {[error.code for error in package.errors]}")
    execution = package.execution
    if execution is None or execution.tier != "standard" or execution.floor_reasons or not execution.model:
        raise WorkerError(f"The package left the standard tier: {execution}")
    receipt = subagent_exchange.store(package)
    return Prepared(package=package, launch=receipt.launch, revision=receipt.contract_revision,
                    context_path=Path(receipt.context_path))


def _quoted(path: Path) -> str:
    real = os.path.realpath(path)
    if '"' in real or "\\" in real:
        raise WorkerError(f"A sandbox path cannot hold quotes or backslashes: {real}")
    return f'"{real}"'


def _prefix_regex(path: Path) -> str:
    """A Seatbelt regex literal matching paths that start with ``path``, with no escapes."""
    real = os.path.realpath(path)
    if '"' in real or "\\" in real or "]" in real or "^" in real:
        raise WorkerError(f"A sandbox path cannot hold quotes, backslashes, ] or ^: {real}")
    body = "".join(character if character.isalnum() or character in "/_- " else f"[{character}]"
                   for character in real)
    return f'#"^{body}"'


def sandbox_profile(*, home: Path, control_dir: Path, snapshot: Path, session_id: str | None = None,
                    exchange: Path | None = None, package: Path | None = None) -> str:
    """The worker's sandbox; in Seatbelt the later of two matching rules wins.

    Writes under home are denied except Claude Code's own state, and the snapshot is
    unwritable. Other runs stay invisible, so the two verdicts of an item stay independent and
    a run sees one item: the host's session records (other transcripts, auto-memory) and the
    prompt history are unreadable and unwritable except the run's own session, and the
    exchange directory is unreadable except the run's own package. Last, the control
    directory is denied outright.
    """
    claude = home / ".claude"
    projects = claude / "projects"
    writable = " ".join(f"(subpath {_quoted(home / name)})" for name in WRITABLE_HOME)
    rules = [
        "(version 1)",
        "(allow default)",
        f"(deny file-write* (subpath {_quoted(home)}))",
        f"(allow file-write* {writable} (regex {_prefix_regex(home / '.claude.json')}))",
        f"(deny file-write* (subpath {_quoted(snapshot)}))",
        f"(deny file-read-data file-write* (subpath {_quoted(projects)}) (regex {_prefix_regex(claude / 'history.jsonl')}))",
    ]
    if session_id is not None:
        if not re.fullmatch(r"[0-9a-f-]{36}", session_id):
            raise WorkerError(f"Unexpected session id {session_id!r}")
        own = _prefix_regex(projects)[:-1] + f'/[^/]+/{session_id}"'
        directory = _prefix_regex(projects)[:-1] + '/[^/]+$"'
        rules.append(f"(allow file-read-data file-write* (regex {own}))")
        rules.append(f"(allow file-write* (regex {directory}))")  # creating the project directory, not listing it
    if exchange is not None:
        rules.append(f"(deny file-read-data (subpath {_quoted(exchange)}))")
        if package is not None:
            rules.append(f"(allow file-read-data (literal {_quoted(package)}))")
    rules += [f"(deny file-read* file-write* (subpath {_quoted(control_dir)}))", ""]
    return "\n".join(rules)


def environment(home: Path, temporary: Path) -> dict[str, str]:
    """A minimal environment: nothing of the parent session, its credentials or its client."""
    return {"HOME": str(home), "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", ""),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8", "SHELL": "/bin/zsh", "TERM": "dumb", "TMPDIR": str(temporary)}


@dataclass(frozen=True)
class Run:
    """What the host reported for one worker process."""

    session_id: str
    exit_code: int | None
    timed_out: bool
    cost_usd: float | None
    duration_ms: int | None
    num_turns: int | None
    is_error: bool | None
    output: str  # the final text the CLI printed, for the canary search only


class HeadlessLauncher:
    """Starts ``claude -p`` under a sandbox profile written for that one run, and waits for it."""

    def __init__(self, *, control_dir: Path, snapshot: Path, claude: str | None = None, home: Path | None = None,
                 max_budget_usd: float = 2.0, exchange: Path | None = None):
        self.control_dir = control_dir
        self.snapshot = snapshot
        self.claude = claude or shutil.which("claude") or "/opt/homebrew/bin/claude"
        self.home = home or Path.home()
        self.max_budget_usd = max_budget_usd
        self.exchange = exchange or subagent_exchange.exchange_dir()

    def __call__(self, prompt: str, *, model: str, effort: str | None, cwd: Path, session_id: str,
                 timeout: float, package: Path | None = None) -> Run:
        profiles = self.control_dir / "profiles"
        profiles.mkdir(mode=0o700, exist_ok=True)
        profile = profiles / f"{session_id}.sb"  # inside the control directory: the worker cannot read it
        control.write_private(profile, sandbox_profile(home=self.home, control_dir=self.control_dir,
                                                       snapshot=self.snapshot, session_id=session_id,
                                                       exchange=self.exchange, package=package))
        try:
            return self._launch(prompt, profile=profile, model=model, effort=effort, cwd=cwd,
                                session_id=session_id, timeout=timeout)
        finally:
            profile.unlink(missing_ok=True)

    def _launch(self, prompt: str, *, profile: Path, model: str, effort: str | None, cwd: Path, session_id: str,
                timeout: float) -> Run:
        argv = [SANDBOX_EXEC, "-f", str(profile), self.claude, "-p", prompt, "--model", model,
                "--output-format", "json", "--strict-mcp-config", "--setting-sources", "project",
                "--tools", ",".join(WORKER_TOOLS), "--permission-mode", "bypassPermissions",
                "--session-id", session_id, "--max-budget-usd", str(self.max_budget_usd)]
        if effort:
            argv += ["--effort", effort]
        with tempfile.TemporaryDirectory(prefix="vacc-") as temporary:
            started = time.monotonic()
            process = subprocess.Popen(argv, cwd=cwd, env=environment(self.home, Path(temporary)),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            try:
                stdout, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _stop(process)
                return Run(session_id, None, True, None, int((time.monotonic() - started) * 1000), None, None, "")
        try:
            report = json.loads(stdout.decode("utf-8", errors="replace"))
        except ValueError:
            report = {}
        return Run(session_id=session_id, exit_code=process.returncode, timed_out=False,
                   cost_usd=report.get("total_cost_usd"), duration_ms=report.get("duration_ms"),
                   num_turns=report.get("num_turns"), is_error=report.get("is_error"),
                   output=str(report.get("result") or ""))


def _stop(process: subprocess.Popen) -> None:
    for sent in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sent)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


@dataclass(frozen=True)
class Validated:
    status: str  # verdict, abstained or invalid
    verdict: str | None  # confirmed or refuted on a verdict
    reason: str | None


def verdict_of(conclusions) -> str | None:
    found = {match.group(1).upper() for conclusion in conclusions
             if (match := _VERDICT.match(conclusion.claim))}
    return next(iter(found)) if len(found) == 1 else None


def validate(revision: str, transcript: Path) -> Validated:
    """``validate_subagent_result`` on the transcript, then the verdict of a valid result."""
    try:
        contract = subagent_exchange.load_contract(revision)
        text = subagent_exchange.read_result(str(transcript))
    except ContractError as error:
        return Validated("invalid", None, error.code)
    response, _ = assess_result(contract, text)
    if not response.valid:
        return Validated("invalid", None, response.errors[0].code if response.errors else "invalid")
    expected = TaskContract.model_validate(contract)
    value, _, _ = parse_result(text, expected)
    report = RESULT_MODELS[RESULT_TYPE].model_validate(value)
    marked = verdict_of(report.result.conclusions)
    if marked is None:
        return Validated("invalid", None, "no_single_verdict")
    if report.status != "completed" or marked == "UNCONFIRMED":
        return Validated("abstained", None, report.status if report.status != "completed" else "unconfirmed")
    return Validated("verdict", "confirmed" if marked == "CONFIRMED" else "refuted", None)
