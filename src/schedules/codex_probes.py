"""Codex probes and the fallback to the Claude worker (#344; design 5.6, 5.7, Q8, Q14 as amended).

Reference: docs/autodev-codex.md. `decide` is the one function a step that wants a Codex worker
calls (#345): it returns the Codex launch (argv, environment, profile; `start` is the only way to
start it), the Claude worker with a machine-readable reason (`REASONS`), or an abort of the step
(`ABORT_REASONS`). Its checks, in order: the platform, the ``codex`` executable, its version
(`codex_sandbox.VERIFIED_VERSIONS`, read with a fresh scratch ``CODEX_HOME`` and ``HOME``), the
hygiene of the run's own ``CODEX_HOME`` (an allowlist, `codex_sandbox.hygiene_problems`), the
login (the API key from the keychain item ``codex.login: keychain:<service>``; ``login_missing``
when it is unset or absent), the launch itself (`codex_sandbox.check_launch`), and **model-free
probes** that run under exactly the launch's profile prefix, environment and working directory
(only the command differs):

* a start control (``codex --version`` must run under the profile; else the profile is broken);
* the **nested-sandbox check**: ``codex sandbox -- /usr/bin/true`` under the outer profile. On
  this macOS (26.5.1) any inner ``sandbox_apply`` of a different profile fails with ``Operation
  not permitted`` (exit 71; the research of 2026-10-14), so every model command of ``codex exec
  -s read-only`` would fail, and the decision is the Claude worker (``nested_sandbox_unavailable``);
* **deny probes**: read the decoys (their output is scanned, a marker trips the schedule), list
  ``~/.ssh``, ``~/Library/Keychains``, ``~/.config/agents-autodev``, the home and the autodev
  state directory (output discarded), write into the decoy directory, the tree and materials (a
  worker) or ``.git/config``, ``.git/hooks``, the rename-and-edit sequence on ``.git`` and the
  environment (a stage), into every read root and into a runner-private directory outside the
  run, read the decoy keychain item through securityd (when it exists), a direct connection to an
  external address, a connection to a second runner-owned local port (its listener must count
  none), a ``launchctl submit`` (no canary, no job) and a CONNECT through the proxy to a host
  outside its allowlist (the proxy's log must show it refused);
* **control probes**: read the materials (a runner-written seed), list the tree, write the output
  directory and ``$TMPDIR``, reach the proxy port (the proxy counts the connection); a stage also
  writes the one-off repository and the exchange directory, runs ``<venv>/bin/python -c 0``
  (``venv_unusable`` when its interpreter cannot run under the profile, as a nix-built one cannot)
  and ``git --version`` by name (``git_unusable``; the launch's ``PATH`` holds only directories the
  profile lets the process execute from, `codex_sandbox.launch_path`).

Detection is the runner's: exit codes, files it checks itself and the proxy's log; a file's
content never enters a report, and every capture (bounded) is scanned. Any deny-probe success,
and any decoy read or leaked, **aborts the step** (`Decision.abort`, reason
``deny_probe_succeeded``, no client applied): the caller fails the step and runs neither Codex
nor the Claude worker for it, and `start` refuses. A failed control or nested check is a fallback
to the Claude worker (`REASONS`: ``probe_failed``, ``profile_broken`` and the others). The run
record keeps ``client_requested``, ``client_applied``, ``fallback_reason``, ``aborted`` and
``abort_reason`` (`record_decision`).

The API key is in the codex process's environment, and a model command can observe that
environment despite ``shell_environment_policy.inherit=core`` (the sysctl allowlist of the profile
does not stop it; a residual risk). A decision therefore carries the key as a known secret
(`Decision.known`): every probe capture is scanned for it, the run record is masked with it, and
the run's outgoing-text scan (the broker's ``known``) refuses any text that holds it.

**Codex as a stage client (Q14)**: `stage_availability` runs the same checks for the stage
launch (``-s workspace-write``), the installed-version check that ``workspace-write`` blocks the
network, and then, only when all of them pass, the #343 probe plan through ``codex exec`` with
exactly the stage argv and environment (a model turn: runner-written scripts, ``__probe__ <id>
rc=<code>`` lines read from the ``--json`` event stream; `exec_plan` lists the research groups,
among them ``observe.env``, which fails when a model command sees the API key or the proxy
variables). Codex stays unavailable with the reason
unless every probe is denied and every control passes; the result is stored in
``<state>/schedules/codex-availability.json`` for the run sequence and #348 with the installed checks, the
codex version and the launch's run-independent shape (`codex_sandbox.launch_shape`);
`read_availability` treats an entry as unavailable once the version or the shape differs or it is
older than `AVAILABILITY_MAX_AGE`. Reading it needs no key and no run: the run sequence (#347,
for a Codex select stage) and #348 pass ``version=installed_version(codex, path=...)`` and
``shape=stage_shape(codex, config, stage_kind)``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any

from src.daemon.state import private_dir, read_json, write_json

from . import codex_sandbox as cs
from . import decoys as decoy_store
from . import probes, records, sandbox
from .codex_proxy import ConnectProxy
from .github import SECURITY
from .lease import schedules_dir
from .scan import KNOWN_MIN_LENGTH, Hit, HitKind, KnownSecret, Scanner

ALLOWED, DENIED = probes.ALLOWED, probes.DENIED
CLAUDE, CODEX = "claude", "codex"
# Why a step that requested Codex uses the Claude worker instead (`Decision.reason`).
REASONS = ("platform_unsupported", "codex_missing", "codex_version_unverified", "codex_home_unclean",
           "login_missing", "profile_broken", "nested_sandbox_unavailable", "probe_failed", "venv_unusable",
           "git_unusable", "workspace_write_network_open", "stage_probe_failed")
# Why a step is aborted, with no client at all (`Decision.abort`): a deny probe got through, or a
# decoy was read or leaked (which also turns the schedule off). Never a fallback.
DENY_PROBE_SUCCEEDED = "deny_probe_succeeded"
ABORT_REASONS = (DENY_PROBE_SUCCEEDED,)
GIT = "/usr/bin/git"  # the C2 probe's git: the system's (xcrun), which the profile lets run
NESTED_EXIT = 71  # sandbox-exec: sandbox_apply: Operation not permitted (research, 2026-10-14)
COMMAND_TIMEOUT = 30
CAPTURE_LIMIT = 64 * 1024
EXEC_TIMEOUT = probes.PROBE_TIMEOUT
MAX_REPORT_AGE = probes.MAX_REPORT_AGE
AVAILABILITY_NAME = "codex-availability.json"
AVAILABILITY_MAX_AGE = 7 * 24 * 3600  # seconds a stored stage availability counts (the run sequence and #348 re-check)
API_KEY_LABEL = "codex api key"
MAX_WARNINGS = 8  # codex's own warning items a report keeps (each bounded to WARNING_LIMIT characters)
WARNING_LIMIT = 200
EXTERNAL_HOST = probes.EXTERNAL_HOST
CURL = "/usr/bin/curl"
NC = "/usr/bin/nc"
OPEN = "/usr/bin/open"
OSASCRIPT = "/usr/bin/osascript"
INCONCLUSIVE_CODES = probes.INCONCLUSIVE_CODES
# A probe's own exit code: the shell found a program the runner saw executable outside the sandbox
# but could not execute it (bash's 126), because the profile refuses its exec (osascript, or a gh
# outside the executable directories; verified under the generated profiles, 2026-10-14). Without
# the mapping 126 would be inconclusive, as it is for every other probe.
EXEC_REFUSED = 77
# The decoy keychain item lives in the owner's login keychain (`decoys.ensure`); the probe names
# that keychain explicitly, because the Codex process's HOME is the empty per-run home and its
# default search list may not hold it (under the profile a lookup by name answered "not found", 44,
# which alone shows nothing).
LOGIN_KEYCHAIN = Path("Library") / "Keychains" / "login.keychain-db"
# What a model command must not see in its environment: the API key and the proxy variables
# (``shell_environment_policy.inherit=core`` drops them; `observe.env.script` proves it).
HIDDEN_VARIABLES = (cs.API_KEY_VARIABLE, *cs.PROXY_VARIABLES)
_RAW_FAILING = (HitKind.KNOWN, HitKind.PATTERN, HitKind.LIMIT)
_SHELLS = {"bash", "zsh", "sh"}
_KEY = re.compile(r"[\x21-\x7e]+")  # an API key as the keychain item must hold it: printable, no spaces
Execute = Callable[..., probes.Captured]


# --- running commands -------------------------------------------------------------------------------

def execute(argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, timeout: float = COMMAND_TIMEOUT,
            capture: bool = False, text: str = "", max_capture: int = CAPTURE_LIMIT) -> probes.Captured:
    """Run ``argv`` with ``env`` in ``cwd``; its whole process group is killed afterwards. Without
    ``capture`` its output goes to ``/dev/null`` (a probe of a real secret keeps its exit code only);
    with it, stdout up to ``max_capture`` and stderr up to 64 KiB are kept for the scan."""
    if capture:
        return probes.run_session(SimpleNamespace(argv=list(argv), cwd=cwd, env=dict(env)), text, timeout=timeout,
                                  max_capture=max_capture)
    process = subprocess.Popen(list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(process.pid)
        process.wait()
    _kill(process.pid)
    return probes.Captured("", "", process.returncode, timed_out, False)


def _kill(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# --- reports ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    """One model-free probe: ``command`` runs under the launch's profile prefix."""

    id: str
    expect: str
    command: tuple[str, ...]
    capture: bool = False  # stdout is kept (bounded) and scanned: decoys and runner-written seeds only
    decoy: str | None = None
    absent: tuple[Path, ...] = ()
    unchanged: tuple[Path, ...] = ()
    content: tuple[tuple[Path, str], ...] = ()
    contains: str | None = None
    proxy_denied: str | None = None  # a denial counts only when the proxy log shows this target refused
    proxy_reached: bool = False  # an allowed probe must raise the proxy's connection count
    codes: frozenset[int] = frozenset()  # when set, the only exit codes that count as denied
    bad_codes: frozenset[int] = frozenset()  # exit codes that make a denial inconclusive
    reason: str = "probe_failed"  # the fallback reason when an allowed probe fails


@dataclass
class CodexReport:
    kind: str
    digest: str = ""
    ok: bool = False
    reason: str | None = None
    failures: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    nested: dict = field(default_factory=dict)
    installed: dict = field(default_factory=dict)
    observed: dict = field(default_factory=dict)
    hits: list[dict] = field(default_factory=list)
    exec_session: dict = field(default_factory=dict)
    tripped: bool = False
    started: str = ""
    finished: str = ""
    seconds: float = 0.0
    finished_at: float = 0.0

    def fail(self, reason: str, message: str) -> None:
        """Record a failure; the most damning reason wins (`REASONS` order is not the rank)."""
        self.failures.append(message)
        if self.reason is None or _RANK.index(reason) < _RANK.index(self.reason):
            self.reason = reason

    def to_record(self) -> dict:
        return {"kind": self.kind, "digest": self.digest, "ok": self.ok, "reason": self.reason,
                "failures": self.failures[:probes.MAX_FAILURES], "checks": self.checks, "nested": self.nested,
                "installed": self.installed, "observed": self.observed, "hits": self.hits,
                "exec_session": self.exec_session, "tripped": self.tripped, "started": self.started,
                "finished": self.finished, "seconds": round(self.seconds, 1)}


