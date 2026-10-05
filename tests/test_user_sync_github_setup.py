"""GitHub in the sync engine and its command line (#166, the terminal wizard of #168).

The fake GitHub of ``test_user_sync_github`` answers the API; git transport goes to a local bare
repository through a fake ``GIT_SSH_COMMAND`` that serves every host from it, can refuse hosts the
way a blocked port or a refused key does, and records its arguments. Nothing reaches the real
GitHub, a network or an OS secret store.
"""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import shutil
import sys

import pytest

from src.flows import FlowCatalog
from src.user_flows import FlowLibrary
from src.user_sync import engine as engine_module, keys, wizard
from src.user_sync.__main__ import main as cli
from src.user_sync.engine import SyncError, Syncer
from src.user_sync.github import GitHubAccount, GitHubError
from src.user_sync.gitcmd import parse_remote
from tests.test_user_sync import plain_git, remote_files
from tests.test_user_sync_github import (  # noqa: F401  (fake and no_real_secret_store are fixtures)
    DEVICE_CODE, GRANTED, PENDING, TOKEN, MemoryStore, fake, no_real_secret_store, repo)

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBmFjZWJvb2tob3N0a2V5Zm9ydGVzdHNvbmx5MDA"
REPOSITORY = "octocat/agents-library"
SSH_URL = f"git@github.com:{REPOSITORY}.git"
PORT_443_URL = f"ssh://git@ssh.github.com:443/{REPOSITORY}.git"
BLOCKED_22 = "ssh: connect to host github.com port 22: Connection timed out"
REFUSED_KEY = "git@github.com: Permission denied (publickey)."
needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

FAKE_SSH = '''\
import json, os, shlex, subprocess, sys
config = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake-ssh.json"), encoding="utf-8"))
arguments = sys.argv[1:]
with open(config["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps(arguments) + "\\n")
if "-G" in arguments:
    sys.exit(0)  # git asks whether this is OpenSSH, which accepts -p
host = next(word for word in arguments if "@" in word and not word.startswith("-")).split("@", 1)[1]
if host in config["refuse"]:
    sys.stderr.write(config["refuse"][host] + "\\n")
    sys.exit(255)
service = shlex.split(arguments[-1])[0]  # git-upload-pack or git-receive-pack
environment = {key: value for key, value in os.environ.items()
               if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
sys.exit(subprocess.call(["git", service[len("git-"):], config["bare"]], env=environment))
'''


class Transport:
    """A ``GIT_SSH_COMMAND`` serving every host from one bare repository, except the refused hosts."""

    def __init__(self, tmp_path: Path, bare: Path, refuse: dict | None = None):
        directory = tmp_path / "fake-ssh"
        directory.mkdir(exist_ok=True)
        self.log = directory / "calls.jsonl"
        self.config = directory / "fake-ssh.json"
        self.refuse(refuse or {}, bare)
        script = directory / "fake_ssh.py"
        script.write_text(FAKE_SSH, encoding="utf-8")
        self.command = f"{shlex.quote(Path(sys.executable).as_posix())} {shlex.quote(script.as_posix())}"

    def refuse(self, hosts: dict, bare: Path | None = None) -> None:
        current = json.loads(self.config.read_text()) if self.config.exists() else {}
        self.config.write_text(json.dumps({"bare": str(bare or current["bare"]), "refuse": hosts,
                                           "log": str(self.log)}), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


class Script:
    """Scripted answers for the wizard's ``ask``; records every prompt."""

    def __init__(self, *answers: str):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError(prompt)
        return self.answers.pop(0)


@pytest.fixture
def bare(tmp_path) -> Path:
    path = tmp_path / "remote.git"
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(path))
    return path


def make_account(fake, state: Path, *, client_id: str = "Iv1.test") -> GitHubAccount:
    return GitHubAccount(state, "agents-core-sync-test", host="github.com", web_url=fake.url, api_url=fake.api,
                         client_id=client_id, store=MemoryStore())


