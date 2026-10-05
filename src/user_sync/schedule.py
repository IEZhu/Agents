"""Periodic sync runs for the current user, for installations without the daemon (#168).

``enable`` installs one job per installation that starts ``python -m src.user_sync
run`` (one cycle, then exit) in the installation root every few minutes;
``disable`` removes it and ``status`` reports it. The sync lock keeps a run from
overlapping any other runner, so a scheduler only has to start the process. Each
OS gets its own scheduler:

* Windows: a hidden Task Scheduler task, started at logon and from now on and
  repeated, that runs ``pythonw.exe`` (no console window) with least privilege,
  only while the user is logged on, so no password is stored;
* Linux: a systemd user timer, or one crontab line where ``systemctl --user``
  cannot reach a user manager (containers, sessions without a bus);
* macOS: a LaunchAgent like the auto-updater's (`src.daemon.autoupdate`), unless
  the daemon is installed for this installation: then the daemon runs the sync
  loop (#167).

Enabling again leaves one job with the new interval; disabling twice is not an
error, and nothing stays behind. Every OS command goes through an injectable
runner as an argument list, never a shell string, and every file location is a
parameter, so tests never touch the real scheduler. Paths that a scheduler would
misread are refused rather than quoted wrongly: a line break anywhere, ``%`` in a
crontab line or a Task Scheduler path (cron ends the command there, Task
Scheduler expands ``%NAME%``), and a systemd working directory that ends with
whitespace or a backslash.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Callable, Sequence
from xml.etree import ElementTree
from xml.parsers.expat import ExpatError

from src.daemon.state import atomic_private, state_dir

DEFAULT_INTERVAL = 5  # minutes
MIN_INTERVAL, MAX_INTERVAL = 1, 60
RUN_TIME_LIMIT = 10  # minutes; Task Scheduler and systemd stop a run that hangs longer
COMMAND_TIMEOUT = 60  # seconds for one scheduler command
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
WRITTEN_BY = "# Written by Agents-Core (src/user_sync/schedule.py); `schedule disable` removes it.\n"
_NO_CRONTAB = re.compile(r"no crontab|no such file", re.I)  # cronie, Debian, BSD and BusyBox wording

Runner = Callable[..., subprocess.CompletedProcess]


class DaemonRunsSync(RuntimeError):
    """The daemon is installed for this installation and runs the sync loop itself (#167)."""


class CronUnavailable(RuntimeError):
    """``crontab`` is missing, or the user's crontab cannot be read and so must not be rewritten."""


def run_command(argv: Sequence[str], *, input: bytes | None = None) -> subprocess.CompletedProcess:
    """The default runner: no shell, bytes in and out; a non-zero exit is returned, not raised."""
    stdin = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(list(argv), capture_output=True, timeout=COMMAND_TIMEOUT, check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **stdin)


def installation_id(installation: str | Path) -> str:
    """16 hex digits of the SHA-256 of the resolved installation root, as in `src.daemon.state.state_dir`."""
    return hashlib.sha256(str(Path(installation).resolve()).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Job:
    """What every backend starts: ``python -m src.user_sync run`` in the installation root."""
    installation: str
    python: str
    ident: str

    @property
    def name(self) -> str:
        return "agents-core-sync-" + self.ident

    @property
    def argv(self) -> list[str]:
        return [self.python, "-m", "src.user_sync", "run"]


def _call(runner: Runner, argv: list[str], *, input: bytes | None = None,
          check: bool = True) -> subprocess.CompletedProcess:
    result = runner(argv, input=input)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(argv[:3])} failed: {_message(result)}")
    return result


def _message(result: subprocess.CompletedProcess) -> str:
    output = b"\n".join(part for part in (result.stderr, result.stdout) if part)
    text = output.decode("oem" if sys.platform == "win32" else "utf-8", "replace").strip()
    return text[-500:] or f"exit code {result.returncode}"


def _report(backend: str, name: str, scheduled: bool, interval: int | None = None, **extra) -> dict:
    return {"backend": backend, "name": name, "scheduled": bool(scheduled), "interval_minutes": interval, **extra}


class LaunchAgent:
    """A background LaunchAgent like the auto-updater's; refused while the daemon is installed."""
    backend = "launchd"

    def __init__(self, job: Job, runner: Runner, *, directory: str | Path | None = None,
                 service_dir: str | Path | None = None, path: str = ""):
        self.job, self.runner, self.path = job, runner, path
        self.label = f"local.agents-core.{job.ident}.sync"
        self.plist = Path(directory or Path.home() / "Library/LaunchAgents") / (self.label + ".plist")
        self.service_dir = Path(service_dir) if service_dir else state_dir(job.installation)
        uid = os.getuid() if hasattr(os, "getuid") else 0  # the fake-runner tests also run on Windows
        self.domain = f"gui/{uid}"
        self.target = f"{self.domain}/{self.label}"

    def daemon_installed(self) -> bool:
        return (self.service_dir / "service.json").is_file()

    def _launchctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return _call(self.runner, ["/bin/launchctl", *args], check=check)

    def _loaded(self) -> bool:
        return self._launchctl("print", self.target, check=False).returncode == 0

    def payload(self, minutes: int) -> dict:
        payload = {"Label": self.label, "ProgramArguments": self.job.argv,
                   "WorkingDirectory": self.job.installation,
                   "StartInterval": minutes * 60, "RunAtLoad": False,
                   "ProcessType": "Background", "LowPriorityIO": True,
                   "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
        if self.path:
            payload["EnvironmentVariables"] = {"PATH": self.path}
        return payload

    def enable(self, minutes: int) -> dict:
        if self.daemon_installed():
            raise DaemonRunsSync(f"The Agents-Core daemon is installed for {self.job.installation} and runs "
                                 "the sync loop itself; no scheduled job is needed")
        # As in `autoupdate.enable`: any failure leaves the previous plist and job as they were.
        previous = self.plist.read_bytes() if self.plist.exists() else None
        was_loaded = self._loaded()
        if was_loaded:  # launchd reads a changed interval only when it loads the job
            self._launchctl("bootout", self.target, check=False)
            if self._loaded():
                raise RuntimeError("launchd could not unload the current sync job; nothing changed")
        try:
            atomic_private(self.plist, plistlib.dumps(self.payload(minutes)))
            self._launchctl("bootstrap", self.domain, str(self.plist))
        except BaseException:
            if previous is None:
                self.plist.unlink(missing_ok=True)
            else:
                atomic_private(self.plist, previous)
                if was_loaded:
                    self._launchctl("bootstrap", self.domain, str(self.plist), check=False)
            raise
        return self.status()

    def disable(self) -> dict:
        result = self._launchctl("bootout", self.target, check=False)
        if result.returncode and self._loaded():
            raise RuntimeError("launchd could not stop the sync job; it is still scheduled")
        self.plist.unlink(missing_ok=True)
        return self.status()

    def status(self) -> dict:
        try:
            interval = plistlib.loads(self.plist.read_bytes())["StartInterval"] // 60
        except (OSError, ValueError, KeyError, TypeError, ExpatError):
            interval = None
        report = _report(self.backend, self.label, self._loaded(), interval)
        if self.daemon_installed():
            report.update(daemon=True, note="the daemon runs the sync loop for this installation; "
                                            "no scheduled job is needed")
        return report


def _systemd_user_dir() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    return (Path(configured) if os.path.isabs(configured) else Path.home() / ".config") / "systemd" / "user"


def _unit_quoted(value: str) -> str:
    """A double-quoted unit file word: C escapes, and ``%%`` because ``%`` starts a specifier."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _exec_word(value: str) -> str:
    """A word of ``ExecStart=``, which also expands ``$NAME``: ``$$`` keeps a dollar sign."""
    return _unit_quoted(value.replace("$", "$$"))


class SystemdTimer:
    """A systemd user timer and the oneshot service it starts."""
    backend = "systemd"

    def __init__(self, job: Job, runner: Runner, *, directory: str | Path | None = None, path: str = ""):
        self.job, self.runner, self.path = job, runner, path
        self.directory = Path(directory) if directory else _systemd_user_dir()
        self.service = self.directory / (job.name + ".service")
        self.timer = self.directory / (job.name + ".timer")
        self.link = self.directory / "timers.target.wants" / self.timer.name  # made by `systemctl enable`

    def _systemctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return _call(self.runner, ["systemctl", "--user", *args], check=check)

    def usable(self) -> bool:
        """Whether ``systemctl --user`` reaches a user manager; containers and sessions without a bus have none."""
        try:
            return self._systemctl("show-environment", check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _active(self) -> bool:
        return self._systemctl("is-active", "--quiet", self.timer.name, check=False).returncode == 0

    def units(self, minutes: int) -> tuple[str, str]:
        """The service and timer files. WorkingDirectory= takes the path as it is, apart from specifiers."""
        if self.job.installation != self.job.installation.strip() or self.job.installation.endswith("\\"):
            # The unit file parser strips trailing whitespace and joins a line ending in "\\" with the next.
            raise ValueError("systemd cannot start a run in a directory whose name ends with whitespace or '\\'")
        service = (WRITTEN_BY + "[Unit]\nDescription=Agents-Core: sync the personal flow library\n\n"
                   "[Service]\nType=oneshot\n"
                   f"WorkingDirectory={self.job.installation.replace('%', '%%')}\n"
                   + (f"Environment={_unit_quoted('PATH=' + self.path)}\n" if self.path else "")
                   + f"ExecStart={' '.join(map(_exec_word, self.job.argv))}\n"
                   f"TimeoutStartSec={RUN_TIME_LIMIT}min\n"
                   "Nice=10\nIOSchedulingClass=idle\n"
                   "StandardOutput=null\nStandardError=null\n")
        # OnActiveSec= is the first run, counted from enable or login; then every interval after the last run.
        timer = (WRITTEN_BY + f"[Unit]\nDescription=Agents-Core: sync the personal flow library every {minutes} min\n\n"
                 f"[Timer]\nOnActiveSec={minutes}min\nOnUnitActiveSec={minutes}min\nAccuracySec=30s\n\n"
                 "[Install]\nWantedBy=timers.target\n")
        return service, timer

    def enable(self, minutes: int) -> dict:
        units = [(path, text.encode("utf-8", "surrogateescape"))
                 for path, text in zip((self.service, self.timer), self.units(minutes))]
        previous = {path: path.read_bytes() for path, _ in units if path.exists()}
        try:
            for path, data in units:
                atomic_private(path, data)
            self._systemctl("daemon-reload")
            self._systemctl("enable", self.timer.name)
            self._systemctl("restart", self.timer.name)  # a changed interval applies now; starts a stopped timer
        except BaseException:
            if previous:
                for path, _ in units:
                    if path in previous:
                        atomic_private(path, previous[path])
                    else:
                        path.unlink(missing_ok=True)
                self._systemctl("daemon-reload", check=False)
            else:
                self._remove(usable=True)
            raise
        return self.status(usable=True)

    def _remove(self, usable: bool) -> None:
        if usable:
            self._systemctl("disable", "--now", self.timer.name, check=False)
        # Without a reachable manager the enable link is removed by hand, so nothing dangles.
        for path in (self.link, self.timer, self.service):
            path.unlink(missing_ok=True)
        if usable:
            self._systemctl("daemon-reload", check=False)
            self._systemctl("reset-failed", self.timer.name, self.service.name, check=False)

    def disable(self, usable: bool) -> dict:
        self._remove(usable)
        if usable and self._active():
            raise RuntimeError("systemd still runs the sync timer")
        return self.status(usable)

    def status(self, usable: bool) -> dict:
        try:
            found = re.search(r"^OnUnitActiveSec=(\d+)min$", self.timer.read_text(encoding="utf-8"), re.M)
        except (OSError, ValueError):
            found = None
        scheduled = self.timer.exists() and (self._active() if usable else os.path.lexists(self.link))
        return _report(self.backend, self.timer.name, scheduled, int(found[1]) if found else None)


class Cron:
    """One crontab line marked with a trailing comment; every other line stays byte for byte."""
    backend = "cron"

    def __init__(self, job: Job, runner: Runner):
        self.job, self.runner = job, runner
        self.marker = os.fsencode(" # " + job.name)  # a shell comment: cron passes it to sh, which ignores it

    def _crontab(self, *args: str, input: bytes | None = None) -> subprocess.CompletedProcess:
        try:
            return _call(self.runner, ["crontab", *args], input=input, check=False)
        except FileNotFoundError:
            raise CronUnavailable("crontab is not installed") from None

    def _read(self) -> list[bytes]:
        result = self._crontab("-l")
        if result.returncode == 0:
            return result.stdout.splitlines(keepends=True)
        if _NO_CRONTAB.search(_message(result)):
            return []  # the user has no crontab yet
        raise CronUnavailable(f"crontab -l failed, so the crontab is left as it is: {_message(result)}")

    def _write(self, lines: list[bytes]) -> None:
        table = b"".join(lines)
        result = self._crontab("-", input=table) if table else self._crontab("-r")
        if result.returncode and (table or not _NO_CRONTAB.search(_message(result))):
            raise RuntimeError(f"crontab {'-' if table else '-r'} failed: {_message(result)}")

    def _ours(self, line: bytes) -> bool:
        return line.rstrip().endswith(self.marker)

    def entry(self, minutes: int) -> bytes:
        if any("%" in value for value in (self.job.installation, self.job.python)):
            raise ValueError("cron ends a command at '%': move the installation and interpreter to paths without it")
        # A step that does not divide 60 restarts at each full hour, which shortens one gap an hour.
        when = "0 * * * *" if minutes == 60 else f"*/{minutes} * * * *"
        command = " ".join(["cd", shlex.quote(self.job.installation), "&&", "exec",
                            *map(shlex.quote, self.job.argv), ">/dev/null", "2>&1"])
        return os.fsencode(f"{when} {command}") + self.marker + b"\n"

    def enable(self, minutes: int) -> dict:
        entry, lines = self.entry(minutes), self._read()
        updated, placed = [], False
        for line in lines:  # an earlier line of ours is replaced in place, duplicates dropped
            if not self._ours(line):
                updated.append(line)
            elif not placed:
                updated.append(entry)
                placed = True
        if not placed:
            if updated and not updated[-1].endswith((b"\n", b"\r")):
                updated.append(b"\n")  # end the last line, or cron would read ours as part of it
            updated.append(entry)
        if updated != lines:
            self._write(updated)
        return self.status()

    def disable(self) -> dict:
        lines = self._read()
        kept = [line for line in lines if not self._ours(line)]
        if kept != lines:
            self._write(kept)
        return self.status()

    def status(self) -> dict:
        ours = [line for line in self._read() if self._ours(line)]
        found = re.match(rb"\s*(?:\*/(\d+)|0) \* \* \* \* ", ours[0]) if ours else None
        interval = (int(found[1]) if found[1] else 60) if found else None
        return _report(self.backend, self.job.name, bool(ours), interval)


class SystemdOrCron:
    """A systemd user timer where ``systemctl --user`` works, else a crontab line; never both.

    Enabling with one backend removes a job the other one holds from an earlier
    enable, for example after a container gained a user manager; disabling removes
    both. A timer that a manager in another session still runs stops there at its
    next reload, once its files are gone.
    """

    def __init__(self, systemd: SystemdTimer, cron: Cron):
        self.systemd, self.cron = systemd, cron

    def _without_cron(self) -> None:
        try:
            self.cron.disable()
        except CronUnavailable:
            pass  # no crontab program, or a crontab this user cannot read: no line of ours either

    def enable(self, minutes: int) -> dict:
        if self.systemd.usable():
            report = self.systemd.enable(minutes)
            self._without_cron()
            return report
        try:
            report = self.cron.enable(minutes)
        except CronUnavailable as error:
            raise CronUnavailable(f"systemctl --user cannot reach a user manager, and {error}") from None
        self.systemd.disable(usable=False)
        return report

    def disable(self) -> dict:
        self.systemd.disable(self.systemd.usable())
        self._without_cron()
        return self.status()

    def status(self) -> dict:
        usable = self.systemd.usable()
        systemd = self.systemd.status(usable)
        if systemd["scheduled"] or self.systemd.timer.exists():
            return systemd
        try:
            cron = self.cron.status()
        except CronUnavailable:
            cron = _report(self.cron.backend, self.cron.job.name, False)
        return systemd if usable and not cron["scheduled"] else cron


def _windowless(python: str) -> str:
    """``pythonw.exe`` next to the interpreter when it exists, so no console window opens on each run."""
    candidate = Path(python).with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else python


def _windows_user() -> str | None:
    user, domain = os.environ.get("USERNAME"), os.environ.get("USERDOMAIN")
    return (f"{domain}\\{user}" if domain else user) if user else None


def _schtasks_text(data: bytes) -> str:
    """``schtasks`` output: UTF-16 after a byte order mark, otherwise ASCII-compatible."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    return data.replace(b"\x00", b"").decode("utf-8", "replace")


class TaskScheduler:
    """A hidden Task Scheduler task of the current user: from logon and from now on, every few minutes."""
    backend = "task-scheduler"

    def __init__(self, job: Job, runner: Runner, *, temp_dir: str | Path | None = None):
        self.job, self.runner, self.temp_dir = job, runner, temp_dir
        self.command = _windowless(job.python)
        self.user = _windows_user()

    def _schtasks(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return _call(self.runner, ["schtasks", *args], check=check)

    def definition(self, minutes: int) -> bytes:
        """The task as Task Scheduler XML, in UTF-16 as ``schtasks /XML`` reads it."""
        if any("%" in value for value in (self.command, self.job.installation)):
            raise ValueError("Task Scheduler expands %NAME% in paths: move the installation and interpreter "
                             "to paths without '%'")
        repeat = "PT1H" if minutes == 60 else f"PT{minutes}M"
        task = ElementTree.Element("Task", {"version": "1.2", "xmlns": TASK_NAMESPACE})

        def add(parent: ElementTree.Element, tag: str, text: str | None = None, **attributes: str):
            node = ElementTree.SubElement(parent, tag, attributes)
            node.text = text
            return node

        add(add(task, "RegistrationInfo"), "Description", "Agents-Core: sync the personal flow library")
        triggers = add(task, "Triggers")
        # The logon trigger starts the runs at each logon, the time trigger now rather than
        # at the next logon. Without a Duration, a repetition never ends.
        for kind in ("LogonTrigger", "TimeTrigger"):
            trigger = add(triggers, kind)
            repetition = add(trigger, "Repetition")
            add(repetition, "Interval", repeat)
            add(repetition, "StopAtDurationEnd", "false")
            if kind == "TimeTrigger":
                add(trigger, "StartBoundary", datetime.now().replace(microsecond=0).isoformat())
            add(trigger, "Enabled", "true")
            if kind == "LogonTrigger" and self.user:
                add(trigger, "UserId", self.user)  # this user's logon: no administrator rights needed
        principal = add(add(task, "Principals"), "Principal", id="Author")
        if self.user:
            add(principal, "UserId", self.user)
        add(principal, "LogonType", "InteractiveToken")  # only while logged on; no stored password
        add(principal, "RunLevel", "LeastPrivilege")
        settings = add(task, "Settings")
        for tag, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                           ("StopIfGoingOnBatteries", "false"), ("AllowHardTerminate", "true"),
                           ("StartWhenAvailable", "false"), ("RunOnlyIfNetworkAvailable", "false")):
            add(settings, tag, value)
        idle = add(settings, "IdleSettings")
        add(idle, "StopOnIdleEnd", "false")
        add(idle, "RestartOnIdle", "false")
        for tag, value in (("AllowStartOnDemand", "true"), ("Enabled", "true"), ("Hidden", "true"),
                           ("RunOnlyIfIdle", "false"), ("WakeToRun", "false"),
                           ("ExecutionTimeLimit", f"PT{RUN_TIME_LIMIT}M"), ("Priority", "7")):
            add(settings, tag, value)
        execute = add(add(task, "Actions", Context="Author"), "Exec")
        add(execute, "Command", f'"{self.command}"')  # quoted for spaces; a Windows path cannot hold '"'
        add(execute, "Arguments", " ".join(self.job.argv[1:]))
        add(execute, "WorkingDirectory", self.job.installation)  # never quoted: "Start in" rejects quotes
        ElementTree.indent(task)
        text = '<?xml version="1.0" encoding="UTF-16"?>\n' + ElementTree.tostring(task, encoding="unicode") + "\n"
        return text.encode("utf-16")

    def _query(self) -> subprocess.CompletedProcess:
        return self._schtasks("/Query", "/TN", self.job.name, "/XML", check=False)

    def enable(self, minutes: int) -> dict:
        document = self.definition(minutes)
        descriptor, path = tempfile.mkstemp(prefix=self.job.name + "-", suffix=".xml", dir=self.temp_dir)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(document)
            # /F replaces a task of the same name, so enabling again leaves one task.
            self._schtasks("/Create", "/XML", path, "/TN", self.job.name, "/F")
        finally:
            os.unlink(path)
        return self.status()

    def disable(self) -> dict:
        if self._query().returncode == 0:
            result = self._schtasks("/Delete", "/TN", self.job.name, "/F", check=False)
            if result.returncode and self._query().returncode == 0:
                raise RuntimeError(f"Task Scheduler could not delete the sync task: {_message(result)}")
        return self.status()

    def status(self) -> dict:
        query = self._query()
        interval = None
        if query.returncode == 0:
            found = re.search(r"<Interval>PT(?:(\d+)H)?(?:(\d+)M)?</Interval>", _schtasks_text(query.stdout or b""))
            if found:
                interval = int(found[1] or 0) * 60 + int(found[2] or 0) or None
        return _report(self.backend, self.job.name, query.returncode == 0, interval)


def scheduler(*, installation: str | Path | None = None, python: str | None = None, platform: str | None = None,
              runner: Runner | None = None, launch_agents_dir: str | Path | None = None,
              systemd_dir: str | Path | None = None, temp_dir: str | Path | None = None,
              service_dir: str | Path | None = None, path: str | None = None):
    """The scheduler backend of this OS for one installation.

    ``installation`` defaults to the checkout that holds this file, ``python`` to
    the running interpreter and ``platform`` to ``sys.platform``. ``runner(argv,
    input=None)`` runs every command and returns a ``subprocess.CompletedProcess``
    with bytes output, as `run_command` does. The other options move files:
    ``launch_agents_dir`` (macOS), ``systemd_dir`` (Linux), ``temp_dir`` (the
    Windows task XML) and ``service_dir``, the daemon's state directory whose
    ``service.json`` marks the daemon as installed (`src.daemon.state.state_dir`).
    ``path`` is the ``PATH`` of the runs on macOS and with systemd, by default this
    process's, so they find the same ``git`` and ``ssh``; an empty one sets none.
    Cron and Task Scheduler runs get the user's own.
    """
    root = Path(installation or Path(__file__).resolve().parents[2]).resolve()
    interpreter = python or sys.executable
    if not interpreter:
        raise RuntimeError("No Python interpreter to schedule; pass python=")
    path = (os.environ.get("PATH") or os.defpath) if path is None else path
    job = Job(str(root), os.path.abspath(interpreter), installation_id(root))
    if any(character in value for value in (job.installation, job.python, path) for character in "\r\n"):
        raise ValueError("A path with a line break cannot be scheduled")
    runner = runner or run_command
    platform = platform or sys.platform
    if platform == "win32":
        return TaskScheduler(job, runner, temp_dir=temp_dir)
    if platform == "darwin":
        return LaunchAgent(job, runner, directory=launch_agents_dir, service_dir=service_dir, path=path)
    return SystemdOrCron(SystemdTimer(job, runner, directory=systemd_dir, path=path), Cron(job, runner))


def _minutes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_INTERVAL <= value <= MAX_INTERVAL:
        raise ValueError(f"The interval must be a whole number of minutes from {MIN_INTERVAL} to {MAX_INTERVAL}")
    return value


def enable(interval_minutes: int = DEFAULT_INTERVAL, **options) -> dict:
    """Install the periodic run, or replace it with this interval; ``options`` as for `scheduler`."""
    minutes = _minutes(interval_minutes)
    return scheduler(**options).enable(minutes)


def disable(**options) -> dict:
    """Remove the periodic run; when there is none, nothing happens."""
    return scheduler(**options).disable()


def status(**options) -> dict:
    """``backend``, ``name``, ``scheduled`` and ``interval_minutes`` of the periodic run."""
    return scheduler(**options).status()


def main(argv: Sequence[str] | None = None) -> int:
    """``schedule enable [--interval MINUTES] [--python PATH] | disable | status``; prints JSON."""
    parser = argparse.ArgumentParser(prog="python -m src.user_sync schedule",
                                     description="Run the library sync periodically without the daemon")
    parser.add_argument("action", choices=["enable", "disable", "status"])
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"minutes between runs, {MIN_INTERVAL}-{MAX_INTERVAL} (default {DEFAULT_INTERVAL})")
    parser.add_argument("--python", help="interpreter of the runs (default: this one)")
    args = parser.parse_args(argv)
    try:
        if args.action == "enable":
            result = enable(args.interval, python=args.python)
        else:
            result = disable() if args.action == "disable" else status()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
