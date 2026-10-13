"""Whether the agents-core Claude Code plugin serves Agents-Core in Claude Code (#367).

The plugin (``plugins/agents-core``) registers the ``Agents-Core`` stdio server for Claude Code
itself. A user-scope ``Agents-Core`` entry that the installer or ``migrate`` writes next to it makes
Claude Code list every tool twice and start two servers, so the writers skip only that entry while
the plugin is installed and enabled, and a check warns about an existing pair.

Everything here reads Claude Code's own plugin state and never writes it. The locations, as the
Claude Code documentation describes them (checked 2026-10-13):

- the plugins root is ``~/.claude/plugins``, or ``CLAUDE_CODE_PLUGIN_CACHE_DIR`` when set
  (code.claude.com/docs/en/plugins/loading, "Find plugins on disk"; docs/en/env-vars); with
  ``CLAUDE_CONFIG_DIR`` set, Claude Code keeps "settings, session history, and plugins" there
  (docs/en/settings);
- ``installed_plugins.json`` in that root "records each install with its ``scope``, ``installPath``,
  and ``version``", one record list under each plugin id; the file declares a format ``version``
  (docs/en/plugins/loading, docs/en/plugins/troubleshooting). The ``{"version": 2, "plugins":
  {"<id>": [records]}}`` layout read here is the one Claude Code writes today; the docs name its
  keys but do not print the whole layout;
- a marketplace plugin's id is ``<name>@<marketplace>``; ``enabledPlugins`` in the user settings
  (``settings.json`` in the configuration home) turns it on or off, and a plugin the user has not
  set there starts enabled unless its manifest sets ``defaultEnabled: false``, which ours does not
  (docs/en/plugins/loading "Find where a plugin came from", docs/en/plugins/manifest-reference).

Not covered, so such a setup counts as "no plugin" and keeps today's behaviour: installs in the
project or local scope (they serve one project, the user-scope entry serves the others), plugins
synced from claude.ai (``@synced``, no install record), session-only ``--plugin-dir`` plugins, and
project, local or managed ``enabledPlugins`` overrides. Any unreadable or unexpected state also
counts as "no plugin".
"""
import json
import os
from pathlib import Path

from src.client_paths import absolute_path, client_config_path, client_home


PLUGIN_NAME = "agents-core"
SERVER = "Agents-Core"
FORCE_ENV = "AGENTS_FORCE_CLAUDE_CODE_MCP"
FORCE_FLAG = "--force-claude-code-mcp"
REMEDY = (f"`claude mcp remove {SERVER} --scope user` removes the user-scope entry "
          f"(or disable the {PLUGIN_NAME} plugin in /plugin to keep it)")


def plugins_root(*, home=None, environ=None):
    env = os.environ if environ is None else environ
    if env.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"):
        return absolute_path(env["CLAUDE_CODE_PLUGIN_CACHE_DIR"])
    return client_home("claude", home=home, environ=env) / "plugins"


def _read_json(path):
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8-sig"))


def installed_ids(*, home=None, environ=None):
    """Ids of user-scope installs of the plugin, from ``installed_plugins.json``; [] when unknown."""
    try:
        document = _read_json(plugins_root(home=home, environ=environ) / "installed_plugins.json")
    except (OSError, ValueError):
        return []
    plugins = document.get("plugins") if isinstance(document, dict) else None
    if not isinstance(plugins, dict):
        return []
    found = []
    for plugin_id, records in plugins.items():
        name, separator, origin = str(plugin_id).partition("@")
        if name != PLUGIN_NAME or not separator or not origin or not isinstance(records, list):
            continue
        if any(isinstance(record, dict) and record.get("scope", "user") == "user" for record in records):
            found.append(plugin_id)
    return found


def enabled_ids(*, home=None, environ=None):
    """The installed ids that the user settings leave enabled; [] when the settings are unreadable."""
    ids = installed_ids(home=home, environ=environ)
    if not ids:
        return []
    settings = client_home("claude", home=home, environ=environ) / "settings.json"
    try:
        document = _read_json(settings)
    except FileNotFoundError:
        document = {}
    except (OSError, ValueError):
        return []
    if not isinstance(document, dict):
        return []
    enabled = document.get("enabledPlugins", {})
    if not isinstance(enabled, dict):
        return []
    # A plugin the user has not set starts enabled (defaultEnabled defaults to true).
    return [plugin_id for plugin_id in ids if enabled.get(plugin_id, True) is not False]


def force_requested(environ=None):
    env = os.environ if environ is None else environ
    return str(env.get(FORCE_ENV, "")).strip().lower() in ("1", "true", "yes")


def skip_reason(*, home=None, environ=None, force=False):
    """Why the Claude Code ``Agents-Core`` registration is skipped, or None to write it.

    Never raises: any failure to read the plugin state means "write it", today's behaviour.
    """
    if force or force_requested(environ):
        return None
    try:
        ids = enabled_ids(home=home, environ=environ)
    except Exception:  # noqa: BLE001 - fail safe: unknown state is "no plugin"
        return None
    if not ids:
        return None
    return (f"the {ids[0]} plugin is installed and enabled for Claude Code and starts its own {SERVER} "
            f"server, so no {SERVER} entry is written for Claude Code (instructions and other clients "
            f"are still configured); pass {FORCE_FLAG} or set {FORCE_ENV}=1 to register it anyway")


def registrations(*, home=None, environ=None, workspaces=()):
    """User- and project-level ``Agents-Core`` entries of Claude Code: ``[(path, scope)]``."""
    found = []
    path = client_config_path("claude", home=home, environ=os.environ if environ is None else environ)
    try:
        document = _read_json(path)
    except (OSError, ValueError):
        document = {}
    if isinstance(document, dict):
        servers = document.get("mcpServers")
        if isinstance(servers, dict) and SERVER in servers:
            found.append((str(path), "user"))
        projects = document.get("projects")
        if isinstance(projects, dict):
            for root, entry in projects.items():
                servers = entry.get("mcpServers") if isinstance(entry, dict) else None
                if isinstance(servers, dict) and SERVER in servers:
                    found.append((str(path), "local:" + str(root)))
    for root in workspaces:
        project = Path(root) / ".mcp.json"
        try:
            servers = _read_json(project).get("mcpServers")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(servers, dict) and SERVER in servers:
            found.append((str(project), "project"))
    return found


def duplicate_warning(*, home=None, environ=None, workspaces=()):
    """A warning when Claude Code has the plugin's server and another ``Agents-Core`` entry, or None."""
    try:
        ids = enabled_ids(home=home, environ=environ)
        if not ids:
            return None
        others = registrations(home=home, environ=environ, workspaces=workspaces)
    except Exception:  # noqa: BLE001 - a check never fails the command it is part of
        return None
    if not others:
        return None
    where = ", ".join(f"{scope} ({path})" for path, scope in others)
    return {"warning": "duplicate_claude_code_server",
            "message": (f"Claude Code has the {SERVER} server of the {ids[0]} plugin and also {SERVER} "
                        f"entries in: {where}. It lists every tool twice and starts two servers; "
                        f"keep one: {REMEDY}. A project's .mcp.json or local entry is removed with "
                        f"`claude mcp remove {SERVER} --scope project` or `--scope local` there."),
            "plugin": ids[0], "registrations": [{"path": path, "scope": scope} for path, scope in others]}
