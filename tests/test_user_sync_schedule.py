"""Scheduled sync runs without the daemon: Task Scheduler, systemd or cron, and a LaunchAgent.

Every backend runs against a fake OS (`FakeOS`), so these tests change no real
scheduler; the crontab command also runs through real shells, with a fake
interpreter. `test_real_scheduler_round_trip` uses the real scheduler of the
current OS and runs only when AGENTS_TEST_REAL_SCHEDULER=1, as in CI.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from src.daemon.state import state_dir
from src.file_lock import file_lock
from src.user_sync import schedule

PATH = "/opt/homebrew/bin:/usr/bin:/bin"
TASK = {"t": schedule.TASK_NAMESPACE}
# What systemd 256+ refuses in a program path (`string_is_safe`): control characters, quotes, "\", globs.
SYSTEMD_UNSAFE_PROGRAM = set("\"'\\*?[\x7f") | {chr(code) for code in range(32)}


class FakeOS:
    """launchctl, systemctl, crontab and schtasks as small in-memory models of the real tools."""

    def __init__(self, systemd_dir):
        self.calls = []
        self.fail = []              # (tool, verb) entries; each fails the next matching call once
        self.missing = set()        # tools that are not installed
        self.loaded = {}            # launchd: label -> payload of the loaded plist
        self.bus = True             # systemctl --user reaches a user manager
        self.systemd_dir = systemd_dir
        self.units = {}             # systemd: unit name -> file text at the last daemon-reload
        self.enabled, self.active, self.running = set(), set(), {}
        self.table = None           # the crontab as bytes; None: the user has no crontab
        self.tasks = {}             # Task Scheduler: name -> XML as registered
        self.query_encoding = "utf-16"

    def __call__(self, argv, input=None):
        assert isinstance(argv, list) and all(isinstance(arg, str) for arg in argv), argv
        self.calls.append(argv)
        tool = Path(argv[0]).name
        if tool in self.missing:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        verb = argv[2] if tool == "systemctl" else argv[1]
        if (tool, verb) in self.fail:
            self.fail.remove((tool, verb))
            if verb == "restart":  # a failed restart has stopped the unit already
                for unit in argv[3:]:
                    self.active.discard(unit)
                    self.running.pop(unit, None)
            return subprocess.CompletedProcess(argv, 5, b"", f"{tool} {verb}: permission denied\n".encode())
        code, stdout, stderr = getattr(self, tool)(argv[1:], input)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    def launchctl(self, args, input):
        verb, label = args[0], args[-1].rsplit("/", 1)[-1]
        if verb == "bootstrap":
            payload = plistlib.loads(Path(args[2]).read_bytes())
            if payload["Label"] in self.loaded:
                return 5, b"", b"Bootstrap failed: 5: Input/output error\n"
            self.loaded[payload["Label"]] = payload
        elif verb == "bootout":
            if self.loaded.pop(label, None) is None:
                return 3, b"", b"Boot-out failed: 3: No such process\n"
        elif verb == "print":
            return (0 if label in self.loaded else 113), b"", b""
        return 0, b"", b""

    def systemctl(self, args, input):
        assert args[0] == "--user"
        if not self.bus:
            return 1, b"", b"Failed to connect to bus: No medium found\n"
        verb, units = args[1], [arg for arg in args[2:] if not arg.startswith("--")]
        if verb == "daemon-reload":
            self.units = {path.name: path.read_text(encoding="utf-8")
                          for path in self.systemd_dir.glob("*") if path.is_file()}
        elif verb in ("enable", "restart", "disable") and any(unit not in self.units for unit in units):
            return 1, b"", b"Unit file does not exist.\n"
        elif verb == "enable":
            for unit in units:  # a file, not a symlink, so this also runs on Windows
                link = self.systemd_dir / "timers.target.wants" / unit
                link.parent.mkdir(exist_ok=True)
                link.write_text(str(self.systemd_dir / unit))
                self.enabled.add(unit)
        elif verb == "restart":
            self.active.update(units)
            self.running.update({unit: self.units[unit] for unit in units})
        elif verb == "disable":
            for unit in units:
                self.enabled.discard(unit)
                (self.systemd_dir / "timers.target.wants" / unit).unlink(missing_ok=True)
                if "--now" in args:
                    self.active.discard(unit)
                    self.running.pop(unit, None)
        elif verb == "stop":
            if units[0] not in self.active and units[0] not in self.units:
                return 5, b"", b"Failed to stop unit: Unit not loaded.\n"
            self.active.discard(units[0])
            self.running.pop(units[0], None)
        elif verb == "is-active":
            return (0 if units[0] in self.active else 3), b"", b""
        else:
            assert verb in ("show-environment", "reset-failed"), args
        return 0, b"", b""

    def crontab(self, args, input):
        if args == ["-"]:
            self.table = input
        elif self.table is None:
            return 1, b"", b"no crontab for tester\n"
        elif args == ["-r"]:
            self.table = None
        else:
            assert args == ["-l"], args
            return 0, self.table, b""
        return 0, b"", b""

    def schtasks(self, args, input):
        verb, name = args[0], args[args.index("/TN") + 1]
        if verb == "/Create":
            assert args[-1] == "/F"
            document = Path(args[args.index("/XML") + 1]).read_bytes()
            ElementTree.fromstring(document)  # schtasks refuses XML it cannot read
            self.tasks[name] = document
            return 0, b"SUCCESS: The scheduled task has successfully been created.\r\n", b""
        if name not in self.tasks:
            return 1, b"", b"ERROR: The system cannot find the file specified.\r\n"
        if verb == "/Delete":
            del self.tasks[name]
            return 0, b"SUCCESS: The scheduled task was successfully deleted.\r\n", b""
        assert verb == "/Query" and "/XML" in args, args
        return 0, self.tasks[name].decode("utf-16").encode(self.query_encoding), b""



# systemd runs on Linux only; on a Windows host the temporary paths hold backslashes, which the
# unit-file checks rightly refuse.
posix_paths = pytest.mark.skipif(os.name == "nt", reason="systemd unit paths are POSIX paths")

@pytest.fixture
def host(tmp_path, monkeypatch):
    """An installation, an interpreter and sync directories in paths with spaces, a fake OS and a home of its own."""
    home = tmp_path / "home"
    for variable, value in (("HOME", home), ("USERPROFILE", home), ("LOCALAPPDATA", home / "AppData" / "Local"),
                            ("XDG_STATE_HOME", home / ".local" / "state"), ("XDG_CONFIG_HOME", home / ".config")):
        monkeypatch.setenv(variable, str(value))
    monkeypatch.delenv("AGENTS_SERVICE_DIR", raising=False)
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setenv("USERDOMAIN", "HOST")
    installation = tmp_path / "Agents Core" / "checkout"
    installation.mkdir(parents=True)
    python = tmp_path / "Python Home" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    (tmp_path / "Temp Dir").mkdir()
    state, library = tmp_path / "State Dir" / "sync", installation / "flows" / ".user"
    fake = FakeOS(tmp_path / "systemd user")
    options = {"installation": installation, "python": str(python), "state_dir": state, "library": library,
               "runner": fake, "launch_agents_dir": tmp_path / "Launch Agents",
               "systemd_dir": tmp_path / "systemd user", "temp_dir": tmp_path / "Temp Dir",
               "service_dir": tmp_path / "Application Support" / "state", "path": PATH}
    ident = schedule.installation_id(installation)
    argv = [str(python), "-m", "src.user_sync", "--state", str(state.resolve()), "--library", str(library.resolve()),
            "run"]
    return SimpleNamespace(fake=fake, options=options, installation=installation.resolve(), python=str(python),
                           state=str(state.resolve()), library=str(library.resolve()), argv=argv,
                           tmp=tmp_path, ident=ident, name="agents-core-sync-" + ident)


def on(platform, host, action="enable", *args, **overrides):
    return getattr(schedule, action)(*args, platform=platform, **{**host.options, **overrides})


def test_installation_id_names_the_daemon_state_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTS_SERVICE_DIR", raising=False)
    assert schedule.installation_id(tmp_path) == state_dir(tmp_path).name
    assert schedule.installation_id(tmp_path / "sub" / "..") == schedule.installation_id(tmp_path)


@pytest.mark.parametrize("interval", [0, 61, 2.5, True, "5"])
def test_interval_is_a_whole_number_of_minutes_from_1_to_60(host, interval):
    with pytest.raises(ValueError, match="from 1 to 60"):
        on("darwin", host, "enable", interval)
    assert host.fake.calls == []


def test_runs_get_state_and_library_only_when_given(host):
    on("darwin", host, state_dir=None, library=None)
    plist, = (host.tmp / "Launch Agents").iterdir()
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [host.python, "-m", "src.user_sync", "run"]


def test_a_change_while_another_holds_the_lock_is_reported_busy(host):
    lock = schedule.scheduler(platform="darwin", **host.options).lock_path
    # Named after the installation alone: a disable without the state directory takes the same lock.
    assert lock == schedule.scheduler(platform="darwin", **{**host.options, "state_dir": None}).lock_path
    assert lock.is_relative_to(host.tmp / "home")
    with file_lock(lock, blocking=False):
        for action in ("enable", "disable"):
            with pytest.raises(schedule.ScheduleBusy, match="busy"):
                on("darwin", host, action)
        assert on("darwin", host, "status")["scheduled"] is False  # reading takes no lock
    assert not any(call[1] in ("bootstrap", "bootout") for call in host.fake.calls)
    assert on("darwin", host)["scheduled"]


# --- macOS: a LaunchAgent ---------------------------------------------------------------

def label(host):
    return f"local.agents-core.{host.ident}.sync"


def test_launch_agent_runs_one_cycle_in_the_installation_root(host):
    report = on("darwin", host)
    plist = host.tmp / "Launch Agents" / (label(host) + ".plist")
    assert plistlib.loads(plist.read_bytes()) == {
        "Label": label(host), "ProgramArguments": host.argv,
        "WorkingDirectory": str(host.installation), "EnvironmentVariables": {"PATH": PATH},
        "StartInterval": 300, "RunAtLoad": False, "ProcessType": "Background", "LowPriorityIO": True,
        "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
    uid = os.getuid() if hasattr(os, "getuid") else 0
    assert ["/bin/launchctl", "bootstrap", f"gui/{uid}", str(plist)] in host.fake.calls  # one argument with spaces
    assert report == {"backend": "launchd", "name": label(host), "scheduled": True, "interval_minutes": 5}


def test_launch_agent_enabled_twice_is_one_job_with_the_latest_interval(host):
    on("darwin", host, "enable", 5)
    report = on("darwin", host, "enable", 15)
    assert [path.name for path in (host.tmp / "Launch Agents").iterdir()] == [label(host) + ".plist"]
    assert host.fake.loaded[label(host)]["StartInterval"] == 900  # reloaded, so launchd uses the new interval
    assert report["scheduled"] and report["interval_minutes"] == 15
    assert [call[1] for call in host.fake.calls if call[1] in ("bootstrap", "bootout")] == \
        ["bootstrap", "bootout", "bootstrap"]


def test_launch_agent_disable_leaves_nothing_and_twice_is_fine(host):
    on("darwin", host)
    for _ in range(2):
        assert on("darwin", host, "disable") == {"backend": "launchd", "name": label(host),
                                                 "scheduled": False, "interval_minutes": None}
    assert list((host.tmp / "Launch Agents").iterdir()) == [] and host.fake.loaded == {}


@pytest.mark.parametrize("installed", ["service_dir", "default", "AGENTS_SERVICE_DIR", "install --state"])
def test_launch_agent_is_refused_while_the_daemon_runs_the_sync_loop(host, monkeypatch, installed):
    options = {}
    if installed == "service_dir":
        daemon = host.options["service_dir"]
    elif installed == "default":  # where `src.daemon.state.state_dir` puts it
        options["service_dir"] = None
        daemon = state_dir(host.installation)
    else:
        daemon = host.tmp / "custom daemon state"
        if installed == "AGENTS_SERVICE_DIR":
            monkeypatch.setenv("AGENTS_SERVICE_DIR", str(daemon))
        else:  # `python -m src.daemon --state DIR install` records DIR here
            marker = host.installation / "data" / ".shared-service.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"directory": str(daemon)}))
    daemon.mkdir(parents=True)
    (daemon / "service.json").write_text("{}")
    with pytest.raises(schedule.DaemonRunsSync, match="daemon is installed"):
        on("darwin", host, **options)
    assert not (host.tmp / "Launch Agents").exists()
    assert not any(call[1] == "bootstrap" for call in host.fake.calls)
    report = on("darwin", host, "status", **options)
    assert report["daemon"] is True and "daemon runs the sync loop" in report["note"]
    assert Path(report["service_dir"]) == daemon and report["scheduled"] is False


def test_launch_agent_changes_nothing_when_launchd_refuses(host):
    plist = host.tmp / "Launch Agents" / (label(host) + ".plist")
    host.fake.fail.append(("launchctl", "bootstrap"))
    with pytest.raises(RuntimeError, match="bootstrap"):
        on("darwin", host)
    assert not plist.exists() and host.fake.loaded == {}

    on("darwin", host, "enable", 5)
    before = plist.read_bytes()
    # The running job cannot be unloaded: plist and job stay as they were.
    host.fake.fail.append(("launchctl", "bootout"))
    with pytest.raises(RuntimeError, match="could not unload"):
        on("darwin", host, "enable", 15)
    assert plist.read_bytes() == before and host.fake.loaded[label(host)]["StartInterval"] == 300
    # The new job fails to load: the previous plist comes back and is loaded again.
    host.fake.fail.append(("launchctl", "bootstrap"))
    with pytest.raises(RuntimeError, match="bootstrap"):
        on("darwin", host, "enable", 15)
    assert plist.read_bytes() == before and host.fake.loaded[label(host)]["StartInterval"] == 300


def test_launch_agent_disable_keeps_a_job_launchd_still_runs(host):
    on("darwin", host)
    host.fake.fail.append(("launchctl", "bootout"))
    with pytest.raises(RuntimeError, match="still scheduled"):
        on("darwin", host, "disable")
    assert (host.tmp / "Launch Agents" / (label(host) + ".plist")).exists()


# --- Linux: a systemd user timer, or cron ------------------------------------------------

def unit_values(path):
    """key -> value of a unit file the scheduler wrote; no key repeats there."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            assert key not in values, key
            values[key] = value
    return values


