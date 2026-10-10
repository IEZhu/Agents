"""Absolute paths of the tools Agents-Core starts: git, ssh-keygen, ssh-keyscan and lsof (#244).

A bare name runs whatever comes first in ``PATH``; on Windows ``CreateProcess`` also searches the
interpreter's directory and the current directory first. The shared service and its updater ran
with the installing shell's ``PATH``, which usually starts with directories the user's own
processes can write (``~/.local/bin``, ``/opt/homebrew/bin``), so a binary placed there ran
inside the service. Every call site therefore asks ``path(name)``:

* ``install`` records each tool in ``service.json`` (``find``): it searches ``PATH`` and then the
  system directories and takes the first candidate the user's own processes cannot replace
  (``protected``) over earlier ones they can. A replaceable one is recorded only when nothing else
  exists, with a warning. A git older than ``MIN_GIT_VERSION`` counts only when no newer one exists.
* The service, its updater and the controller commands use those paths (``configure``) and run
  with ``service_path()``, a fixed list of system directories. A recorded path that is gone,
  replaceable or unusable is looked up again the same way, as is a tool an older installation
  did not record; the service log says so.
* Any other process (a stdio server, ``python -m src.user_sync``) runs what its own ``PATH`` names
  first, as the user's shell would, except from an empty or relative entry (the current
  directory).

``ssh`` stays a name inside ``GIT_SSH_COMMAND`` (`src.user_sync.gitcmd`): git runs it through a
shell with the service's ``PATH`` (``/usr/bin/ssh`` on macOS), and Git for Windows puts its own
``/usr/bin`` first in that shell.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
from functools import lru_cache
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Iterator, Mapping

logger = logging.getLogger(__name__)

TOOLS = ("git", "ssh-keygen", "ssh-keyscan") + (() if os.name == "nt" else ("lsof",))
# The service.json key of each tool; "git" predates the others.
CONFIG_KEYS = {"git": "git", "ssh-keygen": "ssh_keygen", "ssh-keyscan": "ssh_keyscan", "lsof": "lsof"}
# User sync's isolation rests on GIT_CONFIG_GLOBAL, which arrived in git 2.32 (src.user_sync.gitcmd).
MIN_GIT_VERSION = (2, 32)
_PROBE_TIMEOUT = 10
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Tool:
    name: str
    path: str
    protected: bool  # False: the user's own processes could replace it (``protected``)


def _registry(key: str, value: str) -> str | None:
    """A string under ``HKEY_LOCAL_MACHINE``, which only administrators can change; None when absent."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
            return str(winreg.QueryValueEx(handle, value)[0]) or None
    except OSError:
        return None


@lru_cache(maxsize=1)
def _windows_directory() -> str:
    return (_registry(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "SystemRoot")
            or os.environ.get("SystemRoot") or r"C:\Windows")


def system_directories() -> list[str]:
    """Where the OS keeps its own tools: searched after ``PATH``, and the whole service ``PATH``."""
    if os.name == "nt":
        root = _windows_directory()
        system32 = os.path.join(root, "System32")
        return [system32, root, os.path.join(system32, "Wbem"),
                os.path.join(system32, "WindowsPowerShell", "v1.0"), os.path.join(system32, "OpenSSH")]
    return ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def service_path() -> str:
    """``PATH`` of the service and its updater: the system directories, never the installer's ``PATH``."""
    return os.pathsep.join(system_directories())


def _left_out_of_service(name: str) -> bool:
    """``MSYSTEM``: Git for Windows puts its own directories in ``PATH`` only when it is unset, and Git
    Bash sets it, so a controller command run there would otherwise start a git whose helpers cannot
    load their DLLs. ``GIT_*`` and ``DEVELOPER_DIR``: they redirect git, or what macOS's
    ``/usr/bin/git`` runs, and reach the service from ``launchctl setenv`` or the user's environment (#279).
    """
    return name in ("MSYSTEM", "DEVELOPER_DIR") or name.startswith("GIT_")