# A deny success outranks everything; a profile that does not even start codex explains every
# other failure; the rest follow the order the checks run in.
_RANK = ("deny_probe_succeeded", "platform_unsupported", "codex_missing", "codex_version_unverified",
         "codex_home_unclean", "login_missing", "profile_broken", "nested_sandbox_unavailable", "venv_unusable",
         "git_unusable", "workspace_write_network_open", "probe_failed", "stage_probe_failed")
# Controls of the stage probe session whose failure has its own reason (else stage_probe_failed).
_EXEC_REASONS = {"control.git.script": "git_unusable"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finish(report: CodexReport, started: float) -> CodexReport:
    report.ok = not report.failures
    if report.ok:
        report.reason = None
    report.seconds = time.monotonic() - started
    report.finished_at = time.time()
    report.finished = _now()
    return report


# --- targets and the plan ------------------------------------------------------------------------------

@dataclass(frozen=True)
class CodexTargets:
    """Places a Codex process must not reach. ``state_dir`` is the autodev state (records, the decoy
    registry, the daemon's token; the run's output directory lies inside it and is the only part
    re-allowed),
    ``secrets_dir`` the broker's ``~/.config/agents-autodev``, ``owner_temp`` the owner's temporary
    directory (an ephemeral decoy goes there). ``runner`` names the stage probes' write targets and
    keychain items (#343's `probes.Targets`)."""

    state_dir: Path
    secrets_dir: Path
    owner_temp: Path = field(default_factory=sandbox.owner_temp_dir)
    external_host: str = EXTERNAL_HOST
    runner: probes.Targets | None = None


@dataclass
class ProbeRun:
    """What one probe round planted and must clean up."""

    nonce: str
    persistent: list[decoy_store.Decoy]
    ephemeral: list[decoy_store.Decoy] = field(default_factory=list)
    private_dir: Path | None = None
    external_address: str | None = None
    created: list[Path] = field(default_factory=list)
    snapshots: dict[Path, tuple | None] = field(default_factory=dict)
    listener: probes.Listener | None = None  # a runner-owned TCP listener the process must not reach
    unix_socket: Path | None = None  # a runner-owned Unix socket in the private directory (C8)
    unix_listener: probes.Listener | None = None
    launch_label: str = ""  # the launchd job label a probe tries to submit (C9)
    gh: str | None = None  # the gh executable the runner finds on the launch's PATH (C4)
    keychain: dict[str, bool] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)

    @property
    def file_decoys(self) -> list[decoy_store.Decoy]:
        return [decoy for decoy in self.persistent + self.ephemeral if decoy.path is not None]

    @property
    def keychain_decoy(self) -> decoy_store.Decoy | None:
        """The decoy keychain item, when it exists outside the sandbox (``keychain["decoy"]``)."""
        item = next((decoy for decoy in self.persistent if decoy.kind == "keychain"), None)
        return item if item is not None and item.service and item.account and self.keychain.get("decoy") else None

    def decoy_dir(self) -> Path | None:
        return next((decoy.path.parent for decoy in self.persistent if decoy.path is not None
                     and decoy.label in decoy_store.DIRECTORY_LABELS), None)


def resolve_ipv4(host: str) -> str | None:
    """An IPv4 address of ``host`` as the runner (outside the sandbox) resolves it, or None."""
    try:
        found = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None
    return found[0][4][0] if found else None


def _present(path: Path) -> str:
    return probes.target_type(path)


def model_free_plan(launch: cs.CodexLaunch, run: ProbeRun, targets: CodexTargets, proxy: ConnectProxy) -> list[Probe]:
    """The model-free probes of ``launch`` (worker or stage); the nested check is separate."""
    paths, nonce = launch.paths, run.nonce
    home, codex = cs._real(paths.home), launch.executable[0]
    out = cs._real(paths.out_dir)
    plan = [Probe("control.codex-start", ALLOWED, (codex, "--version"), capture=True, contains="codex-cli ",
                  reason="profile_broken"),
            Probe("control.proxy-port", ALLOWED, (NC, "-z", "-w", "3", "127.0.0.1", str(launch.proxy_port)),
                  proxy_reached=True),
            Probe("control.write-output", ALLOWED,
                  ("/bin/sh", "-c", f"printf %s {nonce} > {probes._q(out / f'probe-{nonce}-out')}"),
                  content=((out / f"probe-{nonce}-out", nonce),)),
            Probe("control.write-tmpdir", ALLOWED,
                  ("/bin/sh", "-c", f'printf %s {nonce} > "$TMPDIR/probe-{nonce}-tmp"'),
                  content=((cs._real(paths.tmpdir) / f"probe-{nonce}-tmp", nonce),))]
    if paths.kind == cs.WORKER:
        seed = cs._real(paths.materials) / f"probe-{nonce}-seed.txt"  # type: ignore[arg-type]
        plan += [Probe("control.read-materials", ALLOWED, ("/bin/cat", str(seed)), capture=True, contains=nonce),
                 Probe("control.list-tree", ALLOWED, ("/bin/ls", str(cs._real(paths.tree))))]  # type: ignore[arg-type]
    else:
        repo, exchange = cs._real(paths.repository), cs._real(paths.exchange)  # type: ignore[arg-type]
        seed = exchange / f"probe-{nonce}-seed.txt"
        plan += [Probe("control.read-exchange", ALLOWED, ("/bin/cat", str(seed)), capture=True, contains=nonce),
                 Probe("control.write-repository", ALLOWED,
                       ("/bin/sh", "-c", f"printf %s {nonce} > {probes._q(repo / f'.autodev-probe-{nonce}')}"),
                       content=((repo / f".autodev-probe-{nonce}", nonce),)),
                 Probe("control.write-exchange", ALLOWED,
                       ("/bin/sh", "-c", f"printf %s {nonce} > {probes._q(exchange / f'probe-{nonce}-write')}"),
                       content=((exchange / f"probe-{nonce}-write", nonce),)),
                 # The stage's environment must run under the profile (a nix-built one execs another
                 # store path and cannot); without it a stage could not run the repository's tests.
                 Probe("control.venv-python", ALLOWED, (str(Path(paths.venv) / "bin" / "python"), "-c", "0"),  # type: ignore[arg-type]
                       reason="venv_unusable"),
                 # git by name, as a stage's commands and codex itself call it: the launch's PATH must
                 # lead to a git the profile lets run (`codex_sandbox.launch_path`).
                 Probe("control.git", ALLOWED, ("/bin/sh", "-c", "git --version"), capture=True,
                       contains="git version", reason="git_unusable")]
    # Reads: decoys with their content (scanned), real places by exit code only.
    plan += [Probe(f"read.{decoy.label}", DENIED, ("/bin/cat", str(decoy.path)), capture=True, decoy=decoy.label)
             for decoy in run.file_decoys]
    places = [("ssh-dir", home / ".ssh"), ("keychains-dir", home / "Library" / "Keychains"),
              ("secrets-dir", Path(targets.secrets_dir)), ("home", home), ("state-dir", Path(targets.state_dir))]
    for name, place in places:
        kind = run.types.setdefault(name, _present(place))
        if kind == probes.DIRECTORY:
            plan.append(Probe(f"list.{name}", DENIED, ("/bin/ls", str(place))))
        elif kind == probes.FILE:
            plan.append(Probe(f"read.{name}", DENIED, ("/bin/cat", str(place))))
    # Writes outside the writable directories: a new file must stay absent.
    outside = [("decoy-dir", run.decoy_dir()), ("private-dir", run.private_dir)]
    if paths.kind == cs.WORKER:
        outside += [("tree", cs._real(paths.tree)), ("materials", cs._real(paths.materials))]  # type: ignore[arg-type]
    for name, directory in outside:
        if directory is not None:
            target = Path(directory) / f"probe-{nonce}-{name}"
            plan.append(Probe(f"write.{name}", DENIED, ("/bin/sh", "-c", f"printf x > {probes._q(target)}"),
                              absent=(target,)))
    # The read-only roots (a stage's state copy) and a stage's environment, which it may read and
    # execute: a new file must stay absent (a regular file gets a zero-byte append).
    for name, place in _read_only_roots(paths):
        kind = run.types.setdefault(f"write-{name}", probes.target_type(place))
        if kind == probes.DIRECTORY:
            target = place / f"probe-{nonce}-{name}"
            plan.append(Probe(f"write.{name}", DENIED, ("/bin/sh", "-c", f"printf x > {probes._q(target)}"),
                              absent=(target,)))
        elif kind == probes.FILE:
            plan.append(Probe(f"write.{name}", DENIED, ("/bin/sh", "-c", f": >> {probes._q(place)}"),
                              unchanged=(place,)))
    if paths.kind == cs.STAGE:
        git = cs._real(paths.repository) / ".git"  # type: ignore[arg-type]
        if probes.target_type(git / "config") == probes.FILE:
            plan.append(Probe("write.repo-git-config", DENIED, ("/bin/sh", "-c", f": >> {probes._q(git / 'config')}"),
                              unchanged=(git / "config",)))
        if probes.target_type(git / "hooks") == probes.DIRECTORY:
            hook = git / "hooks" / f"probe-{nonce}"
            plan.append(Probe("write.repo-git-hooks", DENIED, ("/bin/sh", "-c", f"printf x > {probes._q(hook)}"),
                              absent=(hook,)))
        if probes.target_type(git / "config") == probes.FILE and probes.target_type(git / "hooks") == probes.DIRECTORY:
            plan.append(_rename_probe(git, nonce))
    # The keychain through securityd (C3): the decoy item only (never a real one), its value captured
    # and scanned; "not found" (44) proves nothing and is inconclusive, as in #343.
    if (item := run.keychain_decoy) is not None:
        plan.append(Probe("keychain.decoy", DENIED, (SECURITY, "find-generic-password", "-s", item.service, "-a",
                                                     item.account, "-w", str(home / LOGIN_KEYCHAIN)),
                          capture=True, decoy=item.label, bad_codes=frozenset({probes.NOT_FOUND})))
    # The network: only the proxy port is reachable (not another local port), and the proxy refuses
    # other hosts.
    if run.external_address:
        plan.append(Probe("network.direct", DENIED, (NC, "-z", "-G", "3", "-w", "3", run.external_address, "443")))
    plan.append(Probe("network.proxy-refused", DENIED,
                      (CURL, "-sS", "-o", "/dev/null", "--max-time", "5", "-x", cs.proxy_url(launch.proxy_port),
                       f"https://{targets.external_host}/"), proxy_denied=f"{targets.external_host}:443"))
    if run.listener is not None:
        plan.append(Probe("network.local-listener", DENIED, (NC, "-z", "-w", "2", "127.0.0.1", str(run.listener.port))))
    # launchd (C9): a job would run outside the sandbox; its canary must stay absent, and the runner
    # asks launchd for the label afterwards (`_check_launchd`).
    if run.launch_label and run.private_dir is not None:
        canary = Path(run.private_dir) / f"probe-{nonce}-launchd"
        plan.append(Probe("launchd.submit", DENIED, (probes.LAUNCHCTL, "submit", "-l", run.launch_label, "--",
                                                     "/usr/bin/touch", str(canary)), absent=(canary,)))
    ids = [probe.id for probe in plan]
    if len(set(ids)) != len(ids):
        raise ValueError("two Codex probes with the same ID")
    return plan


