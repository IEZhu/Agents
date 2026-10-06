"""GitHub in the sync engine and its command line (#166, the terminal wizard of #168).

The fake GitHub of ``test_user_sync_github`` answers the API; git transport goes to a local bare
repository through a fake ``GIT_SSH_COMMAND`` that serves every host from it, can refuse hosts the
way a blocked port or a refused key does, and records its arguments. Nothing reaches the real
GitHub, a network or an OS secret store.
"""
from __future__ import annotations

import json
import os
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
from src.user_sync.github import GitHubAccount, GitHubClient, GitHubError
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


def script_repository(fake, keys_listed=(), *, private=True, library=False) -> None:
    """GitHub knows ``octocat/agents-library``, empty or (``library``) holding the marker on main;
    its deploy keys list ``keys_listed`` once, and an added key gets the id 11."""
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}", 200, repo(REPOSITORY, private=private), repeat=True)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/branches", 200, [{"name": "main"}] if library else [],
               repeat=True)
    if library:
        fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/contents/.agents-library.json", 200, {"type": "file"},
                   repeat=True)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, list(keys_listed))
    fake.reply("POST", f"/api/v3/repos/{REPOSITORY}/keys", 201,
               {"id": 11, "title": "Agents-Core laptop", "key": "ssh-ed25519 AAAA", "read_only": False})


def key_requests(fake, repository: str = REPOSITORY) -> list[tuple[str, str]]:
    prefix = f"/api/v3/repos/{repository}/keys"
    return [(request.method, request.path[len(prefix):] or "/") for request in fake.requests
            if request.path.startswith(prefix)]


def push_foreign_content(tmp_path: Path, bare: Path) -> None:
    """An unrelated project lands in the repository: it holds no library marker."""
    work = tmp_path / "unrelated"
    plain_git("init", "--quiet", "--initial-branch=main", str(work))
    (work / "README.md").write_text("an unrelated project\n")
    plain_git("-C", str(work), "add", "README.md")
    plain_git("-C", str(work), "commit", "-q", "-m", "init")
    plain_git("-C", str(work), "push", "-q", str(bare), "main")


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


@pytest.mark.parametrize("status, payload, headers", [
    (502, None, {}), (403, {"message": "Resource not accessible"}, {}), (404, {"message": "Not Found"}, {}),
    (429, {"message": "Too Many Requests"}, {"Retry-After": "60"}),
])
def test_an_api_that_cannot_answer_leaves_the_decision_to_the_anonymous_check(fake, tmp_path, status, payload,
                                                                               headers):
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"))
    syncer.anonymous_visibility = lambda remote: "anonymous"
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}", status, payload, headers)
    assert syncer.default_visibility(parse_remote(SSH_URL)) == "anonymous"


def test_other_hosts_and_a_signed_out_machine_use_the_anonymous_check(fake, tmp_path):
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"))
    syncer.anonymous_visibility = lambda remote: "anonymous"
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
    assert first["steps"][0] == f"{REPOSITORY} is private and empty"
    assert "added this machine's deploy key" in first["steps"][-1] and syncer.settings().deploy_key_id == 11
    [posted] = posted_keys(fake)
    assert posted.json == {"title": "Agents-Core laptop", "key": " ".join(first["public_key"].split()[:2]),
                           "read_only": False}
    assert syncer.settings().remote == SSH_URL and syncer.settings().deploy_key_id == 11
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(tmp_path / "state")])
    second = syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    assert second["deploy_key"] == "present" and len(posted_keys(fake)) == 1
    assert syncer.settings().deploy_key_id == 7  # the one GitHub has


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
    assert syncer.settings().deploy_key_id == 12  # recorded as this machine's deploy key
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
    assert "an anonymous check over HTTPS gave no clear answer" in text
    assert any("which only its owner and the people they invite can read" in prompt for prompt in ask.prompts)
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


# --- the second review (#219) -------------------------------------------------------------------


def test_a_repository_with_other_content_is_refused_before_any_key_or_setting(fake, tmp_path):
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"))
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}", 200, repo(REPOSITORY), repeat=True)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/branches", 200, [{"name": "main"}])
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/contents/.agents-library.json", 404, {"message": "Not Found"})
    with pytest.raises(SyncError) as foreign:
        syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    assert foreign.value.reason == "unknown_remote" and "not an Agents-Core library" in foreign.value.message
    assert key_requests(fake) == [] and syncer.settings() is None
    marker = [request for request in fake.requests if request.path.endswith("/.agents-library.json")]
    assert marker[0].query == {"ref": ["main"]}


@needs_ssh_keygen
@pytest.mark.parametrize("blocked", [False, True], ids=["port 22", "port 443"])
def test_check_withdraws_the_key_setup_added_when_the_repository_holds_other_content(fake, tmp_path, bare, blocked):
    transport = Transport(tmp_path, bare, refuse={"github.com": BLOCKED_22} if blocked else {})
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"), ssh_command=transport.command,
                         visibility=lambda remote: "private")
    script_repository(fake)
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    push_foreign_content(tmp_path, bare)  # after the API check: someone used the repository meanwhile
    fake.reply("DELETE", f"/api/v3/repos/{REPOSITORY}/keys/11", 204)
    with pytest.raises(SyncError) as refused:
        syncer.check()
    assert refused.value.reason == "unknown_remote" and "was removed" in refused.value.message
    assert refused.value.details == {"deploy_key": "removed", "repository": REPOSITORY}
    assert ("DELETE", "/11") in key_requests(fake) and syncer.settings().deploy_key_id is None
    assert syncer.settings().remote == (PORT_443_URL if blocked else SSH_URL)  # reached over 443: kept


