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
  ancestors under it get a metadata allow (codex canonicalises them). Writes: ``CODEX_HOME``, the
  output directory, the run's temporary root and ``/dev/null`` (worker); the stage adds the
  one-off repository (except its ``.git/config`` and ``.git/hooks``) and the exchange directory.
  Network: only ``localhost:<proxy port>`` (Seatbelt accepts only ``*`` or ``localhost`` hosts),
  where the runner's CONNECT proxy (`src.schedules.codex_proxy`) tunnels to the login mode's hosts.
* **argv** (`argv`): ``/usr/bin/sandbox-exec -f <profile> -D ... <codex> exec --ignore-user-config
  --ignore-rules --ephemeral -s read-only --skip-git-repo-check -C <tree> --output-schema <file>
  -o <file>`` plus `HARDENING` (extension sources and model-command secrets), ``-m`` and
  ``-c model_reasoning_effort=`` when set, and ``-`` (the prompt on standard input). The stage
  session uses ``-s workspace-write -C <one-off repository>``, `STAGE_SWITCHES` and ``--json``.
  Never ``--add-dir``, a ``--dangerously-*`` flag or ``danger-full-access`` (`check_argv`).
* **Environment** (`environment`): an allowlist, separate from `runner.run_environment`:
  ``PATH``, ``HOME`` (an empty per-run directory), ``LANG``, ``CODEX_HOME``, ``TMPDIR`` (the run's
  temporary root, `sandbox.make_temp_root`), ``HTTPS_PROXY`` and ``HTTP_PROXY`` (the runner's proxy)
  and, only in the ``keychain:<service>`` login mode, ``CODEX_API_KEY``.
* **Login** (`codex_home`, `hygiene_problems`, `require_login`): ``CODEX_HOME`` is a persistent
  runner-owned directory (0700) in the autodev state, logged in once by the owner to a separate
  account. Before every start the runner refuses a ``CODEX_HOME`` that holds instruction or
  extension files, and runs ``codex login status`` (no model turn; only its exit status counts).

Nothing here starts a model or a process other than ``codex --version`` and ``codex login status``.
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
from .lease import schedules_dir

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
WORKER, STAGE = "worker", "stage"
KINDS = (WORKER, STAGE)
EFFORTS = CODEX_EFFORTS
# The version every flag, key and feature name below was checked against (help text, `codex
# features list` and the source at tag rust-v0.162.1); another version is unverified.
VERIFIED_VERSIONS = ("0.162.1",)
VERSION_LINE = re.compile(r"codex-cli (\d+\.\d+\.\d+)")
LOGIN_API_KEY, LOGIN_CHATGPT = "api-key", "chatgpt"
LOGIN_MODES = (LOGIN_API_KEY, LOGIN_CHATGPT)
KEYCHAIN_PREFIX = sandbox.KEYCHAIN_PREFIX
API_KEY_VARIABLE = "CODEX_API_KEY"  # exec reads this, not OPENAI_API_KEY (research, auth precedence)
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY")
BASE_VARIABLES = ("PATH", "HOME", "LANG", "CODEX_HOME", "TMPDIR", *PROXY_VARIABLES)
FORBIDDEN_NAME = sandbox.FORBIDDEN_NAME
# The proxy's allowlist per login mode (research: the API-key provider uses api.openai.com; a
# ChatGPT login uses chatgpt.com and refreshes its token at auth.openai.com). All on port 443.
API_HOSTS = ("api.openai.com",)
CHATGPT_HOSTS = ("api.openai.com", "chatgpt.com", "auth.openai.com")
PROXY_PORT_HTTPS = 443
CODEX_HOME_NAME = "codex-home"
RUN_HOME_NAME = "home"
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
FORBIDDEN_ARGUMENTS = ("--add-dir", "danger-full-access")
FORBIDDEN_PREFIX = "--dangerously"
_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Instruction and extension sources in CODEX_HOME that --ignore-user-config does not drop
# (research: the User layer stays). Names compare case-insensitively (the default macOS volume is).
UNCLEAN_FILES = ("agents.md", "agents.override.md", "hooks.json", "config.toml")
UNCLEAN_DIRECTORIES = ("rules", "plugins")
SKILLS_DIRECTORY = "skills"
SYSTEM_SKILLS = ".system"  # codex's own bundled-skill cache (source: ext/skills tests at the tag)
# System locations a codex package may not be: its directory is readable and executable in full.
_BROAD = tuple(Path(path) for path in ("/", "/usr", "/usr/local", "/opt", "/opt/homebrew", "/Applications",
                                       "/Library", "/System", "/private", "/private/tmp", "/private/var", "/Users"))


class CodexSandboxError(RuntimeError):
    """A Codex process cannot be built or started safely; the message never holds a secret."""


class CodexLoginRequired(CodexSandboxError):
    """``CODEX_HOME`` holds no login; ``command`` is the owner's command for the separate account."""

    def __init__(self, codex_home: Path, detail: str):
        self.codex_home = codex_home
        self.command = f"CODEX_HOME={codex_home} codex login"
        super().__init__(f"the runner's CODEX_HOME is not logged in ({detail}); log the separate account in once: "
                         f"{self.command} (or --with-api-key / --device-auth)")


# --- paths ---------------------------------------------------------------------------------------------

def _real(path: Path | str) -> Path:
    return Path(os.path.realpath(path))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@dataclass(frozen=True)
class CodexPaths:
    """Every path a Codex process's profile, argv and environment name. All absolute.

    ``home`` is the owner's home directory (denied except the run's directories); ``codex_home``,
    ``tmp_root`` (`sandbox.make_temp_root`; ``run_home``, the process's ``HOME``, is an empty
    directory inside it) and ``out_dir`` (the ``-o`` file) are writable; ``profile`` is the outer
    profile file, outside every directory the process may read or write. A worker reads ``tree``
    (``-C``), ``materials`` and ``schema``; a stage session writes ``repository`` (``-C``) and
    ``exchange``. ``read_roots`` are further read-only roots (a stage's environment or state copy).
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

    @property
    def output(self) -> Path:
        return self.out_dir / OUTPUT_NAME

    @property
    def cwd(self) -> Path:
        return self.tree if self.kind == WORKER else self.repository  # type: ignore[return-value]

    @property
    def writable(self) -> tuple[Path, ...]:
        found = [self.codex_home, self.tmp_root, self.out_dir]
        if self.kind == STAGE:
            found += [self.repository, self.exchange]  # type: ignore[list-item]
        return tuple(found)

    @property
    def readable(self) -> tuple[Path, ...]:
        """The read-only roots (directories and the schema file)."""
        found = [self.tree, self.materials, self.schema] if self.kind == WORKER else []
        return tuple(path for path in [*found, *self.read_roots] if path is not None)

    def named(self) -> dict[str, Path]:
        found = {name: getattr(self, name) for name in (
            "home", "codex_home", "tmp_root", "run_home", "out_dir", "profile", "tree", "materials", "schema",
            "repository", "exchange") if getattr(self, name) is not None}
        for index, root in enumerate(self.read_roots):
            found[f"read_roots[{index}]"] = root
        return found

    def problems(self) -> list[str]:
        """Why these paths are unsafe for a Codex process; empty when they are fine."""
        if self.kind not in KINDS:
            return [f"not a Codex kind: {self.kind!r} (one of {', '.join(KINDS)})"]
        needed = ("tree", "materials", "schema") if self.kind == WORKER else ("repository", "exchange")
        found = [f"a {self.kind} needs {name}" for name in needed if getattr(self, name) is None]
        for name, path in self.named().items():
            if not Path(path).is_absolute():
                found.append(f"{name} must be absolute: {path}")
            elif _CONTROL.search(str(path)):
                found.append(f"{name} holds a control character")
        if found:
            return found
        home = _real(self.home)
        protected = {"home": home, "ssh": home / ".ssh", "library": home / "Library",
                     "keychains": home / "Library/Keychains"}
        writable = {name: _real(getattr(self, name)) for name in
                    ("codex_home", "tmp_root", "out_dir", *(("repository", "exchange") if self.kind == STAGE else ()))}
        readable = {name: _real(path) for name, path in self.named().items()
                    if name in ("tree", "materials", "schema") or name.startswith("read_roots")}
        for name, path in {**writable, **readable}.items():
            for label, guarded in protected.items():
                if _inside(guarded, path):
                    found.append(f"{name} ({path}) equals or contains {label} ({guarded})")
        names = list(writable)
        for index, first in enumerate(names):
            for second in names[index + 1:]:
                if _inside(writable[first], writable[second]) or _inside(writable[second], writable[first]):
                    found.append(f"the writable {first} and {second} must not lie inside one another")
        for name, path in readable.items():
            for other, root in writable.items():
                if _inside(path, root):
                    found.append(f"the read-only {name} lies inside the writable {other}")
        profile = _real(self.profile)
        for name, path in {**writable, **readable}.items():
            if _inside(profile, path):
                found.append(f"the profile lies inside {name}, which the process can reach")
        run_home = _real(self.run_home)
        if run_home.parent != _real(self.tmp_root):
            found.append("run_home must be a directory directly inside the temporary root")
        if self.kind == STAGE and not os.path.isdir(_real(self.repository) / ".git"):  # type: ignore[arg-type]
            found.append("the one-off repository's .git must be a directory (a gitdir file would move the "
                         ".git/config and hooks the profile denies)")
        return found


def codex_home(directory: str | Path | None = None) -> Path:
    """The persistent runner-owned ``CODEX_HOME``: ``<state>/schedules/codex-home`` (0700)."""
    private_dir(schedules_dir(directory))
    return private_dir(schedules_dir(directory) / CODEX_HOME_NAME)


def prepare_run_home(tmp_root: Path) -> Path:
    """The process's ``HOME``: a new empty 0700 directory inside the run's temporary root."""
    path = Path(tmp_root) / RUN_HOME_NAME
    os.mkdir(path, 0o700)
    return path


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
    """What in ``CODEX_HOME`` codex would load despite ``--ignore-user-config`` and
    ``--ignore-rules``: ``AGENTS.md``, ``AGENTS.override.md``, ``hooks.json``, ``config.toml``,
    ``*.config.toml`` profiles, ``rules/``, ``plugins/`` and user skills (anything in ``skills/``
    but codex's own ``.system`` cache). A model command can write ``CODEX_HOME``, so a previous run
    could have planted any of them; the runner checks before every start."""
    problems = directory_problems(path, "CODEX_HOME")
    if problems:
        return problems
    try:
        entries = list(os.scandir(path))
    except OSError as error:
        return [f"CODEX_HOME cannot be listed: {type(error).__name__}"]
    for entry in entries:
        lowered = entry.name.lower()
        if lowered in UNCLEAN_FILES or lowered.endswith(".config.toml"):
            problems.append(f"CODEX_HOME holds {entry.name}")
        elif lowered in UNCLEAN_DIRECTORIES:
            problems.append(f"CODEX_HOME holds {entry.name}/")
        elif lowered == SKILLS_DIRECTORY:
            try:
                skills = list(os.scandir(entry.path)) if entry.is_dir(follow_symlinks=False) else None
            except OSError:
                skills = None
            if skills is None:
                problems.append(f"CODEX_HOME holds {entry.name}, which is not a plain directory")
            elif extra := sorted(item.name for item in skills if item.name != SYSTEM_SKILLS):
                problems.append(f"CODEX_HOME holds user skills: {', '.join(extra[:5])}")
            elif any(not item.is_dir(follow_symlinks=False) for item in skills):
                problems.append(f"CODEX_HOME holds {entry.name}/{SYSTEM_SKILLS}, which is not a plain directory")
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
    """The ``X.Y.Z`` of ``codex --version`` (no model turn), or None; ``env`` names a fresh or the
    runner's ``CODEX_HOME`` (codex creates ``tmp/arg0`` in it)."""
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


def parameters(paths: CodexPaths, package: Path, proxy_port: int) -> list[tuple[str, str]]:
    """The ``-D`` parameters of the profile: resolved paths and the proxy port, nothing else."""
    if not isinstance(proxy_port, int) or isinstance(proxy_port, bool) or not 0 < proxy_port < 65536:
        raise CodexSandboxError("the proxy port must be an integer from 1 to 65535")
    found = [("HOME_DIR", _real(paths.home)), ("CODEX_HOME", _real(paths.codex_home)), ("TMP_DIR", _real(paths.tmp_root)),
             ("OUT_DIR", _real(paths.out_dir)), ("CODEX_PKG", _real(package))]
    if paths.kind == WORKER:
        found += [("TREE", _real(paths.tree)), ("MATERIALS", _real(paths.materials)),  # type: ignore[arg-type]
                  ("SCHEMA", _real(paths.schema))]  # type: ignore[arg-type]
    else:
        repository = _real(paths.repository)  # type: ignore[arg-type]
        found += [("REPO", repository), ("EXCHANGE", _real(paths.exchange)),  # type: ignore[arg-type]
                  ("REPO_GIT_CONFIG", repository / ".git" / "config"), ("REPO_GIT_HOOKS", repository / ".git" / "hooks")]
    found += [(f"READ_ROOT_{index}", _real(root)) for index, root in enumerate(paths.read_roots)]
    reached = [path for name, path in found if name not in ("HOME_DIR", "REPO_GIT_CONFIG", "REPO_GIT_HOOKS")]
    found += [(f"ANCESTOR_{index}", path) for index, path in enumerate(ancestors(paths.home, reached))]
    pairs = [(name, str(value)) for name, value in found] + [("PROXY_PORT", str(proxy_port))]
    for name, value in pairs:
        if _CONTROL.search(value) or not value:
            raise CodexSandboxError(f"the profile parameter {name} is empty or holds a control character")
    return pairs


def _params(names: Iterable[str], form: str) -> str:
    return " ".join(f'({form} (param "{name}"))' for name in names)


def profile_text(kind: str, names: Sequence[str]) -> str:
    """The outer SBPL profile of ``kind`` for the parameter ``names`` (`parameters`). It names no
    path of the run: every run-specific path is a parameter, so the text only grows with the
    number of read roots and ancestors. Based on the research's tested deny-default profile
    (``outer-final.sb``, 2026-10-14: codex starts, git and the bundled rg run, home reads, writes
    and ports are denied); the order matters, because a later rule wins: the run's directories are
    allowed after the home directory is denied."""
    if kind not in KINDS:
        raise CodexSandboxError(f"not a Codex kind: {kind!r}")
    names = list(names)
    ancestors_ = [name for name in names if name.startswith("ANCESTOR_")]
    roots = [name for name in names if name.startswith("READ_ROOT_")]
    reads = ["CODEX_HOME", "TMP_DIR", "OUT_DIR"]
    writes = ["CODEX_HOME", "TMP_DIR", "OUT_DIR"]
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
        '  (subpath "/Library/Developer/CommandLineTools/usr/bin")',
        '  (subpath "/Library/Developer/CommandLineTools/usr/libexec"))',
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow file-read-metadata)",
        "(allow file-read*",
        '  (literal "/") (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share")',
        '  (subpath "/Library/Preferences") (subpath "/bin") (subpath "/usr/bin") (subpath "/usr/libexec")',
        '  (subpath "/private/etc/ssl") (literal "/private/etc/hosts")',
        '  (subpath "/Library/Developer/CommandLineTools") (literal "/private/var/db/xcode_select_link")',
        '  (subpath (param "CODEX_PKG"))',
        '  (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
        ';; The owner\'s home directory: denied, then the run\'s directories inside it re-allowed below.',
        '(deny file-read* (subpath (param "HOME_DIR")))',
        f'(allow file-read-metadata {_params(["HOME_DIR", *ancestors_], "literal")})',
        f"(allow file-read* {_params(reads, 'subpath')} {reads_extra}"
        + (f" {_params(roots, 'subpath')}" if roots else "") + ")",
        f"(allow file-write* {_params(writes, 'subpath')}",
        '  (literal "/dev/null") (literal "/dev/dtracehelper"))',
    ]
    if kind == STAGE:
        lines.append('(deny file-write* (literal (param "REPO_GIT_CONFIG")) (subpath (param "REPO_GIT_HOOKS")))')
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


