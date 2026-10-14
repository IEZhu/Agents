"""The Codex worker and stage session launcher: outer Seatbelt profile, argv, environment, login (#344).

Design: docs/autodev-plan.md, sections 5.6, 5.7, Q8 and Q14 (as amended by #344); reference:
docs/autodev-codex.md. The Codex counterpart of `src.schedules.sandbox` (#343): everything a Codex
process may reach is named here; `src.schedules.codex_probes` proves the effect with model-free
probes that run under exactly this profile, environment and working directory, and decides
between Codex and the Claude worker.

* **The outer profile** (`profile_text`, written by `write_profile` as a 0600 file and read back):
  a deny-default Seatbelt profile (the research's tested profile of 2026-10-14). Every path
  reaches it only as a ``-D NAME=<resolved path>`` parameter of ``/usr/bin/sandbox-exec``, never
  in the SBPL text, so a path cannot add a rule; the text depends only on the kind and on how many
  parameters there are. Reads: the system directories codex needs, the codex package, and the
  run's directories; the owner's home directory is denied except those directories, whose
  ancestors under it get a metadata allow (codex canonicalises them). Writes: the run's temporary
  root (which holds the per-run ``CODEX_HOME`` and ``HOME``), the output directory and
  ``/dev/null`` (worker); the stage adds the one-off repository (except the repository and
  ``.git`` entries themselves, ``.git/config``, ``.git/hooks`` and the git redirectors) and the
  exchange directory, and may run its environment's interpreter. ``sysctl-read`` is limited to
  Codex's own allowlist; interpreters that can call ``sysctl`` directly are not executable.
  Network: only ``localhost:<proxy port>`` (Seatbelt accepts only ``*`` or ``localhost`` hosts),
  where the runner's CONNECT proxy (`src.schedules.codex_proxy`) tunnels to ``api.openai.com``.
* **argv** (`prefix` and `codex_command`): ``/usr/bin/sandbox-exec -f <profile> -D ... <codex> exec
  --ignore-user-config --ignore-rules --ephemeral -s read-only --skip-git-repo-check -C <tree>
  --output-schema <file> -o <file>`` plus `HARDENING` (extension sources and model-command
  secrets), ``-m`` and ``-c model_reasoning_effort=`` when set, and ``-`` (the prompt on standard
  input). The stage session uses ``-s workspace-write -C <one-off repository>``, `STAGE_SWITCHES`
  and ``--json``. Never ``--add-dir``, ``--approve-for-me``, a profile (``-p``), a ``--dangerously-*``
  flag, ``danger-full-access`` or another ``sandbox*`` configuration key (`check_argv`).
* **Environment** (`environment`): an allowlist, separate from `runner.run_environment`:
  ``PATH``, ``HOME``, ``CODEX_HOME`` and ``TMPDIR`` (empty per-run directories inside the run's
  temporary root, `sandbox.make_temp_root` and `prepare_run_directories`), ``LANG``,
  ``HTTPS_PROXY`` and ``HTTP_PROXY`` (the runner's proxy) and ``CODEX_API_KEY``.
* **Login** (`login_mode`): the only mode is ``codex.login: "keychain:<service>"``: the runner
  reads an OpenAI API key of a separate project with a spending limit (Q8) from that keychain
  item and passes it as ``CODEX_API_KEY`` to the codex process only. ``CODEX_HOME`` is created
  empty for each run (`prepare_run_directories`) and removed with the temporary root; before a start it
  may hold only what codex itself creates (`hygiene_problems`), never ``auth.json``.

Nothing here starts a model or a process other than ``codex --version``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any

from src.daemon.state import atomic_private, private_dir

from . import records, sandbox, scan
from .config import CODEX_EFFORTS

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
WORKER, STAGE = "worker", "stage"
KINDS = (WORKER, STAGE)
EFFORTS = CODEX_EFFORTS
# The version every flag, key and feature name below was checked against (help text, `codex
# features list` and the source at tag rust-v0.162.1); another version is unverified.
VERIFIED_VERSIONS = ("0.162.1",)
VERSION_LINE = re.compile(r"codex-cli (\d+\.\d+\.\d+)")
KEYCHAIN_PREFIX = sandbox.KEYCHAIN_PREFIX
API_KEY_VARIABLE = "CODEX_API_KEY"  # exec reads this, not OPENAI_API_KEY (research, auth precedence)
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY")
BASE_VARIABLES = ("PATH", "HOME", "LANG", "CODEX_HOME", "TMPDIR", *PROXY_VARIABLES, API_KEY_VARIABLE)
FORBIDDEN_NAME = sandbox.FORBIDDEN_NAME
# The proxy's allowlist: the API-key provider uses api.openai.com (research), on port 443.
API_HOSTS = ("api.openai.com",)
PROXY_PORT_HTTPS = 443
CODEX_HOME_NAME = "codex-home"
RUN_HOME_NAME = "home"
# TMPDIR is a directory inside the temporary root, not the root itself: codex refuses to create its
# arg0 helpers (apply_patch on PATH) when CODEX_HOME lies under TMPDIR (arg0/src/lib.rs at the tag).
RUN_TMP_NAME = "tmp"
OUTPUT_NAME = "last-message.txt"
PACKAGE_MANIFEST = "codex-package.json"
PROFILE_MAX_BYTES = 64 * 1024
COMMAND_TIMEOUT = 30
OUTPUT_LIMIT = 64 * 1024
# Switches that close extension sources and keep secrets out of model commands; each one is in
# `codex exec --help` or `codex features list` of 0.162.1, its config key in the source at the tag:
# hooks, plugins (local and remote) and the Apps connectors off; no project AGENTS.md and no walk
# above -C for project config; no analytics, no web search, no bundled skills or skills
# instructions; model commands inherit only the core variables (the default inherits everything,
# CODEX_API_KEY included).
HARDENING = (
    "--disable", "hooks", "--disable", "plugins", "--disable", "remote_plugin", "--disable", "apps",
    "-c", "project_doc_max_bytes=0", "-c", "project_root_markers=[]", "-c", "analytics.enabled=false",
    "-c", 'web_search="disabled"', "-c", "skills.bundled.enabled=false", "-c", "skills.include_instructions=false",
    "-c", "shell_environment_policy.inherit=core",
)
# workspace-write would also make the shared /tmp writable (TMPDIR is the run's own root).
STAGE_SWITCHES = ("-c", "sandbox_workspace_write.exclude_slash_tmp=true")
FORBIDDEN_PREFIXES = ("--add-dir", "--approve-for-me", "--dangerously", "--profile")
_CONFIG_FLAGS = ("-c", "--config")
_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# CODEX_HOME is created empty for every run; before a start it may hold only what codex itself
# creates (`codex --version` and `codex sandbox` create tmp/arg0; a session adds log/ and its
# state and log databases). Everything else is refused; these names are refused by name, because
# codex loads them despite --ignore-user-config and --ignore-rules (the research and the source
# at the tag): .env (arg0 sets every non-CODEX_ variable from it), AGENTS*.md, hooks.json,
# *config.toml, rules/, plugins/, skills/, models_cache.json (trusted base instructions) and
# auth.json (a stored login; the key comes from the keychain only).
CODEX_HOME_DIRECTORIES = ("tmp", "log")
CODEX_HOME_FILES = re.compile(r"(?:state|logs)_[0-9]{1,3}\.sqlite(?:-wal|-shm|-journal)?")
REFUSED_ENTRIES = (".env", "agents.md", "agents.override.md", "hooks.json", "config.toml", "rules", "plugins",
                   "skills", "models_cache.json", "auth.json")
# System locations a codex package may not be: its directory is readable and executable in full.
_BROAD = tuple(Path(path) for path in ("/", "/usr", "/usr/local", "/opt", "/opt/homebrew", "/Applications",
                                       "/Library", "/System", "/private", "/private/tmp", "/private/var", "/Users",
                                       "/nix", "/nix/store"))
# sysctl-read: Codex's own allowlist (codex-rs/sandboxing/src/seatbelt_base_policy.sbpl at
# rust-v0.162.1) plus the three names a trace of `codex --version` and `codex sandbox --
# /usr/bin/true` under this profile showed every process reading at start (2026-10-14). Defense in
# depth only: in testing on macOS 26.5.1 the name allowlist did not stop a sandboxed command from
# reading a same-user process's environment, so model commands can reach the codex process's
# ``CODEX_API_KEY`` (a residual risk: the key is a separate project's with a spending limit, the
# proxy reaches api.openai.com only, and the key is a known secret of the run's scan,
# `codex_probes.Decision.known`). The interpreters below are not executable either way.
SYSCTL_NAMES = (
    "hw.activecpu", "hw.busfrequency_compat", "hw.byteorder", "hw.cacheconfig", "hw.cachelinesize_compat",
    "hw.cpufamily", "hw.cpufrequency_compat", "hw.cputype", "hw.l1dcachesize_compat", "hw.l1icachesize_compat",
    "hw.l2cachesize_compat", "hw.l3cachesize_compat", "hw.logicalcpu_max", "hw.machine", "hw.model", "hw.memsize",
    "hw.ncpu", "hw.nperflevels", "hw.packages", "hw.pagesize_compat", "hw.pagesize", "hw.physicalcpu",
    "hw.physicalcpu_max", "hw.logicalcpu", "hw.cpufrequency", "hw.tbfrequency_compat", "hw.vectorunit",
    "machdep.cpu.brand_string", "kern.argmax", "kern.hostname", "kern.maxfilesperproc", "kern.maxproc",
    "kern.osproductversion", "kern.osrelease", "kern.ostype", "kern.osvariant_status", "kern.osversion",
    "kern.secure_kernel", "kern.sysv.semmns", "kern.usrstack64", "kern.version", "sysctl.proc_cputype", "vm.loadavg",
)
SYSCTL_PREFIXES = ("hw.optional.arm.", "hw.optional.armv8_", "hw.perflevel", "kern.proc.pgrp.", "kern.proc.pid.",
                   "net.routetable.")
SYSCTL_TRACED = ("kern.bootargs", "kern.iossupportversion", "security.mac.lockdown_mode_state")
# Interpreters in the executable directories that can call sysctl or a C function directly (perl's
# syscall, Ruby's Fiddle, JXA's ObjC bridge, Swift, LLDB's Python, the CLT Python); a review or a
# stage needs none of them (a stage runs its own environment's interpreter, `CodexPaths.venv`).
DENIED_EXECUTABLES = r"^/usr/bin/(perl|ruby|irb|erb|osascript|osacompile|swift|lldb|python|pydoc|tclsh|wish|jrunscript|jshell)"
CLT = "/Library/Developer/CommandLineTools"


class CodexSandboxError(RuntimeError):
    """A Codex process cannot be built or started safely; the message never holds a secret."""


# --- paths ---------------------------------------------------------------------------------------------

def _real(path: Path | str) -> Path:
    return Path(os.path.realpath(path))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@dataclass(frozen=True)
class CodexPaths:
    """Every path a Codex process's profile, argv and environment name. All absolute.

    ``home`` is the owner's home directory (denied except the run's directories); ``tmp_root``
    (`sandbox.make_temp_root`) holds ``codex_home`` and ``run_home`` (the process's ``CODEX_HOME``
    and ``HOME``, empty per-run directories directly inside it) and is writable with ``out_dir``
    (the ``-o`` file); ``profile`` is the outer profile file, outside every directory the process
    may read or write. A worker reads ``tree`` (``-C``), ``materials`` and ``schema``; a stage
    session writes ``repository`` (``-C``) and ``exchange`` and may run ``venv``'s interpreter.
    ``read_roots`` are further read-only roots (a stage's state copy). ``state_dir`` (the autodev
    state) and ``installation`` (the installation checkout) are protected like the home directory.
    """

    kind: str
    home: Path
    codex_home: Path
    tmp_root: Path
    run_home: Path
    out_dir: Path
    profile: Path
    tree: Path | None = None
    materials: Path | None = None
    schema: Path | None = None
    repository: Path | None = None
    exchange: Path | None = None
    read_roots: tuple[Path, ...] = ()
    venv: Path | None = None
    state_dir: Path | None = None
    installation: Path | None = None

    @property
    def output(self) -> Path:
        return self.out_dir / OUTPUT_NAME

    @property
    def tmpdir(self) -> Path:
        """The process's ``TMPDIR``: `RUN_TMP_NAME` inside the temporary root (`prepare_run_directories`)."""
        return Path(self.tmp_root) / RUN_TMP_NAME

    @property
    def cwd(self) -> Path:
        return self.tree if self.kind == WORKER else self.repository  # type: ignore[return-value]

    @property
    def writable(self) -> tuple[Path, ...]:
        """The writable roots (``codex_home`` and ``run_home`` lie inside ``tmp_root``)."""
        found = [self.tmp_root, self.out_dir]
        if self.kind == STAGE:
            found += [self.repository, self.exchange]  # type: ignore[list-item]
        return tuple(found)

    @property
    def readable(self) -> tuple[Path, ...]:
        """The read-only roots (directories and the schema file)."""
        found = [self.tree, self.materials, self.schema] if self.kind == WORKER else []
        return tuple(path for path in [*found, *self.read_roots, *self.interpreters] if path is not None)

    @property
    def interpreters(self) -> tuple[Path, ...]:
        """A stage's environment and the installation prefix of its base interpreter: the profile
        lets the stage read and execute both (`sandbox.interpreter_roots`)."""
        if self.kind != STAGE or self.venv is None:
            return ()
        base = sandbox.venv_base_interpreter(Path(self.venv))
        return (Path(self.venv), *(sandbox.interpreter_roots(base) if base is not None else ()))

    def named(self) -> dict[str, Path]:
        found = {name: getattr(self, name) for name in (
            "home", "codex_home", "tmp_root", "run_home", "out_dir", "profile", "tree", "materials", "schema",
            "repository", "exchange", "venv", "state_dir", "installation") if getattr(self, name) is not None}
        for index, root in enumerate(self.read_roots):
            found[f"read_roots[{index}]"] = root
        return found

    def problems(self) -> list[str]:
        """Why these paths are unsafe for a Codex process; empty when they are fine."""
        if self.kind not in KINDS:
            return [f"not a Codex kind: {self.kind!r} (one of {', '.join(KINDS)})"]
        needed = ("tree", "materials", "schema") if self.kind == WORKER else ("repository", "exchange", "venv")
        found = [f"a {self.kind} needs {name}" for name in needed if getattr(self, name) is None]
        if self.kind == WORKER and self.venv is not None:
            found.append("only a stage runs its environment's interpreter (venv)")
        for name, path in self.named().items():
            if not Path(path).is_absolute():
                found.append(f"{name} must be absolute: {path}")
            elif _CONTROL.search(str(path)):
                found.append(f"{name} holds a control character")
        if found:
            return found
        home = _real(self.home)
        protected = {"home": home, "ssh": home / ".ssh", "library": home / "Library",
                     "keychains": home / "Library/Keychains", "secrets": home / ".config" / "agents-autodev"}
        if self.state_dir is not None:
            protected["state"] = _real(self.state_dir)
        if self.installation is not None:
            protected["installation"] = _real(self.installation)
        writable = {name: _real(getattr(self, name)) for name in
                    ("tmp_root", "out_dir", *(("repository", "exchange") if self.kind == STAGE else ()))}
        readable = {name: _real(path) for name, path in self.named().items()
                    if name in ("tree", "materials", "schema") or name.startswith("read_roots")}
        for index, root in enumerate(self.interpreters):
            readable["venv" if index == 0 else f"interpreter[{index - 1}]"] = _real(root)
        for name, path in {**writable, **readable}.items():
            for label, guarded in protected.items():
                if _inside(guarded, path):
                    found.append(f"{name} ({path}) equals or contains {label} ({guarded})")
            if name.startswith(("venv", "interpreter")) and (path in _BROAD or len(path.parts) < 3):
                found.append(f"{name} ({path}) is too broad to read and execute in full")
        names = list(writable)
        for index, first in enumerate(names):
            for second in names[index + 1:]:
                if _inside(writable[first], writable[second]) or _inside(writable[second], writable[first]):
                    found.append(f"the writable {first} and {second} must not lie inside one another")
        for name, path in readable.items():
            for other, root in writable.items():
                if _inside(path, root) or _inside(root, path):
                    found.append(f"the read-only {name} and the writable {other} overlap")
        profile = _real(self.profile)
        for name, path in {**writable, **readable}.items():
            if _inside(profile, path):
                found.append(f"the profile lies inside {name}, which the process can reach")
        tmp_root = _real(self.tmp_root)
        for name in ("run_home", "codex_home"):
            if _real(getattr(self, name)).parent != tmp_root:
                found.append(f"{name} must be a directory directly inside the temporary root")
        if len({_real(self.run_home), _real(self.codex_home), _real(self.tmpdir)}) != 3:
            found.append("run_home, codex_home and the run's TMPDIR must be different directories")
        if self.kind == STAGE and not os.path.isdir(_real(self.repository) / ".git"):  # type: ignore[arg-type]
            found.append("the one-off repository's .git must be a directory (a gitdir file would move the "
                         ".git/config and hooks the profile denies)")
        return found


