"""GitHub account for library sync: device-flow sign-in, the token in the OS secret store, REST calls (#166).

Setup must not depend on the ``gh`` CLI, so Agents-Core signs in to the GitHub API
itself and uses it to create the library repository, check that it is private and
manage each machine's deploy key. The token never serves git transport: git runs on
the deploy key, so a revoked token only marks the account "reconnect needed" while
sync goes on.

* Sign-in is the OAuth device flow of the Agents-Core OAuth App, which needs no
  client secret, so none ships. ``DeviceFlow.start`` returns the code the user
  enters at ``verification_uri``; ``poll`` makes one request, which the web UI
  repeats through the daemon; ``wait`` blocks for the terminal wizard.
  ``AGENTS_GITHUB_CLIENT_ID`` and ``AGENTS_GITHUB_HOST`` select another OAuth App
  or a GitHub Enterprise Server.
* The token has the ``repo`` scope, which creating a private repository needs. It
  lives in the OS secret store: the macOS Keychain through ``/usr/bin/security``,
  the Windows Credential Manager through ``ctypes``, the Secret Service through
  ``secret-tool`` when that works; otherwise, or when that store refuses the token,
  in a private file in the state directory, which the status reports as a warning.
* ``GitHubAccount`` is what callers use: status, sign-in, a client, "reconnect
  needed" after a 401 and "Forget account". Its ``github-account.json`` holds no
  secret. The caller passes the private state directory and the per-installation
  secret name; this module never computes installation paths.

Security rules:

* The token and the device code never appear in the argv of a child process (a
  ``ps`` listing shows it) or in its environment, in logs, exception messages or
  ``repr()``: ``security -i`` and ``secret-tool store`` read them from stdin.
* The token goes only to the API origin it is used with: a redirect to another
  origin drops the ``Authorization`` header, a pagination link must stay on that
  origin, and a token signed in on one host is never sent to another.
* HTTPS only, except to a loopback address (the tests' fake GitHub).

Standard library only: ``auto-update`` skips targets that change dependency manifests.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, NamedTuple, Protocol
import urllib.error
import urllib.parse
import urllib.request
import uuid

from src.file_lock import file_lock
from src.user_library import MARKER

DEFAULT_HOST = "github.com"
# Client ID of the Agents-Core OAuth App. The owner fills it in after registering
# the app (#166); until then sign-in needs AGENTS_GITHUB_CLIENT_ID.
DEFAULT_CLIENT_ID = ""
SCOPE = "repo"  # GitHub requires it to create a private repository
API_VERSION = "2022-11-28"
USER_AGENT = "Agents-Core"
TIMEOUT = 30
STORE_TIMEOUT = 30  # an OS secret store may wait for the user to unlock it
DEFAULT_REPO_NAME = "agents-library"
LIBRARY_SCAN_LIMIT = 100
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
ACCOUNT_FILE = "github-account.json"
TOKEN_FILE = "github-token"
SECRET_ACCOUNT = "github-token"  # account attribute of the keychain or Secret Service item
SECURITY = "/usr/bin/security"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PAGES = 100
POLL_NETWORK_RETRIES = 3
# Checking a new token right after the device flow: a short outage must not lose it.
SIGN_IN_ATTEMPTS = 3
SIGN_IN_RETRY_DELAY = 5
MAX_RATE_LIMIT_WAIT = 60

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?")
_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,100}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # safe unquoted in a `security -i` line
_TOKEN = re.compile(r"[!-~]{8,1024}")  # printable ASCII without spaces: safe in a header
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_KEY_TYPE = re.compile(r"[a-z0-9][a-z0-9@.-]{2,63}")
_LINK = re.compile(r'<([^>]*)>\s*;\s*rel="([^"]*)"')
_OAUTH_ERRORS = {
    "expired_token": "The sign-in code expired before it was entered; start the sign-in again.",
    "access_denied": "The sign-in was cancelled on GitHub.",
    "device_flow_disabled": "The Agents-Core OAuth App does not allow device sign-in; its owner must "
                            "enable Device Flow in the app's settings.",
    "incorrect_client_credentials": "GitHub does not know this OAuth App client ID; check "
                                    "AGENTS_GITHUB_CLIENT_ID.",
    "incorrect_device_code": "GitHub does not recognize this sign-in attempt; start the sign-in again.",
    "unsupported_grant_type": "GitHub refused the device sign-in request (unsupported grant type).",
}
_ORG_APPROVAL = (" The organization that owns it may need to approve the Agents-Core OAuth App: an "
                 "organization owner allows it under the organization's Settings > Third-party Access, "
                 "or you request it under your Settings > Applications > Authorized OAuth Apps.")

logger = logging.getLogger(__name__)


class GitHubError(Exception):
    """A failure with a stable ``code`` for callers and a ``message`` for the user.

    ``auth``: no account is connected, or GitHub refused the token (revoked or
    expired): a reconnect is needed. ``org_approval``: an organization restricts
    OAuth Apps or requires SAML sign-on. ``rate_limited``: ``reset_at`` (epoch
    seconds) says when to retry. Device sign-in failures carry GitHub's error names
    (``expired_token``, ``access_denied``, ``device_flow_disabled``, ...), unknown
    ones ``oauth``. Others: ``no_client_id``, ``insufficient_scope``, ``forbidden``,
    ``not_found``, ``exists``, ``invalid``, ``public_repo``, ``network`` (also
    GitHub's 5xx), ``http``, ``unexpected_response``, ``config`` and ``storage``.
    Messages never contain the token or the device code.
    """

    def __init__(self, code: str, message: str, *, status: int | None = None,
                 reset_at: float | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.reset_at = reset_at

    def __repr__(self) -> str:
        return f"GitHubError({self.code!r}, {self.message!r})"


# --- hosts and HTTP ----------------------------------------------------------------

def github_host() -> str:
    """``AGENTS_GITHUB_HOST`` (a host name, optionally with ``https://`` or a port), else github.com."""
    configured = os.environ.get("AGENTS_GITHUB_HOST", "").strip()
    host = re.sub(r":443$", "", re.sub(r"^https://", "", configured.lower()).rstrip("/"))
    if not host:
        return DEFAULT_HOST
    if not _HOST.fullmatch(host):
        raise GitHubError("config", "AGENTS_GITHUB_HOST must be a host name such as "
                                    f"github.example.com, not {configured!r}")
    return host


def web_base(host: str) -> str:
    return f"https://{host}"


def api_base(host: str) -> str:
    """The REST API of ``host``: api.github.com, ``api.<host>`` on GHE.com, else GitHub Enterprise Server's."""
    if host == DEFAULT_HOST:
        return "https://api.github.com"
    if host.endswith(".ghe.com"):
        return f"https://api.{host}"
    return f"https://{host}/api/v3"


def default_client_id() -> str:
    """``AGENTS_GITHUB_CLIENT_ID``, else the Agents-Core OAuth App's (empty until it is registered)."""
    return os.environ.get("AGENTS_GITHUB_CLIENT_ID", "").strip() or DEFAULT_CLIENT_ID


def _check_base(url: str) -> str:
    """``url`` without a trailing slash: HTTPS, or HTTP to a loopback address."""
    try:
        parts = urllib.parse.urlsplit(url)
        secure = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK)
        valid = secure and parts.hostname and not parts.username and not parts.query and not parts.fragment
    except (TypeError, ValueError):  # e.g. an unclosed IPv6 bracket
        valid = False
    if not valid:
        raise GitHubError("config", f"Not an HTTPS base URL: {url!r}")
    return url.rstrip("/")


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, carrying the ``Authorization`` header only to the origin it was sent to.

    The header is added unredirected, so urllib drops it on every redirect; GitHub
    redirects a renamed repository within the API, where it is added back.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        authorization = req.unredirected_hdrs.get("Authorization")
        if new is not None and authorization and _origin(new.full_url) == _origin(req.full_url):
            new.add_unredirected_header("Authorization", authorization)
        return new


def _opener(base: str) -> urllib.request.OpenerDirector:
    handlers: list = [_SameOriginRedirect()]
    if urllib.parse.urlsplit(base).hostname in _LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))  # a proxy never serves this machine's loopback
    return urllib.request.build_opener(*handlers)


