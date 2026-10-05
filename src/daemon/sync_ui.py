"""Library sync in the web UI (#170): the routes under ``/ui/api/sync``.

``FlowsUI`` hands a request here only after its own checks: the loopback ``Host``, a valid
session cookie and the ``X-Agents-UI`` header with a same-origin ``Origin``. A GET here needs the
header too, and an ``Origin`` it carries must be the page's own, so no other page, not even one
served on another loopback port, can start one. These requests do not count in ``inflight``: like
``/admin/user-sync/*`` they follow the sync task's drain. The page's polls (``PASSIVE``) leave the
service idle, so an open page never holds back an automatic update.

Engine operations that run git, reach the remote or read this machine's key while a new one may
replace it (check, preview, setup, start, run, a new key, adding the key on GitHub again and
disconnect) go through the daemon's sync task (``UserSync.perform(queued=True)``), one at a time
with its cycles. Settings, scopes and conflicts change at once, as pause and resume do, and the
task learns the settings they leave. GitHub API calls that never touch the repository (sign-in, the
libraries of the account, a new repository, machines, Forget account) run in their own threads,
counted in ``io_pending``.

No answer carries the GitHub token, this machine's private key or a sign-in's device code: the
device code stays here between the start of a sign-in and its polls, and only
``DeviceCode.public()`` leaves. The browser never calls GitHub (the page's CSP keeps
``connect-src 'self'``); the daemon does.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import time

from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from src.user_sync import keys, scope
from src.user_sync.engine import SyncError, default_label, hosted_repository, validate_identity
from src.user_sync.github import DEFAULT_REPO_NAME, GitHubError, parse_public_key
from src.user_sync.gitcmd import RemoteError, config_value, parse_remote
from .sync_loop import DRAINING, Abandoned, Draining, Unavailable, failure

PREFIX = "/ui/api/sync"
MAX_BODY = 64 * 1024     # as /admin/user-sync: every body here is small
TEXT_LIMIT = 512 * 1024  # a conflict's texts shown side by side
CONFLICTS_LISTED = 50    # conflict records the status lists; `conflict_total` counts them all
# The title GitHubClient.add_deploy_key gives a machine's deploy key ("Agents-Core <label>"): keys
# with another title are not machines of this library, and the page never removes them.
DEPLOY_KEY_TITLE = "Agents-Core "
CONFLICT_FIELDS = ("id", "path", "flow", "repo_key", "kind", "kept", "machine", "time", "deleted_on")

# path -> {method: handler}
ROUTES = {
    PREFIX: {"GET": "status"},
    f"{PREFIX}/github/device": {"POST": "device_start", "GET": "device_poll"},
    f"{PREFIX}/github/libraries": {"GET": "libraries"},
    f"{PREFIX}/github/create": {"POST": "create_repository"},
    f"{PREFIX}/github/add-key": {"POST": "add_key"},
    f"{PREFIX}/github/forget": {"POST": "forget"},
    f"{PREFIX}/setup": {"POST": "setup"},
    f"{PREFIX}/check": {"POST": "check"},
    f"{PREFIX}/preview": {"GET": "preview"},
    f"{PREFIX}/start": {"POST": "start"},
    f"{PREFIX}/run": {"POST": "run"},
    f"{PREFIX}/settings": {"PUT": "settings"},
    f"{PREFIX}/conflict": {"GET": "conflict"},
    f"{PREFIX}/conflicts/resolve": {"POST": "resolve"},
    f"{PREFIX}/scopes": {"GET": "scopes", "PUT": "change_scopes"},
    f"{PREFIX}/machines": {"GET": "machines"},
    f"{PREFIX}/machines/remove": {"POST": "remove_machine"},
    f"{PREFIX}/key/regenerate": {"POST": "regenerate_key"},
    f"{PREFIX}/disconnect": {"POST": "disconnect"},
}
# What the page asks by itself, on a timer or to fill a view: it keeps the service idle.
PASSIVE = frozenset({("GET", PREFIX), ("GET", f"{PREFIX}/scopes"), ("GET", f"{PREFIX}/machines"),
                     ("GET", f"{PREFIX}/conflict"), ("GET", f"{PREFIX}/github/device")})

_KINDS = {
    "text": ("a string", lambda value: isinstance(value, str) and len(value) <= 4096),
    "flag": ("true or false", lambda value: isinstance(value, bool)),
    "number": ("a whole number", lambda value: isinstance(value, int) and not isinstance(value, bool)),
    "texts": ("a list of strings", lambda value: isinstance(value, list) and len(value) <= 500
              and all(isinstance(item, str) and len(item) <= 4096 for item in value)),
}


def is_sync_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def is_passive(method: str, path: str) -> bool:
    return (method, path) in PASSIVE


class BadRequest(ValueError):
    """A body or parameter that does not fit the route; ``reason`` names what (400)."""

    def __init__(self, message: str, *, reason: str = "invalid_request", code: int = 400):
        super().__init__(message)
        self.reason, self.code = reason, code


def _json(value, status: int = 200, **headers) -> JSONResponse:
    return JSONResponse(value, status, headers={"Cache-Control": "no-store", **headers})


def _fields(body: dict, allowed: dict) -> dict:
    """The body's fields, checked against ``{name: kind}``; null counts as absent."""
    unknown = sorted(map(str, set(body) - set(allowed)))
    if unknown:
        raise BadRequest(f"unexpected field {', '.join(unknown)}")
    for name, kind in allowed.items():
        value = body.get(name)
        description, valid = _KINDS[kind]
        if value is not None and not valid(value):
            raise BadRequest(f"{name} must be {description}")
    return {name: body[name] for name in allowed if body.get(name) is not None}


