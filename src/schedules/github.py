"""A read-only GitHub REST client for the gate, and the snapshot it reads.

The client can only ``GET`` paths under ``/repos/<repository>/`` and follows ``Link: rel="next"``
pages on ``api.github.com``. Its token comes from a provider called once per client; the default
provider reads the fine-grained token from the macOS keychain item ``agents-autodev-github``
(account ``WonderMr``), never the ``gh`` token. The token is kept in memory only: it is never
printed, logged, put into an exception or written to a record.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import subprocess
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .gate import CLAIM_LABEL, is_self_development

API = "https://api.github.com"
KEYCHAIN_ACCOUNT = "WonderMr"
KEYCHAIN_SERVICE = "agents-autodev-github"
SECURITY = "/usr/bin/security"
TIMEOUT = 30
MAX_PAGES = 20
CLOSED_PULLS_PAGE = 50

TokenProvider = Callable[[], str]
# (url, headers) -> (HTTP status, response headers, body)
Transport = Callable[[str, Mapping[str, str]], tuple[int, Mapping[str, str], bytes]]
_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class TokenUnavailable(RuntimeError):
    """The token could not be read; the message never contains it."""


class GitHubError(RuntimeError):
    """GitHub answered with an error status."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class NetworkError(RuntimeError):
    """GitHub could not be reached."""


def keychain_token(service: str = KEYCHAIN_SERVICE, account: str = KEYCHAIN_ACCOUNT, *,
                   runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """The password of the keychain item; ``TokenUnavailable`` without echoing any output."""
    try:
        result = runner([SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise TokenUnavailable(f"the keychain item {service} (account {account}) could not be read: "
                               f"{type(error).__name__}") from None
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        raise TokenUnavailable(f"the keychain item {service} (account {account}) could not be read "
                               f"(security exit code {result.returncode})")
    return token


def token_provider(config: Mapping) -> TokenProvider:
    """The provider named by ``github.token`` (``keychain:<service>``) in ``autodev.json``."""
    service = str(config["github"]["token"]).removeprefix("keychain:")
    return lambda: keychain_token(service)


def urllib_transport(url: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 (the URL is checked by the client)
            return response.status, dict(response.headers), response.read()
    except HTTPError as error:
        return error.code, dict(error.headers or {}), error.read() or b""
    except (URLError, OSError) as error:
        raise NetworkError(f"GitHub is unreachable: {type(error).__name__}") from None


class GitHubClient:
    """``GET`` only, only under ``/repos/<repository>/``."""

    def __init__(self, repository: str, token: TokenProvider, *, transport: Transport | None = None,
                 max_pages: int = MAX_PAGES):
        self.repository = repository
        self._token_provider = token
        self._token: str | None = None
        self._transport = transport or urllib_transport
        self.max_pages = max_pages
        self.requests: list[str] = []  # paths and queries only, for the run record

    def __repr__(self) -> str:
        return f"GitHubClient({self.repository!r})"

    def _check(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != urlsplit(API).netloc or ".." in parts.path or \
                not parts.path.lower().startswith(f"/repos/{self.repository}/".lower()):
            raise ValueError(f"refusing a GitHub request outside {self.repository}: {parts.path}")
        return url

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            self._token = self._token_provider()
        return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agents-core-autodev"}

    def _fetch(self, url: str) -> tuple[Any, Mapping[str, str]]:
        self._check(url)
        parts = urlsplit(url)
        self.requests.append(parts.path + (f"?{parts.query}" if parts.query else ""))
        status, headers, body = self._transport(url, self._headers())
        if status >= 400:
            raise GitHubError(f"GET {parts.path}: HTTP {status}", status)
        try:
            return json.loads(body or b"null"), headers
        except ValueError:
            raise GitHubError(f"GET {parts.path}: the answer is not JSON", status) from None

    def url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            raise ValueError("a GitHub path starts with /")
        return API + path + (f"?{urlencode(params)}" if params else "")

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._fetch(self.url(path, params))[0]

    def get_all(self, path: str, params: Mapping[str, Any] | None = None) -> list:
        """Every page of a list endpoint, up to ``max_pages``.

        Only the query of a ``rel="next"`` link is used, on ``path`` itself: GitHub may name the
        next page by repository ID (``/repositories/<id>/…``), and the path stays the one checked.
        """
        url: str | None = self.url(path, params)
        items: list = []
        for _ in range(self.max_pages):
            data, headers = self._fetch(url)
            if not isinstance(data, list):
                raise GitHubError(f"GET {path}: expected a list", 200)
            items.extend(data)
            link = next((value for key, value in headers.items() if key.lower() == "link"), "")
            match = _NEXT.search(link or "")
            if not match:
                return items
            url = API + path + "?" + urlsplit(match[1]).query
        raise GitHubError(f"GET {path}: more than {self.max_pages} pages", 200)


def fetch_snapshot(client: GitHubClient, now: datetime) -> dict:
    """What the gate reads (`src.schedules.gate`), from read-only requests."""
    base = f"/repos/{client.repository}"
    claimed = [issue for issue in client.get_all(f"{base}/issues", {"labels": CLAIM_LABEL, "state": "open",
                                                                     "per_page": 100})
               if "pull_request" not in issue]
    events = {str(issue["number"]): client.get_all(f"{base}/issues/{issue['number']}/events", {"per_page": 100})
              for issue in claimed}
    pulls = client.get_all(f"{base}/pulls", {"state": "open", "per_page": 100})
    pulls += client.get(f"{base}/pulls", {"state": "closed", "sort": "updated", "direction": "desc",
                                          "per_page": CLOSED_PULLS_PAGE}) or []
    snapshot = {"repository": client.repository, "fetched_at": now.isoformat(timespec="seconds"),
                "claimed_issues": claimed, "issue_events": events, "pulls": pulls,
                "pull_details": {}, "issue_comments": {}, "reviews": {}, "review_comments": {}}
    for pull in pulls:
        if pull.get("state") != "open" or not is_self_development(pull, client.repository):
            continue
        number = str(pull["number"])
        snapshot["pull_details"][number] = client.get(f"{base}/pulls/{number}")
        snapshot["issue_comments"][number] = client.get_all(f"{base}/issues/{number}/comments", {"per_page": 100})
        snapshot["reviews"][number] = client.get_all(f"{base}/pulls/{number}/reviews", {"per_page": 100})
        snapshot["review_comments"][number] = client.get_all(f"{base}/pulls/{number}/comments", {"per_page": 100})
    return snapshot
