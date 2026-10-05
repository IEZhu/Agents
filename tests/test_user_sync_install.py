"""The installers' sync step and setup from the environment (#171, src/user_sync/installer.py).

The step runs in-process with scripted answers and stubs for the terminal wizard, the browser and
the daemon's settings page; one test probes the page on a fake daemon on 127.0.0.1. Setup from the
environment runs against the fake GitHub of ``test_user_sync_github`` and local bare repositories,
reached through the fake SSH transport of ``test_user_sync_github_setup``. The scheduler is a fake,
``.env`` is never read unless a test supplies one, and the command line's session lease lives in a
temporary installation root. Nothing reaches the real GitHub, a daemon, an OS secret store or
scheduler, or the user's library. ``test_installer_sync.py`` runs the installers' own sections.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from src.user_flows import FlowLibrary
from src.user_sync import __main__ as cli_module, engine as engine_module, installer, keys, schedule
from src.user_sync.__main__ import main as cli
from src.user_sync.engine import SyncError, Syncer
from src.user_sync.github import GitHubError
from src.user_sync.wizard import Cancelled
from tests.test_user_sync import plain_git, remote_files
from tests.test_user_sync_github import (  # noqa: F401  (fake and no_real_secret_store are fixtures)
    TOKEN, assert_secret_free, fake, no_real_secret_store, repo)
from tests.test_user_sync_github_setup import (  # noqa: F401  (bare is a fixture)
    HOST_KEY, REFUSED_KEY, REPOSITORY, SSH_URL, Script, Transport, bare, deploy_key, make_account,
    needs_ssh_keygen, posted_keys, script_repository)

ROOT = Path(__file__).resolve().parents[1]
REAL_LOAD_ENV = cli_module._load_env
REAL_DAEMON_DIRECTORY = installer.daemon_directory
REAL_SETTINGS_PAGE = installer.settings_page
REAL_MISSING_TOOLS = installer.missing_tools
IDENTITY = {installer.NAME: "Owner", installer.EMAIL: "owner@example.com", installer.LABEL: "laptop"}
NAMES = (*installer.VARIABLES, installer.TOKEN, installer.ASSUME_YES)


class FakeSchedule:
    """``schedule.status`` and ``schedule.enable`` without an OS scheduler; records each enable."""

    def __init__(self):
        self.minutes, self.enabled, self.failure = None, [], None

    def status(self, **options):
        return {"backend": "fake", "scheduled": self.minutes is not None, "interval_minutes": self.minutes}

    def enable(self, interval_minutes=5, *, state_dir=None, library=None, **options):
        if self.failure:
            raise self.failure
        self.enabled.append((interval_minutes, Path(state_dir), Path(library)))
        self.minutes = interval_minutes
        return {"backend": "fake", "scheduled": True, "interval_minutes": interval_minutes}


@pytest.fixture(autouse=True)
def isolated_step(monkeypatch, tmp_path):
    """No daemon, settings page, missing tool, .env, real scheduler or lease in the checkout."""
    monkeypatch.setattr(installer, "daemon_directory", lambda: None)
    monkeypatch.setattr(installer, "settings_page", lambda directory: pytest.fail("no settings page here"))
    monkeypatch.setattr(installer, "missing_tools", lambda: [])
    monkeypatch.setattr(cli_module, "_load_env", lambda: None)
    installation = tmp_path / "installation"
    installation.mkdir()
    monkeypatch.setattr(cli_module, "installation_root", lambda: installation)  # the session lease
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def scheduler(monkeypatch):
    fake_schedule = FakeSchedule()
    monkeypatch.setattr(schedule, "status", fake_schedule.status)
    monkeypatch.setattr(schedule, "enable", fake_schedule.enable)
    monkeypatch.setattr(schedule, "disable", lambda **options: pytest.fail("nothing disables the schedule"))
    return fake_schedule


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
             environ=None, token=None, open_browser=None) -> tuple[dict, str]:
    said = []
    result = installer.step(syncer, assume_yes=assume_yes, interactive=interactive, ask=ask, say=said.append,
                            run_wizard=run_wizard, environ={} if environ is None else environ, token=token,
                            open_browser=open_browser or (lambda url: pytest.fail("no browser here")))
    return result, "\n".join(said)


def from_env(syncer: Syncer, environ: dict, **options) -> tuple[dict, str]:
    said = []
    result = installer.from_env(syncer, say=said.append, environ=environ, **options)
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


def run_cli(tmp_path, *arguments, **options):
    base = ["--state", str(tmp_path / "state"), "--library", str(tmp_path / "library")]
    return cli([*base, *arguments], **options)


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


@pytest.mark.parametrize("opened", [True, False], ids=["browser", "no-browser"])
def test_yes_opens_the_settings_page_when_the_daemon_serves_the_sync_page(tmp_path, monkeypatch, opened):
    asked, browsed = [], []
    monkeypatch.setattr(installer, "daemon_directory", lambda: tmp_path / "service")
    monkeypatch.setattr(installer, "settings_page",
                        lambda directory: asked.append(directory) or "http://127.0.0.1:8765/ui#code")
    result, said = run_step(machine(tmp_path), ask=Script("y"),
                            open_browser=lambda url: browsed.append(url) or opened)
    assert asked == [tmp_path / "service"] and browsed == ["http://127.0.0.1:8765/ui#code"]
    assert result == {"status": "off", "reason": "settings_page"} and "its Sync page sets sync up" in said
    # The one-use link is printed only when no browser opened it.
    assert ("http://127.0.0.1:8765/ui#code" in said) is (not opened)


def test_a_settings_page_that_cannot_set_sync_up_means_the_wizard_without_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "daemon_directory", lambda: tmp_path / "service")
    monkeypatch.setattr(installer, "settings_page", lambda directory: None)  # stopped, warming or older
    runs = []
    result, said = run_step(machine(tmp_path), ask=Script("y"),
                            run_wizard=lambda: runs.append("wizard") or {"status": "cancelled"})
    assert runs == ["wizard"] and said == "" and result["status"] == "cancelled"


@pytest.mark.parametrize("failure, status", [
    (Cancelled(), "cancelled"), (EOFError(), "cancelled"), (KeyboardInterrupt(), "cancelled"),
    (SyncError("auth", "the remote refused this machine's key"), "attention"), (TypeError("a bug"), "attention"),
])
def test_a_wizard_that_fails_never_fails_the_installer(tmp_path, failure, status):
    def wizard():
        raise failure
    result, said = run_step(machine(tmp_path), ask=Script("y"), run_wizard=wizard)
    assert result["status"] == status and said


def started_by_wizard(syncer, bare):
    def wizard():
        syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")
        return syncer.start(syncer.preview()["hash"])
    return wizard


@pytest.mark.parametrize("answer, enabled", [("", True), ("y", True), ("n", False)])
def test_after_the_wizard_started_sync_it_offers_background_sync_once(tmp_path, bare, scheduler, answer, enabled):
    syncer = machine(tmp_path)
    ask = Script("y", answer)
    result, said = run_step(syncer, ask=ask, run_wizard=started_by_wizard(syncer, bare))
    assert ask.prompts[1] == "  Also sync every 5 minutes in the background? [Y/n]: " and ask.answers == []
    assert result["status"] == "synced" and result["background"] == ("schedule" if enabled else None)
    if enabled:
        assert scheduler.enabled == [(5, syncer.state_dir, syncer.library)] and "Background sync is on" in said
    else:
        assert scheduler.enabled == [] and "-m src.user_sync schedule enable" in said


def test_where_the_daemon_or_a_schedule_syncs_the_wizard_asks_nothing_more(tmp_path, bare, scheduler, monkeypatch):
    syncer = machine(tmp_path)
    scheduler.minutes = 10
    ask = Script("y")
    result, said = run_step(syncer, ask=ask, run_wizard=started_by_wizard(syncer, bare))
    assert result["background"] == "schedule" and "already runs every 10 minutes" in said and scheduler.enabled == []
    assert len(ask.prompts) == 1


def test_a_wizard_that_did_not_start_sync_is_attention(tmp_path, bare):
    syncer = machine(tmp_path)

    def wizard():
        syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")
        return {"status": "offline", "reason": "network", "message": "github.com is unreachable"}
    result, _ = run_step(syncer, ask=Script("y"), run_wizard=wizard)
    assert (result["status"], result["reason"]) == ("attention", "network")


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
        result, said = run_step(syncer, assume_yes=assume_yes, token=TOKEN,
                                environ={installer.REMOTE: "git@git.example.com:me/other.git", **IDENTITY})
        assert result["status"] == "set_up" and "Sync between machines is on" in said
        assert "change nothing once sync has started" in said and "-m src.user_sync status" in said
        assert f"cd {ROOT}" in said or "cd /" in said  # commands say where they run
        assert "AGENTS_GITHUB_TOKEN was not used: sync is already set up" in said
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
    assert result["reason"] == "existing_git" and "move it away" in said and "-m src.user_sync setup" in said
    assert files(tmp_path) == before and syncer.settings() is None


def test_missing_tools_are_named_and_nothing_is_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "missing_tools", lambda: ["git 2.32 or newer", "ssh-keygen (OpenSSH)"])
    result, said = run_step(machine(tmp_path))
    assert result["reason"] == "git_too_old" and "it needs git 2.32 or newer and ssh-keygen (OpenSSH)" in said
    monkeypatch.setattr(installer, "missing_tools", lambda: ["ssh-keygen (OpenSSH)"])
    assert run_step(machine(tmp_path))[0]["reason"] == "ssh"


def test_a_token_without_a_repository_to_sign_in_for_is_not_used(tmp_path):
    result, said = run_step(machine(tmp_path), assume_yes=True, token=TOKEN)
    assert result["reason"] == "not_asked" and "AGENTS_GITHUB_TOKEN was not used" in said
    assert_secret_free(said)


# --- the summary ------------------------------------------------------------------------------


def test_the_summary_says_how_to_turn_sync_and_background_sync_on(tmp_path, bare, monkeypatch, scheduler):
    syncer, said = machine(tmp_path), []
    installer.summary(syncer, say=said.append)
    text = "\n".join(said)
    assert "Sync between machines is off. To turn it on" in text and "-m src.user_sync setup" in text
    assert installer.REPO in text and "flows-ui" not in text and said[-1] == ""
    monkeypatch.setattr(installer, "daemon_directory", lambda: tmp_path / "service")
    said.clear()
    installer.summary(syncer, say=said.append)
    assert "-m src.daemon flows-ui" in "\n".join(said)
    monkeypatch.setattr(installer, "daemon_directory", lambda: None)
    syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")
    said.clear()
    installer.summary(syncer, say=said.append)
    assert "but not started yet" in "\n".join(said) and "--from-env" not in "\n".join(said)
    syncer.start(syncer.preview()["hash"])
    said.clear()
    assert installer.summary(syncer, say=said.append) == {"status": "set_up", "background": None}
    assert "only in the cycle that follows each save" in said[0] and "schedule enable" in said[0]
    scheduler.minutes = 5
    said.clear()
    assert installer.summary(syncer, say=said.append)["background"] == "schedule"
    assert "in the background every 5 minutes" in said[0]
    monkeypatch.setattr(installer, "daemon_directory", lambda: tmp_path / "service")
    said.clear()
    assert installer.summary(syncer, say=said.append)["background"] == "daemon"
    assert "the daemon syncs in the background" in said[0]


# --- this machine: tools, the library's .git, the daemon, the terminal ----------------------


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


class FakeDaemon:
    """``/health``, ``/ui`` and ``/admin/ui/code`` of a daemon on 127.0.0.1, with its state directory."""

    def __init__(self, directory: Path):
        self.health, self.page = {"state": "ready"}, b"<script>api('/ui/api/sync')</script>"
        self.requests = []
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self):
                daemon.requests.append((self.command, self.path, self.headers.get("Authorization")))
                port = daemon.server.server_address[1]
                if self.path == "/ui":
                    body, kind = daemon.page, "text/html"
                elif self.headers.get("Authorization") != "Bearer secret-bearer":
                    body, kind = b'{"error": "unauthorized"}', "application/json"
                elif self.path == "/health":
                    body, kind = json.dumps(daemon.health).encode(), "application/json"
                else:
                    body, kind = json.dumps({"url": f"http://127.0.0.1:{port}/ui#one-use"}).encode(), "application/json"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "service.json").write_text(json.dumps({"port": self.server.server_address[1]}))
        (directory / "token").write_text("secret-bearer\n")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_the_settings_page_is_used_only_when_the_daemon_is_ready_and_serves_the_sync_page(tmp_path):
    directory = tmp_path / "service"
    daemon = FakeDaemon(directory)
    try:
        port = daemon.server.server_address[1]
        assert REAL_SETTINGS_PAGE(directory) == f"http://127.0.0.1:{port}/ui#one-use"
        assert ("POST", "/admin/ui/code", "Bearer secret-bearer") in daemon.requests
        daemon.page = b"<script>api('/ui/api/flows')</script>"  # a page from before the Sync page
        assert REAL_SETTINGS_PAGE(directory) is None
        daemon.page, daemon.health = b"/ui/api/sync", {"state": "warming"}
        assert REAL_SETTINGS_PAGE(directory) is None
    finally:
        daemon.close()
    assert REAL_SETTINGS_PAGE(directory) is None  # nothing answers: no error, the wizard instead
    assert REAL_SETTINGS_PAGE(tmp_path / "never-installed") is None


def test_only_a_console_counts_as_a_terminal_also_when_stdin_is_nul_on_windows(tmp_path):
    probe = [sys.executable, "-c", "from src.user_sync.__main__ import _terminal; print(_terminal())"]
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(probe, stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=ROOT,
                            env=environment, timeout=60)
    assert result.stdout.strip() == "False", result.stderr  # NUL on Windows, /dev/null elsewhere
    if sys.platform != "win32":
        import pty
        controller, terminal = pty.openpty()
        try:
            result = subprocess.run(probe, stdin=terminal, capture_output=True, text=True, cwd=ROOT,
                                    env=environment, timeout=60)
        finally:
            os.close(terminal)
            os.close(controller)
        assert result.stdout.strip() == "True", result.stderr


# --- setup from the environment -----------------------------------------------------------------


def github_machine(fake, tmp_path, transport, **options) -> Syncer:
    options.setdefault("visibility", None)  # GitHub's API decides
    return machine(tmp_path, github_account=make_account(fake, tmp_path / "machine" / "state"),
                   ssh_command=transport.command, github_host_keys=lambda: [HOST_KEY], allow_file_remote=False,
                   **options)


@needs_ssh_keygen
def test_env_setup_on_github_keeps_the_token_adds_the_deploy_key_starts_and_schedules(
        fake, tmp_path, bare, monkeypatch, scheduler, capsys):
    transport = Transport(tmp_path, bare)
    syncer = github_machine(fake, tmp_path, transport)
    save(syncer)
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    script_repository(fake)
    monkeypatch.setattr(cli_module, "Syncer", lambda library, state: syncer)
    for name, value in {installer.REPO: REPOSITORY, **IDENTITY, installer.TOKEN: TOKEN}.items():
        monkeypatch.setenv(name, value)
    children = spy_children(monkeypatch)
    assert cli(["installer", "--yes"], interactive=False) == 0
    out = capsys.readouterr().out
    assert installer.TOKEN not in os.environ  # taken out before any child process started
    account = syncer.github_account()
    assert account._store.token == TOKEN and account.status()["login"] == "octocat"
    assert len(posted_keys(fake)) == 1 and syncer.settings().remote == SSH_URL and syncer.settings().started
    assert "common/hello.md" in remote_files(bare)
    assert "Signed in to github.com as octocat" in out and "added this machine's deploy key" in out
    assert "Sync started: sent" in out and "Background sync is on: every 5 minutes" in out
    assert scheduler.enabled == [(5, syncer.state_dir, syncer.library)]
    assert not (syncer.state_dir / installer.MARKER).exists()  # started: nothing left to replace
    assert children  # ssh-keygen and git ran, none of them with the token in argv or environment
    assert_secret_free(out, *(" ".join(argv) + json.dumps(env) for argv, env in children))


def test_github_refusing_the_token_stores_nothing_and_never_fails_the_installer(fake, tmp_path, bare):
    syncer = github_machine(fake, tmp_path, Transport(tmp_path, bare))
    fake.reply("GET", "/api/v3/user", 401, {"message": "Bad credentials"})
    result, said = run_step(syncer, assume_yes=True, token=TOKEN, environ={installer.REPO: REPOSITORY, **IDENTITY})
    assert (result["status"], result["reason"]) == ("attention", "auth") and "Sync was not set up" in said
    assert syncer.github_account()._store.token is None and not syncer.github_account().status()["connected"]
    assert syncer.settings() is None and remote_files(bare) == {}
    assert_secret_free(said, json.dumps(result))


@needs_ssh_keygen
def test_env_setup_on_github_without_a_token_prints_the_key_and_a_second_run_finishes(fake, tmp_path, bare,
                                                                                      scheduler):
    transport = Transport(tmp_path, bare, refuse={"github.com": REFUSED_KEY})
    syncer = github_machine(fake, tmp_path, transport, visibility=lambda remote: "private")
    save(syncer)
    environ = {installer.REPO: REPOSITORY, **IDENTITY}
    result, said = run_step(syncer, assume_yes=True, interactive=False, environ=environ)
    public = syncer.status()["public_key"]
    assert (result["status"], result["public_key"]) == ("waiting_for_access", public)
    assert public in said and "deploy key with write access" in said and "setup --from-env" in said
    assert remote_files(bare) == {} and syncer.settings().started is None and fake.requests == []
    lines = []
    installer.summary(syncer, say=lines.append)
    assert "-m src.user_sync setup --from-env" in "\n".join(lines)  # the next step
    transport.refuse({})  # the owner added the key on GitHub
    result, said = run_step(syncer, assume_yes=True, interactive=False, environ=environ)
    assert result["status"] == "synced", said
    assert "common/hello.md" in remote_files(bare) and syncer.settings().remote == SSH_URL
    assert fake.requests == []  # no account: GitHub's API was never asked


def test_env_setup_stops_a_join_with_conflicts_at_confirmation_needed(tmp_path, bare, scheduler):
    first = machine(tmp_path, "first")
    save(first, text="# Hello from the first machine\n")
    first.setup(remote=str(bare), name="Owner", email="owner@example.com", label="first")
    assert first.start(first.preview()["hash"])["status"] == "synced"
    pushed = remote_files(bare)
    second = machine(tmp_path, "second")
    save(second, text="# Hello from the second machine\n")
    result, text = from_env(second, {installer.REMOTE: str(bare), **IDENTITY, installer.LABEL: "second"})
    assert (result["status"], result["reason"]) == ("attention", "confirmation_needed")
    assert f"start --confirm {result['hash']}" in text and "-m src.user_sync preview" in text
    assert "nothing was uploaded" in text and "user:hello" in text
    assert remote_files(bare) == pushed and second.settings().started is None and scheduler.enabled == []
    assert second.start(result["hash"])["status"] == "synced"  # the owner confirms that preview


def test_env_setup_on_another_host_uploads_schedules_and_a_second_run_changes_nothing(tmp_path, bare, scheduler):
    syncer = machine(tmp_path)
    save(syncer)
    environ = {installer.REMOTE: str(bare), **IDENTITY}
    result, text = from_env(syncer, dict(environ), token=TOKEN)
    assert result["status"] == "synced" and "common/hello.md" in remote_files(bare)
    assert "used only for a repository on github.com; it was not stored" in text
    assert "Sync started: sent" in text and len(scheduler.enabled) == 1
    assert_secret_free(text)
    before = files(tmp_path)
    for again in (environ, {installer.REMOTE: str(bare)}):  # started: the identity is not even read
        result, text = from_env(syncer, dict(again))
        assert result["status"] == "set_up" and "nothing changed" in text
    assert files(tmp_path) == before and len(scheduler.enabled) == 1


@pytest.mark.parametrize("failure", [
    {"status": "lock_held", "message": "another sync of this library is running"},
    {"status": "offline", "reason": "network", "message": "ssh: connect to host: Connection timed out"},
], ids=["lock-held", "offline"])
def test_a_start_that_did_not_start_sync_exits_non_zero(tmp_path, bare, monkeypatch, capsys, failure, scheduler):
    syncer = machine(tmp_path)
    save(syncer)
    monkeypatch.setattr(syncer, "start", lambda confirm: dict(failure))
    monkeypatch.setattr(cli_module, "Syncer", lambda library, state: syncer)
    for name, value in {installer.REMOTE: str(bare), **IDENTITY}.items():
        monkeypatch.setenv(name, value)
    assert cli(["setup", "--from-env"]) == 1
    reason = failure.get("reason") or failure["status"]
    assert f"Sync did not start: {reason}: {failure['message']}" in capsys.readouterr().out
    assert cli(["installer", "--yes"], interactive=False) == 1  # init_repo prints its warning
    assert scheduler.enabled == [] and syncer.settings().started is None


def test_env_setup_replaces_only_a_setup_it_made_that_never_started(tmp_path, bare, monkeypatch, scheduler):
    second = tmp_path / "second.git"
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(second))
    syncer = machine(tmp_path, visibility=lambda remote: "private" if "second" in remote.url else "unknown")
    save(syncer)
    with pytest.raises(SyncError) as unknown:  # the first remote's privacy cannot be checked: it stops
        from_env(syncer, {installer.REMOTE: str(bare), **IDENTITY})
    assert unknown.value.reason == "public_repo" and "--confirm-private" in unknown.value.message
    assert syncer.settings().remote == str(bare) and installer.made_here(syncer, syncer.settings())
    result, text = from_env(syncer, {installer.REMOTE: str(second), **IDENTITY})
    assert result["status"] == "synced" and "Replacing the setup for" in text
    assert syncer.settings().remote == str(second) and "common/hello.md" in remote_files(second)
    assert remote_files(bare) == {}


def test_env_setup_never_replaces_a_setup_it_did_not_make(tmp_path, bare):
    other = tmp_path / "other.git"
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(other))
    syncer = machine(tmp_path)
    syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")  # the wizard's
    with pytest.raises(SyncError) as refused:
        from_env(syncer, {installer.REMOTE: str(other), **IDENTITY})
    message = refused.value.message
    assert refused.value.reason == "connected" and syncer.settings().remote == str(bare)
    assert "-m src.user_sync disconnect" in message and f"move {syncer.library / '.git'} away" in message
    assert "-m src.user_sync setup" in message


def test_env_setup_reruns_with_confirm_private(tmp_path, bare, scheduler):
    syncer = machine(tmp_path, visibility=lambda remote: "unknown")
    save(syncer)
    environ = {installer.REMOTE: str(bare), **IDENTITY}
    with pytest.raises(SyncError) as unknown:
        from_env(syncer, environ)
    assert "setup --from-env --confirm-private" in unknown.value.message and remote_files(bare) == {}
    result, _ = from_env(syncer, environ, confirm_private=True)
    assert result["status"] == "synced" and syncer.settings().private_confirmed is True


@needs_ssh_keygen
def test_env_setup_reruns_with_trust_host_key(tmp_path, bare, scheduler):
    transport = Transport(tmp_path, bare)
    syncer = machine(tmp_path, ssh_command=transport.command, scan_host_keys=lambda host, port: [HOST_KEY],
                     allow_file_remote=False)
    save(syncer)
    environ = {installer.REMOTE: "git@git.example.com:me/library.git", **IDENTITY}
    result, text = from_env(syncer, environ)
    fingerprint = keys.fingerprint(HOST_KEY)
    assert (result["status"], result["reason"], result["fingerprints"]) == ("attention", "host_key", [fingerprint])
    assert fingerprint in text and "setup --from-env --trust-host-key" in text and remote_files(bare) == {}
    result, text = from_env(syncer, environ, trust_host_key=fingerprint)
    # The key is not on the host yet: the public key to add, then a third run finishes.
    assert result["status"] in ("waiting_for_access", "synced"), text
    if result["status"] == "waiting_for_access":
        result, text = from_env(syncer, environ)
    assert result["status"] == "synced" and "common/hello.md" in remote_files(bare)


def test_the_command_line_passes_rerun_options_to_env_setup(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(installer, "from_env", lambda syncer, **options: seen.update(options) or {"status": "set_up"})
    assert run_cli(tmp_path, "setup", "--from-env", "--trust-host-key", "SHA256:abc", "--confirm-private",
                   "--confirm-owner", "acme") == 0
    assert (seen["trust_host_key"], seen["confirm_private"], seen["confirm_owner"]) == ("SHA256:abc", True, "acme")


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
        from_env(machine(tmp_path), environ)
    assert refused.value.reason == reason and files(tmp_path) == {}


# --- the command line: the token and .env ----------------------------------------------------------


def test_every_command_takes_the_token_out_of_its_environment_and_the_summary_never_sees_it(
        tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(installer, "summary", lambda syncer, **options: seen.append(
        (os.environ.get(installer.TOKEN), options)) or {"status": "off"})
    monkeypatch.setenv(installer.TOKEN, TOKEN)
    assert run_cli(tmp_path, "installer", "--summary") == 0
    assert seen == [(None, {"say": seen[0][1]["say"]})]  # neither in the environment nor as an argument
    monkeypatch.setenv(installer.TOKEN, TOKEN)
    assert run_cli(tmp_path, "status") == 0
    assert installer.TOKEN not in os.environ
    assert_secret_free(capsys.readouterr().out)


def test_installer_honours_assume_yes_from_the_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(installer.ASSUME_YES, "1")
    assert run_cli(tmp_path, "installer", ask=nothing_asked, interactive=True) == 0
    assert "--yes never asks" in capsys.readouterr().out


def test_dotenv_never_drives_setup(tmp_path, monkeypatch, capsys):
    root = tmp_path / "installation"
    (root / ".env").write_text(f"{installer.REPO}={REPOSITORY}\n{installer.NAME}=Owner\n"
                               f"{installer.EMAIL}=owner@example.com\n{installer.TOKEN}={TOKEN}\n")
    monkeypatch.setattr(cli_module, "_load_env", REAL_LOAD_ENV)
    for name in NAMES:  # load_dotenv writes os.environ: monkeypatch restores it
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    assert run_cli(tmp_path, "installer", "--yes", interactive=False) == 0
    out = capsys.readouterr().out
    assert f"Ignored in .env: {installer.REPO}, {installer.NAME}, {installer.EMAIL}, {installer.TOKEN}" in out
    assert "Sync between machines is off (--yes never asks)" in out
    assert not any(name in os.environ for name in NAMES) and not (tmp_path / "state").exists()
    assert run_cli(tmp_path, "setup", "--from-env") == 1
    out = capsys.readouterr().out
    assert "not_requested" in out and ".env does not count" in out
    assert_secret_free(out)


def test_cli_installer_step_asks_only_with_a_terminal_and_without_yes(tmp_path, capsys):
    assert run_cli(tmp_path, "installer", "--yes", ask=nothing_asked, interactive=True) == 0
    assert "--yes never asks" in capsys.readouterr().out
    assert run_cli(tmp_path, "installer", ask=nothing_asked, interactive=False) == 0
    assert "no terminal to ask in" in capsys.readouterr().out
    ask = Script("n")
    assert run_cli(tmp_path, "installer", ask=ask, interactive=True) == 0
    assert ask.prompts == [installer.PROMPT] and "Sync stays off" in capsys.readouterr().out
    assert run_cli(tmp_path, "installer", "--summary") == 0
    assert "To turn it on" in capsys.readouterr().out
    assert not (tmp_path / "state").exists() and not (tmp_path / "library").exists()
    assert (tmp_path / "installation" / "data" / ".sessions.lock").exists()  # the lease, not the checkout's


def test_cli_installer_yes_starts_the_terminal_wizard(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(engine_module, "default_github_account", lambda state_dir: pytest.fail("no GitHub here"))
    ask = Script("y", "m")  # then the answers run out: the wizard stops before it changes anything
    assert run_cli(tmp_path, "installer", ask=ask, interactive=True) == 0
    assert ask.prompts[1].startswith("Where is the library's repository?")
    assert ask.prompts[2].startswith("SSH URL of the repository")
    assert "cancelled; nothing was uploaded" in capsys.readouterr().out
    assert not (tmp_path / "state").exists() and not (tmp_path / "library").exists()


def test_cli_setup_from_env_without_variables_and_with_identity_flags(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(installer.TOKEN, TOKEN)
    assert run_cli(tmp_path, "setup", "--from-env") == 1
    out = capsys.readouterr().out
    assert "setup: off (not_requested)" in out and installer.REPO in out
    assert installer.TOKEN not in os.environ
    assert_secret_free(out)
    with pytest.raises(SystemExit) as usage:
        run_cli(tmp_path, "setup", "--from-env", "--name", "Owner")
    assert usage.value.code == 2 and "--from-env reads the name" in capsys.readouterr().err


def test_take_environment_and_forget_dotenv():
    environ = {installer.REPO: "me/library", installer.TOKEN: TOKEN, installer.ASSUME_YES: "1", "OTHER": "x"}
    values = installer.take_environment(environ)
    assert values == {installer.REPO: "me/library", installer.TOKEN: TOKEN, installer.ASSUME_YES: "1"}
    assert installer.TOKEN not in environ and installer.assume_yes(values)
    environ.update({installer.TOKEN: "from-dotenv", installer.NAME: "From Dotenv", installer.EMAIL: ""})
    assert installer.forget_dotenv(values, environ) == [installer.NAME, installer.TOKEN]
    assert environ == {installer.REPO: "me/library", installer.ASSUME_YES: "1", "OTHER": "x"}


# --- setups that stopped part way, keys on GitHub, the proxy, messages -------------------------

OTHER = "octocat/other-library"


def lock_held(confirm):
    return {"status": "lock_held", "message": "another sync of this library is running"}


@needs_ssh_keygen
def test_a_mistyped_host_still_leaves_a_setup_the_corrected_run_may_replace(tmp_path, bare, scheduler):
    """Setup saves the settings before it trusts the host: the marker is written all the same."""
    def unreachable(host, port):
        raise keys.SSHKeyError(f"ssh-keyscan found no key for {host}")
    syncer = machine(tmp_path, scan_host_keys=unreachable)
    save(syncer)
    typo = {installer.REMOTE: "git@gti.example.com:me/agents-library.git", **IDENTITY}
    result, _ = run_step(syncer, assume_yes=True, environ=typo)
    assert (result["status"], result["reason"]) == ("attention", "host_key")
    assert syncer.settings() is not None and installer.made_here(syncer, syncer.settings())
    result, said = run_step(syncer, assume_yes=True, environ={installer.REMOTE: str(bare), **IDENTITY})
    assert result["status"] == "synced", said
    assert "Replacing the setup for" in said and syncer.settings().remote == str(bare)


def test_a_refusal_says_to_move_the_git_only_when_there_is_one(tmp_path, bare):
    syncer = machine(tmp_path)
    syncer.state_dir.mkdir(parents=True)
    engine_module.Settings(remote="git@git.example.com:me/library.git", name="Owner", email="owner@example.com",
                           label="laptop").save(syncer.settings_path)  # the wizard's, stopped before .git
    with pytest.raises(SyncError) as refused:
        from_env(syncer, {installer.REMOTE: str(bare), **IDENTITY})
    message = refused.value.message
    assert refused.value.reason == "connected" and ".git" not in message
    assert "-m src.user_sync disconnect" in message and "-m src.user_sync setup" in message


def first_github_run(fake, tmp_path, bare, monkeypatch, transport=None):
    """REPOSITORY from the environment with a token: the deploy key is added, then sync does not start."""
    syncer = github_machine(fake, tmp_path, transport or Transport(tmp_path, bare))
    save(syncer)
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    script_repository(fake)
    real_start = syncer.start
    monkeypatch.setattr(syncer, "start", lock_held)
    result, _ = run_step(syncer, assume_yes=True, token=TOKEN, environ={installer.REPO: REPOSITORY, **IDENTITY})
    assert (result["status"], result["reason"]) == ("attention", "lock_held") and len(posted_keys(fake)) == 1
    monkeypatch.setattr(syncer, "start", real_start)
    return syncer


def script_other(fake, add_status=201):
    """GitHub knows OTHER, private and empty, without deploy keys; adding one answers ``add_status``."""
    fake.reply("GET", f"/api/v3/repos/{OTHER}", 200, repo(OTHER), repeat=True)
    fake.reply("GET", f"/api/v3/repos/{OTHER}/branches", 200, [], repeat=True)
    fake.reply("GET", f"/api/v3/repos/{OTHER}/keys", 200, [])
    if add_status == 201:
        fake.reply("POST", f"/api/v3/repos/{OTHER}/keys", 201,
                   {"id": 21, "title": "Agents-Core laptop", "key": "ssh-ed25519 AAAA", "read_only": False})
    else:
        fake.reply("POST", f"/api/v3/repos/{OTHER}/keys", 422,
                   {"message": "Validation Failed", "errors": [{"message": "key is already in use"}]})


@needs_ssh_keygen
def test_replacing_an_env_github_setup_moves_this_machines_deploy_key(fake, tmp_path, bare, monkeypatch, scheduler):
    """GitHub accepts a key on one repository only: the replaced setup's repository gives it up first."""
    syncer = first_github_run(fake, tmp_path, bare, monkeypatch)
    public = keys.public_key(syncer.state_dir)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(syncer.state_dir, key_id=7)])
    fake.reply("DELETE", f"/api/v3/repos/{REPOSITORY}/keys/7", 204, None)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [])  # what setup_github's own look then finds
    script_other(fake)
    result, said = run_step(syncer, assume_yes=True, environ={installer.REPO: OTHER, **IDENTITY})
    assert result["status"] == "synced", said
    assert f"Removed this machine's deploy key from {REPOSITORY}" in said
    assert [(r.method, r.path) for r in fake.requests if r.method in ("DELETE", "POST") and "/keys" in r.path] == [
        ("POST", f"/api/v3/repos/{REPOSITORY}/keys"), ("DELETE", f"/api/v3/repos/{REPOSITORY}/keys/7"),
        ("POST", f"/api/v3/repos/{OTHER}/keys")]
    settings = syncer.settings()
    assert keys.public_key(syncer.state_dir) == public and settings.remote == f"git@github.com:{OTHER}.git"
    assert (settings.github_repository, settings.deploy_key_id, settings.deploy_key_added) == (OTHER, 21, True)
    assert not (syncer.state_dir / installer.MARKER).exists()  # started: nothing left to replace


