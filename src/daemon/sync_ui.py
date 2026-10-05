"""Library sync in the web UI (#170): the routes under ``/ui/api/sync``.

``FlowsUI`` hands a request here only after its own checks: the loopback ``Host``, a valid
session cookie and the ``X-Agents-UI`` header with a same-origin ``Origin``. A GET here needs the
header too, and an ``Origin`` it carries must be the page's own, so no other page, not even one
served on another loopback port, can start one. These requests do not count in ``inflight``: like
``/admin/user-sync/*`` they follow the sync task's drain.

Engine operations that run git or reach the remote (check, preview, setup, start, run, a new key
and disconnect) go through the daemon's sync task (``UserSync.perform(queued=True)``), one at a
time with its cycles. Settings, scopes and conflicts change at once, as pause and resume do, and
the task learns the settings they leave. GitHub API calls that never touch the repository
(sign-in, the libraries of the account, a new repository, machines, Forget account) run in their
own threads, counted in ``io_pending``.

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
import os
from pathlib import Path
import secrets
import stat
import time

from starlette.responses import JSONResponse

from src.user_sync import keys, scope
from src.user_sync.engine import SyncError, default_label, hosted_repository
from src.user_sync.github import DEFAULT_REPO_NAME, GitHubError, parse_public_key
from src.user_sync.gitcmd import RemoteError, parse_remote
from .sync_loop import DRAINING, Abandoned, Draining, Unavailable, failure

PREFIX = "/ui/api/sync"
TEXT_LIMIT = 512 * 1024  # a conflict's texts shown side by side
DEPLOY_KEY_TITLE = "Agents-Core "  # how setup titles a machine's deploy key on GitHub
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

_KINDS = {
    "text": ("a string", lambda value: isinstance(value, str) and len(value) <= 4096),
    "flag": ("true or false", lambda value: isinstance(value, bool)),
    "number": ("a whole number", lambda value: isinstance(value, int) and not isinstance(value, bool)),
    "texts": ("a list of strings", lambda value: isinstance(value, list) and len(value) <= 500
              and all(isinstance(item, str) and len(item) <= 4096 for item in value)),
}


def is_sync_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


class BadRequest(ValueError):
    """A body or parameter that does not fit the route."""


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


# --- engine work, run in threads --------------------------------------------------------------


def _text_of(library: Path, relative) -> str | None:
    """A library file's UTF-8 text, or None: never through a link or outside the library."""
    if not isinstance(relative, str) or not scope.portable_name(relative):
        return None
    current, info = Path(library), None
    for part in relative.split("/"):
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            return None
        if scope.is_link(info, current):
            return None
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > TEXT_LIMIT:
        return None
    try:
        return current.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


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
        mine = _text_of(syncer.library, version) if version.startswith(".history/") else None
    elif isinstance(record.get("local_content"), str):
        mine = record["local_content"]
    elif isinstance(record.get("local_content_base64"), str):
        try:
            mine = base64.b64decode(record["local_content_base64"], validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            binary = True
    return {**_summary(record), "current": _text_of(syncer.library, record.get("path")), "mine": mine,
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
    """What the page needs beyond the engine's status: whether sync started, the conflicts and the
    repository as ``owner/name`` (GitHub) or ``host/path``, never with credentials."""
    settings = syncer.settings()
    account = syncer.github_account()
    extras = {"started": settings.started if settings else None, "github": _github(account),
              "conflict_list": [_summary(record) for record in syncer.conflicts()],
              "repository": None, "github_repository": None, "ssh": False}
    if settings is None:
        extras["suggested_label"] = default_label()
        return extras
    remote = _remote_of(syncer, settings)
    if remote is not None:
        hosted = hosted_repository(remote, account.host)
        extras.update(repository=hosted or remote.display, github_repository=hosted, ssh=remote.kind == "ssh",
                      private_confirmed=settings.private_confirmed)
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
        label = key.title[len(DEPLOY_KEY_TITLE):] if key.title.startswith(DEPLOY_KEY_TITLE) else key.title
        machines.append({"id": key.id, "title": key.title, "label": label, "read_only": key.read_only,
                         "created_at": key.created_at, "this": mine is not None and _material(key.key) == mine})
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
    """``/ui/api/sync…`` of one service; ``sign_in`` is the GitHub sign-in in progress, if any."""

    def __init__(self, service, *, clock=time.monotonic):
        self.service = service
        self.clock = clock
        self.sign_in = None  # {"id", "device", "lock", "next", "expires"}

    @property
    def loop(self):
        return self.service.user_sync

    async def handle(self, request, path: str, read_body):
        methods = ROUTES.get(path)
        if methods is None:
            return _json({"error": "not_found"}, 404)
        name = methods.get(request.method)
        if name is None:
            return _json({"error": "method_not_allowed"}, 405, Allow=", ".join(sorted(methods)))
        try:
            body = await read_body(request) if request.method in ("POST", "PUT") else {}
        except Exception as error:  # FlowError: not a JSON object, or too large
            return _json({"error": "invalid_request", "message": str(error).split(": ", 1)[-1]}, 400)
        try:
            return await getattr(self, name)(request, body)
        except BadRequest as error:
            return _json({"error": "invalid_request", "message": str(error)}, 400)
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
        view = await self.loop.status_view()
        return _json({**view, **await self._work(_extras)})

    # --- GitHub sign-in: the device code stays here ------------------------------------

    async def device_start(self, request, body):
        _fields(body, {})
        device = await self._work(lambda syncer: syncer.github_account().start_sign_in())
        now = self.clock()
        self.sign_in = {"id": secrets.token_urlsafe(16), "device": device, "lock": asyncio.Lock(),
                        "next": now + device.interval, "expires": now + device.expires_in}
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
                result = await self._work(lambda syncer: syncer.github_account().poll_sign_in(device))
            except GitHubError as error:
                if error.code == "network":  # a short outage must not end the sign-in
                    attempt["next"] = self.clock() + device.interval
                    return _json({"state": "pending", "interval": device.interval, "message": error.message})
                if self.sign_in is attempt:
                    self.sign_in = None
                raise
            attempt["next"] = self.clock() + device.interval  # slow_down raised the interval
            if result.get("state") != "connected":
                return _json({"state": result.get("state"), "interval": result.get("interval", device.interval)})
            if self.sign_in is attempt:
                self.sign_in = None
            return _json({"state": "connected", "github": {key: value for key, value in result.items()
                                                           if key != "state"}})

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
        """This machine's public key as a deploy key again, when GitHub lost it (``github add-key``)."""
        _fields(body, {})
        return _json(await self._work(lambda syncer: syncer.add_deploy_key()))

    async def forget(self, request, body):
        _fields(body, {})
        self.sign_in = None
        result = await self._work(lambda syncer: syncer.github_account().forget())
        return _json({"status": "forgotten", "github": None, "revoke_url": result.get("revoke_url"),
                      "message": "The GitHub authorization is deleted from this machine; revoke it on GitHub "
                                 "as well."})

    # --- setup, access and the first upload ----------------------------------------------

    async def setup(self, request, body):
        fields = _fields(body, {"github": "text", "remote": "text", "name": "text", "email": "text",
                                "label": "text", "trust_host_key": "text", "ask_new_repositories": "flag"})
        github, remote = fields.pop("github", "").strip(), fields.pop("remote", "").strip()
        if bool(github) == bool(remote):
            raise BadRequest("give either github (owner/name) or remote (an SSH URL)")
        if not (fields.get("name", "").strip() and fields.get("email", "").strip()):
            raise BadRequest("name and email are required")
        options = {name: value.strip() if isinstance(value, str) else value for name, value in fields.items()
                   if value != ""}
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
