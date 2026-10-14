"""Small synchronous client for the installed, loopback Agents-Core MCP daemon.

Only preparation and validation are exposed. Installed Codex/bridge credentials
stay in memory: this module never logs headers, configuration paths, or response
bodies on transport errors. It does not register workspaces or retry requests.
"""

from copy import deepcopy
from dataclasses import dataclass
import http.client
import json
import math
import os
from pathlib import Path
import shlex
import time
import tomllib
from urllib.parse import urlsplit


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TOOLS = frozenset({"get_subagent_context", "validate_subagent_result"})
_PROTOCOL = "2025-11-25"


class MCPClientError(RuntimeError):
    """A sanitized configuration, transport, or protocol failure."""


@dataclass(frozen=True)
class TimedResult:
    data: dict
    elapsed_ms: float


class PreparedExchange:
    """Own an immutable serialized original; callers receive independent copies."""

    def __init__(self, package: dict, elapsed_ms: float):
        self._original = json.dumps(package, ensure_ascii=False, allow_nan=False)
        self.elapsed_ms = elapsed_ms

    @property
    def package(self) -> dict:
        return json.loads(self._original)

    @property
    def contract(self) -> dict:
        return self.package["contract"]

    @property
    def worker_context(self) -> str:
        return self.package["worker_context"]


def _endpoint(value):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                 and parsed.path == "/mcp" and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
        port = parsed.port or 80
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise MCPClientError("Only the local Agents-Core HTTP MCP endpoint is supported")
    return parsed.hostname, port, parsed.path


def _private_bridge(path):
    try:
        path = Path(path)
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise MCPClientError("Agents-Core bridge configuration must be private")
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError
        return data
    except MCPClientError:
        raise
    except (OSError, ValueError, TypeError):
        raise MCPClientError("Cannot read installed Agents-Core bridge configuration") from None