@needs_ssh_keygen
def test_a_key_removed_from_the_old_repository_leaves_no_record_of_it(fake, tmp_path, bare, monkeypatch, scheduler):
    """The key left the old repository, then the new one refused it: the settings keep no id of a gone key."""
    syncer = first_github_run(fake, tmp_path, bare, monkeypatch)
    assert (syncer.settings().deploy_key_id, syncer.settings().deploy_key_added) == (11, True)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(syncer.state_dir, key_id=11)])
    fake.reply("DELETE", f"/api/v3/repos/{REPOSITORY}/keys/11", 204, None)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [])
    script_other(fake, add_status=422)
    result, said = run_step(syncer, assume_yes=True, environ={installer.REPO: OTHER, **IDENTITY})
    assert (result["status"], result["reason"]) == ("attention", "key_in_use")
    settings = syncer.settings()
    assert settings.remote == SSH_URL and (settings.deploy_key_id, settings.deploy_key_added) == (None, False)
    assert installer.made_here(syncer, settings)


@needs_ssh_keygen
def test_a_mistyped_new_repository_leaves_the_old_deploy_key_alone(fake, tmp_path, bare, monkeypatch, scheduler):
    syncer = first_github_run(fake, tmp_path, bare, monkeypatch)
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})  # the token again
    fake.reply("GET", "/api/v3/repos/octocat/agents-libary", 404, {"message": "Not Found"})
    asked_before = len(fake.requests)
    result, said = run_step(syncer, assume_yes=True, token=TOKEN,
                            environ={installer.REPO: "octocat/agents-libary", **IDENTITY})
    assert (result["status"], result["reason"]) == ("attention", "unknown_remote")
    assert "AGENTS_GITHUB_TOKEN may lack the repo scope" in said
    run_2 = [(r.method, r.path) for r in fake.requests[asked_before:]]
    assert run_2 == [("GET", "/api/v3/user"), ("GET", "/api/v3/repos/octocat/agents-libary")]  # old key untouched
    assert syncer.settings().remote == SSH_URL and installer.made_here(syncer, syncer.settings())


