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


def release(root):
    """origin/main gets a release as the release PR makes it: a merge commit of origin/dev."""
    branch = git(root, "symbolic-ref", "--short", "HEAD")
    git(root, "fetch", "--quiet", "origin", "dev")
    dev = git(root, "rev-parse", "FETCH_HEAD")
    git(root, "fetch", "--quiet", "origin", "main")
    git(root, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    git(root, "merge", "--quiet", "--no-ff", "-m", "Release", dev)
    merge = git(root, "rev-parse", "HEAD")
    git(root, "push", "--quiet", "origin", "HEAD:refs/heads/main")
    git(root, "checkout", "--quiet", branch)
    return merge


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
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=dev\n")  # as `AGENTS_BRANCH=dev install.sh` writes it
    with pytest.raises(update_channel.ChannelError) as refused:
        update_channel.set_branch(root, "main")
    assert refused.value.code == "downgrade" and "--allow-downgrade" in str(refused.value)
    assert (root / ".env").read_text() == "AGENTS_AUTO_UPDATE_BRANCH=dev\n"
    result = update_channel.set_branch(root, "main", allow_downgrade=True)
    assert result["downgrade"] is True
    settings = update_channel.read(root)
    assert settings.allow_downgrade == f"main@{tip}" and settings.policy.downgrade_confirmed("main", tip)
    assert not settings.policy.downgrade_confirmed("main", old)
    # Saving again without the confirmation is refused and keeps the recorded one.
    with pytest.raises(update_channel.ChannelError):
        update_channel.set_branch(root, "main")
    assert update_channel.read(root).allow_downgrade == f"main@{tip}"


def test_policy_switches_only_from_the_previous_setting_to_the_env_setting():
    policy = update_channel.Policy(previous="release", setting="dev")
    assert policy.may_switch("release", "dev")
    # main and dev are no exception: only the previous setting is switched (issue #381, item 4).
    assert not policy.may_switch("main", "dev") and not policy.may_switch("feature", "dev")
    assert not policy.may_switch("dev", "dev") and not policy.may_switch(None, "dev")
    assert not policy.may_switch("HEAD", "dev")
    # A target that is not the .env setting came from the environment: it never switches.
    assert not policy.may_switch("release", "main")
    assert "exported" in policy.reason("release", "main") and "channel set main" in policy.reason("release", "main")
    unrecorded = update_channel.Policy(setting="dev")
    assert not unrecorded.may_switch("main", "dev")
    assert "`channel set main` and then `channel set dev`" in unrecorded.reason("main", "dev")


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
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=dev\nAGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=main\n")
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
    assert result["downgrade"] is False and "leave this checkout alone" in result["warnings"][0]


# --- Review round 1 (#381): switch policy, merge-aware downgrade, the .env writer -------------

def test_a_hand_edited_setting_without_a_previous_record_is_left_alone(installation):
    controller, root, old, target = installation
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=dev\n")
    summary = update_channel.summary(root, environ={})
    assert summary["action"] == "local" and "channel set main" in summary["reason"]


def test_an_exported_branch_never_plans_a_switch(installation):
    """txn-1: on dev with .env dev / previous main, an exported main only names the override."""
    controller, root, old, target = installation
    tip = push_dev(root)
    git(root, "checkout", "--quiet", "-B", "dev", tip)
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=dev\nAGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=main\n")
    settings = update_channel.read(root, environ={"AGENTS_AUTO_UPDATE_BRANCH": "main"})
    assert settings.overridden and update_channel.plan(settings, "dev") == "local"
    summary = update_channel.summary(root, environ={"AGENTS_AUTO_UPDATE_BRANCH": "main"})
    assert "exported" in summary["reason"] and "overrides .env (dev)" in summary["note"]
    assert update_channel.plan(update_channel.read(root, environ={}), "dev") == "follow"


def test_a_release_merge_commit_is_not_a_downgrade(installation):
    """txn-2: main after a release (a merge commit dev lacks) switches to dev without a confirmation."""
    controller, root, old, target = installation
    tip = push_dev(root)
    merge = release(root)
    git(root, "merge", "--quiet", "--ff-only", merge)  # the installation runs the release
    run = lambda *args: subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert update_channel.drops_commits(run, merge, tip) is False
    result = update_channel.set_branch(root, "dev")
    assert result["downgrade"] is False and "switches from main to dev" in result["next"]
    assert update_channel.read(root).allow_downgrade is None
    # dev with a commit main lacks is still a downgrade, release merge or not.
    git(root, "checkout", "--quiet", "-B", "dev", tip)
    (root / "more.txt").write_text("more")
    git(root, "add", "more.txt")
    git(root, "commit", "--quiet", "-m", "more on dev")
    assert update_channel.drops_commits(run, git(root, "rev-parse", "HEAD"), merge) is True
    assert update_channel.drops_commits(run, tip, merge) is False  # released dev to main goes nowhere back
    assert update_channel.drops_commits(lambda *args: subprocess.CompletedProcess(args, 128, "", "x"),
                                        tip, merge) is None


def test_the_downgrade_check_leaves_fetch_head_alone(installation, monkeypatch):
    """sec-1: the setting's fetch of a branch that is not local yet does not write FETCH_HEAD."""
    controller, root, old, target = installation
    tip = push_dev(root)
    git(root, "checkout", "--quiet", "-B", "dev", tip)
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=dev\n")
    git(root, "push", "--quiet", "origin", f"{tip}:refs/heads/main", "--force")
    git(root, "update-ref", "-d", "refs/remotes/origin/main")
    (root / "later.txt").write_text("x")
    git(root, "add", "later.txt")
    git(root, "commit", "--quiet", "-m", "local on dev")
    # origin/main gets a commit this checkout has never seen.
    remote = git(root, "remote", "get-url", "origin")
    clone = root.parent / "other"
    subprocess.run(["git", "clone", "--quiet", "-b", "main", remote, str(clone)], check=True, capture_output=True)
    (clone / "x.txt").write_text("x")
    subprocess.run(["git", "-C", str(clone), "add", "x.txt"], check=True)
    subprocess.run(["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "x"],
                   check=True)
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True, capture_output=True)
    fetch_head = root / ".git" / "FETCH_HEAD"
    fetch_head.write_text("0" * 40 + "\t\tsentinel\n")
    with pytest.raises(update_channel.ChannelError) as refused:
        update_channel.set_branch(root, "main")
    assert refused.value.code == "downgrade"
    assert fetch_head.read_text() == "0" * 40 + "\t\tsentinel\n"
    # With a git older than 2.29 the check is unavailable: saved with a warning, nothing fetched.
    monkeypatch.setattr(update_channel, "_git_version", lambda root: (2, 28))
    monkeypatch.setattr(update_channel, "remote_tip", lambda root, remote, branch: "1" * 40)
    result = update_channel.set_branch(root, "main")
    assert any("could not check" in warning for warning in result["warnings"])
    assert fetch_head.read_text() == "0" * 40 + "\t\tsentinel\n"
    assert update_channel.read(root).allow_downgrade is None


def test_saving_the_same_branch_twice_keeps_the_previous_setting(installation):
    """tests-5: a second save of dev must not turn the previous setting into dev."""
    controller, root, old, target = installation
    push_dev(root)
    git(root, "push", "--quiet", "origin", f"{old}:refs/heads/release")
    git(root, "checkout", "--quiet", "-b", "release")
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=release\n")
    update_channel.set_branch(root, "dev")
    result = update_channel.set_branch(root, "dev")
    settings = update_channel.read(root, environ={})
    assert settings.previous == "release" and update_channel.plan(settings, "release") == "switch"
    assert "warnings" not in result


def test_a_new_env_is_created_private(installation):
    """tests-6: .env holds secrets; the writer creates it 0600."""
    controller, root, old, target = installation
    push_dev(root)
    assert not (root / ".env").exists()
    update_channel.set_branch(root, "dev")
    assert (root / ".env").stat().st_mode & 0o777 == 0o600


def test_an_implausible_stored_value_never_becomes_an_assignment(installation):
    """sec-2: a previous value that is no branch name is not written back."""
    controller, root, old, target = installation
    push_dev(root)
    (root / ".env").write_text('OPENAI_API_KEY=real\nAGENTS_AUTO_UPDATE_BRANCH="x\\nOPENAI_API_KEY=pwned"\n')
    update_channel.set_branch(root, "dev")
    from dotenv import dotenv_values
    values = dotenv_values(root / ".env")
    assert values["OPENAI_API_KEY"] == "real" and values["AGENTS_AUTO_UPDATE_BRANCH"] == "dev"
    assert "AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH" not in values
    with pytest.raises(update_channel.ChannelError) as refused:
        update_channel.rewrite_env(root / ".env", {"AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH": "x\nOPENAI_API_KEY=pwned"})
    assert refused.value.code == "invalid"
    for bad in ("a b", "dev@" + "z" * 40, "dev@abc", "#x", "$HOME"):
        with pytest.raises(update_channel.ChannelError):
            update_channel.rewrite_env(root / ".env", {"AGENTS_AUTO_UPDATE_BRANCH": bad})
    update_channel.rewrite_env(root / ".env", {"AGENTS_AUTO_UPDATE_ALLOW_DOWNGRADE": "main@" + "a" * 40})
    assert dotenv_values(root / ".env")["OPENAI_API_KEY"] == "real"


def test_set_branch_keeps_a_multi_line_secret_and_crlf(installation):
    """sec-3: the setting is changed binding by binding."""
    controller, root, old, target = installation
    push_dev(root)
    secret = 'PRIVATE_KEY="-----BEGIN-----\r\nAGENTS_AUTO_UPDATE_BRANCH=x\r\n-----END-----"\r\n'
    (root / ".env").write_bytes(("A=1\r\n" + secret).encode())
    update_channel.set_branch(root, "dev")
    assert (root / ".env").read_bytes().decode() == (
        "A=1\r\n" + secret + "AGENTS_AUTO_UPDATE_BRANCH=dev\r\nAGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=main\r\n")


def test_saving_a_branch_another_worktree_has_warns(installation, tmp_path):
    controller, root, old, target = installation
    tip = push_dev(root)
    git(root, "worktree", "add", "--quiet", "-b", "dev", str(tmp_path / "wt"), tip)
    result = update_channel.set_branch(root, "dev")
    assert any("checked out in another worktree" in warning for warning in result["warnings"])
