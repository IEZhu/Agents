"""The installers' sync step and setup from the environment (#171, src/user_sync/installer.py).

The step runs in-process with scripted answers and stubs for the terminal wizard and the daemon's
settings page. Setup from the environment runs against the fake GitHub of ``test_user_sync_github``
and local bare repositories, reached through the fake SSH transport of
``test_user_sync_github_setup``. Nothing reaches the real GitHub, a daemon, an OS secret store or a
scheduler. ``test_installer_sync.py`` runs the installers' own sections around this step.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from src.user_flows import FlowLibrary
from src.user_sync import engine as engine_module, installer
from src.user_sync.__main__ import main as cli
from src.user_sync.engine import SyncError, Syncer
from src.user_sync.wizard import Cancelled
from tests.test_user_sync import plain_git, remote_files
from tests.test_user_sync_github import (  # noqa: F401  (fake and no_real_secret_store are fixtures)
    TOKEN, assert_secret_free, fake, no_real_secret_store)
from tests.test_user_sync_github_setup import (  # noqa: F401  (bare is a fixture)
    HOST_KEY, REFUSED_KEY, REPOSITORY, SSH_URL, Script, Transport, bare, make_account, needs_ssh_keygen,
    posted_keys, script_repository)

REAL_DAEMON_DIRECTORY = installer.daemon_directory
REAL_OPEN_SETTINGS = installer.open_settings
REAL_MISSING_TOOLS = installer.missing_tools
IDENTITY = {installer.NAME: "Owner", installer.EMAIL: "owner@example.com", installer.LABEL: "laptop"}
VARIABLES = (installer.REPO, installer.REMOTE, installer.NAME, installer.EMAIL, installer.LABEL, installer.TOKEN)


@pytest.fixture(autouse=True)
def isolated_step(monkeypatch):
    """No daemon and no missing tool unless a test says so; the real settings page is never opened."""
    monkeypatch.setattr(installer, "daemon_directory", lambda: None)
    monkeypatch.setattr(installer, "open_settings", lambda directory: pytest.fail("no settings page here"))
    monkeypatch.setattr(installer, "missing_tools", lambda: [])
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def machine(tmp_path: Path, name: str = "machine", **options) -> Syncer:
    options.setdefault("allow_file_remote", True)
    options.setdefault("visibility", lambda remote: "private")
    return Syncer(tmp_path / name / "library", tmp_path / name / "state", **options)


def save(syncer: Syncer, name: str = "user:hello", text: str = "# Hello\n") -> None:
    FlowLibrary(user_dir=syncer.library).save(name, text)


def files(path: Path) -> dict[str, bytes]:
    """Every file below ``path`` with its bytes, to show that nothing changed."""
    if not path.exists():
        return {}
    return {str(item.relative_to(path)): item.read_bytes() for item in sorted(path.rglob("*")) if item.is_file()}


def nothing_asked(prompt: str) -> str:
    raise AssertionError(f"asked: {prompt}")


def no_wizard() -> dict:
    raise AssertionError("the terminal wizard started")


def run_step(syncer: Syncer, *, assume_yes=False, interactive=True, ask=nothing_asked, run_wizard=no_wizard,
             environ=None) -> tuple[dict, str]:
    said = []
    result = installer.step(syncer, assume_yes=assume_yes, interactive=interactive, ask=ask, say=said.append,
                            run_wizard=run_wizard, environ={} if environ is None else environ)
    return result, "\n".join(said)


def spy_children(monkeypatch) -> list:
    """Every child process's argv and environment, while the children still run."""
    seen = []
    current = subprocess.run

    def spy(argv, *args, **kwargs):
        seen.append(([str(word) for word in argv], dict(kwargs.get("env") or os.environ)))
        return current(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", spy)
    return seen


# --- the step: when it asks, and what yes does -----------------------------------------------


@pytest.mark.parametrize("assume_yes, interactive", [(True, True), (True, False), (False, False)],
                         ids=["yes-with-a-terminal", "yes-without-a-terminal", "no-terminal"])
def test_the_step_never_asks_under_yes_or_without_a_terminal(tmp_path, assume_yes, interactive):
    syncer = machine(tmp_path)
    save(syncer)
    before = files(tmp_path)
    result, said = run_step(syncer, assume_yes=assume_yes, interactive=interactive)
    assert result == {"status": "off", "reason": "not_asked"}
    assert "Sync between machines is off" in said and "summary below" in said
    assert files(tmp_path) == before  # no settings, no key, no .git


@pytest.mark.parametrize("answer", ["", "n", "No", "later"])
def test_the_step_asks_once_and_a_refusal_changes_nothing(tmp_path, answer):
    syncer = machine(tmp_path)
    save(syncer)
    before = files(tmp_path)
    ask = Script(answer)
    result, said = run_step(syncer, ask=ask)
    assert ask.prompts == ["  Set up sync between your machines now? [y/N]: "]
    assert result == {"status": "off", "reason": "declined"} and "Sync stays off" in said
    assert files(tmp_path) == before


def test_a_terminal_that_closes_counts_as_no(tmp_path):
    result, _ = run_step(machine(tmp_path), ask=Script())  # EOF instead of an answer
    assert result["reason"] == "declined"


@pytest.mark.parametrize("answer", ["y", "Y", " yes "])
def test_yes_starts_the_terminal_wizard_where_no_daemon_is_installed(tmp_path, answer):
    runs = []
    result, _ = run_step(machine(tmp_path), ask=Script(answer),
                         run_wizard=lambda: runs.append("wizard") or {"status": "cancelled"})
    assert runs == ["wizard"] and result == {"status": "cancelled"}


def test_yes_opens_the_settings_page_where_the_daemon_is_installed(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(installer, "daemon_directory", lambda: tmp_path / "service")
    monkeypatch.setattr(installer, "open_settings",
                        lambda directory: opened.append(directory) or "http://127.0.0.1:8765/ui/#code")
    result, said = run_step(machine(tmp_path), ask=Script("y"))
    assert opened == [tmp_path / "service"]
    assert result == {"status": "off", "reason": "settings_page", "url": "http://127.0.0.1:8765/ui/#code"}
    assert "Its Sync page sets sync up" in said and "-m src.daemon flows-ui" in said


def test_a_settings_page_that_cannot_open_falls_back_to_the_terminal_wizard(tmp_path, monkeypatch):
    def refused(directory):
        raise RuntimeError("urllib.error.URLError: <urlopen error [Errno 61] Connection refused>")
    monkeypatch.setattr(installer, "daemon_directory", lambda: tmp_path / "service")
    monkeypatch.setattr(installer, "open_settings", refused)
    runs = []
    result, said = run_step(machine(tmp_path), ask=Script("y"),
                            run_wizard=lambda: runs.append("wizard") or {"status": "cancelled"})
    assert runs == ["wizard"] and "Connection refused" in said and "in this terminal instead" in said


@pytest.mark.parametrize("failure, status", [
    (Cancelled(), "cancelled"), (EOFError(), "cancelled"), (KeyboardInterrupt(), "cancelled"),
    (SyncError("auth", "the remote refused this machine's key"), "attention"), (TypeError("a bug"), "attention"),
])
def test_a_wizard_that_fails_never_fails_the_installer(tmp_path, failure, status):
    def wizard():
        raise failure
    result, said = run_step(machine(tmp_path), ask=Script("y"), run_wizard=wizard)
    assert result["status"] == status and said


def test_a_wizard_that_starts_sync_tells_how_it_keeps_running(tmp_path, bare):
    syncer = machine(tmp_path)

    def wizard():
        syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")
        return syncer.start(syncer.preview()["hash"])
    result, said = run_step(syncer, ask=Script("y"), run_wizard=wizard)
    assert result["status"] == "synced" and "-m src.user_sync schedule enable" in said


# --- the step: what it leaves alone ---------------------------------------------------------


def test_sync_that_is_set_up_is_reported_without_a_question_and_left_alone(tmp_path, bare):
    syncer = machine(tmp_path)
    save(syncer)
    syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")
    before = files(tmp_path)
    result, said = run_step(syncer)
    assert result["status"] == "set_up" and "set up but not started yet" in said
    assert files(tmp_path) == before
    syncer.start(syncer.preview()["hash"])
    before = files(tmp_path)
    for assume_yes in (False, True):
        result, said = run_step(syncer, assume_yes=assume_yes,
                                environ={installer.REMOTE: "git@git.example.com:me/other.git", **IDENTITY})
        assert result["status"] == "set_up" and "Sync between machines is on" in said
        assert "change nothing once sync has started" in said and "-m src.user_sync status" in said
    assert files(tmp_path) == before


def test_a_git_in_the_library_that_sync_did_not_create_is_never_touched(tmp_path):
    syncer = machine(tmp_path)
    syncer.library.mkdir(parents=True)
    plain_git("init", "--quiet", str(syncer.library))
    before = files(tmp_path)
    result, said = run_step(syncer)
    assert result["reason"] == "foreign_git" and "leaves it as it is" in said
    result, said = run_step(syncer, assume_yes=True,
                            environ={installer.REMOTE: "git@git.example.com:me/library.git", **IDENTITY})
    assert result["reason"] == "foreign_git" and "Sync was not set up" in said
    assert files(tmp_path) == before and syncer.settings() is None


def test_a_git_left_from_an_earlier_sync_is_offered_to_the_wizard_but_never_set_up_unattended(tmp_path, bare):
    syncer = machine(tmp_path)
    syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")
    syncer.disconnect()  # the library and its .git stay
    assert (syncer.library / ".git").is_dir() and installer.foreign_git(syncer.library) is False
    result, _ = run_step(syncer, ask=Script("n"))
    assert result["reason"] == "declined"  # the owner may set it up again in the wizard
    before = files(tmp_path)
    result, said = run_step(syncer, assume_yes=True, environ={installer.REMOTE: str(bare), **IDENTITY})
    assert result["reason"] == "existing_git" and "-m src.user_sync setup" in said
    assert files(tmp_path) == before and syncer.settings() is None


def test_missing_tools_are_named_and_nothing_is_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "missing_tools", lambda: ["git 2.32 or newer", "ssh-keygen (OpenSSH)"])
    result, said = run_step(machine(tmp_path))
    assert result["reason"] == "git_too_old" and "it needs git 2.32 or newer and ssh-keygen (OpenSSH)" in said
    monkeypatch.setattr(installer, "missing_tools", lambda: ["ssh-keygen (OpenSSH)"])
    assert run_step(machine(tmp_path))[0]["reason"] == "ssh"


def test_a_token_without_a_repository_to_sign_in_for_is_dropped_not_stored(tmp_path):
    environ = {installer.TOKEN: TOKEN}
    result, said = run_step(machine(tmp_path), assume_yes=True, environ=environ)
    assert environ == {} and result["reason"] == "not_asked" and "it was not stored" in said
    assert_secret_free(said)


# --- the summary ------------------------------------------------------------------------------


def test_the_summary_says_how_to_turn_sync_on_unless_it_runs(tmp_path, bare, monkeypatch):
    syncer, said = machine(tmp_path), []
    installer.summary(syncer, say=said.append)
    text = "\n".join(said)
    assert "Sync between machines is off. To turn it on" in text and "-m src.user_sync setup" in text
    assert installer.REPO in text and "flows-ui" not in text and said[-1] == ""
    monkeypatch.setattr(installer, "daemon_directory", lambda: tmp_path / "service")
    said.clear()
    installer.summary(syncer, say=said.append)
    assert "-m src.daemon flows-ui" in "\n".join(said)
    syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")
    said.clear()
    installer.summary(syncer, say=said.append)
    assert "set up but not started yet" in "\n".join(said)
    syncer.start(syncer.preview()["hash"])
    said.clear()
    assert installer.summary(syncer, say=said.append) == {"status": "set_up"} and said == []


# --- this machine: tools, the library's .git, the daemon --------------------------------------


@pytest.mark.parametrize("version, keygen, expected", [
    ((2, 40, 0), "/usr/bin/ssh-keygen", []),
    (None, "/usr/bin/ssh-keygen", ["git"]),
    ((2, 30, 1), None, ["this machine has 2.30.1", "ssh-keygen"]),
])
def test_missing_tools_needs_git_2_32_and_ssh_keygen(monkeypatch, version, keygen, expected):
    monkeypatch.setattr(installer.gitcmd, "git_version", lambda: version)
    monkeypatch.setattr(installer.shutil, "which", lambda name: keygen if name == "ssh-keygen" else None)
    missing = REAL_MISSING_TOOLS()
    assert len(missing) == len(expected) and all(word in item for word, item in zip(expected, missing))


def test_foreign_git_only_reads_the_librarys_git_config(tmp_path):
    library = tmp_path / "library"
    assert installer.foreign_git(library) is False
    library.mkdir()
    (library / ".git").write_text("gitdir: /elsewhere\n")  # a linked worktree's .git file
    assert installer.foreign_git(library) is True
    (library / ".git").unlink()
    plain_git("init", "--quiet", str(library))
    assert installer.foreign_git(library) is True
    plain_git("--git-dir", str(library / ".git"), "config", engine_module.MANAGED_KEY, "true")
    before = files(library / ".git")
    assert installer.foreign_git(library) is False
    assert files(library / ".git") == before


def test_the_daemon_is_found_through_the_marker_install_writes_and_only_on_macos(tmp_path, monkeypatch):
    service = tmp_path / "service"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".shared-service.json").write_text(json.dumps({"directory": str(service)}))
    monkeypatch.setattr(installer, "installation_root", lambda: tmp_path)
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    assert REAL_DAEMON_DIRECTORY() is None  # recorded, but not installed there
    service.mkdir()
    (service / "service.json").write_text("{}")
    assert REAL_DAEMON_DIRECTORY() == service
    monkeypatch.setattr(installer.sys, "platform", "linux")
    assert REAL_DAEMON_DIRECTORY() is None


