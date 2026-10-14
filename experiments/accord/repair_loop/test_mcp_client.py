"""Offline transport and retention checks; these tests never contact a daemon."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import mcp_client as mcp


HEADERS = {"Authorization": "Bearer test-only-not-a-credential"}
ENDPOINT = "http://127.0.0.1:8765/mcp"
PACKAGE = {"status": "prepared", "task_id": "task", "attempt_id": "1",
           "contract": {"task_id": "task", "attempt_id": "1", "acceptance_criteria": ["Original criterion"]},
           "worker_context": "Exact original context"}


class Response:
    def __init__(self, result=None, *, response_id=1, status=200, content_type="application/json", raw=None):
        self.status = status
        self.content_type = content_type
        self.stream = io.BytesIO(raw if raw is not None else json.dumps(
            {"jsonrpc": "2.0", "id": response_id, "result": result}).encode())

    def getheader(self, _name, default=""):
        return self.content_type or default

    def read(self, amount):
        return self.stream.read(amount)


class Connection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, method, path, body, headers):
        self.requests.append((method, path, json.loads(body), headers))

    def getresponse(self):
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


class MCPClientTests(unittest.TestCase):
    def client(self, *, initialized=True):
        client = mcp.AgentsCoreClient(endpoint=ENDPOINT, headers=HEADERS)
        client._initialized = initialized
        return client

    def transport(self, responses):
        connections = [Connection(response) for response in responses]
        return patch.object(mcp.http.client, "HTTPConnection", side_effect=connections), connections

    def test_handshake_and_decoded_structured_result_are_timed(self):
        mocked, connections = self.transport([
            Response({"protocolVersion": "2025-11-25"}), Response(status=202),
            Response({"structuredContent": PACKAGE}, response_id=3),
            Response({"structuredContent": {"valid": True}}, response_id=4),
        ])
        with mocked as factory:
            client = self.client(initialized=False)
            prepared = client.prepare(task="A task")
            checked = client.validate(prepared, {"status": "completed"})
        self.assertEqual(prepared.package, PACKAGE)
        self.assertGreaterEqual(prepared.elapsed_ms, 0)
        self.assertGreaterEqual(checked.elapsed_ms, 0)
        self.assertEqual(checked.data, {"valid": True})
        methods = [connection.requests[0][2]["method"] for connection in connections]
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call", "tools/call"])
        self.assertEqual(factory.call_count, 4)
        self.assertTrue(all(connection.closed for connection in connections))
        for call in factory.call_args_list:
            self.assertEqual(call.args, ("127.0.0.1", 8765))
            self.assertEqual(call.kwargs["timeout"], 30.0)

    def test_explicit_initialize_is_separately_timed_and_idempotent(self):
        mocked, connections = self.transport([
            Response({"protocolVersion": "2025-11-25"}), Response(status=202),
            Response({"structuredContent": PACKAGE}, response_id=3),
        ])
        with mocked as factory, patch.object(mcp.time, "perf_counter", side_effect=[1, 2, 3, 3, 4, 5]):
            client = self.client(initialized=False)
            self.assertEqual(client.initialize(), 1000)
            self.assertEqual(client.initialize(), 0)
            prepared = client.prepare(task="A task")
        self.assertEqual(prepared.elapsed_ms, 1000)
        self.assertEqual(factory.call_count, 3)
        self.assertEqual(connections[2].requests[0][2]["method"], "tools/call")

    def test_original_contract_is_retained_despite_caller_mutation(self):
        mocked, connections = self.transport([
            Response({"structuredContent": PACKAGE}),
            Response({"structuredContent": {"valid": False}}, response_id=2),
        ])
        with mocked:
            client = self.client()
            prepared = client.prepare(task="A task")
            changed = prepared.package
            changed["contract"]["acceptance_criteria"][0] = "Replaced criterion"
            changed_contract = prepared.contract
            changed_contract["task_id"] = "replacement"
            raw_result = '{"unexpected":"raw worker text is forwarded unchanged"}'
            client.validate(prepared, raw_result)
        sent = connections[1].requests[0][2]["params"]["arguments"]
        self.assertEqual(sent["contract"], PACKAGE["contract"])
        self.assertEqual(sent["result"], raw_result)
        self.assertEqual(prepared.worker_context, PACKAGE["worker_context"])

    def test_call_returns_tuple_and_text_json_fallback(self):
        mocked, _ = self.transport([Response({"content": [{"type": "text", "text": '{"valid":false}'}]})])
        with mocked:
            data, elapsed_ms = self.client().call("validate_subagent_result", {"contract": {}, "result": {}})
        self.assertEqual(data, {"valid": False})
        self.assertGreaterEqual(elapsed_ms, 0)
        self.assertIs(mcp.MCPClient, mcp.AgentsCoreClient)

    def test_loopback_only_with_no_url_credentials_queries_or_redirect_targets(self):
        for endpoint in ("https://127.0.0.1:8765/mcp", "http://localhost:8765/mcp", "http://example.com/mcp",
                         "http://127.0.0.1/other", "http://user:password@127.0.0.1/mcp",
                         "http://127.0.0.1/mcp?token=secret", "http://127.0.0.1/mcp#fragment",
                         "http://127.0.0.1:invalid/mcp", None):
            with self.subTest(endpoint=endpoint), self.assertRaises(mcp.MCPClientError):
                mcp.AgentsCoreClient(endpoint=endpoint, headers=HEADERS)

    def test_timeout_is_finite_and_bounded(self):
        for timeout in (0, -1, 121, float("nan"), float("inf"), "10", True):
            with self.subTest(timeout=timeout), self.assertRaises(mcp.MCPClientError):
                mcp.AgentsCoreClient(endpoint=ENDPOINT, headers=HEADERS, timeout=timeout)

    def test_only_the_two_read_only_tools_are_exposed(self):
        with patch.object(mcp.http.client, "HTTPConnection") as factory:
            with self.assertRaises(mcp.MCPClientError):
                self.client().call("log_interaction", {})
            factory.assert_not_called()

    def test_redirect_and_http_errors_are_sanitized_and_not_retried(self):
        for status in (301, 302, 307, 308, 401, 500):
            with self.subTest(status=status):
                mocked, connections = self.transport([Response(status=status, raw=b"secret /private/config")])
                with mocked as factory, self.assertRaises(mcp.MCPClientError) as caught:
                    self.client().call("get_subagent_context", {})
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("/private/config", str(caught.exception))
                self.assertEqual(factory.call_count, 1)
                self.assertTrue(connections[0].closed)

    def test_network_failure_does_not_expose_exception_text_or_retry(self):
        mocked, connections = self.transport([OSError("secret /private/config")])
        with mocked as factory, self.assertRaises(mcp.MCPClientError) as caught:
            self.client().call("get_subagent_context", {})
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(connections[0].closed)

    def test_malformed_and_mismatched_responses_fail(self):
        for response in (Response(raw=b"invalid-json"), Response({}, response_id=100),
                         Response({}, content_type="text/event-stream"), Response(status=204),
                         Response(raw=b'{"jsonrpc":"2.0","id":1,"error":{"message":"secret"}}')):
            with self.subTest(response=response):
                mocked, _ = self.transport([response])
                with mocked, self.assertRaises(mcp.MCPClientError) as caught:
                    self.client().call("get_subagent_context", {})
                self.assertNotIn("secret", str(caught.exception))

    def test_response_size_is_bounded(self):
        mocked, _ = self.transport([Response({"structuredContent": PACKAGE})])
        with mocked, patch.object(mcp, "_MAX_RESPONSE_BYTES", 8), self.assertRaises(mcp.MCPClientError):
            self.client().call("get_subagent_context", {})

    def test_tool_errors_and_non_json_text_do_not_escape_into_errors(self):
        for result in ({"isError": True, "content": [{"type": "text", "text": "secret"}]},
                       {"content": [{"type": "text", "text": "secret"}]},
                       {"content": [{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}]}):
            mocked, _ = self.transport([Response(result)])
            with mocked, self.assertRaises(mcp.MCPClientError) as caught:
                self.client().call("get_subagent_context", {})
            self.assertNotIn("secret", str(caught.exception))

    def test_preparation_failure_is_not_treated_as_prepared(self):
        mocked, _ = self.transport([Response({"structuredContent": {"status": "error", "errors": []}})])
        with mocked, self.assertRaises(mcp.MCPClientError):
            self.client().prepare(task="A task")

    def test_direct_codex_configuration_and_default_codex_home(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[mcp_servers.Agents-Core]\nurl = "' + ENDPOINT + '"\n'
                            '[mcp_servers.Agents-Core.http_headers]\nAuthorization = "Bearer synthetic"\n')
            with patch.dict(mcp.os.environ, {"CODEX_HOME": directory}):
                client = mcp.AgentsCoreClient(workspace="/not-used-for-pure-tools")
            self.assertEqual(client._headers, {"Authorization": "Bearer synthetic"})
            self.assertNotIn(directory, repr(client))
            self.assertFalse(any(isinstance(value, str) and directory in value for value in vars(client).values()))

    def test_private_bridge_and_known_header_helper_are_read_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "bridge.json"
            bridge.write_text(json.dumps({"url": ENDPOINT, "headers": HEADERS}))
            bridge.chmod(0o600)
            config = Path(directory) / "config.toml"
            configs = [
                '[mcp_servers.Agents-Core]\ncommand = "node"\nargs = ["/installed/bridge/stdio.mjs", ' + json.dumps(str(bridge)) + ']\n',
                '[mcp_servers.Agents-Core]\nurl = "' + ENDPOINT + '"\nhttp_headers_helper = ' + json.dumps('node /installed/bridge/headers.mjs ' + mcp.shlex.quote(str(bridge))) + '\n',
            ]
            for text in configs:
                config.write_text(text)
                self.assertEqual(mcp.AgentsCoreClient(config_path=config)._headers, HEADERS)
            self.assertEqual(mcp.AgentsCoreClient(config_path=bridge)._headers, HEADERS)
            bridge.chmod(0o644)
            if mcp.os.name != "nt":
                with self.assertRaises(mcp.MCPClientError):
                    mcp.AgentsCoreClient(config_path=bridge)

    def test_unsupported_helper_and_config_errors_hide_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[mcp_servers.Agents-Core]\nurl = "' + ENDPOINT + '"\n'
                              'http_headers_helper = "arbitrary command /private/config"\n')
            with self.assertRaises(mcp.MCPClientError) as caught:
                mcp.AgentsCoreClient(config_path=config)
            self.assertNotIn(directory, str(caught.exception))
            self.assertNotIn("/private/config", str(caught.exception))
            with self.assertRaises(mcp.MCPClientError) as caught:
                mcp.AgentsCoreClient(config_path=Path(directory) / "missing.toml")
            self.assertNotIn(directory, str(caught.exception))

    def test_missing_and_injected_credentials_are_rejected(self):
        for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer one\r\nX-Evil: yes"}):
            with self.subTest(headers=headers), self.assertRaises(mcp.MCPClientError):
                mcp.AgentsCoreClient(endpoint=ENDPOINT, headers=headers)


if __name__ == "__main__":
    unittest.main()