def check_argv(argv: Sequence[str], profile: Path) -> list[str]:
    """What makes ``argv`` unsafe whatever else it holds: it must start with the profile prefix and
    hold no ``--add-dir``, no ``--dangerously-*`` flag and no ``danger-full-access``."""
    problems = []
    if list(argv[:3]) != [SANDBOX_EXEC, "-f", str(profile)]:
        problems.append(f"the argv must start with {SANDBOX_EXEC} -f <profile>")
    for argument in argv:
        if argument in FORBIDDEN_ARGUMENTS or argument.startswith(FORBIDDEN_PREFIX) or "danger-full-access" in argument:
            problems.append(f"the argv holds the forbidden argument {argument.split('=')[0]}")
    return problems


# --- environment -----------------------------------------------------------------------------------------

def login_mode(config: Mapping) -> str:
    """``api-key``, ``chatgpt`` or ``keychain:<service>`` from ``autodev.json`` ``codex.login``."""
    mode = (config.get("codex") or {}).get("login", LOGIN_API_KEY)
    if mode in LOGIN_MODES or (isinstance(mode, str) and mode.startswith(KEYCHAIN_PREFIX)
                               and mode[len(KEYCHAIN_PREFIX):].strip()):
        return mode
    raise CodexSandboxError("codex.login must be api-key, chatgpt or keychain:<service>")


