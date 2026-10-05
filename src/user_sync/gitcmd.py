"""Git and SSH for sync, isolated from the user's own configuration on every call.

The user's global and system git configuration never apply: hooks, signing, ``autocrlf``,
``insteadOf``, credential helpers or a work identity would break unattended commits or leak into
the personal repository. Every call therefore runs with:

* ``GIT_CONFIG_GLOBAL`` pointing at sync's own ``gitconfig`` and ``GIT_CONFIG_NOSYSTEM=1``;
  inherited ``GIT_*`` variables, which could redirect the repository or inject configuration,
  are dropped;
* ``--git-dir`` and ``--work-tree`` given explicitly, so git never discovers the installation's
  own repository above the library;
* ``GIT_TERMINAL_PROMPT=0``, ``GIT_ALLOW_PROTOCOL=ssh:https`` and literal pathspecs;
* ``GIT_SSH_COMMAND`` with this machine's key only, sync's own ``known_hosts`` with strict
  checking, an empty ``ssh_config`` and batch mode; the SSH agent is not consulted.

Only plumbing commands are used, and the working tree is written by the engine, not by git.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

LOCAL_TIMEOUT = 10
NETWORK_TIMEOUT = 60
# GIT_CONFIG_GLOBAL, on which the isolation rests, arrived in git 2.32.
MIN_GIT_VERSION = (2, 32)

_SCP = re.compile(r"(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]*)@(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)"
                  r":(?P<path>[A-Za-z0-9_][A-Za-z0-9._/~-]*)")
_URL = re.compile(r"(?P<scheme>ssh|https)://(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]*)@)?"
                  r"(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)(?::(?P<port>[0-9]{1,5}))?"
                  r"/(?P<path>[A-Za-z0-9_][A-Za-z0-9._/~-]*)")
_NETWORK_SIGNS = (
    "could not resolve", "connection timed out", "operation timed out", "connection refused",
    "network is unreachable", "no route to host", "connection reset", "connection closed",
    "temporary failure in name resolution", "name or service not known", "nodename nor servname",
    "failed to connect", "ssh: connect to host", "kex_exchange_identification", "broken pipe",
    "timed out",
)
_HOST_KEY_SIGNS = ("host key verification failed", "remote host identification has changed",
                   "host key is known", "host key for")
_AUTH_SIGNS = ("permission denied", "authentication failed", "could not read username",
               "terminal prompts disabled", "access denied", "repository not found",
               "returned error: 401", "returned error: 403", "not authorized", "invalid username")


class RemoteError(ValueError):
    """A remote URL that sync refuses to use."""


@dataclass(frozen=True)
class Remote:
    """A validated remote. ``kind`` is ``ssh``, ``https`` or, in tests only, ``file``."""

    url: str
    kind: str
    host: str = ""
    port: int | None = None
    path: str = ""

    @property
    def display(self) -> str:
        """``host/owner/name`` without user or scheme, for status lines."""
        if self.kind == "file":
            return self.url
        return f"{self.host}/{re.sub(r'[.]git$', '', self.path.strip('/'))}"

    @property
    def github(self) -> tuple[str, str] | None:
        """``(owner, name)`` for a github.com repository."""
        if self.host.lower() != "github.com":
            return None
        parts = re.sub(r"[.]git$", "", self.path.strip("/")).split("/")
        return (parts[0], parts[1]) if len(parts) == 2 and all(parts) else None

    @property
    def https_url(self) -> str | None:
        """The same repository over HTTPS, for the anonymous visibility check."""
        if self.kind == "file":
            return None
        port = f":{self.port}" if self.kind == "https" and self.port else ""  # an SSH port is not an HTTPS one
        return f"https://{self.host}{port}/{self.path.strip('/')}"


def parse_remote(url: str, *, allow_file: bool = False) -> Remote:
    """Accept ``git@host:owner/repo``, ``ssh://…`` and ``https://…`` only.

    A leading ``-``, whitespace, control characters, ``..`` segments and credentials in an HTTPS
    URL are refused. ``allow_file`` admits an absolute local path; only tests pass it.
    """
    if not isinstance(url, str) or not url or url != url.strip() or url.startswith("-") \
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise RemoteError("remote must be git@host:owner/repo, ssh://… or https://… without spaces")
    if allow_file and os.path.isabs(url) and "://" not in url:
        return Remote(url, "file", path=url)
    match = _SCP.fullmatch(url)
    if match:
        kind, host, port, path = "ssh", match["host"], None, match["path"]
    else:
        match = _URL.fullmatch(url)
        if not match:
            raise RemoteError("remote must be git@host:owner/repo, ssh://… or https://…")
        kind, host, path = match["scheme"], match["host"], match["path"]
        if kind == "https" and match["user"]:
            raise RemoteError("an HTTPS remote must not carry a user name or token")
        port = int(match["port"]) if match["port"] else None
        if port is not None and not 0 < port < 65536:
            raise RemoteError("invalid port in the remote URL")
    if ".." in path.split("/") or host.startswith("-") or host.endswith("."):
        raise RemoteError("invalid host or path in the remote URL")
    return Remote(url, kind, host.lower(), port, path)


def git_version(executable: str = "git") -> tuple[int, ...] | None:
    """``(major, minor, patch)`` of the installed git, or None when it cannot run."""
    try:
        result = subprocess.run([executable, "version"], capture_output=True, text=True,
                                timeout=LOCAL_TIMEOUT, env=clean_environment(), **no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", result.stdout)
    return tuple(int(part or 0) for part in match.groups()) if match else None


def classify(stderr: str) -> str:
    """``host_key``, ``network``, ``auth`` or ``git`` for a failed network command."""
    text = stderr.lower()
    if any(sign in text for sign in _HOST_KEY_SIGNS):
        return "host_key"
    if any(sign in text for sign in _NETWORK_SIGNS):
        return "network"
    if any(sign in text for sign in _AUTH_SIGNS):
        return "auth"
    if "does not appear to be a git repository" in text:
        return "not_found"
    return "git"


class GitError(RuntimeError):
    """A git command failed; ``kind`` is ``classify``'s result, ``local`` or ``timeout``."""

    def __init__(self, command: str, kind: str, stderr: str = "", returncode: int | None = None):
        self.command, self.kind, self.stderr, self.returncode = command, kind, stderr, returncode
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else f"exit code {returncode}"
        super().__init__(f"git {command}: {detail}")


