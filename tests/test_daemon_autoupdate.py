"""Unattended updates: decide without touching the service, then reuse the update transaction."""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.daemon import autoupdate
from src.daemon.state import read_json, write_json
from tests.test_daemon_update import (git, installation, publish_pyproject_update,  # noqa: F401  (fixtures)
                                      publish_requirements, venv_installation)


@pytest.fixture
def scheduled(installation):  # noqa: F811
    controller, root, old, target = installation
    controller.label = "local.agents-core.test"
    controller.plist = controller.directory / "LaunchAgents" / (controller.label + ".plist")
    controller.plist.parent.mkdir()
    controller.config.update(python="/usr/bin/python3", auto_update={"enabled": True, "idle_seconds": 120})
    write_json(controller.directory / "service.json", controller.config)
    controller.health = {"inflight": 0, "io_pending": 0, "idle_seconds": 600}
    controller.calls = []
    controller.loaded, controller.fail = set(), set()

    def launchctl(*args, check=True):
        """A small launchd: bootstrap loads a job, bootout unloads it, print reports it."""
        controller.calls.append(args)
        verb = args[0]
        code = 0
        if verb in controller.fail:
            code = 5
        elif verb == "bootstrap":
            controller.loaded.add(plistlib.loads(Path(args[2]).read_bytes())["Label"])
        elif verb == "bootout":
            controller.loaded.discard(args[1].rsplit("/", 1)[1])
        elif verb == "print":
            code = 0 if args[1].rsplit("/", 1)[1] in controller.loaded else 113
        if check and code:
            raise subprocess.CalledProcessError(code, ["launchctl", *args])
        return SimpleNamespace(returncode=code)

    controller.launchctl = launchctl
    controller.status = lambda: {**controller.health, "state": "ready" if controller.running else "stopped"}
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


def test_pinned_runtime_is_skipped_before_fetch_or_stop(scheduled, monkeypatch, tmp_path):
    controller, root, old, _ = scheduled
    source = str(tmp_path / "pinned-source")
    controller.config["runtime_root"] = source
    write_json(controller.directory / "service.json", controller.config)
    monkeypatch.setattr(autoupdate, "_git", lambda *args, **kwargs: pytest.fail("pinned source must not fetch"))
    result = autoupdate.run(controller)
    assert result["state"] == "skipped" and "explicit owner-controlled runtime migration" in result["reason"]
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == controller.builds == 0
    assert read_json(controller.directory / "service.json")["runtime_root"] == source


def test_idle_service_is_updated_through_the_transaction(scheduled):
    controller, root, old, target = scheduled
    result = autoupdate.run(controller)
    assert result["state"] == "UPDATED" and result["target"] == target
    assert git(root, "rev-parse", "HEAD") == target
    assert controller.stops == 1 and controller.probes == 1
    assert read_json(controller.directory / "auto-update.json")["state"] == "UPDATED"
    assert autoupdate.run(controller)["state"] == "up_to_date"
    assert controller.stops == 1


@pytest.mark.parametrize("health", [{"idle_seconds": 5}, {"local_inference_pending": 1}])
def test_work_arriving_during_the_build_defers_the_restart_to_the_next_run(scheduled, monkeypatch, health):
    from src.daemon import update
    controller, root, old, target = scheduled
    build = update.prepare_reindex

    def busy_build(current, staging_dir):
        controller.health.update(health)  # Detached work also blocks the activation check.
        return build(current, staging_dir)
    monkeypatch.setattr(update, "prepare_reindex", busy_build)

    result = autoupdate.run(controller)
    assert result["state"] == "deferred" and result["reason"] == "service is busy"
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0 and controller.builds == 1

    controller.health.update(idle_seconds=600, local_inference_pending=0)
    assert autoupdate.run(controller)["state"] == "UPDATED"
    assert git(root, "rev-parse", "HEAD") == target
    assert controller.stops == 1 and controller.builds == 1


def test_stop_during_the_build_is_respected(scheduled, monkeypatch):
    from src.daemon import update
    controller, root, old, target = scheduled
    build = update.prepare_reindex

    def build_while_stopped(current, staging_dir):
        built = build(current, staging_dir)
        controller.running = False  # `stop` succeeds: the build does not hold the control lock
        return built
    monkeypatch.setattr(update, "prepare_reindex", build_while_stopped)

    result = autoupdate.run(controller)
    assert result["state"] == "deferred" and result["reason"] == "service is stopped"
    assert git(root, "rev-parse", "HEAD") == old and not controller.running and controller.probes == 0


