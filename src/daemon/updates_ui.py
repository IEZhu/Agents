"""The Updates section of the overview (#381): the branch this installation follows.

``FlowsUI`` hands a request here only after its own checks: the loopback ``Host``, a valid
session cookie and the ``X-Agents-UI`` header with a same-origin ``Origin``, on GET as well
(the protections of ``/ui/api/autodev``).

- ``GET /ui/api/updates``: the version and the update channel as the overview shows it
  (`overview.updates`: configured branch and its source, checked-out branch, whether scheduled
  updates are on, the last scheduled run, the branches offered, the ``update`` command, when a
  saved setting takes effect, and the setting's ``revision``).
- ``PUT /ui/api/updates`` with ``{expected_revision, branch, allow_downgrade?}``: saves the
  branch through `src.update_channel.set_branch`, the writer of ``python -m src.daemon channel
  set``, under ``control.lock`` as that command takes it. The page offers ``main`` and ``dev``
  only; the CLI takes any branch of the remote. Saving changes no code: the next scheduled run
  or ``update`` applies it. A switch to a branch that does not contain HEAD (a downgrade) is
  refused with 409 ``downgrade`` until the page sends ``allow_downgrade`` after the owner
  confirmed it. 409 ``conflict`` when the setting changed since the page read it, 409 ``busy``
  while another control command holds the lock, 400 for a branch the page does not offer or the
  remote does not have, 502 when the remote cannot be read.

``set_branch`` reads the remote (``ls-remote``, maybe a fetch; up to a minute), so it runs in
a worker thread. The channel is the ``.env`` value: the service's own environment never
overrides it here. No answer carries a secret: the remote's URL stays out, and the last run's
strings and a refusal's message lose any credentials in a URL (`overview.without_credentials`).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from src import update_channel
from src.file_lock import file_lock
from src.version import agents_core_version
from . import overview

PATH = "/ui/api/updates"
MAX_BODY = 4 * 1024
FIELDS = {"expected_revision", "branch", "allow_downgrade"}
# ChannelError codes as HTTP statuses.
STATUS = {"invalid": 400, "unknown_branch": 400, "unreachable": 502, "downgrade": 409, "conflict": 409}


def is_updates_path(path: str) -> bool:
    return path == PATH


class UpdatesError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


class UpdatesUI:
    def __init__(self, service, root: Path = overview.INSTALL_ROOT):
        self.service = service
        self.root = root  # the installation whose channel the page shows and saves

    async def handle(self, request: Request, path: str):
        work = {"GET": self.read, "PUT": self.save}.get(request.method)
        if work is None:
            return self._json({"error": "method_not_allowed"}, 405, headers={"Allow": "GET, PUT"})
        body: dict = {}
        if request.method == "PUT":
            raw = await request.body()
            try:
                body = json.loads(raw or b"{}") if len(raw) <= MAX_BODY else None
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._json({"status": "error", "error": "invalid_request",
                                   "message": "the body must be a small JSON object"}, 400)
        try:
            return self._json(await asyncio.to_thread(work, body))
        except UpdatesError as error:
            return self._json({"status": "error", "error": error.code, "message": str(error)}, error.status)
        except OSError as error:
            return self._json({"status": "error", "error": "storage_error",
                               "message": f"storage_error: {error.strerror or error}"}, 500)
        except ValueError:
            # .env that is not UTF-8 (UnicodeDecodeError) or cannot be split into bindings; the
            # exception text may quote it, so only the kind of failure reaches the page.
            return self._json({"status": "error", "error": "storage_error",
                               "message": "storage_error: .env could not be read"}, 500)

    @staticmethod
    def _json(value, status=200, **kwargs):
        headers = {"Cache-Control": "no-store", **kwargs.pop("headers", {})}
        return JSONResponse(value, status, headers=headers, **kwargs)

    def read(self, body: dict | None = None) -> dict:
        return {"version": agents_core_version(), "updates": overview.updates(self.service.directory, self.root)}

    def save(self, body: dict) -> dict:
        unknown = sorted(set(body) - FIELDS)
        if unknown:
            raise UpdatesError("invalid_request", f"unknown fields: {', '.join(unknown)}")
        expected, branch = body.get("expected_revision"), body.get("branch")
        allow_downgrade = body.get("allow_downgrade", False)
        if not isinstance(expected, str):
            raise UpdatesError("invalid_request", "expected_revision (the revision the page read) is required")
        if not isinstance(allow_downgrade, bool):
            raise UpdatesError("invalid_request", "allow_downgrade must be true or false")
        if branch not in update_channel.STANDARD_BRANCHES:
            raise UpdatesError("invalid_branch", "the page follows main or dev; another branch is set with "
                                                 "`python -m src.daemon channel set <branch>`")
        try:
            with file_lock(Path(self.service.directory) / "control.lock", blocking=False):
                saved = update_channel.set_branch(self.root, branch, allow_downgrade=allow_downgrade,
                                                  expected_revision=expected, environ={})
        except BlockingIOError:
            raise UpdatesError("busy", "another control command (an update, a restart) is running; try again "
                                       "when it has finished", 409) from None
        except update_channel.ChannelError as error:
            # git's stderr may name the remote's URL: no credentials in it reach the page.
            raise UpdatesError(error.code, overview.without_credentials(str(error)), STATUS.get(error.code, 400)) \
                from None
        return {**self.read(), "saved": saved}
