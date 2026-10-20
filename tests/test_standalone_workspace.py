"""Light native-workspace checks; all state lives in temporary directories."""
import asyncio
import base64
from contextlib import redirect_stderr
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts._helpers import install_antigravity_workspace as installer
from src import stdio_workspace
from src.daemon import clients
from src.engine import config as engine_config


ROOT = Path(__file__).resolve().parents[1]


class StandaloneWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agents-native-workspace-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.installation = self.root / "installation"
        self.installation.mkdir()
        self.workspace = self.root / "empty project"
        self.workspace.mkdir()
        self.auth = self.root / "authentication.json"
        self.token = "test-private-native-workspace-token"
        self.document = {"transport": "stdio", "installation": str(self.installation),
                         "headers": {"Authorization": "Bearer " + self.token}}
        self.write_auth()
        self.enterContext(patch.dict(os.environ, {stdio_workspace.AUTH_CONFIG_ENV: str(self.auth)}))
        self.enterContext(patch.object(stdio_workspace.runtime_paths, "installation_root", return_value=self.installation))

    def write_auth(self, document=None):
        self.auth.write_text(json.dumps(self.document if document is None else document), encoding="utf-8")
        self.auth.chmod(0o600)
        if os.name == "nt":
            from src.daemon.acl import restrict
            restrict(self.auth)

    def arguments(self, workspace=None, **extra):
        workspace = str(workspace if workspace is not None else self.workspace)
        return {"workspace": workspace, "workspace_signature": hmac.new(
            self.token.encode(), ("workspace:" + workspace).encode(), hashlib.sha256).hexdigest(), **extra}

    def assert_rejected(self, arguments):
        with stdio_workspace.bind(arguments) as sanitized:
            self.assertNotIn("workspace_signature", sanitized)
            self.assertEqual(sanitized.get("workspace"), arguments.get("workspace"))
            binding = stdio_workspace.request_workspace.get()
            self.assertIsNotNone(binding)
            self.assertIsNone(binding.root)
            self.assertIn("workspace_invalid", binding.error)
        self.assertIsNone(stdio_workspace.request_workspace.get())

    def test_signed_empty_directory_works_without_roots_from_filesystem_cwd(self):
        original = self.arguments(limit=4)
        with patch.object(engine_config.os, "getcwd", return_value="/"):
            with stdio_workspace.bind(original) as sanitized:
                self.assertEqual(sanitized, {"limit": 4})
                self.assertEqual(stdio_workspace.request_workspace.get().root, self.workspace)
                self.assertIsNone(stdio_workspace.request_workspace.get().error)
        self.assertIn("workspace_signature", original)
        self.assertIsNone(stdio_workspace.request_workspace.get())
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_unsigned_and_disabled_hints_never_bind(self):
        unsigned = {"workspace": str(self.workspace), "value": 1}
        with stdio_workspace.bind(unsigned) as sanitized:
            self.assertEqual(sanitized, unsigned)
            self.assertIsNone(stdio_workspace.request_workspace.get())
        with patch.object(stdio_workspace, "load_auth_config", side_effect=AssertionError("auth disabled")):
            with stdio_workspace.bind(self.arguments(), enabled=False) as sanitized:
                self.assertEqual(sanitized, {"workspace": str(self.workspace)})
                self.assertIsNone(stdio_workspace.request_workspace.get())

    def test_invalid_proofs_wrong_installation_and_bad_configs_never_bind(self):
        for signature in ("0" * 64, "invalid", {}, 12):
            with self.subTest(signature=signature):
                self.assert_rejected({**self.arguments(), "workspace_signature": signature})
        for document in ([], {}, {**self.document, "transport": "http"},
                         {**self.document, "installation": str(self.workspace)},
                         {**self.document, "headers": {"Authorization": "Bearer "}}):
            with self.subTest(document=document):
                self.write_auth(document)
                self.assert_rejected(self.arguments())
        self.auth.write_bytes(b" " * (stdio_workspace.MAX_CONFIG_BYTES + 1))
        self.assert_rejected(self.arguments())
        self.auth.unlink()
        self.assert_rejected(self.arguments())

    def test_private_regular_owner_file_is_required(self):
        if os.name != "nt":
            self.auth.chmod(0o644)
            self.assert_rejected(self.arguments())
            self.auth.chmod(0o600)
            with patch.object(stdio_workspace.os, "getuid", return_value=os.getuid() + 1):
                self.assert_rejected(self.arguments())
        real = self.root / "real.json"
        self.auth.rename(real)
        try:
            self.auth.symlink_to(real)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable")
            raise
        self.assert_rejected(self.arguments())
        self.auth.unlink()
        self.auth.mkdir()
        self.assert_rejected(self.arguments())
        if hasattr(os, "mkfifo"):
            self.auth.rmdir()
            os.mkfifo(self.auth, 0o600)
            self.assert_rejected(self.arguments())

    def test_authenticated_unsafe_or_missing_directory_is_consumed_but_refused(self):
        for workspace, code in ((Path(self.workspace.anchor), "workspace_unsafe"),
                                (self.root / "missing", "workspace_invalid")):
            with self.subTest(workspace=workspace):
                with stdio_workspace.bind(self.arguments(workspace)) as sanitized:
                    self.assertEqual(sanitized, {})
                    binding = stdio_workspace.request_workspace.get()
                    self.assertIsNone(binding.root)
                    self.assertIn(code, binding.error)

    def test_context_isolated_between_concurrent_requests_and_resets_on_exception(self):
        other = self.root / "other empty project"
        other.mkdir()

        async def invoke(workspace):
            with stdio_workspace.bind(self.arguments(workspace)):
                await asyncio.sleep(0)
                self.assertEqual(stdio_workspace.request_workspace.get().root, workspace)
            self.assertIsNone(stdio_workspace.request_workspace.get())

        async def concurrent():
            await asyncio.gather(invoke(self.workspace), invoke(other))

        asyncio.run(concurrent())
        with self.assertRaisesRegex(RuntimeError, "handler failed"):
            with stdio_workspace.bind(self.arguments()):
                raise RuntimeError("handler failed")
        self.assertIsNone(stdio_workspace.request_workspace.get())


class StandaloneInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agents-native-installer-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.profile = self.home / ".gemini/config"
        self.profile.mkdir(parents=True)
        self.target = self.profile / "mcp_config.json"
        self.installation = self.root / "installation with spaces"
        self.server = self.installation / "src/server.py"
        self.server.parent.mkdir(parents=True)
        self.server.write_text("# Never executed\n")
        template = self.installation / "scripts/templates/routing-protocol-core.md"
        template.parent.mkdir(parents=True)
        template.write_bytes((ROOT / "scripts/templates/routing-protocol-core.md").read_bytes())
        self.python = Path(sys.executable).absolute()
        self.directory = self.installation / "data/native-workspace"
        self.signing_file = self.directory / "antigravity.json"
        self.plugin = self.profile / "plugins/agents-core"
        self.enterContext(patch.object(installer.shutil, "which", return_value=None))

    def configure(self, target=None):
        return installer.configure(self.python, self.server, target or self.target, home=self.home)

    def test_journaled_key_plugin_and_registration_are_private_stable_and_idempotent(self):
        original = {"custom": 9, "mcpServers": {"Other": {"command": "keep"}, "Agents-Core": {
            "command": str(self.python), "args": [str(self.server)], "timeout": 70, "env": {"CUSTOM": "keep"}}}}
        self.target.write_text(json.dumps(original))
        self.plugin.mkdir(parents=True)
        (self.plugin / "hooks.json").write_text(json.dumps({"my-hook": {"enabled": True, "value": "keep"}}))
        self.assertIn("configured", self.configure())
        signing = stdio_workspace.load_auth_config(self.signing_file, installation=self.installation)
        secret = signing["headers"]["Authorization"][len("Bearer "):]
        document = json.loads(self.target.read_text())
        entry = document["mcpServers"]["Agents-Core"]
        self.assertEqual(document["custom"], 9)
        self.assertEqual(document["mcpServers"]["Other"], original["mcpServers"]["Other"])
        self.assertEqual(entry["timeout"], 70)
        self.assertEqual(entry["env"], {"CUSTOM": "keep", stdio_workspace.AUTH_CONFIG_ENV: str(self.signing_file)})
        hooks = json.loads((self.plugin / "hooks.json").read_text())
        self.assertEqual(hooks["my-hook"], {"enabled": True, "value": "keep"})
        self.assertIn("antigravity_hook.py", hooks["agents-core-workspace"]["PreToolUse"][0]["hooks"][0]["command"])
        self.assertEqual(json.loads((self.profile / "config.json").read_text())["plugins"]["agents-core"]["enabled"], True)
        self.assertNotIn(secret, self.target.read_text())
        self.assertNotIn(secret, (self.plugin / "hooks.json").read_text())
        self.assertFalse((self.directory / "service.json").exists())
        if os.name != "nt":
            self.assertEqual(self.signing_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        journal = json.loads((self.directory / "migration.json").read_text())
        self.assertEqual(journal["state"], "applied")
        records = json.loads((Path(journal["backup"]) / "changes.json").read_text())
        mcp_record = next(record for record in records if record["path"] == str(self.target))
        self.assertEqual(json.loads(base64.b64decode(mcp_record["before"])), original)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in (self.signing_file, self.target, self.plugin / "hooks.json", self.directory / "migration.json")}
        self.assertIn("already current", self.configure())
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})

    def test_custom_service_external_and_symlink_configurations_are_preserved(self):
        for entry in ({"url": "http://127.0.0.1:8765/mcp"}, {"command": "node", "args": ["bridge/stdio.mjs"]},
                      {"command": "personal", "args": ["custom.py"]}):
            with self.subTest(entry=entry):
                content = json.dumps({"mcpServers": {"Agents-Core": entry}})
                self.target.write_text(content)
                self.assertIn("skipped", self.configure())
                self.assertEqual(self.target.read_text(), content)
                self.assertFalse(self.signing_file.exists())
        self.target.unlink()
        external = self.home / "external.json"
        self.assertIn("skipped", self.configure(external))
        self.assertFalse(external.exists())
        marker = self.installation / "data/.shared-service.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}")
        self.assertIn("skipped", self.configure())
        self.assertFalse(self.target.exists())
        marker.unlink()
        external.write_text("{}")
        try:
            self.target.symlink_to(external)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable")
            raise
        self.assertIn("skipped", self.configure())
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(external.read_text(), "{}")

    def test_failed_apply_rolls_back_existing_config_and_plugin_assets(self):
        original = {"mcpServers": {"Other": {"command": "keep"}}}
        self.target.write_text(json.dumps(original))
        real_atomic = clients.atomic_private

        def fail_target(path, content):
            if Path(path) == self.target:
                raise PermissionError("simulated write failure")
            return real_atomic(path, content)

        with patch.object(clients, "atomic_private", side_effect=fail_target):
            with self.assertRaises(PermissionError):
                self.configure()
        self.assertEqual(json.loads(self.target.read_text()), original)
        self.assertFalse(self.signing_file.exists())
        self.assertFalse((self.plugin / "hooks.json").exists())

    def test_fresh_and_combined_native_profiles_preserve_settings(self):
        self.target = self.profile / "config.json"
        self.target.write_text(json.dumps({"plugins": {"personal": {"enabled": True}}, "theme": "keep"}))
        self.assertIn("configured", self.configure())
        document = json.loads(self.target.read_text())
        self.assertEqual(document["theme"], "keep")
        self.assertEqual(document["plugins"]["personal"], {"enabled": True})
        self.assertEqual(document["mcpServers"]["Agents-Core"]["args"], [str(self.server)])
        self.assertIn("already current", self.configure())

    def test_invalid_existing_key_is_not_rotated_or_exposed(self):
        self.configure()
        original_config = self.target.read_bytes()
        secret = "private-key-must-not-reach-stderr"
        self.signing_file.write_text(json.dumps({"transport": "stdio", "installation": str(self.home),
                                               "headers": {"Authorization": "Bearer " + secret}}))
        signing_bytes = self.signing_file.read_bytes()
        stream = io.StringIO()
        with patch.object(Path, "home", return_value=self.home), redirect_stderr(stream):
            self.assertEqual(installer.main([str(self.python), str(self.server), str(self.target)]), 1)
        self.assertEqual(self.signing_file.read_bytes(), signing_bytes)
        self.assertEqual(self.target.read_bytes(), original_config)
        self.assertNotIn(secret, stream.getvalue())

    def test_diagnostics_do_not_expose_private_configuration(self):
        self.target.write_text('{"secret-value-from-malformed-input":')
        stream = io.StringIO()
        with patch.object(Path, "home", return_value=self.home), redirect_stderr(stream):
            self.assertEqual(installer.main([str(self.python), str(self.server), str(self.target)]), 1)
        self.assertNotIn("secret-value", stream.getvalue())
        self.assertFalse(self.signing_file.exists())


if __name__ == "__main__":
    unittest.main()