def _check_identity(name=None, email=None, label=None) -> None:
    """Refuse, before anything is stored, what the engine or its isolated gitconfig would refuse.

    Values that are not given are not checked here; the engine checks what it keeps.
    """
    try:
        validate_identity("Owner" if name is None else name, "owner@example.com" if email is None else email,
                          "machine" if label is None else label)
        for value in (name, email):
            if value is not None:
                config_value(value)
    except SyncError as error:
        raise BadRequest(error.message, reason="identity") from None
    except ValueError as error:
        raise BadRequest(str(error), reason="identity") from None


async def _read_body(request) -> dict:
    """The JSON object a request carries, read in pieces up to ``MAX_BODY``."""
    raw = b""
    try:
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY:
                raise BadRequest("the request is too large", reason="too_large", code=413)
    except ClientDisconnect:
        raise BadRequest("the client left before the body ended") from None
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):  # RecursionError: nesting deeper than the parser allows
        raise BadRequest("the body must be a JSON object") from None
    if not isinstance(value, dict):
        raise BadRequest("the body must be a JSON object")
    return value


# --- engine work, run in threads --------------------------------------------------------------


def _read_library(library: Path, relative) -> tuple[str | None, bool]:
    """A file that sync handles, inside the library: ``(text, False)``, or ``(None, True)`` for bytes
    that are not UTF-8.

    ``(None, False)`` for anything else: a path sync never handles (``.git`` among them), a link on
    the way, a file that is missing, too large, or that another one replaced between the checks and
    the read (it is opened without following a link, and must be the file the checks saw).
    """
    if not isinstance(relative, str) or not scope.portable_name(relative) or scope.groups(relative) is None:
        return None, False
    current, info = Path(library), None
    for part in relative.split("/"):
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            return None, False
        if scope.is_link(info, current):
            return None, False
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > TEXT_LIMIT:
        return None, False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(current, flags)
    except OSError:
        return None, False
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return None, False
        data = stream.read(TEXT_LIMIT + 1)
    if len(data) > TEXT_LIMIT:
        return None, False
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return None, True