@needs_ssh_keygen
def test_a_key_github_still_refuses_names_the_old_repository_and_disconnect(fake, tmp_path, bare, monkeypatch,
                                                                            scheduler):
    syncer = first_github_run(fake, tmp_path, bare, monkeypatch)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [], repeat=True)  # gone, yet GitHub refuses it
    script_other(fake, add_status=422)
    result, said = run_step(syncer, assume_yes=True, environ={installer.REPO: OTHER, **IDENTITY})
    assert (result["status"], result["reason"]) == ("attention", "key_in_use")
    assert f"({REPOSITORY} had it last)" in said and "-m src.user_sync disconnect" in said
    # setup_github adds the key before it saves anything: the settings and the marker still name the
    # first repository, so the next run replaces that setup again.
    assert syncer.settings().remote == SSH_URL and installer.made_here(syncer, syncer.settings())


@needs_ssh_keygen
def test_replacing_a_github_setup_without_the_api_makes_a_new_key(fake, tmp_path, bare, scheduler):
    transport = Transport(tmp_path, bare, refuse={"github.com": REFUSED_KEY})
    syncer = github_machine(fake, tmp_path, transport, visibility=lambda remote: "private")
    save(syncer)
    result, _ = run_step(syncer, assume_yes=True, environ={installer.REPO: REPOSITORY, **IDENTITY})
    assert result["status"] == "waiting_for_access"
    old_key = keys.public_key(syncer.state_dir)
    result, said = run_step(syncer, assume_yes=True, environ={installer.REPO: OTHER, **IDENTITY})
    new_key = keys.public_key(syncer.state_dir)
    assert result["status"] == "waiting_for_access" and new_key and new_key != old_key and new_key in said
    assert f"{REPOSITORY} may still hold the old one" in said
    assert installer.made_here(syncer, syncer.settings())  # the marker follows the settings and the key
    assert fake.requests == []  # no account: GitHub's API was never asked