def _renamed_git(git: Path, nonce: str) -> Path:
    return git.parent / f".git-probe-{nonce}"


def _rename_probe(git: Path, nonce: str) -> Probe:
    """Seatbelt checks a rename against its source and destination only, so a stage that may rename
    ``.git`` can edit its config and hooks under another name and rename it back; the profile denies
    the ``.git`` entry itself. The sequence must leave the config and the hooks unchanged."""
    moved = _renamed_git(git, nonce)
    hook = git / "hooks" / f"probe-{nonce}-rename"
    q = probes._q
    body = (f"mv {q(git)} {q(moved)} && printf '[core]\\n\\thooksPath = /nonexistent-{nonce}\\n' >> {q(moved / 'config')}"
            f" && printf x > {q(moved / 'hooks' / hook.name)}; mv {q(moved)} {q(git)}")
    return Probe("write.repo-git-rename", DENIED, ("/bin/sh", "-c", body), absent=(hook, moved),
                 unchanged=(git / "config", git / "hooks"))


def _read_only_roots(paths: cs.CodexPaths) -> list[tuple[str, Path]]:
    """The read-only roots a write probe targets: ``read-root-<n>`` (`CodexPaths.read_roots`) and,
    for a stage, ``venv`` (its environment, readable and executable, never writable)."""
    found = [(f"read-root-{index}", cs._real(root)) for index, root in enumerate(paths.read_roots)]
    if paths.kind == cs.STAGE and paths.venv is not None:
        found.append(("venv", cs._real(paths.venv)))
    return found


def _exec_refused(command: str) -> str:
    """``command`` (a shell command line) with bash's 126 ("cannot execute") turned into
    `EXEC_REFUSED`: for a program the runner found executable, 126 means the profile refused it."""
    return f"( {command}; rc=$?; [ $rc -eq 126 ] && exit {EXEC_REFUSED}; exit $rc )"


def _check_launchd(label: str, check_id: str, reason: str, report: CodexReport,
                   runner: Callable[..., subprocess.CompletedProcess]) -> None:
    """The probe's launchd job must not exist (asked outside the sandbox, output discarded); a job
    that does is removed and fails the report (`deny_probe_succeeded`)."""
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                 timeout=COMMAND_TIMEOUT, check=False)
    try:
        listed = runner([probes.LAUNCHCTL, "list", label], **quiet)
    except (OSError, subprocess.SubprocessError) as error:
        report.fail(reason, f"{check_id}: launchctl list failed: {type(error).__name__}")
        return
    if listed.returncode == 0:
        report.fail("deny_probe_succeeded", f"{check_id}: the probe's launchd job exists")
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            runner([probes.LAUNCHCTL, "remove", label], **quiet)


def _inventory_keychain_decoy(run: ProbeRun, runner: Callable[..., subprocess.CompletedProcess]) -> None:
    """Whether the decoy keychain item exists, asked outside the sandbox with its output discarded
    (never its value): only an existing item proves anything."""
    item = next((decoy for decoy in run.persistent if decoy.kind == "keychain"), None)
    if item is not None and item.service and item.account:
        run.keychain["decoy"] = probes._exists(runner, [SECURITY, "find-generic-password", "-s", item.service,
                                                        "-a", item.account])


def _prepare(launch: cs.CodexLaunch, run: ProbeRun, targets: CodexTargets, socket_root: Path | None) -> None:
    """Plant the seed, the ephemeral decoy and the private directory; snapshot watched files."""
    paths = launch.paths
    run.ephemeral.append(decoy_store.plant(Path(targets.owner_temp), "owner-temp-decoy"))
    run.private_dir = Path(tempfile.mkdtemp(prefix="autodev-codex-probe-", dir=socket_root))
    seed_dir = cs._real(paths.materials if paths.kind == cs.WORKER else paths.exchange)  # type: ignore[arg-type]
    seed = seed_dir / f"probe-{run.nonce}-seed.txt"
    probes._write_new(seed, run.nonce + "\n")
    run.created.append(seed)
    for decoy in run.file_decoys:
        run.snapshots[decoy.path] = probes._snapshot(decoy.path)  # type: ignore[index]


def _cleanup(launch: cs.CodexLaunch, run: ProbeRun) -> None:
    paths = launch.paths
    directories = [paths.out_dir, paths.tmp_root, paths.tmpdir, paths.materials, paths.exchange, paths.repository,
                   paths.tree, *(root for _, root in _read_only_roots(paths))]  # the last ones: a probe that got through
    for directory in directories:
        if directory is None:
            continue
        for pattern in (f"probe-{run.nonce}*", f".autodev-probe-{run.nonce}*"):
            for path in Path(directory).glob(pattern):
                with contextlib.suppress(OSError):
                    probes._remove(path)
    if paths.repository is not None:
        git = Path(paths.repository) / ".git"
        moved = _renamed_git(git, run.nonce)
        with contextlib.suppress(OSError):
            if os.path.isdir(moved) and not os.path.lexists(git):  # a rename probe that got through
                os.rename(moved, git)
        for hook in (git / "hooks" / f"probe-{run.nonce}", git / "hooks" / f"probe-{run.nonce}-rename"):
            with contextlib.suppress(OSError):
                if os.path.lexists(hook):
                    probes._remove(hook)
    if (decoy_dir := run.decoy_dir()) is not None:  # what a probe that got through left in the decoy directory
        for path in decoy_dir.glob(f"probe-{run.nonce}*"):
            with contextlib.suppress(OSError):
                probes._remove(path)
    for path in run.created:
        with contextlib.suppress(OSError):
            if os.path.lexists(path):
                probes._remove(path)
    for decoy in run.ephemeral:
        with contextlib.suppress(OSError, decoy_store.DecoyError):
            decoy_store.discard(decoy)
    for listener in (run.listener, run.unix_listener):
        if listener is not None:
            listener.close()
    if run.private_dir is not None:
        shutil.rmtree(run.private_dir, ignore_errors=True)


def _note_leaked(report: CodexReport, leaked: set[str], directory: str | Path | None, run_id: str | None,
                 what: str) -> None:
    """A decoy read or leaked during ``what``: trip the schedule and count it as a deny-probe
    success (the step aborts, `Decision.abort`)."""
    if not leaked:
        return
    labels = ", ".join(sorted(leaked))
    report.tripped = True
    report.fail(DENY_PROBE_SUCCEEDED, f"a decoy was read or leaked during {what}: {labels}")
    decoy_store.trip(directory, f"decoy read or leaked during {what}: {labels}", run_id=run_id)


def _scan(scanner: Scanner, text: str) -> tuple[list[Hit], set[str]]:
    hits = [hit for hit in scanner.scan(text) if hit.kind in _RAW_FAILING]
    return hits, {hit.name.removeprefix(decoy_store.LABEL_PREFIX) for hit in decoy_store.decoy_hits(hits)}


def _judge(probe: Probe, captured: probes.Captured, run: ProbeRun, proxy: ConnectProxy, accepted_before: int) -> str:
    code = captured.exit_code
    if probe.expect == ALLOWED:
        ok = (not captured.timed_out and code == 0
              and (probe.contains is None or probe.contains in captured.stdout)
              and all(probes._read_exact(path) == text for path, text in probe.content)
              and (not probe.proxy_reached or _wait_for(lambda: proxy.accepted > accepted_before)))
        return "allowed" if ok else "failed"
    if any(os.path.lexists(path) for path in probe.absent):
        return "allowed"
    if any(probes._snapshot(path) != run.snapshots.get(path) for path in probe.unchanged):
        return "allowed"
    if captured.timed_out or code is None:
        return "inconclusive"
    if code == 0:
        return "allowed"
    if code in INCONCLUSIVE_CODES or code in probe.bad_codes or (probe.codes and code not in probe.codes):
        return "inconclusive"
    if probe.proxy_denied and probe.proxy_denied not in proxy.targets(allowed=False):
        return "inconclusive"
    return "denied"


def nested_check(launch: cs.CodexLaunch, execute_: Execute, scanner: Scanner) -> dict:
    """``codex sandbox -- /usr/bin/true`` under the outer profile: ``available`` only on exit 0."""
    captured = execute_(launch.wrap([launch.executable[0], "sandbox", "--", "/usr/bin/true"]), env=launch.env,
                        cwd=launch.cwd, timeout=COMMAND_TIMEOUT, capture=True)
    hits, _ = _scan(scanner, captured.stdout + "\n" + captured.stderr)
    if captured.exit_code == 0 and not captured.timed_out and not hits:
        return {"available": True, "exit_code": 0, "detail": "the inner sandbox applied"}
    if "sandbox_apply" in captured.stderr and "Operation not permitted" in captured.stderr:
        detail = "sandbox-exec: sandbox_apply: Operation not permitted (an inner Seatbelt profile cannot apply)"
    elif captured.timed_out:
        detail = "timed out"
    else:
        detail = f"exit code {captured.exit_code}"
    return {"available": False, "exit_code": captured.exit_code, "detail": detail}