def signed_in(fake, state: Path) -> GitHubAccount:
    account = make_account(fake, state)
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    account.complete_sign_in(TOKEN)
    return account


def make_syncer(tmp_path: Path, account: GitHubAccount | None, **options) -> Syncer:
    options.setdefault("github_host_keys", lambda: [HOST_KEY])
    return Syncer(tmp_path / "library", tmp_path / "state", github_account=account, **options)


def deploy_key(state: Path, key_id: int = 7, read_only: bool = False) -> dict:
    material = " ".join(keys.public_key(state).split()[:2])
    return {"id": key_id, "title": "Agents-Core laptop", "key": material, "read_only": read_only}


def script_repository(fake, keys_listed=(), *, private=True) -> None:
    """GitHub knows ``octocat/agents-library``; its deploy keys list ``keys_listed`` once."""
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}", 200, repo(REPOSITORY, private=private), repeat=True)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, list(keys_listed))
    fake.reply("POST", f"/api/v3/repos/{REPOSITORY}/keys", 201,
               {"id": 11, "title": "Agents-Core laptop", "key": "ssh-ed25519 AAAA", "read_only": False})


def posted_keys(fake) -> list:
    return [request for request in fake.requests if request.method == "POST" and request.path.endswith("/keys")]


def save_flow(tmp_path: Path, name: str = "user:hello", text: str = "# Hello\n") -> None:
    FlowLibrary(FlowCatalog(), user_dir=tmp_path / "library").save(name, text)


# --- privacy through the API ----------------------------------------------------------------


@pytest.mark.parametrize("payload, verdict", [
    (repo(REPOSITORY), "private"), (repo(REPOSITORY, private=False), "public"),
    (repo(REPOSITORY, visibility="internal"), "internal"),
])
def test_privacy_of_a_github_remote_comes_from_the_api(fake, tmp_path, payload, verdict):
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"))
    syncer.anonymous_visibility = lambda remote: pytest.fail("the API answers for a connected account")
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}", 200, payload, repeat=True)
    for url in (SSH_URL, PORT_443_URL):
        assert syncer.default_visibility(parse_remote(url)) == verdict


def test_a_refused_token_marks_reconnect_needed_and_the_anonymous_check_decides(fake, tmp_path):
    account = signed_in(fake, tmp_path / "state")
    syncer = make_syncer(tmp_path, account)
    anonymous = []
    syncer.anonymous_visibility = lambda remote: anonymous.append(remote.url) or "private"
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}", 401, {"message": "Bad credentials"})
    assert syncer.default_visibility(parse_remote(SSH_URL)) == "private"
    assert anonymous == [SSH_URL] and account.status()["reconnect_needed"] is True
    asked = len(fake.requests)
    assert syncer.default_visibility(parse_remote(SSH_URL)) == "private"
    assert len(fake.requests) == asked  # a refused token is not used again until the owner signs in


def test_other_api_failures_leave_privacy_unknown_and_other_hosts_use_the_anonymous_check(fake, tmp_path):
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"))
    syncer.anonymous_visibility = lambda remote: "anonymous"
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}", 502, None)
    assert syncer.default_visibility(parse_remote(SSH_URL)) == "unknown"
    for url in ("git@gitlab.example.com:octocat/agents-library.git", "git@github.com:octocat/a/b.git"):
        assert syncer.default_visibility(parse_remote(url)) == "anonymous"
    signed_out = make_syncer(tmp_path / "other", make_account(fake, tmp_path / "other" / "state"))
    signed_out.anonymous_visibility = lambda remote: "anonymous"
    assert signed_out.default_visibility(parse_remote(SSH_URL)) == "anonymous"


def test_an_internal_repository_is_refused_before_the_first_upload(tmp_path, bare):
    syncer = Syncer(tmp_path / "library", tmp_path / "state", allow_file_remote=True,
                    visibility=lambda remote: "internal")
    save_flow(tmp_path)
    syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")
    result = syncer.start(syncer.preview()["hash"])
    assert result["reason"] == "public_repo" and "internal" in result["message"]
    assert remote_files(bare) == {}