@needs_ssh_keygen
def test_check_keeps_a_deploy_key_that_setup_found_rather_than_added(fake, tmp_path, bare):
    transport = Transport(tmp_path, bare)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    keys.ensure_key(state, "laptop")  # the owner added this machine's key on GitHub before setup
    syncer = make_syncer(tmp_path, signed_in(fake, state), ssh_command=transport.command,
                         visibility=lambda remote: "private")
    script_repository(fake, keys_listed=[deploy_key(state, 7)])
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    settings = syncer.settings()
    assert settings.deploy_key_id == 7 and settings.deploy_key_added is False
    push_foreign_content(tmp_path, bare)
    with pytest.raises(SyncError) as refused:
        syncer.check()
    assert refused.value.reason == "unknown_remote" and "removed" not in refused.value.message
    assert ("DELETE", "/7") not in key_requests(fake) and syncer.settings().deploy_key_id == 7


@needs_ssh_keygen
def test_add_key_checks_privacy_and_content_before_adding(fake, tmp_path):
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"))
    script_repository(fake)
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    fake.replies.pop(("GET", f"/api/v3/repos/{REPOSITORY}/branches"))
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/branches", 200, [{"name": "main"}])
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/contents/.agents-library.json", 404, {"message": "Not Found"})
    with pytest.raises(SyncError) as foreign:
        syncer.add_deploy_key()
    assert foreign.value.reason == "unknown_remote" and len(posted_keys(fake)) == 1
    fake.replies.pop(("GET", f"/api/v3/repos/{REPOSITORY}"))
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}", 200, repo(REPOSITORY, private=False))
    with pytest.raises(GitHubError) as public:
        syncer.add_deploy_key()
    assert public.value.code == "public_repo" and len(posted_keys(fake)) == 1


@needs_ssh_keygen
@pytest.mark.parametrize("message, reason", [
    ("git@ssh.github.com: Permission denied (publickey).", "auth"),
    ("Host key verification failed.", "host_key"),
])
def test_a_refusal_on_port_443_is_reported_and_keeps_the_443_url(fake, tmp_path, bare, message, reason):
    transport = Transport(tmp_path, bare, refuse={"github.com": BLOCKED_22, "ssh.github.com": message})
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"), ssh_command=transport.command,
                         visibility=lambda remote: "private")
    script_repository(fake)
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [])  # the key is gone on GitHub
    with pytest.raises(SyncError) as error:
        syncer.check()
    assert error.value.reason == reason and error.value.state != "offline"
    assert syncer.settings().remote == PORT_443_URL
    if reason == "auth":  # the diagnosis runs, and the wizard offers to check again
        assert error.value.details == {"deploy_key": "missing", "repository": REPOSITORY}
        fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [])
        ask = Script("q")
        with pytest.raises(wizard.Cancelled):
            wizard._check(syncer, ask=ask, say=lambda text: None)
        assert ask.prompts == ["Press Enter to check again, or type q to stop: "]


@needs_ssh_keygen
def test_setup_again_keeps_the_port_443_url_and_the_history_of_the_same_repository(fake, tmp_path, bare):
    transport = Transport(tmp_path, bare, refuse={"github.com": BLOCKED_22})
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"), ssh_command=transport.command,
                         visibility=lambda remote: "private")
    script_repository(fake)
    save_flow(tmp_path)
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    assert syncer.check()["port_443"] is True
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(tmp_path / "state", 11)])
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")  # before start
    assert syncer.settings().remote == PORT_443_URL and syncer._state().get("access") == "ok"
    assert syncer.start(syncer.preview()["hash"])["status"] == "synced"
    git_dir = str(tmp_path / "library" / ".git")
    head = plain_git("--git-dir", git_dir, "rev-parse", "refs/heads/main")
    fake.replies.pop(("GET", f"/api/v3/repos/{REPOSITORY}/branches"))
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/branches", 200, [{"name": "main"}], repeat=True)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/contents/.agents-library.json", 200, {"type": "file"}, repeat=True)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(tmp_path / "state", 11)])
    again = syncer.setup_github(REPOSITORY, name="New Name", email="owner@example.com", label="laptop")
    assert again["deploy_key"] == "present" and "holds a library" in again["steps"][0]
    syncer.setup(remote=SSH_URL, name="Newer Name", email="owner@example.com", label="laptop")
    settings = syncer.settings()
    assert (settings.remote, settings.name, settings.started is not None) == (PORT_443_URL, "Newer Name", True)
    assert plain_git("--git-dir", git_dir, "rev-parse", "refs/heads/main") == head  # history kept
    save_flow(tmp_path, "user:later", "# Later\n")
    assert syncer.run()["status"] == "synced" and "common/later.md" in remote_files(bare)