def run_model_free(launch: cs.CodexLaunch, report: CodexReport, *, proxy: ConnectProxy, targets: CodexTargets,
                   persistent: Sequence[decoy_store.Decoy], known: Iterable[KnownSecret] = (),
                   execute_: Execute = execute, resolver: Callable[[str], str | None] = resolve_ipv4,
                   socket_root: Path | None = None,
                   command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> set[str]:
    """Run the nested check and the model-free probes of ``launch`` into ``report``; return the
    labels of decoys that were read or leaked (the caller trips the schedule)."""
    run = ProbeRun(secrets.token_hex(8), list(persistent))
    leaked: set[str] = set()
    try:
        _prepare(launch, run, targets, socket_root)
        run.listener = probes.Listener.tcp()
        if os.path.exists(probes.LAUNCHCTL):
            run.launch_label = probes.LAUNCH_LABEL_PREFIX + run.nonce
        _inventory_keychain_decoy(run, command_runner)
        report.observed["keychain"] = ("the decoy item" if run.keychain_decoy is not None else
                                       "untested: no decoy keychain item (sandbox.decoy_keychain)")
        run.external_address = resolver(targets.external_host)
        if not run.external_address:
            report.fail("probe_failed", f"network.direct untested: the runner itself cannot resolve "
                                        f"{targets.external_host} (offline?)")
        scanner = Scanner([*decoy_store.known(run.persistent + run.ephemeral), *known])
        plan = model_free_plan(launch, run, targets, proxy)
        for probe in plan:
            for watched in probe.unchanged:
                run.snapshots.setdefault(watched, probes._snapshot(watched))
        report.observed["target_types"] = {name: kind for name, kind in sorted(run.types.items())
                                           if kind != probes.DIRECTORY}
        for probe in plan:
            before = proxy.accepted
            captured = execute_(launch.wrap(probe.command), env=launch.env, cwd=launch.cwd, timeout=COMMAND_TIMEOUT,
                                capture=probe.capture)
            outcome = _judge(probe, captured, run, proxy, before)
            hits, labels = _scan(scanner, captured.stdout + "\n" + captured.stderr) if probe.capture else ([], set())
            if hits:
                report.hits += [{"probe": probe.id, "kind": str(hit.kind), "name": hit.name} for hit in hits]
                outcome = "allowed" if probe.expect == DENIED else "failed"
                leaked |= labels
            if probe.decoy is not None and outcome == "allowed":
                leaked.add(probe.decoy)
            ok = outcome == ("allowed" if probe.expect == ALLOWED else "denied")
            report.checks.append({"id": probe.id, "expect": probe.expect, "outcome": outcome, "ok": ok,
                                  "exit_code": captured.exit_code})
            if not ok:
                if probe.expect == DENIED and outcome == "allowed":
                    report.fail("deny_probe_succeeded", f"{probe.id}: expected denied, got allowed")
                else:
                    report.fail(probe.reason, f"{probe.id}: expected {probe.expect}, got {outcome}")
        if run.listener.accepted:
            report.fail("deny_probe_succeeded", f"network.local-listener: the runner's listener accepted "
                                                f"{run.listener.accepted} connection(s)")
        if run.launch_label:
            _check_launchd(run.launch_label, "launchd.submit", "probe_failed", report, command_runner)
        if any(entry["id"] == "control.codex-start" and entry["ok"] for entry in report.checks):
            report.nested = nested_check(launch, execute_, scanner)
            if not report.nested["available"]:
                report.fail("nested_sandbox_unavailable", "nested sandbox: " + report.nested["detail"])
        report.observed["proxy"] = {"connections": proxy.accepted, "refused": proxy.targets(allowed=False)[:20],
                                    "tunnelled": proxy.targets(allowed=True)[:20]}
    except (OSError, ValueError, decoy_store.DecoyError) as error:
        report.fail("probe_failed", f"the probes could not run: {type(error).__name__}: {error}")
    finally:
        _cleanup(launch, run)
    return leaked


# --- installed-version checks ------------------------------------------------------------------------

@contextlib.contextmanager
def _scratch(base: Path | None = None):
    """A fresh private directory with an empty ``CODEX_HOME`` and ``HOME``, removed afterwards."""
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="autodev-codex-check-", dir=base)))
    try:
        for name in ("codex-home", "home", "cwd", "outside"):
            os.mkdir(root / name, 0o700)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _scratch_environment(root: Path, path: str) -> dict[str, str]:
    """The environment of a model-free codex call in a `_scratch` directory: its own empty
    ``CODEX_HOME`` and ``HOME`` (never the run's or the owner's) and ``TMPDIR`` beside them."""
    return {"PATH": path, "HOME": str(root / "home"), "LANG": sandbox.DEFAULT_LANG,
            "CODEX_HOME": str(root / "codex-home"), "TMPDIR": str(root / "cwd")}


def installed_checks(codex: str, *, path: str, execute_: Execute = execute, base: Path | None = None) -> dict:
    """What the installed codex does, model-free and unnested, with a fresh ``CODEX_HOME``, a
    runner-written file and a local listener only: its version, whether ``-s read-only`` (the
    ``codex sandbox`` default) restricts reads outside the working directory (the research: it does
    not), and whether ``workspace-write`` blocks a local TCP connection (the research: it does)."""
    found: dict[str, Any] = {"verified_versions": list(cs.VERIFIED_VERSIONS), "efforts": list(cs.EFFORTS)}
    with _scratch(base) as root:
        env = _scratch_environment(root, path)
        version = execute_([codex, "--version"], env=env, cwd=root / "cwd", capture=True)
        match = cs.VERSION_LINE.search(version.stdout) if version.exit_code == 0 else None
        found["version"] = match[1] if match else None
        # Controls: codex's own sandbox runs a command here (unnested), and nc reaches the listener
        # without it; without both, a refused read or connection would prove nothing (None).
        runs = execute_([codex, "sandbox", "--", "/bin/echo", "ok"], env=env, cwd=root / "cwd", capture=True)
        found["sandbox_runs"] = runs.exit_code == 0 and runs.stdout.strip() == "ok"
        marker = secrets.token_hex(8)
        (root / "outside" / "marker.txt").write_text(marker + "\n")
        read = execute_([codex, "sandbox", "--", "/bin/cat", str(root / "outside" / "marker.txt")], env=env,
                        cwd=root / "cwd", capture=True)
        found["read_only_restricts_reads"] = (None if read.timed_out or not found["sandbox_runs"]
                                              else not (read.exit_code == 0 and marker in read.stdout))
        listener = probes.Listener.tcp()
        try:
            direct = execute_([NC, "-z", "-w", "2", "127.0.0.1", str(listener.port)], env=env, cwd=root / "cwd")
            reached = _wait_for(lambda: listener.accepted > 0)
            net = execute_([codex, "sandbox", "-c", 'sandbox_mode="workspace-write"', "--", NC, "-z", "-w", "2",
                            "127.0.0.1", str(listener.port)], env=env, cwd=root / "cwd", capture=True)
            before = listener.accepted
            _wait_for(lambda: listener.accepted > before, 0.5)
            usable = found["sandbox_runs"] and direct.exit_code == 0 and reached
            found["workspace_write_blocks_network"] = (None if net.timed_out or net.exit_code is None or not usable
                                                       else net.exit_code != 0 and listener.accepted == before)
        finally:
            listener.close()
    return found


def complete_installed(report: CodexReport, codex: str | None, *, path: str, platform: str,
                       execute_: Execute = execute, base: Path | None = None) -> None:
    """Add `installed_checks` to ``report`` when they are missing and can run (macOS, a codex whose
    version was read): a fallback before the launch skips them otherwise."""
    if ("workspace_write_blocks_network" in report.installed or platform != "darwin" or not codex
            or not report.installed.get("version")):
        return
    report.installed.update(installed_checks(os.path.realpath(codex), path=path, execute_=execute_, base=base))


def _wait_for(condition: Callable[[], bool], seconds: float = 2.0) -> bool:
    """Whether ``condition`` holds within ``seconds`` (a listener thread counts a connection late)."""
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


# --- preflight ---------------------------------------------------------------------------------------

@dataclass
class Preflight:
    reason: str | None = None
    detail: str = ""
    api_key: str | None = field(default=None, repr=False)
    version: str | None = None


def installed_version(codex: str, *, path: str,
                      command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                      base: Path | None = None) -> str | None:
    """The ``X.Y.Z`` of ``codex --version`` run with a fresh scratch ``CODEX_HOME`` and ``HOME``
    (`_scratch`, removed afterwards; codex reads ``$CODEX_HOME/.env`` first), or None. No model
    turn and no key: the run sequence and #348 pass it to `read_availability`."""
    with _scratch(base) as root:
        return cs.installed_version(codex, env=_scratch_environment(root, path), runner=command_runner)


def preflight(codex: str | None, codex_home: Path, *, mode: str | None, path: str, platform: str,
              command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
              base: Path | None = None) -> Preflight:
    """The checks before a launch is built: platform, executable, version (``codex --version`` with
    a fresh scratch ``CODEX_HOME`` and ``HOME``, never the run's or the owner's: codex reads
    ``$CODEX_HOME/.env`` before anything else), the hygiene of the run's ``CODEX_HOME`` and the
    login: the API key from the keychain item of ``codex.login`` (``login_missing`` when the
    setting is unset or the item is absent)."""
    if platform != "darwin":
        return Preflight("platform_unsupported", f"the Codex sandbox is macOS-only (platform {platform})")
    if not codex or not os.path.isabs(codex) or not os.access(codex, os.X_OK):
        return Preflight("codex_missing", "the codex executable was not found (tools.json, PATH)")
    version = installed_version(codex, path=path, command_runner=command_runner, base=base)
    if version not in cs.VERIFIED_VERSIONS:
        return Preflight("codex_version_unverified",
                         f"codex {version or 'of unknown version'} is not one of {', '.join(cs.VERIFIED_VERSIONS)}",
                         version=version)
    if problems := cs.hygiene_problems(codex_home):
        return Preflight("codex_home_unclean", "; ".join(problems), version=version)
    if mode is None:
        return Preflight("login_missing", "codex.login is not set: store an API key of a separate OpenAI project "
                                          "with a spending limit in a keychain item and set codex.login to "
                                          "keychain:<service>", version=version)
    try:
        key = sandbox.keychain_secret(mode.removeprefix(cs.KEYCHAIN_PREFIX), runner=command_runner)
    except sandbox.SandboxError as error:
        return Preflight("login_missing", str(error), version=version)
    if len(key) < KNOWN_MIN_LENGTH or not _KEY.fullmatch(key):
        return Preflight("login_missing", f"the keychain item {mode.removeprefix(cs.KEYCHAIN_PREFIX)} does not hold "
                                          f"an API key (printable, at least {KNOWN_MIN_LENGTH} characters)",
                         version=version)
    return Preflight(api_key=key, version=version)


# --- the decision ------------------------------------------------------------------------------------

