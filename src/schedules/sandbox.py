"""The Claude stage session's launcher: argv, sandbox settings, environment and login (#343).

Design: docs/autodev-plan.md, sections 5.4 and 5.6 (as amended by #343); reference:
docs/autodev-sandbox.md. A model session runs as the owner's macOS user (Q6), so everything it
may reach is named here and checked before it starts; `src.schedules.probes` then proves the
effect with a probe session that has exactly this argv, settings and environment.

* **argv** (`argv`): ``claude -p --restricted --safe-mode --tools <list> --strict-mcp-config
  --permission-mode dontAsk --permission-prompts none --disallowedTools mcp__*
  --no-session-persistence --settings <file> --add-dir <exchange> <state copy>
  --output-format stream-json --verbose`` and the stage's ``--model`` and ``--effort``. The
  ``select`` stage gets ``--tools Read,Grep,Glob``; ``implement`` and ``submit`` get
  ``Bash,Read,Edit,Write,Glob,Grep``. The prompt goes on standard input. The working directory
  is the one-off repository.
* **Settings** (`settings`, written by `write_settings` as a 0600 file the session cannot
  write): ``permissions.blockReadsOutsideWorkingDirectories``; ``Edit`` allow rules for the
  one-off repository and the exchange directory and ``Edit`` deny rules for the read-only state
  copy and virtual environment (they also become the sandbox's ``allowWrite`` and ``denyWrite``);
  ``sandbox.enabled``, ``failIfUnavailable``, ``autoAllowBashIfSandboxed`` and
  ``allowUnsandboxedCommands: false``; ``filesystem.disabled: false``, ``denyRead`` of ``~/``,
  ``~/Library/Keychains``, the owner's per-user temporary directory, ``/private/tmp/claude-<uid>``
  and the session's ``CLAUDE_CONFIG_DIR``; ``allowRead`` of the one-off repository, the virtual
  environment, the exchange directory, the state copy and the runtime read roots (the thin
  clients, the runner socket and the interpreters they need); ``allowWrite`` of the one-off
  repository and the exchange directory only; ``network.allowedDomains: []``,
  ``strictAllowlist: true`` and ``allowUnixSockets`` of the runner socket only;
  ``credentials.envVars`` denying ``CLAUDE_CODE_OAUTH_TOKEN`` to Bash; and
  ``disableClaudeAiConnectors``. Every key was checked against code.claude.com (settings
  reference and sandboxing pages, 2026-10-13); `check_settings` refuses anything else that
  loosens the sandbox. Paths are absolute: how relative paths resolve in a ``--settings`` file is
  undocumented.
* **Environment** (`environment`): an allowlist, never the runner's own environment
  (`src.schedules.runner.run_environment` copies the owner's ``TMPDIR``): ``PATH``, ``HOME``,
  ``LANG``, ``CLAUDE_CONFIG_DIR``, ``CLAUDE_CODE_TMPDIR`` and ``TMPDIR`` (``<exchange>/tmp``; the
  sandbox gives Bash ``$CLAUDE_CODE_TMPDIR/claude-<uid>``), ``GH_CONFIG_DIR`` (an empty
  directory), ``ENABLE_CLAUDEAI_MCP_SERVERS=false``, ``DISABLE_AUTOUPDATER=1`` and the run's
  ``AUTODEV_*`` variables. No name may contain ``TOKEN``, ``KEY``, ``SECRET`` or ``PASSWORD``
  except ``CLAUDE_CODE_OAUTH_TOKEN`` in the ``keychain:<service>`` login mode, and every value
  passes the outgoing-text scan.
* **Login** (`config_dir`, `require_login`): ``CLAUDE_CONFIG_DIR`` is a persistent runner-owned
  directory (0700) in the autodev state, logged in once by the owner. Before a real session the
  runner runs ``claude auth status --json`` under it (no model turn) and refuses with the
  command to run otherwise. The login mode ``keychain:<service>`` (``autodev.json``
  ``sandbox.login``) instead passes the token of that keychain item as
  ``CLAUDE_CODE_OAUTH_TOKEN``; the runner reads it and keeps it in memory only.

Nothing here starts a model; `src.schedules.probes.start_session` starts a stage session only
after its probes passed.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any

from src.daemon.state import atomic_private, private_dir

from . import records, scan
from .github import SECURITY
from .lease import schedules_dir

STAGE_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
READ_TOOLS = ("Read", "Grep", "Glob")
KIND_TOOLS = {"select": READ_TOOLS, "implement": STAGE_TOOLS, "submit": STAGE_TOOLS}
KINDS = tuple(KIND_TOOLS)
PERMISSION_MODE = "dontAsk"
DISALLOWED_TOOLS = "mcp__*"
OUTPUT_FLAGS = ("--output-format", "stream-json", "--verbose")
CONFIG_DIR_NAME = "claude-config"
SESSIONS_DIR = "sessions"
LOGIN_CONFIG_DIR = "config-dir"
KEYCHAIN_PREFIX = "keychain:"
OAUTH_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
DEFAULT_LANG = "en_US.UTF-8"
FORBIDDEN_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD", re.IGNORECASE)
RUN_VARIABLE = re.compile(r"AUTODEV_[A-Z0-9_]{1,64}")
BASE_VARIABLES = ("PATH", "HOME", "LANG", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_TMPDIR", "TMPDIR", "GH_CONFIG_DIR",
                  "ENABLE_CLAUDEAI_MCP_SERVERS", "DISABLE_AUTOUPDATER")
# Characters the sandbox reads as a wildcard in a path list, or that end blockReads for a launch dir.
UNSAFE_PATH = re.compile(r"[*?\[\]{}\x00-\x1f\x7f]")
SETTINGS_MAX_BYTES = 2 * 1024 * 1024  # the --settings file limit (claude --help, docs)
AUTH_TIMEOUT = 30
AUTH_OUTPUT_LIMIT = 64 * 1024
# Keys that loosen the sandbox; none may appear (docs: sandboxing "Security limitations" and the
# settings reference). The paths are under "sandbox".
LOOSENING_KEYS = (
    ("excludedCommands",), ("allowAppleEvents",), ("enableWeakerNetworkIsolation",),
    ("enableWeakerNestedSandbox",), ("ignoreViolations",), ("ripgrep",),
    ("network", "allowAllUnixSockets"), ("network", "allowLocalBinding"), ("network", "allowMachLookup"),
    ("network", "httpProxyPort"), ("network", "socksProxyPort"), ("network", "tlsTerminate"),
    ("network", "deniedDomains"), ("filesystem", "denyWrite"), ("filesystem", "allowManagedReadPathsOnly"),
    ("credentials", "files"), ("credentials", "allowPlaintextInject"), ("credentials", "awsPairs"),
    ("credentials", "sigv4"),
)
# Managed settings rank above --settings and could loosen it (docs: managed-settings, macOS).
MANAGED_DIRECTORY = Path("/Library/Application Support/ClaudeCode")
MANAGED_ENTRIES = ("managed-settings.json", "managed-settings.d", "managed-mcp.json")
MANAGED_PREFERENCES = Path("/Library/Managed Preferences")
MANAGED_DOMAIN = "com.anthropic.claudecode.plist"


class SandboxError(RuntimeError):
    """A session cannot be built or started safely; the message never holds a secret."""


class LoginRequired(SandboxError):
    """The session's ``CLAUDE_CONFIG_DIR`` is not logged in; ``command`` logs it in."""

    def __init__(self, config_dir: Path, detail: str):
        self.config_dir = config_dir
        self.command = f"CLAUDE_CONFIG_DIR={config_dir} claude auth login"
        super().__init__(f"the session's Claude configuration directory is not logged in ({detail}); "
                         f"run once: {self.command}")