def test_open_settings_runs_flows_ui_for_the_daemons_state_and_reports_why_it_failed(tmp_path, monkeypatch):
    calls = []

    def answered(argv, **options):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"url": "http://127.0.0.1:8765/ui/#c"}), "")
    monkeypatch.setattr(installer.subprocess, "run", answered)
    assert REAL_OPEN_SETTINGS(tmp_path / "service") == "http://127.0.0.1:8765/ui/#c"
    assert calls[0][1:] == ["-m", "src.daemon", "--state", str(tmp_path / "service"), "flows-ui"]

    def stopped(argv, **options):
        return subprocess.CompletedProcess(argv, 1, "", "Traceback (most recent call last):\n"
                                                        "urllib.error.URLError: <urlopen error Connection refused>\n")
    monkeypatch.setattr(installer.subprocess, "run", stopped)
    with pytest.raises(RuntimeError, match="Connection refused"):
        REAL_OPEN_SETTINGS(tmp_path / "service")


# --- setup from the environment -----------------------------------------------------------------


@needs_ssh_keygen
def test_env_setup_on_github_keeps_the_token_adds_the_deploy_key_and_starts(fake, tmp_path, bare, monkeypatch):
    transport = Transport(tmp_path, bare)
    account = make_account(fake, tmp_path / "machine" / "state")
    syncer = machine(tmp_path, github_account=account, ssh_command=transport.command,
                     github_host_keys=lambda: [HOST_KEY], allow_file_remote=False, visibility=None)
    save(syncer)
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    script_repository(fake)
    for name, value in {installer.REPO: REPOSITORY, **IDENTITY, installer.TOKEN: TOKEN}.items():
        monkeypatch.setenv(name, value)
    children = spy_children(monkeypatch)
    result, said = run_step(syncer, assume_yes=True, interactive=False, environ=os.environ)
    assert result["status"] == "synced", said
    assert installer.TOKEN not in os.environ  # read once, before any child process started
    assert account._store.token == TOKEN and account.status()["login"] == "octocat"
    assert len(posted_keys(fake)) == 1 and syncer.settings().remote == SSH_URL
    assert "common/hello.md" in remote_files(bare)
    assert "Signed in to github.com as octocat" in said and "added this machine's deploy key" in said
    assert "Sync started: sent" in said
    assert children  # ssh-keygen and git ran, none of them with the token in argv or environment
    assert_secret_free(said, json.dumps(result), *(" ".join(argv) + json.dumps(env) for argv, env in children))


