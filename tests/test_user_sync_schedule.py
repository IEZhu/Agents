"""Scheduled sync runs without the daemon: Task Scheduler, systemd or cron, and a LaunchAgent.

Every backend runs against a fake OS (`FakeOS`), so these tests change no real
scheduler. `test_real_scheduler_round_trip` uses the real one of the current OS
and runs only when AGENTS_TEST_REAL_SCHEDULER=1, as in CI.
"""
from datetime import datetime
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from src.daemon.state import state_dir
from src.user_sync import schedule

PATH = "/opt/homebrew/bin:/usr/bin:/bin"
TASK = {"t": schedule.TASK_NAMESPACE}


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


@pytest.fixture
def host(tmp_path, monkeypatch):
    """An installation and an interpreter in paths with spaces, a fake OS and the scheduler options for both."""
    installation = tmp_path / "Agents Core" / "checkout"
    installation.mkdir(parents=True)
    python = tmp_path / "Python Home" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    (tmp_path / "Temp Dir").mkdir()
    fake = FakeOS(tmp_path / "systemd user")
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setenv("USERDOMAIN", "HOST")
    options = {"installation": installation, "python": str(python), "runner": fake,
               "launch_agents_dir": tmp_path / "Launch Agents", "systemd_dir": tmp_path / "systemd user",
               "temp_dir": tmp_path / "Temp Dir", "service_dir": tmp_path / "Application Support" / "state",
               "path": PATH}
    ident = schedule.installation_id(installation)
    return SimpleNamespace(fake=fake, options=options, installation=installation.resolve(), python=str(python),
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


# --- macOS: a LaunchAgent ---------------------------------------------------------------

def label(host):
    return f"local.agents-core.{host.ident}.sync"


def test_launch_agent_runs_one_cycle_in_the_installation_root(host):
    report = on("darwin", host)
    plist = host.tmp / "Launch Agents" / (label(host) + ".plist")
    assert plistlib.loads(plist.read_bytes()) == {
        "Label": label(host), "ProgramArguments": [host.python, "-m", "src.user_sync", "run"],
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


def test_launch_agent_is_refused_while_the_daemon_runs_the_sync_loop(host):
    service = host.options["service_dir"]
    service.mkdir(parents=True)
    (service / "service.json").write_text("{}")
    with pytest.raises(schedule.DaemonRunsSync, match="daemon is installed"):
        on("darwin", host)
    assert not (host.tmp / "Launch Agents").exists()
    assert not any(call[1] == "bootstrap" for call in host.fake.calls)
    report = on("darwin", host, "status")
    assert report["daemon"] is True and "daemon runs the sync loop" in report["note"]
    assert report["scheduled"] is False


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


def unit_words(value, *, dollars=False):
    """Split a value as systemd does: ``%%`` is ``%``, quoted words take C escapes, ``$$`` is ``$`` in ExecStart=."""
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
        words.append(word.replace("$$", "$") if dollars else word)
    return words


def test_systemd_timer_runs_one_cycle_in_the_installation_root(host):
    report = on("linux", host)
    units = host.options["systemd_dir"]
    service, timer = unit_values(units / f"{host.name}.service"), unit_values(units / f"{host.name}.timer")
    assert unit_words(service["ExecStart"], dollars=True) == [host.python, "-m", "src.user_sync", "run"]
    assert service["WorkingDirectory"].replace("%%", "%") == str(host.installation)
    assert unit_words(service["Environment"]) == ["PATH=" + PATH]
    assert service["Type"] == "oneshot" and service["StandardOutput"] == "null"
    # The first run counts from enable (or login), the next ones from the last run.
    assert timer["OnActiveSec"] == timer["OnUnitActiveSec"] == "5min" and timer["WantedBy"] == "timers.target"
    assert host.fake.enabled == host.fake.active == {f"{host.name}.timer"}
    assert report == {"backend": "systemd", "name": f"{host.name}.timer", "scheduled": True, "interval_minutes": 5}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot hold quotes or backslashes")
def test_systemd_quotes_quotes_backslashes_percent_and_dollar_signs(host, tmp_path):
    installation = tmp_path / 'my "agents" 100% $HOME'
    python = str(tmp_path / "py\\thon $1 %n" / "python")
    on("linux", host, installation=installation, python=python)
    name = "agents-core-sync-" + schedule.installation_id(installation)
    service = unit_values(host.options["systemd_dir"] / f"{name}.service")
    assert unit_words(service["ExecStart"], dollars=True) == [python, "-m", "src.user_sync", "run"]
    assert service["WorkingDirectory"].replace("%%", "%") == str(installation.resolve())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot end with these characters")
@pytest.mark.parametrize("ending", [" ", "\\"])
def test_systemd_refuses_a_directory_the_unit_file_parser_would_change(host, tmp_path, ending):
    with pytest.raises(ValueError, match="ends with whitespace"):
        on("linux", host, installation=tmp_path / ("agents" + ending))
    assert not host.options["systemd_dir"].exists()


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


def test_systemd_enable_that_fails_leaves_no_units(host):
    host.fake.fail.append(("systemctl", "enable"))
    with pytest.raises(RuntimeError, match="systemctl --user enable"):
        on("linux", host)
    assert [path for path in host.options["systemd_dir"].rglob("*") if path.is_file()] == []
    assert host.fake.units == {}


def cron_line(host):
    line, = [line for line in host.fake.table.splitlines() if host.name.encode() in line]
    return line.decode()


@pytest.mark.parametrize("cause", ["no user bus", "no systemctl"])
def test_cron_line_where_systemctl_cannot_reach_a_user_manager(host, cause):
    if cause == "no user bus":
        host.fake.bus = False
    else:
        host.fake.missing.add("systemctl")
    report = on("linux", host)
    assert report == {"backend": "cron", "name": host.name, "scheduled": True, "interval_minutes": 5}
    fields = cron_line(host).split(None, 5)
    assert fields[:5] == ["*/5", "*", "*", "*", "*"] and fields[5].endswith(" # " + host.name)
    assert shlex.split(fields[5], comments=True) == [
        "cd", str(host.installation), "&&", "exec", host.python, "-m", "src.user_sync", "run", ">/dev/null", "2>&1"]
    assert not host.options["systemd_dir"].exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot hold quotes")
def test_cron_quotes_spaces_quotes_and_dollar_signs_for_sh(host, tmp_path):
    host.fake.bus = False
    installation, python = tmp_path / "it's \"my\" $HOME", str(tmp_path / "a b" / "python $1")
    on("linux", host, installation=installation, python=python)
    name = "agents-core-sync-" + schedule.installation_id(installation)
    line, = [line.decode() for line in host.fake.table.splitlines() if name.encode() in line]
    assert shlex.split(line.split(None, 5)[5], comments=True)[:5] == \
        ["cd", str(installation.resolve()), "&&", "exec", python]


def test_cron_keeps_every_other_line_byte_for_byte(host):
    host.fake.bus = False
    others = (b"# m h dom mon dow command\r\n"
              b"MAILTO=me@example.org\n"
              b"\n"
              b"0 3 * * * /usr/bin/backup --to '/mnt/\xff\xfe raw'\n"  # not UTF-8
              b"*/2 * * * * /bin/true # agents-core-sync-0000000000000000\n"  # another installation's line
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
    assert lines[1].startswith(b"0 * * * * cd ") and report["interval_minutes"] == 60
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


def test_cron_refuses_a_path_with_a_percent_sign(host, tmp_path):
    host.fake.bus = False
    with pytest.raises(ValueError, match="%"):
        on("linux", host, installation=tmp_path / "100% agents")
    assert host.fake.table is None and ["crontab", "-"] not in host.fake.calls


def test_linux_without_systemd_or_crontab_says_so(host):
    host.fake.missing.update({"systemctl", "crontab"})
    with pytest.raises(schedule.CronUnavailable, match="user manager, and crontab is not installed"):
        on("linux", host)
    assert on("linux", host, "disable")["scheduled"] is False


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


# --- Windows: Task Scheduler ----------------------------------------------------------------

def task(host):
    return ElementTree.fromstring(host.fake.tasks[host.name])


def test_task_runs_hidden_from_logon_and_from_now_on(host):
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
    for trigger in (logon, start):
        assert trigger.findtext("t:Repetition/t:Interval", namespaces=TASK) == "PT5M"
        assert trigger.find("t:Repetition/t:Duration", TASK) is None  # repeats without end
        assert trigger.findtext("t:Repetition/t:StopAtDurationEnd", namespaces=TASK) == "false"
        assert trigger.findtext("t:Enabled", namespaces=TASK) == "true"
    assert logon.findtext("t:UserId", namespaces=TASK) == "HOST\\tester"
    # The first runs do not wait for the next logon.
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
    assert execute.findtext("t:Arguments", namespaces=TASK) == "-m src.user_sync run"
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


def test_task_refuses_a_path_that_task_scheduler_would_expand(host, tmp_path):
    with pytest.raises(ValueError, match="%NAME%"):
        on("win32", host, installation=tmp_path / "%USERPROFILE%")
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
def test_real_scheduler_round_trip(tmp_path):
    # A fresh directory gives the job a name of its own. Its runs would only fail in an
    # empty directory, and with an interval of 30 or 60 minutes none comes before disable.
    installation = tmp_path / "real round trip"
    installation.mkdir()
    options = {"installation": installation, "service_dir": tmp_path / "no daemon"}
    backend = schedule.scheduler(**options)
    if isinstance(backend, schedule.SystemdOrCron) and not backend.systemd.usable() and not shutil.which("crontab"):
        pytest.skip("this Linux has neither a systemd user manager nor crontab")
    try:
        enabled = schedule.enable(60, **options)
        assert enabled["scheduled"] and enabled["interval_minutes"] == 60, enabled
        assert schedule.status(**options) == enabled
        changed = schedule.enable(30, **options)
        assert changed["scheduled"] and changed["interval_minutes"] == 30, changed
    finally:
        disabled = schedule.disable(**options)
    assert not disabled["scheduled"] and not schedule.status(**options)["scheduled"]
    assert not schedule.disable(**options)["scheduled"]
