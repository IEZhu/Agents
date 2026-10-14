"""The update channel setting (#381): one .env key, read by the controller without the rest of .env."""
import json
import os
import subprocess

import pytest

from src import update_channel
from src.daemon import control
from src.daemon.state import read_json
from tests.test_daemon_update import git, installation  # noqa: F401  (fixture)


def push_dev(root, content="dev\n"):
    """Publish `dev` on the remote one commit ahead of its main, without moving the installation."""
    head = git(root, "rev-parse", "HEAD")
    branch = git(root, "symbolic-ref", "--short", "HEAD")
    git(root, "fetch", "--quiet", "origin", "main")
    git(root, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    (root / "dev.txt").write_text(content)
    git(root, "add", "dev.txt")
    git(root, "commit", "--quiet", "-m", "on dev")
    tip = git(root, "rev-parse", "HEAD")
    git(root, "push", "--quiet", "origin", "HEAD:refs/heads/dev")
    git(root, "checkout", "--quiet", branch)
    assert git(root, "rev-parse", "HEAD") == head
    return tip


def test_controller_reads_only_the_update_keys_and_an_export_overrides_them(installation, monkeypatch):
    controller, root, old, target = installation
    (root / ".env").write_text("UNRELATED_SECRET=do-not-load\nAGENTS_AUTO_UPDATE_BRANCH=dev  # channel\n"
                               "AGENTS_AUTO_UPDATE_REMOTE=origin\n")
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)
    settings = update_channel.read(root)
    assert (settings.branch, settings.sources["AGENTS_AUTO_UPDATE_BRANCH"]) == ("dev", ".env")
    assert settings.sources["AGENTS_AUTO_UPDATE"] == "default" and settings.enabled
    assert "UNRELATED_SECRET" not in os.environ
    assert set(update_channel.file_values(root)) == {"AGENTS_AUTO_UPDATE_BRANCH", "AGENTS_AUTO_UPDATE_REMOTE"}
    monkeypatch.setenv("AGENTS_AUTO_UPDATE_BRANCH", "main")
    settings = update_channel.read(root)
    assert (settings.branch, settings.sources["AGENTS_AUTO_UPDATE_BRANCH"]) == ("main", "environment")
    shown = update_channel.show(root, network=False)
    assert shown["source"] == "environment" and "overrides" in shown["note"]


@pytest.mark.parametrize("name", ["-main", "a b", "foo..bar", "dev@{1}", "x.lock", "HEAD~1", "it's", ""])
def test_unsafe_or_invalid_names_are_refused(installation, name):
    controller, root, old, target = installation
    with pytest.raises(update_channel.ChannelError) as refused:
        update_channel.set_branch(root, name)
    assert refused.value.code == "invalid"
    assert not (root / ".env").exists()


def test_branch_missing_on_the_remote_is_refused(installation):
    controller, root, old, target = installation
    with pytest.raises(update_channel.ChannelError) as refused:
        update_channel.set_branch(root, "release")
    assert refused.value.code == "unknown_branch"
    assert not (root / ".env").exists()


def test_set_keeps_the_rest_of_env_and_records_the_previous_setting(installation):
    controller, root, old, target = installation
    push_dev(root)
    env = root / ".env"
    env.write_text("EMBEDDING_PROMPTS=off\nAGENTS_AUTO_UPDATE_BRANCH=main           # only this branch\nOTHER=1")
    env.chmod(0o640)
    before = update_channel.read(root).revision
    result = update_channel.set_branch(root, "dev", expected_revision=before)
    assert result["branch"] == "dev" and result["previous_setting"] == "main" and not result["downgrade"]
    assert "switches from main to dev" in result["next"] and "warnings" not in result
    assert env.read_text() == ("EMBEDDING_PROMPTS=off\nAGENTS_AUTO_UPDATE_BRANCH=dev           # only this branch\n"
                               "OTHER=1\nAGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=main\n")
    assert env.stat().st_mode & 0o777 == 0o640
    assert result["revision"] == update_channel.read(root).revision != before
    # A writer that read the old revision does not overwrite the newer value.
    with pytest.raises(update_channel.ChannelError) as refused:
        update_channel.set_branch(root, "main", expected_revision=before)
    assert refused.value.code == "conflict"
    assert update_channel.read(root).branch == "dev"


def test_a_feature_branch_is_saved_with_a_warning(installation):
    controller, root, old, target = installation
    git(root, "push", "--quiet", "origin", f"{target}:refs/heads/feature/x")
    result = update_channel.set_branch(root, "feature/x")
    assert "feature branches get rewritten" in result["warnings"][0]
    assert update_channel.read(root).branch == "feature/x"