def test_a_refused_or_scopeless_token_names_the_variable_and_the_repo_scope(fake, tmp_path, bare):
    syncer = github_machine(fake, tmp_path, Transport(tmp_path, bare))
    fake.reply("GET", "/api/v3/user", 401, {"message": "Bad credentials"})
    environ = {installer.REPO: REPOSITORY, **IDENTITY}
    result, said = run_step(syncer, assume_yes=True, token=TOKEN, environ=environ)
    assert result["reason"] == "auth" and "GitHub refused AGENTS_GITHUB_TOKEN: it is wrong" in said
    assert "repo scope" in said and "reconnect" not in said
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})  # accepted, but it sees no private repository
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}", 404, {"message": "Not Found"})
    result, said = run_step(syncer, assume_yes=True, token=TOKEN, environ=environ)
    assert result["reason"] == "unknown_remote" and "AGENTS_GITHUB_TOKEN may lack the repo scope" in said
    assert syncer.settings() is None
    assert_secret_free(said)


def test_setup_from_env_says_once_what_dotenv_set(tmp_path, bare, monkeypatch, capsys, scheduler):
    root = tmp_path / "installation"
    (root / ".env").write_text(f"{installer.REPO}={REPOSITORY}\n{installer.TOKEN}={TOKEN}\n")
    monkeypatch.setattr(cli_module, "_load_env", REAL_LOAD_ENV)
    for name in NAMES:  # load_dotenv writes os.environ: monkeypatch restores it
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    syncer = machine(tmp_path)
    save(syncer)
    monkeypatch.setattr(cli_module, "Syncer", lambda library, state: syncer)
    for name, value in {installer.REMOTE: str(bare), **IDENTITY}.items():
        monkeypatch.setenv(name, value)
    assert cli(["installer", "--yes"], interactive=False) == 0
    out = capsys.readouterr().out
    assert out.count("Ignored in .env") == 1 and f"{installer.REPO}, {installer.TOKEN}" in out
    assert "Sync started" in out and syncer.settings().remote == str(bare)
    assert cli(["setup", "--from-env"]) == 0  # started: it reports the ignored values all the same
    out = capsys.readouterr().out
    assert out.count("Ignored in .env") == 1 and "nothing changed" in out
    assert_secret_free(out)