def unit_split(value):
    """Words of a unit file value: ``%%`` is ``%``; double-quoted words take C escapes."""
    value, words, at = value.replace("%%", "%"), [], 0
    while at < len(value):
        if value[at] == " ":
            at += 1
            continue
        word = ""
        if value[at] == '"':
            at += 1
            while value[at] != '"':
                if value[at] == "\\":
                    at += 1
                word += value[at]
                at += 1
            at += 1
        else:
            end = value.find(" ", at)
            end = len(value) if end < 0 else end
            word, at = value[at:end], end
        words.append(word)
    return words


def exec_argv(value):
    """The argv systemd runs for ``ExecStart=``: the program path as it is, which systemd 256+
    refuses to hold quotes, backslashes, globs or control characters, then arguments with ``$$``
    as ``$`` (systemd.service(5), "Command lines")."""
    program, *arguments = unit_split(value)
    assert program and not set(program) & SYSTEMD_UNSAFE_PROGRAM, program
    return [program, *(argument.replace("$$", "$") for argument in arguments)]


@posix_paths
def test_systemd_timer_runs_one_cycle_in_the_installation_root(host):
    report = on("linux", host)
    units = host.options["systemd_dir"]
    service, timer = unit_values(units / f"{host.name}.service"), unit_values(units / f"{host.name}.timer")
    assert exec_argv(service["ExecStart"]) == host.argv
    assert service["WorkingDirectory"].replace("%%", "%") == str(host.installation)
    assert unit_split(service["Environment"]) == ["PATH=" + PATH]
    assert service["Type"] == "oneshot" and service["StandardOutput"] == "null"
    # The first run counts from enable (or login), the next ones from the last run.
    assert timer["OnActiveSec"] == timer["OnUnitActiveSec"] == "5min" and timer["WantedBy"] == "timers.target"
    assert host.fake.enabled == host.fake.active == {f"{host.name}.timer"}
    assert report == {"backend": "systemd", "name": f"{host.name}.timer", "scheduled": True, "interval_minutes": 5}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot hold quotes or backslashes")
