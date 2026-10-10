"""The shared service on Windows (#195): its Task Scheduler task, private state and refusals.

A fake ``schtasks`` keeps the registered XML, so no test reaches the real Task Scheduler;
the ACL tests run on Windows only.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock
from xml.etree import ElementTree

import pytest

from src import windows_tasks
from src.daemon import autoupdate, control, service
from src.daemon.state import private_dir

NS = {"t": windows_tasks.NAMESPACE}


class FakeSchtasks:
    """``schtasks /Create /XML``, ``/Query /XML``, ``/Run``, ``/End`` and ``/Delete`` of one user."""

    def __init__(self):
        self.tasks, self.calls, self.running = {}, [], set()

    def __call__(self, argv, **_):
        assert argv[0] == "schtasks"
        verb, args = argv[1], argv[2:]
        name = args[args.index("/TN") + 1]
        self.calls.append((verb, name))
        if verb == "/Create":
            self.tasks[name] = Path(args[args.index("/XML") + 1]).read_bytes()  # the file is gone afterwards
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if name not in self.tasks:
            return subprocess.CompletedProcess(argv, 1, b"", b"ERROR: The system cannot find the file specified.")
        if verb == "/Query":
            # Piped, schtasks writes single-byte text that still declares UTF-16.
            return subprocess.CompletedProcess(argv, 0, self.tasks[name].decode("utf-16").encode("utf-8"), b"")
        if verb == "/Run":
            self.running.add(name)
        elif verb == "/End":
            if name not in self.running:
                return subprocess.CompletedProcess(argv, 1, b"", b"ERROR: The task is not running.")
            self.running.discard(name)
        elif verb == "/Delete":
            del self.tasks[name]
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def xml(self, name):
        return ElementTree.fromstring(self.tasks[name].decode("utf-16").split("?>", 1)[1])


@pytest.fixture
def windows(tmp_path, monkeypatch, service_platform):
    """A controller of an installed service whose OS is Windows, with a fake schtasks."""
    fake = FakeSchtasks()
    monkeypatch.setattr(service_platform, "PLATFORM", "win32")
    monkeypatch.setattr(service_platform, "RUNNER", fake)
    monkeypatch.setenv("USERNAME", "alex")
    monkeypatch.setenv("USERDOMAIN", "HOST")
    python = tmp_path / "venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (python.parent / "pythonw.exe").touch()
    controller = control.Controller(tmp_path / "state")
    controller.directory.mkdir()
    controller.config = {"installation": str(tmp_path / "install"), "python": str(python), "path": "",
                         "port": 8765, "autostart": True}
    return controller, fake


def _triggers(fake, name):
    return windows_tasks.enabled_triggers(windows_tasks.schtasks_text(fake.tasks[name]))


def test_the_task_runs_serve_hidden_and_without_a_console(windows):
    controller, fake = windows
    controller.manager.install()
    task = fake.xml(controller.manager.name)
    assert controller.manager.name == "agents-core-daemon-state"
    execute = task.find("t:Actions/t:Exec", NS)
    assert execute.find("t:Command", NS).text == '"' + controller.config["python"].replace("python.exe", "pythonw.exe") + '"'
    assert execute.find("t:Arguments", NS).text == subprocess.list2cmdline(
        ["-m", "src.daemon", "--state", str(controller.directory), "serve"])
    assert execute.find("t:WorkingDirectory", NS).text == controller.config["installation"]
    settings = task.find("t:Settings", NS)
    for tag, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("ExecutionTimeLimit", "PT0S"), ("Priority", "5"),
                       ("Hidden", "true"), ("DisallowStartIfOnBatteries", "false"), ("StopIfGoingOnBatteries", "false")):
        assert settings.find(f"t:{tag}", NS).text == value, tag
    principal = task.find("t:Principals/t:Principal", NS)
    assert principal.find("t:LogonType", NS).text == "InteractiveToken"
    assert principal.find("t:RunLevel", NS).text == "LeastPrivilege"
    assert principal.find("t:UserId", NS).text == "HOST\\alex"
    assert task.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text == "PT1M"


def test_install_leaves_the_start_to_start_or_the_next_logon(windows):
    controller, fake = windows
    controller.manager.install()
    assert _triggers(fake, controller.manager.name) == {"LogonTrigger": True, "TimeTrigger": False}
    assert ("/Run", controller.manager.name) not in fake.calls
    assert not controller.manager.loaded()


def test_start_turns_the_repetition_on_and_runs_the_task(windows):
    controller, fake = windows
    controller._start(probation="-nonce")
    name = controller.manager.name
    assert _triggers(fake, name) == {"LogonTrigger": True, "TimeTrigger": True}
    assert fake.xml(name).find("t:Actions/t:Exec/t:Arguments", NS).text.endswith("serve --probation=-nonce")
    assert fake.calls[-1] == ("/Run", name)
    assert controller.manager.loaded()
    controller.write_plist()  # after probation: the definition without the nonce, the instance keeps running
    assert fake.xml(name).find("t:Actions/t:Exec/t:Arguments", NS).text.endswith(" serve")
    assert name in fake.running and controller.manager.loaded()


def test_stop_ends_the_task_until_the_next_logon(windows):
    controller, fake = windows
    controller._start()
    controller.manager.stop()
    name = controller.manager.name
    assert ("/End", name) in fake.calls and name not in fake.running
    # The repetition would start it again within a minute; the logon trigger starts it at the next login.
    assert _triggers(fake, name) == {"LogonTrigger": True, "TimeTrigger": False}
    assert not controller.manager.loaded()
    controller.manager.ensure_keep_alive()  # what serve does after that logon start
    assert _triggers(fake, name) == {"LogonTrigger": True, "TimeTrigger": True}


def test_stop_asks_the_service_to_exit_and_ends_the_task_only_as_a_fallback(windows, monkeypatch):
    """/End terminates the process before the lifespan can flush queued log writes."""
    controller, fake = windows
    controller._start()
    asked = []
    monkeypatch.setattr(control.Controller, "exit_gracefully", lambda self: asked.append(True) or True)
    controller.manager.stop()
    assert asked == [True] and ("/End", controller.manager.name) not in fake.calls
    assert _triggers(fake, controller.manager.name)["TimeTrigger"] is False  # off before the exit
    monkeypatch.setattr(control.Controller, "exit_gracefully", lambda self: False)
    controller._start()
    controller.manager.stop()
    assert ("/End", controller.manager.name) in fake.calls


@pytest.mark.parametrize("answer", [(404, {"error": "not found"}), (200, {"state": "ready"})])
def test_a_service_that_does_not_agree_to_exit_is_not_waited_for(tmp_path, monkeypatch, answer):
    controller = control.Controller(tmp_path / "state")
    monkeypatch.setattr(control.Controller, "request", lambda self, *a, **k: answer)
    assert controller.exit_gracefully(timeout=60) is False  # at once, not after the timeout


def test_exit_gracefully_waits_for_the_lease(tmp_path, monkeypatch):
    from src.file_lock import file_lock
    controller = control.Controller(tmp_path / "state")
    controller.directory.mkdir()
    monkeypatch.setattr(control.Controller, "request", lambda self, *a, **k: (200, {"state": "exiting"}))
    with file_lock(controller.directory / ".daemon.lock"):
        assert controller.exit_gracefully(timeout=0.3) is False  # still held: the caller ends it
    assert controller.exit_gracefully(timeout=5) is True


def test_stop_without_a_running_instance_or_a_task(windows):
    controller, fake = windows
    controller.manager.stop()  # nothing registered: nothing to do
    assert fake.calls == [("/Query", controller.manager.name)]
    controller.manager.install()
    controller.manager.stop()  # registered, not running: /End fails and changes nothing
    assert _triggers(fake, controller.manager.name)["TimeTrigger"] is False


def test_keep_alive_is_left_alone_while_on_or_without_a_task(windows):
    controller, fake = windows
    controller.manager.ensure_keep_alive()
    assert controller.manager.name not in fake.tasks
    controller._start()
    creates = fake.calls.count(("/Create", controller.manager.name))
    controller.manager.ensure_keep_alive()
    assert fake.calls.count(("/Create", controller.manager.name)) == creates


def test_autostart_off_disables_the_logon_trigger(windows):
    controller, fake = windows
    controller.config["autostart"] = False
    controller._start()
    assert _triggers(fake, controller.manager.name) == {"LogonTrigger": False, "TimeTrigger": True}


def test_remove_deletes_the_task_and_is_quiet_without_one(windows):
    controller, fake = windows
    controller.manager.remove()  # nothing registered: /Delete fails, the query confirms, no error
    controller.manager.install()
    controller.manager.remove()
    assert controller.manager.name not in fake.tasks


def test_a_task_that_will_not_go_away_is_reported(windows):
    controller, fake = windows
    controller.manager.install()

    def refuse_delete(argv, **kwargs):
        if argv[1] == "/Delete":
            return subprocess.CompletedProcess(argv, 1, b"", b"ERROR: Access is denied.")
        return fake(argv, **kwargs)
    with pytest.raises(RuntimeError, match="could not delete"):
        service.TaskScheduler(controller, runner=refuse_delete).remove()
    assert controller.manager.name in fake.tasks


def test_a_failed_registration_installs_nothing(windows, monkeypatch, tmp_path):
    controller, fake = windows
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(control, "__file__", str(root / "src/daemon/control.py"))
    monkeypatch.setattr(control.socket, "socket", MagicMock())
    monkeypatch.setattr(control.shutil, "which", lambda name: str(tmp_path / name))
    monkeypatch.setattr(control, "pin_model", lambda model, cache: {"model_artifact": "a", "model_path": "p"})
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    fresh = control.Controller(tmp_path / "fresh")

    def refuse(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"ERROR: Access is denied.")
    monkeypatch.setattr(service, "RUNNER", refuse)
    with pytest.raises(RuntimeError, match="schtasks /Create failed"):
        fresh.install(python=controller.config["python"])
    assert not (fresh.directory / "service.json").exists() and not (fresh.directory / "token").exists()
    monkeypatch.setattr(service, "RUNNER", fake)
    assert control.Controller(fresh.directory).install(python=controller.config["python"])["state"] == "installed"


def test_status_reports_a_scheduler_that_does_not_answer(windows, monkeypatch):
    controller, _fake = windows

    def down(*_args, **_kwargs):
        raise ConnectionRefusedError

    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 60)
    monkeypatch.setattr(control.Controller, "request", down)
    monkeypatch.setattr(service, "RUNNER", hang)  # what a TaskScheduler made from now on runs with
    status = controller.status()
    assert status["state"] == "unknown" and status["supervised"] is None and "TimeoutExpired" in status["error"]


def test_a_stop_the_scheduler_refuses_resumes_the_drained_service(windows, monkeypatch):
    controller, _fake = windows
    calls = []
    monkeypatch.setattr(control.Controller, "_drain", lambda self, timeout=60: calls.append("drain"))
    monkeypatch.setattr(control.Controller, "request",
                        lambda self, path="/health", **kwargs: calls.append(path) or {"state": "ready"})

    def broken(self):
        raise RuntimeError("schtasks /Create failed: Access is denied.")
    monkeypatch.setattr(service.TaskScheduler, "stop", broken)
    with pytest.raises(RuntimeError, match="Access is denied"):
        controller._stop()
    assert calls == ["drain", "/admin/resume"]


def test_a_percent_sign_is_refused_before_anything_is_registered(windows):
    controller, fake = windows
    controller.config["installation"] = r"C:\%USERPROFILE%\Agents"
    with pytest.raises(ValueError, match="expands %NAME%"):
        controller.manager.install()
    assert not fake.tasks


def test_status_reports_whether_the_task_keeps_the_service_running(windows, monkeypatch):
    controller, _fake = windows

    def down(*_args, **_kwargs):
        raise ConnectionRefusedError
    monkeypatch.setattr(control.Controller, "request", down)
    assert controller.status()["supervised"] is False
    controller._start()
    status = controller.status()
    assert (status["state"], status["supervised"]) == ("starting", True)


def _sync_set_up(root: Path, local_app_data: Path):
    from src.daemon.state import default_state_dir
    old = local_app_data / "Agents-Core" / default_state_dir(root).name / "user-sync"
    old.mkdir(parents=True)
    (old / "user-sync.json").write_text("{}")
    return old


def _home(monkeypatch, tmp_path):
    """AppData and the home in tmp_path; src.daemon.state picks the macOS directory off Windows."""
    for variable, value in (("LOCALAPPDATA", "LocalAppData"), ("USERPROFILE", "profile"), ("HOME", "profile")):
        monkeypatch.setenv(variable, str(tmp_path / value))


def test_windows_install_moves_sync_state_from_appdata_to_the_default_directory(windows, monkeypatch, tmp_path):
    """Before #256, user sync without the service kept its settings in %LOCALAPPDATA%; the service reads
    its own <state>/user-sync and install removes the scheduled run: sync would stop without a word.
    The state moves where sync itself looks without the service, never straight into another --state,
    which an install that fails later would leave where nothing looks."""
    from src.daemon.state import default_state_dir
    controller, _fake = windows
    root = tmp_path / "install"
    _home(monkeypatch, tmp_path)
    old = _sync_set_up(root, tmp_path / "LocalAppData")
    default = default_state_dir(root)
    control.adopt_sync_state(root, default)
    assert (default / "user-sync" / "user-sync.json").read_text() == "{}" and not old.exists()
    control.adopt_sync_state(root, default)  # set up there: nothing more to do
    with pytest.raises(RuntimeError, match="move"):
        control.adopt_sync_state(root, controller.directory)  # another --state


def test_windows_install_elsewhere_refuses_while_sync_lives_in_the_default_directory(windows, monkeypatch, tmp_path):
    """Since #256 user sync without the service keeps its settings in the service's default directory."""
    from src.daemon.state import default_state_dir
    controller, _fake = windows
    root = tmp_path / "install"
    _home(monkeypatch, tmp_path)
    old = _sync_set_up(root, tmp_path / "LocalAppData")  # older, and ignored while the default one is set up
    default = default_state_dir(root) / "user-sync"
    default.mkdir(parents=True)
    (default / "user-sync.json").write_text("{}")
    with pytest.raises(RuntimeError, match="move") as refused:
        control.adopt_sync_state(root, controller.directory)
    assert str(default) in str(refused.value) and str(controller.directory / "user-sync") in str(refused.value)
    assert old.exists() and not (controller.directory / "user-sync").exists()
    control.adopt_sync_state(root, default.parent)  # an install into the default directory finds it there