@dataclass
class Decision:
    """One of three outcomes for a step that requested Codex, with the reason the run record keeps:

    * Codex (``codex``): ``client_applied`` is ``codex`` and ``launch`` is set (`start`);
    * the Claude worker (``fallback``): ``client_applied`` is ``claude`` and ``reason`` is one of
      `REASONS`;
    * **abort** (``abort``): ``client_applied`` is None and ``reason`` one of `ABORT_REASONS`: a
      deny probe got through or a decoy was read. The caller fails the step; it never starts Codex
      (`start` refuses) or the Claude worker for it.

    ``known`` holds the API key as a known secret once it was read (in every outcome): the caller
    adds it to the run's outgoing-text scan and masks the run record with it (`known_values`)."""

    client_requested: str
    client_applied: str | None
    reason: str | None = None
    detail: str = ""
    launch: cs.CodexLaunch | None = None
    report: CodexReport | None = None
    known: tuple[KnownSecret, ...] = field(default=(), repr=False)
    abort: bool = False

    def __post_init__(self) -> None:
        if self.abort != (self.client_applied is None) or (self.abort and self.reason not in ABORT_REASONS):
            raise ValueError("an aborted decision has no client and an abort reason; every other one has a client")

    @property
    def codex(self) -> bool:
        return not self.abort and self.client_applied == CODEX

    @property
    def fallback(self) -> bool:
        """The step runs the Claude worker instead (``reason`` says why)."""
        return not self.abort and self.client_applied == CLAUDE

    def known_values(self) -> list[str]:
        return [secret.value for secret in self.known if isinstance(secret.value, str)]

    def to_record(self) -> dict:
        found = {"client_requested": self.client_requested, "client_applied": self.client_applied,
                 "fallback_reason": None if self.abort else self.reason, "aborted": self.abort,
                 "abort_reason": self.reason if self.abort else None, "detail": self.detail}
        if self.launch is not None:
            found["codex_launch"] = cs.describe(self.launch)
        if self.report is not None:
            found["codex_probes"] = self.report.to_record()
        return found


def _fallback(reason: str, detail: str, report: CodexReport | None = None,
              known: Sequence[KnownSecret] = ()) -> Decision:
    if reason not in REASONS:
        raise ValueError(f"not a fallback reason: {reason}")
    return Decision(CODEX, CLAUDE, reason, detail, None, report, tuple(known))


def _conclude(report: CodexReport, default: str, known: Sequence[KnownSecret]) -> Decision:
    """The decision for a finished report that failed: an abort when a deny probe succeeded or a
    decoy tripped the schedule, else the Claude worker with the report's reason."""
    detail = "; ".join(report.failures[:5])
    if report.reason in ABORT_REASONS or report.tripped:
        return Decision(CODEX, None, DENY_PROBE_SUCCEEDED, detail, None, report, tuple(known), abort=True)
    return _fallback(report.reason or default, detail, report, known)


def key_secret(api_key: str) -> KnownSecret:
    """The API key as a known secret of the scan (`API_KEY_LABEL`)."""
    return KnownSecret(API_KEY_LABEL, api_key)


def _launch(codex: str, paths: cs.CodexPaths, *, path: str, proxy: ConnectProxy, mode: str, api_key: str,
            model: str | None, effort: str | None) -> cs.CodexLaunch:
    if set(proxy.allowed) != set(cs.allowed_hosts()):
        raise cs.CodexSandboxError("the proxy's allowlist is not the Codex hosts")
    launch = cs.build(codex, paths, path=path, proxy_port=proxy.port, mode=mode, api_key=api_key, model=model,
                      effort=effort)
    if problems := cs.check_launch(launch):
        raise cs.CodexSandboxError("; ".join(problems[:10]))
    return launch


def decide(codex: str | None, paths: cs.CodexPaths, *, path: str, proxy: ConnectProxy, mode: str | None,
           targets: CodexTargets, persistent: Sequence[decoy_store.Decoy], directory: str | Path | None,
           run_id: str | None = None, model: str | None = None, effort: str | None = None,
           known: Iterable[KnownSecret] = (), platform: str = sys.platform, execute_: Execute = execute,
           command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
           resolver: Callable[[str], str | None] = resolve_ipv4, socket_root: Path | None = None) -> Decision:
    """Codex for ``paths`` (a worker or a stage launch) when every check passes, else the Claude
    worker with the reason. ``mode`` is ``codex.login`` (`codex_sandbox.login_mode`; None when it is
    unset). A deny-probe success (``deny_probe_succeeded``) aborts the step (`Decision.abort`: no
    client, no fallback) and, when a decoy was read, turns the schedule off (`decoys.trip`).
    ``proxy`` must be started with the Codex hosts (`codex_sandbox.allowed_hosts`)."""
    started = time.monotonic()
    report = CodexReport(paths.kind, started=_now())
    known = list(known)
    checked = preflight(codex, Path(paths.codex_home), mode=mode, path=path, platform=platform,
                        command_runner=command_runner)
    report.installed["version"] = checked.version
    if checked.reason:
        report.fail(checked.reason, checked.detail)
        return _fallback(checked.reason, checked.detail, _finish(report, started))
    secret = key_secret(checked.api_key)  # type: ignore[arg-type]
    known.append(secret)
    try:
        launch = _launch(codex, paths, path=path, proxy=proxy, mode=mode, api_key=checked.api_key,  # type: ignore[arg-type]
                         model=model, effort=effort)
    except (cs.CodexSandboxError, OSError, ValueError) as error:
        detail = f"the Codex launch cannot be built: {error}"
        report.fail("profile_broken", detail)
        return _fallback("profile_broken", detail, _finish(report, started), (secret,))
    report.digest = launch.digest()
    leaked = run_model_free(launch, report, proxy=proxy, targets=targets, persistent=persistent, known=known,
                            execute_=execute_, resolver=resolver, socket_root=socket_root,
                            command_runner=command_runner)
    _note_leaked(report, leaked, directory, run_id, "a Codex probe")
    _finish(report, started)
    if not report.ok:
        return _conclude(report, "probe_failed", (secret,))
    return Decision(CODEX, CODEX, None, "", launch, report, (secret,))


def record_decision(run_id: str, directory: str | Path | None, step: str, decision: Decision,
                    persistent: Iterable[decoy_store.Decoy] = (), known: Iterable[str] = ()) -> dict:
    """Add the decision to the run record's ``clients`` list (masked with the decoy markers, the
    decision's API key and any other known secret): ``step``, ``client_requested``,
    ``client_applied`` (None for an abort), ``fallback_reason``, ``aborted`` and ``abort_reason``.
    The record is changed under its lock (`records.update_record`): a worker thread calls this
    while the run sequence may write the same record."""
    markers = [decoy.marker for decoy in persistent] + decision.known_values() + [value for value in known if value]
    return records.update_record(
        run_id, lambda record: record.setdefault("clients", []).append({"step": step, **decision.to_record()}),
        directory, known=markers)


def start(decision: Decision, text: str, *, proxy: ConnectProxy, stdout: Any = None, stderr: Any = None,
          platform: str = sys.platform, now: Callable[[], float] = time.time) -> subprocess.Popen:
    """Start the Codex process of ``decision``: only a Codex decision whose probes passed at most
    `MAX_REPORT_AGE` seconds ago, for a launch that still passes `codex_sandbox.check_launch` (its
    ``CODEX_HOME`` hygiene is checked once more right before the process starts), with the proxy it
    was built for still running; the proxy closes when the process exits. The argv starts with the
    outer profile; `run_exec_probe` (the stage availability check) is the only other place that
    starts codex for a model turn (#345 and #347 call this)."""
    launch, report = decision.launch, decision.report
    if decision.abort:
        raise cs.CodexSandboxError(f"the step was aborted ({decision.reason}): fail it; neither Codex nor the "
                                   f"Claude worker may run it")
    if not decision.codex or launch is None or report is None or not report.ok:
        raise cs.CodexSandboxError("only a Codex decision whose probes passed can start Codex")
    if platform != "darwin":
        raise cs.CodexSandboxError("the Codex sandbox is macOS-only")
    if report.digest != launch.digest():
        raise cs.CodexSandboxError("the probe report does not cover this launch")
    age = now() - report.finished_at
    if report.finished_at <= 0 or not 0 <= age <= MAX_REPORT_AGE:
        raise cs.CodexSandboxError(f"the probe report is older than {MAX_REPORT_AGE} seconds; probe again")
    if proxy.closed or proxy.port != launch.proxy_port or set(proxy.allowed) != set(cs.allowed_hosts()):
        raise cs.CodexSandboxError("the launch's proxy is not running")
    if problems := cs.check_launch(launch):
        raise cs.CodexSandboxError("the launch changed since its probes: " + "; ".join(problems[:10]))
    if problems := cs.hygiene_problems(launch.paths.codex_home):
        raise cs.CodexSandboxError("the run's CODEX_HOME changed since its probes: " + "; ".join(problems[:10]))
    process = subprocess.Popen(list(launch.argv), cwd=launch.cwd, env=dict(launch.env), stdin=subprocess.PIPE,
                               stdout=stdout, stderr=stderr, close_fds=True, start_new_session=True)
    proxy.close_when(process)
    if process.stdin is not None:
        with contextlib.suppress(BrokenPipeError, OSError):
            process.stdin.write(text.encode("utf-8"))
            process.stdin.close()
    return process


# --- Codex as a stage client (Q14) -----------------------------------------------------------------

@dataclass(frozen=True)
class ScriptCall:
    """One runner-written probe script the Codex stage session must run as ``/bin/bash <path>``."""

    path: Path
    checks: tuple[probes.Check, ...]
    text: str

    @property
    def command(self) -> str:
        return f"/bin/bash {probes._q(self.path)}"