_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL", re.IGNORECASE)


def clean_environment() -> dict[str, str]:
    """The process environment without anything that steers git or ssh, and without secrets
    such as ``AGENTS_GITHUB_TOKEN`` or API keys, which neither needs."""
    dropped = ("SSH_AUTH_SOCK", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "DISPLAY")
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("GIT_") and key not in dropped
                   and not _SECRET_NAME.search(key)}
    environment.update(LC_ALL="C", LANGUAGE="C")  # error messages are classified in English
    return environment


def _path_for_tools(path: Path) -> str:
    """Forward slashes on Windows: Git for Windows' ``sh`` and ``ssh`` read them reliably."""
    return path.as_posix() if os.name == "nt" else str(path)


def ssh_command(key: Path, known_hosts: Path, config: Path) -> str:
    """``GIT_SSH_COMMAND``: this machine's key only, sync's own host keys, no user configuration.

    Git runs the value through a shell, so every word is quoted (the macOS state path has a
    space). Values inside ``-o`` are quoted again for ssh, whose option parser splits on spaces.
    """
    hosts = _path_for_tools(known_hosts).replace('"', '')
    words = ["ssh", "-F", _path_for_tools(config), "-i", _path_for_tools(key),
             "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=yes", "-o", f'UserKnownHostsFile="{hosts}"',
             "-o", f'GlobalKnownHostsFile="{hosts}"', "-o", "UpdateHostKeys=no",
             "-o", "CheckHostIP=no", "-o", "ConnectTimeout=20",
             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    return " ".join(shlex.quote(word) for word in words)


def config_value(value: str) -> str:
    """A double-quoted git config value."""
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("configuration values must not contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def isolated_config(*, name: str, email: str, hooks: Path) -> str:
    """The whole ``gitconfig`` sync uses instead of the user's global and system files."""
    lines = [
        "# Written by Agents-Core user sync; the user's global and system git config are not read.",
        "[user]", f"\tname = {config_value(name)}", f"\temail = {config_value(email)}",
        "[core]", f"\thooksPath = {config_value(_path_for_tools(hooks))}",
        "\tautocrlf = false", "\tsafecrlf = false", "\tsymlinks = false", "\tfileMode = false",
        "\tquotePath = false", "\tfsmonitor = false", "\tuntrackedCache = false",
    ]
    if os.name == "nt":
        lines.append("\tlongpaths = true")
    lines += ["[commit]", "\tgpgSign = false", "[tag]", "\tgpgSign = false",
              "[gc]", "\tautoDetach = false", "[maintenance]", "\tauto = false",
              "[transfer]", "\tfsckObjects = true", "[credential]", "\thelper = ",
              "[advice]", "\tdetachedHead = false", "[init]", "\tdefaultBranch = main"]
    return "\n".join(lines) + "\n"


class Git:
    """Runs git on one library with the isolation described in the module docstring."""

    def __init__(self, work_tree: Path, *, config: Path, ssh: str | None, allow_file: bool = False,
                 executable: str = "git"):
        self.work_tree = Path(work_tree)
        self.git_dir = self.work_tree / ".git"
        self.config, self.ssh, self.allow_file, self.executable = config, ssh, allow_file, executable

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        environment = clean_environment()
        environment.update(GIT_CONFIG_GLOBAL=str(self.config), GIT_CONFIG_NOSYSTEM="1",
                           GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1", GIT_OPTIONAL_LOCKS="0",
                           GIT_ALLOW_PROTOCOL="ssh:https:file" if self.allow_file else "ssh:https")
        if self.ssh:
            environment["GIT_SSH_COMMAND"] = self.ssh
        environment.update(extra or {})
        return environment

    def run(self, *args: str, input: bytes | None = None, network: bool = False, check: bool = True,
            index: Path | None = None, repository: bool = True) -> subprocess.CompletedProcess:
        """Run ``git <args>``; raises ``GitError`` when it fails and ``check`` is set.

        ``index`` selects a temporary index file; ``repository=False`` omits ``--git-dir`` and
        ``--work-tree`` for commands such as ``init`` and ``version``.
        """
        command = [self.executable]
        if repository:
            command += [f"--git-dir={self.git_dir}", f"--work-tree={self.work_tree}"]
        command += list(args)
        extra = {"GIT_INDEX_FILE": str(index)} if index else None
        timeout = NETWORK_TIMEOUT if network else LOCAL_TIMEOUT
        try:
            result = subprocess.run(command, input=input, capture_output=True, timeout=timeout,
                                    env=self.environment(extra), cwd=self.work_tree if self.work_tree.is_dir() else None,
                                    **no_window())
        except subprocess.TimeoutExpired:
            raise GitError(args[0], "timeout" if network else "local", f"timed out after {timeout} s") from None
        except OSError as error:
            raise GitError(args[0], "local", f"git could not start: {error}") from None
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace")
            raise GitError(args[0], classify(stderr) if network else "local", stderr, result.returncode)
        return result

    def text(self, *args: str, **options) -> str:
        return self.run(*args, **options).stdout.decode("utf-8", "replace").strip()


def no_window() -> dict:
    """No console window for git on Windows when a pythonw scheduler run starts it."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}