def service_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """``environment`` (default: this process's) with the service's ``PATH`` and without the variables
    of ``_left_out_of_service``."""
    environment = {name: value for name, value in (os.environ if environment is None else environment).items()
                   if not _left_out_of_service(name)}
    environment["PATH"] = service_path()
    return environment


def enter_service_environment() -> None:
    """Give this process (the service, an update) the service's environment (``service_environment``)."""
    for name in [name for name in os.environ if _left_out_of_service(name)]:
        del os.environ[name]
    os.environ["PATH"] = service_path()


def git_environment(repository: str | os.PathLike, environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """``environment`` (default: this process's) for unattended git on the installation's own ``repository`` (#279).

    Without the user's global git configuration, which can run programs (``core.fsmonitor`` on
    ``status``, ``core.hooksPath``, filter drivers) or redirect a fetch (``url.<base>.insteadOf``),
    and without inherited ``GIT_*`` variables. The system configuration stays: it holds Git for
    Windows' credential manager and TLS settings and Apple git's ``osxkeychain``. As command-line
    configuration: ``repository`` as ``safe.directory``, so an installation that relied on a global
    entry keeps working, and the user's line-ending settings (``_line_endings``).

    Git before 2.38 reads ``safe.directory`` only from the system and global configuration, so
    there the global configuration stays, as before #279; only the inherited variables go.
    """
    repository = Path(repository).resolve().as_posix()
    environment = {name: value for name, value in (os.environ if environment is None else environment).items()
                   if not name.startswith("GIT_")}
    if (_installed_git_version() or (0, 0)) < SAFE_DIRECTORY_ON_COMMAND_LINE:
        return environment
    settings = [("safe.directory", repository), *_line_endings(repository)]
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT=str(len(settings)))
    for index, (key, value) in enumerate(settings):
        environment.update({f"GIT_CONFIG_KEY_{index}": key, f"GIT_CONFIG_VALUE_{index}": value})
    return environment


# The first git that reads safe.directory from command-line configuration.
SAFE_DIRECTORY_ON_COMMAND_LINE = (2, 38)
_git_versions: dict[str, tuple[int, int] | None] = {}


def _installed_git_version() -> tuple[int, int] | None:
    """The version of the git ``path`` starts, looked up once per process and path; None when unknown."""
    try:
        git = path("git")
    except FileNotFoundError:
        return None
    if git not in _git_versions:
        _git_versions[git] = _version(git)
    return _git_versions[git]


# How the checkout's line endings were written; without them git would see a clean checkout as modified.
_LINE_ENDINGS = r"^core\.(autocrlf|eol)$"
_line_endings_found: dict[str, list[tuple[str, str]]] = {}


def _line_endings(repository: str) -> list[tuple[str, str]]:
    """``core.autocrlf`` and ``core.eol`` as the user's own git reads them for ``repository``, in order.

    A global ``core.autocrlf=true`` writes CRLF files on checkout; a git that does not know it
    reports every text file as modified, and the updater then refuses to update. These keys run
    nothing; ``git config`` reads the files without starting a program. Looked up once per process.
    """
    if repository not in _line_endings_found:
        try:
            listed = subprocess.run(
                [path("git"), "-C", repository, "config", "--get-regexp", _LINE_ENDINGS], capture_output=True,
                text=True, stdin=subprocess.DEVNULL, timeout=_PROBE_TIMEOUT, creationflags=_NO_WINDOW,
                env={name: value for name, value in os.environ.items() if not name.startswith("GIT_")})
            found = [tuple(line.split(" ", 1)) for line in listed.stdout.splitlines() if " " in line]
        except (OSError, subprocess.SubprocessError):
            found = []
        _line_endings_found[repository] = found
    return _line_endings_found[repository]


def _secure(path: str) -> bool:
    try:
        info = os.stat(path)
    except OSError:
        return False
    return info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _with_ancestors(path: str) -> Iterator[str]:
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # another drive
        return False


