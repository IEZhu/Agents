"""Unattended updates: decide without touching the service, then reuse the update transaction."""
import os
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.daemon import autoupdate
from src.daemon.state import read_json, write_json
from tests.test_daemon_update import git, installation  # noqa: F401  (fixture)


@pytest.fixture
def scheduled(installation):  # noqa: F811
    controller, root, old, target = installation
    controller.label = "local.agents-core.test"
    controller.plist = controller.directory / "LaunchAgents" / (controller.label + ".plist")
    controller.plist.parent.mkdir()
    controller.config.update(python="/usr/bin/python3", auto_update={"enabled": True, "idle_seconds": 120})
    write_json(controller.directory / "service.json", controller.config)
    controller.health = {"inflight": 0, "io_pending": 0, "idle_seconds": 600}
    controller.calls, controller.stops = [], 0
    controller.launchctl = lambda *args, check=True: controller.calls.append(args) or SimpleNamespace(returncode=0)
    controller.status = lambda: {**controller.health, "state": "ready" if controller.running else "stopped"}
    stop = controller._stop
    def counted_stop():
        controller.stops += 1
        stop()
    controller._stop = counted_stop
    return controller, root, old, target


def push_to_remote(root, path, content, message):
    """Commit on top of the remote's main without moving the installation."""
    head = git(root, "rev-parse", "HEAD")
    git(root, "fetch", "--quiet", "origin", "main")
    git(root, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    (root / path).write_text(content)
    git(root, "add", path)
    git(root, "commit", "--quiet", "-m", message)
    git(root, "push", "--quiet", "origin", "HEAD:main")
    git(root, "checkout", "--quiet", "main")
    assert git(root, "rev-parse", "HEAD") == head


def test_check_target_classifies_without_touching_the_service(scheduled):
    controller, root, old, target = scheduled
    assert autoupdate.check_target(controller, "origin", "main") == {"state": "available", "head": old, "target": target}
    (root / "README.md").write_text("local edit")
    assert autoupdate.check_target(controller, "origin", "main")["reason"] == "tracked files have local changes"
    git(root, "checkout", "--quiet", "README.md")
    git(root, "checkout", "--quiet", "-b", "feature")
    assert "not main" in autoupdate.check_target(controller, "origin", "main")["reason"]
    git(root, "checkout", "--quiet", "main")
    git(root, "commit", "--quiet", "--allow-empty", "-m", "local only")
    assert autoupdate.check_target(controller, "origin", "main")["reason"] == "not a fast-forward"
    assert controller.stops == 0


def test_idle_service_is_updated_through_the_transaction(scheduled):
    controller, root, old, target = scheduled
    result = autoupdate.run(controller)
    assert result["state"] == "UPDATED" and result["target"] == target
    assert git(root, "rev-parse", "HEAD") == target
    assert controller.stops == 1 and controller.probes == 1
    assert read_json(controller.directory / "auto-update.json")["state"] == "UPDATED"
    assert autoupdate.run(controller)["state"] == "up_to_date"
    assert controller.stops == 1


@pytest.mark.parametrize("health", [{"inflight": 1}, {"io_pending": 1}, {"idle_seconds": 5}])
def test_busy_service_defers_without_a_restart(scheduled, health):
    controller, root, old, _ = scheduled
    controller.health.update(health)
    assert autoupdate.run(controller)["state"] == "deferred"
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0


def test_stopped_service_is_not_started_by_an_update(scheduled):
    controller, root, old, _ = scheduled
    controller.running = False
    result = autoupdate.run(controller)
    assert result["state"] == "deferred" and result["reason"] == "service is stopped"
    assert git(root, "rev-parse", "HEAD") == old and controller.probes == 0


def test_dependency_change_is_refused_before_draining_and_logged_once(scheduled):
    controller, root, old, _ = scheduled
    push_to_remote(root, "pyproject.toml", "[project]\nname = 'x'\n", "bump deps")
    for _ in range(3):
        result = autoupdate.run(controller)
        assert result["state"] == "skipped" and "pyproject.toml" in result["reason"]
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0
    log = (controller.directory / "auto-update.log").read_text().splitlines()
    assert len(log) == 1


def test_disabled_or_unfinished_transaction_does_nothing(scheduled):
    controller, root, old, _ = scheduled
    (controller.directory / "transaction.json").write_text("{}")
    assert autoupdate.run(controller)["state"] == "blocked"
    controller.config["auto_update"]["enabled"] = False
    assert autoupdate.run(controller) == {"state": "disabled"}
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0


def test_enable_schedules_a_background_agent_and_disable_removes_it(scheduled):
    controller, *_ = scheduled
    status = autoupdate.enable(controller, interval=600, idle_seconds=300)
    assert status["enabled"] and status["interval"] == 600 and status["idle_seconds"] == 300
    plist = plistlib.loads(autoupdate.plist_path(controller).read_bytes())
    assert plist["Label"] == "local.agents-core.test.updater"
    assert plist["StartInterval"] == 600 and plist["RunAtLoad"] is False
    assert plist["ProgramArguments"][-2:] == ["auto-update", "run"]
    assert [call[0] for call in controller.calls][:2] == ["bootout", "bootstrap"]
    assert read_json(controller.directory / "service.json")["auto_update"]["enabled"] is True
    assert autoupdate.disable(controller) == {"enabled": False}
    assert not autoupdate.plist_path(controller).exists()
    assert read_json(controller.directory / "service.json")["auto_update"]["enabled"] is False
    with pytest.raises(ValueError):
        autoupdate.enable(controller, interval=10)


def test_enable_refuses_an_uninstalled_service(tmp_path):
    controller = SimpleNamespace(config={}, directory=tmp_path)
    with pytest.raises(RuntimeError, match="not installed"):
        autoupdate.enable(controller)
    assert not (tmp_path / "service.json").exists()


def test_disable_keeps_the_plist_when_launchd_still_runs_the_updater(scheduled):
    controller, *_ = scheduled
    autoupdate.write_plist(controller, 900)
    controller.launchctl = lambda *args, check=True: SimpleNamespace(returncode=5 if args[0] == "bootout" else 0)
    with pytest.raises(RuntimeError, match="still scheduled"):
        autoupdate.disable(controller)
    assert autoupdate.plist_path(controller).exists()


def test_option_like_remote_or_branch_is_refused(scheduled):
    controller, *_ = scheduled
    for remote, branch in [("--upload-pack=touch /tmp/x", "main"), ("origin", "-main")]:
        assert autoupdate.check_target(controller, remote, branch)["state"] == "skipped"


def test_git_timeout_is_recorded_not_raised(scheduled, monkeypatch):
    controller, root, old, _ = scheduled
    def stalled(controller, *args, check=True):
        raise subprocess.TimeoutExpired(args, autoupdate.GIT_TIMEOUT)
    monkeypatch.setattr(autoupdate, "_git", stalled)
    result = autoupdate.run(controller)
    assert result["state"] == "skipped" and "TimeoutExpired" in result["reason"]
    assert read_json(controller.directory / "auto-update.json")["reason"] == result["reason"]
    assert controller.stops == 0


def test_stdio_reader_defers_without_stopping_the_service(scheduled):
    controller, root, old, _ = scheduled
    lease = root / "data/.sessions.lock"
    lease.touch()
    reader = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1]); print(1, flush=True); time.sleep(60)",
                               str(lease)], stdout=subprocess.PIPE)
    try:
        reader.stdout.readline()
        result = autoupdate.run(controller)
    finally:
        reader.kill(); reader.wait()
    assert result["state"] == "deferred" and str(reader.pid) in result["reason"]
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0


def test_disable_during_a_run_prevents_the_update(scheduled, monkeypatch):
    controller, root, old, _ = scheduled
    check = autoupdate.check_target
    def disabled_meanwhile(controller):
        found = check(controller)
        write_json(controller.directory / "service.json", {**controller.config, "auto_update": {"enabled": False}})
        return found
    monkeypatch.setattr(autoupdate, "check_target", disabled_meanwhile)
    assert autoupdate.run(controller) == {"state": "disabled"}
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0



def test_cli_passes_a_zero_interval_through_to_validation(tmp_path):
    from src.daemon.control import main
    write_json(tmp_path / "service.json", {"installation": "/unused", "python": "/usr/bin/python3", "path": "/usr/bin"})
    with pytest.raises(ValueError, match="at least 60"):
        main(["--state", str(tmp_path), "auto-update", "enable", "--interval", "0"])