def test_stdio_reader_starting_during_the_build_defers_the_restart(scheduled, monkeypatch):
    from src.daemon import update
    controller, root, old, target = scheduled
    readers, build = [], update.prepare_reindex
    monkeypatch.setattr(autoupdate, "other_readers", lambda current, daemon_pid: list(readers))

    def build_while_a_reader_starts(current, staging_dir):
        readers.append(4242)
        return build(current, staging_dir)
    monkeypatch.setattr(update, "prepare_reindex", build_while_a_reader_starts)

    result = autoupdate.run(controller)
    assert result["state"] == "deferred" and result["reason"] == "stdio readers hold the installation: [4242]"
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0 and controller.builds == 1


@pytest.mark.parametrize("health", [{"inflight": 1}, {"io_pending": 1}, {"local_inference_pending": 1}, {"idle_seconds": 5}])
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


def test_scheduled_update_accepts_metadata_and_new_extra(scheduled):
    from tests.test_daemon_update_dependencies import MANIFEST
    controller, root, _, target = scheduled
    after = MANIFEST.replace('version = "1.0"', 'version = "2.0"')
    after += 'thread-status = ["claude-agent-sdk==0.2.168"]\n'
    base, changed = publish_pyproject_update(root, target, MANIFEST, after)
    assert autoupdate.check_target(controller) == {"state": "available", "head": base, "target": changed}
    result = autoupdate.run(controller)
    assert result["state"] == "UPDATED" and result["target"] == changed
    assert git(root, "rev-parse", "HEAD") == changed and controller.stops == 1


@pytest.fixture
def venv_scheduled(scheduled, venv_installation):  # noqa: F811
    """A scheduled service on the installation's setup .venv (`venv_installation`)."""
    return venv_installation


def test_scheduled_update_skips_an_environment_change_for_a_service_outside_venv(venv_scheduled):
    from tests.test_daemon_update_dependencies import MANIFEST
    env = venv_scheduled
    controller, root = env.controller, env.root
    controller.config["python"] = "/usr/bin/python3"
    write_json(controller.directory / "service.json", controller.config)
    (root / "pyproject.toml").write_text(MANIFEST.replace("httpx>=0.28,<0.29", "httpx>=1"))
    git(root, "commit", "-qam", "existing extra")
    changed = git(root, "rev-parse", "HEAD")
    git(root, "push", "-q", "origin", "main")
    git(root, "reset", "--hard", env.base)
    result = autoupdate.run(controller)
    assert result["state"] == "skipped" and result["target"] == changed
    assert "not an interpreter of the installation's .venv" in result["reason"]
    assert controller.stops == controller.builds == 0 and env.builder.calls == []


def test_check_target_notes_an_environment_and_skips_bridge_package_changes(venv_scheduled):
    env = venv_scheduled
    controller, root = env.controller, env.root
    target = publish_requirements(root, env.base, "mcp==2\n")
    found = autoupdate.check_target(controller)
    assert found["state"] == "available" and found["target"] == target
    assert "requirements.txt changed" in found["environment"]
    (root / "bridge").mkdir()
    push_to_remote(root, "bridge/package.json", "{}\n", "bridge manifest")
    found = autoupdate.check_target(controller)
    assert found["state"] == "skipped" and "bridge/package.json" in found["reason"]
    assert controller.stops == controller.builds == 0 and env.builder.calls == []


def test_scheduled_update_delivers_a_dependency_change(venv_scheduled):
    from src import environments
    env = venv_scheduled
    controller, root = env.controller, env.root
    target = publish_requirements(root, env.base, "mcp==2\n")
    result = autoupdate.run(controller)
    assert result["state"] == "UPDATED" and result["target"] == target and "environment" in result
    assert git(root, "rev-parse", "HEAD") == target and controller.stops == 1
    assert environments.state(str(root))["kind"] == "link"
    assert read_json(controller.directory / "auto-update.json")["environment"] == result["environment"]


def test_branch_moving_after_the_check_applies_nothing(scheduled, monkeypatch):
    controller, root, old, target = scheduled
    checked = autoupdate.check_target

    def check_then_move(*args, **kwargs):
        found = checked(*args, **kwargs)
        push_to_remote(root, "later.txt", "later\n", "lands after the check")
        return found

    monkeypatch.setattr(autoupdate, "check_target", check_then_move)
    result = autoupdate.run(controller)
    # The transaction fetched a commit that was never checked: refuse before any file changes.
    assert result["state"] == "deferred" and "branch moved" in result["reason"] and result["target"] == target
    assert git(root, "rev-parse", "HEAD") == old
    assert controller.running and not (controller.directory / "transaction.json").exists()
    assert read_json(controller.directory / "auto-update.json")["state"] == "deferred"

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
    assert "local.agents-core.test.updater" in controller.loaded
    assert read_json(controller.directory / "service.json")["auto_update"]["enabled"] is True
    assert autoupdate.disable(controller) == {"enabled": False}
    assert not autoupdate.plist_path(controller).exists()
    assert read_json(controller.directory / "service.json")["auto_update"]["enabled"] is False
    with pytest.raises(ValueError):
        autoupdate.enable(controller, interval=10)