def test_adopting_sync_state_is_a_windows_concern_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    _sync_set_up(tmp_path / "install", tmp_path / "LocalAppData")
    control.adopt_sync_state(tmp_path / "install", tmp_path / "state")  # the conftest pins launchd
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("command", [["update"], ["auto-update", "run"]])
def test_updates_run_on_windows(windows, monkeypatch, capsys, command):
    from src.daemon import update
    controller, _fake = windows
    (controller.directory / "service.json").write_text(json.dumps(controller.config))
    calls = []
    monkeypatch.setattr(update, "offline_update", lambda current: calls.append("update") or {"state": "UP_TO_DATE"})
    monkeypatch.setattr(autoupdate, "run", lambda current: calls.append("run") or {"state": "up_to_date"})
    assert control.main(["--state", str(controller.directory), *command]) == 0
    assert calls == [command[-1]] and "state" in json.loads(capsys.readouterr().out)


@pytest.fixture
def no_launchd(monkeypatch):
    def launchd(*_args, **_kwargs):
        raise AssertionError("launchctl on Windows")
    monkeypatch.setattr(control.Controller, "launchctl", launchd)


def test_auto_update_on_windows_is_a_second_hidden_task(windows, no_launchd):
    controller, fake = windows
    (controller.directory / "service.json").write_text(json.dumps(controller.config))
    assert autoupdate.status(controller)["scheduled"] is False
    status = autoupdate.enable(controller, interval=600, idle_seconds=300)
    assert status["enabled"] and status["scheduled"] and status["interval"] == 600 and status["idle_seconds"] == 300
    name = "agents-core-daemon-state-updater"
    task = fake.xml(name)
    execute = task.find("t:Actions/t:Exec", NS)
    assert execute.find("t:Command", NS).text == '"' + controller.config["python"].replace("python.exe", "pythonw.exe") + '"'
    assert execute.find("t:Arguments", NS).text == subprocess.list2cmdline(
        ["-m", "src.daemon", "--state", str(controller.directory), "auto-update", "run"])
    assert execute.find("t:WorkingDirectory", NS).text == controller.config["installation"]
    settings = task.find("t:Settings", NS)
    for tag, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("ExecutionTimeLimit", "PT0S"), ("Priority", "7"),
                       ("Hidden", "true")):
        assert settings.find(f"t:{tag}", NS).text == value, tag
    # Every interval from registration, as launchd's StartInterval without RunAtLoad; never at logon.
    assert _triggers(fake, name) == {"LogonTrigger": False, "TimeTrigger": True}
    assert task.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text == "PT10M"
    assert json.loads((controller.directory / "service.json").read_text())["auto_update"]["enabled"] is True
    autoupdate.enable(controller, interval=90)  # replaces the task; whole minutes, rounded up
    assert fake.xml(name).find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text == "PT2M"
    assert autoupdate.disable(controller) == {"enabled": False}
    assert name not in fake.tasks and autoupdate.status(controller)["scheduled"] is False
    assert json.loads((controller.directory / "service.json").read_text())["auto_update"]["enabled"] is False
    assert autoupdate.disable(controller) == {"enabled": False}  # without a task