# --- paths -------------------------------------------------------------------------------------------

def owner_temp_dir() -> Path:
    """The owner's per-user temporary directory (``$TMPDIR``, ``/var/folders/.../T`` on macOS), resolved."""
    return Path(os.path.realpath(tempfile.gettempdir()))


def claude_temp_root(uid: int | None = None) -> Path:
    """Claude Code's default per-user temporary root on macOS: ``/private/tmp/claude-<uid>``
    (``/tmp`` is a link to ``/private/tmp``; docs: env-vars ``CLAUDE_CODE_TMPDIR``)."""
    return Path(f"/private/tmp/claude-{os.getuid() if uid is None else uid}")


@dataclass(frozen=True)
class SessionPaths:
    """Every path a session's argv, settings and environment name. All absolute.

    ``repository`` (the one-off repository, the working directory) and ``exchange`` are the only
    writable directories; ``state_copy`` and ``venv`` are read-only; ``config_dir``,
    ``gh_config`` and ``settings_file`` are runner-owned and outside both writable directories.
    ``read_roots`` are runtime read roots: the thin clients' directory, the runner socket and the
    interpreters they need, which may lie in a denied region such as ``~/``.
    """

    home: Path
    repository: Path
    exchange: Path
    state_copy: Path
    venv: Path
    config_dir: Path
    gh_config: Path
    settings_file: Path
    socket: Path | None = None
    read_roots: tuple[Path, ...] = ()
    owner_temp: Path = field(default_factory=owner_temp_dir)
    claude_temp: Path = field(default_factory=claude_temp_root)

    @property
    def tmp(self) -> Path:
        """``CLAUDE_CODE_TMPDIR`` and ``TMPDIR``: Bash gets ``<tmp>/claude-<uid>``."""
        return self.exchange / "tmp"

    @property
    def writable(self) -> tuple[Path, Path]:
        return self.repository, self.exchange

    def all_paths(self) -> dict[str, Path]:
        named = {name: getattr(self, name) for name in (
            "home", "repository", "exchange", "state_copy", "venv", "config_dir", "gh_config", "settings_file",
            "owner_temp", "claude_temp")}
        if self.socket is not None:
            named["socket"] = self.socket
        for index, root in enumerate(self.read_roots):
            named[f"read_roots[{index}]"] = root
        return named

    def problems(self) -> list[str]:
        """Why these paths are unsafe for a session; empty when they are fine."""
        found = []
        for name, path in self.all_paths().items():
            if not Path(path).is_absolute():
                found.append(f"{name} must be absolute: {path}")
            elif UNSAFE_PATH.search(str(path)):
                found.append(f"{name} holds a wildcard or control character: {path}")
        if found:
            return found
        writable = [_real(path) for path in self.writable]
        for name in ("state_copy", "venv", "config_dir", "gh_config", "settings_file", "home"):
            path = _real(getattr(self, name))
            for root in writable:
                if path == root or root in path.parents:
                    found.append(f"{name} lies inside the session-writable {root}")
                if path in root.parents and name != "home":
                    found.append(f"{name} contains the session-writable {root}")
        if self.socket is not None:
            socket_dir = _real(self.socket.parent)
            for root in writable:
                if socket_dir == root or root in socket_dir.parents or socket_dir in root.parents:
                    found.append(f"the socket directory lies inside or around the session-writable {root}")
        if _real(self.repository) == _real(self.exchange):
            found.append("the one-off repository and the exchange directory must differ")
        return found


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def sessions_root(clone: Path) -> Path:
    """Where the conventional session directories live: ``sessions`` beside the trusted clone,
    in the runner's own directory (never under ``~/Documents`` or the installation)."""
    return Path(clone).parent / SESSIONS_DIR


