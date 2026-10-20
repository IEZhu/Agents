"""Atomic client configuration migration with private, restorable backups."""
from pathlib import Path
import base64
import hashlib
import json
import locale
import os
import re
import shutil
import shlex
import subprocess
import sys
import time
import tomllib

from src import executables
from .state import atomic_private, read_json, write_json, private_dir
from .workspaces import WorkspaceRegistry
from .bridge import bridge_command
from src.client_paths import DESKTOP_DENY_RULES, DESKTOP_SERVER, absolute_path, client_config_path, desktop_config_paths
from scripts._helpers.inject_claude_md import render as render_routing_section


SERVER = "Agents-Core"
# X-Agents-Client of each managed client: the daemon counts requests per app (usage.py, #187).
APPS = {"claude": "claude-code", "claude-project": "claude-code", "codex": "codex", "cursor": "cursor",
        "desktop": "claude-desktop", "antigravity": "antigravity"}
# The app of a client that still starts `src/server.py` over stdio (`stdio_bridge`, #266).
STDIO_APP = "stdio"
TRANSPORT_KEYS = {"type", "command", "args", "env", "env_vars", "cwd", "url", "headers", "http_headers",
                  "env_http_headers", "bearer_token", "bearer_token_env_var", "http_headers_helper"}


def claude_code_plugin_skip(home=None, *, profile=None, force=False, workspaces=()):
    """Why no Agents-Core entry is written for the Claude Code profile ``profile``, or None to write it.

    The agents-core Claude Code plugin, installed and enabled in that profile (by default the
    environment's), starts its own Agents-Core; a second entry would list every tool twice
    (#367). ``force`` (``--force-claude-code-mcp``, or the choice kept in the registrations
    state) or ``AGENTS_FORCE_CLAUDE_CODE_MCP=1`` writes it anyway. ``workspaces`` are projects
    whose own settings may disable the plugin. Reads Claude Code's plugin state only, and
    uncertain state means "write it", as before.
    """
    from src.claude_plugin import skip_reason
    return skip_reason(profile=profile, home=home, force=force, workspaces=workspaces)


class ConfigChanged(ValueError):
    """A client changed its configuration between our read and our write; preparing again is safe."""


def transport_entry(previous, replacement):
    return {**{key: value for key, value in previous.items() if key not in TRANSPORT_KEYS}, **replacement}


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(k) + " = " + toml_value(v) for k, v in value.items()) + " }"
    if isinstance(value, list): return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(value)