def _summary(record: dict) -> dict:
    """What the page lists of a conflict record; the kept text stays out (see ``_conflict``)."""
    summary = {name: record.get(name) if isinstance(record.get(name), str) else None for name in CONFLICT_FIELDS}
    if isinstance(record.get("local_version"), str):
        summary["mine"] = "version"
    elif isinstance(record.get("local_content"), str) or isinstance(record.get("local_content_base64"), str):
        summary["mine"] = "content"
    else:
        summary["mine"] = "deleted" if record.get("deleted_on") == "local" else None
    return summary


def _conflict(syncer, conflict_id: str) -> dict:
    """A conflict with the current text of its file and the kept local one, for a side-by-side view."""
    record = next((item for item in syncer.conflicts() if item.get("id") == conflict_id), None)
    if record is None:
        raise SyncError("invalid", "unknown conflict")
    mine, binary = None, False
    version = record.get("local_version")
    if isinstance(version, str):
        if version.startswith(".history/"):
            mine, binary = _read_library(syncer.library, version)
    elif isinstance(record.get("local_content"), str):
        mine = record["local_content"]
    elif isinstance(record.get("local_content_base64"), str):
        try:
            mine = base64.b64decode(record["local_content_base64"], validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            binary = True
    current, current_binary = _read_library(syncer.library, record.get("path"))
    return {**_summary(record), "current": current, "current_binary": current_binary, "mine": mine,
            "binary": binary}


def _github(account) -> dict | None:
    """The account's status, never its token; None unless it is connected or needs a reconnect."""
    try:
        status = account.status()
    except Exception:  # an unreadable record must not break the sync status
        return None
    return status if status.get("connected") or status.get("reconnect_needed") else None


def _remote_of(syncer, settings):
    try:
        return parse_remote(settings.remote, allow_file=syncer.allow_file_remote)
    except RemoteError:
        return None


def _extras(syncer) -> dict:
    """What the page needs beyond the engine's status: whether sync started, the conflicts, the
    repository as ``owner/name`` (GitHub) or ``host/path`` (never with credentials) and whether the
    host's key still waits for the owner's confirmation."""
    settings = syncer.settings()
    account = syncer.github_account()
    records = syncer.conflicts()
    extras = {"started": settings.started if settings else None, "github": _github(account),
              "conflict_list": [_summary(record) for record in records[:CONFLICTS_LISTED]],
              "conflict_total": len(records), "repository": None, "github_repository": None, "ssh": False,
              "host_key_unconfirmed": False}
    if settings is None:
        extras["suggested_label"] = default_label()
        return extras
    remote = _remote_of(syncer, settings)
    if remote is not None:
        hosted = hosted_repository(remote, account.host)
        extras.update(repository=hosted or remote.display, github_repository=hosted, ssh=remote.kind == "ssh",
                      private_confirmed=settings.private_confirmed)
        if remote.kind == "ssh":  # a setup that stopped at the fingerprints, or that a reload interrupted
            extras["host_key_unconfirmed"] = not keys.trusted_keys(syncer.known_hosts, remote.host, remote.port)
    return extras


def _connected_repository(syncer, settings):
    """``(owner/name, client)`` for a repository on the connected account's host; the client is None
    without a usable sign-in, and the whole is None for a repository elsewhere."""
    remote = _remote_of(syncer, settings)
    account = syncer.github_account()
    repository = hosted_repository(remote, account.host) if remote is not None else None
    if repository is None:
        return None
    status = account.status()
    if not status["connected"] or status["reconnect_needed"]:
        return repository, None
    return repository, account.client()


def _material(line) -> tuple[str, str] | None:
    try:
        return parse_public_key(line) if line else None
    except GitHubError:
        return None


def _machines(syncer) -> dict:
    """The repository's deploy keys on GitHub. ``managed`` marks the machines of this library: keys
    with the title sync gives them; the others are listed for information only."""
    settings = syncer.settings()
    if settings is None:
        return {"available": False, "label": None, "machines": [], "message": "Sync is not set up."}
    base = {"available": False, "label": settings.label, "machines": []}
    found = _connected_repository(syncer, settings)
    if found is None:
        return {**base, "message": "The machines are listed for a repository on GitHub."}
    repository, client = found
    if client is None:
        return {**base, "repository": repository,
                "message": f"Sign in to GitHub to see the machines that sync with {repository}."}
    mine = _material(keys.public_key(syncer.state_dir))
    machines = []
    for key in client.deploy_keys(repository):
        managed = key.title.startswith(DEPLOY_KEY_TITLE)
        machines.append({"id": key.id, "title": key.title, "managed": managed,
                         "label": key.title[len(DEPLOY_KEY_TITLE):] if managed else key.title,
                         "read_only": key.read_only, "created_at": key.created_at,
                         "this": mine is not None and _material(key.key) == mine})
    return {**base, "available": True, "repository": repository, "machines": machines}


def _remove_machine(syncer, key_id: int) -> dict:
    settings = syncer.settings()
    if settings is None:
        raise SyncError("not_set_up", "sync is not set up", state="off")
    found = _connected_repository(syncer, settings)
    if found is None or found[1] is None:
        raise SyncError("not_signed_in", "sign in to GitHub to remove a machine's deploy key")
    repository, client = found
    key = next((item for item in client.deploy_keys(repository) if item.id == key_id), None)
    if key is None:
        raise SyncError("invalid", f"{repository} has no such deploy key")
    if not key.title.startswith(DEPLOY_KEY_TITLE):
        raise SyncError("not_a_machine", f"{key.title} is not a machine of this library; remove it in the "
                                         "repository's settings if it should go")
    mine = _material(keys.public_key(syncer.state_dir))
    if mine is not None and _material(key.key) == mine:
        raise SyncError("this_machine", "this is this machine's key; disconnect to stop syncing here")
    client.delete_deploy_key(repository, key_id)
    return {"status": "removed", "id": key_id, "title": key.title}


def _disconnect(syncer) -> dict:
    """Disconnect here, then remove this machine's deploy key on GitHub when the account can.

    The key and the repository are read first; a disconnect that fails leaves GitHub as it was.
    """
    settings = syncer.settings()
    public = keys.public_key(syncer.state_dir)
    try:
        found = _connected_repository(syncer, settings) if settings is not None else None
        problem = None
    except (GitHubError, SyncError) as error:
        found, problem = None, error.message
    result = syncer.disconnect()
    note = {"deploy_key": "manual",
            "deploy_key_message": "Remove this machine's deploy key in the repository's settings on its host."}
    if problem is not None:
        note = {"deploy_key": "failed", "deploy_key_message": f"Could not reach GitHub ({problem}); remove this "
                                                              "machine's deploy key in the repository's settings."}
    elif found is not None and found[1] is None:
        note["deploy_key_message"] = (f"Sign in to GitHub to remove this machine's deploy key from {found[0]}, "
                                      "or remove it in the repository's settings.")
    elif found is not None and public:
        repository, client = found
        try:
            key = client.find_deploy_key(repository, public)
            if key is None:
                note = {"deploy_key": "missing", "deploy_key_message": f"{repository} had no deploy key of this machine."}
            else:
                client.delete_deploy_key(repository, key.id)
                note = {"deploy_key": "removed",
                        "deploy_key_message": f"Removed this machine's deploy key ({key.title}) from {repository}."}
        except GitHubError as error:
            note = {"deploy_key": "failed", "deploy_key_message": f"Could not remove this machine's deploy key from "
                                                                  f"{repository} ({error.message}); remove it on GitHub."}
    return {**result, **note}


def _setup_again(syncer, trust_host_key):
    """Setup with what sync keeps, for a setup that stopped at the host's fingerprints."""
    settings = syncer.settings()
    if settings is None:
        raise SyncError("not_set_up", "sync is not set up", state="off")
    return syncer.setup(remote=settings.remote, name=settings.name, email=settings.email, label=settings.label,
                        branch=settings.branch, trust_host_key=trust_host_key)


def _preview(syncer) -> dict:
    preview = syncer.preview()
    settings = syncer.settings()
    return {**preview, "privacy": syncer.privacy(),
            "private_confirmed": bool(settings and settings.private_confirmed)}


def _scopes(syncer) -> dict:
    settings = syncer.settings()
    return {**syncer.scopes(), "ask_new_repositories": bool(settings and settings.ask_new_repositories)}


# --- the routes -------------------------------------------------------------------------------


class SyncUI:
    """``/ui/api/sync…`` of one service.

    ``sign_in`` is the GitHub sign-in in progress, if any. ``account_generation`` counts Forget
    account: a sign-in started before one stores nothing, and ``_account`` orders the two.
    """

    def __init__(self, service, *, clock=time.monotonic):
        self.service = service
        self.clock = clock
        self.sign_in = None  # {"id", "device", "lock", "next", "expires", "generation"}
        self.account_generation = 0
        self._account = asyncio.Lock()

    @property
    def loop(self):
        return self.service.user_sync

    async def handle(self, request, path: str):
        methods = ROUTES.get(path)
        if methods is None:
            return _json({"error": "not_found"}, 404)
        name = methods.get(request.method)
        if name is None:
            return _json({"error": "method_not_allowed"}, 405, Allow=", ".join(sorted(methods)))
        try:
            body = await _read_body(request) if request.method in ("POST", "PUT") else {}
            return await getattr(self, name)(request, body)
        except BadRequest as error:
            return _json({"error": "invalid_request", "status": "error", "reason": error.reason,
                          "message": str(error)}, error.code)
        except Unavailable:
            return _json({"error": "unavailable", "message": "the sync task is not running"}, 503)
        except (Abandoned, Draining):
            return _json(DRAINING, 503)
        except Exception as error:
            code, value = failure(error)
            return _json(value, code)

    async def _queued(self, command: str, call):
        outcome = await self.loop.perform(command, call, queued=True)
        return _json(outcome["result"], outcome["code"])

    async def _at_once(self, command: str, call):
        outcome = await self.loop.perform(command, call, queued=False)
        return _json(outcome["result"], outcome["code"])

    async def _work(self, function, *args):
        """``function(syncer, *args)`` in a thread, outside the queue."""
        return await self.loop.offload(lambda: function(self.loop.engine(), *args))

    # --- status ------------------------------------------------------------------------

    async def status(self, request, body):
        """``?fresh=1`` reads the engine; otherwise a status the loop read within a scan interval."""
        view = await self.loop.status_view(fresh=request.query_params.get("fresh") == "1")
        return _json({**view, **await self._work(_extras)})

    # --- GitHub sign-in: the device code stays here ------------------------------------

    async def device_start(self, request, body):
        _fields(body, {})
        device = await self._work(lambda syncer: syncer.github_account().start_sign_in())
        now = self.clock()
        self.sign_in = {"id": secrets.token_urlsafe(16), "device": device, "lock": asyncio.Lock(),
                        "next": now + device.interval, "expires": now + device.expires_in,
                        "generation": self.account_generation}
        return _json({"attempt": self.sign_in["id"], **device.public()})

    async def device_poll(self, request, body):
        attempt = self.sign_in
        wanted = request.query_params.get("attempt", "")
        gone = {"status": "attention", "reason": "no_sign_in",
                "message": "No GitHub sign-in is in progress here; start it again."}
        if attempt is None or not hmac.compare_digest(attempt["id"].encode(), wanted.encode()):
            return _json(gone, 404)
        async with attempt["lock"]:  # one poll of an attempt at a time: GitHub slows repeated ones down
            if self.sign_in is not attempt:
                return _json(gone, 404)
            device = attempt["device"]
            if self.clock() >= attempt["expires"]:
                self.sign_in = None
                return _json({"status": "attention", "reason": "expired_token",
                              "message": "The sign-in code expired before it was entered; start again."}, 409)
            if self.clock() < attempt["next"]:  # asked sooner than GitHub's interval: answer for it
                return _json({"state": "pending", "interval": device.interval})
            try:
                result = await self._work(lambda syncer: syncer.github_account().device_flow().poll(device))
            except GitHubError as error:
                if error.code == "network":  # a short outage must not end the sign-in
                    attempt["next"] = self.clock() + device.interval
                    return _json({"state": "pending", "interval": device.interval, "message": error.message})
                self._end(attempt)
                raise
            attempt["next"] = self.clock() + device.interval  # slow_down raised the interval
            if result.token is None:
                return _json({"state": result.state, "interval": result.interval})
            async with self._account:  # Forget account either ran before (nothing is kept) or runs after
                if self.account_generation != attempt["generation"]:
                    self._end(attempt)
                    return _json({"status": "attention", "reason": "cancelled",
                                  "message": "The GitHub account was forgotten while this sign-in waited; "
                                             "nothing was kept."}, 409)
                try:
                    github = await self._work(lambda syncer: syncer.github_account().complete_sign_in(result.token))
                finally:
                    self._end(attempt)
            return _json({"state": "connected", "github": github})

    def _end(self, attempt) -> None:
        if self.sign_in is attempt:
            self.sign_in = None

    async def libraries(self, request, body):
        def work(syncer):
            client = syncer.github_client()
            scan = client.libraries()
            return {"login": client.login, "checked": scan.checked, "truncated": scan.truncated,
                    "repositories": [{"full_name": info.full_name} for info in scan.libraries]}
        return _json(await self._work(work))

    async def create_repository(self, request, body):
        name = (_fields(body, {"name": "text"}).get("name") or DEFAULT_REPO_NAME).strip()

        def work(syncer):
            info = syncer.github_client().create_library(name)
            return {"status": "created", "repository": info.full_name}
        return _json(await self._work(work))

    async def add_key(self, request, body):
        """This machine's public key as a deploy key again, when GitHub lost it (``github add-key``).
        Queued: a new key may be replacing the files it reads."""
        _fields(body, {})
        return await self._queued("add_key", lambda syncer: syncer.add_deploy_key())

    async def forget(self, request, body):
        _fields(body, {})
        async with self._account:
            self.account_generation += 1  # a sign-in still waiting for GitHub keeps nothing
            self.sign_in = None
            result = await self._work(lambda syncer: syncer.github_account().forget())
        return _json({"status": "forgotten", "github": None, "revoke_url": result.get("revoke_url"),
                      "message": "The GitHub authorization is deleted from this machine; revoke it on GitHub "
                                 "as well."})

    # --- setup, access and the first upload ----------------------------------------------

    async def setup(self, request, body):
        fields = _fields(body, {"github": "text", "remote": "text", "name": "text", "email": "text",
                                "label": "text", "trust_host_key": "text", "ask_new_repositories": "flag",
                                "again": "flag"})
        if fields.pop("again", False):  # what sync keeps, as before a reload: the fingerprints again
            if set(fields) - {"trust_host_key"}:
                raise BadRequest("again takes only trust_host_key")
            return await self._queued("setup", lambda syncer: _setup_again(syncer, fields.get("trust_host_key")))
        github, remote = fields.pop("github", "").strip(), fields.pop("remote", "").strip()
        if bool(github) == bool(remote):
            raise BadRequest("give either github (owner/name) or remote (an SSH URL)")
        if not (fields.get("name", "").strip() and fields.get("email", "").strip()):
            raise BadRequest("name and email are required", reason="identity")
        options = {name: value.strip() if isinstance(value, str) else value for name, value in fields.items()
                   if value != ""}
        _check_identity(options.get("name"), options.get("email"), options.get("label"))
        if github:
            return await self._queued("setup", lambda syncer: syncer.setup_github(github, **options))
        return await self._queued("setup", lambda syncer: syncer.setup(remote=remote, **options))

    async def check(self, request, body):
        _fields(body, {})
        return await self._queued("check", lambda syncer: syncer.check())

    async def preview(self, request, body):
        return await self._queued("preview", _preview)

    async def start(self, request, body):
        fields = _fields(body, {"confirm": "text", "confirm_private": "flag"})
        confirm, private = fields.get("confirm"), fields.get("confirm_private", False)
        if not confirm:
            raise BadRequest("start needs the preview's hash (confirm)")

        def call(syncer):
            settings = syncer.settings()
            if private and settings is not None and not settings.private_confirmed:
                # As the terminal wizard does: setup again, with the same remote, records the confirmation.
                syncer.setup(remote=settings.remote, name=settings.name, email=settings.email,
                             label=settings.label, branch=settings.branch, confirm_private=True)
            return syncer.start(confirm)
        return await self._queued("start", call)

    async def run(self, request, body):
        confirm = _fields(body, {"confirm": "text"}).get("confirm") or None
        return await self._queued("run", lambda syncer: syncer.run(force=True, confirm=confirm))

    async def regenerate_key(self, request, body):
        _fields(body, {})
        return await self._queued("regenerate_key", lambda syncer: syncer.regenerate_key())

    async def disconnect(self, request, body):
        _fields(body, {})
        return await self._queued("disconnect", _disconnect)

    # --- settings, conflicts and scopes: at once -----------------------------------------

    async def settings(self, request, body):
        fields = _fields(body, {"fetch_minutes": "number", "ask_new_repositories": "flag", "name": "text",
                                "email": "text", "label": "text", "paused": "flag"})
        paused = fields.pop("paused", None)
        if not fields and paused is None:
            raise BadRequest("nothing to change")
        _check_identity(fields.get("name"), fields.get("email"), fields.get("label"))
        outcome = None
        if fields:
            outcome = await self.loop.perform("configure", lambda syncer: syncer.configure(**fields), queued=False)
            if outcome["code"] != 200:
                return _json(outcome["result"], outcome["code"])
        if paused is not None:
            command = "pause" if paused else "resume"
            outcome = await self.loop.perform(command, lambda syncer: getattr(syncer, command)(), queued=False)
        return _json(outcome["result"], outcome["code"])

    async def conflict(self, request, body):
        return _json(await self._work(_conflict, request.query_params.get("id", "")))

    async def resolve(self, request, body):
        fields = _fields(body, {"id": "text", "action": "text"})
        if not fields.get("id") or fields.get("action") not in ("keep", "mine", "dismiss"):
            raise BadRequest("id and action (keep, mine or dismiss) are required")
        return await self._at_once("resolve", lambda syncer: syncer.resolve(fields["id"], fields["action"]))

    async def scopes(self, request, body):
        return _json(await self._work(_scopes))

    async def change_scopes(self, request, body):
        fields = _fields(body, {"exclude": "texts", "include": "texts", "approve": "texts",
                                "approve_files": "texts", "confirm": "text"})
        confirm = fields.pop("confirm", None) or None
        if not any(fields.values()):
            raise BadRequest("nothing to change")
        return await self._at_once("scopes", lambda syncer: syncer.change_scopes(**fields, confirm=confirm))

    # --- machines: this repository's deploy keys on GitHub -------------------------------

    async def machines(self, request, body):
        return _json(await self._work(_machines))

    async def remove_machine(self, request, body):
        key_id = _fields(body, {"id": "number"}).get("id")
        if not key_id or key_id <= 0:
            raise BadRequest("id must be a deploy key's id")
        return _json(await self._work(_remove_machine, key_id))