@pytest.mark.skipif(os.name == "nt", reason="launchd plists, written as text, exist on macOS only")
def test_enable_changes_nothing_when_launchd_fails(scheduled):
    controller, *_ = scheduled
    autoupdate.enable(controller, interval=600)
    plist = autoupdate.plist_path(controller)
    before = plist.read_bytes()
    # The running job cannot be unloaded: config, plist and job stay as they were.
    controller.fail.add("bootout")
    with pytest.raises(RuntimeError, match="could not unload"):
        autoupdate.enable(controller, interval=900)
    assert plist.read_bytes() == before and autoupdate.settings(controller)["interval"] == 600
    # The new job fails to load: the previous plist and job come back, config untouched.
    controller.fail = {"bootstrap"}
    with pytest.raises(subprocess.CalledProcessError):
        autoupdate.enable(controller, interval=900)
    assert plist.read_bytes() == before and autoupdate.settings(controller)["interval"] == 600
    assert read_json(controller.directory / "service.json")["auto_update"]["interval"] == 600


def test_enable_from_scratch_leaves_no_plist_when_launchd_refuses(scheduled):
    controller, *_ = scheduled
    before = read_json(controller.directory / "service.json")
    controller.fail.add("bootstrap")
    with pytest.raises(subprocess.CalledProcessError):
        autoupdate.enable(controller, interval=900)
    assert not autoupdate.plist_path(controller).exists()
    assert read_json(controller.directory / "service.json") == before


@pytest.mark.parametrize("work_key", ["inflight", "local_inference_pending"])
def test_work_arriving_after_the_first_check_defers_before_draining(scheduled, monkeypatch, work_key):
    controller, root, old, _ = scheduled
    enabled = autoupdate._enabled_on_disk
    calls = []

    def busy_right_after_the_check(controller):
        calls.append(enabled(controller))
        if len(calls) == 1:  # a request lands between the first check and the transaction
            controller.health[work_key] = 1
        return calls[-1]

    monkeypatch.setattr(autoupdate, "_enabled_on_disk", busy_right_after_the_check)
    result = autoupdate.run(controller)
    assert result["state"] == "deferred" and result["reason"] == "service is busy"
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0

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


@pytest.mark.skipif(not shutil.which("lsof"), reason="lsof lists the lease's readers; Windows probes "
                    "stdio slots (tests/test_daemon_service.py)")
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



def test_disable_after_the_last_check_still_wins_under_the_lock(scheduled, monkeypatch):
    controller, root, old, _ = scheduled
    enabled = autoupdate._enabled_on_disk
    calls = []

    def disable_right_after_the_check(controller):
        calls.append(enabled(controller))
        if len(calls) == 1:  # `disable` runs between this check and the transaction
            write_json(controller.directory / "service.json", {**controller.config, "auto_update": {"enabled": False}})
        return calls[-1]

    monkeypatch.setattr(autoupdate, "_enabled_on_disk", disable_right_after_the_check)
    assert autoupdate.run(controller) == {"state": "disabled"}
    assert calls == [True, False]
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0


def test_stop_after_the_last_check_is_not_undone(scheduled, monkeypatch):
    controller, root, old, _ = scheduled
    enabled = autoupdate._enabled_on_disk
    calls = []

    def stop_right_after_the_check(controller):
        calls.append(enabled(controller))
        if len(calls) == 1:  # a manual `stop` lands between this check and the transaction
            controller.running = False
        return calls[-1]

    monkeypatch.setattr(autoupdate, "_enabled_on_disk", stop_right_after_the_check)
    result = autoupdate.run(controller)
    assert result["state"] == "deferred" and result["reason"] == "service is stopped"
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0 and not controller.running

def test_up_to_date_run_replaces_an_older_status(scheduled):
    controller, root, _, target = scheduled
    write_json(controller.directory / "auto-update.json", {"state": "deferred", "reason": "service is busy"})
    git(root, "merge", "--quiet", "--ff-only", target)
    assert autoupdate.run(controller)["state"] == "up_to_date"
    assert read_json(controller.directory / "auto-update.json")["state"] == "up_to_date"