def allowed_hosts(mode: str) -> tuple[str, ...]:
    """The hosts the proxy tunnels to (port 443) for the login ``mode``."""
    return CHATGPT_HOSTS if mode == LOGIN_CHATGPT else API_HOSTS


def proxy_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def environment(paths: CodexPaths, *, path: str, proxy_port: int, mode: str = LOGIN_API_KEY,
                api_key: str | None = None, lang: str | None = None) -> dict[str, str]:
    """The Codex process's whole environment, built from the allowlist; `CodexSandboxError` names
    a refused variable (never a value)."""
    if not path or any(not os.path.isabs(entry) for entry in path.split(os.pathsep)):
        raise CodexSandboxError("PATH must be a list of absolute directories")
    built = {"PATH": path, "HOME": str(_real(paths.run_home)), "LANG": lang or sandbox.DEFAULT_LANG,
             "CODEX_HOME": str(_real(paths.codex_home)), "TMPDIR": str(_real(paths.tmp_root)),
             "HTTPS_PROXY": proxy_url(proxy_port), "HTTP_PROXY": proxy_url(proxy_port)}
    if mode.startswith(KEYCHAIN_PREFIX):
        if not api_key:
            raise CodexSandboxError("the keychain login mode needs the API key")
        built[API_KEY_VARIABLE] = api_key
    elif api_key is not None:
        raise CodexSandboxError(f"{API_KEY_VARIABLE} is passed only in the keychain login mode")
    if problems := check_environment(built, paths, proxy_port, mode):
        raise CodexSandboxError("; ".join(problems))
    return built


