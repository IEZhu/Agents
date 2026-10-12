"""The read-only GitHub client and its keychain token provider, with fakes only (#338)."""
import contextlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from src.schedules import github
from src.schedules.github import GitHubClient, GitHubError, TokenUnavailable, fetch_snapshot, keychain_token

SECRET = "github_pat_" + "Z" * 40
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


class FakeGitHub:
    """Answers GET requests from a table of path -> pages; records every request."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        path, _, query = url.removeprefix(github.API).partition("?")
        page = int(dict(part.split("=") for part in query.split("&") if part).get("page", 1)) if query else 1
        answers = self.pages.get(path)
        if answers is None:
            return 404, {}, b'{"message": "Not Found"}'
        if not isinstance(answers, list) or not answers or not isinstance(answers[0], list):
            return 200, {}, json.dumps(answers).encode()
        link = {}
        if page < len(answers):
            link = {"Link": f'<https://api.github.com/repositories/1/x?{query}&page={page + 1}>; rel="next"'}
        return 200, link, json.dumps(answers[page - 1]).encode()


def client(pages, token=lambda: SECRET):
    fake = FakeGitHub(pages)
    return GitHubClient("IEZhu/Agents", token, transport=fake), fake


def test_get_all_follows_pages_on_the_checked_path():
    api, fake = client({"/repos/IEZhu/Agents/issues": [[{"number": 1}], [{"number": 2}], [{"number": 3}]]})
    assert [item["number"] for item in api.get_all("/repos/IEZhu/Agents/issues", {"per_page": 1})] == [1, 2, 3]
    assert all(url.startswith("https://api.github.com/repos/IEZhu/Agents/issues?") for url, _ in fake.calls)
    assert all(headers["Authorization"] == f"Bearer {SECRET}" for _, headers in fake.calls)


def test_requests_outside_the_repository_are_refused():
    api, fake = client({})
    for path in ("/repos/IEZhu/Other/issues", "/user", "/repos/IEZhu/Agents/../Other/issues", "/graphql"):
        with pytest.raises(ValueError):
            api.get(path)
    assert fake.calls == []


def test_the_client_has_no_write_methods():
    assert not any(hasattr(GitHubClient, name) for name in ("post", "patch", "put", "delete", "request"))


def test_errors_and_records_never_contain_the_token():
    api, _ = client({})
    with pytest.raises(GitHubError) as raised:
        api.get("/repos/IEZhu/Agents/issues/1")
    assert raised.value.status == 404 and SECRET not in str(raised.value)
    assert SECRET not in repr(api) and SECRET not in json.dumps(api.requests)


def test_the_token_is_read_once_per_client():
    reads = []

    def token():
        reads.append(1)
        return SECRET
    api, _ = client({"/repos/IEZhu/Agents/pulls": [[]]}, token)
    api.get_all("/repos/IEZhu/Agents/pulls")
    api.get_all("/repos/IEZhu/Agents/pulls")
    assert len(reads) == 1


def test_network_failure_is_reported_without_details(monkeypatch):
    def unreachable(*args, **kwargs):
        raise github.URLError("name resolution failed")
    monkeypatch.setattr(github, "urlopen", unreachable)
    with pytest.raises(github.NetworkError, match="unreachable"):
        github.urllib_transport("https://api.github.com/repos/IEZhu/Agents", {"Authorization": "Bearer " + SECRET})


def test_keychain_provider_reads_the_named_item_and_never_echoes_it():
    calls = []

    def security(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=SECRET + "\n", stderr="")
    assert keychain_token(runner=security) == SECRET
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/security", "find-generic-password", "-s", "agents-autodev-github", "-a", "WonderMr", "-w"]
    assert kwargs["capture_output"] is True


@pytest.mark.parametrize("answer", [SimpleNamespace(returncode=44, stdout="", stderr="could not be found " + SECRET),
                                    SimpleNamespace(returncode=0, stdout="  \n", stderr="")])
def test_keychain_failure_names_the_item_only(answer):
    with pytest.raises(TokenUnavailable) as raised:
        keychain_token(runner=lambda argv, **kwargs: answer)
    assert "agents-autodev-github" in str(raised.value) and SECRET not in str(raised.value)


def test_keychain_runner_crash_is_a_token_failure():
    def crash(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)
    with pytest.raises(TokenUnavailable, match="TimeoutExpired"):
        keychain_token(runner=crash)


def test_provider_from_configuration_uses_its_keychain_item(monkeypatch):
    seen = []
    monkeypatch.setattr(github, "keychain_token", lambda service: seen.append(service) or SECRET)
    provider = github.token_provider({"github": {"token": "keychain:agents-autodev-github"}})
    assert provider() == SECRET and seen == ["agents-autodev-github"]


def test_snapshot_reads_only_what_the_gate_needs():
    base = "/repos/IEZhu/Agents"
    pull = {"number": 410, "state": "open", "labels": [{"name": "autodev"}],
            "head": {"ref": "claude/autodev-401-x", "sha": "a", "repo": {"full_name": "IEZhu/Agents"}}}
    other = {"number": 411, "state": "open", "labels": [], "head": {"ref": "feature", "repo": {"full_name": "IEZhu/Agents"}}}
    api, fake = client({
        f"{base}/issues": [[{"number": 401, "labels": [{"name": "autodev:in-progress"}]},
                            {"number": 405, "pull_request": {}, "labels": [{"name": "autodev:in-progress"}]}]],
        f"{base}/issues/401/events": [[{"event": "labeled"}]],
        f"{base}/pulls": [[pull, other]],
        f"{base}/pulls/410": {**pull, "mergeable": True},
        f"{base}/issues/410/comments": [[]],
        f"{base}/pulls/410/reviews": [[]],
        f"{base}/pulls/410/comments": [[]],
    })
    snapshot = fetch_snapshot(api, NOW)
    assert [issue["number"] for issue in snapshot["claimed_issues"]] == [401]
    assert set(snapshot["pull_details"]) == {"410"} and snapshot["fetched_at"] == NOW.isoformat()
    paths = {url.removeprefix(github.API).partition("?")[0] for url, _ in fake.calls}
    assert f"{base}/pulls/411" not in paths
    assert all(path.startswith(base + "/") for path in paths)


# --- redirects (review F6): a real local server ------------------------------------------------------

class _Recorder(BaseHTTPRequestHandler):
    """Answers every GET with ``answer(path)`` and records the request's headers."""

    def do_GET(self):  # noqa: N802 (http.server's name)
        self.server.seen.append((self.path, dict(self.headers)))
        status, headers = self.server.answer(self.path)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        body = b"[]"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def http_server(answer):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.seen, server.answer = [], answer
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_and_the_token_never_reaches_its_target(status):
    with http_server(lambda path: (200, {})) as target, \
            http_server(lambda path: (status, {"Location": f"http://127.0.0.1:{target.server_port}/stolen"})) as origin:
        local = f"http://127.0.0.1:{origin.server_port}"

        def transport(url, headers):
            return github.urllib_transport(url.replace(github.API, local), headers)
        api = GitHubClient("IEZhu/Agents", lambda: SECRET, transport=transport)
        with pytest.raises(GitHubError) as raised:
            api.get_all("/repos/IEZhu/Agents/issues")
        assert raised.value.status == status and "redirect" in str(raised.value)
        assert SECRET not in str(raised.value)
        assert [path for path, _ in origin.seen] == ["/repos/IEZhu/Agents/issues"]
        assert origin.seen[0][1]["Authorization"] == f"Bearer {SECRET}"
    assert target.seen == []  # not followed, so nothing, and no token, reached another host or path