@needs_ssh_keygen
def test_switching_repository_before_start_moves_the_key_only_when_asked(fake, tmp_path):
    state = tmp_path / "state"
    syncer = make_syncer(tmp_path, signed_in(fake, state))
    script_repository(fake)
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    other = "octocat/other-library"
    fake.reply("GET", f"/api/v3/repos/{other}", 200, repo(other), repeat=True)
    fake.reply("GET", f"/api/v3/repos/{other}/branches", 200, [], repeat=True)
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(state, 11)])
    with pytest.raises(SyncError) as in_use:
        syncer.setup_github(other, name="Owner", email="owner@example.com", label="laptop")
    assert in_use.value.reason == "key_in_use" and in_use.value.details["previous"] == REPOSITORY
    assert syncer.settings().remote == SSH_URL and key_requests(fake, other) == []  # nothing moved
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(state, 11)])
    fake.reply("DELETE", f"/api/v3/repos/{REPOSITORY}/keys/11", 204)
    fake.reply("GET", f"/api/v3/repos/{other}/keys", 200, [])
    fake.reply("POST", f"/api/v3/repos/{other}/keys", 201,
               {"id": 21, "title": "Agents-Core laptop", "key": "ssh-ed25519 AAAA", "read_only": False})
    moved = syncer.setup_github(other, name="Owner", email="owner@example.com", label="laptop", move_key=True)
    assert f"removed this machine's deploy key from {REPOSITORY}" in moved["steps"]
    assert ("DELETE", "/11") in key_requests(fake) and ("POST", "/") in key_requests(fake, other)
    settings = syncer.settings()
    assert (settings.remote, settings.github_repository, settings.deploy_key_id) == (
        f"git@github.com:{other}.git", other, 21)


@needs_ssh_keygen
def test_a_read_only_deploy_key_is_replaced_with_a_write_key(fake, tmp_path):
    state = tmp_path / "state"
    syncer = make_syncer(tmp_path, signed_in(fake, state))
    keys.ensure_key(state, "laptop")
    script_repository(fake, [deploy_key(state, 7, read_only=True)])
    fake.reply("DELETE", f"/api/v3/repos/{REPOSITORY}/keys/7", 204)
    result = syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    assert result["deploy_key"] == "replaced" and "read-only" in result["steps"][-1]
    assert key_requests(fake) == [("GET", "/"), ("DELETE", "/7"), ("POST", "/")]
    assert posted_keys(fake)[0].json["read_only"] is False and syncer.settings().deploy_key_id == 11


def test_a_repository_of_another_owner_needs_the_owner_confirmed(fake, tmp_path):
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"))
    team = "acme/library"
    fake.reply("GET", f"/api/v3/repos/{team}", 200, repo(team), repeat=True)
    with pytest.raises(SyncError) as unconfirmed:
        syncer.setup_github(team, name="Owner", email="owner@example.com", label="laptop")
    assert unconfirmed.value.reason == "owner_unconfirmed" and "--confirm-owner acme" in unconfirmed.value.message
    assert unconfirmed.value.details == {"owner": "acme", "repository": team}
    with pytest.raises(SyncError) as wrong:
        syncer.setup_github(team, name="Owner", email="owner@example.com", label="laptop", confirm_owner="octocat")
    assert wrong.value.reason == "owner_unconfirmed"
    assert key_requests(fake, team) == [] and syncer.settings() is None


@needs_ssh_keygen
def test_a_confirmed_owner_is_accepted(fake, tmp_path):
    syncer = make_syncer(tmp_path, signed_in(fake, tmp_path / "state"))
    team = "acme/library"
    fake.reply("GET", f"/api/v3/repos/{team}", 200, repo(team), repeat=True)
    fake.reply("GET", f"/api/v3/repos/{team}/branches", 200, [], repeat=True)
    fake.reply("GET", f"/api/v3/repos/{team}/keys", 200, [])
    fake.reply("POST", f"/api/v3/repos/{team}/keys", 201,
               {"id": 31, "title": "Agents-Core laptop", "key": "ssh-ed25519 AAAA", "read_only": False})
    result = syncer.setup_github(team, name="Owner", email="owner@example.com", label="laptop", confirm_owner="ACME")
    assert result["repository"] == team and result["deploy_key"] == "added"


def test_the_wizard_defaults_to_a_found_library_and_asks_before_using_another_owners_repository(fake, tmp_path):
    client = signed_in(fake, tmp_path / "state").client()
    fake.reply("GET", "/api/v3/user/repos", 200, [repo(REPOSITORY)])
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/contents/.agents-library.json", 200, {"type": "file"})
    ask = Script("")
    assert wizard._choose_repository(client, "octocat", ask, lambda text: None) == (REPOSITORY, None)
    assert ask.prompts == ["Choose a number [2]: "]
    fake.reply("GET", "/api/v3/user/repos", 200, [])
    fake.reply("GET", "/api/v3/repos/acme/library", 200, repo("acme/library"), repeat=True)
    said = []
    ask = Script("2", "acme/library", "", "2", "acme/library", "acme")
    assert wizard._choose_repository(client, "octocat", ask, said.append) == ("acme/library", "acme")
    assert ask.prompts[0] == "Choose a number [1]: " and ask.answers == []
    assert any("belongs to acme, not to you" in line for line in said)


def test_a_privacy_confirmation_covers_one_repository(tmp_path, bare):
    other = tmp_path / "other.git"
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(other))
    syncer = Syncer(tmp_path / "library", tmp_path / "state", allow_file_remote=True,
                    visibility=lambda remote: "unknown")
    save_flow(tmp_path)
    syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop", confirm_private=True)
    syncer.setup(remote=str(bare), name="Owner", email="owner@example.com", label="laptop")
    assert syncer.settings().private_confirmed is True  # the same repository keeps it
    syncer.setup(remote=str(other), name="Owner", email="owner@example.com", label="laptop")
    assert syncer.settings().private_confirmed is False
    assert syncer.start(syncer.preview()["hash"])["reason"] == "public_repo" and remote_files(other) == {}


