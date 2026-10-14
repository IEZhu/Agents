"""Starting one worker attempt: the Claude worker, or Codex through #344 with the Claude fallback (#345).

Design: docs/autodev-plan.md 5.6 and 7.6; reference: docs/autodev-workers.md. `Launcher.run`
starts one CLI for one prepared package and returns what it produced, unjudged: the caller
(`src.schedules.workers`) validates it against the retained contract. A structural success of a
CLI is never acceptance.

* **Claude** (`sandbox.build_worker`, checked by `sandbox.check_worker_spec` and
  `sandbox.require_login` before every start): ``worker_context`` goes to standard input byte for
  byte; standard output is one JSON result. Only exit code 0 with ``subtype: success``, no
  ``is_error`` and an object ``structured_output`` yields a result; the CLI validated it against
  the unchanged ``--json-schema`` (research of 2026-10-14: Ajv draft-07, every keyword of the
  subagent/1 schema enforced). Everything else is a failure with the subtype and exit code.
* **Codex** (`codex_probes.decide`, then `codex_probes.start`, the only way a Codex worker starts):
  the schema goes to a file as `codex_schema` derives it (OpenAI's strict subset: ``minLength``,
  ``maxLength`` and ``title`` dropped, ``const`` as a one-value ``enum``), checked by
  `relaxation_problems` to only relax the original; ``-o`` names a fresh path in a new directory,
  because a failed turn leaves an existing file untouched. The result is the file's raw text,
  read only after exit code 0. When ``decide`` falls back, the Claude worker runs and the result
  says ``client_requested``, ``client_applied`` and ``fallback_reason``; `codex_probes.record_decision`
  adds the decision to the run record.

A process runs in its own session and process group, is killed with its group at the timeout or
when the launcher stops, and its standard streams go to private files that are removed once read.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import contextlib
import copy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any

from src.daemon.state import atomic_private, private_dir

from . import codex_probes, codex_proxy, sandbox
from . import codex_sandbox as cs
from . import decoys as decoy_store
from .scan import HitKind, KnownSecret, Scanner

CLAUDE, CODEX = codex_probes.CLAUDE, codex_probes.CODEX
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")  # claude --help of 2.1.296
DEFAULT_TIMEOUT = 30 * 60  # seconds of one attempt unless the delegate's budget sets timeout_minutes
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024  # the Codex -o file; validate_result takes 128000 characters
POLL = 0.2  # seconds between checks of a running process for a stop
# OpenAI's strict structured outputs reject minLength and maxLength (research); title is an
# annotation the strict subset does not document. const becomes the equivalent one-value enum.
DROPPED_KEYWORDS = frozenset({"minLength", "maxLength", "title"})
SUBSCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")
SUBSCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
SUBSCHEMA_SINGLE = ("items", "additionalProperties", "not", "contains", "propertyNames")


class LaunchRefused(RuntimeError):
    """A worker cannot start safely or at all (an infrastructure problem); never a secret."""


class OutputTooLarge(Exception):
    """A worker's output file holds more than the launcher reads: the attempt failed."""


# --- the Codex schema -------------------------------------------------------------------------------

def codex_schema(schema: Mapping) -> dict:
    """The schema for ``codex exec --output-schema``: ``schema`` with the keywords of
    `DROPPED_KEYWORDS` removed and ``const`` rewritten as a one-value ``enum``, in schema
    positions only (a property named ``title`` stays)."""
    def derive(node: Any) -> Any:
        if not isinstance(node, dict):
            return copy.deepcopy(node)
        out: dict = {}
        for key, value in node.items():
            if key in DROPPED_KEYWORDS:
                continue
            if key == "const":
                if "enum" in node:
                    raise ValueError("a schema node with both const and enum")
                out["enum"] = [copy.deepcopy(value)]
            elif key in SUBSCHEMA_MAPS and isinstance(value, dict):
                out[key] = {name: derive(sub) for name, sub in value.items()}
            elif key in SUBSCHEMA_LISTS and isinstance(value, list):
                out[key] = [derive(sub) for sub in value]
            elif key in SUBSCHEMA_SINGLE and isinstance(value, dict):
                out[key] = derive(value)
            else:
                out[key] = copy.deepcopy(value)
        return out
    return derive(dict(schema))


