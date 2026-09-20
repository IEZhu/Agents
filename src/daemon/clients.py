"""Atomic client configuration migration with private, restorable backups."""
from pathlib import Path
import base64
import hashlib
import json
import os
import re
import shutil
import shlex
import subprocess
import time
import tomllib

from .state import atomic_private, read_json, write_json, private_dir
from .workspaces import WorkspaceRegistry


SERVER = "Agents-Core"
DESKTOP_SERVER = "Agents-Core-Desktop"
TRANSPORT_KEYS = {"type", "command", "args", "env", "env_vars", "cwd", "url", "headers", "http_headers",
                  "env_http_headers", "bearer_token", "bearer_token_env_var", "http_headers_helper"}


def transport_entry(previous, replacement):
    return {**{key: value for key, value in previous.items() if key not in TRANSPORT_KEYS}, **replacement}


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(k) + " = " + toml_value(v) for k, v in value.items()) + " }"
    if isinstance(value, list): return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(value)


def tracked(path):
    path = Path(path)
    cwd = path.parent
    while not cwd.exists():
        cwd = cwd.parent
    result = subprocess.run(["git", "-C", str(cwd), "ls-files", "--error-unmatch", str(path)],
                            capture_output=True, text=True)
    return result.returncode == 0


def exclude_private(path):
    """Local git exclusion only; never commit a token-bearing configuration."""
    path = Path(path)
    result = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True)
    if result.returncode:
        return
    root = Path(result.stdout.strip())
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
                            capture_output=True, text=True, check=True)
    exclude = Path(result.stdout.strip())
    if not exclude.is_absolute(): exclude = root / exclude
    pattern = "/" + path.relative_to(root).as_posix()
    original = exclude.read_text() if exclude.exists() else ""
    if pattern not in original.splitlines():
        atomic_private(exclude, original.rstrip("\n") + "\n" + pattern + "\n")


def replace_toml_server(original, name, entry):
    """Replace one complete server subtree, preserving other TOML byte-for-byte."""
    tomllib.loads(original)  # reject malformed input before any write
    lines, kept, skip = original.splitlines(keepends=True), [], False
    for line in lines:
        if re.match(r"\s*\[", line):
            header = line.strip().split("#", 1)[0].strip()
            # Parse header with tomllib, so quoted keys have their normal meaning.
            try:
                value = tomllib.loads(header + "\n__agents_probe = true\n")
                skip = name in value.get("mcp_servers", {})
            except tomllib.TOMLDecodeError:
                skip = False
        if not skip: kept.append(line)
    # Inline mcp_servers definitions cannot be rewritten without a TOML AST.
    remaining = "".join(kept).rstrip() + "\n"
    if name in tomllib.loads(remaining).get("mcp_servers", {}):
        raise ValueError("Inline MCP tables require conversion to table syntax before migration")
    section = "\n[mcp_servers." + json.dumps(name) + "]\n"
    for key, value in entry.items():
        section += json.dumps(key) + " = " + toml_value(value) + "\n"
    updated = remaining + section
    tomllib.loads(updated)
    return updated