def test_the_recorded_repository_and_a_host_with_a_port_find_the_repository(fake, tmp_path):
    remote = parse_remote("git@github.example.com:octocat/agents-library.git")
    assert engine_module.hosted_repository(remote, "github.example.com:8443") == "octocat/agents-library"
    state = tmp_path / "state"
    account = GitHubAccount(state, "agents-core-sync-test", host="ghe.example.com", web_url=fake.url,
                            api_url=fake.api, client_id="Iv1.test", store=MemoryStore())
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    account.complete_sign_in(TOKEN)
    syncer = make_syncer(tmp_path, account)
    syncer.anonymous_visibility = lambda remote: "anonymous"
    ssh_url = "git@ssh.ghe.example.com:team/library.git"  # GHES with SSH on another host name
    engine_module.Settings(remote=ssh_url, name="Owner", email="owner@example.com", label="laptop",
                           github_repository="team/library").save(syncer.settings_path)
    fake.reply("GET", "/api/v3/repos/team/library", 200, repo("team/library"))
    assert syncer.default_visibility(parse_remote(ssh_url)) == "private"


@needs_ssh_keygen
def test_an_offline_run_on_port_22_names_the_check_that_moves_to_port_443(tmp_path, bare):
    transport = Transport(tmp_path, bare, refuse={"github.com": BLOCKED_22})
    syncer = make_syncer(tmp_path, None, ssh_command=transport.command, visibility=lambda remote: "private")
    syncer.setup(remote=SSH_URL, name="Owner", email="owner@example.com", label="laptop")
    settings = syncer.settings()
    settings.started = "2026-10-05T11:00:00+00:00"
    settings.save(syncer.settings_path)
    result = syncer.run()
    assert result["status"] == "offline" and "python -m src.user_sync check" in result["message"]


def test_cli_passes_the_privacy_owner_and_key_choices_to_setup_github(monkeypatch, tmp_path):
    seen = {}

    def setup_github(self, repository, **options):
        seen.update(options, repository=repository)
        return {"status": "waiting_for_access"}

    monkeypatch.setattr(engine_module.Syncer, "setup_github", setup_github)
    assert run_cli(tmp_path, "setup", "--github", "acme/library", "--name", "O", "--email", "o@x.y",
                   "--confirm-private", "--confirm-owner", "acme", "--move-key") == 0
    assert (seen["repository"], seen["confirm_private"], seen["confirm_owner"], seen["move_key"]) == (
        "acme/library", True, "acme", True)


@pytest.mark.parametrize("returned, code", [
    ({"status": "offline", "reason": "network", "message": "git push: connection timed out"}, 1),
    ({"status": "lock_held", "message": "another sync of this library is running"}, 1),
    ({"status": "synced", "sent": []}, 0),
    ({"status": "already_set_up", "message": "sync is already set up; nothing changed"}, 0),
])
def test_cli_wizard_exits_with_1_when_its_start_did_not_finish(monkeypatch, tmp_path, capsys, returned, code):
    monkeypatch.setattr(wizard, "run", lambda syncer, **options: dict(returned))
    assert run_cli(tmp_path, "setup", "--json", interactive=True) == code
    result = json.loads(capsys.readouterr().out)
    if code:
        assert result["message"].startswith(f"sync did not start ({returned['status']})")


def test_cli_json_sends_the_sign_in_code_and_the_prompts_to_stderr(fake, tmp_path, cli_account, monkeypatch, capsys):
    script_device_flow(fake)
    assert run_cli(tmp_path, "github", "login", "--json", open_browser=lambda url: None,
                   sleep=lambda seconds: None) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["login"] == "octocat"
    assert "WDJB-MJHT" in out.err and "WDJB-MJHT" not in out.out
    answers = iter(["m", "git@git.example.com:me/library.git"])

    def scripted_input(prompt=""):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", scripted_input)
    assert run_cli(tmp_path / "wizard", "setup", "--json", interactive=True) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["status"] == "cancelled"
    assert "Where is the library's repository?" in out.err and "SSH URL of the repository" in out.err
    assert "Your name for commits" in out.err and "Where is" not in out.out


def test_a_browser_opened_during_json_login_cannot_write_into_the_json(fake, tmp_path, cli_account, capfd):
    script_device_flow(fake)

    def chatty_browser(url):
        os.write(1, b"browser output on its inherited stdout\n")
        return True

    assert run_cli(tmp_path, "github", "login", "--json", open_browser=chatty_browser,
                   sleep=lambda seconds: None) == 0
    out = capfd.readouterr()
    assert json.loads(out.out)["login"] == "octocat"
    assert "browser output" in out.err


def test_cli_login_never_hands_the_link_to_a_console_browser(fake, tmp_path, cli_account, monkeypatch, capsys):
    script_device_flow(fake)
    monkeypatch.setenv("DISPLAY", ":0")
    lynx = wizard.webbrowser.GenericBrowser("lynx")
    started = []
    monkeypatch.setattr(lynx, "open", lambda url, *args, **kwargs: started.append(url) or True)
    monkeypatch.setattr(wizard.webbrowser, "get", lambda *args: lynx)
    monkeypatch.setattr(wizard.webbrowser, "open", lambda url, *args, **kwargs: started.append(url) or True)
    assert run_cli(tmp_path, "github", "login", "--json", sleep=lambda seconds: None) == 0
    assert json.loads(capsys.readouterr().out)["login"] == "octocat" and started == []


