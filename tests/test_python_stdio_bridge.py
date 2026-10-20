"""Real subprocess coverage for the Python bridge, with only a local fake daemon."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
from unittest import mock
import importlib.util
import ntpath
import nturl2path
from types import SimpleNamespace


BRIDGE = Path(__file__).resolve().parents[1] / "bridge" / "stdio.py"
TOKEN = "bridge-test-secret"


def call(identity, workspace=None, signature=None, name="describe_repo"):
    args = {}
    if workspace is not None:
        args["workspace"] = workspace
    if signature is not None:
        args["workspace_signature"] = signature
    return {"jsonrpc": "2.0", "id": identity, "method": "tools/call", "params": {"name": name, "arguments": args}}


def signature(workspace):
    return hmac.new(TOKEN.encode(), ("workspace:" + workspace).encode(), hashlib.sha256).hexdigest()


class Daemon:
    def __init__(self, registration=None, response=None):
        self.registrations, self.calls, self.authorization = [], [], []
        self.registration = registration or self.bind
        self.response = response
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                authorization = self.headers.get("Authorization")
                daemon.authorization.append(authorization)
                if authorization != "Bearer " + TOKEN:
                    self.send_error(401)
                    return
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/workspaces":
                    daemon.registrations.append(message)
                    status, payload = daemon.registration(message, len(daemon.registrations))
                else:
                    event = {"message": message, "workspace": self.headers.get("X-Agents-Workspace"),
                        "claimed": self.headers.get("X-Agents-Claimed-Workspace"), "refused": self.headers.get("X-Agents-Workspace-Refused"),
                        "protocol": self.headers.get("MCP-Protocol-Version")}
                    daemon.calls.append(event)
                    if daemon.response:
                        custom = daemon.response(event)
                        if custom is None:
                            self.close_connection = True
                            return
                        status, payload = custom
                    elif "id" not in message:
                        status, payload = 202, None
                    else:
                        result = {"protocolVersion": "2025-11-25", "serverInfo": {"name": "Agents-Core"}, "capabilities": {"tools": {}, "resources": {}}} if message["method"] == "initialize" else {}
                        status, payload = 200, {"jsonrpc": "2.0", "id": message["id"], "result": result}
                raw = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    @staticmethod
    def bind(message, _count):
        path = message["path"]
        if "roots" in message:
            roots = [os.path.normcase(os.path.realpath(url2pathname(urlsplit(uri).path))) for uri in message["roots"]]
            selected = os.path.normcase(os.path.realpath(path))
            if not any(selected == root or selected.startswith(root.rstrip(os.sep) + os.sep) for root in roots):
                return 400, {"error": "workspace_invalid", "message": "outside the roots"}
        return 200, {"workspace_id": "id-" + hashlib.sha256(path.encode()).hexdigest()[:12]}

    def event(self, identity):
        return next(event for event in self.calls if event["message"].get("id") == identity)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class Client:
    def __init__(self, directory, daemon, settings=None, env=None, cwd=None, roots=None, capabilities=None, answer_roots=True):
        self.roots = roots if roots is not None else []
        self.roots_asked = 0
        self.messages = queue.Queue()
        self.saved, self.stderr = {}, []
        config = Path(directory) / "config.json"
        config.write_text(json.dumps({"url": daemon.url, "headers": {"Authorization": "Bearer " + TOKEN}, **(settings or {})}))
        config.chmod(0o600)
        environment = dict(os.environ)
        for key in ("AGENTS_CLIENT_REPO_ROOT", "CLAUDE_PROJECT_DIR", "PYTHONPATH"):
            environment.pop(key, None)
        environment.update({key: value for key, value in (env or {}).items() if value is not None})
        # No PATH or PYTHONPATH lookup is needed by the installed fallback.
        environment["PATH"] = ""
        self.process = subprocess.Popen([sys.executable, str(BRIDGE), str(config)], cwd=cwd or directory,
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1)

        def read_output():
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                    if message.get("method") == "roots/list":
                        self.roots_asked += 1
                        if answer_roots:
                            self.send({"jsonrpc": "2.0", "id": message["id"], "result": {"roots": [{"uri": root} for root in self.roots]}})
                    else:
                        self.messages.put(message)
                except Exception as error:
                    self.messages.put(error)

        self.reader = threading.Thread(target=read_output, daemon=True)
        self.errors = threading.Thread(target=lambda: self.stderr.extend(self.process.stderr), daemon=True)
        self.reader.start()
        self.errors.start()
        self.send({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": capabilities if capabilities is not None else {"roots": {}}}})
        self.wait("init")

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def wait(self, identity):
        while identity not in self.saved:
            message = self.messages.get(timeout=8)
            if isinstance(message, Exception):
                raise message
            self.saved[message["id"]] = message
        return self.saved.pop(identity)

    def request(self, message):
        self.send(message)
        return self.wait(message["id"])

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(2)
                raise
        self.reader.join(2)
        self.errors.join(2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                stream.close()
        if self.process.returncode:
            raise AssertionError("bridge failed: " + "".join(self.stderr))


class PythonStdioBridgeTests(unittest.TestCase):
    @contextmanager
    def session(self, registration=None, response=None, **options):
        daemon = Daemon(registration, response)
        try:
            with tempfile.TemporaryDirectory(prefix="python-bridge-") as directory:
                client = Client(directory, daemon, **options)
                try:
                    yield daemon, client
                finally:
                    client.close()
        finally:
            daemon.close()

    def test_absolute_script_binds_empty_launch_directory_without_import_path(self):
        with tempfile.TemporaryDirectory(prefix="empty-project-") as cwd:
            self.assertEqual(list(Path(cwd).iterdir()), [])
            with self.session(settings={"workspace": "auto"}, cwd=cwd, capabilities={}) as (daemon, client):
                response = client.request(call(1))
                self.assertNotIn("error", response)
                self.assertEqual(len(daemon.registrations), 1)
                self.assertEqual(Path(daemon.registrations[0]["path"]).resolve(), Path(cwd).resolve())
                self.assertIsNotNone(daemon.event(1)["workspace"])
                self.assertEqual(client.roots_asked, 0)
                self.assertEqual(daemon.event("init")["message"]["params"]["capabilities"], {})

    def test_environment_override_decides_alone_and_strips_hook_signature(self):
        with self.session(settings={"workspace": "auto"}, env={"AGENTS_CLIENT_REPO_ROOT": "relative-folder", "CLAUDE_PROJECT_DIR": "/ignored"}, roots=["file:///other"]) as (daemon, client):
            client.request(call(1, "/signed", signature("/signed")))
            registration = daemon.registrations[0]
            self.assertEqual(Path(registration["path"]).name, "relative-folder")
            self.assertTrue(Path(registration["path"]).is_absolute())
            self.assertEqual(registration["origin"], "AGENTS_CLIENT_REPO_ROOT")
            self.assertEqual(len(daemon.registrations), 1)
            self.assertEqual(client.roots_asked, 0)
            self.assertNotIn("workspace_signature", daemon.event(1)["message"]["params"]["arguments"])

    def test_claude_project_directory_names_auto_session(self):
        with self.session(settings={"workspace": "auto"}, env={"CLAUDE_PROJECT_DIR": "/claude/session"}, capabilities={}) as (daemon, client):
            client.request(call(1))
            self.assertEqual(daemon.registrations, [{"path": "/claude/session", "origin": "CLAUDE_PROJECT_DIR"}])
            self.assertIsNotNone(daemon.event(1)["workspace"])

    def test_forwarded_standalone_signature_uses_native_key_scoped_to_installation(self):
        with tempfile.TemporaryDirectory(prefix="native-auth-") as directory:
            config = Path(directory) / "native.json"
            workspace = str(Path(directory).resolve())
            proof = hmac.new(b"native-test-secret", ("workspace:" + workspace).encode(), hashlib.sha256).hexdigest()
            for installation, accepted in ((str(BRIDGE.parent.parent), True), (directory, False)):
                config.write_text(json.dumps({"transport": "stdio", "installation": installation,
                    "headers": {"Authorization": "Bearer native-test-secret"}}))
                config.chmod(0o600)
                with self.session(settings={"workspace": "auto"}, capabilities={},
                        env={"AGENTS_WORKSPACE_AUTH_CONFIG": str(config)}) as (daemon, client):
                    client.request(call(1, workspace, proof))
                    hook = [item for item in daemon.registrations if item.get("origin") == "hook"]
                    self.assertEqual(bool(hook), accepted)
                    self.assertTrue(all(value == "Bearer " + TOKEN for value in daemon.authorization))
                    self.assertNotIn("workspace_signature", daemon.event(1)["message"]["params"]["arguments"])
                    self.assertEqual("workspace" not in daemon.event(1)["message"]["params"]["arguments"], accepted)

    def test_shared_bridge_uses_current_sole_root_without_global_pinning(self):
        with tempfile.TemporaryDirectory(prefix="native-roots-") as directory:
            first_path, second_path = Path(directory) / "first folder", Path(directory) / "second"
            first_path.mkdir(); second_path.mkdir()
            self._check_sole_root_switch(first_path.resolve(), second_path.resolve())

    def _check_sole_root_switch(self, first_path, second_path):
        with self.session(roots=[first_path.as_uri()]) as (daemon, client):
            self.assertEqual(daemon.registrations, [])
            client.request(call(1))
            self.assertEqual(daemon.registrations[-1], {"path": str(first_path), "roots": [first_path.as_uri()]})
            first = daemon.event(1)["workspace"]
            client.roots = [second_path.as_uri()]
            client.request(call(2))
            self.assertNotEqual(daemon.event(2)["workspace"], first)
            self.assertEqual([item["path"] for item in daemon.registrations], [str(first_path), str(second_path)])

    def test_multiple_roots_are_ambiguous_and_explicit_paths_are_checked(self):
        with self.session(roots=["file:///projects/a", "file:///projects/b"]) as (daemon, client):
            response = client.request(call(1))
            self.assertNotIn("error", response)
            self.assertEqual(daemon.registrations, [])
            self.assertIsNone(daemon.event(1)["workspace"])
            client.request(call(2, "/projects/b/sub"))
            self.assertIsNotNone(daemon.event(2)["workspace"])
            refusal = client.request(call(3, "/outside"))
            self.assertEqual(refusal["error"]["code"], -32602)
            self.assertFalse(any(event["message"].get("id") == 3 for event in daemon.calls))

    def test_root_changes_invalidate_verified_registration(self):
        with self.session(roots=["file:///projects/a"]) as (daemon, client):
            client.request(call(1, "/projects/a"))
            client.roots = ["file:///projects/b"]
            client.send({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"})
            refusal = client.request(call(2, "/projects/a"))
            self.assertEqual(refusal["error"]["code"], -32602)
            self.assertEqual(len(daemon.registrations), 2)

    def test_auto_session_is_safe_fallback_after_root_refusal(self):
        with self.session(settings={"workspace": "auto"}, env={"CLAUDE_PROJECT_DIR": "/session"}, roots=["file:///allowed"]) as (daemon, client):
            client.request(call(1, "/outside"))
            self.assertEqual(daemon.event(1)["workspace"], daemon.event("init")["workspace"])
            self.assertIsNotNone(daemon.event(1)["workspace"])

    def test_signed_hook_works_without_roots_and_does_not_pin_shared_bridge(self):
        with self.session(capabilities={}) as (daemon, client):
            client.request(call(1, "/signed", signature("/signed")))
            self.assertEqual(daemon.registrations, [{"path": "/signed", "origin": "hook"}])
            self.assertNotIn("workspace_signature", daemon.event(1)["message"]["params"]["arguments"])
            client.request(call(2))
            self.assertIsNone(daemon.event(2)["workspace"])

    def test_signed_hook_cannot_seed_unsigned_roots_cache(self):
        with self.session(roots=["file:///allowed"]) as (daemon, client):
            client.request(call(1, "/outside", signature("/outside")))
            refusal = client.request(call(2, "/outside"))
            self.assertEqual(refusal["error"]["code"], -32602)
            self.assertEqual(daemon.registrations[-1], {"path": "/outside", "roots": ["file:///allowed"]})

    def test_bad_signature_is_stripped_and_never_registers(self):
        with self.session(capabilities={}) as (daemon, client):
            client.request(call(1, "/forged", "0" * 64))
            self.assertEqual(daemon.registrations, [])
            self.assertIsNone(daemon.event(1)["workspace"])
            self.assertNotIn("workspace_signature", daemon.event(1)["message"]["params"]["arguments"])

    def test_signed_workspace_replaces_refused_cwd_and_tracks_switches(self):
        def register(message, _):
            if message.get("origin") != "hook":
                return 400, {"error": "workspace_unsafe", "message": "unsafe launch directory"}
            return 200, {"workspace_id": message["path"]}

        with self.session(registration=register, settings={"workspace": "auto"}, cwd="/", capabilities={}) as (daemon, client):
            for identity, workspace in ((1, "/first"), (2, "/second")):
                client.request(call(identity, workspace, signature(workspace)))
                client.request(call(str(identity)))
                self.assertEqual(daemon.event(str(identity))["workspace"], workspace)
            self.assertEqual([item["path"] for item in daemon.registrations], [str(Path("/").resolve()), "/first", "/second"])

    def test_concurrent_signed_switches_keep_newer_request(self):
        second_forwarded = threading.Event()

        def register(message, _):
            if message.get("origin") != "hook":
                return 400, {"error": "workspace_unsafe", "message": "unsafe launch directory"}
            if message["path"] == "/first":
                self.assertTrue(second_forwarded.wait(5))
            return 200, {"workspace_id": message["path"]}

        def respond(event):
            message = event["message"]
            if message.get("id") == 2:
                second_forwarded.set()
            return 200, {"jsonrpc": "2.0", "id": message["id"], "result": {}}

        with self.session(registration=register, response=respond, settings={"workspace": "auto"}, cwd="/", capabilities={}) as (daemon, client):
            client.send(call(1, "/first", signature("/first")))
            client.send(call(2, "/second", signature("/second")))
            client.wait(1)
            client.wait(2)
            client.request(call(3))
            self.assertEqual(daemon.event(3)["workspace"], "/second")

    def test_registration_transient_failure_retries_but_rpc_write_never_replays(self):
        def register(message, count):
            return (503, {}) if count == 1 else Daemon.bind(message, count)

        def respond(event):
            message = event["message"]
            return None if message.get("id") == 1 else (200, {"jsonrpc": "2.0", "id": message["id"], "result": {}})

        with self.session(registration=register, response=respond, settings={"workspace": "auto"}, capabilities={}) as (daemon, client):
            failure = client.request(call(1, name="write_file"))
            self.assertEqual(failure["error"]["code"], -32000)
            self.assertIn("no automatic retry", failure["error"]["message"])
            self.assertEqual(sum(event["message"].get("id") == 1 for event in daemon.calls), 1)
            self.assertEqual(len(daemon.registrations), 2)
            self.assertIsNotNone(daemon.event(1)["workspace"])

    def test_history_refusal_carries_only_safe_claim_metadata(self):
        with self.session(roots=["file:///allowed"], env={"CLAUDE_PROJECT_DIR": "/projects/ünicode dir"}) as (daemon, client):
            client.request(call(1, "/outside", name="log_interaction"))
            event = daemon.event(1)
            self.assertIsNone(event["workspace"])
            self.assertEqual(unquote(event["claimed"]), "/outside")
            self.assertEqual(event["refused"], "workspace_invalid")
            client.roots = []
            client.request(call(2, name="read_history"))
            self.assertEqual(unquote(daemon.event(2)["claimed"]), "/projects/ünicode dir")
            client.request(call(3, "/" + "a" * 5000, name="log_interaction"))
            self.assertIsNone(daemon.event(3)["claimed"])

    def test_pinned_workspace_never_sends_claim(self):
        settings = {"headers": {"Authorization": "Bearer " + TOKEN, "X-Agents-Workspace": "pinned-id"}}
        with self.session(settings=settings, roots=["file:///another-project"]) as (daemon, client):
            client.request(call(1, name="read_history"))
            client.request(call(2, "/outside"))
            client.request(call(3, "/signed", signature("/signed")))
            for identity in (1, 2, 3):
                self.assertEqual(daemon.event(identity)["workspace"], "pinned-id")
                self.assertIsNone(daemon.event(identity)["claimed"])
            self.assertEqual(daemon.registrations, [])
            self.assertEqual(client.roots_asked, 0)
            self.assertNotIn("workspace_signature", daemon.event(3)["message"]["params"]["arguments"])

    def test_auto_session_does_not_hide_ambiguous_client_roots(self):
        with self.session(settings={"workspace": "auto"}, roots=["file:///first", "file:///second"]) as (daemon, client):
            self.assertIsNotNone(daemon.event("init")["workspace"])
            response = client.request(call(1))
            self.assertNotIn("error", response)
            self.assertIsNone(daemon.event(1)["workspace"])

    def test_alias_filters_tools_and_preserves_compatibility_view(self):
        def respond(event):
            message = event["message"]
            result = {"tools": [{"name": "native"}, {"name": "plata__run"}, {"name": "other__run"}]} if message["method"] == "tools/list" else {
                "serverInfo": {"name": "Agents-Core", "version": "test"}, "capabilities": {"tools": {}, "resources": {}}, "instructions": "native"}
            return 200, {"jsonrpc": "2.0", "id": message["id"], "result": result}

        with self.session(settings={"alias": "plata"}, response=respond, roots=["file:///irrelevant-project"]) as (daemon, client):
            initialize = client.request({"jsonrpc": "2.0", "id": "init-again", "method": "initialize", "params": {"capabilities": {"roots": {}}}})
            self.assertEqual(initialize["result"]["serverInfo"]["name"], "plata")
            self.assertEqual(initialize["result"]["capabilities"], {"tools": {}})
            tools = client.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(tools["result"]["tools"], [{"name": "run"}])
            client.request(call(2, name="run"))
            self.assertEqual(daemon.event(2)["message"]["params"]["name"], "plata__run")
            refusal = client.request({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
            self.assertEqual(refusal["error"]["code"], -32601)
            self.assertFalse(any(event["message"].get("id") == 3 for event in daemon.calls))
            self.assertEqual(daemon.registrations, [])
            self.assertEqual(client.roots_asked, 0)

    def test_protocol_input_and_uncertain_responses_do_not_pollute_stdout(self):
        def respond(event):
            message = event["message"]
            if "id" not in message:
                return 202, None
            if message["id"] == 2:
                return 200, b"not-json"
            return 200, {"jsonrpc": "2.0", "id": "wrong" if message["id"] == 3 else message["id"], "result": {}}

        with self.session(response=respond, capabilities={}) as (daemon, client):
            client.process.stdin.write("invalid-json\n")
            client.process.stdin.flush()
            self.assertEqual(client.wait(None)["error"]["code"], -32700)
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            for identity in (2, 3):
                self.assertEqual(client.request(call(identity))["error"]["code"], -32000)
                self.assertEqual(sum(event["message"].get("id") == identity for event in daemon.calls), 1)
            self.assertNotIn(TOKEN, "".join(client.stderr))

    def test_deeply_nested_input_does_not_disconnect_the_bridge(self):
        with self.session(capabilities={}) as (daemon, client):
            client.process.stdin.write("[" * 2000 + "0" + "]" * 2000 + "\n")
            client.process.stdin.flush()
            self.assertEqual(client.wait(None)["error"]["code"], -32700)
            self.assertNotIn("error", client.request(call(1, name="route_and_load")))

    def test_persona_routing_survives_ambiguous_roots(self):
        with self.session(roots=["file:///one", "file:///two"]) as (daemon, client):
            response = client.request(call(1, name="route_and_load"))
            self.assertNotIn("error", response)
            self.assertIsNone(daemon.event(1)["workspace"])

    def test_sole_unc_root_uses_windows_path_policy(self):
        spec = importlib.util.spec_from_file_location("tested_stdio_bridge", BRIDGE)
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        with mock.patch.object(bridge, "os", SimpleNamespace(name="nt", path=ntpath)), mock.patch.object(
                bridge, "url2pathname", nturl2path.url2pathname):
            self.assertEqual(bridge.Bridge.sole_root(["file://server/share/project"]), r"\\server\share\project")

    @unittest.skipIf(os.name == "nt", "Windows supports UNC file roots")
    def test_nonlocal_sole_root_fails_without_registration(self):
        with self.session(roots=["file://remote/project"]) as (daemon, client):
            response = client.request(call(1))
            self.assertNotIn("error", response)
            self.assertIsNone(daemon.event(1)["workspace"])
            self.assertEqual(daemon.registrations, [])

    def test_missing_roots_answer_has_bounded_failure(self):
        with self.session(roots=["file:///allowed"], answer_roots=False) as (daemon, client):
            started = time.monotonic()
            refusal = client.request(call(1, "/allowed"))
            self.assertEqual(refusal["error"]["code"], -32602)
            self.assertLess(time.monotonic() - started, 7)
            self.assertEqual(daemon.registrations, [])

    def test_upstream_errors_and_redirects_are_attempted_once(self):
        def respond(event):
            message = event["message"]
            status = {1: 503, 2: 307}.get(message["id"], 200)
            return status, {"jsonrpc": "2.0", "id": message["id"], "result": {}}

        with self.session(response=respond, capabilities={}) as (daemon, client):
            for identity in (1, 2):
                response = client.request(call(identity, name="write_file"))
                self.assertEqual(response["error"]["code"], -32000)
                self.assertEqual(sum(event["message"].get("id") == identity for event in daemon.calls), 1)

    def test_http_deadline_closes_slow_response(self):
        def respond(event):
            time.sleep(0.5)
            return 200, {"jsonrpc": "2.0", "id": event["message"]["id"], "result": {}}

        daemon = Daemon(response=respond)
        try:
            script = ("import runpy,sys; m=runpy.run_path(sys.argv[1]); "
                "c={'url':sys.argv[2],'headers':{'Authorization':sys.argv[3]}}; "
                "b=m['Bridge'](c,m['urlsplit'](c['url']));\n"
                "try: b.http('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/list'},c['headers'],0.1)\n"
                "except Exception: sys.exit(0)\n"
                "sys.exit(1)\n")
            result = subprocess.run([sys.executable, "-c", script, str(BRIDGE), daemon.url, "Bearer " + TOKEN], capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(daemon.calls), 1)
        finally:
            daemon.close()

    def test_invalid_private_config_and_endpoints_fail_before_http(self):
        daemon = Daemon()
        try:
            with tempfile.TemporaryDirectory(prefix="python-bridge-bad-") as directory:
                config_path = Path(directory) / "config.json"
                valid = {"url": daemon.url, "headers": {"Authorization": "Bearer " + TOKEN}}
                invalid = [[], {}, {**valid, "alias": "invalid__alias"}, {**valid, "headers": {"Authorization": "Bearer token\r\nBad: value"}},
                    *({**valid, "url": url} for url in (
                        "https://127.0.0.1:8765/mcp", "http://localhost:8765/mcp", "http://127.0.0.2:8765/mcp", "http://example.com/mcp",
                        "http://user:password@127.0.0.1:8765/mcp", "http://127.0.0.1:8765/other", "http://127.0.0.1:8765/mcp?redirect=1",
                        "http://127.0.0.1:8765/mcp#fragment", "http://127.0.0.1:bad/mcp", "http://127.0.0.1:0/mcp"))]
                for config in invalid:
                    with self.subTest(config=config):
                        config_path.write_text(json.dumps(config))
                        config_path.chmod(0o600)
                        result = subprocess.run([sys.executable, str(BRIDGE), str(config_path)], input="", text=True, capture_output=True, timeout=5)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, "")
                        self.assertNotIn(TOKEN, result.stderr)
                if os.name != "nt":
                    config_path.write_text(json.dumps(valid))
                    config_path.chmod(0o644)
                    result = subprocess.run([sys.executable, str(BRIDGE), str(config_path)], input="", text=True, capture_output=True, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    fifo = Path(directory) / "config.fifo"
                    os.mkfifo(fifo, 0o600)
                    result = subprocess.run([sys.executable, str(BRIDGE), str(fifo)], input="", text=True, capture_output=True, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                self.assertEqual(daemon.authorization, [])
        finally:
            daemon.close()


if __name__ == "__main__":
    unittest.main()