def prepare_run_directories(tmp_root: Path) -> dict[str, Path]:
    """The process's ``HOME``, ``CODEX_HOME`` and ``TMPDIR``: new empty 0700 directories inside the
    run's temporary root, removed with it (`sandbox.remove_temp_root`); never shared between runs
    or kinds. Returns ``run_home`` and ``codex_home`` (`CodexPaths` derives ``tmpdir``)."""
    found = {}
    for key, name in (("run_home", RUN_HOME_NAME), ("codex_home", CODEX_HOME_NAME), ("tmpdir", RUN_TMP_NAME)):
        found[key] = Path(tmp_root) / name
        os.mkdir(found[key], 0o700)
    del found["tmpdir"]
    return found


def directory_problems(path: Path, name: str, *, empty: bool = False) -> list[str]:
    """``path`` must be a 0700 directory of this user, not a link (and empty, when asked)."""
    try:
        found = os.lstat(path)
    except OSError as error:
        return [f"{name} cannot be checked: {type(error).__name__}"]
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or stat.S_IMODE(found.st_mode) != 0o700:
        return [f"{name} must be a 0700 directory of this user, not a link"]
    if empty and any(os.scandir(path)):
        return [f"{name} must be empty"]
    return []


def hygiene_problems(path: Path) -> list[str]:
    """What in the per-run ``CODEX_HOME`` codex did not create itself: only the directories
    `CODEX_HOME_DIRECTORIES` and the database files `CODEX_HOME_FILES` may exist (plain, not
    links); `REFUSED_ENTRIES` are named as sources codex would load. The runner checks before the
    probes and again right before a start, since a probe or another process could have written it."""
    problems = directory_problems(path, "CODEX_HOME")
    if problems:
        return problems
    try:
        entries = list(os.scandir(path))
    except OSError as error:
        return [f"CODEX_HOME cannot be listed: {type(error).__name__}"]
    for entry in sorted(entries, key=lambda item: item.name):
        name = entry.name
        try:
            mode = os.lstat(entry.path).st_mode
        except OSError:
            problems.append(f"CODEX_HOME holds {name}, which cannot be checked")
            continue
        if name in CODEX_HOME_DIRECTORIES and stat.S_ISDIR(mode):
            continue
        if CODEX_HOME_FILES.fullmatch(name) and stat.S_ISREG(mode):
            continue
        lowered = name.lower()
        if lowered in REFUSED_ENTRIES or lowered.endswith(".config.toml"):
            problems.append(f"CODEX_HOME holds {name}, which codex would load")
        else:
            problems.append(f"CODEX_HOME holds {name}, which codex does not create")
    return problems