@needs_ssh_keygen
def test_env_setup_on_github_without_a_token_prints_the_key_and_a_second_run_finishes(fake, tmp_path, bare):
    transport = Transport(tmp_path, bare, refuse={"github.com": REFUSED_KEY})
    syncer = machine(tmp_path, github_account=make_account(fake, tmp_path / "machine" / "state"),
                     ssh_command=transport.command, github_host_keys=lambda: [HOST_KEY], allow_file_remote=False)
    save(syncer)
    environ = {installer.REPO: REPOSITORY, **IDENTITY}
    result, said = run_step(syncer, assume_yes=True, interactive=False, environ=dict(environ))
    public = syncer.status()["public_key"]
    assert (result["status"], result["public_key"]) == ("waiting_for_access", public)
    assert public in said and "deploy key with write access" in said and "setup --from-env" in said
    assert remote_files(bare) == {} and syncer.settings().started is None and fake.requests == []
    transport.refuse({})  # the owner added the key on GitHub
    result, said = run_step(syncer, assume_yes=True, interactive=False, environ=dict(environ))
    assert result["status"] == "synced", said
    assert "common/hello.md" in remote_files(bare) and syncer.settings().remote == SSH_URL
    assert fake.requests == []  # no account: GitHub's API was never asked


def test_env_setup_stops_a_join_with_conflicts_at_confirmation_needed(tmp_path, bare):
    first = machine(tmp_path, "first")
    save(first, text="# Hello from the first machine\n")
    first.setup(remote=str(bare), name="Owner", email="owner@example.com", label="first")
    assert first.start(first.preview()["hash"])["status"] == "synced"
    pushed = remote_files(bare)
    second = machine(tmp_path, "second")
    save(second, text="# Hello from the second machine\n")
    said = []
    result = installer.from_env(second, say=said.append,
                                environ={installer.REMOTE: str(bare), **IDENTITY, installer.LABEL: "second"})
    text = "\n".join(said)
    assert (result["status"], result["reason"]) == ("attention", "confirmation_needed")
    assert f"start --confirm {result['hash']}" in text and "-m src.user_sync preview" in text
    assert "nothing was uploaded" in text and "user:hello" in text
    assert remote_files(bare) == pushed and second.settings().started is None
    assert second.start(result["hash"])["status"] == "synced"  # the owner confirms that preview