# --- setup --github, add-key and the diagnosis ----------------------------------------------


@needs_ssh_keygen
def test_setup_github_adds_this_machines_deploy_key_once(fake, tmp_path):
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"))
    script_repository(fake)
    first = syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    assert (first["status"], first["repository"], first["deploy_key"]) == ("waiting_for_access", REPOSITORY, "added")
    assert first["steps"][0] == f"{REPOSITORY} is private" and "added this machine's deploy key" in first["steps"][-1]
    [posted] = posted_keys(fake)
    assert posted.json == {"title": "Agents-Core laptop", "key": " ".join(first["public_key"].split()[:2]),
                           "read_only": False}
    assert syncer.settings().remote == SSH_URL
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(tmp_path / "state")])
    second = syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    assert second["deploy_key"] == "present" and len(posted_keys(fake)) == 1


def test_setup_github_refuses_a_missing_or_public_repository_and_needs_an_account(fake, tmp_path):
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"))
    fake.reply("GET", "/api/v3/repos/octocat/missing", 404, {"message": "Not Found"})
    with pytest.raises(SyncError) as missing:
        syncer.setup_github("octocat/missing", name="Owner", email="owner@example.com")
    assert missing.value.reason == "unknown_remote"
    assert "python -m src.user_sync github create missing" in missing.value.message
    fake.reply("GET", "/api/v3/repos/octocat/site", 200, repo("octocat/site", private=False))
    with pytest.raises(GitHubError) as public:
        syncer.setup_github("octocat/site", name="Owner", email="owner@example.com")
    assert public.value.code == "public_repo" and syncer.settings() is None
    signed_out = make_syncer(tmp_path / "other", make_account(fake, tmp_path / "other" / "state"))
    with pytest.raises(SyncError) as anonymous:
        signed_out.setup_github(REPOSITORY, name="Owner", email="owner@example.com")
    assert anonymous.value.reason == "not_signed_in" and "github login" in anonymous.value.message


@needs_ssh_keygen
def test_check_tells_whether_the_deploy_key_is_still_there_and_add_key_restores_it(fake, tmp_path, bare):
    transport = Transport(tmp_path, bare, refuse={"github.com": REFUSED_KEY})
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"), ssh_command=transport.command,
                         visibility=lambda remote: "private")
    script_repository(fake)
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [])  # removed on GitHub meanwhile
    with pytest.raises(SyncError) as refused:
        syncer.check()
    assert refused.value.reason == "auth" and refused.value.details == {"deploy_key": "missing",
                                                                       "repository": REPOSITORY}
    assert "python -m src.user_sync github add-key" in refused.value.message
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [])
    fake.reply("POST", f"/api/v3/repos/{REPOSITORY}/keys", 201,
               {"id": 12, "title": "Agents-Core laptop", "key": "ssh-ed25519 AAAA", "read_only": False})
    assert syncer.add_deploy_key()["status"] == "added" and len(posted_keys(fake)) == 2
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(tmp_path / "state", 12)])
    assert syncer.add_deploy_key()["status"] == "present" and len(posted_keys(fake)) == 2
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(tmp_path / "state", 12)])
    with pytest.raises(SyncError) as still:
        syncer.check()
    assert still.value.details["deploy_key"] == "present" and "still on" in still.value.message


# --- SSH over port 443 ------------------------------------------------------------------------