@pytest.mark.parametrize("platform", ["linux", "freebsd14"])
def test_no_browser_opens_without_a_display(monkeypatch, platform):
    monkeypatch.setattr(wizard.sys, "platform", platform)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    def never(*args, **kwargs):
        raise AssertionError("webbrowser was asked although there is no display")

    monkeypatch.setattr(wizard.webbrowser, "get", never)
    assert wizard.open_in_browser("https://github.com/login/device") is False


@pytest.mark.parametrize("console", [
    lambda: wizard.webbrowser.GenericBrowser("lynx"),
    lambda: type("Elinks", (wizard.webbrowser.UnixBrowser,), {"background": False})("elinks"),
], ids=["generic", "unix-foreground"])
def test_a_console_browser_is_never_started(monkeypatch, console):
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    browser = console()
    opened = []
    monkeypatch.setattr(browser, "open", lambda url, *args, **kwargs: opened.append(url) or True)
    monkeypatch.setattr(wizard.webbrowser, "get", lambda *args: browser)
    assert wizard.open_in_browser("https://github.com/login/device") is False and opened == []


def test_a_graphical_browser_is_used(monkeypatch):
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    browser = wizard.webbrowser.BackgroundBrowser("firefox")
    opened = []
    monkeypatch.setattr(browser, "open", lambda url, *args, **kwargs: opened.append(url) or True)
    monkeypatch.setattr(wizard.webbrowser, "get", lambda *args: browser)
    assert wizard.open_in_browser("https://github.com/login/device") is True
    assert opened == ["https://github.com/login/device"]


@needs_ssh_keygen
def test_the_wizard_never_takes_the_identity_from_the_users_git_configuration(tmp_path, bare, monkeypatch):
    hostile = tmp_path / "hostile.gitconfig"
    hostile.write_text("[user]\n\tname = Work Account\n\temail = work@corp.example\n")
    home = tmp_path / "home"
    home.mkdir()
    shutil.copy(hostile, home / ".gitconfig")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Work Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@corp.example")
    monkeypatch.setenv("EMAIL", "env@corp.example")
    transport = Transport(tmp_path, bare)
    syncer = make_syncer(tmp_path, None, ssh_command=transport.command,
                         scan_host_keys=lambda host, port: [HOST_KEY], visibility=lambda remote: "private")
    save_flow(tmp_path)
    ask = Script("m", "git@git.example.com:me/library.git", "Owner", "owner@example.com", "laptop",
                 keys.fingerprint(HOST_KEY), "", "yes")
    result = wizard.run(syncer, ask=ask, say=lambda text: None, open_browser=lambda url: None,
                        sleep=lambda seconds: None)
    assert result["status"] == "synced" and ask.answers == []
    assert not [prompt for prompt in ask.prompts if "Work" in prompt or "corp" in prompt]  # no defaults from git
    assert plain_git("--git-dir", str(bare), "log", "-1", "--format=%an <%ae>|%cn <%ce>", "main") == \
        "Owner <owner@example.com>|Owner <owner@example.com>"


# --- a new key for this machine (#170) ----------------------------------------------------------


@needs_ssh_keygen
def test_a_new_key_is_added_on_github_before_the_old_one_is_removed(fake, tmp_path):
    state = tmp_path / "state"
    syncer = make_syncer(tmp_path, signed_in(fake, state))
    script_repository(fake)
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    old = keys.public_key(state)
    fake.reply("POST", f"/api/v3/repos/{REPOSITORY}/keys", 201,
               {"id": 12, "title": "Agents-Core laptop", "key": "ssh-ed25519 AAAA", "read_only": False})
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 200, [deploy_key(state, key_id=11)])  # the old key
    fake.reply("DELETE", f"/api/v3/repos/{REPOSITORY}/keys/11", 204, None)
    result = syncer.regenerate_key()
    new = keys.public_key(state)
    assert result == {"status": "replaced", "public_key": new, "deploy_key": "added", "repository": REPOSITORY,
                      "old_key_removed": True,
                      "message": f"added the new key to {REPOSITORY} and removed the old one"}
    assert new != old and new.endswith(" agents-core-sync:laptop")
    added = posted_keys(fake)[-1]
    assert added.json["key"] == " ".join(new.split()[:2]) and added.json["read_only"] is False
    methods = [(request.method, request.path) for request in fake.requests[-3:]]
    assert methods == [("POST", f"/api/v3/repos/{REPOSITORY}/keys"), ("GET", f"/api/v3/repos/{REPOSITORY}/keys"),
                       ("DELETE", f"/api/v3/repos/{REPOSITORY}/keys/11")]