def _git(*arguments, check=False):
    # The service starts git too (#292): no console window and no inherited stdin on Windows.
    return subprocess.run([executables.path("git"), *arguments], capture_output=True, text=True, check=check,
                          stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def tracked(path):
    """Whether Git tracks the file. A Git failure other than "not tracked" or "not a repository",
    such as a repository with dubious ownership, counts as tracked: a token must not reach it."""
    path = Path(path)
    cwd = path.parent
    while not cwd.exists():
        cwd = cwd.parent
    result = _git("-C", str(cwd), "ls-files", "--error-unmatch", str(path))
    if result.returncode in (0, 1):
        return result.returncode == 0
    if "did not match any file" in result.stderr or "not a git repository" in result.stderr.lower():
        return False
    return True


def exclude_private(path):
    """Local git exclusion only; never commit a token-bearing configuration."""
    path = Path(path)
    result = _git("-C", str(path.parent), "rev-parse", "--show-toplevel")
    if result.returncode:
        return
    root = Path(result.stdout.strip())
    result = _git("-C", str(root), "rev-parse", "--git-path", "info/exclude", check=True)
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
    def __init__(self, directory, *, config=None, token=None):
        self.directory = Path(directory)
        self.config = config if config is not None else read_json(self.directory / "service.json")
        if not self.config: raise ValueError("Service is not installed")
        self.token = token if token is not None else (self.directory / "token").read_text().strip()
        self.registry = WorkspaceRegistry(directory)
        self.expected_versions = {}
        self.config_targets = {}
        self.staged_changes = {}

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

    def headers(self, identity=None, app=None):
        headers = {"Authorization": "Bearer " + self.token}
        if identity: headers["X-Agents-Workspace"] = identity
        if app: headers["X-Agents-Client"] = app
        return headers

    def bridge(self, identity=None, app=None, *, auto_workspace=False):
        """A stdio bridge entry. With auto_workspace, the bridge registers the project its
        client starts it in, once per session (#253), instead of carrying a fixed identity."""
        command, script = bridge_command(self.config)
        private, settings = self._bridge_file(identity, app, auto_workspace=auto_workspace)
        private_dir(private.parent)
        write_json(private, settings)
        return {"command": command, "args": [str(script), str(private)]}

    def _bridge_file(self, identity=None, app=None, *, auto_workspace=False):
        # One file per app and workspace: the headers name the app.
        name = "auto" if auto_workspace else identity or "routing"
        settings = {"url": self.url, "headers": self.headers(identity, app)}
        if auto_workspace: settings["workspace"] = "auto"
        return self.directory / "bridges" / ((f"{app}-" if app else "") + name + ".json"), settings

    def stdio_bridge(self):
        """The bridge configuration that stdio servers of this installation serve through (#266).

        `src.startup` runs `bridge/stdio.mjs` with `bridges/stdio-auto.json` in place of a
        second engine; `token rotate` rewrites it with every other bridge configuration. It is
        written only when it changed. Python supplies the bridge when Node is absent.
        """
        private, settings = self._bridge_file(app=STDIO_APP, auto_workspace=True)
        try:
            current = read_json(private)
        except ValueError:  # torn: write it again
            current = None
        if current != settings:
            self.bridge(app=STDIO_APP, auto_workspace=True)
        return private

    def provision_antigravity_plugin(self, *, home=None, bridge_config=None, registry_document=None):
        """Prepares the plugin's startup protocol, workspace hook and enabled registration."""
        command, _ = bridge_command(self.config)
        home = absolute_path(home or Path.home())
        config_dir = home / ".gemini" / "config"
        plugin_dir = config_dir / "plugins" / "agents-core"
        installation = Path(self.config.get("installation", Path(__file__).resolve().parents[2]))
        hook_script = installation / "bridge" / ("antigravity_hook.mjs" if self.has_node() else "antigravity_hook.py")
        bridge_path = str(bridge_config) if bridge_config else str(self.directory / "bridges" / "antigravity-auto.json")
        cmd = shlex.join([command, str(hook_script), bridge_path])

        registry_json = config_dir / "config.json"
        try:
            raw_registry = self.read_config(registry_json) if registry_document is None else registry_document
        except (ValueError, OSError) as e:
            raise ValueError(f"Invalid JSON in {registry_json}: {e}") from e
        if not isinstance(raw_registry, dict):
            raise ValueError(f"Plugin registry in {registry_json} must be a JSON object")
        raw_plugins = raw_registry.get("plugins", {})
        if not isinstance(raw_plugins, dict):
            raise ValueError(f"plugins in {registry_json} must be a JSON object")
        raw_plugin = raw_plugins.get("agents-core", {})
        if not isinstance(raw_plugin, dict):
            raise ValueError(f"plugins.agents-core in {registry_json} must be a JSON object")
        registry = dict(raw_registry)
        plugins = dict(raw_plugins)
        plugins["agents-core"] = {**raw_plugin, "enabled": True}
        registry["plugins"] = plugins
        registry_content = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"

        plugin_json = plugin_dir / "plugin.json"
        try:
            raw_manifest = self.read_config(plugin_json)
        except Exception as e:
            raise ValueError(f"Invalid JSON in {plugin_json}: {e}") from e
        if not isinstance(raw_manifest, dict):
            raise ValueError(f"Plugin configuration in {plugin_json} must be a JSON object")
        manifest = dict(raw_manifest)
        manifest.setdefault("name", "agents-core")
        manifest.setdefault("description", "Agents-Core workspace binding and session management for Antigravity.")
        plugin_content = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

        hooks_json = plugin_dir / "hooks.json"
        try:
            raw_hooks = self.read_config(hooks_json)
        except Exception as e:
            raise ValueError(f"Invalid JSON in {hooks_json}: {e}") from e
        if not isinstance(raw_hooks, dict):
            raise ValueError(f"Hooks configuration in {hooks_json} must be a JSON object")
        hooks = dict(raw_hooks)
        hooks["agents-core-workspace"] = {
            "enabled": True,
            "PreToolUse": [
                {
                    "matcher": "call_mcp_tool|mcp_Agents-Core_.*|mcp_Agents_Core_.*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": cmd,
                            "timeout": 5,
                        }
                    ],
                }
            ],
        }
        hooks_content = json.dumps(hooks, ensure_ascii=False, indent=2) + "\n"
        rules_file = plugin_dir / "rules" / "AGENTS.md"
        rules_original = self.read_config(rules_file, as_json=False)
        template = installation / "scripts" / "templates" / "routing-protocol-core.md"
        rules_content = render_routing_section(
            rules_original.encode(), template.read_bytes(), rules_file,
        ).decode()
        return [
            (plugin_json, plugin_content, False),
            (hooks_json, hooks_content, False),
            (rules_file, rules_content, False),
            (registry_json, registry_content, False),
        ]

    def has_node(self):
        node = self.config.get("node")
        return bool(node) and Path(node).is_absolute() and Path(node).is_file()

    def layer_transport(self, client, root=None, *, home=None, requested=None):
        """``bridge`` or ``http`` for a Codex or Cursor layer (#292).

        Codex merges a server's fields across ~/.codex/config.toml and a project's
        .codex/config.toml, so a project layer takes the transport of the user layer it is
        merged with; a stdio layer over an HTTP one is an entry Codex refuses. Otherwise, and
        for the user layer, the bridge when Node is recorded. ``requested`` decides alone.
        """
        if requested is not None:
            if requested not in ("bridge", "http"):
                raise ValueError("Unknown transport: " + str(requested))
            return requested
        if root is not None:
            try:
                user = client_config_path(client, home=home)
                text = user.read_text(encoding="utf-8")
                servers = (tomllib.loads(text).get("mcp_servers", {}) if client == "codex"
                           else json.loads(text).get("mcpServers", {}))
                entry = servers.get(SERVER)
            except (OSError, ValueError, AttributeError):
                entry = None
            if isinstance(entry, dict) and set(entry) & TRANSPORT_KEYS:
                return "http" if "url" in entry else "bridge"
        return "bridge"

    @property
    def url(self): return f"http://127.0.0.1:{self.config['port']}/mcp"

    def prepare(self, client, workspace=None, *, home=None, config_path=None, transport=None):
        """One client configuration's change: ``(path, content, holds_a_secret)``.

        ``transport`` (``bridge`` or ``http``) sets a Codex or Cursor layer's transport; by
        default it follows ``layer_transport``.
        """
        home = absolute_path(home or Path.home())
        root = Path(workspace).resolve() if workspace else None
        path = client_config_path(client, root, home=home, config_path=config_path)
        self.config_targets[str(path)] = {"client": client, "path": str(path), "workspace": str(root) if root else None}
        identity = self.registry.register(root) if root else None
        app = APPS.get(client)
        headers = self.headers(identity, app)
        if client == "codex":
            original = self.read_config(path, as_json=False)
            old_entry = tomllib.loads(original).get("mcp_servers", {}).get(SERVER, {})
            if self.layer_transport(client, root, home=home, requested=transport) == "bridge":
                # Codex starts a stdio server without `cwd` in the session's working directory
                # (codex-rs LocalStdioServerLauncher), so the user layer's bridge names that
                # project (#253); a project layer's bridge keeps its fixed identity.
                entry = transport_entry(old_entry, self.bridge(identity, app, auto_workspace=root is None))
                return path, replace_toml_server(original, SERVER, entry), False
            if tracked(path):
                # HTTP like the layer it is merged with, and the token stays out of the file:
                # the helper reads a private bridge config.
                bridge = self.bridge(identity, app)
                helper = Path(bridge["args"][0]).with_name("headers" + Path(bridge["args"][0]).suffix)
                entry = transport_entry(old_entry, {"url": self.url, "http_headers_helper": shlex.join(
                    [bridge["command"], str(helper), bridge["args"][1]])})
                return path, replace_toml_server(original, SERVER, entry), False
            entry = transport_entry(old_entry, {"url": self.url, "http_headers": headers})
            return path, replace_toml_server(original, SERVER, entry), True
        if client == "claude":
            document = self.read_config(path)
            scope = document.setdefault("projects", {}).setdefault(str(root), {}) if root else document
            servers = scope.setdefault("mcpServers", {})
            if root is None:
                # The user scope serves every project: a bridge per session names its own (#253).
                entry = self.bridge(None, app, auto_workspace=True)
            else:
                entry = {"type": "http", "url": self.url, "headers": headers}
            servers[SERVER] = transport_entry(servers.get(SERVER, {}), entry)
        elif client == "claude-project":
            if root is None: raise ValueError("claude-project requires a workspace")
            document = self.read_config(path)
            servers = document.setdefault("mcpServers", {})
            entry = self.bridge(identity, app) if tracked(path) else {"type": "http", "url": self.url, "headers": headers}
            servers[SERVER] = transport_entry(servers.get(SERVER, {}), entry)
        elif client == "claude-deny-desktop":
            document = self.read_config(path)
            deny = document.setdefault("permissions", {}).setdefault("deny", [])
            deny.extend(rule for rule in DESKTOP_DENY_RULES if rule not in deny)
        elif client == "cursor":
            document = self.read_config(path)
            servers = document.setdefault("mcpServers", {})
            # One transport in every layer, as for Codex (#292): whether Cursor merges a server
            # across ~/.cursor/mcp.json and a project's .cursor/mcp.json is not documented. A
            # tracked file never holds the token, so it takes the bridge either way.
            if self.layer_transport(client, root, home=home, requested=transport) == "bridge" or tracked(path):
                entry = self.bridge(identity, app, auto_workspace=root is None)
            else:
                entry = {"url": self.url, "headers": headers}
            servers[SERVER] = transport_entry(servers.get(SERVER, {}), entry)
        elif client == "desktop":
            document = self.read_config(path)
            servers = document.setdefault("mcpServers", {})
            previous = servers.pop(SERVER, servers.get(DESKTOP_SERVER, {}))
            servers[DESKTOP_SERVER] = transport_entry(previous, self.bridge(identity, app))
        elif client == "antigravity":
            manage_plugin = root is None
            if manage_plugin:
                target = path.parent.resolve() / path.name
                plugin_dir = home / ".gemini/config/plugins/agents-core"
                for relative in ("plugin.json", "hooks.json", "rules/AGENTS.md"):
                    plugin_path = plugin_dir / relative
                    if target == plugin_path.parent.resolve() / plugin_path.name:
                        raise ValueError(f"Antigravity MCP config cannot share plugin asset: {plugin_path}")
            document = self.read_config(path)
            servers = document.setdefault("mcpServers", {})
            plugin_changes = []
            if manage_plugin:
                registry_path = home / ".gemini/config/config.json"
                # Resolve directory aliases while retaining final-file symlink checks.
                shared_registry = target == registry_path.parent.resolve() / registry_path.name
                registry_document = document if shared_registry else None
                bridge_config, _ = self._bridge_file(app=app, auto_workspace=True)
                # Validate instructions and every plugin asset before writing bridge settings.
                plugin_changes = self.provision_antigravity_plugin(
                    home=home, bridge_config=bridge_config, registry_document=registry_document,
                )
            entry = self.bridge(identity, app, auto_workspace=root is None)
            servers[SERVER] = transport_entry(servers.get(SERVER, {}), entry)
            if plugin_changes:
                self.staged_changes[str(path)] = []
                for plugin_path, plugin_content, secret in plugin_changes:
                    if shared_registry and plugin_path == registry_path:
                        # An alternate MCP config can share the application registry.
                        document["plugins"] = json.loads(plugin_content)["plugins"]
                    else:
                        self.staged_changes[str(path)].append((plugin_path, plugin_content, secret))
        else: raise ValueError("Unknown client")
        content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        return path, content, self.token in content

    def apply(self, changes, *, on_prepared=None):
        changes = list(changes)
        applied_staged_keys = []
        if self.staged_changes:
            extra = []
            for path, _, _ in changes:
                key = str(path)
                if key in self.staged_changes:
                    extra.extend(self.staged_changes[key])
                    applied_staged_keys.append(key)
            if extra:
                seen = {str(p) for p, _, _ in changes}
                for c in extra:
                    if str(c[0]) not in seen:
                        changes.append(c)
                        seen.add(str(c[0]))
        if len({str(path) for path, _, _ in changes}) != len(changes):
            raise ValueError("Duplicate configuration target")
        targets = [self.config_targets[str(path)] for path, _, _ in changes if str(path) in self.config_targets]
        if targets:
            # Journal this metadata with the configs so failure/restore remain atomic.
            registry_path = self.directory / "client-configs.json"
            if any(Path(path).absolute() == registry_path.absolute() for path, _, _ in changes):
                raise ValueError("Client config collides with the service configuration registry")
            previous = self.read_config(registry_path)
            from .audit import registered_configs
            known = registered_configs(previous)
            merged = {record["path"]: record for record in known}
            merged.update({record["path"]: record for record in targets})
            content = json.dumps({"version": 1, "configs": list(merged.values())}, indent=2) + "\n"
            changes.append((registry_path, content, False))
        backups = private_dir(self.directory / "backups" / str(time.time_ns()))
        journal = []
        for path, content, secret in changes:
            path = Path(path)
            if path.is_symlink(): raise ValueError("Client config cannot be a symlink")
            if secret and tracked(path): raise ValueError("Refusing to write a token into a tracked configuration")
            current = path.read_bytes() if path.exists() else None
            digest = hashlib.sha256(current).hexdigest() if current is not None else None
            if str(path) in self.expected_versions and digest != self.expected_versions[str(path)]:
                raise ConfigChanged("Client config changed after preparation; prepare the migration again")
            record = {"path": str(path), "before": base64.b64encode(path.read_bytes()).decode() if path.exists() else None,
                      "after": base64.b64encode(content.encode()).decode()}
            if path == self.directory / "client-configs.json":
                record["inventory"] = True
            journal.append(record)
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
                    raise ConfigChanged("Client config changed during migration")
                # The journaled bytes exactly: a str would be written in text mode, CRLF and the ANSI
                # code page on Windows, and neither restore nor the rollback below would match it.
                atomic_private(path, content.encode())
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
        for key in applied_staged_keys:
            self.staged_changes.pop(key, None)
        return backups

    def restore(self, backup, *, check=True):
        records = read_json(Path(backup) / "changes.json", [])
        # Keep discovery metadata, including restored legacy paths. A later profile
        # migration must not prevent restoring an unrelated earlier client backup.
        records = [(record, restore_target(record)) for record in records if not record.get("inventory")]
        if check:
            for record, path in records:
                if not path.exists() or path.read_bytes() not in written_forms(base64.b64decode(record["after"])):
                    raise ValueError("Client config changed after migration; restore would overwrite user edits")
        for record, path in records:
            if record["before"] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_private(path, base64.b64decode(record["before"]))


def written_forms(after):
    """The bytes a journaled write can have left in its file: ``after`` itself, or, on Windows,
    the text-mode form that migrations wrote before #270 (CRLF, the ANSI code page)."""
    forms = {after}
    if sys.platform == "win32":
        try:
            forms.add(after.decode("utf-8").replace("\n", "\r\n").encode(locale.getpreferredencoding(False)))
        except (UnicodeError, LookupError):  # the old write of such content failed before writing
            pass
    return forms


def restore_target(record):
    """The file a journaled change restores: its path, except for a Claude desktop configuration
    journaled under %APPDATA% before #270 by a migration that ran inside the app's MSIX package.
    That write went to the package's copy, which then holds what the journal says was written."""
    path = Path(record["path"])
    forms = written_forms(base64.b64decode(record["after"]))
    try:
        if sys.platform != "win32" or path.read_bytes() in forms:
            return path
    except OSError:
        pass
    appdata, package_copy = desktop_config_paths()
    try:
        if package_copy is not None and path == appdata and package_copy.read_bytes() in forms:
            return package_copy
    except OSError:
        pass
    return path