def test_systemd_quotes_arguments_fully_and_the_program_for_spaces_only(host, tmp_path):
    installation, odd = tmp_path / "my agents 100%", tmp_path / 'it\'s "odd" $HOME \\x %n'
    python = str(tmp_path / "Python 3.11 %h" / "python")
    on("linux", host, installation=installation, python=python, state_dir=odd / "state", library=odd / "library")
    name = "agents-core-sync-" + schedule.installation_id(installation)
    service = unit_values(host.options["systemd_dir"] / f"{name}.service")
    assert exec_argv(service["ExecStart"]) == [python, "-m", "src.user_sync", "--state", str(odd / "state"),
                                               "--library", str(odd / "library"), "run"]
    assert service["WorkingDirectory"].replace("%%", "%") == str(installation.resolve())


@pytest.mark.parametrize("character", ["$", '"', "'", "\\", "*", "?", "["])
def test_systemd_refuses_an_interpreter_path_it_would_refuse_or_expand(host, tmp_path, character):
    if character == "\\" and sys.platform == "win32":
        pytest.skip("a backslash separates Windows path components")
    with pytest.raises(ValueError, match="systemd refuses an interpreter path"):
        on("linux", host, python=f"{tmp_path}/py{character}thon/python")
    assert not host.options["systemd_dir"].exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot end with these characters")