def test_env_setup_on_another_host_uploads_and_a_second_run_changes_nothing(tmp_path, bare):
    syncer = machine(tmp_path)
    save(syncer)
    environ = {installer.REMOTE: str(bare), **IDENTITY}
    said = []
    result = installer.from_env(syncer, say=said.append, environ=dict(environ), token=TOKEN)
    text = "\n".join(said)
    assert result["status"] == "synced" and "common/hello.md" in remote_files(bare)
    assert "used only for a repository on github.com; it was not stored" in text
    assert "Sync started: sent" in text and "schedule enable" in text
    assert_secret_free(text)
    before = files(tmp_path)
    again = installer.from_env(syncer, say=said.append, environ=dict(environ))
    assert again["status"] == "set_up" and files(tmp_path) == before


def test_env_setup_never_replaces_a_setup_for_another_remote(tmp_path, bare):
    other = tmp_path / "other.git"
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(other))
    syncer = machine(tmp_path)
    syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")
    with pytest.raises(SyncError) as refused:
        installer.from_env(syncer, say=lambda text: None, environ={installer.REMOTE: str(other), **IDENTITY})
    assert refused.value.reason == "connected" and syncer.settings().remote == str(bare)


@pytest.mark.parametrize("environ, reason", [
    ({}, "not_requested"),
    ({installer.REPO: REPOSITORY, installer.REMOTE: SSH_URL, **IDENTITY}, "invalid"),
    ({installer.REPO: "not a repository", **IDENTITY}, "invalid"),
    ({installer.REPO: REPOSITORY, installer.NAME: "Owner"}, "identity"),
    ({installer.REPO: REPOSITORY, **IDENTITY, installer.LABEL: "Bad Label"}, "identity"),
    ({installer.REMOTE: "ftp://git.example.com/me/library.git", **IDENTITY}, "unknown_remote"),
    ({installer.REMOTE: "https://git.example.com/me/library.git", **IDENTITY}, "unknown_remote"),
])
def test_env_setup_checks_its_variables_before_it_changes_anything(tmp_path, environ, reason):
    with pytest.raises(SyncError) as refused:
        installer.from_env(machine(tmp_path), say=lambda text: None, environ=environ)
    assert refused.value.reason == reason and files(tmp_path) == {}