def pending_machine(tmp_path, remote, state=None, *, ours=False):
    syncer = machine(tmp_path)
    syncer.state_dir.mkdir(parents=True)
    engine_module.Settings(remote=remote, name="Owner", email="owner@example.com",
                           label="laptop").save(syncer.settings_path)
    (syncer.state_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPendingKey laptop\n")
    if state is not None:
        syncer.state_path.write_text(json.dumps(state))
    if ours:
        installer._mark(syncer)
    return syncer


@pytest.mark.parametrize("remote, state, ours, expected", [
    ("git@git.example.com:me/library.git", None, True,
     ["host key of git.example.com is not confirmed", "setup --from-env --trust-host-key SHA256:..."]),
    ("git@git.example.com:me/library.git", None, False,
     ["host key of git.example.com is not confirmed", "-m src.user_sync setup"]),
    (None, {"state": "attention", "reason": "public_repo", "message": "could not confirm"}, True,
     ["could not check that the repository is private", "setup --from-env --confirm-private"]),
    (None, {"state": "offline", "reason": "network", "message": "Connection timed out"}, True,
     ["could not be reached (Connection timed out)", "setup --from-env"]),
    (None, {"state": "waiting_for_access", "reason": "auth", "access": "denied"}, True,
     ["refuses this machine's key", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPendingKey", "setup --from-env"]),
    (None, {"state": "waiting_for_access", "access": "ok"}, True,
     ["-m src.user_sync preview", "-m src.user_sync start --confirm HASH"]),
    (None, None, False, ["To finish it", "-m src.user_sync setup"]),
], ids=["host-key-env", "host-key-wizard", "privacy", "network", "deploy-key", "confirmation", "fresh"])
def test_the_summary_of_a_pending_setup_names_the_next_step(tmp_path, bare, remote, state, ours, expected):
    syncer = pending_machine(tmp_path, remote or str(bare), state, ours=ours)
    said = []
    assert installer.summary(syncer, say=said.append) == {"status": "pending"}
    text = "\n".join(said)
    assert "but not started yet" in text and all(piece in text for piece in expected), text
    assert ("same AGENTS_USER_SYNC_* variables" in text) is (ours and "HASH" not in text)


def test_commands_say_where_they_run_also_on_windows(monkeypatch):
    root = str(installer.installation_root())
    assert installer.command("src.user_sync", "status").startswith("cd ")
    monkeypatch.setattr(installer, "_windows", lambda: True)
    windows = installer.command("src.user_sync", "status")
    assert windows.startswith(f"in {root}, run ") and windows.endswith(" -m src.user_sync status")
    assert "&&" not in windows and "cd " not in windows  # cmd and PowerShell chain commands differently


def test_loopback_calls_never_go_through_an_http_proxy(tmp_path):
    """settings_page and the controller's requests carry the daemon's bearer token: never to a proxy."""
    import socket
    received = []
    proxy = socket.socket()
    proxy.bind(("127.0.0.1", 0))
    proxy.listen(5)

    def serve():
        while True:
            try:
                connection, _ = proxy.accept()
            except OSError:
                return
            received.append(connection.recv(65536))
            connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            connection.close()
    threading.Thread(target=serve, daemon=True).start()
    directory = tmp_path / "service"
    daemon = FakeDaemon(directory)
    code = ("import sys\nfrom pathlib import Path\nfrom src.user_sync import installer\n"
            "from src.daemon.control import Controller\n"
            f"print(installer.settings_page(Path({str(directory)!r})))\n"
            f"print(Controller(Path({str(directory)!r})).request('/health'))\n")
    environment = {key: value for key, value in os.environ.items() if not key.lower().endswith("_proxy")}
    address = f"http://127.0.0.1:{proxy.getsockname()[1]}"
    environment.update(http_proxy=address, HTTP_PROXY=address, PYTHONPATH=str(ROOT))
    try:
        result = subprocess.run([sys.executable, "-c", code], env=environment, cwd=ROOT, capture_output=True,
                                text=True, timeout=60, start_new_session=True)
    finally:
        daemon.close()
        proxy.close()
    port = daemon.server.server_address[1]
    assert result.stdout.splitlines() == [f"http://127.0.0.1:{port}/ui#one-use", "{'state': 'ready'}"], result.stderr
    assert received == []


def test_a_repository_of_another_owner_needs_the_owner_confirmed_from_the_environment_too(fake, tmp_path, bare):
    syncer = github_machine(fake, tmp_path, Transport(tmp_path, bare))
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    fake.reply("GET", "/api/v3/repos/acme/library", 200, repo("acme/library"), repeat=True)
    result, said = run_step(syncer, assume_yes=True, token=TOKEN, environ={installer.REPO: "acme/library", **IDENTITY})
    assert (result["status"], result["reason"]) == ("attention", "owner_unconfirmed")
    assert "-m src.user_sync setup --from-env --confirm-owner acme" in said
    assert syncer.settings() is None and not posted_keys(fake)