def relaxation_problems(original: Any, derived: Any, where: str = "$") -> list[str]:
    """Why ``derived`` is not ``original`` with only `DROPPED_KEYWORDS` removed and ``const``
    rewritten as ``enum``; empty when every value the original accepts the derived one accepts
    too. Every other difference counts, a dropped ``required`` or property included (a property
    left out of ``properties`` would narrow ``additionalProperties: false``)."""
    if not isinstance(original, dict) or not isinstance(derived, dict):
        return [] if original == derived else [f"{where}: differs"]
    problems = []
    for key in original:
        if key in derived or key in DROPPED_KEYWORDS:
            continue
        if key == "const" and derived.get("enum") == [original["const"]]:
            continue
        problems.append(f"{where}: drops {key}")
    for key, value in derived.items():
        here = f"{where}.{key}"
        if key == "enum" and "enum" not in original and "const" in original:
            if value != [original["const"]]:
                problems.append(f"{here}: is not the original const")
            continue
        if key not in original:
            problems.append(f"{here}: is not in the original")
            continue
        before = original[key]
        if key in SUBSCHEMA_MAPS and isinstance(value, dict) and isinstance(before, dict):
            if set(value) != set(before):
                problems.append(f"{here}: names other entries")
            problems += [problem for name in set(value) & set(before)
                         for problem in relaxation_problems(before[name], value[name], f"{here}.{name}")]
        elif key in SUBSCHEMA_LISTS and isinstance(value, list) and isinstance(before, list):
            if len(value) != len(before):
                problems.append(f"{here}: has another number of branches")
            problems += [problem for index, (one, two) in enumerate(zip(before, value))
                         for problem in relaxation_problems(one, two, f"{here}[{index}]")]
        elif key in SUBSCHEMA_SINGLE and isinstance(value, dict) and isinstance(before, dict):
            problems += relaxation_problems(before, value, here)
        elif value != before:
            problems.append(f"{here}: changes the original value")
    return problems


def schema_text(schema: Mapping) -> str:
    """Compact JSON of a schema, as the inline ``--json-schema`` value and the Codex file."""
    return json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# --- processes ----------------------------------------------------------------------------------------

def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _feed(process: subprocess.Popen, data: bytes) -> None:
    def write() -> None:
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            process.stdin.write(data)  # type: ignore[union-attr]
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            process.stdin.close()  # type: ignore[union-attr]
    threading.Thread(target=write, name="autodev-worker-stdin", daemon=True).start()


def wait(process: subprocess.Popen, timeout: float, stop: threading.Event) -> tuple[int | None, str | None]:
    """Wait for ``process`` (its own process group) until it exits, ``timeout`` passes or ``stop``
    is set; the group is killed in every case. Returns the exit code and ``timeout``/``stopped``."""
    deadline = time.monotonic() + timeout
    ended = None
    try:
        while True:
            try:
                process.wait(timeout=POLL)
                break
            except subprocess.TimeoutExpired:
                if stop.is_set():
                    ended = "stopped"
                elif time.monotonic() >= deadline:
                    ended = "timeout"
                if ended:
                    _kill_group(process.pid)
                    process.wait()
                    break
    finally:
        _kill_group(process.pid)  # descendants that outlived the CLI
    return (None if ended else process.returncode), ended