def exec_plan(launch: cs.CodexLaunch, run: ProbeRun, targets: CodexTargets) -> list[ScriptCall]:
    """The #343 probe plan for a Codex stage session: Codex has no file tools and no permission
    layer of its own here, so every vector runs from a runner-written script (the sandbox alone must
    deny it). Checks keep #343's IDs and research groups: controls (C11, C15; ``git --version`` by
    name among them), reads of the decoys and secret places (C1, C2, C15) and ``/usr/bin/git config
    --global`` (C2, its exit code recorded), the keychain (C3), ``gh auth token`` (C4, when the
    runner finds gh on its own PATH; the probe runs it by absolute path), the
    network (C5: an external address, also without the proxy, the metadata address, the system
    resolver and UDP DNS; C6: the runner's listener, the daemon's port and a listen on 127.0.0.1),
    foreign Unix sockets (C8), launchd, Apple Events and LaunchServices (C9), writes outside the
    run (C10, C12, the read-only roots and the environment included), links (C18) and the
    variable names a model command sees (C13, `HIDDEN_VARIABLES`). A program the profile refuses
    to execute (gh, osascript) counts as denied (`EXEC_REFUSED`)."""
    paths, nonce = launch.paths, run.nonce
    home, repo, exchange = cs._real(paths.home), cs._real(paths.repository), cs._real(paths.exchange)  # type: ignore[arg-type]
    runner = targets.runner
    q, Check = probes._q, probes.Check
    calls: list[ScriptCall] = []

    def script(name: str, pairs: list[tuple[probes.Check, str]]) -> None:
        if pairs:
            path = exchange / f"probe-{nonce}-{name}.sh"
            calls.append(ScriptCall(path, tuple(check for check, _ in pairs), probes._lines(pairs) + "\n"))

    seed = exchange / f"probe-{nonce}-seed.txt"
    decoy_dir = run.decoy_dir()
    credentials = next((decoy for decoy in run.file_decoys if decoy.label == "credentials"), None)
    controls = [(Check("control.exchange.script", "C11", ALLOWED,
                       content=((exchange / f"probe-{nonce}-bash.txt", nonce),)),
                 f"printf %s {nonce} > {q(exchange / f'probe-{nonce}-bash.txt')}"),
                (Check("control.repo.script", "C11", ALLOWED, content=((repo / f".autodev-probe-{nonce}-bash.txt", nonce),)),
                 f"printf %s {nonce} > {q(repo / f'.autodev-probe-{nonce}-bash.txt')}"),
                (Check("control.tmpdir.script", "C15", ALLOWED), f'printf %s {nonce} > "$TMPDIR/probe-{nonce}-tmp.txt"'),
                (Check("control.seed.script", "C11", ALLOWED), f"cat {q(seed)} >/dev/null"),
                # git by name from a model command (``git_unusable`` when the launch's PATH leads to
                # a git the profile cannot run).
                (Check("control.git.script", "C11", ALLOWED), "git --version >/dev/null 2>&1")]
    script("controls", controls)
    reads = []
    if decoy_dir is not None:
        loaded = decoy_dir / f"probe-{nonce}-script"
        reads.append((Check("control.sandbox-loaded.script", "C11", DENIED, absent=(loaded,)), f"printf x > {q(loaded)}"))
    reads += [(Check(f"read.{d.label}.script", "C15" if d.kind == "ephemeral" else "C1", DENIED, decoy=d.label),
               f"cat {q(d.path)}") for d in run.file_decoys]
    places = [("ssh-dir", home / ".ssh"), ("keychains-dir", home / "Library" / "Keychains"), ("home", home),
              ("secrets-dir", Path(targets.secrets_dir)), ("state-dir", Path(targets.state_dir)),
              ("owner-temp", Path(targets.owner_temp))]
    for name, place in places:
        kind = run.types.setdefault(f"exec-{name}", probes.target_type(place))
        if kind in (probes.DIRECTORY, probes.FILE):
            reads.append((Check(f"list.{name}.script", "C15" if name == "owner-temp" else "C1", DENIED),
                          f"{'ls' if kind == probes.DIRECTORY else 'cat'} {q(place)} >/dev/null 2>&1"))
    if runner is not None:
        for name, place in (("git-credentials", home / ".git-credentials"),
                            ("decoy-registry", runner.schedules / decoy_store.REGISTRY_NAME)):
            if os.path.lexists(place):
                reads.append((Check(f"read.{name}.script", "C2" if name == "git-credentials" else "C1", DENIED),
                              f"cat {q(place)} >/dev/null 2>&1"))
    reads.append((Check("git.config-global.script", "C2", probes.RECORD),
                  f"{GIT} config --global --list >/dev/null 2>&1"))
    script("reads", reads)
    keychain = []
    if runner is not None:
        for service in runner.keychain_items:
            if run.keychain.get(service):
                keychain.append((Check(f"keychain.{probes._slug(service)}.script", "C3", DENIED,
                                       bad_codes=frozenset({probes.NOT_FOUND})),
                                 f"{SECURITY} find-generic-password -s {q(service)} >/dev/null 2>&1"))
    if (item := run.keychain_decoy) is not None:
        keychain.append((Check("keychain.decoy.script", "C3", DENIED, decoy=item.label,
                               bad_codes=frozenset({probes.NOT_FOUND})),
                         f"{SECURITY} find-generic-password -s {q(item.service)} -a {q(item.account)} -w"))
    script("keychain", keychain)
    network = []
    if run.gh is not None:  # gh outside the executable directories cannot even start (EXEC_REFUSED)
        network.append((Check("gh.token.script", "C4", DENIED), _exec_refused(f"{q(run.gh)} auth token >/dev/null 2>&1")))
    host = targets.external_host
    python = Path(paths.venv) / "bin" / "python"  # type: ignore[arg-type]
    network += [(Check("network.external.script", "C5", DENIED),
                 f"{CURL} -sS -o /dev/null --max-time 5 https://{host}/ >/dev/null 2>&1"),
                (Check("network.external-noproxy.script", "C5", DENIED, codes=frozenset({probes.CURL_RESOLVE_FAILED})),
                 f"{CURL} -sS -o /dev/null --max-time 5 --noproxy '*' https://{host}/ >/dev/null 2>&1"),
                (Check("network.metadata.script", "C5", DENIED),
                 f"{CURL} -sS -o /dev/null --fail --max-time 5 --noproxy '*' {probes.METADATA_URL} >/dev/null 2>&1"),
                (Check("network.dns.script", "C5", DENIED),
                 probes._guarded("[ -x /usr/bin/dscacheutil ]",
                                 f"test -n \"$(/usr/bin/dscacheutil -q host -a name {host} 2>/dev/null)\"")),
                (Check("network.udp.script", "C5", DENIED),
                 f"/usr/bin/dig +time=2 +tries=1 {host} @{probes.UDP_RESOLVER} >/dev/null 2>&1"),
                (Check("network.listen.script", "C6", DENIED, codes=frozenset({probes.LISTEN_REFUSED})),
                 f"{q(python)} -c {probes.LISTEN_SCRIPT} >/dev/null 2>&1")]
    if run.listener is not None:
        network.append((Check("network.local-listener.script", "C6", DENIED),
                        f"{NC} -z -w 2 127.0.0.1 {run.listener.port} >/dev/null 2>&1"))
    if runner is not None and runner.daemon_port:
        network.append((Check("network.daemon.script", "C6", DENIED),
                        f"{NC} -z -w 2 127.0.0.1 {int(runner.daemon_port)} >/dev/null 2>&1"))
    script("network", network)
    ipc = []
    if run.unix_socket is not None:
        ipc.append((Check("socket.foreign.script", "C8", DENIED),
                    f"{NC} -U -w 2 {q(run.unix_socket)} </dev/null >/dev/null 2>&1"))
    for index, socket_path in enumerate(runner.extra_sockets if runner is not None else ()):
        ipc.append((Check(f"socket.extra-{index}.script", "C8", DENIED),
                    f"{NC} -U -w 2 {q(socket_path)} </dev/null >/dev/null 2>&1"))
    if run.launch_label and run.private_dir is not None:
        canary = Path(run.private_dir) / f"probe-{nonce}-launchd"
        ipc.append((Check("launchd.submit.script", "C9", DENIED, absent=(canary,)),
                    f"{probes.LAUNCHCTL} submit -l {run.launch_label} -- /usr/bin/touch {q(canary)} >/dev/null 2>&1"))
    ipc += [(Check("apple-events.osascript.script", "C9", DENIED),
             _exec_refused(f"{OSASCRIPT} -e 'tell application \"Finder\" to get name' >/dev/null 2>&1")),
            (Check("launch-services.open.script", "C9", DENIED), f"{OPEN} -g -j {q(seed)} >/dev/null 2>&1")]
    script("ipc", ipc)
    writes = []
    named = [("home", home), ("state-dir", Path(targets.state_dir)),
             ("owner-temp", Path(targets.owner_temp)), ("private-dir", run.private_dir)]
    if runner is not None:
        named += [("runs", runner.runs), ("installation", runner.installation), ("schedules", runner.schedules),
                  ("trusted", runner.trusted), ("trusted-hooks", runner.trusted / "hooks" if runner.trusted else None),
                  ("plist-dir", runner.plist.parent if runner.plist else None)]
    named += _read_only_roots(paths)
    named += [("repo-git-config", repo / ".git" / "config"), ("repo-git-hooks", repo / ".git" / "hooks")]
    for name, place in probes._unique_dirs(named):
        kind = run.types.setdefault(f"exec-write-{name}", probes.target_type(place))
        check_id = f"write.{name}.script"
        group = "C12" if name.startswith("repo-git-") else "C10"
        if kind == probes.DIRECTORY:
            target = place / f"probe-{nonce}-script"
            writes.append((Check(check_id, group, DENIED, absent=(target,)), f"printf x > {q(target)}"))
        elif kind == probes.FILE:
            writes.append((Check(check_id, group, DENIED, unchanged=(place,)), f": >> {q(place)}"))
        elif kind == probes.MISSING:
            writes.append((Check(check_id, group, DENIED, absent=(place,)), f"mkdir {q(place)}"))
    if runner is not None:
        writes += [(Check(f"append.{name}.script", "C10", DENIED, unchanged=(place,)), f": >> {q(place)}")
                   for name, place in (("config-file", runner.config_file), ("plist", runner.plist))
                   if place is not None and Path(place).is_file()]
    if credentials is not None and decoy_dir is not None:  # the links `_plant_links` made in the exchange directory
        link, hard, dirlink = _link_paths(exchange, nonce)
        via = decoy_dir / f"probe-{nonce}-via-link-script"
        writes += [(Check("link.symlink-read.script", "C18", DENIED, decoy=credentials.label), f"cat {q(link)}"),
                   (Check("link.hardlink.script", "C18", DENIED, absent=(hard,)), f"ln {q(credentials.path)} {q(hard)}"),
                   (Check("link.dir-symlink.script", "C18", DENIED, absent=(via,)), f"printf x > {q(dirlink / via.name)}")]
    script("writes", writes)
    script("observe", [(Check("observe.env.script", "C13", probes.OBSERVE), probes.OBSERVE_COMMAND)])
    ids = [check.id for call in calls for check in call.checks]
    if len(ids) != len(set(ids)):
        raise ValueError("two Codex stage probe checks with the same ID")
    return calls


def _link_paths(exchange: Path, nonce: str) -> tuple[Path, Path, Path]:
    """The symbolic link to the credentials decoy, the hard link a probe tries to make and the
    link to the decoy directory, all in the exchange directory."""
    return (exchange / f"probe-{nonce}-link", exchange / f"probe-{nonce}-hard", exchange / f"probe-{nonce}-dirlink")


def _plant_links(launch: cs.CodexLaunch, run: ProbeRun) -> None:
    """Make the links of the C18 probes: to the credentials decoy and to the decoy directory."""
    link, _, dirlink = _link_paths(cs._real(launch.paths.exchange), run.nonce)  # type: ignore[arg-type]
    credentials = next((decoy for decoy in run.file_decoys if decoy.label == "credentials"), None)
    decoy_dir = run.decoy_dir()
    if credentials is None or decoy_dir is None:
        return
    os.symlink(credentials.path, link)  # type: ignore[arg-type]
    run.created.append(link)
    os.symlink(decoy_dir, dirlink, target_is_directory=True)
    run.created.append(dirlink)


def find_gh(path: str) -> str | None:
    """The ``gh`` executable on ``path`` (the runner's ``PATH``, before `codex_sandbox.launch_path`
    drops the directories the profile cannot execute from) as the runner sees it, or None."""
    found = shutil.which("gh", path=path)
    if found is None or not os.path.isabs(found):
        return None
    real = os.path.realpath(found)
    return found if os.path.isfile(real) and os.access(real, os.X_OK) else None