@lru_cache(maxsize=1)
def _protected_roots() -> tuple[str, ...]:
    """Windows: the Windows directory and Program Files, from the registry rather than the environment."""
    version = r"SOFTWARE\Microsoft\Windows\CurrentVersion"
    roots = [_windows_directory(),
             *(_registry(version, value) for value in ("ProgramFilesDir", "ProgramFilesDir (x86)", "ProgramW6432Dir"))]
    return tuple(os.path.normcase(os.path.realpath(root)) for root in roots if root)


def protected(path: str) -> bool:
    """Whether the user's own processes cannot replace the file at ``path``.

    POSIX: the file and every directory above it, both where it is found and where its symlinks
    lead, belong to root and are not writable by group or others. Windows, where ``os.access``
    ignores ACLs: it really lies (junctions resolved) under the Windows directory or Program Files,
    which default ACLs leave to administrators. ``find`` records that real path on Windows, so a
    junction the user owns cannot be pointed elsewhere later.
    """
    real = os.path.realpath(path)
    if os.name == "nt":
        real = os.path.normcase(real)
        return any(_inside(real, root) for root in _protected_roots())
    found_in = os.path.dirname(os.path.abspath(path))
    return all(map(_secure, _with_ancestors(real))) and all(map(_secure, _with_ancestors(found_in)))


@lru_cache(maxsize=1)
def _developer_git() -> str | None:
    """macOS: the git that ``/usr/bin/git`` forwards to (Command Line Tools or Xcode), None without one."""
    try:
        # Without DEVELOPER_DIR, which the service drops too (``_left_out_of_service``).
        selected = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=_PROBE_TIMEOUT,
                                  env={name: value for name, value in os.environ.items() if name != "DEVELOPER_DIR"})
    except (OSError, subprocess.SubprocessError):
        return None
    git = os.path.join(selected.stdout.strip(), "usr", "bin", "git")
    return git if selected.returncode == 0 and selected.stdout.strip() and os.path.isfile(git) else None


def _forwards(candidate: str) -> bool:
    return sys.platform == "darwin" and candidate == "/usr/bin/git"


def _usable(candidate: str) -> bool:
    """False for macOS's ``/usr/bin/git`` without the developer tools it forwards to."""
    return not _forwards(candidate) or _developer_git() is not None


def _protected(candidate: str) -> bool:
    """``protected``, and for macOS's ``/usr/bin/git`` also the git it runs."""
    return protected(candidate) and (not _forwards(candidate) or protected(_developer_git()))


def _version(candidate: str) -> tuple[int, int] | None:
    try:
        output = subprocess.run([candidate, "version"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                timeout=_PROBE_TIMEOUT, creationflags=_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)", output)
    return (int(match[1]), int(match[2])) if match else None


def _recent(name: str, candidate: str) -> bool:
    """Whether the candidate is new enough: git ``MIN_GIT_VERSION``; other tools always."""
    return name != "git" or (_version(candidate) or (0, 0)) >= MIN_GIT_VERSION


def _filenames(name: str) -> tuple[str, ...]:
    # subprocess cannot start a .cmd or .bat without a shell.
    return (name + ".exe",) if os.name == "nt" and not os.path.splitext(name)[1] else (name,)


def candidates(name: str, search: str) -> Iterator[str]:
    """Each directory's ``name``, in the order of ``search`` and then the system directories."""
    seen = set()
    for directory in [*search.split(os.pathsep), *system_directories()]:
        directory = directory.strip().strip('"')
        # Empty or relative entries name the current directory.
        if not directory or not os.path.isabs(directory):
            continue
        key = os.path.normcase(os.path.normpath(directory))
        if key in seen:
            continue
        seen.add(key)
        for filename in _filenames(name):
            candidate = os.path.join(directory, filename)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK) and _usable(candidate):
                yield candidate
                break


