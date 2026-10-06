"""GitHub account for library sync (src/user_sync/github.py) against a fake GitHub on 127.0.0.1.

Standard library and pytest only, so that the file runs on Linux, Windows and macOS.
The real OS secret stores are used only with AGENTS_TEST_REAL_SECRET_STORE=1, which
is meant for CI jobs: on a developer machine they would write to the real keychain.
Every other test injects a store or simulates the store's tool, and
``no_real_secret_store`` fails a test that would reach a real one.
"""
from __future__ import annotations

import base64
import ctypes
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

import pytest

from src.user_sync import github

TOKEN = "gho_Q8xk2LmZ7vNw4Rt1Ys9Bc3Df6Gh0Jp5Ke"
OLD_TOKEN = "gho_previous_token_value"
DEVICE_CODE = "3584d83530557fdd1f46af8289938c8ef79f9dc5"
SECRETS = (TOKEN, TOKEN.encode().hex(), DEVICE_CODE)
FRAGMENT = 8  # any piece of a secret this long counts as a leak
REAL_RUN = subprocess.run
REAL_DEFAULT_STORE = github.default_store
REAL_CREDENTIAL_API = github._CredentialApi


@pytest.fixture(autouse=True)
def no_real_secret_store(monkeypatch):
    """Fail instead of reaching this machine's keychain, Secret Service or Credential Manager."""
    def guarded_run(argv, *args, **kwargs):
        if Path(str(argv[0])).name in ("security", "secret-tool"):
            raise AssertionError(f"a test reached the real secret store: {argv[:2]}")
        return REAL_RUN(argv, *args, **kwargs)

    def no_default_store(*args, **kwargs):
        raise AssertionError("inject a store: the default one is this machine's real secret store")

    def no_windows_api():
        raise AssertionError("a test reached the real Windows Credential Manager")

    monkeypatch.setattr(github.subprocess, "run", guarded_run)
    monkeypatch.setattr(github, "default_store", no_default_store)
    monkeypatch.setattr(github, "_CredentialApi", no_windows_api)


def assert_secret_free(*texts: str) -> None:
    """No secret, nor any piece of one of FRAGMENT characters or more, appears in ``texts``."""
    pieces = {secret[start:start + FRAGMENT] for secret in SECRETS for start in range(len(secret) - FRAGMENT + 1)}
    for text in texts:
        leaked = sorted(piece for piece in pieces if piece in text)
        assert not leaked, f"secret pieces leaked: {leaked}"


# --- a fake GitHub -----------------------------------------------------------------

class Recorded:
    def __init__(self, method, path, query, headers, body):
        self.method, self.path, self.query, self.headers, self.body = method, path, query, headers, body

    @property
    def json(self):
        return json.loads(self.body)

    @property
    def form(self):
        return dict(urllib.parse.parse_qsl(self.body.decode()))


class FakeGitHub:
    """A scripted GitHub on 127.0.0.1: ``reply`` queues answers per method and path; requests are recorded."""

    def __init__(self):
        self.replies: dict = {}
        self.requests: list[Recorded] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urllib.parse.urlsplit(self.path)
                fake.requests.append(Recorded(self.command, parts.path, urllib.parse.parse_qs(parts.query),
                                              {key.lower(): value for key, value in self.headers.items()}, body))
                status, headers, payload = fake.answer(self.command, parts.path)
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = serve

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A short poll interval lets shutdown() return at once instead of after 0.5 s.
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                                       daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.api = self.url + "/api/v3"

    def reply(self, method, path, status=200, payload=None, headers=None, *, repeat=False):
        self.replies.setdefault((method, path), []).append((status, headers or {}, payload, repeat))

    def answer(self, method, path):
        queue = self.replies.get((method, path))
        if not queue:
            return 404, {}, {"message": f"unscripted {method} {path}"}
        status, headers, payload, repeat = queue[0]
        if not repeat:
            queue.pop(0)
        return status, headers, payload

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    server = FakeGitHub()
    yield server
    server.close()


def client(fake, **kwargs) -> github.GitHubClient:
    return github.GitHubClient(TOKEN, api_url=fake.api, **kwargs)


def repo(full_name, private=True, visibility=None):
    return {"full_name": full_name, "name": full_name.split("/")[1], "private": private,
            "visibility": visibility or ("private" if private else "public"),
            "ssh_url": f"git@github.com:{full_name}.git", "html_url": f"https://github.com/{full_name}",
            "default_branch": "main", "archived": False}


def public_key(seed: int = 1, comment: str = "agents-core-sync:laptop") -> str:
    blob = (11).to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + bytes([seed]) * 32
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".strip()


def key_material(line: str) -> str:
    return " ".join(line.split()[:2])


# --- hosts ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, web, api", [
    (None, "https://github.com", "https://api.github.com"),
    ("github.example.com", "https://github.example.com", "https://github.example.com/api/v3"),
    ("https://GitHub.Example.com:8443/", "https://github.example.com:8443", "https://github.example.com:8443/api/v3"),
    ("github.com:443", "https://github.com", "https://api.github.com"),
    ("acme.ghe.com", "https://acme.ghe.com", "https://api.acme.ghe.com"),
])
def test_the_host_selects_the_web_and_api_bases(monkeypatch, value, web, api):
    if value is None:
        monkeypatch.delenv("AGENTS_GITHUB_HOST", raising=False)
    else:
        monkeypatch.setenv("AGENTS_GITHUB_HOST", value)
    host = github.github_host()
    assert (github.web_base(host), github.api_base(host)) == (web, api)


@pytest.mark.parametrize("value", ["http://github.example.com", "github.example.com/path", "a b", "-x"])
def test_an_invalid_host_is_a_config_error(monkeypatch, value):
    monkeypatch.setenv("AGENTS_GITHUB_HOST", value)
    with pytest.raises(github.GitHubError) as caught:
        github.github_host()
    assert caught.value.code == "config"


def test_the_client_id_variable_overrides_the_default(monkeypatch):
    monkeypatch.setenv("AGENTS_GITHUB_CLIENT_ID", "Iv1.fork")
    assert github.default_client_id() == "Iv1.fork"


@pytest.mark.parametrize("url", ["http://github.example.com", "https://user@github.example.com", "ftp://x",
                                 "https://[::1"])
def test_base_urls_must_be_https_without_credentials(url):
    with pytest.raises(github.GitHubError) as caught:
        github.GitHubClient(TOKEN, api_url=url)
    assert caught.value.code == "config"


# --- device flow ---------------------------------------------------------------------

def script_device_code(fake, interval=5, expires_in=900):
    fake.reply("POST", "/login/device/code", 200, {
        "device_code": DEVICE_CODE, "user_code": "WDJB-MJHT", "verification_uri": "https://github.com/login/device",
        "expires_in": expires_in, "interval": interval})


def script_polls(fake, *answers):
    for answer in answers:
        fake.reply("POST", "/login/oauth/access_token", 200, answer)