# --- the executable --------------------------------------------------------------------------------------

def executable_identity(codex: str) -> tuple[str, str]:
    """The resolved path and SHA-256 of the ``codex`` executable (a Homebrew ``codex`` is a link into
    a versioned Caskroom directory, so an upgrade changes both)."""
    try:
        return sandbox.executable_identity(codex, name="codex")
    except sandbox.SandboxError as error:
        raise CodexSandboxError(str(error)) from None


def package_dir(real: str | Path) -> Path:
    """The codex package around the resolved executable ``<package>/bin/codex``: it holds
    ``codex-package.json`` (and the bundled ``rg`` and ``zsh``), and the profile lets the process
    read and execute it in full, so it must not be a broad system directory."""
    executable = Path(real)
    package = executable.parent.parent
    if executable.parent.name != "bin" or not (package / PACKAGE_MANIFEST).is_file():
        raise CodexSandboxError(f"the codex executable is not inside a codex package (<package>/bin/codex with "
                                f"{PACKAGE_MANIFEST}): {executable}")
    if package in _BROAD or len(package.parts) < 3:
        raise CodexSandboxError(f"the codex package directory is too broad: {package}")
    return package


def package_problems(package: Path, paths: CodexPaths) -> list[str]:
    """The package must lie outside the home directory (the home deny would hide it) and must not
    overlap a directory of the run (a writable package could replace codex)."""
    problems = []
    if _inside(package, _real(paths.home)):
        problems.append(f"the codex package lies inside the home directory: {package}")
    for root in [*map(_real, paths.writable), *map(_real, paths.readable)]:
        if _inside(package, root) or _inside(root, package):
            problems.append(f"the codex package and the run's directory {root} overlap")
    return problems


