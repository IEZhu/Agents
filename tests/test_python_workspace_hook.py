"""Actual-subprocess coverage for the stdlib Antigravity workspace hook."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HOOK = Path(__file__).resolve().parents[1] / "bridge" / "antigravity_hook.py"
TOKEN = "hook-test-secret"


class PythonWorkspaceHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="python-workspace-hook-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "empty workspace"
        self.workspace.mkdir()
        self.config = self.root / "private.json"
        self.write_config()

    def write_config(self, **overrides):
        config = {"url": "http://127.0.0.1:8765/mcp", "headers": {"Authorization": "bEaReR " + TOKEN}}
        config.update(overrides)
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.config.chmod(0o600)

    def run_hook(self, payload, *, config=True, environment=None):
        env = dict(os.environ)
        for name in ("AGENTS_BRIDGE_CONFIG", "PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)
        env.update({"HOME": str(self.root), "USERPROFILE": str(self.root), "PATH": "", "PYTHONDONTWRITEBYTECODE": "1"})
        env.update(environment or {})
        command = [sys.executable, str(HOOK)]
        if config:
            command.append(str(self.config))
        raw = payload if isinstance(payload, bytes) else (payload if isinstance(payload, str) else json.dumps(payload)).encode("utf-8")
        completed = subprocess.run(command, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.workspace, env=env, timeout=5, check=True)
        self.assertNotIn(TOKEN.encode(), completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        return json.loads(completed.stdout)

    def payload(self, name="mcp_Agents_Core_read_history", args=None, workspace=None):
        return {"toolCall": {"name": name, "args": {} if args is None else args},
            "workspacePaths": [str(self.workspace) if workspace is None else workspace]}

    def expected_args(self, **args):
        workspace = str(self.workspace)
        return {**args, "workspace": workspace, "workspace_signature": hmac.new(
            TOKEN.encode(), ("workspace:" + workspace).encode(), hashlib.sha256).hexdigest()}

    def test_absolute_command_signs_empty_workspace_for_lazy_object_arguments(self):
        self.assertTrue(Path(sys.executable).is_absolute())
        self.assertTrue(HOOK.is_absolute())
        self.assertTrue(self.config.is_absolute())
        self.assertEqual(list(self.workspace.iterdir()), [])
        payload = self.payload("call_mcp_tool", {"ServerName": "Agents-Core", "ToolName": "read_history",
            "Arguments": {"limit": 5, "workspace": "/stale", "workspace_signature": "stale", "extra": {"keep": True}}})
        self.assertEqual(self.run_hook(payload), {"decision": "ask", "overwrite": {
            "Arguments": self.expected_args(limit=5, extra={"keep": True})}})
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_lazy_string_arguments_remain_a_string_and_underscore_server_is_supported(self):
        payload = self.payload("call_mcp_tool", {"ServerName": "Agents_Core", "ToolName": "read_history",
            "Arguments": json.dumps({"limit": 3, "text": "caf\u00e9"})})
        response = self.run_hook(payload)
        self.assertEqual(response["decision"], "ask")
        self.assertEqual(set(response["overwrite"]), {"Arguments"})
        self.assertIsInstance(response["overwrite"]["Arguments"], str)
        self.assertEqual(json.loads(response["overwrite"]["Arguments"]), self.expected_args(limit=3, text="caf\u00e9"))

    def test_standalone_private_config_needs_no_http_endpoint(self):
        self.config.write_text(json.dumps({"transport": "stdio", "installation": str(HOOK.parent.parent),
            "headers": {"Authorization": "Bearer " + TOKEN}}), encoding="utf-8")
        self.config.chmod(0o600)
        self.assertEqual(self.run_hook(self.payload()), {"decision": "ask", "overwrite": self.expected_args()})

    def test_eager_names_preserve_argument_fields_and_trim_workspace(self):
        for name in ("mcp_Agents-Core_read_history", "mcp_Agents_Core_read_history"):
            with self.subTest(name=name):
                payload = self.payload(name, {"limit": 10, "nested": {"keep": [1, 2]}}, "  " + str(self.workspace) + "  ")
                self.assertEqual(self.run_hook(payload), {"decision": "ask", "overwrite": self.expected_args(
                    limit=10, nested={"keep": [1, 2]})})

    def test_missing_arguments_default_to_an_empty_object(self):
        lazy = self.payload("call_mcp_tool", {"ServerName": "Agents-Core", "ToolName": "read_history"})
        self.assertEqual(self.run_hook(lazy), {"decision": "ask", "overwrite": {"Arguments": self.expected_args()}})
        eager = self.payload()
        eager["toolCall"].pop("args")
        self.assertEqual(self.run_hook(eager), {"decision": "ask", "overwrite": self.expected_args()})

    def test_explicit_environment_and_native_config_discovery(self):
        missing = str(self.root / "missing.json")
        # An explicit private config wins over a stale environment value.
        self.assertIn("overwrite", self.run_hook(self.payload(), environment={"AGENTS_BRIDGE_CONFIG": missing}))
        self.assertIn("overwrite", self.run_hook(self.payload(), config=False,
            environment={"AGENTS_BRIDGE_CONFIG": str(self.config)}))
        native = self.root / ".gemini" / "config" / "mcp_config.json"
        native.parent.mkdir(parents=True)
        native.write_text(json.dumps({"mcpServers": {"Agents-Core": {
            "command": sys.executable, "args": [str(HOOK.with_name("stdio.py")), str(self.config)]}}}), encoding="utf-8")
        self.assertIn("overwrite", self.run_hook(self.payload(), config=False))
        # An explicitly selected missing config never falls back to another token.
        self.assertEqual(self.run_hook(self.payload(), config=False, environment={"AGENTS_BRIDGE_CONFIG": missing}), {"decision": "ask"})

    def test_unrelated_malformed_and_missing_inputs_never_overwrite(self):
        cases = [b"", b"{invalid json", b"\xff", [], None, {}, {"toolCall": []},
            self.payload("other_tool"), self.payload("call_mcp_tool", {"ServerName": "Other", "Arguments": {}}),
            self.payload("mcp_Other_read_history"), self.payload(args=[]),
            self.payload("call_mcp_tool", {"ServerName": "Agents-Core", "Arguments": "{bad json"}),
            self.payload("call_mcp_tool", {"ServerName": "Agents-Core", "Arguments": "[]"}),
            self.payload("call_mcp_tool", {"ServerName": "Agents-Core", "Arguments": 7}),
            self.payload(workspace="  "), self.payload(workspace={"path": str(self.workspace)}),
            self.payload(workspace="\x00")]
        missing_workspace = self.payload()
        missing_workspace.pop("workspacePaths")
        cases.extend([missing_workspace, {**self.payload(), "workspacePaths": []},
            {**self.payload(), "workspacePaths": str(self.workspace)}])
        for payload in cases:
            with self.subTest(payload=repr(payload)[:100]):
                self.assertEqual(self.run_hook(payload), {"decision": "ask"})

    def test_missing_malformed_and_insecure_config_never_overwrite(self):
        configs = ["not json", "[]", "{}", json.dumps({"url": "http://127.0.0.1:8765/mcp", "headers": {}})]
        for raw in configs:
            with self.subTest(raw=raw):
                self.config.write_text(raw, encoding="utf-8")
                self.assertEqual(self.run_hook(self.payload()), {"decision": "ask"})
        for url in ("https://127.0.0.1:8765/mcp", "http://example.invalid/mcp", "http://127.0.0.1:8765/not-mcp"):
            with self.subTest(url=url):
                self.write_config(url=url)
                self.assertEqual(self.run_hook(self.payload()), {"decision": "ask"})
        self.write_config()
        if os.name != "nt":
            self.config.chmod(0o644)
            self.assertEqual(self.run_hook(self.payload()), {"decision": "ask"})
        self.config.unlink()
        self.assertEqual(self.run_hook(self.payload()), {"decision": "ask"})
        self.config.mkdir()
        self.assertEqual(self.run_hook(self.payload()), {"decision": "ask"})

    def test_oversized_input_is_bounded_and_never_overwrites(self):
        self.assertEqual(self.run_hook(b" " * (1024 * 1024 + 1)), {"decision": "ask"})


if __name__ == "__main__":
    unittest.main()