def _installed_config(config_path=None):
    """Read the named configuration or the documented Codex user configuration.

    Support direct HTTP headers and the installed bridge/header-helper forms.
    The known helper's JSON input is read directly; no configured program runs.
    """
    path = Path(config_path) if config_path else (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml")
    if path.suffix.lower() == ".json":
        config = _private_bridge(path)
        return config.get("url"), config.get("headers")
    try:
        document = tomllib.loads(path.read_text())
        entry = document["mcp_servers"]["Agents-Core"]
        if not isinstance(entry, dict):
            raise ValueError
        if "url" in entry:
            headers = dict(entry.get("http_headers", {}))
            # A merged entry may have both; use the installed static credentials.
            if not headers.get("Authorization") and entry.get("http_headers_helper"):
                parts = shlex.split(entry["http_headers_helper"])
                if len(parts) != 3 or not parts[1].replace("\\", "/").endswith("/bridge/headers.mjs"):
                    raise MCPClientError("Unsupported Agents-Core header helper")
                bridge = _private_bridge(parts[2])
                headers.update(bridge.get("headers", {}))
            return entry["url"], headers
        args = entry.get("args", [])
        if (len(args) != 2 or not isinstance(args[0], str)
                or not args[0].replace("\\", "/").endswith("/bridge/stdio.mjs")):
            raise MCPClientError("Unsupported installed Agents-Core transport")
        bridge = _private_bridge(args[1])
        return bridge.get("url"), bridge.get("headers")
    except MCPClientError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise MCPClientError("Cannot read installed Codex Agents-Core configuration") from None


class AgentsCoreClient:
    """Call the two stateless worker tools through a bounded local HTTP request.

    ``workspace`` is accepted for caller consistency; these tools do not require
    workspace registration or a workspace argument. The installed workspace
    header, when present, is preserved. Call ``initialize()`` during batch setup
    to measure the handshake separately; otherwise its time is included in the
    first call. No model inference is performed here.
    """

    def __init__(self, workspace=None, *, config_path=None, timeout=30.0,
                 endpoint=None, headers=None):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise MCPClientError("MCP timeout must be greater than zero and at most 120 seconds")
        if (endpoint is None) != (headers is None):
            raise MCPClientError("Explicit MCP endpoint and headers must be provided together")
        if endpoint is None:
            endpoint, headers = _installed_config(config_path)
        self._host, self._port, self._path = _endpoint(endpoint)
        if (not isinstance(headers, dict) or not isinstance(headers.get("Authorization"), str)
                or not headers["Authorization"].startswith("Bearer ")
                or not headers["Authorization"][7:].strip()):
            raise MCPClientError("Installed Agents-Core bearer credentials are missing")
        # Only known, relevant headers cross this boundary; never retain a config path.
        self._headers = {key: value for key, value in headers.items()
                         if key in {"Authorization", "X-Agents-Workspace", "X-Agents-Client"}}
        if any(not isinstance(value, str) or "\r" in value or "\n" in value
               for value in self._headers.values()):
            raise MCPClientError("Invalid installed Agents-Core headers")
        self.timeout = timeout
        self._next_id = 0
        self._initialized = False
        self._protocol = _PROTOCOL
        self._prepared = {}

    def _rpc(self, method, params=None, *, notification=False):
        self._next_id += 1
        request_id = self._next_id
        message = {"jsonrpc": "2.0", "method": method}
        if not notification:
            message["id"] = request_id
        if params is not None:
            message["params"] = params
        try:
            body = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise MCPClientError("MCP request must be JSON serializable") from None
        connection = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)
        headers = {**self._headers, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": self._protocol}
        try:
            connection.request("POST", self._path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status not in (200, 202, 204):
                # HTTPConnection never follows redirects; discard all error bodies.
                raise MCPClientError(f"Agents-Core HTTP {response.status}; request was not retried")
            if notification and response.status in (202, 204):
                return None
            if response.status != 200 or "application/json" not in response.getheader("Content-Type", ""):
                raise MCPClientError("Expected stateless JSON MCP response")
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(payload) > _MAX_RESPONSE_BYTES:
                raise MCPClientError("MCP response exceeds the client size limit")
            try:
                decoded = json.loads(payload)
            except (ValueError, UnicodeError):
                raise MCPClientError("Malformed JSON MCP response") from None
            if not isinstance(decoded, dict) or decoded.get("jsonrpc") != "2.0" or decoded.get("id") != request_id:
                raise MCPClientError("Mismatched MCP response identity")
            if "error" in decoded:
                raise MCPClientError("Agents-Core returned an RPC error; inspect tool arguments")
            if not isinstance(decoded.get("result"), dict):
                raise MCPClientError("MCP response result is not an object")
            return decoded["result"]
        except MCPClientError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise MCPClientError("Agents-Core connection failed or timed out; request was not retried") from None
        finally:
            connection.close()

    def _initialize(self):
        if self._initialized:
            return
        result = self._rpc("initialize", {"protocolVersion": _PROTOCOL, "capabilities": {},
                                          "clientInfo": {"name": "accord-repair-loop", "version": "1"}})
        protocol = result.get("protocolVersion")
        if not isinstance(protocol, str) or protocol not in {"2024-11-05", "2025-03-26", "2025-06-18", _PROTOCOL}:
            raise MCPClientError("Unsupported MCP protocol negotiated by daemon")
        self._protocol = protocol
        self._rpc("notifications/initialized", notification=True)
        self._initialized = True

    def initialize(self) -> float:
        """Negotiate MCP once and return elapsed milliseconds without calling a tool."""
        started = time.perf_counter()
        self._initialize()
        return (time.perf_counter() - started) * 1000

    @staticmethod
    def _decode_tool(result):
        if result.get("isError"):
            raise MCPClientError("Agents-Core tool call failed; inspect tool arguments")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        content = result.get("content", [])
        texts = [block.get("text") for block in content
                 if isinstance(block, dict) and block.get("type") == "text"] if isinstance(content, list) else []
        if len(texts) == 1 and isinstance(texts[0], str):
            try:
                decoded = json.loads(texts[0])
                if isinstance(decoded, dict):
                    return decoded
            except (ValueError, TypeError):
                pass
        raise MCPClientError("MCP tool returned no structured JSON object")

    def call(self, name: str, arguments: dict) -> tuple[dict, float]:
        if name not in _TOOLS or not isinstance(arguments, dict):
            raise MCPClientError("Only worker preparation and validation tools are supported")
        started = time.perf_counter()
        self._initialize()
        result = self._decode_tool(self._rpc("tools/call", {"name": name, "arguments": arguments}))
        elapsed_ms = (time.perf_counter() - started) * 1000
        if name == "get_subagent_context" and result.get("status") == "prepared":
            self._prepared[(result.get("task_id"), result.get("attempt_id"))] = deepcopy(result)
        return result, elapsed_ms

    def prepare(self, **arguments) -> PreparedExchange:
        data, elapsed_ms = self.call("get_subagent_context", arguments)
        if (data.get("status") != "prepared" or not isinstance(data.get("contract"), dict)
                or not isinstance(data.get("worker_context"), str)):
            raise MCPClientError("Agents-Core did not prepare a worker package")
        return PreparedExchange(data, elapsed_ms)

    def validate(self, prepared: PreparedExchange, result) -> TimedResult:
        if not isinstance(prepared, PreparedExchange):
            raise MCPClientError("Validation requires the original prepared exchange")
        data, elapsed_ms = self.call("validate_subagent_result", {"contract": prepared.contract, "result": result})
        return TimedResult(data, elapsed_ms)


MCPClient = AgentsCoreClient