def test_cli_passes_a_zero_interval_through_to_validation(tmp_path):
    from src.daemon.control import main
    write_json(tmp_path / "service.json", {"installation": "/unused", "python": "/usr/bin/python3", "path": "/usr/bin"})
    with pytest.raises(ValueError, match="at least 60"):
        main(["--state", str(tmp_path), "auto-update", "enable", "--interval", "0"])


def test_up_to_date_service_on_an_old_model_generation_is_switched_once(scheduled, monkeypatch):
    from src.daemon import control, update
    from src.model_migration import DEFAULT_MODEL, GENERATION
    controller, root, old, target = scheduled
    git(root, "merge", "--quiet", "--ff-only", target)
    controller.config.pop("model_generation")
    write_json(controller.directory / "service.json", controller.config)
    monkeypatch.setattr(control, "switched_model_config", lambda config: {
        **config, "model": DEFAULT_MODEL, "model_generation": GENERATION,
        "model_artifact": "export@rev", "model_path": "/cache/local/copy"})
    monkeypatch.setattr(update, "reindex", lambda current: None)

    result = autoupdate.run(controller)

    assert result["state"] == "UP_TO_DATE" and result["model"] == DEFAULT_MODEL and result["model_switch"]
    assert read_json(controller.directory / "service.json")["model"] == DEFAULT_MODEL
    assert controller.stops == 1 and controller.probes == 1
    assert autoupdate.run(controller)["state"] == "up_to_date"
    assert controller.stops == 1


def test_switch_waits_for_an_idle_service(scheduled, monkeypatch):
    controller, root, old, target = scheduled
    git(root, "merge", "--quiet", "--ff-only", target)
    controller.config.pop("model_generation")
    controller.health["inflight"] = 1
    result = autoupdate.run(controller)
    assert result["state"] == "deferred" and result["reason"] == "service is busy"
    assert controller.stops == 0


def test_autodev_run_lease_defers_the_update_until_the_run_ends(scheduled):
    from src.schedules import lease
    controller, root, old, target = scheduled
    with lease.hold(controller.directory):
        result = autoupdate.run(controller)
        assert result["state"] == "deferred" and result["reason"] == "service is busy: an autodev run holds its run lease"
        assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0
    assert autoupdate.run(controller)["state"] == "UPDATED"
    assert git(root, "rev-parse", "HEAD") == target


def test_an_autodev_run_cannot_start_while_the_update_runs(scheduled, monkeypatch):
    """The update holds the run lease from its first check to the end, so no run starts between
    its lease check and maintenance.json (the run's side is in tests/test_schedules_runner.py)."""
    from src.daemon import update
    from src.schedules import lease
    controller, root, old, target = scheduled
    build, refused, during_maintenance = update.prepare_reindex, [], []

    def build_while_a_run_tries_to_start(current, staging_dir):
        with pytest.raises(lease.LeaseBusy):
            with lease.hold(controller.directory, wait=0.2):
                pass
        refused.append("build")
        return build(current, staging_dir)
    monkeypatch.setattr(update, "prepare_reindex", build_while_a_run_tries_to_start)
    stop = controller._stop

    def stop_while_a_run_tries_to_start():
        assert (controller.directory / "maintenance.json").exists()
        during_maintenance.append(lease.held(controller.directory))
        return stop()
    monkeypatch.setattr(controller, "_stop", stop_while_a_run_tries_to_start)
    result = autoupdate.run(controller)
    assert result["state"] == "UPDATED" and git(root, "rev-parse", "HEAD") == target
    assert refused == ["build"] and during_maintenance == [True]
    assert not lease.held(controller.directory)  # released with the transaction


# --- Update channel (#381) ----------------------------------------------------