PENDING = {"error": "authorization_pending", "error_description": "The authorization request is still pending."}
SLOW_DOWN = {"error": "slow_down", "error_description": "Too many requests have been made in the same timeframe."}
GRANTED = {"access_token": TOKEN, "token_type": "bearer", "scope": "repo"}


def test_device_flow_goes_from_pending_through_slow_down_to_a_token(fake):
    script_device_code(fake)
    script_polls(fake, PENDING, SLOW_DOWN, GRANTED)
    flow = github.DeviceFlow("Iv1.test", web_url=fake.url)
    device = flow.start()
    assert device.public() == {"user_code": "WDJB-MJHT", "verification_uri": "https://github.com/login/device",
                               "interval": 5, "expires_in": 900}
    assert device.device_code == DEVICE_CODE
    assert flow.poll(device) == github.PollResult("pending", 5)
    assert flow.poll(device) == github.PollResult("slow_down", 10)
    assert device.interval == 10
    granted = flow.poll(device)
    assert (granted.state, granted.token) == ("authorized", TOKEN)
    assert_secret_free(repr(device), repr(granted))
    start, *polls = fake.requests
    assert (start.path, start.form) == ("/login/device/code", {"client_id": "Iv1.test", "scope": "repo"})
    assert start.headers["accept"] == "application/json"
    assert [poll.form for poll in polls] == [{"client_id": "Iv1.test", "device_code": DEVICE_CODE,
                                              "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}] * 3
    assert all("authorization" not in request.headers for request in fake.requests)


def test_slow_down_keeps_a_longer_interval_that_github_asks_for(fake):
    script_polls(fake, {**SLOW_DOWN, "interval": 15})
    device = github.DeviceCode(DEVICE_CODE, "WDJB-MJHT", "https://github.com/login/device", 5, 900)
    assert github.DeviceFlow("Iv1.test", web_url=fake.url).poll(device).interval == 15


def test_the_blocking_helper_sleeps_each_interval_with_the_injected_clock(fake):
    script_polls(fake, PENDING, SLOW_DOWN, GRANTED)
    device = github.DeviceCode(DEVICE_CODE, "WDJB-MJHT", "https://github.com/login/device", 5, 900)
    now, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    assert github.DeviceFlow("Iv1.test", web_url=fake.url).wait(device, sleep=sleep, clock=lambda: now[0]) == TOKEN
    assert sleeps == [5, 5, 10]


def test_the_blocking_helper_gives_up_at_the_deadline(fake):
    fake.reply("POST", "/login/oauth/access_token", 200, PENDING, repeat=True)
    device = github.DeviceCode(DEVICE_CODE, "WDJB-MJHT", "https://github.com/login/device", 5, 12)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(github.GitHubError) as caught:
        github.DeviceFlow("Iv1.test", web_url=fake.url).wait(device, sleep=sleep, clock=lambda: now[0])
    assert caught.value.code == "expired_token"
    assert len(fake.requests) == 2  # at 5 s and 10 s; 15 s is past the deadline


def test_the_blocking_helper_retries_a_few_network_failures(fake):
    fake.reply("POST", "/login/oauth/access_token", 502, None)
    fake.reply("POST", "/login/oauth/access_token", 503, None)
    script_polls(fake, GRANTED)
    device = github.DeviceCode(DEVICE_CODE, "WDJB-MJHT", "https://github.com/login/device", 1, 900)
    flow = github.DeviceFlow("Iv1.test", web_url=fake.url)
    assert flow.wait(device, sleep=lambda seconds: None, clock=lambda: 0.0) == TOKEN
    for _ in range(3):
        fake.reply("POST", "/login/oauth/access_token", 502, None)
    with pytest.raises(github.GitHubError) as caught:
        flow.wait(device, sleep=lambda seconds: None, clock=lambda: 0.0)
    assert caught.value.code == "network"


@pytest.mark.parametrize("error", ["expired_token", "access_denied", "device_flow_disabled",
                                   "incorrect_client_credentials", "incorrect_device_code"])
def test_device_flow_errors_keep_github_names_and_hide_the_device_code(fake, error):
    script_polls(fake, {"error": error, "error_description": f"refused {DEVICE_CODE}"})
    device = github.DeviceCode(DEVICE_CODE, "WDJB-MJHT", "https://github.com/login/device", 5, 900)
    with pytest.raises(github.GitHubError) as caught:
        github.DeviceFlow("Iv1.test", web_url=fake.url).poll(device)
    assert caught.value.code == error
    assert_secret_free(str(caught.value), repr(caught.value))


def test_an_unknown_oauth_error_is_redacted(fake):
    script_polls(fake, {"error": "strange_error", "error_description": f"echo {DEVICE_CODE}"})
    device = github.DeviceCode(DEVICE_CODE, "WDJB-MJHT", "https://github.com/login/device", 5, 900)
    with pytest.raises(github.GitHubError) as caught:
        github.DeviceFlow("Iv1.test", web_url=fake.url).poll(device)
    assert caught.value.code == "oauth" and "strange_error" in caught.value.message
    assert_secret_free(str(caught.value), repr(caught.value))


def test_a_disabled_device_flow_fails_at_the_start(fake):
    fake.reply("POST", "/login/device/code", 200, {"error": "device_flow_disabled"})
    with pytest.raises(github.GitHubError) as caught:
        github.DeviceFlow("Iv1.test", web_url=fake.url).start()
    assert caught.value.code == "device_flow_disabled"


def test_sign_in_without_a_client_id_fails_before_any_request(fake, monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTS_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.setattr(github, "DEFAULT_CLIENT_ID", "")
    for start in (github.DeviceFlow(web_url=fake.url).start,
                  github.GitHubAccount(tmp_path, "agents-core-sync-test", web_url=fake.url,
                                       api_url=fake.api, store=MemoryStore()).start_sign_in):
        with pytest.raises(github.GitHubError) as caught:
            start()
        assert caught.value.code == "no_client_id"
    assert fake.requests == []


def test_a_token_without_the_repo_scope_is_refused(fake):
    script_polls(fake, {**GRANTED, "scope": "read:user"})
    device = github.DeviceCode(DEVICE_CODE, "WDJB-MJHT", "https://github.com/login/device", 5, 900)
    with pytest.raises(github.GitHubError) as caught:
        github.DeviceFlow("Iv1.test", web_url=fake.url).poll(device)
    assert caught.value.code == "insufficient_scope"
    assert_secret_free(str(caught.value))


def test_a_rate_limited_device_request_reports_when_to_retry(fake):
    fake.reply("POST", "/login/device/code", 429, None, {"Retry-After": "30"})
    with pytest.raises(github.GitHubError) as caught:
        github.DeviceFlow("Iv1.test", web_url=fake.url).start()
    assert caught.value.code == "rate_limited" and caught.value.reset_at is not None


@pytest.mark.parametrize("status, payload, headers, code", [
    (404, {"error": "Not Found"}, {}, "not_found"),
    (429, {"error": "rate_limited"}, {"Retry-After": "30"}, "rate_limited"),
    (403, {"error": "rate_limited"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1900000000"}, "rate_limited"),
    (503, {"error": "temporarily_unavailable"}, {}, "network"),
    (403, {"error": "forbidden"}, {}, "oauth"),
])
def test_the_status_is_mapped_before_an_error_body(fake, status, payload, headers, code):
    fake.reply("POST", "/login/device/code", status, payload, headers)
    with pytest.raises(github.GitHubError) as caught:
        github.DeviceFlow("Iv1.test", web_url=fake.url).start()
    assert (caught.value.code, caught.value.status) == (code, status)
    if code == "not_found":
        assert "AGENTS_GITHUB_HOST" in caught.value.message and "AGENTS_GITHUB_CLIENT_ID" in caught.value.message
    if code == "rate_limited":
        assert caught.value.reset_at is not None


@pytest.mark.parametrize("status", [400, 401])
def test_an_oauth_error_is_also_accepted_on_400_and_401(fake, status):
    fake.reply("POST", "/login/oauth/access_token", status, {"error": "access_denied"})
    device = github.DeviceCode(DEVICE_CODE, "WDJB-MJHT", "https://github.com/login/device", 5, 900)
    with pytest.raises(github.GitHubError) as caught:
        github.DeviceFlow("Iv1.test", web_url=fake.url).poll(device)
    assert caught.value.code == "access_denied"


def test_the_blocking_helper_retries_a_server_error_that_has_an_error_body(fake):
    fake.reply("POST", "/login/oauth/access_token", 503, {"error": "temporarily_unavailable"})
    script_polls(fake, GRANTED)
    device = github.DeviceCode(DEVICE_CODE, "WDJB-MJHT", "https://github.com/login/device", 1, 900)
    flow = github.DeviceFlow("Iv1.test", web_url=fake.url)
    assert flow.wait(device, sleep=lambda seconds: None, clock=lambda: 0.0) == TOKEN


# --- REST API ------------------------------------------------------------------------

def test_user_returns_the_login_with_the_api_headers(fake):
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat", "id": 1})
    gh = client(fake)
    assert gh.user() == "octocat" and gh.login == "octocat"
    request = fake.requests[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert_secret_free(repr(gh))


def test_create_library_makes_an_empty_private_repository(fake):
    fake.reply("POST", "/api/v3/user/repos", 201, repo("octocat/agents-library"))
    info = client(fake).create_library()
    assert (info.full_name, info.owner, info.name, info.private) == (
        "octocat/agents-library", "octocat", "agents-library", True)
    assert info.ssh_url == "git@github.com:octocat/agents-library.git"
    body = fake.requests[0].json
    assert body["name"] == "agents-library" and body["private"] is True and body["auto_init"] is False
    assert body["has_issues"] is body["has_wiki"] is body["has_projects"] is False


def test_create_library_reports_an_existing_name(fake):
    fake.reply("POST", "/api/v3/user/repos", 422, {
        "message": "Repository creation failed.",
        "errors": [{"resource": "Repository", "code": "custom", "field": "name",
                    "message": "name already exists on this account"}]})
    with pytest.raises(github.GitHubError) as caught:
        client(fake).create_library("my-library")
    assert caught.value.code == "exists" and "my-library" in caught.value.message


@pytest.mark.parametrize("name", ["my library", "..", "", "a/b"])
def test_create_library_refuses_an_invalid_name_without_a_request(fake, name):
    with pytest.raises(github.GitHubError) as caught:
        client(fake).create_library(name)
    assert caught.value.code == "invalid" and fake.requests == []


def marker(full_name, status=200):
    path = f"/api/v3/repos/{full_name}/contents/.agents-library.json"
    return path, status, ({"type": "file", "name": ".agents-library.json"} if status == 200 else {"message": "x"})


def test_libraries_pages_through_private_repositories_and_checks_the_marker(fake):
    second = f"{fake.api}/user/repos?visibility=private&page=2"
    fake.reply("GET", "/api/v3/user/repos", 200, [repo("octocat/notes"), repo("octocat/agents-library")],
               {"Link": f'<{second}>; rel="next", <{second}>; rel="last"'})
    fake.reply("GET", "/api/v3/user/repos", 200, [repo("octocat/empty"), repo("octocat/old-library")])
    for full_name, status in (("octocat/notes", 404), ("octocat/agents-library", 200),
                              ("octocat/empty", 409), ("octocat/old-library", 200)):
        path, status, payload = marker(full_name, status)
        fake.reply("GET", path, status, payload)
    scan = client(fake).libraries()
    assert [info.full_name for info in scan.libraries] == ["octocat/agents-library", "octocat/old-library"]
    assert (scan.checked, scan.truncated) == (4, False)
    first, page_two = (request for request in fake.requests if request.path == "/api/v3/user/repos")
    assert (first.query["visibility"], first.query["affiliation"]) == (["private"], ["owner"])
    assert page_two.query["page"] == ["2"]


def test_libraries_stops_at_the_cap_and_says_so(fake):
    fake.reply("GET", "/api/v3/user/repos", 200, [repo(f"octocat/r{number}") for number in range(3)])
    for number in range(2):
        path, status, payload = marker(f"octocat/r{number}", 404)
        fake.reply("GET", path, status, payload)
    scan = client(fake).libraries(limit=2)
    assert (scan.libraries, scan.checked, scan.truncated) == ((), 2, True)
    assert not any("/r2/" in request.path for request in fake.requests)


def test_repository_reports_privacy_and_require_private_refuses_public_and_internal(fake):
    fake.reply("GET", "/api/v3/repos/octocat/agents-library", 200, repo("octocat/agents-library"))
    fake.reply("GET", "/api/v3/repos/octocat/site", 200, repo("octocat/site", private=False), repeat=True)
    fake.reply("GET", "/api/v3/repos/acme/library", 200, repo("acme/library", visibility="internal"))
    gh = client(fake)
    info = gh.require_private("octocat/agents-library")
    assert (info.private, info.visibility, info.default_branch) == (True, "private", "main")
    assert gh.repository("octocat/site").visibility == "public"
    for full_name in ("octocat/site", "acme/library"):
        with pytest.raises(github.GitHubError) as caught:
            gh.require_private(full_name)
        assert caught.value.code == "public_repo"


@pytest.mark.parametrize("full_name", ["../user", "octocat", "octocat/..", "octocat/a/b", "-x/y", "octo cat/x"])
def test_an_invalid_repository_name_is_refused_without_a_request(fake, full_name):
    with pytest.raises(github.GitHubError) as caught:
        client(fake).repository(full_name)
    assert caught.value.code == "invalid" and fake.requests == []


def test_deploy_keys_are_added_listed_found_by_key_material_and_deleted(fake):
    keys = "/api/v3/repos/octocat/agents-library/keys"
    mine, other = public_key(1), public_key(2, "someone-else")
    fake.reply("POST", keys, 201, {"id": 7, "title": "Agents-Core laptop", "key": key_material(mine),
                                   "read_only": False, "created_at": "2026-10-05T10:00:00Z"})
    gh = client(fake)
    added = gh.add_deploy_key("octocat/agents-library", mine, "laptop")
    assert (added.id, added.title, added.read_only) == (7, "Agents-Core laptop", False)
    assert fake.requests[-1].json == {"title": "Agents-Core laptop", "key": key_material(mine), "read_only": False}

    second = f"{fake.api}/repos/octocat/agents-library/keys?page=2"
    listing = ([{"id": 3, "title": "Agents-Core desktop", "key": key_material(other), "read_only": False}],
               [{"id": 7, "title": "Agents-Core laptop", "key": key_material(mine), "read_only": False}])
    for _ in range(2):
        fake.reply("GET", keys, 200, listing[0], {"Link": f'<{second}>; rel="next"'})
        fake.reply("GET", keys, 200, listing[1])
    assert [key.id for key in gh.deploy_keys("octocat/agents-library")] == [3, 7]
    found = gh.find_deploy_key("octocat/agents-library", public_key(1, "a different comment"))
    assert found is not None and found.id == 7

    fake.reply("GET", keys, 200, listing[0])
    assert gh.find_deploy_key("octocat/agents-library", public_key(9)) is None

    fake.reply("DELETE", keys + "/7", 204)
    gh.delete_deploy_key("octocat/agents-library", 7)
    assert (fake.requests[-1].method, fake.requests[-1].path) == ("DELETE", keys + "/7")


def test_a_key_already_in_use_is_reported_as_existing(fake):
    fake.reply("POST", "/api/v3/repos/octocat/agents-library/keys", 422, {
        "message": "Validation Failed",
        "errors": [{"resource": "PublicKey", "code": "custom", "field": "key", "message": "key is already in use"}]})
    with pytest.raises(github.GitHubError) as caught:
        client(fake).add_deploy_key("octocat/agents-library", public_key(1), "laptop")
    assert caught.value.code == "exists"


@pytest.mark.parametrize("text", ["", "ssh-ed25519", "ssh-ed25519 not-base64!", "ssh-rsa " + public_key(1).split()[1],
                                  "ssh-ed25519 " + base64.b64encode(b"short").decode()])
def test_a_malformed_public_key_is_refused(text):
    with pytest.raises(github.GitHubError) as caught:
        github.parse_public_key(text)
    assert caught.value.code == "invalid"


def test_parse_public_key_ignores_the_comment():
    assert github.parse_public_key(public_key(1, "x")) == github.parse_public_key(public_key(1, "y z"))


RATE_RESET = 1900000000


@pytest.mark.parametrize("full_name, status, payload, headers, code", [
    ("octocat/lib", 401, {"message": "Bad credentials"}, {}, "auth"),
    ("acme/lib", 403, {"message": "Although you appear to have the correct authorization credentials, the `acme` "
                                  "organization has enabled OAuth App access restrictions, meaning that data access "
                                  "to third-parties is limited."}, {}, "org_approval"),
    ("acme/lib", 403, {"message": "Resource not accessible"}, {}, "forbidden"),
    ("friend/lib", 403, {"message": "Must have admin rights to Repository."}, {}, "forbidden"),  # a collaborator
    ("acme/lib", 403, {"message": "Resource protected by organization SAML enforcement."},
     {"X-GitHub-SSO": "required; url=https://github.com/orgs/acme/sso?authorization_request=A1"}, "org_approval"),
    ("octocat/lib", 403, {"message": "Must have admin rights to Repository."}, {}, "forbidden"),
    ("octocat/lib", 404, {"message": "Not Found"}, {}, "not_found"),
    ("octocat/lib", 403, {"message": "API rate limit exceeded"},
     {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(RATE_RESET)}, "rate_limited"),
    ("octocat/lib", 429, {"message": "Too Many Requests"}, {"Retry-After": "60"}, "rate_limited"),
    ("octocat/lib", 403, {"message": "You have exceeded a secondary rate limit."}, {}, "rate_limited"),
    ("octocat/lib", 422, {"message": f"Validation Failed for {TOKEN}"}, {}, "invalid"),
    ("octocat/lib", 502, None, {}, "network"),
    ("octocat/lib", 418, {"message": "I'm a teapot"}, {}, "http"),
])
def test_api_errors_map_to_stable_codes_without_the_token(fake, full_name, status, payload, headers, code):
    fake.reply("GET", f"/api/v3/repos/{full_name}", status, payload, headers)
    unauthorized = []
    gh = client(fake, login="octocat", on_unauthorized=lambda: unauthorized.append(True))
    with pytest.raises(github.GitHubError) as caught:
        gh.repository(full_name)
    error = caught.value
    assert (error.code, error.status) == (code, status)
    assert unauthorized == ([True] if status == 401 else [])
    assert_secret_free(str(error), repr(error), error.message)
    if code == "rate_limited":
        assert error.reset_at is not None
    if headers.get("X-RateLimit-Reset"):
        assert error.reset_at == RATE_RESET
        assert datetime.fromtimestamp(RATE_RESET, timezone.utc).strftime("%Y-%m-%d %H:%M") in error.message
    if code == "org_approval" and "X-GitHub-SSO" not in headers:
        assert "approve the Agents-Core OAuth App" in error.message
    if code == "forbidden":
        assert "may need to approve the Agents-Core OAuth App" in error.message and ".." not in error.message


def test_redaction_happens_before_a_long_message_is_cut(fake):
    for filler in range(262, 301, 2):  # the 300-character cut falls at every point of the token
        fake.reply("GET", "/api/v3/repos/octocat/lib", 422, {"message": "x" * filler + TOKEN})
        with pytest.raises(github.GitHubError) as caught:
            client(fake).repository("octocat/lib")
        assert_secret_free(caught.value.message, repr(caught.value))


def test_an_absurd_rate_limit_reset_still_reports_the_rate_limit(fake):
    fake.reply("GET", "/api/v3/user", 429, None, {"Retry-After": "99999999999999"})
    with pytest.raises(github.GitHubError) as caught:
        client(fake).user()
    assert (caught.value.code, caught.value.reset_at) == ("rate_limited", None)


def test_an_unreachable_api_is_a_network_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(github.GitHubError) as caught:
        github.GitHubClient(TOKEN, api_url=f"http://127.0.0.1:{port}", timeout=5).user()
    assert caught.value.code == "network"
    assert_secret_free(str(caught.value), repr(caught.value))


def test_a_redirect_keeps_the_token_only_on_the_same_origin(fake):
    other = FakeGitHub()
    try:
        fake.reply("GET", "/api/v3/repos/octocat/old-name", 301, {"message": "Moved Permanently"},
                   {"Location": f"{fake.api}/repositories/42"})
        fake.reply("GET", "/api/v3/repositories/42", 200, repo("octocat/agents-library"))
        assert client(fake).repository("octocat/old-name").full_name == "octocat/agents-library"
        assert fake.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"

        fake.reply("GET", "/api/v3/repos/octocat/elsewhere", 302, None, {"Location": f"{other.api}/repositories/43"})
        other.reply("GET", "/api/v3/repositories/43", 200, repo("octocat/elsewhere"))
        client(fake).repository("octocat/elsewhere")
        assert len(other.requests) == 1 and "authorization" not in other.requests[0].headers
    finally:
        other.close()


def test_a_pagination_link_to_another_origin_is_not_followed(fake):
    fake.reply("GET", "/api/v3/repos/octocat/lib/keys", 200, [],
               {"Link": '<https://elsewhere.example/api/v3/keys?page=2>; rel="next"'})
    with pytest.raises(github.GitHubError) as caught:
        client(fake).deploy_keys("octocat/lib")
    assert caught.value.code == "unexpected_response"


def test_a_malformed_token_is_refused_before_any_request():
    with pytest.raises(github.GitHubError) as caught:
        github.GitHubClient("gho_abc\r\nX-Injected: 1", api_url="http://127.0.0.1:1")
    assert caught.value.code == "invalid" and "X-Injected" not in caught.value.message


# --- token storage -------------------------------------------------------------------

def test_the_file_store_keeps_the_token_private(tmp_path):
    state = tmp_path / "state"
    store = github.FileStore(state)
    assert store.load() is None
    store.save(TOKEN)
    assert store.load() == TOKEN
    if os.name == "posix":
        assert stat.S_IMODE((state / github.TOKEN_FILE).stat().st_mode) == 0o600
    store.save(TOKEN + "x")
    assert store.load() == TOKEN + "x"
    store.delete()
    assert store.load() is None
    store.delete()
    assert list(state.iterdir()) == []
    assert_secret_free(repr(store))


def done(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


class FakeTools:
    """``security`` and ``secret-tool`` simulated in memory; records the argv, stdin and env of every call."""

    def __init__(self):
        self.items: dict = {}
        self.calls: list[dict] = []

    def run(self, argv, *args, input=None, env=None, **kwargs):
        self.calls.append({"argv": list(argv), "input": input, "env": env})
        tool, *rest = argv
        return (self.security if Path(tool).name == "security" else self.secret_tool)(argv, rest, input)

    def security(self, argv, args, stdin):
        if args == ["-i"]:
            words = stdin.split()
            assert words[:2] == ["add-generic-password", "-U"] and stdin.endswith("\n")
            key = (words[words.index("-s") + 1], words[words.index("-a") + 1])
            self.items[key] = bytes.fromhex(words[words.index("-X") + 1]).decode()
            return done(argv)
        command, *options = args
        key = (options[options.index("-s") + 1], options[options.index("-a") + 1])
        missing = done(argv, 44, stderr="security: SecKeychainSearchCopyNext: The specified item could not be found.")
        if command == "find-generic-password":
            assert options[-1] == "-w"
            return done(argv, 0, self.items[key] + "\n") if key in self.items else missing
        assert command == "delete-generic-password"
        return done(argv) if self.items.pop(key, None) is not None else missing

    def secret_tool(self, argv, args, stdin):
        command, *rest = args
        if command == "store":
            assert rest[0].startswith("--label=")
            self.items[tuple(rest[1:])] = stdin
            return done(argv)
        if command == "lookup":
            return done(argv, 0, self.items[tuple(rest)]) if tuple(rest) in self.items else done(argv, 1)
        assert command == "clear"
        self.items.pop(tuple(rest), None)
        return done(argv)


@pytest.fixture
def tools(monkeypatch):
    simulated = FakeTools()

    def no_popen(*args, **kwargs):
        raise AssertionError("secret stores start child processes through subprocess.run only")

    monkeypatch.setattr(github.subprocess, "run", simulated.run)
    monkeypatch.setattr(github.subprocess, "Popen", no_popen)
    monkeypatch.setattr(github.shutil, "which", lambda name, *args, **kwargs: f"/usr/bin/{Path(name).name}")
    return simulated


def assert_token_only_on_stdin(tools):
    for call in tools.calls:
        assert_secret_free(*call["argv"])
        assert call["env"] is None  # inherited, and the token is never put into os.environ
    assert TOKEN not in os.environ.values()


@pytest.mark.parametrize("make", [lambda: github.KeychainStore("agents-core-sync-test"),
                                  lambda: github.SecretToolStore("agents-core-sync-test")])
def test_command_line_stores_pass_the_token_on_stdin_only(tools, make):
    store = make()
    assert store.load() is None
    store.save(TOKEN)
    assert store.load() == TOKEN
    store.save(TOKEN + "x")
    assert store.load() == TOKEN + "x"
    store.delete()
    assert store.load() is None
    store.delete()
    saves = [call for call in tools.calls if call["input"]]
    assert len(saves) == 2
    if isinstance(store, github.KeychainStore):
        assert saves[0]["argv"] == [github.SECURITY, "-i"] and TOKEN.encode().hex() in saves[0]["input"]
    else:
        assert saves[0]["input"] == TOKEN  # exactly the token: no newline becomes part of it
    assert_token_only_on_stdin(tools)
    assert_secret_free(repr(store))


def test_store_failures_never_show_the_token(monkeypatch):
    def echoing(argv, *args, input=None, **kwargs):
        return done(argv, 1, stderr=f"{argv}\nfailed: {input.strip()}")  # echoes stdin on the last line

    monkeypatch.setattr(github.subprocess, "run", echoing)
    for store in (github.KeychainStore("agents-core-sync-test"),
                  github.SecretToolStore("agents-core-sync-test", executable="/usr/bin/secret-tool")):
        with pytest.raises(github.GitHubError) as caught:
            store.save(TOKEN)
        assert caught.value.code == "storage"
        assert_secret_free(str(caught.value), repr(caught.value))


def test_a_store_that_does_not_keep_the_token_is_an_error(tools, monkeypatch):
    store = github.KeychainStore("agents-core-sync-test")
    monkeypatch.setattr(store, "load", lambda: None)
    with pytest.raises(github.GitHubError) as caught:
        store.save(TOKEN)
    assert caught.value.code == "storage"


def test_secret_tool_counts_as_available_only_when_a_lookup_answers(monkeypatch):
    answers = {"works": done([], 1), "found": done([], 0, "x"),
               "no_dbus": done([], 1, stderr="secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY")}
    monkeypatch.setattr(github.shutil, "which", lambda name, *args, **kwargs: "/usr/bin/secret-tool")
    for case, expected in (("works", True), ("found", True), ("no_dbus", False)):
        monkeypatch.setattr(github.subprocess, "run", lambda argv, *a, answer=answers[case], **k: answer)
        assert github.SecretToolStore("agents-core-sync-test").available() is expected

    def hangs(argv, *args, **kwargs):
        raise subprocess.TimeoutExpired(argv, github.STORE_TIMEOUT)

    monkeypatch.setattr(github.subprocess, "run", hangs)
    assert github.SecretToolStore("agents-core-sync-test").available() is False
    monkeypatch.setattr(github.shutil, "which", lambda name, *args, **kwargs: None)
    assert github.SecretToolStore("agents-core-sync-test").available() is False


class FakeCredentialApi:
    """advapi32's CredWriteW, CredReadW, CredDeleteW and CredFree over an in-memory vault."""

    def __init__(self, fail_with: int = 0, fail_reads_with: int = 0):
        self.vault: dict = {}
        self.error = 0
        self.fail_with = fail_with
        self.fail_reads_with = fail_reads_with
        self.freed = 0
        self._alive: list = []

    def last_error(self):
        return self.error

    def CredWriteW(self, pointer, flags):
        if self.fail_with:
            self.error = self.fail_with
            return 0
        credential = pointer.contents
        self.vault[credential.TargetName] = {
            "type": credential.Type, "persist": credential.Persist, "user": credential.UserName,
            "blob": ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)}
        return 1

    def CredReadW(self, target, kind, flags, out):
        if self.fail_reads_with:
            self.error = self.fail_reads_with
            return 0
        entry = self.vault.get(target)
        if entry is None or entry["type"] != kind:
            self.error = github.ERROR_NOT_FOUND
            return 0
        buffer = (ctypes.c_ubyte * len(entry["blob"])).from_buffer_copy(entry["blob"])
        credential = github._Credential(Type=kind, TargetName=target, CredentialBlobSize=len(entry["blob"]),
                                        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        self._alive.append((credential, buffer))
        out[0] = ctypes.pointer(credential)
        return 1

    def CredDeleteW(self, target, kind, flags):
        if self.vault.pop(target, None) is None:
            self.error = github.ERROR_NOT_FOUND
            return 0
        return 1

    def CredFree(self, pointer):
        self.freed += 1


def test_the_credential_manager_store_marshals_a_generic_local_credential():
    api = FakeCredentialApi()
    store = github.CredentialManagerStore("agents-core-sync-test", api=api)
    assert store.load() is None
    store.save(TOKEN)
    assert api.vault["agents-core-sync-test"] == {"type": github.CRED_TYPE_GENERIC, "user": github.SECRET_ACCOUNT,
                                                  "persist": github.CRED_PERSIST_LOCAL_MACHINE,
                                                  "blob": TOKEN.encode()}
    assert store.load() == TOKEN and api.freed == 1
    store.delete()
    assert store.load() is None
    store.delete()
    assert_secret_free(repr(store))
    with pytest.raises(github.GitHubError) as caught:
        github.CredentialManagerStore("agents-core-sync-test", api=FakeCredentialApi(fail_with=5)).save(TOKEN)
    assert caught.value.code == "storage" and "error 5" in caught.value.message
    assert_secret_free(str(caught.value))


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="the expected offsets are those of 64-bit Windows")
def test_the_credential_structure_has_the_64_bit_credentialw_layout():
    """wincred.h's CREDENTIALW on x64, checked independently: the fake API reuses the module's structure."""
    fields = [name for name, _ in github._Credential._fields_]
    assert fields == ["Flags", "Type", "TargetName", "Comment", "LastWritten", "CredentialBlobSize",
                      "CredentialBlob", "Persist", "AttributeCount", "Attributes", "TargetAlias", "UserName"]
    assert [getattr(github._Credential, name).offset for name in fields] == [0, 4, 8, 16, 24, 32, 40, 48, 52, 56,
                                                                             64, 72]
    assert ctypes.sizeof(github._Credential) == 80 and ctypes.sizeof(github._FileTime) == 8


@pytest.mark.parametrize("name", ["x -A", "x\nadd-generic-password -A -s y", 'x"y', "x'y", "x;y", "-x", "", "x" * 129])
def test_hostile_secret_names_are_refused_before_anything_runs(tools, tmp_path, name):
    makers = (lambda: github.KeychainStore(name), lambda: github.SecretToolStore(name),
              lambda: github.CredentialManagerStore(name, api=FakeCredentialApi()),
              lambda: github.GitHubAccount(tmp_path, name, web_url="http://127.0.0.1:9",
                                           api_url="http://127.0.0.1:9/api/v3", store=MemoryStore()))
    for make in makers:
        with pytest.raises(github.GitHubError) as caught:
            make()
        assert caught.value.code == "config"
    assert tools.calls == []  # nothing reached `security -i` or secret-tool


def test_the_default_store_is_the_os_store_where_one_works(monkeypatch, tmp_path):
    name = "agents-core-sync-test"
    security = tmp_path / "security"
    security.write_text("")
    monkeypatch.setattr(github, "SECURITY", str(security))
    assert isinstance(REAL_DEFAULT_STORE(tmp_path, name, platform="darwin"), github.KeychainStore)
    monkeypatch.setattr(github, "SECURITY", str(tmp_path / "missing"))
    assert isinstance(REAL_DEFAULT_STORE(tmp_path, name, platform="darwin"), github.FileStore)

    monkeypatch.setattr(github, "_CredentialApi", FakeCredentialApi)
    assert isinstance(REAL_DEFAULT_STORE(tmp_path, name, platform="win32"), github.CredentialManagerStore)
    # ERROR_NO_SUCH_LOGON_SESSION: a session without cached credentials, such as SSH with a key
    monkeypatch.setattr(github, "_CredentialApi", lambda: FakeCredentialApi(fail_reads_with=1312))
    assert isinstance(REAL_DEFAULT_STORE(tmp_path, name, platform="win32"), github.FileStore)

    def no_advapi32():
        raise OSError("advapi32 is missing")

    monkeypatch.setattr(github, "_CredentialApi", no_advapi32)
    assert isinstance(REAL_DEFAULT_STORE(tmp_path, name, platform="win32"), github.FileStore)

    monkeypatch.setattr(github.shutil, "which", lambda command, *args, **kwargs: "/usr/bin/secret-tool")
    monkeypatch.setattr(github.subprocess, "run", lambda argv, *args, **kwargs: done(argv, 1))
    assert isinstance(REAL_DEFAULT_STORE(tmp_path, name, platform="linux"), github.SecretToolStore)
    monkeypatch.setattr(github.subprocess, "run", lambda argv, *args, **kwargs: done(argv, 1, stderr="no D-Bus"))
    assert isinstance(REAL_DEFAULT_STORE(tmp_path, name, platform="linux"), github.FileStore)
    monkeypatch.setattr(github.shutil, "which", lambda command, *args, **kwargs: None)
    assert isinstance(REAL_DEFAULT_STORE(tmp_path, name, platform="linux"), github.FileStore)


REAL_STORE = os.environ.get("AGENTS_TEST_REAL_SECRET_STORE") == "1"


@pytest.mark.skipif(not REAL_STORE, reason="AGENTS_TEST_REAL_SECRET_STORE=1 allows writing this machine's store")
def test_the_real_os_secret_store_round_trip(monkeypatch):
    monkeypatch.setattr(github.subprocess, "run", REAL_RUN)
    monkeypatch.setattr(github, "_CredentialApi", REAL_CREDENTIAL_API)
    name = f"agents-core-sync-test-{uuid.uuid4().hex[:12]}"
    if sys.platform == "darwin":
        store = github.KeychainStore(name)
    elif sys.platform == "win32":
        store = github.CredentialManagerStore(name)
    else:
        if not shutil.which("secret-tool") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            pytest.skip("secret-tool or a D-Bus session is missing")
        store = github.SecretToolStore(name)
        if not store.available():
            pytest.skip("secret-tool cannot reach a Secret Service")
    token = "gho_" + uuid.uuid4().hex
    try:
        assert store.load() is None
        store.save(token)
        assert store.load() == token
        store.save(token + "x")
        assert store.load() == token + "x"
        store.delete()
        assert store.load() is None
        store.delete()
    finally:
        store.delete()


# --- account facade ------------------------------------------------------------------

class MemoryStore:
    backend = "memory"

    def __init__(self):
        self.token = None

    def load(self):
        return self.token

    def save(self, token):
        self.token = token

    def delete(self):
        self.token = None


def account(fake, state: Path, store=None) -> github.GitHubAccount:
    return github.GitHubAccount(state, "agents-core-sync-test", web_url=fake.url, api_url=fake.api,
                                client_id="Iv1.test", store=store or github.FileStore(state))


def test_web_ui_sign_in_then_a_revoked_token_then_forget(fake, tmp_path):
    state = tmp_path / "state"
    script_device_code(fake)
    script_polls(fake, PENDING, GRANTED)
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    acct = account(fake, state)
    assert acct.status() == {"connected": False, "host": acct.host, "login": None, "reconnect_needed": False,
                             "storage": None, "connected_at": None, "warning": None}
    device = acct.start_sign_in()
    assert acct.poll_sign_in(device) == {"state": "pending", "interval": 5}
    connected = acct.poll_sign_in(device)
    assert (connected["state"], connected["connected"], connected["login"]) == ("connected", True, "octocat")
    status = acct.status()
    assert (status["storage"], status["warning"], status["reconnect_needed"]) == ("file", github.FileStore.WARNING,
                                                                                   False)
    metadata = (state / github.ACCOUNT_FILE).read_text()
    assert json.loads(metadata)["login"] == "octocat"
    assert_secret_free(metadata, json.dumps(status))
    if os.name == "posix":
        assert stat.S_IMODE((state / github.ACCOUNT_FILE).stat().st_mode) == 0o600

    fake.reply("GET", "/api/v3/repos/octocat/agents-library", 401, {"message": "Bad credentials"})
    gh = acct.client()
    with pytest.raises(github.GitHubError) as caught:
        gh.repository("octocat/agents-library")
    assert caught.value.code == "auth"
    assert acct.status()["reconnect_needed"] is True and acct.status()["connected"] is True

    forgotten = acct.forget()
    assert forgotten["connected"] is False and forgotten["revoke_url"] == f"{fake.url}/settings/applications"
    assert not (state / github.ACCOUNT_FILE).exists() and github.FileStore(state).load() is None


def test_terminal_sign_in_blocks_with_the_injected_sleep(fake, tmp_path):
    script_device_code(fake)
    script_polls(fake, PENDING, GRANTED)
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    store = MemoryStore()
    acct = account(fake, tmp_path / "state", store)
    sleeps = []
    status = acct.wait_for_sign_in(acct.start_sign_in(), sleep=sleeps.append, clock=lambda: 0.0)
    assert (status["connected"], status["login"], status["storage"], status["warning"]) == (
        True, "octocat", "memory", None)
    assert store.token == TOKEN and sleeps == [5, 5]


def test_a_token_that_github_rejects_is_never_stored(fake, tmp_path):
    state = tmp_path / "state"
    fake.reply("GET", "/api/v3/user", 401, {"message": "Bad credentials"})
    with pytest.raises(github.GitHubError) as caught:
        account(fake, state).complete_sign_in(TOKEN)
    assert caught.value.code == "auth"
    assert not (state / github.TOKEN_FILE).exists() and not (state / github.ACCOUNT_FILE).exists()


def test_a_new_sign_in_removes_the_old_file_copy(fake, tmp_path):
    state = tmp_path / "state"
    github.FileStore(state).save("gho_previous_token_value")
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    store = MemoryStore()
    acct = account(fake, state, store)
    assert acct.complete_sign_in(TOKEN)["storage"] == "memory"
    assert store.token == TOKEN and not (state / github.TOKEN_FILE).exists()
    assert acct.client().login == "octocat"
    acct.forget()
    assert store.token is None and acct.status()["connected"] is False


def test_a_token_from_another_host_is_never_sent(fake, tmp_path):
    state = tmp_path / "state"
    github.FileStore(state).save(TOKEN)
    (state / github.ACCOUNT_FILE).write_text(json.dumps({"host": "github.com", "login": "octocat",
                                                          "storage": "file"}))
    acct = account(fake, state)
    status = acct.status()
    assert status["connected"] is False and "github.com" in status["warning"]
    with pytest.raises(github.GitHubError) as caught:
        acct.client()
    assert caught.value.code == "auth" and fake.requests == []


def test_a_missing_token_marks_the_account_for_a_reconnect(fake, tmp_path):
    state = tmp_path / "state"
    acct = account(fake, state)
    state.mkdir()
    (state / github.ACCOUNT_FILE).write_text(json.dumps({"host": acct.host, "login": "octocat", "storage": "file",
                                                          "reconnect_needed": False}))
    with pytest.raises(github.GitHubError) as caught:
        acct.client()
    assert caught.value.code == "auth"
    assert acct.status()["reconnect_needed"] is True and acct.status()["login"] == "octocat"


def test_a_state_directory_that_cannot_be_created_is_a_storage_error(fake, tmp_path):
    blocked = tmp_path / "state"
    blocked.write_text("a file where the directory should be")
    with pytest.raises(github.GitHubError) as caught:
        account(fake, blocked, MemoryStore()).forget()
    assert caught.value.code == "storage"


def test_unreadable_metadata_counts_as_not_connected(fake, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / github.ACCOUNT_FILE).write_text("{not json")
    acct = account(fake, state)
    assert acct.status()["connected"] is False
    (state / github.ACCOUNT_FILE).write_text(json.dumps({"host": acct.host, "login": "octocat", "storage": ["x"]}))
    assert acct.status()["storage"] is None
    assert acct.forget()["connected"] is False


def test_a_401_for_an_old_token_never_marks_a_newer_sign_in(fake, tmp_path):
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"}, repeat=True)
    state = tmp_path / "state"
    acct = account(fake, state, MemoryStore())
    acct.complete_sign_in(OLD_TOKEN)
    first = json.loads((state / github.ACCOUNT_FILE).read_text())["sign_in"]
    stale = acct.client()  # a sync run that started before the reconnect
    acct.complete_sign_in(TOKEN)  # the user reconnects in the web UI meanwhile
    assert json.loads((state / github.ACCOUNT_FILE).read_text())["sign_in"] != first
    fake.reply("GET", "/api/v3/repos/octocat/lib", 401, {"message": "Bad credentials"})
    with pytest.raises(github.GitHubError) as caught:
        stale.repository("octocat/lib")
    assert caught.value.code == "auth" and acct.status()["reconnect_needed"] is False
    fake.reply("GET", "/api/v3/repos/octocat/lib", 401, {"message": "Bad credentials"})
    with pytest.raises(github.GitHubError):
        acct.client().repository("octocat/lib")
    assert acct.status()["reconnect_needed"] is True


def test_forget_empties_the_default_store_also_without_a_readable_record(fake, tmp_path, monkeypatch):
    state = tmp_path / "state"
    default = MemoryStore()  # this machine's default store, chosen as in production: not injected
    monkeypatch.setattr(github, "default_store", lambda directory, name, **kwargs: default)
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"}, repeat=True)
    acct = github.GitHubAccount(state, "agents-core-sync-test", web_url=fake.url, api_url=fake.api,
                                client_id="Iv1.test")
    for damage in ("none", "unreadable", "deleted"):
        acct.complete_sign_in(TOKEN)
        assert default.token == TOKEN
        (state / f".{github.TOKEN_FILE}.k3j2h1").write_text(TOKEN)  # a write killed before its rename
        if damage == "unreadable":
            (state / github.ACCOUNT_FILE).write_text("{truncated")
        elif damage == "deleted":
            (state / github.ACCOUNT_FILE).unlink()
        assert acct.forget()["connected"] is False
        assert default.token is None, damage
        assert sorted(path.name for path in state.iterdir()) == ["github-account.lock"]


def test_forget_finishes_when_a_store_that_never_held_the_token_refuses(fake, tmp_path, monkeypatch, caplog):
    """A Keychain that refuses in an SSH session: sign-in kept the token in the file; Forget still ends."""
    class Refusing(MemoryStore):
        backend = "keychain"

        def save(self, token):
            raise github.GitHubError("storage", "The macOS Keychain did not save the GitHub token: "
                                                "User interaction is not allowed")

        def delete(self):
            raise github.GitHubError("storage", "The macOS Keychain did not delete the GitHub token: "
                                                "User interaction is not allowed")

    state = tmp_path / "state"
    monkeypatch.setattr(github, "default_store", lambda directory, name, **kwargs: Refusing())
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    acct = github.GitHubAccount(state, "agents-core-sync-test", web_url=fake.url, api_url=fake.api,
                                client_id="Iv1.test")
    acct.complete_sign_in(TOKEN)
    assert acct.status()["storage"] == github.FileStore.backend
    with caplog.at_level("WARNING", logger=github.__name__):
        assert acct.forget()["connected"] is False
    assert sorted(path.name for path in state.iterdir()) == ["github-account.lock"]
    assert "User interaction is not allowed" in caplog.text and TOKEN not in caplog.text


def test_a_failed_deletion_keeps_the_record_so_forget_can_be_retried(fake, tmp_path):
    class Locked(MemoryStore):
        def delete(self):
            raise github.GitHubError("storage", "The macOS Keychain did not delete the GitHub token: locked")

    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    acct = account(fake, tmp_path / "state", Locked())
    acct.complete_sign_in(TOKEN)
    with pytest.raises(github.GitHubError) as caught:
        acct.forget()
    assert caught.value.code == "storage" and "locked" in caught.value.message
    assert acct.status()["connected"] is True


def test_a_failed_record_write_takes_the_token_back(fake, tmp_path, monkeypatch):
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    store = MemoryStore()
    acct = account(fake, tmp_path / "state", store)

    def full_disk(meta):
        raise github.GitHubError("storage", "Cannot write github-account.json: No space left on device")

    monkeypatch.setattr(acct, "_write", full_disk)
    with pytest.raises(github.GitHubError) as caught:
        acct.complete_sign_in(TOKEN)
    assert caught.value.code == "storage" and store.token is None and acct.status()["connected"] is False


def test_a_short_outage_after_the_device_flow_does_not_lose_the_token(fake, tmp_path):
    fake.reply("GET", "/api/v3/user", 502, None)
    fake.reply("GET", "/api/v3/user", 429, {"message": "Too Many Requests"}, {"Retry-After": "2"})
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    store, sleeps = MemoryStore(), []
    status = account(fake, tmp_path / "state", store).complete_sign_in(TOKEN, sleep=sleeps.append)
    assert status["connected"] and store.token == TOKEN
    assert len(sleeps) == 2 and sleeps[0] == github.SIGN_IN_RETRY_DELAY and 1 <= sleeps[1] <= 2


def test_the_login_check_gives_up_after_a_few_attempts_or_on_a_long_rate_limit(fake, tmp_path):
    store, sleeps = MemoryStore(), []
    acct = account(fake, tmp_path / "state", store)
    for _ in range(github.SIGN_IN_ATTEMPTS):
        fake.reply("GET", "/api/v3/user", 502, None)
    with pytest.raises(github.GitHubError) as caught:
        acct.complete_sign_in(TOKEN, sleep=sleeps.append)
    assert caught.value.code == "network" and len(sleeps) == github.SIGN_IN_ATTEMPTS - 1
    fake.reply("GET", "/api/v3/user", 403, {"message": "API rate limit exceeded"},
               {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 3600)})
    sleeps.clear()
    with pytest.raises(github.GitHubError) as caught:
        acct.complete_sign_in(TOKEN, sleep=sleeps.append)
    assert caught.value.code == "rate_limited" and sleeps == [] and store.token is None


def test_a_store_that_refuses_the_token_falls_back_to_the_file_with_a_warning(fake, tmp_path):
    class Refusing(MemoryStore):
        def save(self, token):
            raise github.GitHubError("storage", "The macOS Keychain did not store the GitHub token: "
                                                "User interaction is not allowed.")

    state = tmp_path / "state"
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    acct = account(fake, state, Refusing())
    status = acct.complete_sign_in(TOKEN)
    assert (status["connected"], status["storage"]) == (True, "file")
    assert status["warning"].startswith(github.FileStore.WARNING)
    assert "User interaction is not allowed" in status["warning"]
    assert github.FileStore(state).load() == TOKEN and acct.client().login == "octocat"
    assert_secret_free(json.dumps(status), (state / github.ACCOUNT_FILE).read_text())