def session_layout(root: Path, run_id: str, stage: str) -> dict[str, Path]:
    """The conventional directories of one session under ``<root>/<run-id>/<stage>``: ``repo``
    (one-off repository), ``exchange``, ``state`` (state copy), ``gh`` (empty ``GH_CONFIG_DIR``)
    and ``settings.json``. The caller creates them (`prepare_layout`)."""
    if not records.valid_run_id(run_id):
        raise ValueError(f"not a run ID: {run_id!r}")
    if stage not in KINDS and stage != "probe":
        raise ValueError(f"not a session kind: {stage!r}")
    base = Path(root) / run_id / stage
    return {"base": base, "repository": base / "repo", "exchange": base / "exchange", "state_copy": base / "state",
            "gh_config": base / "gh", "settings_file": base / "settings.json"}


def prepare_layout(layout: Mapping[str, Path]) -> None:
    """Create the private base, exchange (with ``tmp``), state and ``gh`` directories; the
    one-off repository is cloned into ``repository`` by `src.schedules.repos`."""
    private_dir(Path(layout["base"]).parent.parent)
    private_dir(Path(layout["base"]).parent)
    private_dir(layout["base"])
    for name in ("exchange", "state_copy", "gh_config"):
        private_dir(layout[name])
    private_dir(Path(layout["exchange"]) / "tmp")


