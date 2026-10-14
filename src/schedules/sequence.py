"""The run sequence of an autodev run (#347; design 9 and 10.1; reference: docs/autodev-sequence.md).

`run_sequence` is what ``python -m src.schedules run autodev`` executes under the run lease in
every mode (``runner.run_autodev``): ``dry-run``, ``run`` (``run-now --mode run``, the supervised
real run) and ``resume`` (``run-now --mode resume --pr N``, or a ``run`` whose gate decides
``resume``). It returns the fields the run record gets; it writes them as it goes as well
(`records.update_record`), so a run killed half way still names its issue, comments and endpoint.

**Order.** The drift check (and in it the fetch of ``main`` and the quarantine pruning,
`runner.default_drift`); in a real mode the preconditions (`preconditions.check`, a missing item
ends the run ``refused``); the gate on a read-only snapshot. ``wait`` ends the run. ``select``:
the candidates (`candidates.fetch_candidates`, GET only); none at all ends the run without a
session; otherwise the select stage. A dry-run stops there with the decision and the top-five
table; no broker exists in a dry-run, so nothing can be written. A real run then binds its broker
to the selected issue (`broker.Broker.bind_issue`), posts the selection comment and the claim
(`Broker.select`), sets aside a ``refs/heads/<branch>`` an earlier run left in the trusted
repository (`Sequence._set_aside`), opens one worker service for the implement and submit stages
(`workers.WorkerService`, ``max_verifications = 1 + max_bot_rounds``) and runs the implement
stage. The broker answers a session's write only in the submit stage (`Broker.enter_stage`), so
the implement session reads GitHub and nothing more. After it (`Sequence.after_implement`): the
final handover import, the change check on the imported head, the head checks (the output's
``head_sha`` is the imported head and the last head a validated review covered), the accepted
pre-mortem, no open security risk (design 5.8) and no pull request yet. A blocker on any of these
ends the run ``blocked``: a control-path change is exported as a patch and
a bundle (`repos.TrustedRepository.export_commits`), the branch moves to a local quarantine ref,
the issue gets ``autodev:blocked`` with the neutral blockers and loses its claim. Otherwise the
service imports from the submit stage's exchange from now on (`WorkerService.rebind_importer`)
and the submit stage runs on a repository holding the branch. After it (`Sequence.after_submit`):
the output must name the broker's pull request and pushed head (the record, the report and the
state comment take both from the broker), the state comment on the pull request (`Broker.state`,
with ``next_attempt``), the risk issues of the pre-mortem's known risks
(`Broker.create_risk_issues`), and with ``waiting_bots`` the tick's ``next_attempt``. ``resume`` checks that the pull request's head is the head the trusted
repository imported (otherwise blocked), takes ``pinned`` or the merge base as the base, opens a
fresh worker service (its push gate starts closed) and runs the submit stage with the trusted
state comment's rounds and findings; its state comment is edited, not duplicated.

**A stage session** (`Sequence.stage`): the effort check (`src.flow_execution`), the session's
layout with the run's state copy, a one-off repository (with the branch for submit), the
environment at ``pinned``, the inputs file and ``state.json``, the runner socket for a session
with Bash, `sandbox.build` with the stage schema, `probes.guard` (a probe session for Bash kinds),
`probes.record_report`, a ``.git`` snapshot, the prompt (`stages.render_prompt`), the session
through `probes.start_session` with the stage's timeout (its process group is killed afterwards),
`probes.stage_tripwire` on the whole stream file (one over `MAX_STREAM_BYTES` is refused and turns
the schedule off), the ``.git`` check (a change stops the run and turns the schedule off), and the
output check (the last result event, `stages.parse_stage_stream`, then `stages.validate_output`).
A stage configured for Codex runs as Codex only
for ``select`` while the stored availability says so; otherwise, and always for ``implement`` and
``submit``, it falls back to Claude with the reason (`stages.stage_profile`).

**The end.** ``state.json`` (stage ``done``); a run that ends other than ``completed`` after its
claim, without a pull request and without a block comment, blocks its issue with the generic
phrase and removes the claim (`Sequence.release_claim`); the session directories and temporary
roots are removed, the report goes into the record (`report.build_report`) and the owner is
notified (`report.should_notify`). SIGTERM or SIGHUP (the supervisor's timeout or stop) ends the
run ``failed: infra`` the same way, the record and the notification first. Statuses:
``completed``, ``blocked``, ``refused`` (preconditions, the sandbox, the tripwire, a changed
``.git``, a stream too long to scan) and ``failed: infra``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Any

from src import flow_execution
from src.daemon.state import private_dir, read_json

from . import broker as broker_module
from . import candidates as candidate_data
from . import codex_probes, codex_proxy, codex_sandbox, decoys, drift, heavy, integrity, preconditions, probes
from . import records, repos, run_state, runner, sandbox, scan, socket_service, stages, workers
from . import paths as path_check
from . import report as run_report
from . import worker_launch
from .gate import gate, issue_of, trusted_state
from .github import GitHubError, NetworkError, TokenUnavailable, fetch_snapshot

SELECT_MINUTES = 30
SUBMIT_MINUTES = 90
OVERHEAD_MINUTES = 30
DEFAULT_IMPLEMENT_MINUTES = 180
STAGES_DIR = "stages"
STREAM_NAME = "stream.jsonl"
STDERR_NAME = "stderr.log"
COMMIT_AUTHOR = f"{repos.SESSION_NAME} <{repos.SESSION_EMAIL}>"
TERMINATE_WAIT = 10
STOP_CLOSE_WAIT = 2.0  # seconds the workers get after a stop, inside the supervisor's grace period
STOP_TERMINATE_WAIT = 1.0  # seconds a stopped run's session gets between SIGTERM and SIGKILL
# A stage stream longer than this is refused unread and turns the schedule off (#347 review): every
# shorter one is scanned whole and parsed to its end.
MAX_STREAM_BYTES = 100_000_000

# Blockers the runner posts on the issue (`broker.Broker.block`): neutral, no details.
INVALID_OUTPUT = "the {kind} stage ended without a valid result"
BUNDLE_REFUSED = "the final handover bundle was refused"
HEAD_MISMATCH = "the final head is not the head the session handed over"
NO_VALIDATED_REVIEW = "no validated review covers the final head"
NO_PREMORTEM = "the pre-mortem did not run on this change"
OPEN_RISK = "an open pre-mortem risk needs the owner"
CONTROL_PATH = "the change needs a control path; a local patch for the owner is in the run records"
PROTECTED_PATH = "the change touches protected paths without autodev:next"
FOREIGN_HEAD = "the pull request's head is not the head this runner pushed"
EARLY_PR = "a pull request was opened before the runner's checks after the implement stage"
SUBMIT_BLOCKED = "the submit stage ended blocked"
GENERIC_BLOCKED = "the run stopped and needs the owner; the details are in the local run record"


class Stopped(Exception):
    """Ends the run with these record fields (``status``, ``reason`` and the like)."""

    def __init__(self, fields: Mapping):
        super().__init__(str(fields.get("reason") or fields.get("status")))
        self.fields = dict(fields)


def infra(reason: str) -> Stopped:
    return Stopped(runner.infra(reason))


def refused(reason: str, **details: Any) -> Stopped:
    return Stopped({"status": "refused", "reason": reason, **details})


# --- timeouts -------------------------------------------------------------------------------------

def stage_minutes(config: Mapping, kind: str) -> int:
    """The stage's timeout (#347 Q-timeouts): select 30, implement ``budget.timeout_minutes``
    (180 by default), submit 90 minutes."""
    if kind == stages.SELECT:
        return SELECT_MINUTES
    if kind == stages.IMPLEMENT:
        budget = ((config.get("stages") or {}).get(stages.IMPLEMENT) or {}).get("budget") or {}
        minutes = budget.get("timeout_minutes")
        valid = isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0
        return minutes if valid else DEFAULT_IMPLEMENT_MINUTES
    return SUBMIT_MINUTES


def run_timeout(config: Mapping, mode: str) -> int:
    """The supervisor's limit for a whole run, in seconds: its stages plus `OVERHEAD_MINUTES`
    (dry-run: select; run: select, implement and submit; resume: submit)."""
    kinds = {"dry-run": (stages.SELECT,), "resume": (stages.SUBMIT,)}.get(mode, stages.KINDS)
    return (sum(stage_minutes(config, kind) for kind in kinds) + OVERHEAD_MINUTES) * 60


# --- the drift check ------------------------------------------------------------------------------

def check_drift(ctx: runner.Context, config: Mapping) -> dict:
    """``pinned`` and the runner's commit, or those with the ``failed: infra`` fields."""
    try:
        checked = ctx.drift_check(ctx.installation) if ctx.drift_check else runner.default_drift(ctx.installation, config)
    except drift.DriftCheckFailed as error:
        return runner.infra(f"cli: {error}")
    except repos.FetchError as error:
        return runner.infra(f"network: {error}")
    except (repos.RepositoryError, ValueError, OSError) as error:
        return runner.infra(f"cli: trusted repository: {error}")
    result = {"pinned": checked.get("pinned"), "runner_commit": checked.get("runner_commit")}
    if checked.get("pruned"):
        result["quarantine_pruned"] = len(checked["pruned"])
    if checked.get("changed"):
        return {**result, **runner.infra("runner drift: " + ", ".join(checked["changed"][:20]))}
    return result


