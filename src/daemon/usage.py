"""How Agents-Core is used on this machine, for the settings page (#187, #188).

Two sources, both local:

* Answer counts and recent answers come from the history files of the registered workspaces
  (``src/memory/history_stats.py``).
* AI apps are counted as they use the daemon. A request names its app in ``X-Agents-Client``,
  which the managed client configurations send (``clients.py``); one without it counts as
  ``unknown``. The MCP ``initialize`` request also carries the client's name and version in
  ``clientInfo``; with stateless HTTP it is the only request that does, so the daemon keeps the
  latest one per app. Apps that run Agents-Core over stdio never reach the daemon.

Nothing here leaves the machine, and nothing holds the text of a query or an answer.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from pathlib import Path

from src.memory.history_stats import FileSummaries, summarize
from .state import read_json

CONNECTED_SECONDS = 300  # an app with a request this recent counts as connected
MAX_APPS = 16  # distinct X-Agents-Client values kept; further ones count as unknown
MAX_OBSERVED = 64 * 1024  # request bytes read for an initialize, which is far smaller
UNKNOWN = "unknown"
_APP = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def app_name(value) -> str | None:
    """The app that ``X-Agents-Client`` names, or None for a missing or malformed value."""
    value = (value or "").strip().lower()
    return value if _APP.fullmatch(value) else None


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:limit] or None


def _iso(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat(timespec="seconds")


class AppActivity:
    """Requests, open notification streams and ``initialize`` calls per app since the daemon started."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._apps: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _record(self, app: str | None, now: float) -> dict:
        key = app or UNKNOWN
        if key not in self._apps and len(self._apps) >= MAX_APPS:
            key = UNKNOWN
        record = self._apps.setdefault(key, {
            "last_seen": None, "last_request": None, "streams": 0, "day": None, "requests_today": 0,
            "client": None, "version": None, "initialized": None, "workspace": None})
        record["last_seen"] = now
        day = dt.datetime.fromtimestamp(now).date()
        if record["day"] != day:
            record["day"], record["requests_today"] = day, 0
        return record

    def request(self, app: str | None) -> None:
        with self._lock:
            record = self._record(app, self.clock())
            record["requests_today"] += 1
            record["last_request"] = record["last_seen"]

    def stream_opened(self, app: str | None) -> None:
        with self._lock:
            self._record(app, self.clock())["streams"] += 1

    def stream_closed(self, app: str | None) -> None:
        with self._lock:
            record = self._record(app, self.clock())
            record["streams"] = max(0, record["streams"] - 1)

    def observe(self, body: bytes, app: str | None, workspace: str | None) -> None:
        """Keep the ``clientInfo`` of an ``initialize`` in a request body; other bodies are ignored."""
        if b'"initialize"' not in body:
            return
        try:
            message = json.loads(body)
        except ValueError:
            return
        for item in message if isinstance(message, list) else [message]:
            if not isinstance(item, dict) or item.get("method") != "initialize":
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
            with self._lock:
                record = self._record(app, self.clock())
                record.update(client=_text(info.get("name"), 64), version=_text(info.get("version"), 32),
                              initialized=record["last_seen"], workspace=workspace)

    def snapshot(self) -> list[dict]:
        now = self.clock()
        today = dt.datetime.fromtimestamp(now).date()
        with self._lock:
            return [{
                "app": app,
                # A closed stream is when the app was last seen, not a request: it does not keep it connected.
            "connected": record["streams"] > 0 or (record["last_request"] is not None
                                                   and now - record["last_request"] <= CONNECTED_SECONDS),
                "streams": record["streams"],
                "last_seen": _iso(record["last_seen"]),
                "requests_today": record["requests_today"] if record["day"] == today else 0,
                "client": record["client"], "version": record["version"],
                "initialized": _iso(record["initialized"]), "workspace": record["workspace"],
            } for app, record in sorted(self._apps.items())]


def observing(receive, apps: AppActivity, app: str | None, workspace: str | None):
    """``receive`` that also shows the request body to ``apps.observe`` once it has arrived whole.

    Only the bytes are seen: the transport still reads every message, and a body larger than
    ``MAX_OBSERVED`` is not an ``initialize`` and is not kept.
    """
    seen = bytearray()
    state = {"done": False}

    async def observed():
        message = await receive()
        if not state["done"] and message.get("type") == "http.request":
            body = message.get("body", b"")
            if len(seen) + len(body) > MAX_OBSERVED:
                state["done"] = True
                seen.clear()
            else:
                seen.extend(body)
                if not message.get("more_body", False):
                    state["done"] = True
                    apps.observe(bytes(seen), app, workspace)
        return message

    return observed


class Usage:
    """``GET /ui/api/stats``: answer counts, recent answers and the apps that use the daemon."""

    def __init__(self, service, clock=time.time):
        self.service = service
        self.clock = clock
        self.apps = AppActivity(clock)
        self.files = FileSummaries()

    def stats(self) -> dict:
        """Blocks on the registry and the history files; the daemon calls it off the event loop."""
        try:
            records = read_json(self.service.registry.path, {})
        except ValueError:
            records = None
        workspaces = sorted(((identity, Path(root).name, Path(root)) for identity, root in records.items()
                             if isinstance(identity, str) and isinstance(root, str)),
                            key=lambda item: str(item[2])) if isinstance(records, dict) else []
        now = dt.datetime.fromtimestamp(self.clock()).astimezone()
        answers = summarize(workspaces, now=now, files=self.files)
        if not isinstance(records, dict):
            answers["skipped"].append({"workspace": None, "name": None, "reason": "registry_unreadable"})
        names = {identity: name for identity, name, _ in workspaces}
        apps = self.apps.snapshot()
        for app in apps:
            app["workspace"] = names.get(app["workspace"])
        return {"computed_at": now.isoformat(timespec="seconds"),
                "uptime_seconds": round(time.monotonic() - self.service.started),
                "connected_window_seconds": CONNECTED_SECONDS,
                "events": answers.pop("events"), "skipped": answers.pop("skipped"),
                "answers": answers, "apps": apps}
