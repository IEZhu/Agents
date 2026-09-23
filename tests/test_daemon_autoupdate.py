"""Unattended updates: decide without touching the service, then reuse the update transaction."""
import plistlib
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
    write_json(controller.directory / "service.json", controller.config)
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