@pytest.mark.parametrize("ending", [" ", "\\"])
def test_systemd_refuses_a_directory_the_unit_file_parser_would_change(host, tmp_path, ending):
    with pytest.raises(ValueError, match="ends with whitespace"):
        on("linux", host, installation=tmp_path / ("agents" + ending))
    assert not host.options["systemd_dir"].exists()


@posix_paths
def test_systemd_enabled_twice_is_one_timer_and_disable_leaves_nothing(host):
    on("linux", host, "enable", 5)
    report = on("linux", host, "enable", 10)
    units = host.options["systemd_dir"]
    assert sorted(path.name for path in units.iterdir()) == \
        [f"{host.name}.service", f"{host.name}.timer", "timers.target.wants"]
    assert report["interval_minutes"] == 10
    assert "OnUnitActiveSec=10min" in host.fake.running[f"{host.name}.timer"]  # restarted with it
    for _ in range(2):
        assert on("linux", host, "disable")["scheduled"] is False
    assert [path for path in units.rglob("*") if path.is_file()] == []
    assert host.fake.units == {} and host.fake.enabled == host.fake.active == set()


@posix_paths
def test_systemd_enable_that_fails_leaves_no_units(host):
    host.fake.fail.append(("systemctl", "enable"))
    with pytest.raises(RuntimeError, match="systemctl --user enable"):
        on("linux", host)
    assert [path for path in host.options["systemd_dir"].rglob("*") if path.is_file()] == []
    assert host.fake.units == {}


