"""Lazy Streamable HTTP sessions with task-owned MCP context lifetimes.

Only an explicit connect catalogs tools. Listing uses that revision-bound local
catalog, and a call requires an enabled application and a confirmed catalog.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import time

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from ..state import private_dir, read_json, write_json


_proxy_log_context = ContextVar("mcp_proxy_log_context", default=False)


class _TransportLogFilter(logging.Filter):
    """The SDK can log malformed upstream bodies, including echoed credentials."""

    def filter(self, record):
        if _proxy_log_context.get():
            record.msg, record.args = "MCP upstream transport diagnostic (details withheld)", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


# The pinned SDK logs both to its transport/client loggers and directly to root.
# Context variables restrict this redaction to tasks belonging to this proxy.
for _logger_name in ("", "mcp.client.streamable_http", "client", "httpx"):
    logging.getLogger(_logger_name).addFilter(_TransportLogFilter())


class UpstreamError(RuntimeError):
    """A public failure category; upstream messages and credentials never escape."""

    def __init__(self, alias, code):
        self.alias, self.code = alias, code
        super().__init__(f"MCP upstream {alias}: {code}")


def _failure_code(error):
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
        return "authentication_failed"
    for nested in getattr(error, "exceptions", ()):
        code = _failure_code(nested)
        if code != "unavailable":
            return code
    return "unavailable"


def _exposed(definition, name):
    policy = definition.get("tools", {})
    expose = policy.get("expose", "all")
    return name not in policy.get("deny", []) and (expose == "all" or name in expose)


@dataclass
class _Operation:
    kind: str
    name: str | None
    arguments: dict | None
    future: asyncio.Future


class _Owner:
    """Enter, use and exit all AnyIO contexts in this single asyncio task."""

    def __init__(self, definition, headers, failed, output_schemas):
        self.definition = definition
        self.alias = definition["alias"]
        self.revision = definition["revision"]
        self.header_digest = hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).hexdigest()
        self._headers = headers
        self._failed = failed
        self._output_schemas = deepcopy(output_schemas)
        self.queue = asyncio.Queue(maxsize=32)
        self.accepting, self.active = True, False
        self.state = "connecting"
        self.ready = asyncio.get_running_loop().create_future()
        # Cancellation of a connect caller must not leave an unobserved exception.
        self.ready.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        self.task = asyncio.create_task(self._run(), name=f"mcp-upstream:{self.alias}")

    def drain(self):
        if self.accepting:
            self.accepting = False
            # A full queue is already bounded; the owner checks accepting after it empties.
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def submit(self, kind, name=None, arguments=None):
        if not self.accepting or self.task.done():
            raise UpstreamError(self.alias, "disconnected")
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait(_Operation(kind, name, arguments, future))
        except asyncio.QueueFull:
            raise UpstreamError(self.alias, "busy") from None
        return await future

    async def _catalog(self, session):
        tools, cursor, seen = [], None, set()
        for _ in range(100):
            page = await session.list_tools(cursor=cursor)
            tools.extend(page.tools)
            if len(tools) > 10000:
                raise UpstreamError(self.alias, "catalog_too_large")
            cursor = page.nextCursor
            if not cursor:
                if len({tool.name for tool in tools}) != len(tools):
                    raise UpstreamError(self.alias, "catalog_invalid")
                return tools
            if cursor in seen:
                raise UpstreamError(self.alias, "catalog_invalid")
            seen.add(cursor)
        raise UpstreamError(self.alias, "catalog_too_large")

    async def _run(self):
        timeout = float(self.definition.get("limits", {}).get("call_timeout_seconds", 60))
        idle = float(self.definition.get("limits", {}).get("idle_seconds", 600))
        current, failure = None, None
        log_context = _proxy_log_context.set(True)
        try:
            async with httpx.AsyncClient(headers=self._headers, trust_env=False,
                                         follow_redirects=False, timeout=timeout) as http:
                async with streamable_http_client(self.definition["transport"]["url"], http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        async with asyncio.timeout(timeout):
                            await session.initialize()
                        # MCP 1.28.1 otherwise calls tools/list after a successful tool
                        # call when this per-session schema cache is empty. Seed only
                        # the catalog explicitly confirmed for this definition revision.
                        session._tool_output_schemas.update(self._output_schemas)
                        self.state = "connected"
                        self.ready.set_result(None)
                        while self.accepting or not self.queue.empty():
                            try:
                                async with asyncio.timeout(idle):
                                    current = await self.queue.get()
                            except TimeoutError:
                                break
                            if current is None:
                                break
                            if current.future.cancelled():
                                current = None
                                continue
                            self.active = True
                            async with asyncio.timeout(timeout):
                                result = (await self._catalog(session) if current.kind == "catalog" else
                                          await session.call_tool(current.name, current.arguments))
                            self.active = False
                            if not current.future.done():
                                current.future.set_result(result)
                            current = None
        except asyncio.CancelledError:
            failure = "disconnected"
            raise
        except Exception as error:
            failure = error.code if isinstance(error, UpstreamError) else _failure_code(error)
        finally:
            self._headers = {}
            self.accepting, self.active, self.state = False, False, "closed"
            if failure:
                self._failed(self.alias, failure)
            error = UpstreamError(self.alias, failure or "disconnected")
            if not self.ready.done():
                self.ready.set_exception(error)
            if current is not None and not current.future.done():
                current.future.set_exception(error)
            while not self.queue.empty():
                pending = self.queue.get_nowait()
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(error)
            _proxy_log_context.reset(log_context)


class Supervisor:
    """One serialized, lazily opened session per alias; no credential-bearing status."""

    def __init__(self, registry, secrets, state_dir):
        self.registry, self.secrets = registry, secrets
        self.state_dir = Path(state_dir)
        self.catalog_path = self.state_dir / "mcp-proxy-catalogs.json"
        self._owners, self._locks, self._failures = {}, {}, {}
        self._draining = False
        try:
            stored = read_json(self.catalog_path, {})
            candidates = stored.get("catalogs", {}) if stored.get("version") == 1 else {}
            self._catalogs = {}
            if isinstance(candidates, dict):
                for alias, entry in candidates.items():
                    if not isinstance(entry, dict) or not isinstance(entry.get("revision"), str) or not isinstance(entry.get("tools"), list):
                        continue
                    try:
                        for item in entry["tools"]:
                            types.Tool.model_validate(item)
                    except (ValueError, TypeError):
                        continue
                    self._catalogs[alias] = entry
        except (OSError, ValueError, AttributeError, RecursionError):
            self._catalogs = {}

    def _definition(self, alias, app=None):
        try:
            definition = self.registry.get(alias)
            approved = (self.registry.resolved(app) if app is not None else
                        [item for item in self.registry.list() if item.get("enabled") and item.get("confirmed") and item.get("apps")])
            if not any(item["alias"] == alias and item.get("revision") == definition["revision"] for item in approved):
                raise UpstreamError(alias, "not_enabled")
        except UpstreamError:
            raise
        except Exception:
            raise UpstreamError(alias, "definition_unavailable") from None
        if definition.get("transport", {}).get("type") != "http":
            raise UpstreamError(alias, "unsupported_transport")
        return definition

    def _failed(self, alias, code):
        failures = self._failures.get(alias, (0, 0, None))[0] + 1
        self._failures[alias] = (failures, time.monotonic() + min(30, 2 ** min(failures - 1, 5)), code)

    async def _owner(self, definition):
        alias = definition["alias"]
        async with self._locks.setdefault(alias, asyncio.Lock()):
            if self._draining:
                raise UpstreamError(alias, "draining")
            try:
                declared = {item["name"].lower() for item in definition.get("secrets", [])}
                stored = await asyncio.to_thread(self.secrets.load, alias) if declared else {}
                loaded = {name.lower(): value for name, value in stored.items() if name.lower() in declared}
                headers = {**{name.lower(): value for name, value in definition["transport"].get("headers", {}).items()}, **loaded}
                required = [item["name"].lower() for item in definition.get("secrets", []) if item.get("required")]
                if any(not headers.get(name) for name in required):
                    raise UpstreamError(alias, "credentials_missing")
            except UpstreamError:
                raise
            except Exception:
                raise UpstreamError(alias, "credentials_unavailable") from None
            if self._draining:
                raise UpstreamError(alias, "draining")
            digest = hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).hexdigest()
            owner = self._owners.get(alias)
            if owner is not None and (owner.revision != definition["revision"] or owner.header_digest != digest):
                owner.drain()
                await asyncio.shield(owner.task)
                owner = None
                self._failures.pop(alias, None)
            if owner is not None and (owner.task.done() or not owner.accepting):
                await asyncio.shield(owner.task)
                owner = None
            if self._draining:
                raise UpstreamError(alias, "draining")
            if owner is None:
                if time.monotonic() < self._failures.get(alias, (0, 0, None))[1]:
                    raise UpstreamError(alias, "backoff")
                entry = self._catalogs.get(alias, {})
                schemas = ({item["name"]: item.get("outputSchema") for item in entry["tools"]}
                           if entry.get("revision") == definition["revision"] else {})
                owner = self._owners[alias] = _Owner(deepcopy(definition), headers, self._failed, schemas)
            await asyncio.shield(owner.ready)
            return owner, loaded

    async def connect(self, alias, app=None):
        """Explicitly initialize, fetch and persist a catalog for this exact revision."""
        if self._draining:
            raise UpstreamError(alias, "draining")
        definition = self._definition(alias, app)
        owner, credentials = await self._owner(definition)
        tools = await owner.submit("catalog")
        if self._definition(alias, app)["revision"] != definition["revision"]:
            raise UpstreamError(alias, "revision_changed")
        serialized = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
        content = json.dumps(serialized, ensure_ascii=False)
        markers = set(credentials.values())
        markers.update(value[7:] for value in credentials.values() if value.lower().startswith("bearer "))
        # Refuse an echoed credential rather than storing a corrupted tool schema.
        def echoed(value):
            if isinstance(value, str):
                return any(marker and marker in value for marker in markers)
            if isinstance(value, dict):
                return any(echoed(key) or echoed(item) for key, item in value.items())
            return isinstance(value, list) and any(echoed(item) for item in value)

        if echoed(serialized):
            owner.drain()
            raise UpstreamError(alias, "catalog_contains_credentials")
        if len(content.encode()) > 4 * 1024 * 1024:
            owner.drain()
            raise UpstreamError(alias, "catalog_too_large")
        entry = {"revision": definition["revision"], "tools": serialized}
        previous = self._catalogs.get(alias)
        self._catalogs[alias] = entry
        try:
            private_dir(self.state_dir)
            write_json(self.catalog_path, {"version": 1, "catalogs": self._catalogs})
        except (OSError, ValueError):
            owner.drain()
            if previous is None:
                self._catalogs.pop(alias, None)
            else:
                self._catalogs[alias] = previous
            raise UpstreamError(alias, "catalog_write_failed") from None
        self._failures.pop(alias, None)
        return [tool.model_copy(deep=True) for tool in tools]

    def cached(self, app):
        """Return only enabled and revision-confirmed tools, without connecting."""
        if app is None or self._draining:
            return []
        result = []
        for definition in self.registry.resolved(app):
            entry = self._catalogs.get(definition["alias"], {})
            if not isinstance(entry, dict) or entry.get("revision") != definition["revision"]:
                continue
            try:
                tools = [types.Tool.model_validate(deepcopy(item)) for item in entry.get("tools", [])]
            except (ValueError, TypeError):
                continue
            result.extend((deepcopy(definition), tool) for tool in tools if _exposed(definition, tool.name))
        return result

    async def call(self, alias, tool, arguments, app):
        """Forward a confirmed tool and preserve its complete MCP result."""
        if self._draining:
            raise UpstreamError(alias, "draining")
        if app is None:
            raise UpstreamError(alias, "app_required")
        definition = self._definition(alias, app)
        if not any(item["alias"] == alias and cached.name == tool for item, cached in self.cached(app)):
            raise UpstreamError(alias, "tool_not_cataloged")
        owner, _ = await self._owner(definition)
        if self._definition(alias, app)["revision"] != definition["revision"]:
            raise UpstreamError(alias, "revision_changed")
        result = await owner.submit("call", tool, arguments)
        self._failures.pop(alias, None)
        return result

    def health(self):
        """An in-memory service summary, with no registry or secret-store reads."""
        return {"servers": len(self._owners.keys() | self._catalogs.keys()),
                "ready": sum(owner.state == "connected" for owner in self._owners.values()),
                "failed": len(self._failures)}

    def status(self):
        result = []
        definitions = self.registry.list()
        owners, catalogs, failures = self._owners.copy(), self._catalogs.copy(), self._failures.copy()
        for definition in definitions:
            alias, revision = definition["alias"], definition.get("revision")
            owner, entry = owners.get(alias), catalogs.get(alias, {})
            failure = failures.get(alias, (0, 0, None))
            current = bool(revision) and isinstance(entry, dict) and entry.get("revision") == revision
            state = "invalid" if not revision else "draining" if self._draining else owner.state if owner else "disconnected"
            result.append({"alias": alias, "revision": revision,
                           "state": state,
                           "catalog_confirmed": current,
                           "tools": len(entry.get("tools", [])) if current else 0,
                           "active_calls": int(owner.active) if owner else 0,
                           "error": "definition_invalid" if not revision else failure[2], "retry_in_seconds": max(0, round(failure[1] - time.monotonic(), 2))})
        return result

    def begin_drain(self):
        self._draining = True
        for owner in self._owners.values():
            owner.drain()

    def resume(self):
        """Allow new work after a service drain; reopen sessions only when used."""
        self._draining = False

    async def close(self):
        """Reject new operations and wait for accepted calls before closing contexts."""
        self.begin_drain()
        if self._owners:
            await asyncio.shield(asyncio.gather(*(owner.task for owner in self._owners.values()), return_exceptions=True))