def _read(response) -> bytes:
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise GitHubError("unexpected_response", "GitHub's response is too large")
    return data


def _send(opener: urllib.request.OpenerDirector, request: urllib.request.Request,
          timeout: float) -> tuple[int, Any, bytes]:
    """``(status, headers, body)``; an HTTP error status is returned, an unreachable host raises ``network``."""
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.headers, _read(response)
    except urllib.error.HTTPError as error:
        try:
            body = _read(error)
        except (OSError, http.client.HTTPException):
            body = b""
        finally:
            error.close()
        return error.code, error.headers, body
    except (OSError, http.client.HTTPException) as error:  # URLError, timeouts, resets
        reason = getattr(error, "reason", None) or error
        host = urllib.parse.urlsplit(request.full_url).netloc
        raise GitHubError("network", f"Cannot reach {host}: {reason}") from None


def _json(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _redact(text: str, *secrets: str | None) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _github_message(payload, *secrets: str | None) -> str:
    """GitHub's ``message`` and the messages of its ``errors``, for an error text.

    ``secrets`` are redacted before the text is shortened, so no cut leaves part of one.
    """
    if not isinstance(payload, dict):
        return ""
    parts = [payload.get("message")]
    errors = payload.get("errors")
    for item in errors if isinstance(errors, list) else ():
        parts.append((item.get("message") or item.get("code")) if isinstance(item, dict) else item)
    return _redact("; ".join(str(part) for part in parts if part), *secrets)[:300]


def _rate_limit(status: int, headers, message: str) -> GitHubError | None:
    """``rate_limited`` for a 403 or 429 that GitHub marks as a primary or secondary rate limit."""
    if status not in (403, 429):
        return None
    retry_after = (headers.get("Retry-After") or "").strip()
    remaining = (headers.get("X-RateLimit-Remaining") or "").strip()
    reset = (headers.get("X-RateLimit-Reset") or "").strip()
    if status == 403 and not retry_after and remaining != "0" and "rate limit" not in message.lower():
        return None
    if retry_after.isdigit():
        reset_at = time.time() + int(retry_after)
    elif remaining == "0" and reset.isdigit():
        reset_at = float(reset)
    else:
        reset_at = time.time() + 60  # GitHub asks for at least a minute after a secondary limit
    try:
        when = datetime.fromtimestamp(reset_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):  # an absurd header must not hide the rate limit
        when, reset_at = "a while", None
    return GitHubError("rate_limited", f"GitHub's API rate limit was reached; try again after {when}.",
                       status=status, reset_at=reset_at)


def _check_token(token: str) -> str:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise GitHubError("invalid", "This is not a GitHub access token")
    return token


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise GitHubError("config", f"Not a secret store name of letters, digits, '.', '_' and '-': {name!r}")
    return name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- OAuth device flow ---------------------------------------------------------------

@dataclass
class DeviceCode:
    """One sign-in attempt. ``device_code`` is a secret, so it stays out of ``repr``.

    ``poll`` raises ``interval`` after a ``slow_down``; a caller that polls step by
    step waits ``interval`` seconds between polls.
    """
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int

    def public(self) -> dict:
        """What the user may see: everything but the device code."""
        return {"user_code": self.user_code, "verification_uri": self.verification_uri,
                "interval": self.interval, "expires_in": self.expires_in}


@dataclass(frozen=True)
class PollResult:
    """``state`` is ``pending``, ``slow_down`` or ``authorized`` (then ``token`` is set)."""
    state: str
    interval: int
    token: str | None = field(default=None, repr=False)


def _positive(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class DeviceFlow:
    """The OAuth device flow of one OAuth App against GitHub's web host (no client secret)."""

    def __init__(self, client_id: str | None = None, *, web_url: str | None = None,
                 timeout: float = TIMEOUT):
        self.client_id = default_client_id() if client_id is None else client_id
        self.web_url = _check_base(web_url or web_base(github_host()))
        self.timeout = timeout
        self._opener = _opener(self.web_url)

    def _checked_client_id(self) -> str:
        if not self.client_id:
            raise GitHubError("no_client_id", "No OAuth App client ID is configured for GitHub sign-in: set "
                                              "AGENTS_GITHUB_CLIENT_ID to the client ID of an OAuth App with "
                                              "Device Flow enabled, or add the deploy key manually.")
        if not _CLIENT_ID.fullmatch(self.client_id):
            raise GitHubError("config", "AGENTS_GITHUB_CLIENT_ID is not an OAuth App client ID")
        return self.client_id

    def start(self) -> DeviceCode:
        """Ask GitHub for a code that the user enters at ``verification_uri``."""
        payload = self._post("/login/device/code", {"client_id": self._checked_client_id(), "scope": SCOPE})
        if isinstance(payload.get("error"), str):
            raise _oauth_error(payload)
        device = DeviceCode(payload.get("device_code"), payload.get("user_code"),
                            payload.get("verification_uri"), payload.get("interval", 5),
                            payload.get("expires_in", 900))
        texts = (device.device_code, device.user_code, device.verification_uri)
        if not all(isinstance(text, str) and text for text in texts) \
                or not (_positive(device.interval) and _positive(device.expires_in)):
            raise GitHubError("unexpected_response", "GitHub returned an unexpected device code")
        return device

    def poll(self, device: DeviceCode) -> PollResult:
        """One request: whether the user has entered the code yet."""
        payload = self._post("/login/oauth/access_token",
                             {"client_id": self._checked_client_id(), "device_code": device.device_code,
                              "grant_type": DEVICE_GRANT})
        error = payload.get("error")
        if error == "authorization_pending":
            return PollResult("pending", device.interval)
        if error == "slow_down":
            proposed = payload.get("interval")
            device.interval = max(device.interval + 5, proposed if _positive(proposed) else 0)
            return PollResult("slow_down", device.interval)
        if error is not None:
            raise _oauth_error(payload, device.device_code)
        token = payload.get("access_token")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise GitHubError("unexpected_response", "GitHub returned no usable access token")
        granted = payload.get("scope")
        if isinstance(granted, str) and SCOPE not in re.split(r"[,\s]+", granted):
            raise GitHubError("insufficient_scope", "The authorization lacks the repo scope that creating a "
                                                    "private repository needs; sign in again and approve it.")
        return PollResult("authorized", device.interval, token)

    def wait(self, device: DeviceCode, *, sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> str:
        """Poll every ``interval`` seconds until the user enters the code; returns the token.

        Gives up with ``expired_token`` after ``expires_in`` seconds. A few network
        failures in a row are retried at the next interval.
        """
        deadline = clock() + device.expires_in
        failures = 0
        while True:
            sleep(device.interval)
            if clock() >= deadline:
                raise GitHubError("expired_token", _OAUTH_ERRORS["expired_token"])
            try:
                result = self.poll(device)
            except GitHubError as error:
                failures += 1
                if error.code != "network" or failures >= POLL_NETWORK_RETRIES:
                    raise
                continue
            failures = 0
            if result.token is not None:
                return result.token

    def _post(self, path: str, fields: dict) -> dict:
        """POST a form and return GitHub's JSON object.

        Rate limits, a missing endpoint and server errors are mapped first, whatever the
        body says. Only then is a JSON object an OAuth answer: on 200, where GitHub also
        sends errors such as ``authorization_pending``, or an ``error`` on 400 or 401.
        """
        request = urllib.request.Request(
            self.web_url + path, data=urllib.parse.urlencode(fields).encode("ascii"), method="POST",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT,
                     "Content-Type": "application/x-www-form-urlencoded"})
        status, headers, raw = _send(self._opener, request, self.timeout)
        payload = _json(raw)
        payload = payload if isinstance(payload, dict) else None
        text = str(payload.get("error_description") or payload.get("error") or "") if payload else ""
        limited = _rate_limit(status, headers, text)
        if limited is not None:
            raise limited
        if status == 404:
            raise GitHubError("not_found", f"{self.web_url} offers no device sign-in for this OAuth App; "
                                           "check AGENTS_GITHUB_HOST and AGENTS_GITHUB_CLIENT_ID.", status=status)
        if status >= 500:
            raise GitHubError("network", f"GitHub is unavailable (HTTP {status}); try again later.",
                              status=status)
        if payload is not None and (status == 200 or (status in (400, 401)
                                                      and isinstance(payload.get("error"), str))):
            return payload
        if status == 200:
            raise GitHubError("unexpected_response", f"{self.web_url} did not answer the device sign-in")
        raise GitHubError("oauth", f"GitHub refused the device sign-in (HTTP {status}).", status=status)


def _oauth_error(payload: dict, secret: str | None = None) -> GitHubError:
    error = str(payload.get("error"))
    if error in _OAUTH_ERRORS:
        return GitHubError(error, _OAUTH_ERRORS[error])
    detail = payload.get("error_description")
    text = f"GitHub refused the sign-in: {error}" + (f" ({detail})" if isinstance(detail, str) else "")
    return GitHubError("oauth", _redact(text, secret)[:300])


# --- REST API ----------------------------------------------------------------------

@dataclass(frozen=True)
class RepoInfo:
    """What sync needs to know of a repository. ``visibility`` is private, public or internal."""
    full_name: str
    private: bool
    visibility: str
    ssh_url: str
    default_branch: str | None
    archived: bool = False
    html_url: str | None = None

    @property
    def owner(self) -> str:
        return self.full_name.split("/")[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/")[1]


@dataclass(frozen=True)
class DeployKey:
    """A deploy key as GitHub lists it; ``key`` is ``<type> <base64>``, without a comment."""
    id: int
    title: str
    key: str
    read_only: bool
    created_at: str | None = None


@dataclass(frozen=True)
class LibraryScan:
    """Libraries found among the ``checked`` repositories; ``truncated``: more were not checked."""
    libraries: tuple[RepoInfo, ...]
    checked: int
    truncated: bool


class _Reply(NamedTuple):
    status: int
    headers: Any
    data: Any


def _repo_path(full_name: str) -> str:
    owner, _, name = full_name.partition("/") if isinstance(full_name, str) else ("", "", "")
    if not (_OWNER.fullmatch(owner) and _REPO.fullmatch(name)) or name in (".", ".."):
        raise GitHubError("invalid", f"Not a repository name of the form owner/name: {full_name!r}")
    return f"/repos/{owner}/{name}"


def _repo_info(payload) -> RepoInfo:
    data = payload if isinstance(payload, dict) else {}
    full_name, private, ssh_url = data.get("full_name"), data.get("private"), data.get("ssh_url")
    visibility = data.get("visibility") or ("private" if private is True else "public")
    if not (isinstance(full_name, str) and full_name.count("/") == 1 and isinstance(private, bool)
            and isinstance(visibility, str) and isinstance(ssh_url, str)):
        raise GitHubError("unexpected_response", "GitHub returned an unexpected repository description")
    branch, html_url = data.get("default_branch"), data.get("html_url")
    return RepoInfo(full_name, private, visibility, ssh_url, branch if isinstance(branch, str) else None,
                    data.get("archived") is True, html_url if isinstance(html_url, str) else None)


def _is_private(info: RepoInfo) -> bool:
    return info.private and info.visibility == "private"


def _deploy_key(payload) -> DeployKey:
    data = payload if isinstance(payload, dict) else {}
    key_id, title, key, read_only = data.get("id"), data.get("title"), data.get("key"), data.get("read_only")
    if not (_positive(key_id) and isinstance(title, str) and isinstance(key, str) and isinstance(read_only, bool)):
        raise GitHubError("unexpected_response", "GitHub returned an unexpected deploy key")
    created = data.get("created_at")
    return DeployKey(key_id, title, key, read_only, created if isinstance(created, str) else None)


def parse_public_key(text: str) -> tuple[str, str]:
    """``(type, base64)`` of an OpenSSH public key line; the comment is ignored."""
    parts = text.split() if isinstance(text, str) else []
    if len(parts) >= 2 and _KEY_TYPE.fullmatch(parts[0]):
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except (binascii.Error, ValueError):
            blob = b""
        kind = parts[0].encode("ascii")
        # The blob starts with its own type name, prefixed by its length.
        if blob[:4] == len(kind).to_bytes(4, "big") and blob[4:4 + len(kind)] == kind:
            return parts[0], parts[1]
    raise GitHubError("invalid", "Not an OpenSSH public key")


def _next_link(header: str | None) -> str | None:
    for url, rel in _LINK.findall(header or ""):
        if "next" in rel.split():
            return url
    return None


class GitHubClient:
    """REST calls of the GitHub API with one token.

    ``login`` (the signed-in account, which ``user()`` also sets) tells an
    organization's repository from the user's own in a 403. ``on_unauthorized``
    runs on every 401.
    """

    def __init__(self, token: str, *, api_url: str | None = None, login: str | None = None,
                 timeout: float = TIMEOUT, on_unauthorized: Callable[[], None] | None = None):
        self._token = _check_token(token)
        self.api_url = _check_base(api_url or api_base(github_host()))
        self.login = login
        self.timeout = timeout
        self._on_unauthorized = on_unauthorized
        self._opener = _opener(self.api_url)

    def __repr__(self) -> str:
        return f"GitHubClient(api_url={self.api_url!r}, login={self.login!r})"

    def user(self) -> str:
        """The signed-in account's login."""
        payload = self._call("GET", "/user").data
        login = payload.get("login") if isinstance(payload, dict) else None
        if not isinstance(login, str) or not login:
            raise GitHubError("unexpected_response", "GitHub did not return the account's login")
        self.login = login
        return login

    def create_library(self, name: str = DEFAULT_REPO_NAME, *,
                       description: str = "Agents-Core personal flow library") -> RepoInfo:
        """Create an empty private repository for the library on the signed-in account."""
        if not isinstance(name, str) or not _REPO.fullmatch(name) or name in (".", ".."):
            raise GitHubError("invalid", f"Not a repository name: {name!r}")
        body = {"name": name, "description": description, "private": True, "has_issues": False,
                "has_wiki": False, "has_projects": False, "auto_init": False}
        try:
            return _repo_info(self._call("POST", "/user/repos", body=body).data)
        except GitHubError as error:
            if error.code != "exists":
                raise
            raise GitHubError("exists", f"This account already has a repository named {name}; choose another "
                                        "name, or pick it from the list if it holds your library.",
                              status=error.status) from None

    def libraries(self, limit: int = LIBRARY_SCAN_LIMIT) -> LibraryScan:
        """The account's private repositories with the library marker at the root of their default branch.

        The most recently pushed repositories are checked first, at most ``limit`` of
        them (one request each); ``truncated`` says that more were not checked.
        """
        query = urllib.parse.urlencode({"visibility": "private", "affiliation": "owner",
                                        "sort": "pushed", "per_page": 100})
        found, checked, truncated = [], 0, False
        for entry in self._items(f"/user/repos?{query}"):
            if checked >= limit:
                truncated = True
                break
            info = _repo_info(entry)
            if not _is_private(info):
                continue
            checked += 1
            reply = self._call("GET", f"{_repo_path(info.full_name)}/contents/{MARKER}",
                               repo=info.full_name, allow=(404, 409))  # 404 or 409: no marker or empty
            if reply.status == 200 and isinstance(reply.data, dict) and reply.data.get("type") == "file":
                found.append(info)
        return LibraryScan(tuple(found), checked, truncated)

    def repository(self, full_name: str) -> RepoInfo:
        """``owner/name``'s privacy, SSH URL and default branch."""
        return _repo_info(self._call("GET", _repo_path(full_name), repo=full_name).data)

    def require_private(self, full_name: str) -> RepoInfo:
        """The repository, or ``public_repo`` unless only its owner and collaborators can read it."""
        info = self.repository(full_name)
        if info.visibility == "internal":
            raise GitHubError("public_repo", f"{info.full_name} is internal: every member of the enterprise "
                                             "can read it. Use a private repository for the library.")
        if not _is_private(info):
            raise GitHubError("public_repo", f"{info.full_name} is public. Agents-Core syncs the library "
                                             "only to a private repository.")
        return info

    def deploy_keys(self, full_name: str) -> list[DeployKey]:
        return [_deploy_key(entry) for entry in
                self._items(f"{_repo_path(full_name)}/keys?per_page=100", repo=full_name)]

    def add_deploy_key(self, full_name: str, public_key: str, label: str) -> DeployKey:
        """Add this machine's public key with write access, titled ``Agents-Core <label>``."""
        key_type, blob = parse_public_key(public_key)
        label = label.strip() if isinstance(label, str) else ""
        if not label or len(label) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in label):
            raise GitHubError("invalid", "A machine label must be 1-80 printable characters")
        body = {"title": f"Agents-Core {label}", "key": f"{key_type} {blob}", "read_only": False}
        try:
            return _deploy_key(self._call("POST", f"{_repo_path(full_name)}/keys", body=body,
                                          repo=full_name).data)
        except GitHubError as error:
            if error.code != "exists":
                raise
            raise GitHubError("exists", "This key is already in use: GitHub accepts a key once, as a deploy key "
                                        "of one repository or as a user key.", status=error.status) from None

    def delete_deploy_key(self, full_name: str, key_id: int) -> None:
        if not _positive(key_id):
            raise GitHubError("invalid", f"Not a deploy key ID: {key_id!r}")
        self._call("DELETE", f"{_repo_path(full_name)}/keys/{key_id}", repo=full_name)

    def find_deploy_key(self, full_name: str, public_key: str) -> DeployKey | None:
        """The deploy key with the same type and key material as ``public_key``, whatever its comment."""
        wanted = parse_public_key(public_key)
        for key in self.deploy_keys(full_name):
            try:
                if parse_public_key(key.key) == wanted:
                    return key
            except GitHubError:
                continue
        return None

    def _items(self, path: str, *, repo: str | None = None) -> Iterator:
        """Every item of a paginated list, following ``Link: rel="next"``."""
        url: str | None = path
        for _ in range(MAX_PAGES):
            if url is None:
                return
            reply = self._call("GET", url, repo=repo)
            if not isinstance(reply.data, list):
                raise GitHubError("unexpected_response", "GitHub returned an unexpected list")
            yield from reply.data
            url = _next_link(reply.headers.get("Link"))

    def _call(self, method: str, target: str, *, body: dict | None = None, repo: str | None = None,
              allow: tuple[int, ...] = ()) -> _Reply:
        url = target if target.startswith(("https://", "http://")) else self.api_url + target
        if _origin(url) != _origin(self.api_url):
            raise GitHubError("unexpected_response", "GitHub sent a link outside its API; it was not followed")
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                   "User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        status, response_headers, raw = _send(self._opener, request, self.timeout)
        payload = _json(raw)
        if 200 <= status < 300 or status in allow:
            return _Reply(status, response_headers, payload)
        raise self._failure(status, response_headers, payload, repo)

    def _foreign(self, repo: str | None) -> bool:
        """Whether ``repo`` belongs to another account than the signed-in one, such as an organization."""
        return bool(repo and self.login) and repo.split("/")[0].lower() != self.login.lower()

    def _failure(self, status: int, headers, payload, repo: str | None) -> GitHubError:
        message = _github_message(payload, self._token)
        if status == 401:
            if self._on_unauthorized is not None:
                try:
                    self._on_unauthorized()
                except Exception:  # the caller must still see the 401
                    logger.exception("Could not record that the GitHub account needs a reconnect")
            return GitHubError("auth", "GitHub no longer accepts this machine's authorization (it was revoked "
                                       "or expired); reconnect the GitHub account.", status=status)
        limited = _rate_limit(status, headers, message)
        if limited is not None:
            return limited
        subject = repo or "the request"
        if status == 403:
            sso = headers.get("X-GitHub-SSO")
            if sso:
                link = re.search(r"url=(\S+)", sso)
                where = f" at {link[1]}" if link else " in your GitHub settings"
                return GitHubError("org_approval", f"The organization that owns {subject} requires SAML single "
                                                   f"sign-on: authorize Agents-Core for it{where}.", status=status)
            if "oauth app access restrictions" in message.lower():
                return GitHubError("org_approval", f"GitHub refused access to {subject}.{_ORG_APPROVAL} "
                                                   f"({message})", status=status)
            # Any other 403, also on another account's repository (a collaborator lacking
            # admin rights, for example), is not known to be an approval question.
            hint = " If it belongs to an organization, the organization may need to approve the Agents-Core " \
                   "OAuth App." if repo else ""
            detail = message.rstrip(".") or "HTTP 403"
            return GitHubError("forbidden", f"GitHub refused access to {subject}: {detail}.{hint}", status=status)
        if status == 404:
            hint = _ORG_APPROVAL if self._foreign(repo) else ""
            return GitHubError("not_found", f"GitHub found no {repo or 'such resource'}, or this account cannot "
                                            f"see it.{hint}", status=status)
        if status == 422:
            code = "exists" if "already" in message.lower() else "invalid"
            return GitHubError(code, f"GitHub refused the request: {message or 'HTTP 422'}", status=status)
        if status >= 500:
            return GitHubError("network", f"GitHub is unavailable (HTTP {status}); try again later.",
                               status=status)
        return GitHubError("http", f"GitHub answered HTTP {status}: {message or 'no details'}", status=status)


# --- token storage -----------------------------------------------------------------

class SecretStore(Protocol):
    """Where the token lives. ``load`` returns None when there is none and raises ``storage`` when it cannot tell."""
    backend: str

    def load(self) -> str | None: ...

    def save(self, token: str) -> None: ...

    def delete(self) -> None: ...


def _run_tool(argv: list[str], what: str, *, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run a secret store's command-line tool; a secret travels only in ``stdin``, never in argv."""
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=STORE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise GitHubError("storage", f"{what} did not answer within {STORE_TIMEOUT} seconds") from None
    except (OSError, subprocess.SubprocessError) as error:
        raise GitHubError("storage", f"{what} is unavailable ({type(error).__name__})") from None


def _tool_error(what: str, result: subprocess.CompletedProcess, *secrets: str) -> GitHubError:
    lines = (result.stderr or "").strip().splitlines()
    detail = lines[-1] if lines else f"exit status {result.returncode}"
    return GitHubError("storage", f"{what}: {_redact(detail, *secrets)[:200]}")


class KeychainStore:
    """A generic password in the macOS keychain, through ``/usr/bin/security``.

    ``security -i`` reads the command that carries the token from stdin; the token
    goes in hex (``-X``), so no character needs quoting. ``security`` created the
    item, so ``security`` reads it back without a prompt.
    """
    backend = "macos-keychain"
    NOT_FOUND = 44  # exit status of `security` for a missing item

    def __init__(self, name: str, *, security: str = SECURITY):
        self.name = _check_name(name)
        self.security = security

    def __repr__(self) -> str:
        return f"KeychainStore({self.name!r})"

    def load(self) -> str | None:
        result = _run_tool([self.security, "find-generic-password", "-a", SECRET_ACCOUNT, "-s", self.name, "-w"],
                           "The macOS Keychain")
        if result.returncode == self.NOT_FOUND:
            return None
        if result.returncode:
            raise _tool_error("The macOS Keychain could not read the GitHub token", result)
        return result.stdout.strip() or None

    def save(self, token: str) -> None:
        token = _check_token(token)
        encoded = token.encode("ascii").hex()
        command = f"add-generic-password -U -a {SECRET_ACCOUNT} -s {self.name} -X {encoded}\n"
        result = _run_tool([self.security, "-i"], "The macOS Keychain", stdin=command)
        if result.returncode:
            raise _tool_error("The macOS Keychain did not store the GitHub token", result, token, encoded)
        if self.load() != token:
            raise GitHubError("storage", "The macOS Keychain did not keep the GitHub token")

    def delete(self) -> None:
        result = _run_tool([self.security, "delete-generic-password", "-a", SECRET_ACCOUNT, "-s", self.name],
                           "The macOS Keychain")
        if result.returncode not in (0, self.NOT_FOUND):
            raise _tool_error("The macOS Keychain did not delete the GitHub token", result)


class SecretToolStore:
    """A secret in the Secret Service (GNOME Keyring, KWallet), through ``secret-tool``.

    ``secret-tool store`` reads the secret from stdin, exactly as given, so it gets
    no trailing newline.
    """
    backend = "secret-service"

    def __init__(self, name: str, *, executable: str | None = None):
        self.name = _check_name(name)
        self.executable = executable or shutil.which("secret-tool") or "secret-tool"

    def __repr__(self) -> str:
        return f"SecretToolStore({self.name!r})"

    @property
    def _attributes(self) -> list[str]:
        return ["service", self.name, "account", SECRET_ACCOUNT]

    def available(self) -> bool:
        """Whether ``secret-tool`` is installed and reaches a Secret Service: a lookup answers without an error."""
        if not shutil.which(self.executable):
            return False
        try:
            result = _run_tool([self.executable, "lookup", *self._attributes], "secret-tool")
        except GitHubError:
            return False
        return result.returncode == 0 or (result.returncode == 1 and not result.stderr.strip())

    def load(self) -> str | None:
        result = _run_tool([self.executable, "lookup", *self._attributes], "secret-tool")
        if result.returncode == 0:
            return result.stdout.strip() or None
        if result.returncode == 1 and not result.stderr.strip():
            return None  # no such item
        raise _tool_error("The Secret Service could not read the GitHub token", result)

    def save(self, token: str) -> None:
        token = _check_token(token)
        result = _run_tool([self.executable, "store", f"--label=Agents-Core GitHub token ({self.name})",
                            *self._attributes], "secret-tool", stdin=token)
        if result.returncode:
            raise _tool_error("The Secret Service did not store the GitHub token", result, token)
        if self.load() != token:
            raise GitHubError("storage", "The Secret Service did not keep the GitHub token")

    def delete(self) -> None:
        result = _run_tool([self.executable, "clear", *self._attributes], "secret-tool")
        if result.returncode and result.stderr.strip():
            raise _tool_error("The Secret Service did not delete the GitHub token", result)


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2  # this machine only: the credential never roams with the profile
ERROR_NOT_FOUND = 1168


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _Credential(ctypes.Structure):
    """``CREDENTIALW`` from ``wincred.h``, in fixed-width types so that the module imports everywhere."""
    _fields_ = [("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32),
                ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
                ("LastWritten", _FileTime), ("CredentialBlobSize", ctypes.c_uint32),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", ctypes.c_uint32),
                ("AttributeCount", ctypes.c_uint32), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p)]


class _CredentialApi:  # pragma: no cover - Windows only
    """``advapi32``'s credential functions, loaded only when a Windows store is used."""

    def __init__(self):
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise GitHubError("storage", "The Windows Credential Manager exists only on Windows")
        advapi32 = loader("advapi32", use_last_error=True)
        self.CredWriteW = advapi32.CredWriteW
        self.CredWriteW.argtypes = [ctypes.POINTER(_Credential), ctypes.c_uint32]
        self.CredWriteW.restype = ctypes.c_int
        self.CredReadW = advapi32.CredReadW
        self.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.POINTER(ctypes.POINTER(_Credential))]
        self.CredReadW.restype = ctypes.c_int
        self.CredDeleteW = advapi32.CredDeleteW
        self.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        self.CredDeleteW.restype = ctypes.c_int
        self.CredFree = advapi32.CredFree
        self.CredFree.argtypes = [ctypes.c_void_p]
        self.CredFree.restype = None

    @staticmethod
    def last_error() -> int:
        return ctypes.get_last_error()


class CredentialManagerStore:
    """A generic credential in the Windows Credential Manager, through ``ctypes``; nothing runs a child process."""
    backend = "windows-credential-manager"

    def __init__(self, name: str, *, api=None):
        self.name = _check_name(name)
        self._api = api

    def __repr__(self) -> str:
        return f"CredentialManagerStore({self.name!r})"

    @property
    def api(self):
        if self._api is None:
            self._api = _CredentialApi()
        return self._api

    def load(self) -> str | None:
        found = ctypes.POINTER(_Credential)()
        if not self.api.CredReadW(self.name, CRED_TYPE_GENERIC, 0, ctypes.pointer(found)):
            code = self.api.last_error()
            if code == ERROR_NOT_FOUND:
                return None
            raise GitHubError("storage", f"The Windows Credential Manager could not read the GitHub token "
                                         f"(error {code})")
        try:
            credential = found.contents
            size = credential.CredentialBlobSize
            blob = ctypes.string_at(credential.CredentialBlob, size) if size else b""
        finally:
            self.api.CredFree(found)
        try:
            return blob.decode("utf-8").strip() or None
        except UnicodeDecodeError:
            raise GitHubError("storage", "The Windows Credential Manager holds an unreadable GitHub token") from None

    def save(self, token: str) -> None:
        blob = _check_token(token).encode("utf-8")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _Credential(Type=CRED_TYPE_GENERIC, TargetName=self.name,
                                 Comment="Agents-Core: GitHub token for library sync",
                                 CredentialBlobSize=len(blob),
                                 CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
                                 Persist=CRED_PERSIST_LOCAL_MACHINE, UserName=SECRET_ACCOUNT)
        if not self.api.CredWriteW(ctypes.pointer(credential), 0):
            raise GitHubError("storage", f"The Windows Credential Manager did not store the GitHub token "
                                         f"(error {self.api.last_error()})")

    def delete(self) -> None:
        if not self.api.CredDeleteW(self.name, CRED_TYPE_GENERIC, 0):
            code = self.api.last_error()
            if code != ERROR_NOT_FOUND:
                raise GitHubError("storage", f"The Windows Credential Manager did not delete the GitHub token "
                                             f"(error {code})")


def _write_private(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` through a temporary file only this user can read (0600 on POSIX)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise OSError(f"{path} is a symlink")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as error:
        raise GitHubError("storage", f"Cannot write {path}: {error.strerror or error}") from None


class FileStore:
    """A private file in the state directory: the fallback where no OS secret store works."""
    backend = "file"
    WARNING = ("No OS secret store is available, so the GitHub token is kept in a private file in "
               "Agents-Core's state directory; anyone who can read your files can use it.")

    def __init__(self, directory: str | Path):
        self.path = Path(directory) / TOKEN_FILE

    def __repr__(self) -> str:
        return f"FileStore({str(self.path.parent)!r})"

    def load(self) -> str | None:
        try:
            if self.path.is_symlink():
                raise OSError(f"{self.path} is a symlink")
            text = self.path.read_text(encoding="ascii")
        except FileNotFoundError:
            return None
        except UnicodeError:
            raise GitHubError("storage", f"{self.path} does not hold a GitHub token") from None
        except OSError as error:
            raise GitHubError("storage", f"Cannot read {self.path}: {error.strerror or error}") from None
        return text.strip() or None

    def save(self, token: str) -> None:
        _write_private(self.path, _check_token(token).encode("ascii"))

    def delete(self) -> None:
        """Delete the token and any temporary copy that a killed write left next to it."""
        for path in (self.path, *self.path.parent.glob(f".{TOKEN_FILE}.*")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise GitHubError("storage", f"Cannot delete {path}: {error.strerror or error}") from None


def default_store(directory: str | Path, name: str, *, platform: str | None = None) -> SecretStore:
    """The best store this machine has: its OS secret store, else the private file in ``directory``.

    The Credential Manager and the Secret Service count only when a lookup answers:
    a Windows session without cached credentials (such as SSH with a key) or a Linux
    session without a Secret Service gets the file.
    """
    platform = platform or sys.platform
    if platform == "darwin":
        if os.path.exists(SECURITY):
            return KeychainStore(name, security=SECURITY)
    elif platform == "win32":
        try:
            store = CredentialManagerStore(name, api=_CredentialApi())
            store.load()
            return store
        except (GitHubError, OSError, AttributeError):
            logger.warning("The Windows Credential Manager is unavailable; the GitHub token goes to a file")
    else:
        store = SecretToolStore(name)
        if store.available():
            return store
    return FileStore(directory)


def open_store(backend: str, directory: str | Path, name: str) -> SecretStore | None:
    """The store that a recorded backend name refers to; None for an unknown name."""
    if backend == KeychainStore.backend:
        return KeychainStore(name)
    if backend == CredentialManagerStore.backend:
        return CredentialManagerStore(name)
    if backend == SecretToolStore.backend:
        return SecretToolStore(name)
    if backend == FileStore.backend:
        return FileStore(directory)
    return None


# --- account facade ------------------------------------------------------------------

class GitHubAccount:
    """The GitHub account of one installation: sign-in, the stored token and its status.

    ``directory`` is the installation's private state directory and ``name`` its
    secret name (for example ``agents-core-sync-<installation id>``). There,
    ``github-account.json`` records the host, the login, the storage backend, an ID
    of the sign-in and whether a reconnect is needed; the token stays in the backend
    recorded at sign-in. ``web_url``/``api_url`` replace the bases derived from
    ``host`` (tests use a local fake GitHub); ``store`` replaces this machine's
    default store.
    """

    def __init__(self, directory: str | Path, name: str, *, host: str | None = None,
                 web_url: str | None = None, api_url: str | None = None, client_id: str | None = None,
                 store: SecretStore | None = None):
        self.directory = Path(directory)
        self.name = _check_name(name)
        if web_url:
            self.web_url = _check_base(web_url)
            host = host or urllib.parse.urlsplit(self.web_url).netloc.lower()
        else:
            host = host or github_host()
            self.web_url = _check_base(web_base(host))
        self.host = host
        self.api_url = _check_base(api_url or api_base(host))
        self.client_id = client_id
        self._store = store

    def status(self) -> dict:
        """``connected``, ``login``, ``reconnect_needed``, ``storage`` and ``warning``; never reads the secret."""
        meta = self._read()
        login = meta.get("login") if isinstance(meta.get("login"), str) else None
        connected = self._connected(meta)
        warning = None
        if login and not connected:
            warning = (f"Signed in to {meta.get('host')}, but this installation now uses {self.host}; "
                       "sign in again.")
        elif connected and _backend(meta) == FileStore.backend:
            failure = meta.get("storage_error")
            warning = FileStore.WARNING + (f" The OS secret store failed: {failure}"
                                           if isinstance(failure, str) and failure else "")
        return {"connected": connected, "host": self.host, "login": login if connected else None,
                "reconnect_needed": connected and meta.get("reconnect_needed") is True,
                "storage": _backend(meta) if connected else None,
                "connected_at": meta.get("connected_at") if connected else None, "warning": warning}

    def device_flow(self) -> DeviceFlow:
        return DeviceFlow(self.client_id, web_url=self.web_url)

    def start_sign_in(self) -> DeviceCode:
        """Show ``user_code`` and ``verification_uri``; keep the returned object for the polls."""
        return self.device_flow().start()

    def poll_sign_in(self, device: DeviceCode) -> dict:
        """One poll for the web UI: ``{"state": "pending"|"slow_down", "interval"}``, or the status once connected."""
        result = self.device_flow().poll(device)
        if result.token is None:
            return {"state": result.state, "interval": result.interval}
        return {"state": "connected", **self.complete_sign_in(result.token)}

    def wait_for_sign_in(self, device: DeviceCode, *, sleep: Callable[[float], None] = time.sleep,
                         clock: Callable[[], float] = time.monotonic) -> dict:
        """Block until the user enters the code (the terminal wizard); returns the status."""
        token = self.device_flow().wait(device, sleep=sleep, clock=clock)
        return self.complete_sign_in(token, sleep=sleep)

    def complete_sign_in(self, token: str, *, sleep: Callable[[float], None] = time.sleep) -> dict:
        """Check the token, keep it in the secret store and record the login; returns the status.

        Nothing is stored unless GitHub accepts the token; a network failure, or a rate
        limit that ends within a minute, is retried a few times before the token is
        given up. When the secret store fails, the token goes to the private file and
        the status says why. The record is written after the token, and a failed write
        removes the token again. A new sign-in gets a new ID, replaces the previous one
        and removes old copies of the token from other stores.
        """
        login = self._login(token, sleep)
        with self._lock():
            previous = self._read()
            store, failure = self._keep(token)
            record = {"host": self.host, "login": login, "storage": store.backend, "sign_in": uuid.uuid4().hex,
                      "connected_at": _now(), "reconnect_needed": False}
            if failure:
                record["storage_error"] = failure
            try:
                self._write(record)
            except BaseException:
                _delete_quietly(store)  # no token stays behind without a record that points to it
                raise
            for backend in {_backend(previous), FileStore.backend} - {None, store.backend}:
                stale = self._store_for(backend)
                if stale is not None:
                    _delete_quietly(stale)
        return self.status()

    def client(self) -> GitHubClient:
        """A client with the stored token; a 401 from it marks its sign-in "reconnect needed".

        The client keeps the ID of the sign-in it was made for, so a 401 for an old token
        never marks a newer sign-in. Raises ``auth`` when no account is connected to this
        host or its token is gone, ``storage`` when the secret store cannot be read.
        """
        meta = self._read()
        if not self._connected(meta):
            raise GitHubError("auth", f"No GitHub account on {self.host} is connected; sign in first.")
        sign_in = _sign_in_id(meta)
        store = self._store_for(_backend(meta))
        token = store.load() if store is not None else None
        if token is None or not _TOKEN.fullmatch(token):
            try:
                self._mark_reconnect_needed(sign_in)
            except GitHubError as error:
                logger.warning("Could not record that the GitHub account needs a reconnect: %s", error.message)
            raise GitHubError("auth", "The stored GitHub authorization is missing; reconnect the GitHub account.")
        return GitHubClient(token, api_url=self.api_url, login=meta["login"],
                            on_unauthorized=lambda: self._mark_reconnect_needed(sign_in))

    def mark_reconnect_needed(self) -> None:
        """Record that GitHub refused the current sign-in's token.

        Only a status: git sync keeps running on the deploy key.
        """
        self._mark_reconnect_needed(None)

    def forget(self) -> dict:
        """Delete the stored token and the record ("Forget account"); returns the status and ``revoke_url``.

        The token is deleted from the store the record names, from this machine's default
        store and from the private file, so a lost or unreadable record leaves no token
        behind. If a deletion fails, the record stays and the error says so: Forget can be
        retried. The token stays valid on GitHub until the user revokes it at
        ``revoke_url``: revoking through the API needs the OAuth App's client secret,
        which does not ship.
        """
        with self._lock():
            meta = self._read()
            failures = []
            for store in self._stores_to_clear(meta):
                try:
                    store.delete()
                except GitHubError as error:
                    failures.append(error.message)
            if failures:
                raise GitHubError("storage", "Forget did not finish; try again. " + "; ".join(failures))
            try:
                (self.directory / ACCOUNT_FILE).unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise GitHubError("storage", f"Cannot delete {ACCOUNT_FILE}: {error.strerror or error}") from None
        return {**self.status(), "revoke_url": f"{self.web_url}/settings/applications"}

    def _login(self, token: str, sleep: Callable[[float], None]) -> str:
        """The login of a new token; a failure that may pass soon is retried, so the token is not lost to it."""
        client = GitHubClient(token, api_url=self.api_url)
        attempt = 1
        while True:
            try:
                return client.user()
            except GitHubError as error:
                delay = _retry_delay(error)
                if delay is None or attempt >= SIGN_IN_ATTEMPTS:
                    raise
            attempt += 1
            sleep(delay)

    def _keep(self, token: str) -> tuple[SecretStore, str | None]:
        """Save the token in this machine's store, or in the private file if that store fails.

        Returns the store that holds the token and, after a fallback, why the first one failed.
        """
        store = self._store or default_store(self.directory, self.name)
        try:
            store.save(token)
            return store, None
        except GitHubError as error:
            if error.code != "storage" or store.backend == FileStore.backend:
                raise
            failure = error.message
        logger.warning("%s; the GitHub token goes to a private file instead", failure)
        _delete_quietly(store)  # nothing half-saved stays behind
        fallback = FileStore(self.directory)
        fallback.save(token)
        return fallback, failure

    def _mark_reconnect_needed(self, sign_in: str | None) -> None:
        """Mark sign-in ``sign_in`` (None: the current one), unless a newer sign-in replaced it."""
        with self._lock():
            meta = self._read()
            if not meta.get("login") or meta.get("reconnect_needed") is True:
                return
            if sign_in is not None and _sign_in_id(meta) != sign_in:
                return
            self._write({**meta, "reconnect_needed": True})

    def _stores_to_clear(self, meta: dict) -> list[SecretStore]:
        """The recorded store, this machine's default store and the private file, each once."""
        stores: dict[str, SecretStore] = {}
        for store in (self._store_for(_backend(meta)), self._store or default_store(self.directory, self.name),
                      FileStore(self.directory)):
            if store is not None:
                stores.setdefault(store.backend, store)
        return list(stores.values())

    def _connected(self, meta: dict) -> bool:
        return isinstance(meta.get("login"), str) and bool(meta["login"]) and meta.get("host") == self.host

    def _store_for(self, backend: str | None) -> SecretStore | None:
        if self._store is not None and backend == self._store.backend:
            return self._store
        return open_store(backend, self.directory, self.name) if backend else None

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Serialize changes of the account between threads and processes of this installation."""
        with ExitStack() as stack:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                stack.enter_context(file_lock(self.directory / "github-account.lock"))
            except OSError as error:
                raise GitHubError("storage", f"Cannot lock the GitHub account in {self.directory}: "
                                             f"{error.strerror or error}") from None
            yield

    def _read(self) -> dict:
        try:
            value = json.loads((self.directory / ACCOUNT_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("%s is unreadable; the GitHub account counts as not connected", ACCOUNT_FILE)
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, meta: dict) -> None:
        _write_private(self.directory / ACCOUNT_FILE, (json.dumps(meta, indent=2) + "\n").encode("utf-8"))


def _backend(meta: dict) -> str | None:
    """The storage backend that the account record names."""
    backend = meta.get("storage")
    return backend if isinstance(backend, str) and backend else None


def _sign_in_id(meta: dict) -> str:
    """The recorded sign-in's ID; ``connected_at`` stands in for a record without one."""
    return str(meta.get("sign_in") or meta.get("connected_at") or "")


def _delete_quietly(store: SecretStore) -> None:
    """Remove a copy of the token that nothing needs any more; a failure is logged, not raised."""
    try:
        store.delete()
    except GitHubError as error:
        logger.warning("Could not remove the GitHub token from %s: %s", store.backend, error.message)


def _retry_delay(error: GitHubError) -> float | None:
    """Seconds to wait before trying again after ``error``; None when waiting cannot help."""
    if error.code == "network":
        return SIGN_IN_RETRY_DELAY
    if error.code == "rate_limited" and error.reset_at is not None:
        wait = error.reset_at - time.time()
        if wait <= MAX_RATE_LIMIT_WAIT:
            return max(wait, 1.0)
    return None
