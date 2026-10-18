"""Shared authenticated UI/admin operations for manual HTTP MCP connections."""
import asyncio
import json

from starlette.responses import JSONResponse


async def handle(service, request):
    from src.mcp_registry import RegistryError
    from src.mcp_secrets import SecretsError
    from .mcp_proxy.upstreams import UpstreamError
    headers = {"Cache-Control": "no-store"}
    try:
        if request.method == "GET":
            records = await asyncio.to_thread(service.mcp_registry.list)
            states = await asyncio.to_thread(service.mcp_proxy.status)
            return JSONResponse({"servers": records, "runtime": states}, headers=headers)
        if request.method != "POST":
            return JSONResponse({"error": "method_not_allowed"}, 405, headers=headers)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                return JSONResponse({"error": "request_too_large"}, 413, headers=headers)
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        action, alias = body.get("action"), body.get("alias")
        if action == "save" and set(body) == {"action", "definition"}:
            result = await asyncio.to_thread(service.mcp_registry.save, body["definition"])
        elif action == "enable" and set(body) == {"action", "alias", "apps", "revision"}:
            result = await asyncio.to_thread(service.mcp_registry.enable, alias, body["apps"], body["revision"])
        elif action == "secret" and set(body) == {"action", "alias", "name", "value"}:
            result = await asyncio.to_thread(service.mcp_secrets.set, alias, body["name"], body["value"])
        elif action == "connect" and set(body) == {"action", "alias"}:
            tools = await service.mcp_proxy.connect(alias)
            result = {"alias": alias, "tools": len(tools), "reconnect_required": True}
        else:
            raise ValueError()
        return JSONResponse({"result": result}, headers=headers)
    except (RegistryError, SecretsError, UpstreamError) as error:
        return JSONResponse({"error": error.code, "message": getattr(error, "message", str(error))}, 400, headers=headers)
    except (ValueError, TypeError, KeyError, UnicodeError):
        return JSONResponse({"error": "invalid_request"}, 400, headers=headers)