def test_downgrade_needs_a_confirmation_bound_to_head(installation):
    controller, root, old, target = installation
    tip = push_dev(root)
    git(root, "checkout", "--quiet", "-B", "dev", tip)  # the installation runs dev, ahead of main
    with pytest.raises(update_channel.ChannelError) as refused:
        update_channel.set_branch(root, "main")
    assert refused.value.code == "downgrade" and "--allow-downgrade" in str(refused.value)
    assert not (root / ".env").exists()
    result = update_channel.set_branch(root, "main", allow_downgrade=True)
    assert result["downgrade"] is True
    settings = update_channel.read(root)
    assert settings.allow_downgrade == f"main@{tip}" and settings.policy.downgrade_confirmed("main", tip)
    assert not settings.policy.downgrade_confirmed("main", old)
    # Saving again without the confirmation is refused and keeps the recorded one.
    with pytest.raises(update_channel.ChannelError):
        update_channel.set_branch(root, "main")
    assert update_channel.read(root).allow_downgrade == f"main@{tip}"


def test_policy_switches_only_from_channel_branches():
    policy = update_channel.Policy(previous="release")
    assert policy.may_switch("main", "dev") and policy.may_switch("dev", "main") and policy.may_switch("release", "dev")
    assert not policy.may_switch("feature", "dev") and not policy.may_switch("dev", "dev")
    assert not policy.may_switch(None, "dev") and not policy.may_switch("HEAD", "dev")


def test_cli_set_and_show_under_the_control_lock(installation, capsys):
    controller, root, old, target = installation
    tip = push_dev(root)
    assert control.main(["--state", str(controller.directory), "channel", "set", "dev"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["state"] == "saved" and saved["branch"] == "dev"
    assert control.main(["--state", str(controller.directory), "channel", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["branch"] == "dev" and shown["source"] == ".env" and shown["checked_out"] == "main"
    assert shown["head"] == old and shown["remote_tip"] == tip and shown["previous"] == "main"
    assert set(shown["last_update"]) == {"service", "stdio"}

    from src.file_lock import file_lock
    with file_lock(controller.directory / "control.lock"):
        assert control.main(["--state", str(controller.directory), "channel", "set", "main"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "not_changed"
    assert control.main(["--state", str(controller.directory), "channel", "set", "--", "-x"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "invalid"
    assert update_channel.read(root).branch == "dev"


def test_module_without_the_service_sets_and_shows(installation, capsys):
    controller, root, old, target = installation
    push_dev(root)
    assert update_channel.main(["--root", str(root), "set", "dev"]) == 0
    assert json.loads(capsys.readouterr().out)["branch"] == "dev"
    assert update_channel.main(["--root", str(root), "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["branch"] == "dev" and "service" not in shown["last_update"]
    assert update_channel.main(["--root", str(root), "set", "nope"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "unknown_branch"


def test_the_module_needs_no_service_or_engine_import():
    script = ("import sys, src.update_channel; "
              "print(any(m.startswith(('src.daemon', 'src.engine')) for m in sys.modules))")
    result = subprocess.run([os.sys.executable, "-c", script], capture_output=True, text=True, check=True,
                            cwd=update_channel.INSTALL_ROOT)
    assert result.stdout.strip() == "False"


def test_status_health_and_overview_show_both_branches(installation, monkeypatch):
    from src.daemon import autoupdate, overview
    controller, root, old, target = installation
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=dev\n")
    controller.launchctl = lambda *args, check=True: subprocess.CompletedProcess(args, 113)
    monkeypatch.setattr(autoupdate.service, "PLATFORM", "darwin")
    controller.label, controller.plist = "local.agents-core.test", controller.directory / "x.plist"
    status = autoupdate.status(controller)
    assert status["channel"]["configured_branch"] == "dev" and status["channel"]["checked_out_branch"] == "main"
    assert status["channel"]["action"] == "switch"
    data = overview.updates(controller.directory, root)
    assert (data["configured_branch"], data["checked_out_branch"], data["auto_update"]) == ("dev", "main", False)
    # The service's own environment (its .env loaded at start) does not mask a newer saved value.
    monkeypatch.setenv("AGENTS_AUTO_UPDATE_BRANCH", "main")
    assert overview.updates(controller.directory, root)["configured_branch"] == "dev"
    from src import version
    monkeypatch.setattr(version, "ROOT", root)
    version.agents_core_branch.cache_clear()
    try:
        assert version.agents_core_branch() == "main"
        git(root, "checkout", "--quiet", "--detach")
        version.agents_core_branch.cache_clear()
        assert version.agents_core_branch() is None
    finally:
        version.agents_core_branch.cache_clear()
    assert read_json(controller.directory / "service.json")["installation"] == str(root)


def test_health_reports_the_checked_out_branch(tmp_path, monkeypatch):
    from src.daemon import app
    monkeypatch.setattr(app, "agents_core_branch", lambda: "dev")
    directory = tmp_path / "service"
    directory.mkdir()
    service = app.create_app(directory, "t" * 40, runtime_loader=lambda port: None).state.service
    health = service.health()
    assert health["agents_core_branch"] == "dev" and "agents_core_version" in health


def test_setting_from_a_third_branch_needs_no_confirmation_and_warns(installation):
    controller, root, old, target = installation
    tip = push_dev(root)
    git(root, "checkout", "--quiet", "-B", "experiment", tip)  # local development, ahead of main
    result = update_channel.set_branch(root, "main")
    assert result["downgrade"] is False and "leave it alone" in result["warnings"][0]