def test_a_refused_updater_task_changes_no_setting(windows, no_launchd, monkeypatch):
    controller, fake = windows
    (controller.directory / "service.json").write_text(json.dumps(controller.config))
    autoupdate.enable(controller, interval=600)

    def refuse(argv, **kwargs):
        if argv[1] in ("/Create", "/Delete"):
            return subprocess.CompletedProcess(argv, 1, b"", b"ERROR: Access is denied.")
        return fake(argv, **kwargs)
    monkeypatch.setattr(service, "RUNNER", refuse)
    with pytest.raises(RuntimeError, match="schtasks /Create failed"):
        autoupdate.enable(controller, interval=1200)
    with pytest.raises(RuntimeError, match="still scheduled"):
        autoupdate.disable(controller)
    assert json.loads((controller.directory / "service.json").read_text())["auto_update"] == {
        "enabled": True, "interval": 600, "idle_seconds": autoupdate.DEFAULT_IDLE_SECONDS}
    assert "PT10M" in fake.tasks["agents-core-daemon-state-updater"].decode("utf-16")


def test_stdio_readers_on_windows_are_the_held_slots(windows):
    """Windows does not say who holds a lock: a stdio server holds its slot's lease for its session."""
    from src.file_lock import file_lock
    controller, _fake = windows
    slots = Path(controller.config["installation"]) / "data" / "stdio"
    for slot in ("0", "1", "2"):
        (slots / slot).mkdir(parents=True)
        (slots / slot / ".lease").touch()
    assert autoupdate.other_readers(controller, daemon_pid=1) == []
    with file_lock(slots / "1" / ".lease", blocking=False):
        assert autoupdate.other_readers(controller, daemon_pid=1) == ["stdio slot 1"]
        assert autoupdate._readers_refusal(controller, 1)["state"] == "deferred"
    assert autoupdate.other_readers(controller, daemon_pid=1) == []


