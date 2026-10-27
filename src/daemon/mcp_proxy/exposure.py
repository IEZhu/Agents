"""Request-scoped cached tools and unchanged upstream MCP results."""
import asyncio
import hashlib
import re

from mcp import types
from mcp.server.lowlevel.server import request_ctx


def tool_name(alias, original):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", original):
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", original)[:27]
        original = stem + "_" + hashlib.sha256(original.encode()).hexdigest()[:4]
    return alias + "__" + original


def proxy_context():
    context = request_ctx.get(None)
    request = getattr(context, "request", None)
    state = getattr(request, "state", None)
    client = getattr(state, "client_context", None)
    app = getattr(client, "client", None)
    if not app or app == "unknown":
        return None, None
    return getattr(state, "mcp_proxy", None), app


def visible(definition, name):
    policy = definition.get("tools", {})
    exposed = policy.get("expose", "all")
    return name not in policy.get("deny", []) and (exposed == "all" or name in exposed)


async def catalog(proxy, app):
    from src.mcp_registry import RegistryError
    try:
        entries = await asyncio.to_thread(proxy.cached, app)
    except (RegistryError, OSError):
        # Corrupt optional registry state cannot hide Agents-Core's own tools.
        return {}
    mapping = {}
    collisions = set()
    for definition, original in entries:
        if not visible(definition, original.name):
            continue
        name = tool_name(definition["alias"], original.name)
        if name in mapping:
            collisions.add(name)
        mapping[name] = (definition, original)
    # Ambiguous names must never dispatch to the wrong upstream tool.
    return {name: pair for name, pair in mapping.items() if name not in collisions}


class ProxyToolsMixin:
    async def list_tools(self):
        own = await super().list_tools()
        proxy, app = proxy_context()
        if proxy is None:
            return own
        for name, (definition, original) in (await catalog(proxy, app)).items():
            meta = dict(original.meta or {})
            if original.name in definition.get("tools", {}).get("always_load", []):
                meta["anthropic/alwaysLoad"] = True
            own.append(original.model_copy(update={
                "name": name, "icons": None, "meta": meta,
                "description": ("[" + definition.get("title", definition["alias"]) + "] " +
                                (original.description or ""))[:2048]}))
        return own

    async def call_tool(self, name, arguments):
        if "__" not in name:
            return await super().call_tool(name, arguments)
        proxy, app = proxy_context()
        found = (await catalog(proxy, app)).get(name) if proxy is not None else None
        if found is None:
            return types.CallToolResult(isError=True, content=[types.TextContent(
                type="text", text="Unknown or unavailable MCP proxy tool. Check MCP configuration and reconnect.")])
        definition, original = found
        from .upstreams import UpstreamError
        try:
            return await proxy.call(definition["alias"], original.name, arguments, app)
        except UpstreamError as error:
            return types.CallToolResult(isError=True, content=[types.TextContent(
                type="text", text=f"MCP proxy {definition['alias']}: {error.code}. Open /ui/mcp to reconnect or authorize.")])