def check_environment(env: Mapping[str, str], paths: CodexPaths, proxy_port: int,
                      mode: str = LOGIN_API_KEY) -> list[str]:
    """Every way ``env`` departs from the allowlist; values are scanned but never named."""
    problems = []
    keychain = mode.startswith(KEYCHAIN_PREFIX)
    allowed = set(BASE_VARIABLES) | ({API_KEY_VARIABLE} if keychain else set())
    for name in env:
        if name not in allowed:
            problems.append(f"{name} is not on the Codex allowlist")
        elif FORBIDDEN_NAME.search(name) and name != API_KEY_VARIABLE:
            problems.append(f"{name} names a secret")
    if API_KEY_VARIABLE in env and not keychain:
        problems.append(f"{API_KEY_VARIABLE} is passed only in the keychain login mode")
    for name in BASE_VARIABLES:
        if name not in env:
            problems.append(f"{name} is missing")
    expected = {"HOME": str(_real(paths.run_home)), "CODEX_HOME": str(_real(paths.codex_home)),
                "TMPDIR": str(_real(paths.tmp_root)), "HTTPS_PROXY": proxy_url(proxy_port),
                "HTTP_PROXY": proxy_url(proxy_port)}
    for name, value in expected.items():
        if name in env and env[name] != value:
            problems.append(f"{name} must be {value}")
    if not env.get("CODEX_HOME"):
        problems.append("CODEX_HOME must not be empty (codex would fall back to ~/.codex)")
    scanner = scan.Scanner()
    for name, value in env.items():
        if name == API_KEY_VARIABLE:
            continue  # the one secret this mode passes on purpose; model commands inherit only core variables
        if "\x00" in value:
            problems.append(f"{name} holds a NUL character")
        elif scanner.scan(value) or any(pattern.search(value) for pattern in records.SECRET_PATTERNS):
            problems.append(f"{name} holds secret-like text")
    return problems