# --- the command line ---------------------------------------------------------------------------


def test_cli_installer_step_asks_only_with_a_terminal_and_without_yes(tmp_path, capsys):
    base = ["--state", str(tmp_path / "state"), "--library", str(tmp_path / "library")]
    assert cli([*base, "installer", "--yes"], ask=nothing_asked, interactive=True) == 0
    assert "--yes never asks" in capsys.readouterr().out
    assert cli([*base, "installer"], ask=nothing_asked, interactive=False) == 0
    assert "no terminal to ask in" in capsys.readouterr().out
    ask = Script("n")
    assert cli([*base, "installer"], ask=ask, interactive=True) == 0
    assert ask.prompts == [installer.PROMPT] and "Sync stays off" in capsys.readouterr().out
    assert cli([*base, "installer", "--summary"]) == 0
    assert "To turn it on" in capsys.readouterr().out
    assert files(tmp_path) == {}


def test_cli_installer_yes_starts_the_terminal_wizard(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(engine_module, "default_github_account", lambda state_dir: pytest.fail("no GitHub here"))
    ask = Script("y", "m")  # then the answers run out: the wizard stops before it changes anything
    base = ["--state", str(tmp_path / "state"), "--library", str(tmp_path / "library")]
    assert cli([*base, "installer"], ask=ask, interactive=True) == 0
    assert ask.prompts[1].startswith("Where is the library's repository?")
    assert ask.prompts[2].startswith("SSH URL of the repository")
    assert "cancelled; nothing was uploaded" in capsys.readouterr().out
    assert files(tmp_path) == {}


def test_cli_setup_from_env_takes_the_token_out_of_the_environment(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(installer.TOKEN, TOKEN)
    base = ["--state", str(tmp_path / "state"), "--library", str(tmp_path / "library")]
    assert cli([*base, "setup", "--from-env"]) == 1
    out = capsys.readouterr().out
    assert "setup: off (not_requested)" in out and installer.REPO in out
    assert installer.TOKEN not in os.environ
    assert_secret_free(out)
    with pytest.raises(SystemExit) as usage:
        cli([*base, "setup", "--from-env", "--name", "Owner"])
    assert usage.value.code == 2 and "--from-env reads the name" in capsys.readouterr().err