def read_bounded(path: Path, limit: int) -> bytes | None:
    """A regular file's bytes, never through a link; None when it is missing or not a regular
    file; `OutputTooLarge` when it holds more than ``limit`` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (FileNotFoundError, OSError):
        return None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise OutputTooLarge(f"the worker's output holds more than {limit} bytes")
    return data


def parse_claude(stdout: str) -> tuple[dict | None, dict, list[dict]]:
    """The ``structured_output`` of a ``--output-format json`` result (None without a usable
    result), what the record keeps (``subtype``, ``is_error``, the number of permission denials)
    and the denials themselves for the tripwire."""
    data = None
    for candidate in (stdout, *reversed(stdout.strip().splitlines())):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            break
        data = None
    if data is None:
        return None, {"subtype": None, "problem": "no JSON result object"}, []
    found = data.get("permission_denials")
    denials = [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []
    info = {"subtype": data.get("subtype") if isinstance(data.get("subtype"), str) else None,
            "is_error": data.get("is_error") is True, "permission_denials": len(denials)}
    output = data.get("structured_output")
    if info["subtype"] != "success" or info["is_error"] or data.get("type") != "result":
        info["problem"] = "the result is not a success"
        return None, info, denials
    if not isinstance(output, dict):
        info["problem"] = "the success result carries no structured_output object"
        return None, info, denials
    return output, info, denials


# --- one attempt -------------------------------------------------------------------------------------

@dataclass
class LaunchResult:
    """What one worker process produced. ``raw`` is the Claude ``structured_output`` object or the
    Codex ``-o`` text, None when there is none (``failure`` says why)."""

    client_requested: str
    client_applied: str
    fallback_reason: str | None = None
    detail: str = ""
    raw: Any = None
    failure: str | None = None
    cli: dict = field(default_factory=dict)
    launch: dict = field(default_factory=dict)
    model: str | None = None
    effort: str | None = None
    stdout: str = field(default="", repr=False)  # for the tripwire scan only, never recorded
    denials: list = field(default_factory=list, repr=False)
    known: list[KnownSecret] = field(default_factory=list, repr=False)

    def client(self) -> dict:
        return {"client_requested": self.client_requested, "client_applied": self.client_applied,
                "fallback_reason": self.fallback_reason, "detail": self.detail[:500]}


@dataclass
class Launcher:
    """Starts worker attempts for one run. Every collaborator a test replaces is a field."""

    claude: str | None
    path: str
    home: Path
    state_dir: Path
    run_id: str
    codex: str | None = None
    installation: Path | None = None
    login_mode: str = sandbox.LOGIN_CONFIG_DIR
    oauth_token: Callable[[], str | None] | None = None
    codex_login: str | None = None
    decoys: Sequence[decoy_store.Decoy] = ()
    known: Sequence[KnownSecret] = ()
    temp_base: Path = sandbox.TMP_BASE
    platform: str = sys.platform
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    codex_decide: Callable[..., codex_probes.Decision] = codex_probes.decide
    codex_start: Callable[..., subprocess.Popen] = codex_probes.start
    codex_execute: Callable[..., Any] = codex_probes.execute
    codex_resolver: Callable[[str], str | None] = codex_probes.resolve_ipv4
    worker_paths: Mapping[str, Path] = field(default_factory=dict)  # owner_temp, claude_temp, claude_shared
    stop: threading.Event = field(default_factory=threading.Event)

    def run(self, *, client: str, step: str, context: str, schema: Mapping, tree: Path, materials: Path,
            attempt_dir: Path, model: str | None, effort: str | None, fallback: Mapping[str, str],
            timeout: float = DEFAULT_TIMEOUT) -> LaunchResult:
        """One attempt. ``client`` is the delegate's (``claude`` or ``codex``); ``fallback`` is the
        Claude model and effort for a Codex step that falls back."""
        private_dir(attempt_dir)
        if client == CODEX:
            result = self._codex(step, context, schema, tree, materials, attempt_dir, model, effort, timeout)
            if result.client_applied == CODEX:
                return result
            fallen = self._claude(context, schema, tree, materials, attempt_dir, fallback.get("model"),
                                  fallback.get("effort"), timeout)
            fallen.client_requested, fallen.fallback_reason = CODEX, result.fallback_reason
            fallen.detail = result.detail
            fallen.known = result.known + fallen.known
            return fallen
        return self._claude(context, schema, tree, materials, attempt_dir, model, effort, timeout)

    # --- Claude ---------------------------------------------------------------------------------

    def _claude(self, context: str, schema: Mapping, tree: Path, materials: Path, attempt_dir: Path,
                model: str | None, effort: str | None, timeout: float) -> LaunchResult:
        result = LaunchResult(CLAUDE, CLAUDE, model=model, effort=effort)
        if not self.claude:
            raise LaunchRefused("the claude executable was not found (tools.json, PATH)")
        token = self.oauth_token() if self.oauth_token and self.login_mode != sandbox.LOGIN_CONFIG_DIR else None
        if token:
            result.known.append(KnownSecret("claude oauth token", token))
        temp_root = sandbox.make_temp_root(self.temp_base)
        try:
            paths = sandbox.WorkerPaths(
                home=self.home, tree=Path(tree), materials=Path(materials),
                config_dir=sandbox.config_dir(self.state_dir),
                gh_config=private_dir(attempt_dir / "gh"), settings_file=attempt_dir / "claude" / "settings.json",
                tmp_root=temp_root, state_dir=self.state_dir, **dict(self.worker_paths))
            spec = sandbox.build_worker(self.claude, paths, schema=schema_text(schema), path=self.path,
                                        mode=self.login_mode, oauth_token=token, model=model, effort=effort)
            if problems := sandbox.check_worker_spec(spec):
                raise LaunchRefused("the Claude worker fails its checks: " + "; ".join(problems[:10]))
            sandbox.require_login(self.claude, paths.config_dir, path=self.path, home=self.home, oauth_token=token,
                                  runner=self.command_runner)
            result.launch = spec.describe()
            stdout, code, ended = self._start(list(spec.argv), spec.cwd, dict(spec.env), context, attempt_dir, timeout)
        except sandbox.SandboxError as error:
            raise LaunchRefused(str(error)) from None
        finally:
            sandbox.remove_temp_root(temp_root, self.temp_base)
        if stdout is None:
            result.cli, result.failure = {"exit_code": code, "ended": ended}, f"the claude worker's output exceeds " \
                                                                               f"{MAX_STDOUT_BYTES} bytes"
            return result
        result.stdout = stdout
        output, info, result.denials = parse_claude(stdout)
        result.cli = {"exit_code": code, "ended": ended, **info}
        if ended or code != 0 or output is None:
            result.failure = f"the claude worker ended without a result ({ended or f'exit code {code}'}, " \
                             f"{info.get('subtype') or 'no subtype'})"
            return result
        result.raw = output
        return result

    def _start(self, argv: list[str], cwd: Path, env: dict, context: str, attempt_dir: Path,
               timeout: float) -> tuple[str | None, int | None, str | None]:
        """Run ``argv`` with ``context`` on standard input; its standard output (None when it is too
        large), exit code and how it ended."""
        out, err = attempt_dir / "stdout", attempt_dir / "stderr"
        try:
            with open(out, "xb") as stdout, open(err, "xb") as stderr:
                os.chmod(out, 0o600)
                os.chmod(err, 0o600)
                process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                           close_fds=True, start_new_session=True)
                _feed(process, context.encode("utf-8"))
                code, ended = wait(process, timeout, self.stop)
            try:
                data = read_bounded(out, MAX_STDOUT_BYTES) or b""
            except OutputTooLarge:
                return None, code, ended
        finally:
            for path in (out, err):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)
        return data.decode("utf-8", "replace"), code, ended

    # --- Codex ----------------------------------------------------------------------------------

    def _codex(self, step: str, context: str, schema: Mapping, tree: Path, materials: Path, attempt_dir: Path,
               model: str | None, effort: str | None, timeout: float) -> LaunchResult:
        derived = codex_schema(schema)
        if problems := relaxation_problems(schema, derived):
            raise LaunchRefused("the derived Codex schema does not only relax the original: " + "; ".join(problems[:5]))
        schema_file = attempt_dir / "codex-schema.json"
        atomic_private(schema_file, schema_text(derived))
        os.chmod(schema_file, 0o600)
        out_dir = attempt_dir / "codex-out"
        if os.path.lexists(out_dir):
            raise LaunchRefused("the Codex output directory of this attempt already exists")
        os.mkdir(out_dir, 0o700)
        temp_root = sandbox.make_temp_root(self.temp_base)
        try:
            directories = cs.prepare_run_directories(temp_root)
            paths = cs.CodexPaths(kind=cs.WORKER, home=self.home, tmp_root=temp_root, out_dir=out_dir,
                                  profile=attempt_dir / "codex.sb", tree=Path(tree), materials=Path(materials),
                                  schema=schema_file, state_dir=self.state_dir, installation=self.installation,
                                  **directories)
            targets = codex_probes.CodexTargets(state_dir=self.state_dir,
                                                secrets_dir=self.home / ".config" / "agents-autodev")
            with codex_proxy.ConnectProxy(cs.allowed_hosts()) as proxy:
                decision = self.codex_decide(
                    self.codex, paths, path=self.path, proxy=proxy, mode=self.codex_login, targets=targets,
                    persistent=list(self.decoys), directory=self.state_dir, run_id=self.run_id, model=model,
                    effort=effort, known=list(self.known), platform=self.platform, execute_=self.codex_execute,
                    command_runner=self.command_runner, resolver=self.codex_resolver)
                codex_probes.record_decision(self.run_id, self.state_dir, step, decision, self.decoys,
                                             known=[secret.value for secret in self.known])
                result = LaunchResult(CODEX, decision.client_applied, decision.reason, decision.detail,
                                      model=model, effort=effort, known=list(decision.known))
                if not decision.codex:
                    return result
                result.launch = cs.describe(decision.launch)  # type: ignore[arg-type]
                if os.path.lexists(paths.output):
                    raise LaunchRefused("the Codex output file exists before the start")
                code, ended = self._start_codex(decision, proxy, context, attempt_dir, timeout)
        except (cs.CodexSandboxError, sandbox.SandboxError) as error:
            raise LaunchRefused(f"codex: {error}") from None
        finally:
            sandbox.remove_temp_root(temp_root, self.temp_base)
        try:
            data = read_bounded(paths.output, MAX_OUTPUT_BYTES)
        except OutputTooLarge as error:
            result.cli, result.failure = {"exit_code": code, "ended": ended, "output_file": True}, str(error)
            return result
        result.cli = {"exit_code": code, "ended": ended, "output_file": data is not None}
        if ended or code != 0 or not data:
            result.failure = f"codex ended without a result ({ended or f'exit code {code}'}, " \
                             f"{'an empty' if data == b'' else 'no' if data is None else 'an'} output file)"
            return result
        result.raw = data.decode("utf-8", "replace")
        return result

    def _start_codex(self, decision: codex_probes.Decision, proxy: codex_proxy.ConnectProxy, context: str,
                     attempt_dir: Path, timeout: float) -> tuple[int | None, str | None]:
        out, err = attempt_dir / "codex.stdout", attempt_dir / "codex.stderr"
        try:
            with open(out, "xb") as stdout, open(err, "xb") as stderr:
                process = self.codex_start(decision, context, proxy=proxy, stdout=stdout, stderr=stderr,
                                           platform=self.platform)
                return wait(process, timeout, self.stop)
        finally:
            for path in (out, err):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)


def decoy_findings(text: str, denials: Iterable[Mapping], decoys: Sequence[decoy_store.Decoy],
                   known: Sequence[KnownSecret] = ()) -> list[str]:
    """What in a worker's output names a decoy or carries a decoy marker or another known secret:
    labels only, never a value."""
    findings = []
    references = decoy_store.references(decoys)
    for index, denial in enumerate(denials, 1):
        named = json.dumps(denial, ensure_ascii=False)
        for label, names in references.items():
            if any(name in named for name in names):
                findings.append(f"denied tool call {index} names decoy {label}")
    hits = Scanner([*decoy_store.known(decoys), *known]).scan(text) if text else ()
    findings += sorted({f"the output carries {hit.name}" for hit in hits if hit.kind == HitKind.KNOWN})
    return findings