@needs_ssh_keygen
def test_a_blocked_port_22_moves_the_remote_to_ssh_github_com_on_port_443(fake, tmp_path, bare):
    transport = Transport(tmp_path, bare, refuse={"github.com": BLOCKED_22})
    syncer = make_syncer(tmp_path, None, ssh_command=transport.command, visibility=lambda remote: "private")
    save_flow(tmp_path)
    syncer.setup(remote=SSH_URL, name="Owner", email="owner@example.com", label="laptop")
    result = syncer.check()
    assert result["port_443"] is True and result["remote_state"] == "empty" and "port 443" in result["message"]
    assert syncer.settings().remote == PORT_443_URL
    assert any("-p" in call and "443" in call and "git@ssh.github.com" in call for call in transport.calls())
    assert syncer.start(syncer.preview()["hash"])["status"] == "synced"
    assert "common/hello.md" in remote_files(bare)
    known = (tmp_path / "state" / "known_hosts").read_text()
    assert f"[ssh.github.com]:443 {HOST_KEY}" in known  # written with github.com's at setup


@needs_ssh_keygen
def test_when_port_443_fails_too_the_configured_remote_stays(fake, tmp_path, bare):
    transport = Transport(tmp_path, bare, refuse={"github.com": BLOCKED_22, "ssh.github.com": BLOCKED_22})
    syncer = make_syncer(tmp_path, None, ssh_command=transport.command, visibility=lambda remote: "private")
    syncer.setup(remote=SSH_URL, name="Owner", email="owner@example.com", label="laptop")
    with pytest.raises(SyncError) as offline:
        syncer.check()
    assert (offline.value.reason, offline.value.state) == ("network", "offline")
    assert syncer.settings().remote == SSH_URL


# --- the terminal wizard ----------------------------------------------------------------------


def script_device_flow(fake) -> None:
    fake.reply("POST", "/login/device/code", 200, {
        "device_code": DEVICE_CODE, "user_code": "WDJB-MJHT", "verification_uri": "https://github.com/login/device",
        "expires_in": 900, "interval": 5})
    for answer in (PENDING, GRANTED):
        fake.reply("POST", "/login/oauth/access_token", 200, answer)
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})


@needs_ssh_keygen
@pytest.mark.parametrize("answer, started", [("yes", True), ("no", False)])
def test_the_wizard_signs_in_creates_the_repository_and_starts_only_after_yes(fake, tmp_path, bare, answer, started):
    transport = Transport(tmp_path, bare)
    syncer = make_syncer(tmp_path, make_account(fake, tmp_path / "state"), ssh_command=transport.command)
    save_flow(tmp_path)
    script_device_flow(fake)
    fake.reply("GET", "/api/v3/user/repos", 200, [])
    fake.reply("POST", "/api/v3/user/repos", 201, repo(REPOSITORY))
    script_repository(fake)
    said, opened = [], []
    ask = Script("", "1", "", "Owner", "owner@example.com", "laptop", answer)
    result = wizard.run(syncer, ask=ask, say=said.append, open_browser=opened.append, sleep=lambda seconds: None)
    text = "\n".join(said)
    assert "WDJB-MJHT" in text and opened == ["https://github.com/login/device"]
    assert "Signed in to github.com as octocat." in text and f"Created the private repository {REPOSITORY}." in text
    assert "Upload (" in text and "user:hello  common/hello.md" in text
    assert [r.json["name"] for r in fake.requests if r.path == "/api/v3/user/repos" and r.method == "POST"] \
        == ["agents-library"]
    assert len(posted_keys(fake)) == 1 and ask.answers == []
    if started:
        assert result["status"] == "synced" and "common/hello.md" in remote_files(bare)
    else:
        assert (result["status"], result["reason"]) == ("attention", "confirmation_needed")
        assert remote_files(bare) == {} and syncer.settings().started is None