def test_the_sync_task_is_refused_while_the_daemon_is_installed(tmp_path):
    from src.user_sync import schedule
    service_dir = tmp_path / "daemon"
    service_dir.mkdir()
    (service_dir / "service.json").write_text("{}")
    calls = []
    runs = schedule.scheduler(installation=tmp_path, python=sys.executable, platform="win32",
                              runner=lambda argv, input=None: calls.append(argv), service_dir=service_dir,
                              lock_dir=tmp_path / "locks")
    with pytest.raises(schedule.DaemonRunsSync):
        runs.enable(5)
    assert calls == []


def test_a_logon_start_turns_the_repetition_on_through_serve(windows):
    """bootstrap.serve calls _keep_alive under control.lock after a start by the logon trigger."""
    from src.daemon.bootstrap import _keep_alive
    controller, fake = windows
    (controller.directory / "service.json").write_text(json.dumps(controller.config))
    controller._start()
    controller.manager.stop()
    _keep_alive(controller.directory)
    assert _triggers(fake, controller.manager.name)["TimeTrigger"] is True


windows_only = pytest.mark.skipif(os.name != "nt", reason="the owner-only ACL is Windows-specific")


@windows_only
def test_private_state_is_readable_by_its_owner_only(tmp_path):
    from src.daemon import acl
    from src.daemon.bootstrap import _private_file
    from src.daemon.state import atomic_private
    directory = private_dir(tmp_path / "state")
    atomic_private(directory / "token", "secret\n")
    assert acl.is_private(directory) and _private_file(directory / "token")
    listed = subprocess.run(["icacls", str(directory / "token")], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, check=True).stdout
    entries = [line for line in listed.splitlines() if ":(" in line]  # "<path> DOMAIN\user:(I)(F)"
    assert len(entries) == 1 and os.environ["USERNAME"].lower() in entries[0].lower(), listed


