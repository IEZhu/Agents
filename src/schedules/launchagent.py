"""The scheduler LaunchAgent ``local.agents-core.<id>.scheduler`` (design 10.1).

It runs ``<python> -P -m src.schedules tick`` every ``START_INTERVAL`` seconds in the owner's
session, with the installation as working directory and an explicit environment: ``PATH`` made of
the directories of the tools resolved at installation (``claude``, ``codex``, ``gh``, ``git``,
through `src.executables`) and the system directories, ``PYTHONPATH`` naming the installation
(``-P`` keeps the working directory off ``sys.path``) and ``AGENTS_SERVICE_DIR`` naming the state
directory. The resolved paths go to ``<state_dir>/schedules/tools.json``.

``install`` and ``uninstall`` are idempotent and touch only this label's plist and job: the
daemon's own LaunchAgents (``local.agents-core.<id>`` and ``.updater``) are never named. Every
``launchctl`` call goes through an injectable runner as an argument list.
"""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
from typing import Callable, Sequence
from xml.parsers.expat import ExpatError

from src import executables
from src.daemon.state import atomic_private, private_dir, write_json

from .lease import schedules_dir

START_INTERVAL = 300  # seconds
SUFFIX = ".scheduler"
TOOLS = ("claude", "codex", "gh", "git")
LAUNCHCTL = "/bin/launchctl"
COMMAND_TIMEOUT = 60

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=COMMAND_TIMEOUT, check=False)


def label(directory: str | Path) -> str:
    """``local.agents-core.<id>.scheduler``; ``<id>`` names the state directory, as the daemon's label does."""
    return f"local.agents-core.{Path(directory).name}{SUFFIX}"


def resolve_tools(search: str | None = None) -> dict[str, dict | None]:
    """Absolute paths of the CLIs a run needs, preferring ones this user's processes cannot replace."""
    found = {}
    for name in TOOLS:
        tool = executables.find(name, search)
        found[name] = {"path": tool.path, "protected": tool.protected} if tool else None
    return found


def tool_warnings(tools: dict[str, dict | None]) -> list[str]:
    warnings = []
    for name, tool in tools.items():
        if tool is None:
            warnings.append(f"{name}: not found in PATH or the system directories; real runs will need it")
        elif not tool["protected"]:
            warnings.append(executables.warning(executables.Tool(name, tool["path"], False)))
    return warnings


def explicit_path(tools: dict[str, dict | None]) -> str:
    directories = []
    for tool in tools.values():
        if tool and os.path.dirname(tool["path"]) not in directories:
            directories.append(os.path.dirname(tool["path"]))
    for directory in executables.system_directories():
        if directory not in directories:
            directories.append(directory)
    return os.pathsep.join(directories)


class Scheduler:
    def __init__(self, installation: str | Path, directory: str | Path, python: str, *,
                 runner: Runner = run_command, agents_dir: str | Path | None = None, uid: int | None = None):
        self.installation = Path(installation).resolve()
        self.directory = Path(directory)
        self.python = python
        self.runner = runner
        self.label = label(self.directory)
        self.plist = Path(agents_dir or Path.home() / "Library/LaunchAgents") / (self.label + ".plist")
        uid = uid if uid is not None else (os.getuid() if hasattr(os, "getuid") else 0)
        self.domain = f"gui/{uid}"
        self.target = f"{self.domain}/{self.label}"
        if not self.label.endswith(SUFFIX) or self.plist.name != self.label + ".plist":
            raise ValueError("the scheduler manages only its own LaunchAgent")

    def _launchctl(self, *args: str) -> subprocess.CompletedProcess:
        return self.runner([LAUNCHCTL, *args])

    def loaded(self) -> bool:
        return self._launchctl("print", self.target).returncode == 0

    def payload(self, tools: dict[str, dict | None]) -> dict:
        log = schedules_dir(self.directory) / "scheduler.log"
        return {
            "Label": self.label,
            "ProgramArguments": [self.python, "-P", "-m", "src.schedules", "tick"],
            "WorkingDirectory": str(self.installation),
            "EnvironmentVariables": {"PATH": explicit_path(tools), "PYTHONPATH": str(self.installation),
                                     "AGENTS_SERVICE_DIR": str(self.directory)},
            "StartInterval": START_INTERVAL,
            "RunAtLoad": False,
            "ProcessType": "Background",
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": str(log),
        }

    def install(self, search: str | None = None) -> dict:
        tools = resolve_tools(search)
        content = plistlib.dumps(self.payload(tools))
        private_dir(schedules_dir(self.directory))
        write_json(schedules_dir(self.directory) / "tools.json", tools)
        current = self.plist.read_bytes() if self.plist.is_file() else None
        was_loaded = self.loaded()
        if current == content and was_loaded:
            return {**self.status(), "changed": False, "tools": tools, "warnings": tool_warnings(tools)}
        if was_loaded:  # launchd reads a changed plist only when it loads the job
            self._launchctl("bootout", self.target)
            if self.loaded():
                raise RuntimeError(f"launchd could not unload {self.label}; nothing changed")
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        atomic_private(self.plist, content)
        result = self._launchctl("bootstrap", self.domain, str(self.plist))
        if result.returncode:
            raise RuntimeError(f"launchctl bootstrap {self.label} failed with exit code {result.returncode}")
        return {**self.status(), "changed": True, "tools": tools, "warnings": tool_warnings(tools)}

    def uninstall(self) -> dict:
        changed = self.plist.exists()
        if self.loaded():
            changed = True
            self._launchctl("bootout", self.target)
            if self.loaded():
                raise RuntimeError(f"launchd could not stop {self.label}; it is still scheduled")
        self.plist.unlink(missing_ok=True)
        return {**self.status(), "changed": changed}

    def status(self) -> dict:
        interval = None
        try:
            interval = plistlib.loads(self.plist.read_bytes()).get("StartInterval")
        except (OSError, ValueError, ExpatError, AttributeError):
            pass
        return {"label": self.label, "plist": str(self.plist), "installed": self.plist.is_file(),
                "loaded": self.loaded(), "start_interval": interval}