@needs_ssh_keygen
def test_the_wizard_falls_back_to_a_manual_ssh_url_and_confirms_host_key_and_privacy(fake, tmp_path, bare):
    transport = Transport(tmp_path, bare)
    syncer = make_syncer(tmp_path, make_account(fake, tmp_path / "state", client_id=""),
                         ssh_command=transport.command, scan_host_keys=lambda host, port: [HOST_KEY],
                         visibility=lambda remote: "unknown")
    save_flow(tmp_path)
    fingerprint = keys.fingerprint(HOST_KEY)
    said = []
    ask = Script("g", "yes", "git@git.example.com:me/library.git", "", "Owner", "not-an-email",
                 "owner@example.com", "Bad Label", "laptop", "SHA256:wrong", fingerprint, "", "yes", "yes")
    result = wizard.run(syncer, ask=ask, say=said.append, open_browser=lambda url: None, sleep=lambda s: None)
    text = "\n".join(said)
    assert "GitHub cannot be used" in text and "AGENTS_GITHUB_CLIENT_ID" in text
    assert "An answer is required." in text and "a commit email is required" in text
    assert "That is not one of the fingerprints above." in text and fingerprint in text
    assert "agents-core-sync:laptop" in text  # the public key to add by hand
    assert result["status"] == "synced" and "common/hello.md" in remote_files(bare)
    assert syncer.settings().private_confirmed is True and ask.answers == []
    assert fake.requests == []  # no GitHub API call without a client ID


def test_the_wizard_stops_when_privacy_is_not_confirmed(tmp_path, bare):
    syncer = Syncer(tmp_path / "library", tmp_path / "state", allow_file_remote=True,
                    visibility=lambda remote: "unknown")
    save_flow(tmp_path)
    ask = Script("m", str(bare), "Owner", "owner@example.com", "laptop", "no")
    result = wizard.run(syncer, ask=ask, say=lambda text: None, open_browser=lambda url: None,
                        sleep=lambda s: None)
    assert result["reason"] == "public_repo" and remote_files(bare) == {}


# --- the command line -----------------------------------------------------------------------


@pytest.fixture
def cli_account(fake, tmp_path, monkeypatch):
    """The CLI's Syncer gets an account on the fake GitHub, and github.com's host keys without the network."""
    account = make_account(fake, tmp_path / "state")
    monkeypatch.setattr(engine_module, "default_github_account", lambda state_dir: account)
    monkeypatch.setattr(engine_module.keys, "github_host_keys", lambda: [HOST_KEY])
    return account


def run_cli(tmp_path, *arguments, **options):
    return cli(["--state", str(tmp_path / "state"), "--library", str(tmp_path / "library"), *arguments], **options)


def test_cli_github_login_status_libraries_create_and_logout(fake, tmp_path, cli_account, capsys):
    script_device_flow(fake)
    said, opened = [], []
    assert run_cli(tmp_path, "github", "login", "--json", say=said.append, open_browser=opened.append,
                   sleep=lambda seconds: None) == 0
    login = json.loads(capsys.readouterr().out)
    assert (login["status"], login["login"], login["storage"]) == ("connected", "octocat", "memory")
    assert any("WDJB-MJHT" in line for line in said) and opened == ["https://github.com/login/device"]
    assert run_cli(tmp_path, "github", "status") == 0
    assert "github status: connected" in capsys.readouterr().out
    fake.reply("GET", "/api/v3/user/repos", 200, [repo(REPOSITORY)])
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/contents/.agents-library.json", 200, {"type": "file"})
    assert run_cli(tmp_path, "github", "libraries", "--json") == 0
    assert json.loads(capsys.readouterr().out)["repositories"][0]["ssh_url"] == SSH_URL
    fake.reply("POST", "/api/v3/user/repos", 201, repo("octocat/library-two"))
    assert run_cli(tmp_path, "github", "create", "library-two") == 0
    out = capsys.readouterr().out
    assert "github create: created" in out and "setup --github octocat/library-two" in out
    assert [r.json["name"] for r in fake.requests if r.method == "POST" and r.path == "/api/v3/user/repos"] \
        == ["library-two"]
    assert run_cli(tmp_path, "github", "logout") == 0
    out = capsys.readouterr().out
    assert "github logout: forgotten" in out and f"{fake.url}/settings/applications" in out
    assert cli_account.status()["connected"] is False