# --- login -----------------------------------------------------------------------------------------------

def login_environment(codex_home_dir: Path, *, path: str, home: Path) -> dict[str, str]:
    """The environment of ``codex login status``: the runner's ``CODEX_HOME`` and an empty ``HOME``."""
    return {"PATH": path, "HOME": str(home), "LANG": sandbox.DEFAULT_LANG, "CODEX_HOME": str(codex_home_dir)}


def login_status(codex: str, codex_home_dir: Path, *, path: str, home: Path,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """``{"logged_in": bool, "detail": str}`` from ``codex login status`` under ``CODEX_HOME`` (no
    model turn; it exits 0 when a login is stored and 1 otherwise, per the source at the tag). Its
    output names the login and part of an API key, so it is discarded: only the exit status and a
    fixed message are kept."""
    try:
        result = runner([codex, "login", "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=login_environment(codex_home_dir, path=path, home=home), stdin=subprocess.DEVNULL,
                        timeout=COMMAND_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {"logged_in": False, "detail": f"codex login status failed: {type(error).__name__}"}
    if result.returncode == 0:
        return {"logged_in": True, "detail": "logged in"}
    return {"logged_in": False, "detail": f"not logged in (exit code {result.returncode})"}


def require_login(codex: str, codex_home_dir: Path, *, path: str, home: Path,
                  runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """`login_status`, or `CodexLoginRequired` naming the owner's command."""
    status = login_status(codex, codex_home_dir, path=path, home=home, runner=runner)
    if not status["logged_in"]:
        raise CodexLoginRequired(codex_home_dir, status["detail"])
    return status


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


def build(codex: str, paths: CodexPaths, *, path: str, proxy_port: int, mode: str = LOGIN_API_KEY,
          api_key: str | None = None, model: str | None = None, effort: str | None = None,
          lang: str | None = None) -> CodexLaunch:
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
    env = environment(paths, path=path, proxy_port=proxy_port, mode=mode, api_key=api_key, lang=lang)
    if problems := directory_problems(paths.run_home, "the run's HOME", empty=True):
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
    problems += check_environment(launch.env, paths, launch.proxy_port, launch.mode)
    problems += sandbox.temp_root_problems(paths.tmp_root)
    problems += hygiene_problems(paths.codex_home)
    return problems


def describe(launch: CodexLaunch) -> dict[str, Any]:
    """What a record may keep about a launch: kind, digest, version-independent argv shape (the
    codex flags without paths), the login mode and the parameter names; never the environment."""
    flags = [item for item in launch.command[1:] if not os.path.isabs(item)]
    return {"kind": launch.kind, "digest": launch.digest(), "flags": flags, "mode": launch.mode.split(":")[0],
            "parameters": [name for name, _ in launch.parameters], "executable": launch.executable[0]}