def installed_version(codex: str, *, env: Mapping[str, str],
                      runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str | None:
    """The ``X.Y.Z`` of ``codex --version`` (no model turn), or None; ``env`` must name a fresh
    ``CODEX_HOME`` and ``HOME`` (codex creates ``tmp/arg0`` in it and reads ``.env`` from it)."""
    try:
        result = runner([codex, "--version"], capture_output=True, text=True, env=dict(env), stdin=subprocess.DEVNULL,
                        timeout=COMMAND_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = VERSION_LINE.search((result.stdout or "")[:OUTPUT_LIMIT]) if result.returncode == 0 else None
    return match[1] if match else None


# --- the outer profile -----------------------------------------------------------------------------------

def ancestors(home: Path, targets: Iterable[Path]) -> list[Path]:
    """Every directory strictly between the resolved ``home`` and each resolved target under it:
    codex canonicalises its directories, which needs metadata of each ancestor, and the home deny
    covers them (the research saw ``failed to canonicalize CODEX_HOME`` without the allow)."""
    home = _real(home)
    found: list[Path] = []
    for target in targets:
        target = _real(target)
        if target == home or home not in target.parents:
            continue
        for parent in reversed(target.parents):
            if parent != home and home in parent.parents and parent not in found:
                found.append(parent)
    return found


# The stage's write denials inside the one-off repository: the repository and its .git entries
# themselves (a rename moves everything below them past every path rule), .git/config, the hooks,
# and the files that redirect git elsewhere (and their directories, which a rename would also move).
GIT_DENIALS = (("REPO_GIT", ".git"), ("REPO_GIT_CONFIG", ".git/config"), ("REPO_GIT_COMMONDIR", ".git/commondir"),
               ("REPO_GIT_INFO", ".git/info"), ("REPO_GIT_INFO_ALTERNATES", ".git/info/alternates"),
               ("REPO_GIT_OBJECTS", ".git/objects"), ("REPO_GIT_OBJECTS_INFO", ".git/objects/info"),
               ("REPO_GIT_OBJECTS_ALTERNATES", ".git/objects/info/alternates"))
_NOT_REACHED = {"HOME_DIR", "REPO_GIT_HOOKS", *(name for name, _ in GIT_DENIALS)}


def parameters(paths: CodexPaths, package: Path, proxy_port: int) -> list[tuple[str, str]]:
    """The ``-D`` parameters of the profile: resolved paths and the proxy port, nothing else."""
    if not isinstance(proxy_port, int) or isinstance(proxy_port, bool) or not 0 < proxy_port < 65536:
        raise CodexSandboxError("the proxy port must be an integer from 1 to 65535")
    found = [("HOME_DIR", _real(paths.home)), ("TMP_DIR", _real(paths.tmp_root)), ("OUT_DIR", _real(paths.out_dir)),
             ("CODEX_PKG", _real(package))]
    if paths.kind == WORKER:
        found += [("TREE", _real(paths.tree)), ("MATERIALS", _real(paths.materials)),  # type: ignore[arg-type]
                  ("SCHEMA", _real(paths.schema))]  # type: ignore[arg-type]
    else:
        repository = _real(paths.repository)  # type: ignore[arg-type]
        found += [("REPO", repository), ("EXCHANGE", _real(paths.exchange))]  # type: ignore[arg-type]
        found += [(name, repository / relative) for name, relative in GIT_DENIALS]
        found += [("REPO_GIT_HOOKS", repository / ".git" / "hooks")]
        found += [(f"INTERPRETER_{index}", _real(root)) for index, root in enumerate(paths.interpreters)]
    found += [(f"READ_ROOT_{index}", _real(root)) for index, root in enumerate(paths.read_roots)]
    reached = [path for name, path in found if name not in _NOT_REACHED]
    found += [(f"ANCESTOR_{index}", path) for index, path in enumerate(ancestors(paths.home, reached))]
    pairs = [(name, str(value)) for name, value in found] + [("PROXY_PORT", str(proxy_port))]
    for name, value in pairs:
        if _CONTROL.search(value) or not value:
            raise CodexSandboxError(f"the profile parameter {name} is empty or holds a control character")
    return pairs


def _params(names: Iterable[str], form: str) -> str:
    return " ".join(f'({form} (param "{name}"))' for name in names)


def _sysctl_rule() -> list[str]:
    names = [f'(sysctl-name "{name}")' for name in (*SYSCTL_NAMES, *SYSCTL_TRACED)]
    names += [f'(sysctl-name-prefix "{prefix}")' for prefix in SYSCTL_PREFIXES]
    lines = ["(allow sysctl-read"]
    for start in range(0, len(names), 4):
        lines.append("  " + " ".join(names[start:start + 4]))
    lines[-1] += ")"
    return lines


def profile_text(kind: str, names: Sequence[str]) -> str:
    """The outer SBPL profile of ``kind`` for the parameter ``names`` (`parameters`). It names no
    path of the run: every run-specific path is a parameter, so the text only grows with the
    number of read roots, interpreter roots and ancestors. Based on the research's tested
    deny-default profile (``outer-final.sb``, 2026-10-14: codex starts, git and the bundled rg run,
    home reads, writes and ports are denied); the order matters, because a later rule wins: the
    run's directories are allowed after the home directory is denied, and the stage's git
    denials come after its repository is allowed."""
    if kind not in KINDS:
        raise CodexSandboxError(f"not a Codex kind: {kind!r}")
    names = list(names)
    ancestors_ = [name for name in names if name.startswith("ANCESTOR_")]
    roots = [name for name in names if name.startswith("READ_ROOT_")]
    interpreters = [name for name in names if name.startswith("INTERPRETER_")]
    reads = ["TMP_DIR", "OUT_DIR"]
    writes = ["TMP_DIR", "OUT_DIR"]
    if kind == WORKER:
        reads_extra = f"{_params(['TREE', 'MATERIALS'], 'subpath')} {_params(['SCHEMA'], 'literal')}"
    else:
        reads_extra = _params(["REPO", "EXCHANGE"], "subpath")
        writes += ["REPO", "EXCHANGE"]
    lines = [
        "(version 1)",
        f";; autodev Codex {kind} (#344): every path is a -D parameter of sandbox-exec, never text here.",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec",
        '  (subpath (param "CODEX_PKG"))',
        '  (subpath "/bin") (subpath "/usr/bin") (subpath "/usr/libexec")',
        f'  (literal "{CLT}/usr/bin/git") (subpath "{CLT}/usr/libexec/git-core"))',
        f'(deny process-exec (regex #"{DENIED_EXECUTABLES}"))',
    ]
    if kind == STAGE and interpreters:
        lines.append(f"(allow process-exec {_params(interpreters, 'subpath')})")
    lines += [
        "(allow signal (target same-sandbox))",
        *_sysctl_rule(),
        "(allow file-read-metadata)",
        "(allow file-read*",
        '  (literal "/") (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share")',
        '  (subpath "/bin") (subpath "/usr/bin") (subpath "/usr/libexec")',
        '  (subpath "/private/etc/ssl") (literal "/private/etc/hosts")',
        f'  (subpath "{CLT}") (literal "/private/var/db/xcode_select_link")',
        '  (subpath (param "CODEX_PKG"))',
        '  (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
        ';; The owner\'s home directory: denied, then the run\'s directories inside it re-allowed below.',
        '(deny file-read* (subpath (param "HOME_DIR")))',
        f'(allow file-read-metadata {_params(["HOME_DIR", *ancestors_], "literal")})',
        f"(allow file-read* {_params(reads, 'subpath')} {reads_extra}"
        + (f" {_params(roots, 'subpath')}" if roots else "")
        + (f" {_params(interpreters, 'subpath')}" if kind == STAGE and interpreters else "") + ")",
        f"(allow file-write* {_params(writes, 'subpath')}",
        '  (literal "/dev/null") (literal "/dev/dtracehelper"))',
    ]
    if kind == STAGE:
        lines += [";; The repository and .git entries themselves, .git/config, the hooks and git's redirectors.",
                  f"(deny file-write* (literal (param \"REPO\")) {_params([name for name, _ in GIT_DENIALS], 'literal')}",
                  '  (subpath (param "REPO_GIT_HOOKS")))']
    lines.append('(allow network-outbound (remote ip (string-append "localhost:" (param "PROXY_PORT"))))')
    return "\n".join(lines) + "\n"


def write_profile(path: Path, text: str) -> Path:
    """Write the profile (0600, atomically) into its private runner-owned directory and read it back."""
    if len(text.encode()) > PROFILE_MAX_BYTES:
        raise CodexSandboxError("the profile is too large")
    private_dir(Path(path).parent)
    atomic_private(path, text)
    os.chmod(path, 0o600)
    if read_profile(path) != text:
        raise CodexSandboxError("the profile on disk differs from the one written")
    return Path(path)


def read_profile(path: Path) -> str:
    """The profile as written, refusing a link, a foreign or group-readable file."""
    try:
        found = os.lstat(path)
    except OSError as error:
        raise CodexSandboxError(f"the profile cannot be read: {type(error).__name__}") from None
    if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid() or found.st_mode & 0o077:
        raise CodexSandboxError("the profile must be a regular 0600 file of this user")
    if found.st_size > PROFILE_MAX_BYTES:
        raise CodexSandboxError("the profile is too large")
    return Path(path).read_text(encoding="utf-8")


# --- argv ------------------------------------------------------------------------------------------------

def prefix(profile: Path, params: Iterable[tuple[str, str]]) -> list[str]:
    """``/usr/bin/sandbox-exec -f <profile> -D NAME=value ...``: what every Codex process and every
    probe starts with."""
    found = [SANDBOX_EXEC, "-f", str(profile)]
    for name, value in params:
        found += ["-D", f"{name}={value}"]
    return found


def _plain(flag: str, value: str | None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str) or not _PLAIN.fullmatch(value):
        raise CodexSandboxError(f"{flag} must be a plain name: {value!r}")
    return [flag, value]


def check_effort(effort: str | None) -> str | None:
    """``effort`` when it is one of `EFFORTS` (codex accepts any string and passes it to the API)."""
    if effort is not None and effort not in EFFORTS:
        raise CodexSandboxError(f"model_reasoning_effort must be one of {', '.join(EFFORTS)}: {effort!r}")
    return effort


def codex_command(codex: str, paths: CodexPaths, *, model: str | None = None, effort: str | None = None) -> list[str]:
    """The codex part of the argv (after the profile prefix); the prompt is ``-`` (standard input)."""
    if not os.path.isabs(codex):
        raise CodexSandboxError(f"the codex executable must be an absolute path: {codex!r}")
    common = [codex, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral"]
    if paths.kind == WORKER:
        command = [*common, "-s", "read-only", "--skip-git-repo-check", "-C", str(_real(paths.tree)),  # type: ignore[arg-type]
                   "--output-schema", str(_real(paths.schema)), "-o", str(_real(paths.out_dir) / OUTPUT_NAME),  # type: ignore[arg-type]
                   *HARDENING]
    else:
        command = [*common, "-s", "workspace-write", "--skip-git-repo-check", "-C",
                   str(_real(paths.repository)), *HARDENING, *STAGE_SWITCHES, "--json",  # type: ignore[arg-type]
                   "-o", str(_real(paths.out_dir) / OUTPUT_NAME)]
    command += _plain("-m", model)
    if check_effort(effort) is not None:
        command += ["-c", f"model_reasoning_effort={effort}"]
    return [*command, "-"]


def _config_values(argv: Sequence[str]) -> list[str]:
    """Every ``-c``/``--config`` value of ``argv``, separate or attached (``--config=k=v``, ``-ck=v``)."""
    found = []
    for index, argument in enumerate(argv):
        if argument in _CONFIG_FLAGS:
            if index + 1 < len(argv):
                found.append(argv[index + 1])
        elif argument.startswith("--config="):
            found.append(argument[len("--config="):])
        elif argument.startswith("-c") and len(argument) > 2 and not argument.startswith("--"):
            found.append(argument[2:])
    return found


def check_argv(argv: Sequence[str], profile: Path) -> list[str]:
    """What makes ``argv`` unsafe whatever else it holds: it must start with the profile prefix and
    hold no argument starting with ``--add-dir``, ``--approve-for-me``, ``--dangerously`` or
    ``--profile``, no ``-p``, no ``danger-full-access`` and no ``sandbox*`` configuration key other
    than the stage's own `STAGE_SWITCHES` value."""
    problems = []
    if list(argv[:3]) != [SANDBOX_EXEC, "-f", str(profile)]:
        problems.append(f"the argv must start with {SANDBOX_EXEC} -f <profile>")
    for argument in argv[3:]:
        name = argument.split("=", 1)[0]
        if (argument.startswith(FORBIDDEN_PREFIXES) or "danger-full-access" in argument
                or re.fullmatch(r"-p.*", argument)):
            problems.append(f"the argv holds the forbidden argument {name}")
    for value in _config_values(argv[3:]):
        key = value.split("=", 1)[0].strip().strip('"')
        if key.startswith("sandbox") and value != STAGE_SWITCHES[1]:
            problems.append(f"the argv sets the configuration key {key[:40]}")
    return problems


# --- environment -----------------------------------------------------------------------------------------

def login_mode(config: Mapping) -> str | None:
    """``keychain:<service>`` from ``autodev.json`` ``codex.login``, or None when it is not set (the
    decision then falls back with ``login_missing``)."""
    mode = (config.get("codex") or {}).get("login")
    if mode is None:
        return None
    if isinstance(mode, str) and mode.startswith(KEYCHAIN_PREFIX) and mode[len(KEYCHAIN_PREFIX):].strip():
        return mode
    raise CodexSandboxError("codex.login must be keychain:<service> (an API key of a separate project in the keychain)")


def allowed_hosts() -> tuple[str, ...]:
    """The hosts the proxy tunnels to (port 443)."""
    return API_HOSTS


def proxy_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def environment(paths: CodexPaths, *, path: str, proxy_port: int, api_key: str,
                lang: str | None = None) -> dict[str, str]:
    """The Codex process's whole environment, built from the allowlist; `CodexSandboxError` names
    a refused variable (never a value)."""
    if not path or any(not os.path.isabs(entry) for entry in path.split(os.pathsep)):
        raise CodexSandboxError("PATH must be a list of absolute directories")
    if not isinstance(api_key, str) or not api_key.strip():
        raise CodexSandboxError("the Codex launch needs the API key from the keychain")
    built = {"PATH": path, "HOME": str(_real(paths.run_home)), "LANG": lang or sandbox.DEFAULT_LANG,
             "CODEX_HOME": str(_real(paths.codex_home)), "TMPDIR": str(_real(paths.tmpdir)),
             "HTTPS_PROXY": proxy_url(proxy_port), "HTTP_PROXY": proxy_url(proxy_port), API_KEY_VARIABLE: api_key}
    if problems := check_environment(built, paths, proxy_port):
        raise CodexSandboxError("; ".join(problems))
    return built


def check_environment(env: Mapping[str, str], paths: CodexPaths, proxy_port: int) -> list[str]:
    """Every way ``env`` departs from the allowlist; values are scanned but never named."""
    problems = []
    for name in env:
        if name not in BASE_VARIABLES:
            problems.append(f"{name} is not on the Codex allowlist"
                            + (" (it names a secret)" if FORBIDDEN_NAME.search(name) else ""))
    for name in BASE_VARIABLES:
        if name not in env:
            problems.append(f"{name} is missing")
    expected = {"HOME": str(_real(paths.run_home)), "CODEX_HOME": str(_real(paths.codex_home)),
                "TMPDIR": str(_real(paths.tmpdir)), "HTTPS_PROXY": proxy_url(proxy_port),
                "HTTP_PROXY": proxy_url(proxy_port)}
    for name, value in expected.items():
        if name in env and env[name] != value:
            problems.append(f"{name} must be {value}")
    if not env.get("CODEX_HOME"):
        problems.append("CODEX_HOME must not be empty (codex would fall back to ~/.codex)")
    scanner = scan.Scanner()
    for name, value in env.items():
        if name == API_KEY_VARIABLE:
            continue  # the one secret passed on purpose; model commands inherit only core variables
        if "\x00" in value:
            problems.append(f"{name} holds a NUL character")
        elif scanner.scan(value) or any(pattern.search(value) for pattern in records.SECRET_PATTERNS):
            problems.append(f"{name} holds secret-like text")
    return problems


# --- the launch ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CodexLaunch:
    """One Codex process as it will start: argv (profile prefix and codex command), environment,
    working directory and profile."""

    kind: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(repr=False)
    paths: CodexPaths
    profile: str
    parameters: tuple[tuple[str, str], ...]
    mode: str
    proxy_port: int
    executable: tuple[str, str]
    package: str

    @property
    def cwd(self) -> Path:
        return _real(self.paths.cwd)

    @property
    def prefix(self) -> list[str]:
        return list(self.argv[:3 + 2 * len(self.parameters)])

    @property
    def command(self) -> list[str]:
        return list(self.argv[3 + 2 * len(self.parameters):])

    def wrap(self, command: Sequence[str]) -> list[str]:
        """``command`` under exactly this profile and its parameters (a probe)."""
        return [*self.prefix, *command]

    def digest(self) -> str:
        """SHA-256 over argv, environment, working directory, profile text and the executable's
        resolved path and SHA-256: what a probe report cleared."""
        payload = json.dumps({"argv": list(self.argv), "env": dict(sorted(self.env.items())), "cwd": str(self.cwd),
                              "profile": self.profile, "executable": list(self.executable)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def shape(self) -> str:
        """SHA-256 over what does not change from run to run: the kind, the profile text, the codex
        flags without paths, the environment's names and the executable's resolved path and
        SHA-256. The stored stage availability is bound to it (`codex_probes.read_availability`)."""
        payload = json.dumps({"kind": self.kind, "profile": self.profile, "flags": describe_flags(self),
                              "env": sorted(self.env), "executable": list(self.executable)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


def build(codex: str, paths: CodexPaths, *, path: str, proxy_port: int, api_key: str, mode: str = KEYCHAIN_PREFIX,
          model: str | None = None, effort: str | None = None, lang: str | None = None) -> CodexLaunch:
    """Build a Codex launch: its profile is written here; nothing starts. ``codex`` may be a link;
    the argv names its resolved path, whose package the profile allows."""
    if problems := paths.problems():
        raise CodexSandboxError("unsafe Codex paths: " + "; ".join(problems))
    identity = executable_identity(codex)
    package = package_dir(identity[0])
    if problems := package_problems(package, paths):
        raise CodexSandboxError("; ".join(problems))
    params = parameters(paths, package, proxy_port)
    text = profile_text(paths.kind, [name for name, _ in params])
    command = codex_command(identity[0], paths, model=model, effort=effort)
    env = environment(paths, path=path, proxy_port=proxy_port, api_key=api_key, lang=lang)
    for directory, name in ((paths.run_home, "the run's HOME"), (paths.codex_home, "the run's CODEX_HOME"),
                            (paths.tmpdir, "the run's TMPDIR")):
        if problems := directory_problems(directory, name):
            raise CodexSandboxError("; ".join(problems))
    if problems := hygiene_problems(paths.codex_home):
        raise CodexSandboxError("; ".join(problems))
    write_profile(paths.profile, text)
    argv = (*prefix(paths.profile, params), *command)
    if problems := check_argv(argv, paths.profile):
        raise CodexSandboxError("; ".join(problems))
    return CodexLaunch(paths.kind, argv, env, paths, text, tuple(params), mode, proxy_port, identity, str(package))


def check_launch(launch: CodexLaunch) -> list[str]:
    """Every way ``launch`` (and its profile on disk) departs from what `build` makes."""
    paths = launch.paths
    problems = list(paths.problems())
    if problems:
        return problems
    problems += check_argv(launch.argv, paths.profile)
    try:
        identity = executable_identity(launch.executable[0])
        package = package_dir(identity[0])
        params = parameters(paths, package, launch.proxy_port)
        given = launch.command
        model = given[given.index("-m") + 1] if "-m" in given else None
        effort = next((item.split("=", 1)[1] for item in given if item.startswith("model_reasoning_effort=")), None)
        expected = (*prefix(paths.profile, params), *codex_command(identity[0], paths, model=model, effort=effort))
    except (CodexSandboxError, ValueError, IndexError) as error:
        return problems + [str(error)]
    if identity != tuple(launch.executable):
        problems.append("the codex executable differs from the one the launch was built with (resolved path or SHA-256)")
    if tuple(params) != tuple(launch.parameters) or tuple(launch.argv) != expected:
        problems.append("the argv differs from the launcher's")
    if launch.profile != profile_text(paths.kind, [name for name, _ in params]):
        problems.append("the profile differs from the launcher's")
    try:
        if read_profile(paths.profile) != launch.profile:
            problems.append("the profile on disk differs from the launch's")
    except CodexSandboxError as error:
        problems.append(str(error))
    problems += check_environment(launch.env, paths, launch.proxy_port)
    problems += sandbox.temp_root_problems(paths.tmp_root)
    problems += directory_problems(paths.run_home, "the run's HOME")
    problems += directory_problems(paths.tmpdir, "the run's TMPDIR")
    problems += hygiene_problems(paths.codex_home)
    return problems


def describe_flags(launch: CodexLaunch) -> list[str]:
    """The codex flags of a launch without its paths."""
    return [item for item in launch.command[1:] if not os.path.isabs(item)]


def describe(launch: CodexLaunch) -> dict[str, Any]:
    """What a record may keep about a launch: kind, digests, version-independent argv shape (the
    codex flags without paths), the login mode and the parameter names; never the environment."""
    return {"kind": launch.kind, "digest": launch.digest(), "shape": launch.shape(), "flags": describe_flags(launch),
            "mode": launch.mode.split(":")[0], "parameters": [name for name, _ in launch.parameters],
            "executable": launch.executable[0]}