def test_scheduled_run_follows_the_channel_setting_and_then_fast_forwards_dev(scheduled):
    from src import update_channel
    from tests.test_update_channel import push_dev
    controller, root, old, target = scheduled
    tip = push_dev(root)
    update_channel.set_branch(root, "dev")  # the scheduled run reads it from .env
    result = autoupdate.run(controller)
    assert result["state"] == "UPDATED" and result["target"] == tip
    assert result["switch"] == {"from": "main", "to": "dev", "downgrade": False}
    assert git(root, "symbolic-ref", "--short", "HEAD") == "dev" and controller.stops == 1
    assert autoupdate.status(controller)["channel"] == {
        "configured_branch": "dev", "source": ".env", "checked_out_branch": "dev", "action": "follow",
        "revision": update_channel.read(root).revision}
    assert autoupdate.run(controller)["state"] == "up_to_date"

    # New dev commits fast-forward as on main before.
    head = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "--quiet", "--detach")
    (root / "later.txt").write_text("later\n")
    git(root, "add", "later.txt")
    git(root, "commit", "--quiet", "-m", "later on dev")
    later = git(root, "rev-parse", "HEAD")
    git(root, "push", "--quiet", "origin", "HEAD:refs/heads/dev")
    git(root, "checkout", "--quiet", "dev")
    assert git(root, "rev-parse", "HEAD") == head
    result = autoupdate.run(controller)
    assert result["state"] == "UPDATED" and "switch" not in result
    assert git(root, "rev-parse", "HEAD") == later and controller.stops == 2


def test_scheduled_run_skips_a_third_branch_and_an_unconfirmed_downgrade(scheduled):
    from src import update_channel
    from tests.test_update_channel import push_dev
    controller, root, old, target = scheduled
    tip = push_dev(root)
    update_channel.set_branch(root, "dev")
    git(root, "checkout", "--quiet", "-b", "experiment")
    result = autoupdate.run(controller)
    assert result == {**result, "state": "skipped",
                      "reason": "checked-out branch is experiment, not dev or the previous setting main: local "
                                "development"}
    git(root, "checkout", "--quiet", "-B", "dev", tip)
    env = root / ".env"
    env.write_text(env.read_text().replace("AGENTS_AUTO_UPDATE_BRANCH=dev", "AGENTS_AUTO_UPDATE_BRANCH=main")
                   .replace("AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=main", "AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=dev"))
    result = autoupdate.run(controller)
    assert result["state"] == "skipped" and "--allow-downgrade" in result["reason"]
    assert "keep dev with `channel set dev`" in result["reason"]  # tests-3: both ways out
    assert result["switch"] == {"from": "dev", "to": "main", "downgrade": True}
    assert controller.stops == 0
    update_channel.set_branch(root, "main", allow_downgrade=True)
    result = autoupdate.run(controller)
    assert result["state"] == "UPDATED" and result["switch"]["downgrade"] is True
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main" and git(root, "rev-parse", "HEAD") == target


def test_scheduled_run_reads_only_the_update_keys_from_env(scheduled, monkeypatch):
    controller, root, old, target = scheduled
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=main\nUNRELATED_SECRET=not-for-the-controller\n")
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)
    assert autoupdate.check_target(controller)["state"] == "available"
    assert "UNRELATED_SECRET" not in os.environ
    # An exported value overrides .env for this command.
    monkeypatch.setenv("AGENTS_AUTO_UPDATE_BRANCH", "elsewhere")
    found = autoupdate.check_target(controller)
    assert found["state"] == "skipped" and "is exported and overrides .env (main)" in found["reason"]


# --- Review round 1 (#381) ---------------------------------------------------

def test_check_target_skips_a_target_branch_checked_out_in_another_worktree(scheduled, tmp_path):
    """txn-4: otherwise every interval would build, stop, fail at `checkout -B` and roll back."""
    from src import update_channel
    from tests.test_update_channel import push_dev
    controller, root, old, target = scheduled
    tip = push_dev(root)
    update_channel.set_branch(root, "dev")
    git(root, "worktree", "add", "--quiet", "-b", "dev", str(tmp_path / "wt"), tip)
    found = autoupdate.check_target(controller)
    assert found["state"] == "skipped" and "checked out in another worktree" in found["reason"]
    result = autoupdate.run(controller)
    assert result["state"] == "skipped" and controller.stops == 0 and controller.builds == 0


def test_check_target_refuses_a_local_target_ahead_of_its_remote_and_an_invalid_name(scheduled):
    """tests-4: the switch prechecks that run before the service is touched."""
    from src import update_channel
    from tests.test_update_channel import push_dev
    controller, root, old, target = scheduled
    tip = push_dev(root)
    update_channel.set_branch(root, "dev")
    git(root, "branch", "dev", tip)
    tree = git(root, "rev-parse", f"{tip}^{{tree}}")
    git(root, "branch", "-f", "dev", git(root, "commit-tree", tree, "-p", tip, "-m", "local on dev"))
    found = autoupdate.check_target(controller)
    assert found["state"] == "skipped" and found["reason"] == "local branch dev has commits origin/dev lacks"
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=dev..x\nAGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=main\n")
    found = autoupdate.check_target(controller)
    assert found == {"state": "skipped", "reason": "invalid branch name: 'dev..x'"}