def interpreter_roots(*interpreters: str | Path) -> tuple[Path, ...]:
    """The installation prefixes (the parent of ``bin``) of resolved interpreters, which a sandboxed
    command must read when they lie under a denied region such as ``~/`` (uv's Pythons do)."""
    roots: list[Path] = []
    for interpreter in interpreters:
        real = _real(Path(interpreter))
        prefix = real.parent.parent if real.parent.name == "bin" else real.parent
        if prefix not in roots:
            roots.append(prefix)
    return tuple(roots)


def venv_base_interpreter(venv: Path) -> Path | None:
    """The base interpreter a virtual environment's ``bin/python`` resolves to, if any."""
    candidate = Path(venv) / "bin" / "python"
    real = _real(candidate)
    return real if real != candidate and real.exists() else None


# --- argv ----------------------------------------------------------------------------------------------

def tools_for(kind: str) -> tuple[str, ...]:
    if kind not in KIND_TOOLS:
        raise ValueError(f"not a session kind: {kind!r} (one of {', '.join(KINDS)})")
    return KIND_TOOLS[kind]


def argv(claude: str, kind: str, paths: SessionPaths, *, model: str | None = None,
         effort: str | None = None) -> list[str]:
    """The exact argv of a Claude session of ``kind``; the prompt goes on standard input."""
    if not os.path.isabs(claude):
        raise SandboxError(f"the claude executable must be an absolute path: {claude!r}")
    command = [claude, "-p", "--restricted", "--safe-mode", "--tools", ",".join(tools_for(kind)),
               "--strict-mcp-config", "--permission-mode", PERMISSION_MODE, "--permission-prompts", "none",
               "--disallowedTools", DISALLOWED_TOOLS, "--no-session-persistence",
               "--settings", str(paths.settings_file),
               "--add-dir", str(paths.exchange), str(paths.state_copy), *OUTPUT_FLAGS]
    for flag, value in (("--model", model), ("--effort", effort)):
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}", value):
                raise SandboxError(f"{flag} must be a plain name: {value!r}")
            command += [flag, value]
    return command


# --- settings ------------------------------------------------------------------------------------------

def _rule(path: Path) -> str:
    """An absolute path in permission-rule syntax: ``//abs/path`` (docs: permissions, Read and Edit)."""
    return "/" + str(path)


def settings(kind: str, paths: SessionPaths) -> dict:
    """The ``--settings`` content of a session of ``kind`` (the same for every kind; a session
    without Bash has no socket)."""
    tools_for(kind)
    if problems := paths.problems():
        raise SandboxError("unsafe session paths: " + "; ".join(problems))
    read = [paths.repository, paths.venv, paths.exchange, paths.state_copy, *paths.read_roots]
    if paths.socket is not None:
        read.append(paths.socket)
    return {
        "permissions": {
            "blockReadsOutsideWorkingDirectories": True,
            "allow": [f"Edit({_rule(paths.repository)}/**)", f"Edit({_rule(paths.exchange)}/**)"],
            "deny": [f"Edit({_rule(paths.state_copy)}/**)", f"Edit({_rule(paths.venv)}/**)"],
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "disabled": False,
                "denyRead": deny_read(paths),
                "allowRead": list(dict.fromkeys(str(path) for path in read)),
                "allowWrite": [str(paths.repository), str(paths.exchange)],
            },
            "network": {
                "allowedDomains": [],
                "strictAllowlist": True,
                "allowUnixSockets": [str(paths.socket)] if paths.socket is not None else [],
            },
            "credentials": {"envVars": [{"name": OAUTH_VARIABLE, "mode": "deny"}]},
        },
        "disableClaudeAiConnectors": True,
    }