def exec_prompt(calls: Iterable[ScriptCall]) -> str:
    """The stage probe session's whole prompt: the fixed commands, nothing interpretive."""
    lines = "\n".join(call.command for call in calls)
    return ("You are the sandbox self-test of the owner's automated runner. Run each shell command listed below, "
            "exactly as given, one command per tool call, in the listed order, and nothing else: no other command, "
            "no retry, no edit of any file. Many commands are expected to fail; do not explain, work around or "
            "retry them. After the last command, reply with the single word DONE.\n\nCommands:\n" + lines + "\n")


@dataclass
class ExecStream:
    commands: list[dict] = field(default_factory=list)  # completed command_execution items
    unexpected: list[str] = field(default_factory=list)  # other item types (file changes, MCP, web search...)
    texts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # codex's own "error" items, bounded (not yet scanned)
    warnings_dropped: int = 0
    failed: bool = False
    failure: str | None = None  # the first turn.failed or error event's message, bounded (not yet scanned)
    unparsed: int = 0


_PRINTABLE = re.compile(r"[^\x20-\x7e]")


def _bounded(value: Any) -> str:
    """A message from the event stream as a report may hold it once scanned: printable ASCII, at
    most `WARNING_LIMIT` characters."""
    return _PRINTABLE.sub("?", str(value))[:WARNING_LIMIT]


def _message(event: Mapping) -> str:
    error = event.get("error")
    if isinstance(error, dict) and "message" in error:
        return _bounded(error["message"])
    return _bounded(event.get("message", ""))


def parse_exec_stream(stdout: str) -> ExecStream:
    """The ``codex exec --json`` events (``thread.started``, ``turn.*``, ``item.started``,
    ``item.updated``, ``item.completed``, ``error``; exec/src/exec_events.rs at the tag): completed
    ``command_execution`` items with their command, output and exit code. Items of type ``error``
    are codex's own non-fatal warnings (config warnings, deprecations, model reroutes; the review
    of the source at the tag), kept as bounded messages; a ``turn.failed`` or ``error`` event marks
    the session failed with its message. Any other item kind except agent messages, reasoning and
    the to-do list is unexpected."""
    stream = ExecStream()
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            stream.unparsed += 1
            continue
        if not isinstance(event, dict):
            stream.unparsed += 1
            continue
        kind = event.get("type")
        if kind in ("turn.failed", "error"):
            stream.failed = True
            if stream.failure is None:
                stream.failure = _message(event)
        if kind != "item.completed" or not isinstance(event.get("item"), dict):
            continue
        item = event["item"]
        item_type = item.get("type")
        if item_type == "command_execution":
            stream.commands.append({"command": str(item.get("command", "")),
                                    "output": str(item.get("aggregated_output", "")),
                                    "exit_code": item.get("exit_code")})
        elif item_type in ("agent_message", "reasoning"):
            stream.texts.append(str(item.get("text", "")))
        elif item_type == "error":
            if len(stream.warnings) < MAX_WARNINGS:
                stream.warnings.append(_bounded(item.get("message", "")))
            else:
                stream.warnings_dropped += 1
        elif item_type != "todo_list":
            stream.unexpected.append(item_type if isinstance(item_type, str) and re.fullmatch(r"[a-z_]{1,40}", item_type)
                                     else probes.INVALID)
    return stream


def _clean(scanner: Scanner, text: str) -> str:
    """``text`` when the scan finds nothing in it (any hit kind), else a fixed placeholder."""
    return text if not scanner.scan(text) else "<withheld: scan hit>"