# --- one stage ------------------------------------------------------------------------------------

@dataclass
class StageOutcome:
    """A stage session that ran to its end: ``output`` is the validated structured output, or
    None with ``problems`` (an invalid or missing output)."""

    kind: str
    output: dict | None
    problems: list[str] = field(default_factory=list)
    record: dict = field(default_factory=dict)
    layout: dict | None = None


def _sha256_json(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _kill_group(process: subprocess.Popen, wait: float = TERMINATE_WAIT) -> None:
    """End the session's process group (it is a session leader, `probes.start_session`): SIGTERM,
    then SIGKILL after ``wait`` seconds."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TERMINATE_WAIT)


class Sequence:
    """One run of the sequence; see the module docstring."""

    def __init__(self, ctx: runner.Context, config: Mapping, run_id: str, mode: str, *, pr: int | None,
                 now: datetime, trigger: Mapping | None = None):
        self.ctx, self.config, self.run_id, self.mode, self.pr, self.now = ctx, config, run_id, mode, pr, now
        self.trigger = (trigger or {}).get("kind") or "manual"
        self.repository = str(config["repository"])
        self.run_dir = records.run_dir(run_id, ctx.directory)
        self.fields: dict = {}
        self.markers: list[str] = []
        self.known: list[scan.KnownSecret] = []
        self.planted: list = []
        self.prepared = False
        self.claude: str | None = None
        self.login = sandbox.LOGIN_CONFIG_DIR
        self.oauth: str | None = None
        self.path = ""
        self.pinned: str | None = None
        self.resume_head: str | None = None  # the PR head a resume checked at its start
        self.trusted: repos.TrustedRepository | None = None
        self.client = None
        self.credentials: broker_module.Credentials | None = None
        self.snapshot: dict = {}
        self.broker: broker_module.Broker | None = None
        self.service: workers.WorkerService | None = None
        self.registry: socket_service.Registry | None = None
        self.state: run_state.RunState | None = None
        self.capabilities: dict | None = None
        self.temp_roots: list[Path] = []
        self.holder: dict = {"pid": None}
        self.venv: Path | None = None

    # --- records ----------------------------------------------------------------------------------

    def _write(self, change: Callable[[dict], Any]) -> None:
        records.update_record(self.run_id, change, self.ctx.directory, known=self.markers)

    def note(self, **fields: Any) -> None:
        """Merge ``fields`` into the result and the record on disk."""
        self.fields.update(fields)
        self._write(lambda record: record.update(fields))

    def comment(self, entry: Mapping) -> None:
        """A comment this run wrote (``{"id", "kind", "number"}``): only these IDs are trusted later."""
        item = {key: entry.get(key) for key in ("id", "kind", "number")}
        self.fields.setdefault("comments", []).append(item)
        self._write(lambda record: record.setdefault("comments", []).append(item))

    def stage_record(self, kind: str, data: Mapping) -> None:
        item = dict(data)
        self.fields.setdefault("stages", {})[kind] = item
        self._write(lambda record: record.setdefault("stages", {}).__setitem__(kind, item))

    def mask(self, values: Iterable[str]) -> None:
        self.markers.extend(value for value in values if isinstance(value, str) and value and value not in self.markers)

    # --- the run ----------------------------------------------------------------------------------

    def run(self) -> dict:
        """The run (see the module docstring), then its end. SIGTERM or SIGHUP, the supervisor's
        timeout or stop, raises `socket_service.RunStopped` anywhere in it (inside a stage with the
        runner socket through `socket_service.serve`); the run then ends ``failed: infra`` and
        records, notifies, releases its claim and cleans up, in that order, before the
        supervisor's grace period ends. A second signal during that end is only recorded."""
        stop = {"armed": True, "stopped": None, "raised": False}
        previous = socket_service._install_stop_handlers(stop)
        stopped = False
        try:
            try:
                self._run()
            except Stopped as error:
                self.fields.update(error.fields)
            except broker_module.CredentialsMissing as error:
                self.fields.update(preconditions.refusal(error.missing))
            except socket_service.RunStopped as error:
                stop["armed"], stopped = False, True
                self.fields.update(runner.infra(f"stopped: the run received {signal.Signals(error.signal).name} "
                                                "(a supervisor timeout or stop); it ended its session and recorded"))
            except Exception as error:  # noqa: BLE001 (a defect still ends the run recorded and notified)
                self.fields.update(runner.infra(f"cli: the run sequence failed: {type(error).__name__}: "
                                                f"{str(error)[:300]}"))
            finally:
                stop["armed"] = False
            self._end(stopped)
        finally:
            socket_service._restore_handlers(previous)
        return records.mask(self.fields, self.markers)  # the caller merges it into the record unmasked

    def _end(self, stopped: bool) -> None:
        """``state.json`` done, the claim released (`release_claim`), the session directories and
        workers ended (`_close`), the report and the notification (`_finish`). After a stop the
        record and the notification come first and the workers get `STOP_CLOSE_WAIT`: the
        supervisor kills the run `supervise.GRACE_SECONDS` after its SIGTERM, so the claim's release,
        which talks to GitHub, comes last and is best effort."""
        self._final_state()
        if stopped:
            # Local work first; the claim's release talks to GitHub and is best effort within
            # the grace (#347 review round 2).
            self._write(lambda record: record.update(self.fields))
            self._finish()
            self._close(wait=STOP_CLOSE_WAIT)
            self.release_claim()
            return
        self.release_claim()
        self._close()
        self._finish()

    def release_claim(self) -> None:
        """A run that ends other than ``completed`` after its claim, with no pull request and no
        block comment yet (a failure, a refusal, a stop), blocks its issue with `GENERIC_BLOCKED`
        and removes the claim, as a blocked run does (Q-claim, #347 review): the issue neither waits
        six hours for the gate's stale-claim cleanup nor returns to the pool unseen."""
        broker = self.broker
        if broker is None or broker.run.issue is None or broker.run.pr is not None or broker.run.read_only:
            return
        if self.fields.get("status") == "completed" or not broker.log.done("select") or broker.log.done("block"):
            return
        try:
            self.block([GENERIC_BLOCKED])
        except Exception as error:  # noqa: BLE001 (the end of the run goes on)
            self.note(block_failed=f"{type(error).__name__}: {str(error)[:300]}")

    def _run(self) -> None:
        checked = check_drift(self.ctx, self.config)
        self.note(**checked)
        if checked.get("status"):
            return
        self.pinned = checked["pinned"]
        self.client = (self.ctx.client_factory or runner.default_client)(self.config)
        if self.mode != "dry-run":
            self.credentials = (self.ctx.credentials or broker_module.Credentials.from_config)(self.config)
            self.mask(broker_module.credential_values(self.credentials, self.ctx.key_reader).values())
            missing = preconditions.check(self.client, self.credentials, repository=self.repository,
                                          key_reader=self.ctx.key_reader)
            if missing:
                raise Stopped(preconditions.refusal(missing))
        decision = self._gate()
        if decision.decision == "wait":
            if decision.actions and self.mode != "dry-run":
                self._open_broker("run", self.pinned)
                self._cleanup(decision.actions)
            self.note(status="completed")
        elif decision.decision == "resume":
            if self.mode == "dry-run":
                self.note(status="completed", note="dry-run: the gate decided resume; no session started")
                return
            self.resume(decision)
        elif self.mode == "resume":
            self.note(status="completed")
        else:
            self.select(decision)

    def _gate(self):
        try:
            self.snapshot = fetch_snapshot(self.client, self.now)
        except TokenUnavailable as error:
            raise infra(f"cli: {error}") from None
        except NetworkError as error:
            raise infra(f"network: {error}") from None
        except GitHubError as error:
            self.note(github_requests=list(self.client.requests))
            raise infra(f"network: {error}") from None
        decision = gate(self.snapshot, records.trusted_comment_ids(self.ctx.directory),
                        max_bot_rounds=self.config["limits"]["max_bot_rounds"], now=self.now, pr=self.pr)
        self.note(gate=decision.to_dict(), cleanup_executed=False)
        return decision

    # --- the broker ---------------------------------------------------------------------------------

    def _stage_paths(self) -> list[Path]:
        root = self._sessions_root()
        found = []
        for kind in stages.KINDS:
            layout = sandbox.session_layout(root, self.run_id, kind)
            found += [layout["repository"], layout["exchange"]]
        return found

    def _open_broker(self, mode: str, base: str) -> broker_module.Broker:
        if self.broker is not None:
            return self.broker
        self._plant()
        trusted = self._trusted()
        git = broker_module.TrustedGit(trusted.path, runner=self.ctx.push_runner, lists_at=self.pinned)
        run = broker_module.Run(self.run_id, self.repository, None, base, mode)

        def pr_allowed() -> str | None:
            # A new run's pull request needs the accepted pre-mortem too (#347 review); a resume's exists.
            if mode != "resume" and not self._premortem_accepted():
                return NO_PREMORTEM
            return OPEN_RISK if workers.open_security_risks(self.run_dir) else None

        try:
            self.broker = broker_module.Broker(
                run, credentials=self.credentials, git=git, run_dir=self.run_dir,
                session_writable=self._stage_paths(), transport=self.ctx.broker_transport,
                key_reader=self.ctx.key_reader, decoys=decoys.known(self.planted), on_decoy=self._decoy_hit,
                known=[secret for secret in self.known if secret.label == "oauth token"], pr_allowed=pr_allowed)
        except broker_module.UnsafeRunDirectory as error:
            raise refused(f"broker: {error}") from None
        return self.broker

    def _decoy_hit(self, hits) -> None:
        labels = sorted({hit.name for hit in hits})
        decoys.trip(self.ctx.directory, "broker: outgoing text named " + ", ".join(labels[:5]), run_id=self.run_id)
        self.note(tripwire=True)

    def _cleanup(self, actions: list) -> None:
        try:
            done = self.broker.cleanup(actions)  # type: ignore[union-attr]
        except broker_module.BrokerRefused as error:
            raise infra(f"network: cleanup: {error}") from None
        self.note(cleanup_executed=True, cleanup=done)

    def block(self, blockers: list[str], *, unclaim: bool = True) -> None:
        """``autodev:blocked`` with the neutral ``blockers`` on the run's issue, and the claim removed.
        A list the broker refuses to post (a mention, a closing keyword for another issue, a
        secret-like value) is replaced by `GENERIC_BLOCKED`, so the issue is still labelled."""
        self.note(blockers=list(blockers))
        if self.broker is None or self.broker.run.issue is None:
            return
        try:
            try:
                comment = self.broker.block(blockers)
            except broker_module.BrokerRefused as error:
                if self.broker.log.done("block"):
                    raise  # something was written: no second comment
                self.note(block_refused=str(error)[:300])
                comment = self.broker.block([GENERIC_BLOCKED])
            self.comment(comment)
            if unclaim:
                self.broker.unclaim()
        except broker_module.BrokerRefused as error:
            self.note(block_failed=str(error)[:300])

    # --- the session environment -------------------------------------------------------------------

    def _trusted(self) -> repos.TrustedRepository:
        if self.trusted is None:
            trusted = repos.TrustedRepository.from_config(self.config, self.ctx.installation)
            try:
                trusted.verify()
            except repos.RepositoryError as error:
                raise infra(f"cli: trusted repository: {error}") from None
            self.trusted = trusted
        return self.trusted

    def _sessions_root(self) -> Path:
        return sandbox.sessions_root(self._trusted().path)

    def _state_copy(self) -> Path:
        return sandbox.session_layout(self._sessions_root(), self.run_id, stages.SELECT)["state_copy"]

    def _plant(self) -> None:
        """The decoys (`decoys.ensure`) and the secrets every scan of the run knows (the OAuth token
        of the keychain login mode, the broker's token and deploy key); once, before the broker."""
        if self.prepared:
            return
        ctx = self.ctx
        try:
            self.login = sandbox.login_mode(self.config)
            if self.login != sandbox.LOGIN_CONFIG_DIR:
                self.oauth = sandbox.keychain_secret(self.login.removeprefix(sandbox.KEYCHAIN_PREFIX),
                                                     runner=ctx.command_runner)
                self.known.append(scan.KnownSecret("oauth token", self.oauth))
            if self.credentials is not None:
                self.known += [scan.KnownSecret(label, value) for label, value in
                               broker_module.credential_values(self.credentials, ctx.key_reader).items()
                               if len(value) >= scan.KNOWN_MIN_LENGTH]
            else:
                self.known += runner.broker_secrets(ctx, self.config)
            self.mask(secret.value for secret in self.known if isinstance(secret.value, str))
            self.planted = decoys.ensure(ctx.directory, ctx.home, keychain=self.config["sandbox"]["decoy_keychain"],
                                         runner=ctx.command_runner)
            self.mask(decoy.marker for decoy in self.planted)
        except (sandbox.SandboxError, decoys.DecoyError) as error:
            raise refused(f"sandbox: {error}") from None
        self.prepared = True

    def _session_setup(self) -> None:
        """The claude executable and its login (no model turn), after `_plant`; once."""
        if self.claude is not None:
            return
        self._plant()
        claude = runner.claude_executable(self.ctx)
        if not claude:
            raise infra("cli: the claude executable was not found (tools.json, PATH)")
        self.path = runner.run_environment(self.ctx)["PATH"]
        try:
            sandbox.require_login(claude, sandbox.config_dir(self.ctx.directory), path=self.path, home=self.ctx.home,
                                  oauth_token=self.oauth, runner=self.ctx.command_runner)
        except sandbox.LoginRequired as error:
            raise refused(str(error), login_command=error.command) from None
        except sandbox.SandboxError as error:
            raise refused(f"sandbox: {error}") from None
        self.claude = claude

    def _capability_table(self) -> dict:
        if self.capabilities is None:
            uses_codex = any(((self.config.get("stages") or {}).get(kind) or {}).get("client") == "codex"
                             for kind in stages.KINDS)
            codex = runner.codex_executable(self.ctx) if uses_codex else None
            self.capabilities = flow_execution.installed(self.claude, codex, path=self.path,
                                                         runner=self.ctx.command_runner)
            self.note(capabilities={client: capability.to_dict() for client, capability in self.capabilities.items()})
        return self.capabilities

    def _codex_unavailable(self, kind: str) -> str | None:
        """Why a Codex ``select`` stage cannot run now (the stored availability), or None."""
        profile = ((self.config.get("stages") or {}).get(kind)) or {}
        if profile.get("client") != "codex" or kind in stages.WRITING_KINDS:
            return None
        if self.ctx.codex_availability is not None:
            found = self.ctx.codex_availability(kind)
        else:
            codex = runner.codex_executable(self.ctx)
            if not codex:
                return "codex_missing"
            try:
                found = codex_probes.read_availability(
                    self.ctx.directory, codex_sandbox.STAGE,
                    version=codex_probes.installed_version(codex, path=self.path, command_runner=self.ctx.command_runner),
                    shape=codex_probes.stage_shape(codex, self.config, kind))
            except codex_sandbox.CodexSandboxError:
                return "codex_missing"
        return None if found.get("available") is True else str(found.get("reason") or "not_checked")

    def write_state(self, stage: str) -> None:
        if self.pinned is None or self.trusted is None:
            return
        if self.state is None:
            try:
                flows = self.trusted.blob_ids(self.pinned, run_state.FLOW_PATHS)
            except (repos.RepositoryError, ValueError):
                flows = {}
            self.state = run_state.RunState(self.run_id, self.mode, self.repository, self.pinned,
                                            runner_commit=self.fields.get("runner_commit"), base=self.pinned,
                                            flows=flows, limits=dict(self.config["limits"]))
        self.state.stage = stage
        run_state.write_state(self.run_dir, self.state, self._state_copy(), known=self.markers)

    # --- a stage session ----------------------------------------------------------------------------

    def _layout(self, kind: str) -> dict:
        layout = sandbox.session_layout(self._sessions_root(), self.run_id, kind)
        if not os.path.isdir(layout["base"]):
            sandbox.prepare_layout(layout)
        return layout

    def stage(self, kind: str, *, inputs: Mapping, branch: str | None = None, issue: int | None = None,
              candidates: Iterable[int] | None = None, run_branch: str | None = None) -> StageOutcome:
        """Run one stage session (see the module docstring). ``branch`` is the branch the one-off
        repository holds (submit); ``run_branch`` the run's branch the output must name (implement)."""
        self._session_setup()
        profile = stages.stage_profile(kind, self.config, codex_unavailable=self._codex_unavailable(kind))
        record: dict = {"status": "running", "client": profile["client"], "requested_client": profile["requested"],
                        "fallback_reason": profile["fallback_reason"],
                        "stream": f"{STAGES_DIR}/{kind}/{STREAM_NAME}"}
        try:
            flow_execution.check_effort(profile["client"], profile["effort"], self._capability_table())
            warnings = flow_execution.check_model(profile["model"], profile["client"])
        except flow_execution.FlowInvalid as error:
            self.stage_record(kind, {**record, "status": "refused", "reason": str(error)})
            raise refused(str(error), allowed=error.allowed) from None
        if warnings:
            record["model_warnings"] = warnings
        self.stage_record(kind, record)
        layout = self._layout(kind)
        trusted = self._trusted()
        try:
            session = trusted.create_session_repository(layout["repository"], branch=branch)
            if self.venv is None:
                self.venv = trusted.environment(self.pinned, trusted.path.parent, builder=self.ctx.venv_builder)
        except repos.FetchError as error:
            raise infra(f"network: {error}") from None
        except (repos.RepositoryError, ValueError, OSError) as error:
            raise infra(f"cli: {kind} session repository: {type(error).__name__}: {error}") from None
        state_copy = Path(layout["state_copy"])
        if kind == stages.SELECT:
            stages.write_select_inputs(inputs, state_copy)
        else:
            stages.write_inputs(state_copy, kind, inputs)
        self.write_state(kind)
        values = {"repository": self.repository, "worktree": str(session.path), "pinned": self.pinned,
                  "inputs_file": str(stages.inputs_path(state_copy, kind)), "state_copy": str(state_copy),
                  "exchange_dir": str(layout["exchange"]), "branch": run_branch or branch,
                  "commit_author": COMMIT_AUTHOR, "python": str(Path(self.venv) / "bin" / "python")}
        stream_dir = private_dir(private_dir(self.run_dir / STAGES_DIR) / kind)
        if self.broker is not None:
            self.broker.enter_stage(kind)  # only the submit session's requests may write (#347 review)
        try:
            if profile["client"] == "codex":
                outcome = self._codex_stage(kind, profile, layout, session, values, stream_dir, record,
                                            issue=issue, candidates=candidates, run_branch=run_branch)
                if outcome is not None:
                    return outcome
                profile = stages.stage_profile(kind, self.config, codex_unavailable=record["fallback_reason"])
                record.update(client="claude", fallback_reason=profile["fallback_reason"])
            return self._claude_stage(kind, profile, layout, session, values, stream_dir, record,
                                      issue=issue, candidates=candidates, run_branch=run_branch)
        finally:
            if self.broker is not None:
                self.broker.enter_stage(None)

    def _seconds(self, kind: str) -> float:
        override = (self.ctx.stage_seconds or {}).get(kind)
        return float(override) if override else stage_minutes(self.config, kind) * 60.0

    def _probe_mode(self) -> str:
        if self.mode == "run":
            return "scheduled" if self.trigger in ("slot", "continuation") else "run-now"
        return self.mode

    def _claude_stage(self, kind: str, profile: dict, layout: dict, session: repos.SessionRepository,
                      values: dict, stream_dir: Path, record: dict, *, issue: int | None,
                      candidates: Iterable[int] | None, run_branch: str | None) -> StageOutcome:
        ctx = self.ctx
        has_bash = "Bash" in sandbox.tools_for(kind)
        temp_root = sandbox.make_temp_root(ctx.temp_base)
        self.temp_roots.append(temp_root)
        repository, exchange = Path(session.path), Path(layout["exchange"])
        with contextlib.ExitStack() as stack:
            endpoint = None
            try:
                if has_bash:
                    endpoint = stack.enter_context(socket_service.serve(
                        self.run_dir, self.registry, session_writable=[repository, exchange, temp_root],
                        temp_root=ctx.socket_root, session_root=lambda: self.holder["pid"]))
                    self.note(endpoint=endpoint.to_record())
                    values.update(endpoint.commands())
                interpreters = [socket_service.client_interpreter()]
                if (base := sandbox.venv_base_interpreter(self.venv)) is not None:
                    interpreters.append(str(base))
                roots = sandbox.interpreter_roots(*interpreters)
                if endpoint is not None:
                    roots = (endpoint.socket.parent / socket_service.CLIENTS_DIR, *roots)
                config_dir = sandbox.config_dir(ctx.directory)
                paths = sandbox.SessionPaths(
                    home=ctx.home, repository=repository, exchange=exchange, state_copy=Path(layout["state_copy"]),
                    venv=Path(self.venv), config_dir=config_dir, gh_config=Path(layout["gh_config"]),
                    settings_file=Path(layout["settings_file"]), tmp_root=temp_root, state_dir=Path(ctx.directory),
                    socket=endpoint.socket if endpoint is not None else None, read_roots=roots,
                    **dict(ctx.session_temps or {}))
                spec = sandbox.build(self.claude, kind, paths, path=self.path, mode=self.login,
                                     oauth_token=self.oauth, model=profile["model"], effort=profile["effort"],
                                     schema=stages.schema_text(kind), run_variables={"AUTODEV_RUN_ID": self.run_id})
                report = probes.guard(spec, directory=ctx.directory,
                                      targets=runner.probe_targets(ctx, self.config, self.login, config_dir),
                                      persistent=self.planted, mode=self._probe_mode(), run_id=self.run_id,
                                      known=self.known, force_session=False, platform=ctx.platform,
                                      timeout=ctx.probe_timeout, socket_root=ctx.socket_root,
                                      command_runner=ctx.command_runner, managed=ctx.managed_sources,
                                      allowed_plugins=self.config["sandbox"]["allowed_plugins"],
                                      resolver=ctx.resolver)
                probes.record_report(self.run_id, ctx.directory, report, self.planted, self.markers)
                record.update(probe_digest=report.digest, probe_session=report.ran_session)
                if not report.ok:
                    tripped = " and the decoy tripwire turned the schedule off" if report.tripped else ""
                    self.stage_record(kind, {**record, "status": "refused", "reason": "sandbox probes failed"})
                    raise refused(f"sandbox probes failed before the {kind} session{tripped}",
                                  tripwire=bool(report.tripped))
                before = integrity.git_snapshot(repository)
                prompt = stages.render_prompt(kind, {name: values[name] for name in stages.template_fields(kind)})
                exit_code, timed_out, stream = self._run_process(kind, stream_dir, lambda out, err: ctx.stage_starter(
                    spec, report, prompt, stdout=out, stderr=err, platform=ctx.platform,
                    managed=ctx.managed_sources))
            except (sandbox.SandboxError, socket_service.UnsafeSocketDirectory) as error:
                self.stage_record(kind, {**record, "status": "refused", "reason": f"sandbox: {error}"})
                raise refused(f"sandbox: {error}") from None
            except (ValueError, OSError) as error:
                self.stage_record(kind, {**record, "status": "failed", "reason": f"{type(error).__name__}"})
                raise infra(f"cli: {kind} session: {type(error).__name__}: {error}") from None
        self._tripwire(kind, record, stream)
        with open(stream, encoding="utf-8", errors="replace") as lines:
            result = stages.parse_stage_stream(lines)
        record.update(exit=exit_code, timed_out=timed_out, cost_usd=result.cost_usd, denials=len(result.denials),
                      execution=stages.execution_of(kind, self.config, spec.argv, result, client="claude",
                                                    fallback_reason=profile.get("fallback_reason")))
        return self._after_session(kind, record, layout, repository, before, timed_out,
                                   result.structured if result.structured is not None else None,
                                   result.info.get("problem"), issue=issue, candidates=candidates,
                                   run_branch=run_branch)

    def _run_process(self, kind: str, stream_dir: Path, start: Callable) -> tuple[int | None, bool, Path]:
        """Start the session with standard output and error into the run directory, wait with the
        stage's timeout, then end its process group; the exit code, whether it timed out, and the
        file its output went to."""
        stream, errors = stream_dir / STREAM_NAME, stream_dir / STDERR_NAME
        with open(stream, "wb") as out, open(errors, "wb") as err:
            os.chmod(stream, 0o600)
            os.chmod(errors, 0o600)
            process = start(out, err)
            self.holder["pid"] = process.pid
            timed_out, stopping = False, False
            try:
                process.wait(timeout=self._seconds(kind))
            except subprocess.TimeoutExpired:
                timed_out = True
            except socket_service.RunStopped:
                stopping = True  # the supervisor's grace period runs: SIGKILL after one second
                raise
            finally:
                _kill_group(process, STOP_TERMINATE_WAIT if stopping else TERMINATE_WAIT)
                self.holder["pid"] = None
        return process.returncode, timed_out, stream

    def _tripwire(self, kind: str, record: dict, stream: Path) -> None:
        """`probes.stage_tripwire` on the whole stream file, read line by line (#347 review); a
        stream over `MAX_STREAM_BYTES` cannot be scanned in time and is refused the same way."""
        size = stream.stat().st_size
        if size > MAX_STREAM_BYTES:
            reason = f"the {kind} session's stream has {size} bytes, more than the {MAX_STREAM_BYTES} bytes scanned"
            decoys.trip(self.ctx.directory, f"{kind} session: {reason}", run_id=self.run_id)
            self.stage_record(kind, {**record, "status": "refused", "reason": "the stream is too long to scan"})
            raise refused(f"{reason}; the schedule is off", tripwire=True)
        with open(stream, encoding="utf-8", errors="replace") as lines:
            findings = probes.stage_tripwire(lines, self.planted, directory=self.ctx.directory, run_id=self.run_id)
        if findings:
            self.stage_record(kind, {**record, "status": "refused", "reason": "the decoy tripwire"})
            raise refused(f"the decoy tripwire turned the schedule off after the {kind} session", tripwire=True,
                          tripwire_findings=findings[:10])

    def _after_session(self, kind: str, record: dict, layout: dict, repository: Path, before: dict,
                       timed_out: bool, raw: Any, problem: str | None, *, issue: int | None,
                       candidates: Iterable[int] | None, run_branch: str | None) -> StageOutcome:
        """The ``.git`` check and the output check after a session ended and passed `_tripwire`."""
        try:
            changes = integrity.git_changes(before, integrity.git_snapshot(repository))
        except OSError as error:
            changes = [f".git cannot be read: {type(error).__name__}"]
        if changes:
            reason = integrity.reason(changes)
            decoys.trip(self.ctx.directory, f"{kind} session: {reason}", run_id=self.run_id)
            self.stage_record(kind, {**record, "status": "refused", "reason": reason, "integrity": changes[:10]})
            raise refused(reason, tripwire=True, schedule="disabled")
        record["integrity"] = "unchanged"
        if timed_out:
            minutes = round(self._seconds(kind) / 60, 1)
            self.stage_record(kind, {**record, "status": "failed", "reason": "timeout"})
            raise infra(f"timeout: the {kind} session exceeded {minutes} minutes; its process group was ended")
        if raw is None:
            output, problems = None, [problem or "no structured output"]
        else:
            output, problems = stages.validate_output(kind, raw, repository=self.repository, issue=issue,
                                                      candidates=candidates, branch=run_branch)
        record.update(status="completed" if output is not None else "invalid", output_sha256=_sha256_json(output),
                      problems=problems[:20])
        self.stage_record(kind, record)
        return StageOutcome(kind, output, problems, record, layout)

    # --- a Codex stage (select only) -----------------------------------------------------------------

    def _codex_stage(self, kind: str, profile: dict, layout: dict, session: repos.SessionRepository, values: dict,
                     stream_dir: Path, record: dict, *, issue: int | None, candidates: Iterable[int] | None,
                     run_branch: str | None) -> StageOutcome | None:
        """The select stage through ``codex exec`` (`codex_probes.decide`, then `codex_probes.start`);
        None when the decision falls back to Claude (``record['fallback_reason']`` says why)."""
        ctx = self.ctx
        codex = runner.codex_executable(ctx)
        temp_root = sandbox.make_temp_root(ctx.temp_base)
        self.temp_roots.append(temp_root)
        repository, state_copy = Path(session.path), Path(layout["state_copy"])
        try:
            mode = codex_sandbox.login_mode(self.config)
            directories = codex_sandbox.prepare_run_directories(temp_root)
            out_dir = private_dir(stream_dir / "codex-out")
            paths = codex_sandbox.CodexPaths(
                kind=codex_sandbox.STAGE, home=ctx.home, tmp_root=temp_root, out_dir=out_dir,
                profile=stream_dir / "codex.sb", state_dir=Path(ctx.directory), installation=Path(ctx.installation),
                repository=repository, exchange=Path(layout["exchange"]), read_roots=(state_copy,),
                venv=Path(self.venv), schema=stages.write_schema(state_copy, kind), **directories)
            targets = codex_probes.CodexTargets(
                state_dir=Path(ctx.directory), secrets_dir=ctx.home / ".config" / "agents-autodev",
                runner=runner.probe_targets(ctx, self.config, mode or sandbox.LOGIN_CONFIG_DIR))
            with codex_proxy.ConnectProxy(codex_sandbox.allowed_hosts()) as proxy:
                decision = ctx.codex_decide(
                    codex, paths, path=self.path, proxy=proxy, mode=mode, targets=targets, persistent=self.planted,
                    directory=ctx.directory, run_id=self.run_id, model=profile["model"], effort=profile["effort"],
                    known=self.known, platform=ctx.platform, execute_=ctx.codex_execute,
                    command_runner=ctx.command_runner, resolver=ctx.codex_resolver, socket_root=ctx.socket_root)
                self.mask(decision.known_values())
                codex_probes.record_decision(self.run_id, ctx.directory, f"stage-{kind}", decision, self.planted,
                                             self.markers)
                if decision.abort:
                    self.stage_record(kind, {**record, "status": "refused", "reason": decision.reason})
                    raise refused(f"codex: a deny probe got through before the {kind} session ({decision.reason})")
                if not decision.codex:
                    record["fallback_reason"] = decision.reason
                    return None
                before = integrity.git_snapshot(repository)
                prompt = stages.render_prompt(kind, {name: values[name] for name in stages.template_fields(kind)})
                exit_code, timed_out, stream = self._run_process(kind, stream_dir, lambda out, err: ctx.codex_start(
                    decision, prompt, proxy=proxy, stdout=out, stderr=err, platform=ctx.platform))
        except codex_sandbox.CodexSandboxError as error:
            self.stage_record(kind, {**record, "status": "refused", "reason": f"codex: {error}"})
            raise refused(f"codex: {error}") from None
        except (ValueError, OSError) as error:
            raise infra(f"cli: {kind} Codex session: {type(error).__name__}: {error}") from None
        try:
            raw = paths.output.read_text(encoding="utf-8")
        except OSError:
            raw = None
        self._tripwire(kind, record, stream)
        record.update(exit=exit_code, timed_out=timed_out, client="codex", fallback_reason=None,
                      execution=stages.execution_of(kind, self.config, decision.launch.argv, None, client="codex"))
        return self._after_session(kind, record, layout, repository, before, timed_out, raw,
                                   "codex wrote no output", issue=issue, candidates=candidates,
                                   run_branch=run_branch)

    # --- select -------------------------------------------------------------------------------------

    def select(self, decision) -> None:
        try:
            data = candidate_data.fetch_candidates(self.client, pulls=self.snapshot.get("pulls") or [])
        except TokenUnavailable as error:
            raise infra(f"cli: {error}") from None
        except (NetworkError, GitHubError) as error:
            raise infra(f"network: candidates: {error}") from None
        numbers = [item["number"] for item in data["issues"]]
        self.note(candidates={"count": len(numbers), "numbers": numbers[:50], "dropped": data["dropped"]})
        if self.mode != "dry-run":
            self._open_broker("run", self.pinned)
            if decision.actions:
                self._cleanup(decision.actions)
        if not numbers:
            self.note(selection={"decision": "none", "issue": None, "slice": None, "scores": [], "top_five": [],
                                 "rationale": "no open issue carries autodev:ready or autodev:next", "table": None},
                      status="completed")
            return
        mode = "dry-run" if self.mode == "dry-run" else "run"
        inputs = stages.select_inputs(repository=self.repository, mode=mode,
                                      now=self.now.astimezone(timezone.utc).isoformat(timespec="seconds"),
                                      issues=data["issues"])
        outcome = self.stage(stages.SELECT, inputs=inputs, candidates=numbers)
        self._stage_done(stages.SELECT)
        if outcome.output is None:
            self.note(selection={"decision": None, "problems": outcome.problems[:20]})
            raise Stopped({"status": "blocked", "reason": INVALID_OUTPUT.format(kind="select"),
                           "problems": outcome.problems[:20]})
        output = outcome.output
        view = run_report.selection_view(output, self.repository)
        self.note(selection={**output, "top_five": view["top_five"], "table": view["table"]})
        if self.mode == "dry-run" or output["decision"] != "task":
            self.note(status="completed")
            return
        candidate = candidate_data.selected(data, output["issue"])
        self.implement(output, candidate)

    # --- implement ----------------------------------------------------------------------------------

    def _open_service(self, issue: int, base: str, branch: str, exchange: Path, criteria: tuple[str, ...]) -> None:
        ctx = self.ctx
        delegates = self.config.get("delegates") or {}
        uses_codex = any((profile or {}).get("client") == "codex" for profile in delegates.values())
        try:
            codex_login = codex_sandbox.login_mode(self.config)
        except codex_sandbox.CodexSandboxError:
            codex_login = None
        launcher = worker_launch.Launcher(
            claude=self.claude, path=self.path, home=ctx.home, state_dir=Path(ctx.directory), run_id=self.run_id,
            codex=runner.codex_executable(ctx) if uses_codex else None, installation=Path(ctx.installation),
            login_mode=self.login, oauth_token=lambda: self.oauth, codex_login=codex_login, decoys=self.planted,
            known=self.known, temp_base=ctx.temp_base, platform=ctx.platform, command_runner=ctx.command_runner,
            codex_execute=ctx.codex_execute, codex_resolver=ctx.codex_resolver,
            worker_paths=dict(ctx.session_temps or {}))
        binding = workers.Binding(self.run_id, issue, base, self._trusted().path, Path(ctx.directory),
                                  log_copy=self._state_copy(), criteria=criteria)
        self.service = workers.WorkerService(
            binding, self.config, launcher, importer=workers.Handover(self._trusted(), exchange, branch),
            on_known=self.broker.add_known if self.broker is not None else None,
            max_verifications=1 + int(self.config["limits"]["max_bot_rounds"]))
        self.registry = socket_service.Registry()
        workers.register(self.registry, self.service)
        heavy.register(self.registry, ctx.directory)
        broker_module.register(self.registry, self.broker)

    def _stage_done(self, kind: str) -> None:
        if self.state is not None and kind not in self.state.stages_done:
            self.state.stages_done.append(kind)

    def implement(self, output: Mapping, candidate: Mapping) -> None:
        issue, labels = int(output["issue"]), list(candidate.get("labels") or [])
        try:
            self.broker.bind_issue(issue, labels)  # type: ignore[union-attr]
            body = stages.selection_body(output, self.repository)
            self.comment({**self.broker.select(body), "number": issue})  # type: ignore[union-attr]
        except ValueError as error:
            raise Stopped({"status": "blocked", "reason": f"the selection comment: {error}"}) from None
        except broker_module.BrokerRefused as error:
            raise infra(f"network: the selection comment: {error}") from None
        slug = stages.branch_slug(str(candidate.get("title") or ""), issue)
        branch = repos.branch_name(issue, slug)
        self.note(issue=issue, branch=branch)
        self._set_aside(branch)
        if self.state is not None:
            self.state.issue, self.state.branch = issue, branch
            self.state.slice, self.state.criteria_refs = dict(output["slice"]), list(output["criteria_refs"])
        criteria = candidate_data.criteria(candidate, output["criteria_refs"], self.repository,
                                           limit=workers.MAX_CRITERIA, max_chars=workers.MAX_CRITERION_CHARS)
        layout = self._layout(stages.IMPLEMENT)
        self._open_service(issue, self.pinned, branch, Path(layout["exchange"]), criteria)
        inputs = stages.implement_inputs(issue=issue, slice=output["slice"], criteria_refs=output["criteria_refs"],
                                         labels=labels, max_reviews=int(self.config["limits"]["max_reviews"]))
        outcome = self.stage(stages.IMPLEMENT, inputs=inputs, issue=issue, run_branch=branch)
        self._stage_done(stages.IMPLEMENT)
        self.after_implement(outcome, issue, branch, labels)
        self.submit(issue, branch, slice=output["slice"], description_extra=outcome.output["pr_sections"],
                    labels=labels)

    def _set_aside(self, branch: str) -> None:
        """Move a ``refs/heads/<branch>`` an earlier run left in the trusted repository (a failed or
        refused run, a squash-merged slice, a closed pull request selected again) to this run's
        ``refs/autodev/quarantine/<run-id>/stale`` before the implement stage, and record it: the run
        builds on ``pinned``, and no import is refused as a non-fast-forward of it (#347 review)."""
        trusted = self._trusted()
        try:
            stale = trusted.branch_head(branch)
            if stale is not None:
                self.note(stale_branch={"head": stale,
                                        "quarantined": trusted.quarantine_branch(branch, self.run_id, "stale")})
        except (repos.RepositoryError, ValueError) as error:
            raise infra(f"cli: trusted repository: {error}") from None

    def _base(self) -> str:
        """The run's base: ``pinned``, or the merge base a resume run's broker is bound to."""
        return self.broker.run.base if self.broker is not None else self.pinned  # type: ignore[return-value]

    def _change_refusals(self, head: str | None, labels: Iterable[str]) -> list:
        """The change check's refusals of ``<base>..head`` in the trusted repository (the lists at
        ``pinned``), or none without a head."""
        if head is None:
            return []
        trusted = self._trusted()
        try:
            classes = path_check.load_path_classes(trusted.path, self.pinned)
            return list(path_check.check_changes(trusted.path, self._base(), head, labels, classes).refusals)
        except path_check.PathsError as error:
            raise infra(f"cli: the change check: {error}") from None

    def _export(self, head: str) -> None:
        """The control-path change ``<base>..head`` as a patch and a bundle for the owner (design 5.3)."""
        try:
            exports = self._trusted().export_commits(self._base(), head, self.run_dir / repos.EXPORT_DIR, self.run_id)
            self.note(exports={key: exports[key] for key in ("patch", "bundle", "sha256", "commits")})
        except (repos.RepositoryError, ValueError, OSError) as error:
            self.note(export_failed=f"{type(error).__name__}: {str(error)[:300]}")

    def _premortem_accepted(self) -> bool:
        data = read_json(self.run_dir / broker_module.PREMORTEM_RECORD, None)
        return isinstance(data, dict) and data.get("validated") is True

    def after_implement(self, outcome: StageOutcome, issue: int, branch: str, labels: list[str]) -> None:
        """The runner's steps after the implement stage (design 9.4); returns when the submit stage
        may run, else ends the run ``blocked``."""
        trusted = self._trusted()
        blockers: list[str] = []
        try:
            self.service.importer(self.service.binding)  # type: ignore[union-attr]
        except repos.BundleRefused as error:
            blockers.append(BUNDLE_REFUSED)
            self.note(final_import={"refused": error.reason[:300]})
        except (repos.RepositoryError, ValueError, OSError) as error:
            raise infra(f"cli: the final import: {type(error).__name__}: {error}") from None
        head = trusted.branch_head(branch)
        refusals = self._change_refusals(head, labels)
        control = [item for item in refusals if item.reason == path_check.Reason.CONTROL_PATH]
        output = outcome.output
        if output is None:
            blockers.append(INVALID_OUTPUT.format(kind=stages.IMPLEMENT))
        elif output["blocked"]:
            blockers += list(output["blockers"])
        else:
            if output["head_sha"] != head:
                blockers.append(HEAD_MISMATCH)
            elif broker_module.validated_review_head(self.run_dir) != head:
                blockers.append(NO_VALIDATED_REVIEW)
            if not self._premortem_accepted():
                blockers.append(NO_PREMORTEM)
        if workers.open_security_risks(self.run_dir):
            blockers.append(OPEN_RISK)
        if control:
            blockers.append(CONTROL_PATH)
        elif refusals:
            blockers.append(PROTECTED_PATH)
        if (early := self.broker.run.pr) is not None:  # type: ignore[union-attr]
            # The broker lets only the submit session write; should a pull request exist anyway,
            # it is named in the record, the report and the notification, and the run stops.
            blockers.append(EARLY_PR)
            self.note(pr=early, pr_url=f"https://github.com/{self.repository}/pull/{early}")
        self.note(implement={"head": head, "blocked": bool(blockers),
                             "change_check": [{"path": item.path, "reason": str(item.reason)} for item in refusals[:20]]},
                  checks=list((output or {}).get("checks") or []), head=head)
        if self.state is not None:
            self.state.head = head
        if not blockers:
            return
        if control and head is not None:
            self._export(head)
        if head is not None:
            try:
                self.note(quarantined=trusted.quarantine_branch(branch, self.run_id))
            except (repos.RepositoryError, ValueError) as error:
                self.note(quarantine_failed=str(error)[:300])
        self.block(list(dict.fromkeys(blockers)), unclaim=early is None)  # the claim stays while a PR exists
        raise Stopped({"status": "blocked", "reason": "the implementation stage ended blocked"})

    # --- submit -------------------------------------------------------------------------------------

    def submit(self, issue: int, branch: str, *, slice: Mapping | None, description_extra: Mapping,
               labels: Iterable[str] = (), resume: Mapping | None = None, state_comment: int | None = None) -> None:
        layout = self._layout(stages.SUBMIT)
        self.service.rebind_importer(workers.Handover(self._trusted(), Path(layout["exchange"]), branch))  # type: ignore[union-attr]
        limits = self.config["limits"]
        inputs = stages.submit_inputs(branch=branch, description_extra=description_extra, issue=issue, slice=slice,
                                      max_bot_rounds=int(limits["max_bot_rounds"]),
                                      max_wait_minutes=int(limits["max_wait_minutes"]), resume=resume)
        outcome = self.stage(stages.SUBMIT, inputs=inputs, branch=branch, issue=issue, run_branch=branch)
        self._stage_done(stages.SUBMIT)
        self.after_submit(outcome, state_comment, branch=branch, labels=list(labels), resume=resume)

    def _recorded_state_comment(self, pr: int) -> int | None:
        """The newest state comment of ``pr`` an earlier run record lists."""
        for record in records.all_records(self.ctx.directory):
            if record.get("run_id") == self.run_id:
                continue
            for comment in reversed(record.get("comments") or []):
                if isinstance(comment, dict) and comment.get("kind") == "state" and comment.get("number") == pr \
                        and type(comment.get("id")) is int:
                    return comment["id"]
        return None

    def _pushed_head(self) -> str | None:
        """The head the broker last pushed in this run, else (a resume that has not pushed) the PR
        head the resume checked at its start: an in-session review import moves the trusted branch
        head without a push, so the PR does not carry that head (#347 review round 2)."""
        pushed = [entry.get("head") for entry in self.broker.log.done("push")]  # type: ignore[union-attr]
        if pushed and isinstance(pushed[-1], str):
            return pushed[-1]
        return self.resume_head

    def _submit_problems(self, output: Mapping, pr: int | None, head: str | None) -> list[str]:
        """What makes a schema-valid submit output unusable (#347 review): a PR link that is not the
        broker's PR, and for an unblocked status no PR or a head the broker did not push."""
        problems = []
        url = output.get("pr_url")
        if url is not None and (pr is None or int(str(url).rstrip("/").rsplit("/", 1)[-1]) != pr):
            problems.append("pr_url: not this run's pull request")
        if output["review_status"] != "blocked":
            if pr is None:
                problems.append("no pull request was opened")
            elif output.get("head_sha") != head:
                problems.append("head_sha: not the head the broker pushed")
        return problems

    def after_submit(self, outcome: StageOutcome, state_comment: int | None, *, branch: str,
                     labels: list[str], resume: Mapping | None = None) -> None:
        """The runner's steps after the submit stage (design 9.5). The PR and its link come from
        the broker and the state's head from its last push, never from the session's output; an
        output that names others is invalid, as is an unblocked one without a PR. An invalid
        output keeps a resume's rounds and findings in the state comment."""
        output = outcome.output
        pr = self.broker.run.pr  # type: ignore[union-attr]
        head = self._pushed_head() if pr is not None else None
        if output is not None and (problems := self._submit_problems(output, pr, head)):
            self.note(submit_problems=problems, pr_url_reported=output.get("pr_url"),
                      head_reported=output.get("head_sha"))
            output = None
        status = output["review_status"] if output is not None else "blocked"
        next_attempt = output.get("next_attempt") if output is not None and status == "waiting_bots" else None
        pr_url = f"https://github.com/{self.repository}/pull/{pr}" if pr is not None else None
        self.note(pr=pr, pr_url=pr_url, review_status=status)
        if self.state is not None:
            self.state.pr = pr
        if pr is not None:
            earlier = resume or {}
            rounds = output["rounds"] if output is not None else int(earlier.get("rounds") or 0)
            findings = output["findings"] if output is not None else list(earlier.get("findings_seen") or [])
            state = {"head": head, "next_attempt": next_attempt, "rounds": rounds, "review_status": status,
                     "findings": list(findings)}
            written = False
            try:
                _, clean = broker_module.state_body(state, datetime.now(timezone.utc))
                self.comment(self.broker.state(state, comment_id=state_comment))  # type: ignore[union-attr]
                next_attempt, written = clean["next_attempt"], True
            except (ValueError, broker_module.BrokerRefused) as error:
                self.note(state_comment_failed=str(error)[:300])
            self._risk_issues()
            if status == "waiting_bots" and next_attempt and written:  # the gate trusts only a written comment
                tick = runner._tick_state(self.ctx)
                runner._save_tick_state(self.ctx, {**tick, "next_attempt": {"at": next_attempt, "pr": pr}})
        self.note(next_attempt=next_attempt)
        if status == "blocked":
            # A push the broker refused for a control path: the owner's local patch (design 5.3).
            current = self._trusted().branch_head(branch)
            control = [item for item in self._change_refusals(current, labels)
                       if item.reason == path_check.Reason.CONTROL_PATH]
            blockers = [SUBMIT_BLOCKED]
            if control and current is not None:
                self._export(current)
                blockers.append(CONTROL_PATH)
            # The session's own reason stays in the local record: only fixed phrases go public.
            reason = str((output or {}).get("reason") or "")[:300]
            self.block(blockers, unclaim=pr is None)
            raise Stopped({"status": "blocked", "reason": SUBMIT_BLOCKED if output is None else
                           f"{SUBMIT_BLOCKED}: {reason}"})
        self.note(status="completed")

    def _risk_issues(self) -> None:
        created, refused_count = [], 0
        for finding in workers.risk_candidates(self.run_dir):
            try:
                created += self.broker.create_risk_issues([finding])  # type: ignore[union-attr]
            except broker_module.BrokerRefused:
                refused_count += 1
        if created or refused_count:
            self.note(risk_issues=[{"number": item.get("number"), "finding": item.get("finding")} for item in created],
                      risk_issues_refused=refused_count)

    # --- resume -------------------------------------------------------------------------------------

    def resume(self, decision) -> None:
        pr, branch = int(decision.pr), str(decision.resume_branch or "")
        pull = (self.snapshot.get("pull_details") or {}).get(str(pr)) or \
            next((item for item in self.snapshot.get("pulls") or [] if item.get("number") == pr), {})
        issue = issue_of(pull)
        if issue is None or not repos._BRANCH.fullmatch(branch):
            raise Stopped({"status": "blocked", "reason": f"PR #{pr} names no autodev branch"})
        pr_head = (pull.get("head") or {}).get("sha")
        trusted = self._trusted()
        try:
            labels = candidate_data.issue_labels(self.client, issue)
            head = trusted.branch_head(branch)
            base = self.pinned
            if head is not None and trusted.merge_base(self.pinned, head) != self.pinned:
                base = trusted.merge_base(self.pinned, head)
        except (GitHubError, NetworkError, TokenUnavailable) as error:
            raise infra(f"network: {error}") from None
        except (repos.RepositoryError, ValueError) as error:
            raise infra(f"cli: trusted repository: {error}") from None
        self._open_broker("resume", base)
        if decision.actions:
            self._cleanup(decision.actions)
        self.note(issue=issue, branch=branch, pr=pr, base=base)
        try:
            self.broker.bind_issue(issue, labels)  # type: ignore[union-attr]
            self.broker.bind_resume(pr)  # type: ignore[union-attr]
        except broker_module.BrokerRefused as error:
            raise Stopped({"status": "blocked", "reason": f"resume: {error}"}) from None
        if self.state is None:
            self.write_state(stages.SUBMIT)
        if self.state is not None:
            self.state.issue, self.state.branch, self.state.pr, self.state.base, self.state.head = \
                issue, branch, pr, base, head
        if head is None or head != pr_head:
            self.block([FOREIGN_HEAD], unclaim=False)
            raise Stopped({"status": "blocked", "reason": FOREIGN_HEAD})
        self.resume_head = head
        self._session_setup()
        state, _ = trusted_state((self.snapshot.get("issue_comments") or {}).get(str(pr)) or [],
                                 records.trusted_comment_ids(self.ctx.directory))
        state = state or {}
        seen = [item for item in state.get("findings") or [] if isinstance(item, str)
                and stages._FINDING.fullmatch(item)]
        rounds = state.get("rounds") if isinstance(state.get("rounds"), int) and state["rounds"] >= 0 else 0
        state_head = state.get("head") if isinstance(state.get("head"), str) and \
            repos._SHA.fullmatch(state["head"]) else None
        try:
            criteria = candidate_data.owner_criteria(self.client, issue, limit=workers.MAX_CRITERIA,
                                                     max_chars=workers.MAX_CRITERION_CHARS)
        except (GitHubError, NetworkError, TokenUnavailable) as error:
            raise infra(f"network: {error}") from None
        layout = self._layout(stages.SUBMIT)
        self._open_service(issue, base, branch, Path(layout["exchange"]), criteria)
        self.submit(issue, branch, slice=None, description_extra={}, labels=labels,
                    resume={"pr": pr, "rounds": rounds, "findings_seen": seen, "state_comment_head": state_head},
                    state_comment=self._recorded_state_comment(pr))

    # --- the end ------------------------------------------------------------------------------------

    def _final_state(self) -> None:
        if self.state is None:
            return
        self.state.stage = "done"
        with contextlib.suppress(ValueError, OSError):
            run_state.write_state(self.run_dir, self.state, self._state_copy(), known=self.markers)

    def _close(self, wait: float = workers.CLOSE_WAIT) -> None:
        if self.service is not None:
            self.service.close(wait)
        for root in self.temp_roots:
            with contextlib.suppress(OSError, sandbox.SandboxError):
                sandbox.remove_temp_root(root, self.ctx.temp_base)
        if self.trusted is not None:
            shutil.rmtree(self._sessions_root() / self.run_id, ignore_errors=True)

    def _finish(self) -> None:
        if self.client is not None:
            self.fields["github_requests"] = list(self.client.requests)
        if not self.fields.get("status"):
            self.fields.update(runner.infra("cli: the run sequence ended without a status"))
        record = {**(records.read_record(self.run_id, self.ctx.directory) or {}), **self.fields,
                  "mode": self.mode, "run_id": self.run_id}
        report = run_report.build_report(record, run_state.read_state(self.run_dir), self.run_dir,
                                         known=self.markers)
        self.note(report=report, github_requests=self.fields.get("github_requests", []))
        if run_report.should_notify(record):
            title, message = run_report.notification(report, known=self.markers)
            sent = run_report.notify(title, message, runner=self.ctx.notify_runner, platform=self.ctx.platform)
            self.note(notification={"sent": sent, "title": title})


def run_sequence(ctx: runner.Context, config: Mapping, run_id: str, mode: str, *, pr: int | None = None,
                 now: datetime, trigger: Mapping | None = None) -> dict:
    """One run under the lease; the fields its record gets (see the module docstring)."""
    return Sequence(ctx, config, run_id, mode, pr=pr, now=now, trigger=trigger).run()