@windows_only
def test_a_state_directory_another_account_owns_is_refused():
    from src.daemon import acl
    assert not acl.owned_by_user_or_admins(os.environ["SystemRoot"])  # owned by TrustedInstaller


@windows_only
def test_after_uninstall_user_sync_still_finds_its_settings(tmp_path, monkeypatch):
    """uninstall keeps the service's state, and without the marker sync keeps its own there (#256)."""
    from src.daemon.state import default_state_dir
    from src.user_sync import engine
    monkeypatch.delenv("AGENTS_SERVICE_DIR", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setattr(engine, "installation_root", lambda: tmp_path / "install")
    kept = default_state_dir(tmp_path / "install") / "user-sync"
    assert engine.default_state_dir() == kept
    kept.mkdir(parents=True)
    (kept / engine.SETTINGS_FILE).write_text("{}")
    assert engine.default_state_dir() == kept


@windows_only
def test_a_token_that_others_can_read_is_refused(tmp_path):
    from src.daemon.bootstrap import _private_file
    token = tmp_path / "token"  # inherits the temporary directory's ACL: SYSTEM, Administrators
    token.write_text("secret\n")
    assert not _private_file(token)


@windows_only
def test_the_state_directory_is_outside_appdata_and_holds_user_sync(monkeypatch, tmp_path):
    """The Claude desktop app's MSIX package virtualizes what its processes write under AppData."""
    from src.daemon.state import state_dir
    from src.user_sync import engine
    monkeypatch.delenv("AGENTS_SERVICE_DIR", raising=False)
    assert state_dir().parent == Path(os.environ["USERPROFILE"]) / ".agents-core"
    monkeypatch.setenv("AGENTS_SERVICE_DIR", str(tmp_path / "service"))  # what serve sets
    assert engine.default_state_dir() == state_dir() / "user-sync"


@pytest.mark.parametrize("call", ["version", "repository flows", "history sync", "web UI overview"])
def test_git_under_the_hidden_service_opens_no_console_and_never_reads_stdin(monkeypatch, tmp_path, call):
    """The service runs under pythonw: a console child without CREATE_NO_WINDOW opens a visible window."""
    from src import user_flows, version
    from src.daemon import overview
    from src.user_sync import history
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 1, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    {"version": lambda: version._git("status"), "repository flows": lambda: user_flows._origin(tmp_path),
     "history sync": lambda: history._top_level(str(tmp_path)),
     "web UI overview": lambda: overview.repository(tmp_path)}[call]()
    assert seen and all(kwargs.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        and kwargs.get("stdin") == subprocess.DEVNULL for kwargs in seen)


@windows_only
@pytest.mark.parametrize("grant", ["F", "(OI)(CI)(NP)F"])
def test_a_private_directory_whose_entry_is_not_inherited_is_restricted_again(tmp_path, grant):
    """A user-only ACE without inheritance flags, or one that stops at the first level (NP), leaves
    files created below the directory the creator's default DACL."""
    from src.daemon import acl
    directory = tmp_path / "state"
    directory.mkdir()
    subprocess.run(["icacls", str(directory), "/inheritance:r", "/grant:r", f"*{acl.user_sid()}:{grant}"],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    assert acl.is_private(directory) and not acl.is_private(directory, inherited_below=True)
    private_dir(directory)
    assert acl.is_private(directory, inherited_below=True)
    (directory / "nested").mkdir()
    (directory / "nested" / "token").write_text("secret\n")
    assert acl.is_private(directory / "nested" / "token")
