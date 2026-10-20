#!/usr/bin/env python3
"""Stdlib stdio bridge: one HTTP attempt per message, with no write replay.

This file deliberately imports no installation modules: clients launch it with
an absolute Python executable, this script, and their private bridge config.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import stat
import sys
import threading
from urllib.parse import quote, unquote, urlsplit
from urllib.request import url2pathname


MAX_BYTES = 16 * 1024 * 1024
MAX_PENDING = 32
HISTORY_TOOLS = {"log_interaction", "read_history"}


def load_config(path: str, *, allow_stdio: bool = False) -> tuple[dict, object]:
    # O_NONBLOCK lets the regular-file check reject a FIFO without hanging.
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise ValueError("Private regular config required")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        # Windows uses the service directory's owner-only ACL (src/daemon/acl.py).
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
            raise ValueError("Private config (0600) required")
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Bridge config too large")
    config = json.loads(raw)
    if not isinstance(config, dict):
        raise ValueError("Invalid bridge config")
    alias = config.get("alias")
    if "alias" in config and (not isinstance(alias, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,11}", alias) or "__" in alias):
        raise ValueError("Invalid proxy alias")
    headers = config.get("headers")
    if not isinstance(headers, dict) or not isinstance(headers.get("Authorization"), str) or not headers["Authorization"].strip():
        raise ValueError("Authenticated bridge config required")
    for name, value in headers.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or not isinstance(value, str) or any(ord(char) < 32 or ord(char) > 126 for char in value)):
            raise ValueError("Invalid bridge headers")
        if name.lower() in {"host", "content-length", "transfer-encoding", "connection"}:
            raise ValueError("Invalid bridge headers")
    if allow_stdio and config.get("transport") == "stdio":
        installation = config.get("installation")
        if not isinstance(installation, str) or not os.path.isabs(installation):
            raise ValueError("Invalid native workspace config")
        return config, None
    url = config.get("url")
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) >= 127 for char in url):
        raise ValueError("Invalid local endpoint")
    endpoint = urlsplit(url)
    if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1"
            or endpoint.path != "/mcp" or endpoint.query or endpoint.fragment
            or endpoint.username is not None or endpoint.password is not None
            or endpoint.port == 0):
        raise ValueError("Only the local Agents-Core endpoint is supported")
    return config, endpoint


class RegistrationError(Exception):
    def __init__(self, message: str, refused: bool = False):
        super().__init__(message)
        self.refused = refused


class Bridge:
    def __init__(self, config: dict, endpoint):
        self.config, self.endpoint = config, endpoint
        self.alias = config.get("alias")
        self.prefix = self.alias + "__" if self.alias else None
        self.protocol = "2025-11-25"
        self.client_roots = False
        self.session = None
        # A signed path must not seed an unsigned roots-verified registration.
        self.named: dict[str, asyncio.Task] = {}
        self.signed: dict[str, asyncio.Task] = {}
        self.answers: dict[str, asyncio.Future] = {}
        self.requests = self.signed_request = self.bound_signed_request = 0
        overridden = os.environ.get("AGENTS_CLIENT_REPO_ROOT", "").strip()
        self.override = os.path.abspath(os.path.expanduser(overridden)) if config.get("workspace") == "auto" and overridden else None
        self.native_token = None
        native_path = os.environ.get("AGENTS_WORKSPACE_AUTH_CONFIG")
        if native_path:
            try:
                native, _ = load_config(native_path, allow_stdio=True)
                installation = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
                if native.get("transport") == "stdio" and os.path.realpath(native["installation"]) == installation:
                    self.native_token = re.sub(r"^Bearer\s+", "", native["headers"]["Authorization"], flags=re.I).strip()
            except (OSError, ValueError, TypeError, RecursionError):
                pass  # Unverified launcher config grants no workspace identity.

    def send(self, message):
        sys.stdout.write(json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def log(self, message):
        # Diagnostics never reveal the authentication token, including daemon errors.
        authorization = self.config["headers"]["Authorization"]
        token = re.sub(r"^Bearer\s+", "", authorization, flags=re.I).strip()
        for secret in (authorization, token):
            if secret:
                message = message.replace(secret, "[redacted]")
        sys.stderr.write("Agents-Core bridge: " + re.sub(r"[\r\n\x00-\x1f]", " ", message[:2048]) + "\n")

    def http(self, path, message, headers, timeout):
        # HTTPConnection uses the literal loopback host and ignores proxy env vars.
        connection = http.client.HTTPConnection("127.0.0.1", self.endpoint.port or 80, timeout=timeout)
        transport = None
        expired = threading.Event()

        def expire():
            # A socket inactivity timeout alone permits an endless trickle response.
            expired.set()
            if transport is not None:
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                transport.close()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            body = json.dumps(message, ensure_ascii=True, allow_nan=False).encode("utf-8")
            if len(body) > MAX_BYTES:
                raise ValueError("Request too large")
            connection.connect()
            transport = connection.sock
            if expired.is_set():
                raise TimeoutError("HTTP request timed out")
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("Response too large")
            return response.status, response.getheader("Content-Type", ""), raw
        finally:
            timer.cancel()
            connection.close()

    async def register(self, request):
        status, _, raw = await asyncio.to_thread(self.http, "/workspaces", request, {
            "Authorization": self.config["headers"]["Authorization"], "Content-Type": "application/json",
        }, 30)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        identity = body.get("workspace_id")
        if not 200 <= status < 300 or not isinstance(identity, str) or not identity.strip() or any(ord(c) < 33 or ord(c) > 126 for c in identity):
            message = f"{body.get('error')}: {body['message']}" if body.get("message") else f"Daemon HTTP {status}"
            raise RegistrationError(message, refused=status == 400)
        return identity

    async def session_workspace(self):
        if self.config.get("workspace") != "auto":
            return None
        if self.session is None:
            named = os.environ.get("CLAUDE_PROJECT_DIR")
            request = {"path": self.override, "origin": "AGENTS_CLIENT_REPO_ROOT"} if self.override else (
                {"path": named, "origin": "CLAUDE_PROJECT_DIR"} if named else {"path": os.getcwd()})

            async def bind():
                try:
                    return await self.register(request)
                except Exception as error:
                    self.log(f"no workspace for this session ({error}).")
                    # A delayed launch-directory failure cannot erase a signed binding.
                    if not getattr(error, "refused", False) and self.session is identity:
                        self.session = None
                    return None

            identity = asyncio.create_task(bind())
            self.session = identity
        return await self.session

    async def ask_roots(self):
        identity = f"agents-core-bridge-{self.requests}"
        self.requests += 1
        answer = asyncio.get_running_loop().create_future()
        self.answers[identity] = answer
        self.send({"jsonrpc": "2.0", "id": identity, "method": "roots/list"})
        try:
            result = await asyncio.wait_for(answer, 5)
            if not isinstance(result, dict) or not isinstance(result.get("roots"), list):
                raise RegistrationError("workspace_invalid: invalid client roots")
            roots = result["roots"]
            if any(not isinstance(root, dict) or not isinstance(root.get("uri"), str) for root in roots):
                raise RegistrationError("workspace_invalid: invalid client roots")
            return [root["uri"] for root in roots]
        finally:
            self.answers.pop(identity, None)

    @staticmethod
    def sole_root(roots):
        paths = set()
        for uri in roots:
            try:
                root = urlsplit(uri)
                if root.scheme != "file" or root.query or root.fragment or not root.path.startswith("/"):
                    continue
                # Decode exactly once; the daemon independently verifies every root.
                remote = root.netloc and root.netloc.lower() != "localhost"
                if remote and os.name != "nt":
                    continue
                uri_path = f"//{root.netloc}{root.path}" if remote else root.path
                path = url2pathname(uri_path) if os.name == "nt" else unquote(uri_path, errors="strict")
                if path and "\x00" not in path:
                    paths.add(os.path.realpath(path))
            except (ValueError, OSError):
                continue
        if len(paths) != 1:
            raise RegistrationError("workspace_required: the client has no unique workspace; pass workspace")
        return paths.pop()

    async def cached_registration(self, cache, key, request):
        if key not in cache:
            cache[key] = asyncio.create_task(self.register(request))
        identity = cache[key]
        try:
            return await identity
        except Exception:
            if cache.get(key) is identity:
                cache.pop(key, None)
            raise

    def valid_signature(self, workspace, signature):
        token = re.sub(r"^Bearer\s+", "", self.config["headers"]["Authorization"], flags=re.I).strip()
        if not workspace or not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
            return False
        try:
            for secret in (token, self.native_token):
                if secret:
                    expected = hmac.new(secret.encode("utf-8"), ("workspace:" + workspace).encode("utf-8"), hashlib.sha256).hexdigest()
                    if hmac.compare_digest(signature.lower(), expected):
                        return True
            return False
        except UnicodeEncodeError:
            return False

    async def workspace_for(self, message):
        params = message.get("params")
        args = params.get("arguments") if message["method"] == "tools/call" and isinstance(params, dict) else None
        args = args if isinstance(args, dict) else {}
        workspace = args.get("workspace")
        workspace = workspace.strip() if isinstance(workspace, str) else ""
        signature = args.pop("workspace_signature", None)
        signature = signature.strip() if isinstance(signature, str) else ""
        authenticated = bool(signature) and self.valid_signature(workspace, signature)
        if authenticated:
            args.pop("workspace", None)  # Authenticated hint is transport metadata.
        if self.override:
            return await self.session_workspace()
        # A private config's fixed HTTP identity outranks tool arguments/roots.
        if self.config["headers"].get("X-Agents-Workspace"):
            return None
        if signature:
            if authenticated:
                self.signed_request += 1
                request = self.signed_request
                try:
                    identity = await self.cached_registration(self.signed, workspace, {"path": workspace, "origin": "hook"})
                    if self.config.get("workspace") == "auto" and request > self.bound_signed_request:
                        self.bound_signed_request = request
                        self.session = asyncio.get_running_loop().create_future()
                        self.session.set_result(identity)
                    return identity
                except Exception as error:
                    self.log(f"signed workspace refused ({error}).")
            else:
                self.log("invalid workspace signature; ignored.")
        if message["method"] != "tools/call" or not self.client_roots or (self.prefix and not workspace):
            return await self.session_workspace()
        try:
            # Re-read roots on each selection: a shared bridge never pins a first project.
            roots = await self.ask_roots()
            selected = workspace or self.sole_root(roots)
            key = json.dumps([selected, roots], ensure_ascii=True)
            return await self.cached_registration(self.named, key, {"path": selected, "roots": roots})
        except Exception as error:
            if not workspace:
                # Forward workspace-independent tools. The daemon itself refuses
                # repository operations without a verified identity.
                self.log(f"no unique workspace ({error}).")
                return None
            own = await self.session_workspace() if workspace else None
            if not own:
                raise
            self.log(f"workspace refused ({error}); using this session's project.")
            return own

    def claim(self, message):
        params = message.get("params")
        args = params.get("arguments") if isinstance(params, dict) else None
        named = args.get("workspace") if isinstance(args, dict) else None
        named = named.strip() if isinstance(named, str) else ""
        claim = named or self.override or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        if len(claim) > 4096:
            return None
        try:
            return quote(claim, safe="~()*!.'-_")
        except UnicodeEncodeError:
            return None

    async def forward(self, message):
        has_id = "id" in message

        def error(code, text):
            if has_id:
                self.send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": code, "message": text}})

        method = message["method"]
        params = message.get("params")
        if self.prefix and method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                error(-32602, "Tool name required")
                return
            params["name"] = self.prefix + params["name"]
        if self.prefix and re.match(r"^(prompts|resources)/", method):
            error(-32601, "This proxy view exposes tools only")
            return
        if method == "initialize":
            capabilities = params.get("capabilities") if isinstance(params, dict) else None
            self.client_roots = isinstance(capabilities, dict) and "roots" in capabilities and capabilities["roots"] is not False and capabilities["roots"] is not None
        if method == "notifications/roots/list_changed":
            self.named.clear()
        tool = params.get("name") if method == "tools/call" and isinstance(params, dict) else None
        workspace, refused = None, None
        try:
            workspace = await self.workspace_for(message)
        except Exception as problem:
            if tool not in HISTORY_TOOLS:
                error(-32602, f"Agents-Core refused the workspace: {problem}")
                return
            match = re.match(r"workspace_(required|invalid|unsafe)\b", str(problem))
            refused = match.group(0) if match else None
            self.log(f"workspace refused ({problem}); the turn goes to the fallback journal.")
        headers = dict(self.config["headers"])
        if workspace:
            headers["X-Agents-Workspace"] = workspace
        if tool in HISTORY_TOOLS and not workspace and not headers.get("X-Agents-Workspace"):
            claim = self.claim(message)
            if claim:
                headers["X-Agents-Claimed-Workspace"] = claim
        if refused:
            headers["X-Agents-Workspace-Refused"] = refused
        if method == "initialize":
            params = dict(params) if isinstance(params, dict) else {}
            params["capabilities"] = {}
            message["params"] = params
            if isinstance(params.get("protocolVersion"), str) and params["protocolVersion"]:
                self.protocol = params["protocolVersion"]
        headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": self.protocol})
        try:
            status, content_type, raw = await asyncio.to_thread(self.http, "/mcp", message, headers, 120)
            if not 200 <= status < 300:
                raise ValueError("Upstream HTTP failure")
            if status in (202, 204):
                return
            if "application/json" not in content_type:
                raise ValueError("Expected stateless JSON response")
            result = json.loads(raw)
            if has_id:
                if not isinstance(result, dict) or "id" not in result or result["id"] != message["id"] or isinstance(result["id"], bool):
                    raise ValueError("Mismatched response ID")
                payload = result.get("result")
                if method == "initialize" and isinstance(payload, dict):
                    if isinstance(payload.get("protocolVersion"), str) and payload["protocolVersion"]:
                        self.protocol = payload["protocolVersion"]
                    if self.prefix:
                        info = payload.get("serverInfo")
                        payload["serverInfo"] = {**(info if isinstance(info, dict) else {}), "name": self.alias}
                        capabilities = payload.get("capabilities")
                        payload["capabilities"] = {"tools": capabilities.get("tools", {}) if isinstance(capabilities, dict) else {}}
                        payload["instructions"] = f"Tools for {self.alias} through the shared Agents-Core daemon. Do not start another upstream server."
                if self.prefix and method == "tools/list" and isinstance(payload, dict) and isinstance(payload.get("tools"), list):
                    payload["tools"] = [{**tool, "name": tool["name"][len(self.prefix):]} for tool in payload["tools"]
                        if isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"].startswith(self.prefix)]
                self.send(result)
        except Exception:
            error(-32000, "Agents-Core unavailable or response uncertain; no automatic retry. Read state before repeating a write.")
            self.log("upstream request failed; not replayed.")

    async def run(self):
        loop = asyncio.get_running_loop()
        lines = asyncio.Queue(maxsize=64)

        def read_input():
            try:
                while line := sys.stdin.buffer.readline(MAX_BYTES + 1):
                    if len(line) > MAX_BYTES:
                        while line and not line.endswith(b"\n"):
                            line = sys.stdin.buffer.readline(MAX_BYTES + 1)
                        line = b"!oversized"
                    asyncio.run_coroutine_threadsafe(lines.put(line), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(lines.put(None), loop).result()

        threading.Thread(target=read_input, daemon=True).start()
        pending = set()
        while (line := await lines.get()) is not None:
            if not line.strip():
                continue
            try:
                message = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")))
            except (ValueError, UnicodeDecodeError, RecursionError):
                message = None
            if isinstance(message, dict) and message.get("jsonrpc") == "2.0" and "method" not in message and isinstance(message.get("id"), str) and message["id"] in self.answers:
                answer = self.answers.pop(message["id"])
                if message.get("error"):
                    problem = message["error"]
                    text = problem.get("message", "client error") if isinstance(problem, dict) else "client error"
                    answer.set_exception(RegistrationError(str(text)))
                else:
                    answer.set_result(message.get("result"))
                continue
            if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str)
                    or ("id" in message and (not isinstance(message["id"], (str, int, float, type(None))) or isinstance(message["id"], bool)))):
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC message"}})
                continue
            if len(pending) >= MAX_PENDING:
                if "id" in message:
                    self.send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "busy"}})
                continue
            task = asyncio.create_task(self.forward(message))
            pending.add(task)
            task.add_done_callback(pending.discard)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError("Private bridge config path required")
        config, endpoint = load_config(sys.argv[1])
        asyncio.run(Bridge(config, endpoint).run())
    except (OSError, ValueError, RecursionError):
        # Config/endpoint errors must never echo a URL or a token.
        sys.stderr.write("Agents-Core bridge: invalid private configuration or endpoint.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