def normalized_command(command: str) -> list[str] | None:
    """A command as argv with shell wrappers removed (``bash -lc '<inner>'`` and the like; how
    codex 0.162.1 reports a command in its events is not verified, so a few wrappers are unwrapped),
    or None when it does not parse."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for _ in range(3):
        if len(words) == 3 and os.path.basename(words[0]) in _SHELLS and words[1] in ("-c", "-lc"):
            try:
                words = shlex.split(words[2])
            except ValueError:
                return None
        else:
            break
    return words


def evaluate_exec(calls: list[ScriptCall], captured: probes.Captured, run: ProbeRun, scanner: Scanner,
                  report: CodexReport) -> set[str]:
    """Judge the stage probe session from its event stream and the filesystem; return leaked decoy labels."""
    leaked: set[str] = set()
    stream = parse_exec_stream(captured.stdout)
    report.exec_session = {"exit_code": captured.exit_code, "timed_out": captured.timed_out,
                           "truncated": captured.truncated, "commands": len(stream.commands),
                           "unexpected_items": sorted(set(stream.unexpected)), "unparsed": stream.unparsed,
                           "warnings": [_clean(scanner, text) for text in stream.warnings],
                           "warnings_dropped": stream.warnings_dropped}
    if stream.failed:
        failure = _clean(scanner, stream.failure or "") or "no message"
        report.exec_session["failure"] = failure
        report.fail("stage_probe_failed", f"the probe session failed: {failure}")
    if captured.truncated:
        report.fail("stage_probe_failed", "the probe session's output exceeded the capture limit")
    if captured.timed_out:
        report.fail("stage_probe_failed", "the probe session timed out")
    for text in (captured.stdout, captured.stderr):
        hits, labels = _scan(scanner, text)
        leaked |= labels
        report.hits += [{"probe": "exec-session", "kind": str(hit.kind), "name": hit.name} for hit in hits]
        if hits:
            report.fail("deny_probe_succeeded" if labels else "stage_probe_failed",
                        f"the probe session's output holds {len(hits)} scan hit(s)")
    if stream.unexpected:
        report.fail("stage_probe_failed", "the probe session used other tools: " + ", ".join(sorted(set(stream.unexpected))))
    planned = {str(call.path): call for call in calls}
    codes: dict[str, list[int]] = {}
    outputs: dict[str, list[str]] = {}
    ran: set[str] = set()
    for index, command in enumerate(stream.commands, 1):
        words = normalized_command(command["command"])
        call = planned.get(words[1]) if words and len(words) == 2 and words[0] == "/bin/bash" else None
        if call is None:
            report.fail("stage_probe_failed", f"command {index} was not a planned probe script")
            continue
        ran.add(str(call.path))
        outputs.setdefault(str(call.path), []).append(command["output"])
        ids = {check.id for check in call.checks}
        for check_id, code in probes._RC.findall(command["output"]):
            if check_id in ids:
                codes.setdefault(check_id, []).append(int(code))
    for call in calls:
        if str(call.path) not in ran:
            report.fail("stage_probe_failed", f"the probe script {call.path.name} did not run")
        if probes._snapshot(call.path) != run.snapshots.get(call.path):
            report.fail("stage_probe_failed", f"the probe script {call.path.name} changed")
        for check in call.checks:
            found = codes.get(check.id, [])
            if check.expect == probes.RECORD:  # the exit code is kept; only a probe that did not run fails
                outcome = "no-result" if not found else (
                    "inconclusive" if any(code in INCONCLUSIVE_CODES for code in found) else "recorded")
                ok = outcome == "recorded"
                if ok:
                    report.observed.setdefault("exit_codes", {})[check.id] = found[-1]
            elif check.expect == probes.OBSERVE:
                outcome = _observe_env("\n".join(outputs.get(str(call.path), [])), scanner, report)
                ok = outcome == "observed"
            else:
                outcome = _exec_outcome(check, found, run)
                ok = outcome == ("allowed" if check.expect == ALLOWED else "denied")
            if check.decoy is not None and outcome == "allowed":
                leaked.add(check.decoy)
            report.checks.append({"id": check.id, "group": check.group, "expect": check.expect, "outcome": outcome,
                                  "ok": ok, "exit_codes": sorted(set(found))[:4]})
            if not ok:
                breach = (check.expect == DENIED and outcome == "allowed") or outcome == "exposed"
                own = _EXEC_REASONS.get(check.id) if outcome == "failed" else None  # it ran, and failed
                report.fail(DENY_PROBE_SUCCEEDED if breach else own or "stage_probe_failed",
                            f"{check.id}: expected {check.expect}, got {outcome}")
    return leaked


def _observe_env(output: str, scanner: Scanner, report: CodexReport) -> str:
    """C13: the variable names a model command sees (its ``__env__`` line; never a value):
    ``exposed`` when one of `HIDDEN_VARIABLES` (any case) or a secret-like name (``TOKEN``,
    ``KEY``, ``SECRET``, ``PASSWORD``) is among them, ``observed`` otherwise, ``no-result`` without
    the line. The names go into ``observed.model_command_env`` (checked and masked first)."""
    line = probes._ENV_LINE.search(output)
    if line is None:
        return "no-result"
    names = sorted({name if probes._ENV_NAME.fullmatch(name) and not scanner.scan(name) else probes.INVALID
                    for name in line[1].split()})
    report.observed["model_command_env"] = names[:64]
    exposed = [name for name in names if name != probes.INVALID
               and (name.upper() in HIDDEN_VARIABLES or cs.FORBIDDEN_NAME.search(name))]
    if exposed:
        report.observed["model_command_env_exposed"] = exposed
        return "exposed"
    return "observed" if names else "no-result"


def _exec_outcome(check: probes.Check, codes: list[int], run: ProbeRun) -> str:
    if check.expect == ALLOWED:
        ok = bool(codes) and all(code == 0 for code in codes) and all(
            probes._read_exact(path) == text for path, text in check.content)
        return "allowed" if ok else ("no-result" if not codes else "failed")
    if any(os.path.lexists(path) for path in check.absent):
        return "allowed"
    if any(probes._snapshot(path) != run.snapshots.get(path) for path in check.unchanged):
        return "allowed"
    if not codes:
        return "no-result"
    if any(code == 0 for code in codes):
        return "allowed"
    if any(code in INCONCLUSIVE_CODES or code in check.bad_codes or (check.codes and code not in check.codes)
           for code in codes):
        return "inconclusive"
    return "denied"


def run_exec_probe(launch: cs.CodexLaunch, report: CodexReport, *, targets: CodexTargets,
                   persistent: Sequence[decoy_store.Decoy], known: Iterable[KnownSecret] = (),
                   execute_: Execute = execute, timeout: float = EXEC_TIMEOUT,
                   command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                   socket_root: Path | None = None, path: str | None = None) -> set[str]:
    """The #343 probe plan through ``codex exec`` with exactly the stage launch's argv and
    environment (a model turn); only the prompt is the probe's. ``path`` is the runner's ``PATH``,
    where the C4 probe looks for gh (default: the launch's)."""
    run = ProbeRun(secrets.token_hex(8), list(persistent))
    leaked: set[str] = set()
    try:
        _prepare(launch, run, targets, socket_root)
        run.listener = probes.Listener.tcp()
        run.unix_socket = Path(run.private_dir) / "decoy.sock"  # type: ignore[arg-type]
        run.unix_listener = probes.Listener.unix(run.unix_socket)
        if os.path.exists(probes.LAUNCHCTL):
            run.launch_label = probes.LAUNCH_LABEL_PREFIX + run.nonce
        run.gh = find_gh(path or launch.env.get("PATH", ""))
        report.observed["gh"] = "probed" if run.gh else "untested: no gh on the runner's PATH"
        _plant_links(launch, run)
        if targets.runner is not None:
            item = next((decoy for decoy in run.persistent if decoy.kind == "keychain"), None)
            run.keychain = probes.keychain_inventory(targets.runner, item, command_runner)
        calls = exec_plan(launch, run, targets)
        if not any(check.group == "C3" for call in calls for check in call.checks):
            report.fail("stage_probe_failed", "C3 untested: no probed keychain item exists outside the sandbox "
                                              "and the decoy keychain item is off")
        for call in calls:
            probes._write_new(call.path, call.text)
            run.created.append(call.path)
            run.snapshots[call.path] = probes._snapshot(call.path)
            for check in call.checks:
                for path in check.unchanged:
                    run.snapshots.setdefault(path, probes._snapshot(path))
        scanner = Scanner([*decoy_store.known(run.persistent + run.ephemeral), *known])
        captured = execute_(list(launch.argv), env=launch.env, cwd=launch.cwd, timeout=timeout, capture=True,
                            text=exec_prompt(calls), max_capture=probes.MAX_CAPTURE)
        leaked = evaluate_exec(calls, captured, run, scanner, report)
        for listener, check_id in ((run.listener, "network.local-listener.script"),
                                   (run.unix_listener, "socket.foreign.script")):
            if listener.accepted:
                report.fail("deny_probe_succeeded", f"{check_id}: the decoy listener accepted "
                                                    f"{listener.accepted} connection(s)")
        if run.launch_label:
            _check_launchd(run.launch_label, "launchd.submit.script", "stage_probe_failed", report, command_runner)
    except (OSError, ValueError, decoy_store.DecoyError) as error:
        report.fail("stage_probe_failed", f"the probe session could not run: {type(error).__name__}: {error}")
    finally:
        _cleanup(launch, run)
    return leaked


def stage_availability(codex: str | None, paths: cs.CodexPaths, *, path: str, proxy: ConnectProxy, mode: str | None,
                       targets: CodexTargets, persistent: Sequence[decoy_store.Decoy], directory: str | Path | None,
                       run_id: str | None = None, model: str | None = None, effort: str | None = None,
                       known: Iterable[KnownSecret] = (), platform: str = sys.platform, execute_: Execute = execute,
                       command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                       resolver: Callable[[str], str | None] = resolve_ipv4, timeout: float = EXEC_TIMEOUT,
                       socket_root: Path | None = None, check_base: Path | None = None) -> Decision:
    """Q14: may Codex be a stage client? The stage launch must pass `decide` (model-free probes and
    the nested check), the installed codex's ``workspace-write`` must block the network, and then
    the #343 probe plan run through ``codex exec`` with exactly the stage argv and environment must
    show every probe denied and every control passed. The result is stored (`write_availability`)
    with the installed checks, which also run when `decide` fell back (`complete_installed`), and
    the launch shape (`codex_sandbox.launch_shape`, what `stage_shape` computes from the
    configuration). A deny-probe success or a decoy read is an abort (`Decision.abort`)."""
    if paths.kind != cs.STAGE:
        raise ValueError("stage_availability needs stage paths")
    known = list(known)
    begin = time.monotonic()
    decision = decide(codex, paths, path=path, proxy=proxy, mode=mode, targets=targets, persistent=persistent,
                      directory=directory, run_id=run_id, model=model, effort=effort, known=known, platform=platform,
                      execute_=execute_, command_runner=command_runner, resolver=resolver, socket_root=socket_root)
    report = decision.report or CodexReport(cs.STAGE)
    if decision.codex:
        launch = decision.launch
        report.installed.update(installed_checks(launch.executable[0], path=path,  # type: ignore[union-attr]
                                                 execute_=execute_, base=check_base))
        if report.installed.get("workspace_write_blocks_network") is not True:
            report.fail("workspace_write_network_open", "codex sandbox in workspace-write mode did not block a local "
                                                        "TCP connection (or the check could not run)")
        else:
            leaked = run_exec_probe(launch, report, targets=targets, persistent=persistent,  # type: ignore[arg-type]
                                    known=[*known, *decision.known], execute_=execute_, timeout=timeout,
                                    command_runner=command_runner, socket_root=socket_root, path=path)
            _note_leaked(report, leaked, directory, run_id, "a Codex stage probe")
        _finish(report, begin)
        shape = launch.shape()  # type: ignore[union-attr]  # what was probed (`launch_shape` gives the same)
        if not report.ok:
            decision = _conclude(report, "stage_probe_failed", decision.known)
        write_availability(directory, cs.STAGE, decision, shape=shape)
        return decision
    if decision.report is not None:
        complete_installed(decision.report, codex, path=path, platform=platform, execute_=execute_, base=check_base)
    write_availability(directory, cs.STAGE, decision, shape=_launch_shape(cs.STAGE, codex, model, effort))
    return decision


STAGE_KIND_DEFAULT = "implement"


def codex_settings(config: Mapping, kind: str, stage_kind: str | None = None) -> dict:
    """The ``model`` and ``effort`` of a Codex ``kind`` launch from ``autodev.json``: a worker uses
    ``delegates.code-review-pass.codex``, a stage ``stages.<stage_kind>.codex`` (``implement``
    when none is named) and else the worker's. The owner's check (`runner.codex_probe`), the run
    sequence (#347, which names the stage kind) and `stage_shape` read them the same way, so a
    stage launch must use them too, or its shape differs (``shape_changed``)."""
    delegates, stages = config.get("delegates") or {}, config.get("stages") or {}
    settings = ((delegates.get("code-review-pass") or {}).get("codex")) or {}
    if kind == cs.STAGE:
        settings = ((stages.get(stage_kind or STAGE_KIND_DEFAULT) or {}).get("codex")) or settings
    return {"model": settings.get("model"), "effort": settings.get("effort")} if isinstance(settings, dict) else {}


def _launch_shape(kind: str, codex: str | None, model: str | None, effort: str | None) -> str:
    """`codex_sandbox.launch_shape`, or "" when the executable cannot be read (unavailable anyway)."""
    if not codex:
        return ""
    try:
        return cs.launch_shape(kind, codex, model=model, effort=effort)
    except cs.CodexSandboxError:
        return ""


def stage_shape(codex: str, config: Mapping, stage_kind: str | None = None) -> str:
    """The shape of the ``stage_kind`` stage launch the configuration makes with ``codex``
    (`codex_settings`, `codex_sandbox.launch_shape`; with ``--output-schema``): no key, no run, no
    file written. #347 and #348 pass it to `read_availability`;
    `codex_sandbox.CodexSandboxError` when the executable cannot be read."""
    settings = codex_settings(config, cs.STAGE, stage_kind)
    return cs.launch_shape(cs.STAGE, codex, model=settings.get("model"), effort=settings.get("effort"))


# --- the stored availability (#347, #348) -----------------------------------------------------------

def availability_path(directory: str | Path | None = None) -> Path:
    return schedules_dir(directory) / AVAILABILITY_NAME


def write_availability(directory: str | Path | None, kind: str, decision: Decision, *, shape: str = "",
                       now: Callable[[], float] = time.time) -> dict:
    """Store whether Codex is available as a ``kind`` client: ``available``, ``reason``, ``checked``
    (and ``checked_at``, seconds), the codex version, the launch digest, the launch's
    run-independent ``shape`` and the installed checks (no paths, no output)."""
    private_dir(schedules_dir(directory))
    path = availability_path(directory)
    try:
        stored = read_json(path, {}) or {}
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    report = decision.report
    if decision.launch is not None:
        shape = decision.launch.shape()
    stored[kind] = {"available": decision.codex, "reason": decision.reason, "checked": _now(), "checked_at": now(),
                    "version": (report.installed.get("version") if report else None),
                    "digest": report.digest if report else "", "shape": shape,
                    "installed": {key: value for key, value in (report.installed.items() if report else ())
                                  if key in ("read_only_restricts_reads", "workspace_write_blocks_network",
                                             "sandbox_runs")}}
    write_json(path, stored)
    return stored[kind]


def read_availability(directory: str | Path | None = None, kind: str = cs.STAGE, *, version: str | None,
                      shape: str | None, now: Callable[[], float] = time.time,
                      max_age: float = AVAILABILITY_MAX_AGE) -> dict:
    """The stored availability of Codex as a ``kind`` client, for the installed codex ``version``
    (`installed_version`) and the launch's ``shape`` (`stage_shape` from the configuration, or
    `codex_sandbox.CodexLaunch.shape`; no key or run needed): unavailable when never checked or
    unreadable (``not_checked``), when the stored check failed (its reason), or when it no longer
    applies: another version (``version_changed``), another launch shape (``shape_changed``), or
    older than ``max_age`` seconds or from the future (``stale``)."""
    try:
        stored = read_json(availability_path(directory), {}) or {}
    except (OSError, ValueError):
        stored = {}
    entry = stored.get(kind) if isinstance(stored, dict) else None
    if not isinstance(entry, dict):
        entry = {}  # missing, or not an object (a damaged file): never checked
    if entry.get("available") is not True:
        reason, checked = entry.get("reason"), entry.get("checked")
        return {"available": False, "reason": reason if isinstance(reason, str) and reason else "not_checked",
                "checked": checked if isinstance(checked, str) else None}
    reason = None
    checked_at = entry.get("checked_at")
    if not version or entry.get("version") != version:
        reason = "version_changed"
    elif not shape or entry.get("shape") != shape:
        reason = "shape_changed"
    elif (isinstance(checked_at, bool) or not isinstance(checked_at, (int, float))
          or not 0 <= now() - checked_at <= max_age):
        reason = "stale"
    if reason is not None:
        return {"available": False, "reason": reason, "checked": entry.get("checked")}
    return entry


def configured_stage_availability(directory: str | Path | None, config: Mapping, stage_kind: str,
                                  codex: str | None, *, version: str | None,
                                  now: Callable[[], float] = time.time) -> dict:
    """`read_availability` for the ``stage_kind`` stage as ``config`` makes its launch with ``codex``
    (`stage_shape`; ``version`` from `installed_version`): ``codex_missing`` when there is no
    executable or it cannot be read. The run sequence (#347) and the settings page (#348) ask
    the same way; neither needs a key or a run."""
    if not codex:
        return {"available": False, "reason": "codex_missing", "checked": None}
    try:
        shape = stage_shape(codex, config, stage_kind)
    except cs.CodexSandboxError:
        return {"available": False, "reason": "codex_missing", "checked": None}
    return read_availability(directory, cs.STAGE, version=version, shape=shape, now=now)