class ClientMigration:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.config = read_json(self.directory / "service.json")
        if not self.config: raise ValueError("Service is not installed")
        self.token = (self.directory / "token").read_text().strip()
        self.registry = WorkspaceRegistry(directory)
        self.expected_versions = {}

    def read_config(self, path, *, as_json=True):
        path = Path(path)
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            content = None
        self.expected_versions[str(path)] = hashlib.sha256(content).hexdigest() if content is not None else None
        if as_json:
            return json.loads(content) if content is not None else {}
        return content.decode() if content is not None else ""

    def headers(self, identity=None):
        headers = {"Authorization": "Bearer " + self.token}
        if identity: headers["X-Agents-Workspace"] = identity
        return headers

    def bridge(self, identity=None):
        node = self.config.get("node")
        if not node or not Path(node).is_file(): raise ValueError("An absolute Node executable is required")
        private = self.directory / "bridges" / ((identity or "routing") + ".json")
        private_dir(private.parent)
        write_json(private, {"url": self.url, "headers": self.headers(identity)})
        return {"command": node, "args": [str(Path(self.config["installation"]) / "bridge/stdio.mjs"), str(private)]}

    @property
    def url(self): return f"http://127.0.0.1:{self.config['port']}/mcp"

    def prepare(self, client, workspace=None, *, home=None):
        home = Path(home or Path.home())
        root = Path(workspace).resolve() if workspace else None
        identity = self.registry.register(root) if root else None
        headers = self.headers(identity)
        if client == "codex":
            path = (root or home) / ".codex/config.toml"
            original = self.read_config(path, as_json=False)
            old_entry = tomllib.loads(original).get("mcp_servers", {}).get(SERVER, {})
            entry = transport_entry(old_entry, {})
            entry["url"] = self.url
            if tracked(path):
                # Helper reads a private bridge config; it never embeds the token.
                bridge = self.bridge(identity)
                entry["http_headers_helper"] = shlex.join([bridge["command"], str(Path(self.config["installation"]) / "bridge/headers.mjs"), bridge["args"][1]])
            else: entry["http_headers"] = headers
            return path, replace_toml_server(original, SERVER, entry), bool("http_headers" in entry)
        if client == "claude":
            path = home / ".claude.json"
            document = self.read_config(path)
            scope = document.setdefault("projects", {}).setdefault(str(root), {}) if root else document
            servers = scope.setdefault("mcpServers", {})
            servers[SERVER] = transport_entry(servers.get(SERVER, {}), {"type": "http", "url": self.url, "headers": headers})
        elif client == "claude-project":
            if root is None: raise ValueError("claude-project requires a workspace")
            path = root / ".mcp.json"
            document = self.read_config(path)
            servers = document.setdefault("mcpServers", {})
            entry = self.bridge(identity) if tracked(path) else {"type": "http", "url": self.url, "headers": headers}
            servers[SERVER] = transport_entry(servers.get(SERVER, {}), entry)
        elif client == "claude-deny-desktop":
            path = home / ".claude/settings.json"
            document = self.read_config(path)
            deny = document.setdefault("permissions", {}).setdefault("deny", [])
            for namespace in (DESKTOP_SERVER, DESKTOP_SERVER.replace("-", "_")):
                rule = "mcp__" + namespace + "__*"
                if rule not in deny: deny.append(rule)
        elif client == "cursor":
            path = (root or home) / ".cursor/mcp.json"
            document = self.read_config(path)
            servers = document.setdefault("mcpServers", {})
            entry = self.bridge(identity) if tracked(path) else {"url": self.url, "headers": headers}
            servers[SERVER] = transport_entry(servers.get(SERVER, {}), entry)
        elif client == "desktop":
            path = home / "Library/Application Support/Claude/claude_desktop_config.json"
            document = self.read_config(path)
            servers = document.setdefault("mcpServers", {})
            previous = servers.pop(SERVER, servers.get(DESKTOP_SERVER, {}))
            servers[DESKTOP_SERVER] = transport_entry(previous, self.bridge(identity))
        else: raise ValueError("Unknown client")
        content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        return path, content, self.token in content

    def apply(self, changes, *, on_prepared=None):
        if len({str(path) for path, _, _ in changes}) != len(changes):
            raise ValueError("Duplicate configuration target")
        backups = private_dir(self.directory / "backups" / str(time.time_ns()))
        journal = []
        for path, content, secret in changes:
            path = Path(path)
            if path.is_symlink(): raise ValueError("Client config cannot be a symlink")
            if secret and tracked(path): raise ValueError("Refusing to write a token into a tracked configuration")
            current = path.read_bytes() if path.exists() else None
            digest = hashlib.sha256(current).hexdigest() if current is not None else None
            if str(path) in self.expected_versions and digest != self.expected_versions[str(path)]:
                raise ValueError("Client config changed after preparation; prepare the migration again")
            journal.append({"path": str(path), "before": base64.b64encode(path.read_bytes()).decode() if path.exists() else None,
                            "after": base64.b64encode(content.encode()).decode()})
        write_json(backups / "changes.json", journal)
        write_json(self.directory / "migration.json", {"backup": str(backups), "state": "applying"})
        if on_prepared:
            on_prepared(backups)
        written = []
        try:
            for (path, content, secret), record in zip(changes, journal):
                path = Path(path)
                current = base64.b64encode(path.read_bytes()).decode() if path.exists() else None
                if current != record["before"]:
                    raise ValueError("Client config changed during migration")
                atomic_private(path, content)
                written.append(record)
                if secret: exclude_private(path)
        except BaseException:
            # Roll back only our writes; preserve a concurrently edited file.
            for record in reversed(written):
                path = Path(record["path"])
                if path.exists() and path.read_bytes() == base64.b64decode(record["after"]):
                    if record["before"] is None: path.unlink()
                    else: atomic_private(path, base64.b64decode(record["before"]))
            raise
        write_json(self.directory / "migration.json", {"backup": str(backups), "state": "applied"})
        return backups

    def restore(self, backup, *, check=True):
        records = read_json(Path(backup) / "changes.json", [])
        if check:
            for record in records:
                path = Path(record["path"])
                if not path.exists() or path.read_bytes() != base64.b64decode(record["after"]):
                    raise ValueError("Client config changed after migration; restore would overwrite user edits")
        for record in records:
            path = Path(record["path"])
            if record["before"] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_private(path, base64.b64decode(record["before"]).decode())
