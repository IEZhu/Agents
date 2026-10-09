"""Service managers: how the OS starts, keeps and stops the shared service (#194, #195).

``Controller`` drains the service, holds ``control.lock`` and waits for ``.daemon.lock``;
a manager only talks to the OS scheduler:

* ``install()`` records the definition so that the service starts at the next login;
* ``write(probation)`` records the definition, ``serve`` with ``--probation=<nonce>`` while
  a controller transaction verifies a restart (`src.daemon.update.probation`);
* ``start(probation)`` writes it and starts the service now;
* ``stop()`` stops it for this login session (after the controller's drain); it starts again at
  the next login;
* ``loaded()`` says whether the scheduler keeps it running;
* ``remove()`` deletes the definition.

``Launchd`` is a LaunchAgent with RunAtLoad and KeepAlive (macOS). ``TaskScheduler`` is a
hidden task of the current user (Windows) with a logon trigger and a time trigger every
minute: ``RestartOnFailure`` restarts a task only when it fails to start, not after its
process exits, so the repetition restarts a stopped or killed service within a minute, and
``IgnoreNew`` makes it a no-op while the service runs. ``stop`` disables the repetition and
ends the task; the logon trigger then starts the service at the next login, which turns the
repetition on again (``ensure_keep_alive``), as launchd loads a LaunchAgent at each login.
"""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

from src import windows_tasks
from .state import atomic_private

KEEP_ALIVE_MINUTES = 1
COMMAND_TIMEOUT = 60  # seconds for one schtasks command
# The platform whose scheduler runs the service; tests pin it (tests/conftest.py).
PLATFORM = sys.platform


def run_command(argv, *, input=None) -> subprocess.CompletedProcess:
    """No shell, no console window, never the caller's stdin (an MCP server's pipe); bytes out."""
    given = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(list(argv), capture_output=True, timeout=COMMAND_TIMEOUT, check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **given)


# What TaskScheduler runs its commands with; tests replace it (tests/conftest.py).
RUNNER = run_command


def manager(controller):
    return TaskScheduler(controller) if PLATFORM == "win32" else Launchd(controller)


def serve_arguments(controller, probation=None) -> list[str]:
    """``serve`` after the interpreter, as the OS scheduler starts it."""
    arguments = ["-m", "src.daemon", "--state", str(controller.directory), "serve"]
    # One argument: a URL-safe nonce may start with "-", which argparse would read as an option.
    if probation:
        arguments.append("--probation=" + probation)
    return arguments