@needs_ssh_keygen
def test_cli_setup_github_and_add_key(fake, tmp_path, cli_account, capsys):
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    cli_account.complete_sign_in(TOKEN)
    script_repository(fake)
    assert run_cli(tmp_path, "setup", "--github", REPOSITORY, "--name", "Owner", "--email", "owner@example.com",
                   "--label", "laptop") == 0
    out = capsys.readouterr().out
    assert "setup: waiting_for_access" in out and "added this machine's deploy key" in out
    assert "deploy_key: added" in out
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(tmp_path / "state")])
    assert run_cli(tmp_path, "github", "add-key", "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "present" and len(posted_keys(fake)) == 1


@pytest.mark.parametrize("arguments, scripted, reason", [
    (("github", "libraries"), ("GET", "/api/v3/user/repos", 401, {"message": "Bad credentials"}), "auth"),
    (("github", "create"), ("POST", "/api/v3/user/repos", 422, {"message": "Repository creation failed.",
                                                                "errors": [{"message": "name already exists"}]}),
     "exists"),
    (("setup", "--github", "octocat/missing", "--name", "Owner", "--email", "owner@example.com"),
     ("GET", "/api/v3/repos/octocat/missing", 404, {"message": "Not Found"}), "unknown_remote"),
])
def test_cli_maps_github_failures_to_reasons(fake, tmp_path, cli_account, capsys, arguments, scripted, reason):
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    cli_account.complete_sign_in(TOKEN)
    fake.reply(*scripted)
    assert run_cli(tmp_path, *arguments, "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert (result["status"], result["reason"]) == ("attention", reason)
    if reason == "unknown_remote":
        assert "github create missing" in result["message"]


def test_cli_reports_sign_in_problems_without_a_traceback(fake, tmp_path, monkeypatch, capsys):
    account = make_account(fake, tmp_path / "state", client_id="")
    monkeypatch.setattr(engine_module, "default_github_account", lambda state_dir: account)
    assert run_cli(tmp_path, "github", "login") == 1
    out = capsys.readouterr()
    assert "github login: attention (no_client_id)" in out.out and "Traceback" not in out.out + out.err
    assert run_cli(tmp_path, "github", "libraries", "--json") == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "not_signed_in"


def test_cli_setup_needs_a_terminal_for_the_wizard_and_an_identity_otherwise(tmp_path, capsys):
    assert run_cli(tmp_path, "setup", interactive=False) == 2
    assert "a terminal for the wizard" in capsys.readouterr().err
    with pytest.raises(SystemExit) as usage:
        run_cli(tmp_path, "setup", "--github", REPOSITORY)
    assert usage.value.code == 2
    assert "--name and --email are required" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run_cli(tmp_path, "setup", "--github", REPOSITORY, "--remote", SSH_URL, "--name", "O", "--email", "o@x.y")


def test_cli_wizard_reads_answers_and_a_cancelled_wizard_uploads_nothing(tmp_path, bare, monkeypatch, capsys):
    monkeypatch.setattr(engine_module, "default_github_account", lambda state_dir: pytest.fail("no GitHub here"))
    save_flow(tmp_path)
    said = []
    ask = Script("m", "git@git.example.com:me/library.git", "Owner")
    assert run_cli(tmp_path, "setup", "--json", ask=ask, say=said.append, interactive=True) == 1  # answers run out
    result = json.loads(capsys.readouterr().out)
    assert (result["status"], result["reason"]) == ("cancelled", "cancelled")
    assert ask.prompts[-1].startswith("Your email for commits") and said
    assert remote_files(bare) == {} and not (tmp_path / "state" / "user-sync.json").exists()


def test_the_sync_status_shows_the_github_account_without_its_token(fake, tmp_path):
    state = tmp_path / "state"
    syncer = make_syncer(tmp_path, signed_in(fake, state))
    syncer.setup(remote="git@github.com:me/agents-library.git", name="Owner", email="owner@example.com",
                 label="a")
    status = syncer.status()
    assert status["github"]["connected"] is True and status["github"]["login"] == "octocat"
    assert TOKEN not in json.dumps(status)