def test_the_transport_returns_a_redirect_without_following_it():
    with http_server(lambda path: (200, {})) as target, \
            http_server(lambda path: (307, {"Location": f"http://127.0.0.1:{target.server_port}/x"})) as origin:
        status, headers, _ = github.urllib_transport(f"http://127.0.0.1:{origin.server_port}/repos/IEZhu/Agents/x",
                                                     {"Authorization": f"Bearer {SECRET}", "Accept": "x"})
    assert status == 307 and target.seen == []


def test_snapshot_reads_the_issue_a_merged_pr_closed():
    base = "/repos/IEZhu/Agents"
    merged = {"number": 411, "state": "closed", "merged_at": "2026-10-12T06:00:00Z", "labels": [{"name": "autodev"}],
              "head": {"ref": "claude/autodev-402-done", "repo": {"full_name": "IEZhu/Agents"}}}
    other = {"number": 412, "state": "closed", "labels": [], "head": {"ref": "claude/autodev-403-x",
                                                                       "repo": {"full_name": "IEZhu/Agents"}}}
    issue = {"number": 402, "state": "closed", "labels": [{"name": "autodev:in-progress"}]}
    api, fake = client({f"{base}/issues": [[]], f"{base}/pulls": [[merged, other]], f"{base}/issues/402": issue})
    snapshot = fetch_snapshot(api, NOW)
    assert snapshot["linked_issues"] == {"402": issue}
    paths = [url.removeprefix(github.API).partition("?")[0] for url, _ in fake.calls]
    assert f"{base}/issues/402" in paths and f"{base}/issues/403" not in paths  # 412 is not self-development