def deny_read(paths: SessionPaths) -> list[str]:
    """``denyRead``: the home directory (``~/``, the documented prefix; a bare ``~`` is not),
    the keychains, the owner's temporary directory, Claude Code's per-user temporary root and the
    session's own configuration directory."""
    return ["~/", "~/Library/Keychains", str(paths.owner_temp), str(paths.claude_temp), str(paths.config_dir)]


def write_settings(paths: SessionPaths, content: Mapping) -> Path:
    """Write the settings file (0600, atomically) into its private runner-owned directory."""
    text = json.dumps(content, indent=2, sort_keys=True) + "\n"
    if len(text.encode()) > SETTINGS_MAX_BYTES:
        raise SandboxError("the settings file exceeds the 2 MiB limit of --settings")
    private_dir(paths.settings_file.parent)
    atomic_private(paths.settings_file, text)
    os.chmod(paths.settings_file, 0o600)
    return paths.settings_file


def read_settings_file(path: Path) -> dict:
    """The settings file as written, refusing a link, a foreign or group-writable file."""
    try:
        found = os.lstat(path)
    except OSError as error:
        raise SandboxError(f"the settings file cannot be read: {type(error).__name__}") from None
    if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid() or found.st_mode & 0o077:
        raise SandboxError("the settings file must be a regular 0600 file of this user")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SandboxError(f"the settings file is not JSON: {type(error).__name__}") from None
    if not isinstance(data, dict):
        raise SandboxError("the settings file must hold a JSON object")
    return data