@posix_paths
def test_systemd_reenable_that_fails_restores_the_running_timer(host):
    on("linux", host, "enable", 5)
    timer = f"{host.name}.timer"
    host.fake.fail.append(("systemctl", "restart"))  # stops the timer, then fails to start it
    with pytest.raises(RuntimeError, match="systemctl --user restart"):
        on("linux", host, "enable", 10)
    assert "OnUnitActiveSec=5min" in (host.options["systemd_dir"] / timer).read_text(encoding="utf-8")
    assert host.fake.active == host.fake.enabled == {timer}
    assert "OnUnitActiveSec=5min" in host.fake.running[timer]  # running again, with the previous interval
    assert on("linux", host, "status")["interval_minutes"] == 5


def cron_line(host, name=None):
    line, = [line for line in host.fake.table.splitlines() if (name or host.name).encode() in line]
    return line.decode()


def cron_command(line):
    """The crontab command split as sh splits it, and its inner script as /bin/sh splits that."""
    shell, flag, script, name = shlex.split(line.split(None, 5)[5])
    assert (shell, flag) == ("/bin/sh", "-c")
    return shlex.split(script), name


@pytest.mark.parametrize("cause", ["no user bus", "no systemctl"])
def test_cron_line_where_systemctl_cannot_reach_a_user_manager(host, cause):
    if cause == "no user bus":
        host.fake.bus = False
    else:
        host.fake.missing.add("systemctl")
    report = on("linux", host)
    assert report == {"backend": "cron", "name": host.name, "scheduled": True, "interval_minutes": 5}
    line = cron_line(host)
    assert line.split(None, 5)[:5] == ["*/5", "*", "*", "*", "*"]
    script, name = cron_command(line)
    assert name == host.name  # the inner shell's $0 marks the line
    assert script == ["exec", ">/dev/null", "2>&1", "&&", "cd", str(host.installation), "&&",
                      "export", "PATH=" + PATH, "&&", "exec", *host.argv]
    assert not host.options["systemd_dir"].exists()


@pytest.fixture
def short_tmp():
    """A directory with a short path, so that a crontab line with five odd paths stays within cron's limit."""
    directory = Path(tempfile.mkdtemp(dir="/tmp")).resolve()
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="runs the crontab command through POSIX shells")
@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash", "/bin/zsh", "/bin/tcsh", "/bin/csh", "/usr/bin/fish"])
def test_cron_command_runs_the_same_under_any_crontab_shell(host, tmp_path, short_tmp, shell):
    if not os.path.exists(shell):
        pytest.skip(f"{shell} is not installed")
    odd = short_tmp / 'it\'s "odd" $HOME & `co`'
    installation, record = odd / "agents", tmp_path / "record.txt"
    installation.mkdir(parents=True)
    python = odd / "bin" / "python"  # records what the run gets, in place of the interpreter
    python.parent.mkdir()
    python.write_text(f"#!/bin/sh\n{{ pwd; printf '%s\\n' \"$PATH\" \"$@\"; }} > {shlex.quote(str(record))}\n")
    python.chmod(0o755)
    host.fake.bus = False
    path = f"/usr/bin:/bin:{odd}/tools:/c/%SystemRoot%"
    on("linux", host, installation=installation, python=str(python), state_dir=odd / "state",
       library=odd / "library", path=path)
    command = cron_line(host, "agents-core-sync-" + schedule.installation_id(installation)).split(None, 5)[5]
    assert "export" in command
    # cron runs the command with the crontab's SHELL and its own short PATH.
    subprocess.run([shell, "-c", command], env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                   check=True, timeout=30, capture_output=True)
    assert record.read_text().splitlines() == [
        str(installation.resolve()), f"/usr/bin:/bin:{odd}/tools",  # the '%' entry cannot pass cron
        "-m", "src.user_sync", "--state", str(odd / "state"), "--library", str(odd / "library"), "run"]