@needs_ssh_keygen
def test_a_new_key_that_github_refuses_leaves_the_old_one(fake, tmp_path):
    state = tmp_path / "state"
    account = signed_in(fake, state)
    syncer = make_syncer(tmp_path, account)
    script_repository(fake)
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    old, old_private = keys.public_key(state), keys.key_path(state).read_bytes()
    fake.reply("POST", f"/api/v3/repos/{REPOSITORY}/keys", 403, {"message": "Resource not accessible"})
    with pytest.raises(GitHubError) as refused:
        syncer.regenerate_key()
    assert refused.value.code == "forbidden"
    assert keys.public_key(state) == old and keys.key_path(state).read_bytes() == old_private
    assert sorted(path.name for path in state.glob("id_ed25519*")) == ["id_ed25519", "id_ed25519.pub"]
    # The old key's removal may fail: the new key works, and the answer says what is left to do.
    fake.reply("POST", f"/api/v3/repos/{REPOSITORY}/keys", 201,
               {"id": 12, "title": "Agents-Core laptop", "key": "ssh-ed25519 AAAA", "read_only": False})
    fake.reply("GET", f"/api/v3/repos/{REPOSITORY}/keys", 502, None)
    result = syncer.regenerate_key()
    assert result["old_key_removed"] is False and "could not be removed" in result["message"]
    assert keys.public_key(state) != old
    account.mark_reconnect_needed()  # GitHub refused the token: no new key that only GitHub could make work
    with pytest.raises(SyncError) as reconnect:
        syncer.regenerate_key()
    assert reconnect.value.reason == "reconnect_needed"


KEYS = f"/api/v3/repos/{REPOSITORY}/keys"
NEW_KEY = {"id": 12, "title": "Agents-Core laptop", "key": "ssh-ed25519 AAAA", "read_only": False}


def key_files(state: Path) -> list[str]:
    return sorted(path.name for path in state.glob("id_ed25519*"))


def set_up_on_github(fake, tmp_path):
    state = tmp_path / "state"
    account = signed_in(fake, state)
    syncer = make_syncer(tmp_path, account)
    script_repository(fake)
    syncer.setup_github(REPOSITORY, name="Owner", email="owner@example.com", label="laptop")
    return syncer, account, state


@needs_ssh_keygen
def test_a_new_key_for_a_github_repository_needs_the_account_signed_in(fake, tmp_path):
    syncer, account, state = set_up_on_github(fake, tmp_path)
    old = keys.public_key(state)
    account.forget()  # Forget account: a key made now could not become a deploy key
    asked = len(fake.requests)
    with pytest.raises(SyncError) as refused:
        syncer.regenerate_key()
    assert refused.value.reason == "not_signed_in" and REPOSITORY in refused.value.message
    assert keys.public_key(state) == old and len(fake.requests) == asked and key_files(state) == ["id_ed25519",
                                                                                                    "id_ed25519.pub"]


@needs_ssh_keygen
def test_a_new_key_that_cannot_be_installed_is_taken_off_github_again(fake, tmp_path, monkeypatch):
    syncer, _, state = set_up_on_github(fake, tmp_path)
    old, old_private = keys.public_key(state), keys.key_path(state).read_bytes()

    def refuse(private, state_dir, public, **options):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(engine_module.keys, "install_key", refuse)
    fake.reply("POST", KEYS, 201, NEW_KEY)
    fake.reply("DELETE", f"{KEYS}/12", 204, None)
    with pytest.raises(SyncError) as failed:
        syncer.regenerate_key()
    assert failed.value.reason == "ssh" and "could not be installed (Permission denied)" in failed.value.message
    assert [request.path for request in fake.requests if request.method == "DELETE"] == [f"{KEYS}/12"]
    assert keys.public_key(state) == old and keys.key_path(state).read_bytes() == old_private
    assert key_files(state) == ["id_ed25519", "id_ed25519.pub"]
    # When GitHub does not take it back, the error names the key left there.
    fake.reply("POST", KEYS, 201, {**NEW_KEY, "id": 13})
    fake.reply("DELETE", f"{KEYS}/13", 502, None)
    with pytest.raises(SyncError) as left:
        syncer.regenerate_key()
    assert f"the new deploy key 13 (Agents-Core laptop) could not be removed from {REPOSITORY}" in left.value.message
    assert key_files(state) == ["id_ed25519", "id_ed25519.pub"]


def derived(state: Path) -> str:
    """``type base64`` of the public key that ssh derives from this machine's private key."""
    return " ".join(keys.derive_public(keys.key_path(state)).split()[:2])


def material(line: str | None) -> str | None:
    return " ".join(line.split()[:2]) if line else None


@needs_ssh_keygen
def test_a_new_private_key_in_place_rolls_forward_and_is_never_discarded(fake, tmp_path, monkeypatch):
    syncer, _, state = set_up_on_github(fake, tmp_path)
    old, old_private = keys.public_key(state), keys.key_path(state).read_bytes()
    real_replace = keys.os.replace

    def replace(source, target):  # the public half cannot move: it is written from the public line
        if str(source).endswith(".pub") and ".new-" in str(source):
            raise OSError(5, "Input/output error")
        return real_replace(source, target)
    monkeypatch.setattr(keys.os, "replace", replace)
    fake.reply("POST", KEYS, 201, NEW_KEY)
    fake.reply("GET", KEYS, 200, [deploy_key(state, key_id=11)])
    fake.reply("DELETE", f"{KEYS}/11", 204, None)
    result = syncer.regenerate_key()
    assert result["status"] == "replaced" and result["old_key_removed"] is True
    assert keys.public_key(state) == result["public_key"] != old
    assert keys.key_path(state).read_bytes() != old_private and key_files(state) == ["id_ed25519", "id_ed25519.pub"]
    assert syncer.settings().deploy_key_id == 12