def _get(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return _MISSING
        data = data[key]
    return data


_MISSING = object()


def check_settings(content: Mapping, kind: str, paths: SessionPaths) -> list[str]:
    """Every difference from `settings` that could loosen the session; empty when it matches.

    The comparison is exact, so an extra key, a missing ``denyRead`` entry, an extra
    ``allowWrite`` path or a changed flag is a problem, and each known loosening key is named."""
    problems = []
    sandbox = _get(content, "sandbox")
    for keys in LOOSENING_KEYS:
        if _get(sandbox, *keys) is not _MISSING:
            problems.append(f"sandbox.{'.'.join(keys)} must not be set")
    for keys, value in ((("enabled",), True), (("failIfUnavailable",), True), (("autoAllowBashIfSandboxed",), True),
                        (("allowUnsandboxedCommands",), False), (("filesystem", "disabled"), False),
                        (("network", "strictAllowlist"), True), (("network", "allowedDomains"), [])):
        if _get(sandbox, *keys) != value:
            problems.append(f"sandbox.{'.'.join(keys)} must be {json.dumps(value)}")
    if _get(content, "permissions", "blockReadsOutsideWorkingDirectories") is not True:
        problems.append("permissions.blockReadsOutsideWorkingDirectories must be true")
    if _get(content, "disableClaudeAiConnectors") is not True:
        problems.append("disableClaudeAiConnectors must be true")
    deny = _get(sandbox, "filesystem", "denyRead")
    for entry in deny_read(paths):
        if not isinstance(deny, list) or entry not in deny:
            problems.append(f"sandbox.filesystem.denyRead lacks {entry}")
    try:
        expected = settings(kind, paths)
    except SandboxError as error:
        return problems + [str(error)]
    if not problems and dict(content) != expected:
        problems.append("the settings differ from the launcher's: " + ", ".join(_differences(content, expected)))
    return problems


def _differences(actual: Any, expected: Any, prefix: str = "") -> list[str]:
    if isinstance(actual, dict) and isinstance(expected, dict):
        found = []
        for key in sorted(set(actual) | set(expected)):
            where = f"{prefix}.{key}" if prefix else key
            if key not in expected:
                found.append(f"{where} (unexpected)")
            elif key not in actual:
                found.append(f"{where} (missing)")
            else:
                found += _differences(actual[key], expected[key], where)
        return found
    return [] if actual == expected else [prefix or "(root)"]


def managed_sources(directory: Path = MANAGED_DIRECTORY, preferences: Path = MANAGED_PREFERENCES,
                    user: str | None = None) -> list[str]:
    """Managed settings sources present on this machine; any can loosen ``--settings``, so a
    session refuses to start while one exists (the owner reviews it first). The MDM profile
    locations are recalled, not verified (docs name only the ``com.anthropic.claudecode`` domain)."""
    user = user or os.environ.get("USER") or ""
    candidates = [directory / name for name in MANAGED_ENTRIES] + [preferences / MANAGED_DOMAIN]
    if user:
        candidates.append(preferences / user / MANAGED_DOMAIN)
    return [str(candidate) for candidate in candidates if os.path.lexists(candidate)]


# --- environment -------------------------------------------------------------------------------------

def login_mode(config: Mapping) -> str:
    """``config-dir`` or ``keychain:<service>`` from ``autodev.json`` ``sandbox.login``."""
    mode = (config.get("sandbox") or {}).get("login", LOGIN_CONFIG_DIR)
    if mode == LOGIN_CONFIG_DIR or (isinstance(mode, str) and mode.startswith(KEYCHAIN_PREFIX)
                                    and mode[len(KEYCHAIN_PREFIX):].strip()):
        return mode
    raise SandboxError("sandbox.login must be config-dir or keychain:<service>")


def environment(paths: SessionPaths, *, path: str, mode: str = LOGIN_CONFIG_DIR, oauth_token: str | None = None,
                lang: str | None = None, run_variables: Mapping[str, str] | None = None) -> dict[str, str]:
    """The session's whole environment, built from the allowlist; `SandboxError` names a refused
    variable (never a value)."""
    if not path or any(not os.path.isabs(entry) for entry in path.split(os.pathsep)):
        raise SandboxError("PATH must be a list of absolute directories")
    built = {
        "PATH": path,
        "HOME": str(paths.home),
        "LANG": lang or DEFAULT_LANG,
        "CLAUDE_CONFIG_DIR": str(paths.config_dir),
        "CLAUDE_CODE_TMPDIR": str(paths.tmp),
        "TMPDIR": str(paths.tmp),
        "GH_CONFIG_DIR": str(paths.gh_config),
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "DISABLE_AUTOUPDATER": "1",
    }
    for name, value in (run_variables or {}).items():
        if not RUN_VARIABLE.fullmatch(name) or FORBIDDEN_NAME.search(name):
            raise SandboxError(f"run variable {name!r} is not an AUTODEV_* name without TOKEN, KEY, SECRET or PASSWORD")
        built[name] = str(value)
    if mode == LOGIN_CONFIG_DIR:
        if oauth_token is not None:
            raise SandboxError("an OAuth token is passed only in the keychain login mode")
    else:
        if not oauth_token:
            raise SandboxError("the keychain login mode needs the OAuth token")
        built[OAUTH_VARIABLE] = oauth_token
    if problems := check_environment(built, paths, mode):
        raise SandboxError("; ".join(problems))
    return built


def check_environment(env: Mapping[str, str], paths: SessionPaths, mode: str = LOGIN_CONFIG_DIR) -> list[str]:
    """Every way ``env`` departs from the allowlist; values are scanned but never named."""
    problems = []
    allowed = set(BASE_VARIABLES) | ({OAUTH_VARIABLE} if mode != LOGIN_CONFIG_DIR else set())
    for name in env:
        if name not in allowed and not RUN_VARIABLE.fullmatch(name):
            problems.append(f"{name} is not on the session allowlist")
        elif FORBIDDEN_NAME.search(name) and name != OAUTH_VARIABLE:
            problems.append(f"{name} names a secret")
    if OAUTH_VARIABLE in env and mode == LOGIN_CONFIG_DIR:
        problems.append(f"{OAUTH_VARIABLE} is passed only in the keychain login mode")
    for name in BASE_VARIABLES:
        if name not in env:
            problems.append(f"{name} is missing")
    expected = {"HOME": str(paths.home), "CLAUDE_CONFIG_DIR": str(paths.config_dir), "CLAUDE_CODE_TMPDIR": str(paths.tmp),
                "TMPDIR": str(paths.tmp), "GH_CONFIG_DIR": str(paths.gh_config), "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    for name, value in expected.items():
        if name in env and env[name] != value:
            problems.append(f"{name} must be {value}")
    gh = Path(env.get("GH_CONFIG_DIR", ""))
    if gh.is_dir() and any(gh.iterdir()):
        problems.append("GH_CONFIG_DIR must be an empty directory")
    scanner = scan.Scanner()
    for name, value in env.items():
        if name == OAUTH_VARIABLE:
            continue  # the one secret this mode passes on purpose; Bash never sees it (credentials.envVars)
        if "\x00" in value:
            problems.append(f"{name} holds a NUL character")
        elif scanner.scan(value) or any(pattern.search(value) for pattern in records.SECRET_PATTERNS):
            problems.append(f"{name} holds secret-like text")
    return problems


def keychain_secret(service: str, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """The password of the keychain item ``service`` (the OAuth token of the keychain login mode),
    in memory only; `SandboxError` never echoes any output."""
    try:
        result = runner([SECURITY, "find-generic-password", "-s", service, "-w"], capture_output=True, text=True,
                        stdin=subprocess.DEVNULL, timeout=AUTH_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise SandboxError(f"the keychain item {service} could not be read: {type(error).__name__}") from None
    value = (result.stdout or "").strip()
    if result.returncode != 0 or not value:
        raise SandboxError(f"the keychain item {service} could not be read (security exit code {result.returncode})")
    return value


# --- login ---------------------------------------------------------------------------------------------

def config_dir(directory: str | Path | None = None) -> Path:
    """The persistent session ``CLAUDE_CONFIG_DIR``: ``<state>/schedules/claude-config`` (0700)."""
    private_dir(schedules_dir(directory))
    return private_dir(schedules_dir(directory) / CONFIG_DIR_NAME)


def login_status(claude: str, config: Path, *, path: str, home: Path, oauth_token: str | None = None,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """``{"logged_in": bool, "detail": str}`` from ``claude auth status --json`` under ``config``
    (no model turn). Only the ``loggedIn`` field is read; e-mail and organisation are dropped.
    The field name is recalled, not verified on the installed binary (an open item of the live check)."""
    env = {"PATH": path, "HOME": str(home), "LANG": DEFAULT_LANG, "CLAUDE_CONFIG_DIR": str(config),
           "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "DISABLE_AUTOUPDATER": "1"}
    if oauth_token:
        env[OAUTH_VARIABLE] = oauth_token
    try:
        result = runner([claude, "auth", "status", "--json"], capture_output=True, text=True, env=env,
                        stdin=subprocess.DEVNULL, timeout=AUTH_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {"logged_in": False, "detail": f"claude auth status failed: {type(error).__name__}"}
    output = (result.stdout or "")[:AUTH_OUTPUT_LIMIT]
    try:
        data = json.loads(output)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"logged_in": False, "detail": f"claude auth status gave no JSON object (exit code {result.returncode})"}
    if data.get("loggedIn") is True:
        return {"logged_in": True, "detail": "logged in"}
    return {"logged_in": False, "detail": f"not logged in (exit code {result.returncode})"}


def require_login(claude: str, config: Path, *, path: str, home: Path, oauth_token: str | None = None,
                  runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """`login_status`, or `LoginRequired` with the command that logs the directory in."""
    status = login_status(claude, config, path=path, home=home, oauth_token=oauth_token, runner=runner)
    if not status["logged_in"]:
        raise LoginRequired(config, status["detail"])
    return status


# --- the session spec ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionSpec:
    """One session as it will start: argv, environment, working directory and settings."""

    kind: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(repr=False)
    paths: SessionPaths
    settings: Mapping[str, Any]
    mode: str = LOGIN_CONFIG_DIR

    @property
    def cwd(self) -> Path:
        return self.paths.repository

    @property
    def tools(self) -> tuple[str, ...]:
        return tools_for(self.kind)

    @property
    def has_bash(self) -> bool:
        return "Bash" in self.tools

    def digest(self) -> str:
        """SHA-256 over argv, environment, working directory and settings: what a probe cleared."""
        payload = json.dumps({"argv": list(self.argv), "env": dict(sorted(self.env.items())), "cwd": str(self.cwd),
                              "settings": self.settings}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


def build(claude: str, kind: str, paths: SessionPaths, *, path: str, mode: str = LOGIN_CONFIG_DIR,
          oauth_token: str | None = None, model: str | None = None, effort: str | None = None,
          lang: str | None = None, run_variables: Mapping[str, str] | None = None) -> SessionSpec:
    """Build a session: its settings file is written here; nothing starts."""
    content = settings(kind, paths)
    write_settings(paths, content)
    return SessionSpec(kind, tuple(argv(claude, kind, paths, model=model, effort=effort)),
                       environment(paths, path=path, mode=mode, oauth_token=oauth_token, lang=lang,
                                   run_variables=run_variables),
                       paths, content, mode)


def check_spec(spec: SessionSpec, claude: str | None = None) -> list[str]:
    """Every way ``spec`` (and its settings file on disk) departs from what the launcher builds."""
    problems = list(spec.paths.problems())
    if problems:
        return problems
    given = {flag: spec.argv[index + 1] for index, flag in enumerate(spec.argv[:-1]) if flag in ("--model", "--effort")}
    try:
        expected = argv(claude or spec.argv[0], spec.kind, spec.paths, model=given.get("--model"),
                        effort=given.get("--effort"))
    except (SandboxError, ValueError) as error:
        return [str(error)]
    if list(spec.argv) != expected:
        problems.append("the argv differs from the launcher's")
    problems += check_settings(spec.settings, spec.kind, spec.paths)
    try:
        on_disk = read_settings_file(spec.paths.settings_file)
    except SandboxError as error:
        problems.append(str(error))
    else:
        if on_disk != dict(spec.settings):
            problems.append("the settings file on disk differs from the session's settings")
    problems += check_environment(spec.env, spec.paths, spec.mode)
    if (spec.paths.repository / ".git").is_file():
        problems.append("the one-off repository's .git is a gitdir file, not a directory (a linked worktree)")
    return problems


def tree_problems(roots: Iterable[Path], limit: int = 200_000) -> list[str]:
    """Hard links and outward symbolic links in the session-writable trees (probe C18 pre-check):
    every regular file must have one link, and no symbolic link may resolve outside its root."""
    problems = []
    seen = 0
    for root in roots:
        real_root = _real(Path(root))
        for directory, names, files in os.walk(root, followlinks=False):
            for name in [*names, *files]:
                seen += 1
                if seen > limit:
                    return problems + [f"more than {limit} entries in the session-writable trees"]
                path = Path(directory) / name
                try:
                    found = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISLNK(found.st_mode):
                    target = _real(path)
                    if target != real_root and real_root not in target.parents:
                        problems.append(f"{path} is a symbolic link out of {root}")
                elif stat.S_ISREG(found.st_mode) and found.st_nlink != 1:
                    problems.append(f"{path} has {found.st_nlink} hard links")
    return problems