def test_cron_line_without_path_when_the_path_would_not_fit(host):
    host.fake.bus = False
    on("linux", host, path="/x" * 600)
    script, _ = cron_command(cron_line(host))
    assert "export" not in script and len(cron_line(host).split(None, 5)[5]) <= schedule.CRON_COMMAND_LIMIT
    with pytest.raises(ValueError, match="shorter paths"):
        on("linux", host, state_dir=Path(host.tmp, *["s" * 100] * 10))


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot hold quotes")
def test_cron_quotes_spaces_quotes_and_dollar_signs_for_sh(host, tmp_path):
    host.fake.bus = False
    installation, python = tmp_path / "it's \"my\" $HOME", str(tmp_path / "a b" / "python $1")
    on("linux", host, installation=installation, python=python)
    script, _ = cron_command(cron_line(host, "agents-core-sync-" + schedule.installation_id(installation)))
    assert script[4:6] == ["cd", str(installation.resolve())] and script[11] == python


def test_cron_keeps_every_other_line_byte_for_byte(host):
    host.fake.bus = False
    others = (b"# m h dom mon dow command\r\n"
              b"SHELL=/bin/tcsh\n"
              b"MAILTO=me@example.org\n"
              b"\n"
              b"0 3 * * * /usr/bin/backup --to '/mnt/\xff\xfe raw'\n"  # not UTF-8
              b"*/2 * * * * /bin/sh -c true agents-core-sync-0000000000000000\n"  # another installation's line
              b"@reboot /usr/local/bin/start")  # no newline at the end
    host.fake.table = others
    on("linux", host, "enable", 5)
    # The last line gets the newline cron needs; then the job's line follows it.
    first = host.fake.table
    assert first.startswith(others + b"\n") and first.count(host.name.encode()) == 1
    on("linux", host, "enable", 7)
    assert host.fake.table.startswith(others + b"\n") and host.fake.table.count(host.name.encode()) == 1
    assert cron_line(host).startswith("*/7 * * * * ")
    on("linux", host, "disable")
    assert host.fake.table == others + b"\n"


def test_cron_interval_change_replaces_the_line_in_place(host):
    host.fake.bus = False
    host.fake.table = b"MAILTO=me\n"
    on("linux", host, "enable", 5)
    host.fake.table += b"0 4 * * * /bin/later\n"
    report = on("linux", host, "enable", 60)
    lines = host.fake.table.splitlines()
    assert lines[0] == b"MAILTO=me" and lines[2] == b"0 4 * * * /bin/later" and len(lines) == 3
    assert lines[1].startswith(b"0 * * * * /bin/sh -c ") and report["interval_minutes"] == 60
    calls = len(host.fake.calls)
    on("linux", host, "enable", 60)  # unchanged: the crontab is not written again
    assert ["crontab", "-"] not in host.fake.calls[calls:]


def test_cron_disable_removes_its_line_and_twice_is_fine(host):
    host.fake.bus = False
    on("linux", host)
    assert host.fake.table is not None
    for _ in range(2):
        assert on("linux", host, "disable") == {"backend": "cron", "name": host.name,
                                                "scheduled": False, "interval_minutes": None}
    assert host.fake.table is None  # the crontab this job created is gone again


def test_cron_never_rewrites_a_crontab_it_cannot_read(host):
    host.fake.bus = False
    host.fake.table = b"0 3 * * * /usr/bin/backup\n"
    host.fake.fail.append(("crontab", "-l"))
    with pytest.raises(schedule.CronUnavailable, match="left as it is"):
        on("linux", host)
    assert host.fake.table == b"0 3 * * * /usr/bin/backup\n"
    assert ["crontab", "-"] not in host.fake.calls


@pytest.mark.parametrize("option", ["installation", "state_dir", "library"])
def test_cron_refuses_a_path_with_a_percent_sign(host, tmp_path, option):
    host.fake.bus = False
    with pytest.raises(ValueError, match="%"):
        on("linux", host, **{option: tmp_path / "100% agents"})
    assert host.fake.table is None and ["crontab", "-"] not in host.fake.calls


def test_linux_without_systemd_or_crontab_says_so(host):
    host.fake.missing.update({"systemctl", "crontab"})
    with pytest.raises(schedule.CronUnavailable, match="user manager, and crontab is not installed"):
        on("linux", host)
    assert on("linux", host, "disable")["scheduled"] is False


@posix_paths
def test_linux_moves_between_cron_and_systemd_without_leaving_the_other_job(host):
    units = host.options["systemd_dir"]
    host.fake.bus = False
    assert on("linux", host)["backend"] == "cron"
    host.fake.bus = True  # the user manager is reachable now: the timer replaces the crontab line
    assert on("linux", host)["backend"] == "systemd" and host.fake.table is None
    host.fake.bus = False  # and back: the unit files and the enable link go
    assert on("linux", host)["backend"] == "cron"
    assert [path for path in units.rglob("*") if path.is_file()] == []
    host.fake.bus = True
    assert on("linux", host, "disable")["scheduled"] is False
    assert host.fake.table is None and host.fake.active == set()