@needs_ssh_keygen
def test_a_public_file_that_cannot_be_written_leaves_the_new_key_working_and_recorded(fake, tmp_path, monkeypatch):
    syncer, _, state = set_up_on_github(fake, tmp_path)
    old = keys.public_key(state)
    listed_old = deploy_key(state, key_id=11)
    real_replace = keys.os.replace

    def replace(source, target):
        if str(source).endswith(".pub") and ".new-" in str(source):
            raise OSError(5, "Input/output error")
        return real_replace(source, target)
    monkeypatch.setattr(keys.os, "replace", replace)

    def no_space(path, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(keys, "write_private", no_space)
    fake.reply("POST", KEYS, 201, {**NEW_KEY, "id": 14})
    fake.reply("GET", KEYS, 200, [listed_old, {**NEW_KEY, "id": 14}])
    fake.reply("DELETE", f"{KEYS}/11", 204, None)
    with pytest.raises(SyncError) as half:
        syncer.regenerate_key()
    assert half.value.reason == "ssh"
    for part in (f"the new key is in place and {REPOSITORY} has it", "No space left on device",
                 "the old deploy key is removed", "Regenerate the key again"):
        assert part in half.value.message, part
    # The old public file is gone, so ssh derives the new one from the private key; no leftover stays.
    assert key_files(state) == ["id_ed25519"] and derived(state) != material(old)
    assert syncer.settings().deploy_key_id == 14
    assert [r.path for r in fake.requests if r.method == "DELETE"] == [f"{KEYS}/11"]
    # The next new key removes deploy key 14, found by its recorded id and by the derived key.
    monkeypatch.undo()
    fake.reply("POST", KEYS, 201, {**NEW_KEY, "id": 15})
    fake.reply("GET", KEYS, 200, [{**NEW_KEY, "id": 14, "key": derived(state)}, {**NEW_KEY, "id": 15}])
    fake.reply("DELETE", f"{KEYS}/14", 204, None)
    again = syncer.regenerate_key()
    assert again["old_key_removed"] is True and key_files(state) == ["id_ed25519", "id_ed25519.pub"]
    assert [r.path for r in fake.requests if r.method == "DELETE"] == [f"{KEYS}/11", f"{KEYS}/14"]
    assert material(keys.public_key(state)) == derived(state) and syncer.settings().deploy_key_id == 15


@needs_ssh_keygen
def test_a_private_key_that_cannot_move_changes_nothing_and_github_hears_first(fake, tmp_path, monkeypatch):
    syncer, _, state = set_up_on_github(fake, tmp_path)
    old, old_private = keys.public_key(state), keys.key_path(state).read_bytes()
    real_replace, real_discard = keys.os.replace, keys.discard_key
    events = []

    def replace(source, target):
        if ".new-" in str(source) and not str(source).endswith(".pub"):
            raise PermissionError(13, "Permission denied")
        return real_replace(source, target)

    def discard(private):
        events.append("discard")
        if len(events) > 1:  # the first is generate_key's own: the undo cannot delete the new pair
            raise PermissionError(13, "Permission denied")
        return real_discard(private)

    def delete(self, repository, key_id):
        events.append(f"delete {key_id}")
        return real_delete(self, repository, key_id)
    real_delete = GitHubClient.delete_deploy_key
    monkeypatch.setattr(keys.os, "replace", replace)
    monkeypatch.setattr(engine_module.keys, "discard_key", discard)
    monkeypatch.setattr(GitHubClient, "delete_deploy_key", delete)
    fake.reply("POST", KEYS, 201, NEW_KEY)
    fake.reply("DELETE", f"{KEYS}/12", 204, None)
    with pytest.raises(SyncError) as failed:
        syncer.regenerate_key()
    assert failed.value.reason == "ssh" and "Permission denied" in failed.value.message
    assert events == ["discard", "delete 12", "discard"]  # GitHub first: a local failure leaves it right
    assert keys.public_key(state) == old and keys.key_path(state).read_bytes() == old_private
    assert syncer.settings().deploy_key_id == 11


@needs_ssh_keygen
def test_an_interrupt_after_the_new_private_key_moved_keeps_it(fake, tmp_path, monkeypatch):
    syncer, _, state = set_up_on_github(fake, tmp_path)
    old = keys.public_key(state)
    real_replace = keys.os.replace
    interrupted = []

    def replace(source, target):  # Ctrl-C between the private key's move and the public half's
        if str(source).endswith(".pub") and ".new-" in str(source) and not interrupted:
            interrupted.append(source)
            raise KeyboardInterrupt()
        return real_replace(source, target)
    monkeypatch.setattr(keys.os, "replace", replace)
    fake.reply("POST", KEYS, 201, {**NEW_KEY, "id": 16})
    asked = len(fake.requests)
    with pytest.raises(KeyboardInterrupt):
        syncer.regenerate_key()
    assert [r.method for r in fake.requests[asked:]] == ["POST"]  # nothing taken off GitHub, nothing looked up
    assert derived(state) != material(old) and material(keys.public_key(state)) == derived(state)
    assert key_files(state) == ["id_ed25519", "id_ed25519.pub"] and syncer.settings().deploy_key_id == 16


@needs_ssh_keygen
def test_an_interrupt_before_the_new_private_key_moved_takes_its_deploy_key_off_github(fake, tmp_path, monkeypatch):
    syncer, _, state = set_up_on_github(fake, tmp_path)
    old, old_private = keys.public_key(state), keys.key_path(state).read_bytes()
    real_replace = keys.os.replace

    def replace(source, target):
        if ".new-" in str(source) and not str(source).endswith(".pub"):
            raise KeyboardInterrupt()
        return real_replace(source, target)
    monkeypatch.setattr(keys.os, "replace", replace)
    fake.reply("POST", KEYS, 201, {**NEW_KEY, "id": 17})
    fake.reply("DELETE", f"{KEYS}/17", 204, None)
    with pytest.raises(KeyboardInterrupt):
        syncer.regenerate_key()
    assert [r.path for r in fake.requests if r.method == "DELETE"] == [f"{KEYS}/17"]
    assert keys.public_key(state) == old and keys.key_path(state).read_bytes() == old_private
    assert key_files(state) == ["id_ed25519", "id_ed25519.pub"] and syncer.settings().deploy_key_id == 11


@needs_ssh_keygen
def test_another_sync_holding_the_lock_takes_the_new_deploy_key_off_github(fake, tmp_path):
    from src.file_lock import file_lock
    syncer, _, state = set_up_on_github(fake, tmp_path)
    old = keys.public_key(state)
    fake.reply("POST", KEYS, 201, {**NEW_KEY, "id": 17})
    fake.reply("DELETE", f"{KEYS}/17", 204, None)
    with file_lock(syncer.library / ".git" / engine_module.SYNC_LOCK, blocking=False):
        with pytest.raises(SyncError) as busy:
            syncer.regenerate_key()
    assert (busy.value.reason, busy.value.state) == ("lock_held", "busy") and keys.public_key(state) == old
    assert [r.path for r in fake.requests if r.method == "DELETE"] == [f"{KEYS}/17"]
    assert key_files(state) == ["id_ed25519", "id_ed25519.pub"]


@needs_ssh_keygen
def test_setup_derives_a_missing_public_file_instead_of_making_a_new_key(fake, tmp_path):
    syncer, _, state = set_up_on_github(fake, tmp_path)
    old, old_private = keys.public_key(state), keys.key_path(state).read_bytes()
    keys.key_path(state).with_name("id_ed25519.pub").unlink()
    result = syncer.setup(remote=SSH_URL, name="Owner", email="owner@example.com", label="laptop")
    assert keys.key_path(state).read_bytes() == old_private  # still the key GitHub has
    assert result["public_key"] == keys.public_key(state) and material(result["public_key"]) == material(old)
    assert syncer.settings().deploy_key_id == 11
    syncer.setup(remote="git@github.com:octocat/other.git", name="Owner", email="owner@example.com", label="laptop")
    assert syncer.settings().deploy_key_id is None  # a deploy key of another repository


@needs_ssh_keygen
def test_github_hears_of_the_new_key_before_and_of_the_old_one_after_the_sync_lock(fake, tmp_path, monkeypatch):
    syncer, _, state = set_up_on_github(fake, tmp_path)
    seen = []
    real_add, real_list, real_install = GitHubClient.add_deploy_key, GitHubClient.deploy_keys, keys.install_key

    def add(self, *arguments):
        seen.append(("add", syncer._lock_busy()))
        return real_add(self, *arguments)

    def listed(self, *arguments):
        seen.append(("list", syncer._lock_busy()))
        return real_list(self, *arguments)

    def install(*arguments, **options):
        seen.append(("install", syncer._lock_busy()))
        return real_install(*arguments, **options)
    monkeypatch.setattr(GitHubClient, "add_deploy_key", add)
    monkeypatch.setattr(GitHubClient, "deploy_keys", listed)
    monkeypatch.setattr(engine_module.keys, "install_key", install)
    fake.reply("POST", KEYS, 201, NEW_KEY)
    fake.reply("GET", KEYS, 200, [deploy_key(state, key_id=11)])
    fake.reply("DELETE", f"{KEYS}/11", 204, None)
    syncer.regenerate_key()
    assert seen == [("add", False), ("install", True), ("list", False)]


@needs_ssh_keygen
def test_cli_changes_the_identity_and_makes_a_new_key(fake, tmp_path, cli_account, capsys):
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    cli_account.complete_sign_in(TOKEN)
    script_repository(fake)
    assert run_cli(tmp_path, "setup", "--github", REPOSITORY, "--name", "Owner", "--email", "owner@example.com",
                   "--label", "laptop") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "configure", "--name", "New Owner", "--label", "desk", "--json") == 0
    configured = json.loads(capsys.readouterr().out)
    assert configured["identity"] == {"name": "New Owner", "email": "owner@example.com"}
    assert configured["label"] == "desk"
    assert run_cli(tmp_path, "configure", "--email", "not an email", "--json") == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "identity"
    old = keys.public_key(tmp_path / "state")
    fake.reply("POST", KEYS, 201, NEW_KEY)
    fake.reply("GET", KEYS, 200, [deploy_key(tmp_path / "state", key_id=11)])
    fake.reply("DELETE", f"{KEYS}/11", 204, None)
    assert run_cli(tmp_path, "github", "regenerate-key", "--json") == 0
    replaced = json.loads(capsys.readouterr().out)
    assert replaced["status"] == "replaced" and replaced["old_key_removed"] is True
    assert replaced["public_key"] == keys.public_key(tmp_path / "state") != old
    fake.reply("POST", KEYS, 201, {**NEW_KEY, "id": 13})
    fake.reply("GET", KEYS, 200, [deploy_key(tmp_path / "state", key_id=12)])
    fake.reply("DELETE", f"{KEYS}/12", 204, None)
    assert run_cli(tmp_path, "github", "regenerate-key") == 0
    out = capsys.readouterr().out
    assert "github regenerate-key: replaced" in out and "public key: ssh-ed25519 " in out