class Launchd:
    """A LaunchAgent in ``~/Library/LaunchAgents``. It reads ``controller.plist`` and calls
    ``controller.launchctl``, which tests replace."""

    def __init__(self, controller):
        self.controller = controller

    @property
    def path(self) -> Path:
        return Path.home() / "Library/LaunchAgents" / (self.controller.label + ".plist")

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.controller.label}"

    def install(self) -> None:
        """The plist alone: launchd loads it (RunAtLoad) at the next login, ``start`` loads it now."""
        self.write()

    def write(self, probation=None) -> None:
        config = self.controller.config
        payload = {"Label": self.controller.label,
                   "ProgramArguments": [config["python"], *serve_arguments(self.controller, probation)],
                   "WorkingDirectory": config["installation"],
                   "EnvironmentVariables": {"PATH": config["path"], "PYTHONUNBUFFERED": "1"},
                   "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
                   "ThrottleInterval": 15, "ProcessType": "Interactive",
                   "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
        atomic_private(self.controller.plist, plistlib.dumps(payload).decode())

    def loaded(self) -> bool:
        return self.controller.launchctl("print", self.target, check=False).returncode == 0

    def start(self, probation=None) -> None:
        self.write(probation)
        if not self.loaded():
            self.controller.launchctl("bootstrap", self.domain, str(self.controller.plist))
        else:
            self.controller.launchctl("kickstart", self.target)

    def stop(self) -> None:
        result = self.controller.launchctl("bootout", self.target, check=False)
        if result.returncode and self.loaded():
            raise RuntimeError("launchd could not stop the service")

    def remove(self) -> None:
        self.controller.plist.unlink(missing_ok=True)

    def ensure_keep_alive(self) -> None:
        """launchd keeps a loaded LaunchAgent alive by itself."""


class TaskScheduler:
    """A hidden Task Scheduler task of the current user, named after the state directory."""

    def __init__(self, controller, runner=None):
        self.controller = controller
        self.runner = runner or RUNNER

    @property
    def name(self) -> str:
        return "agents-core-daemon-" + self.controller.directory.name

    def _schtasks(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = self.runner(["schtasks", *args])
        if check and result.returncode:
            output = b"\n".join(part for part in (result.stderr, result.stdout) if part)
            message = output.decode("oem" if sys.platform == "win32" else "utf-8", "replace").strip()
            raise RuntimeError(f"schtasks {args[0]} failed: {message[-500:] or f'exit code {result.returncode}'}")
        return result

    def definition(self, probation=None, *, keep_alive=True) -> bytes:
        config = self.controller.config
        command = windows_tasks.windowless(config["python"])
        arguments = serve_arguments(self.controller, probation)
        if any("%" in value for value in (command, config["installation"], *arguments)):
            raise ValueError("Task Scheduler expands %NAME% in paths: move the installation, the interpreter "
                             "and the service directory to paths without '%'")
        return windows_tasks.definition(windows_tasks.Task(
            description="Agents-Core: the shared MCP service", command=command, arguments=tuple(arguments),
            working_directory=config["installation"], interval_minutes=KEEP_ALIVE_MINUTES,
            time_limit="PT0S", priority=5, at_logon=bool(config.get("autostart", True)),
            repeating=keep_alive, user=windows_tasks.windows_user()))

    def install(self) -> None:
        """The task with only its logon trigger on: a registered repetition would start the service
        within a minute, while ``install`` leaves that to ``start`` or the next login."""
        self.write(keep_alive=False)

    def write(self, probation=None, *, keep_alive=True) -> None:
        """Register the task, replacing one of the same name; a running instance keeps running."""
        document = self.definition(probation, keep_alive=keep_alive)
        descriptor, path = tempfile.mkstemp(prefix=self.name + "-", suffix=".xml", dir=self.controller.directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(document)
            self._schtasks("/Create", "/XML", path, "/TN", self.name, "/F")
        finally:
            os.unlink(path)

    def triggers(self) -> dict[str, bool] | None:
        """The enabled state of the registered task's triggers, or None without a task."""
        query = self._schtasks("/Query", "/TN", self.name, "/XML", check=False)
        if query.returncode:
            return None
        return windows_tasks.enabled_triggers(windows_tasks.schtasks_text(query.stdout or b""))

    def loaded(self) -> bool:
        return bool((self.triggers() or {}).get("TimeTrigger"))

    def start(self, probation=None) -> None:
        self.write(probation)
        self._schtasks("/Run", "/TN", self.name)

    def stop(self) -> None:
        if self.triggers() is None:
            return
        self.write(keep_alive=False)
        # /End terminates the process: no shutdown runs, and log writes still queued are lost. The
        # service exits by itself when asked, as on launchd's SIGTERM; /End is the fallback for one
        # that does not answer. Without a running instance /End fails, which changes nothing.
        if not self.controller.exit_gracefully():
            self._schtasks("/End", "/TN", self.name, check=False)

    def remove(self) -> None:
        """Delete the task; without one, /Delete fails and changes nothing. A task left behind
        would start ``serve`` every minute for a service that is no longer installed."""
        result = self._schtasks("/Delete", "/TN", self.name, "/F", check=False)
        if result.returncode and self.triggers() is not None:
            raise RuntimeError("Task Scheduler could not delete the service task")

    def ensure_keep_alive(self) -> None:
        """Turn the repetition on again after a ``stop``, when the logon trigger started the service."""
        found = self.triggers()
        if found is not None and not found.get("TimeTrigger"):
            self.write()