@posix_paths
def test_linux_tries_every_removal_and_reports_each_failure(host):
    host.fake.bus = False
    on("linux", host)
    leftover = host.fake.table
    host.fake.bus = True
    host.fake.fail.append(("crontab", "-r"))  # the earlier crontab line cannot go
    with pytest.raises(schedule.BackendErrors, match="systemd job is scheduled") as raised:
        on("linux", host)
    assert set(raised.value.errors) == {"cron"} and host.fake.active == {f"{host.name}.timer"}
    assert host.fake.table == leftover

    host.fake.fail.append(("systemctl", "stop"))  # the timer keeps running
    with pytest.raises(schedule.BackendErrors, match="Not every sync job") as raised:
        on("linux", host, "disable")
    assert set(raised.value.errors) == {"systemd"}
    assert host.fake.table is None  # the crontab line went although systemd failed
    assert on("linux", host, "disable")["scheduled"] is False


# --- Windows: Task Scheduler ----------------------------------------------------------------

def task(host):
    return ElementTree.fromstring(host.fake.tasks[host.name])


def test_task_runs_hidden_at_logon_and_every_few_minutes_from_now_on(host):
    python = Path(host.python).with_name("python.exe")
    pythonw = python.with_name("pythonw.exe")
    python.touch()
    pythonw.touch()
    before = datetime.now().replace(microsecond=0)
    report = on("win32", host, "enable", 5, python=str(python))
    after = datetime.now()
    assert host.fake.tasks[host.name][:2] in (b"\xff\xfe", b"\xfe\xff")  # UTF-16, as schtasks reads it
    definition = task(host)
    logon, start = (definition.find(f"t:Triggers/t:{kind}", TASK) for kind in ("LogonTrigger", "TimeTrigger"))
    # One repeating trigger, so two schedules never run side by side after a logon.
    assert len(definition.findall(".//t:Repetition", TASK)) == 1 and logon.find("t:Repetition", TASK) is None
    assert logon.findtext("t:UserId", namespaces=TASK) == "HOST\\tester"
    assert start.findtext("t:Repetition/t:Interval", namespaces=TASK) == "PT5M"
    assert start.find("t:Repetition/t:Duration", TASK) is None  # repeats without end
    assert start.findtext("t:Repetition/t:StopAtDurationEnd", namespaces=TASK) == "false"
    assert all(trigger.findtext("t:Enabled", namespaces=TASK) == "true" for trigger in (logon, start))
    # The repeated runs start now, not at the next logon.
    assert before <= datetime.fromisoformat(start.findtext("t:StartBoundary", namespaces=TASK)) <= after
    principal = definition.find("t:Principals/t:Principal", TASK)
    assert [principal.findtext(f"t:{tag}", namespaces=TASK) for tag in ("UserId", "LogonType", "RunLevel")] == \
        ["HOST\\tester", "InteractiveToken", "LeastPrivilege"]
    settings = {child.tag.split("}")[1]: child.text for child in definition.find("t:Settings", TASK)}
    assert settings["MultipleInstancesPolicy"] == "IgnoreNew" and settings["Hidden"] == "true"
    assert settings["DisallowStartIfOnBatteries"] == settings["StopIfGoingOnBatteries"] == "false"
    assert settings["ExecutionTimeLimit"] == "PT10M"
    execute = definition.find("t:Actions/t:Exec", TASK)
    assert execute.findtext("t:Command", namespaces=TASK) == f'"{pythonw}"'  # no console window
    # Python splits its command line by the C runtime's rules: paths with spaces in double quotes.
    assert execute.findtext("t:Arguments", namespaces=TASK) == \
        f'-m src.user_sync --state "{host.state}" --library "{host.library}" run'
    assert execute.findtext("t:WorkingDirectory", namespaces=TASK) == str(host.installation)
    create, = [call for call in host.fake.calls if call[1] == "/Create"]
    assert create == ["schtasks", "/Create", "/XML", create[3], "/TN", host.name, "/F"]
    assert Path(create[3]).parent == host.options["temp_dir"] and not Path(create[3]).exists()
    assert report == {"backend": "task-scheduler", "name": host.name, "scheduled": True, "interval_minutes": 5}


def test_task_runs_the_interpreter_itself_without_a_pythonw_next_to_it(host):
    on("win32", host)
    assert task(host).findtext("t:Actions/t:Exec/t:Command", namespaces=TASK) == f'"{host.python}"'