def find(name: str, search: str | None = None, *, prefer_protected: bool = True) -> Tool | None:
    """The tool ``name`` from ``search`` (default ``PATH``) and the system directories, or None.

    ``prefer_protected``: the first protected candidate that is recent enough, else the first
    recent one, else the first protected one, else the first one; an unprotected git is run to
    learn its version only when no protected one is recent enough. Without it: the first one.
    """
    found = candidates(name, os.environ.get("PATH", "") if search is None else search)
    if not prefer_protected:
        first = next(found, None)
        return Tool(name, first, _protected(first)) if first else None
    found = list(found)
    safe = [candidate for candidate in found if _protected(candidate)]
    for candidate in safe:
        if _recent(name, candidate):
            return Tool(name, _recorded(candidate), True)
    for candidate in found:
        if candidate not in safe and _recent(name, candidate):
            return Tool(name, candidate, False)
    if safe:
        return Tool(name, _recorded(safe[0]), True)
    return Tool(name, found[0], False) if found else None


def _recorded(candidate: str) -> str:
    """Windows: the real path of a protected tool, since a junction the user owns could be pointed elsewhere."""
    return os.path.realpath(candidate) if os.name == "nt" else candidate


def find_all(search: str | None = None) -> dict[str, Tool | None]:
    return {name: find(name, search) for name in TOOLS}


def warning(tool: Tool) -> str:
    """What ``install`` and the service say about a tool the user's own processes could replace."""
    return (f"{tool.name}: {tool.path} can be replaced by this user's processes, and no {tool.name} that only "
            f"an administrator can change was found; install one there, then reinstall the service")


_configured = False
_pins: dict[str, str] = {}
_search: str | None = None
# Tools already looked up, by name (and PATH, outside a service); a missing one is looked up again.
_found: dict[tuple[str, str | None], Tool] = {}


def configure(config: Mapping) -> None:
    """Start tools as the service of ``config`` (its ``service.json``) does, in this process.

    A tool without a usable recorded path is looked up in the installer's ``PATH`` that ``config``
    recorded (``config["path"]``), which is no longer the service's own ``PATH``.
    """
    global _configured, _search
    _configured = True
    _pins.clear()
    for name, key in CONFIG_KEYS.items():
        value = config.get(key)
        if isinstance(value, str) and os.path.isabs(value):
            _pins[name] = value
    _search = config.get("path") if isinstance(config.get("path"), str) else None
    _found.clear()


def reset() -> None:
    """Forget ``configure`` and every lookup (tests)."""
    global _configured, _search
    _configured, _search = False, None
    _pins.clear()
    _found.clear()
    _line_endings_found.clear()
    _git_versions.clear()
    for cached in (_windows_directory, _protected_roots, _developer_git):
        cached.cache_clear()


def _service_tool(name: str) -> Tool | None:
    pinned = _pins.get(name)
    if pinned and os.path.isfile(pinned) and _usable(pinned) and _protected(pinned):
        return Tool(name, pinned, True)
    # Gone, replaceable (an installation from before #244 recorded git from its PATH alone) or
    # unusable: looked up again, the recorded directory first among the unprotected ones.
    search = os.pathsep.join(part for part in (pinned and os.path.dirname(pinned), _search) if part)
    tool = find(name, search)
    if tool and pinned and tool.path != pinned:
        logger.warning("%s: using %s instead of %s, which install recorded", name, tool.path, pinned)
    if tool and not tool.protected:
        logger.warning("%s", warning(tool))
    return tool


def path(name: str) -> str:
    """The absolute path to start ``name`` with; ``FileNotFoundError`` when there is none."""
    key = (name, None) if _configured else (name, os.environ.get("PATH", ""))
    tool = _found.get(key)
    if tool is None:
        tool = _service_tool(name) if _configured else find(name, key[1], prefer_protected=False)
        if tool is None:
            raise FileNotFoundError(errno.ENOENT, f"{name} is neither in PATH nor in the system directories", name)
        _found[key] = tool
    return tool.path


def available(name: str) -> bool:
    try:
        path(name)
    except FileNotFoundError:
        return False
    return True