@pytest.mark.parametrize("query_encoding", ["utf-16", "utf-8"])
def test_task_enabled_twice_is_one_task_and_disable_twice_is_fine(host, query_encoding):
    host.fake.query_encoding = query_encoding
    on("win32", host, "enable", 5)
    report = on("win32", host, "enable", 60)
    assert list(host.fake.tasks) == [host.name] and report["interval_minutes"] == 60
    assert {node.text for node in task(host).iter(f"{{{schedule.TASK_NAMESPACE}}}Interval")} == {"PT1H"}
    for _ in range(2):
        assert on("win32", host, "disable") == {"backend": "task-scheduler", "name": host.name,
                                                "scheduled": False, "interval_minutes": None}
    assert host.fake.tasks == {} and list(host.options["temp_dir"].iterdir()) == []


def test_task_disable_reports_a_task_it_could_not_delete(host):
    on("win32", host)
    host.fake.fail.append(("schtasks", "/Delete"))
    with pytest.raises(RuntimeError, match="could not delete"):
        on("win32", host, "disable")
    assert host.name in host.fake.tasks


@pytest.mark.parametrize("option", ["installation", "state_dir", "library"])
def test_task_refuses_a_path_that_task_scheduler_would_expand(host, tmp_path, option):
    with pytest.raises(ValueError, match="%NAME%"):
        on("win32", host, **{option: tmp_path / "%USERPROFILE%"})
    assert host.fake.tasks == {} and list(host.options["temp_dir"].iterdir()) == []


def test_cli_refuses_an_interval_out_of_range(capsys, monkeypatch):
    def no_scheduler(argv, input=None):
        raise AssertionError(f"ran {argv}")
    monkeypatch.setattr(schedule, "run_command", no_scheduler)
    assert schedule.main(["enable", "--interval", "0"]) == 1
    assert "from 1 to 60" in capsys.readouterr().err


# --- the real scheduler of this OS (CI only) --------------------------------------------------

@pytest.mark.skipif(os.environ.get("AGENTS_TEST_REAL_SCHEDULER") != "1",
                    reason="changes the OS scheduler; CI sets AGENTS_TEST_REAL_SCHEDULER=1")
def test_real_scheduler_round_trip(tmp_path, monkeypatch):
    # A fresh directory gives the job a name of its own. Its runs would only fail in an
    # empty directory, and with an interval of 30 or 60 minutes none comes before disable.
    monkeypatch.delenv("AGENTS_SERVICE_DIR", raising=False)
    installation = tmp_path / "real round trip"
    installation.mkdir()
    options = {"installation": installation, "service_dir": tmp_path / "no daemon", "lock_dir": tmp_path}
    backend = schedule.scheduler(**options).backend
    if isinstance(backend, schedule.SystemdOrCron) and not backend.systemd.usable() and not shutil.which("crontab"):
        pytest.skip("this Linux has neither a systemd user manager nor crontab")
    try:
        enabled = schedule.enable(60, state_dir=tmp_path / "sync state", library=tmp_path / "flow library",
                                  **options)
        assert enabled["scheduled"] and enabled["interval_minutes"] == 60, enabled
        assert schedule.status(**options) == enabled
        changed = schedule.enable(30, state_dir=tmp_path / "sync state", library=tmp_path / "flow library",
                                  **options)
        assert changed["scheduled"] and changed["interval_minutes"] == 30, changed
    finally:
        disabled = schedule.disable(**options)
    assert not disabled["scheduled"] and not schedule.status(**options)["scheduled"]
    assert not schedule.disable(**options)["scheduled"]


def test_the_command_line_schedules_runs_with_explicit_directories(tmp_path, monkeypatch, capsys):
    from src.user_sync import engine
    from src.user_sync.__main__ import main

    calls = []
    monkeypatch.setattr(schedule, "enable", lambda minutes, **kwargs: calls.append((minutes, kwargs)) or {"scheduled": True})
    state, library = tmp_path / "state", tmp_path / "library"
    assert main(["--state", str(state), "--library", str(library), "schedule", "enable", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "not_set_up"
    state.mkdir()
    settings = engine.Settings(remote="git@github.com:me/lib.git", name="Owner", email="owner@example.com",
                               label="a", fetch_minutes=7)
    settings.save(state / engine.SETTINGS_FILE)
    assert main(["--state", str(state), "--library", str(library), "schedule", "enable", "--json"]) == 0
    assert calls == [(7, {"state_dir": state.resolve(), "library": library.resolve()})]
    assert main(["--state", str(state), "--library", str(library), "schedule", "enable", "--interval", "3"]) == 0
    assert calls[-1][0] == 3
