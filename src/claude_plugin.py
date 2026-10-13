"""Whether the agents-core Claude Code plugin serves Agents-Core in Claude Code (#367).

The plugin (``plugins/agents-core``) registers the ``Agents-Core`` stdio server for Claude Code
itself. A user-scope ``Agents-Core`` entry that the installer or ``migrate`` writes next to it makes
Claude Code list every tool twice and start two servers, so the writers skip only that entry while
the plugin is installed and enabled, and a check warns about an existing pair.

The rule throughout: when unsure, write the entry. A duplicate server is noisy; a missing one is
broken. So every case this module cannot settle counts as "no plugin".

Everything here reads Claude Code's own state and never writes it. Each check is about one Claude
Code profile, the configuration home that holds ``settings.json`` and ``plugins``: ``~/.claude``
for ``~/.claude.json``, otherwise the directory of the profile's ``.claude.json``
(``CLAUDE_CONFIG_DIR``). The locations, as the Claude Code documentation describes them (checked
2026-10-13):

- the plugins root is ``<profile>/plugins``, or ``CLAUDE_CODE_PLUGIN_CACHE_DIR`` when set, in the
  environment or in the ``env`` block of the profile's ``settings.json``
  (code.claude.com/docs/en/plugins/loading "Find plugins on disk"; docs/en/env-vars); with
  ``CLAUDE_CONFIG_DIR`` set, Claude Code keeps "settings, session history, and plugins" there
  (docs/en/settings);
- ``installed_plugins.json`` in that root "records each install with its ``scope``, ``installPath``,
  and ``version``", one record list under each plugin id (docs/en/plugins/loading). The
  ``{"version": 2, "plugins": {"<id>": [records]}}`` layout read here is the one Claude Code writes
  today; the docs name its keys but do not print the whole layout;
- a marketplace plugin's id is ``<name>@<marketplace>``. ``enabledPlugins`` merges across user,
  project (``.claude/settings.json``), local (``.claude/settings.local.json``), ``--settings`` and
  managed settings, managed ``false`` blocking it (docs/en/plugins/loading, docs/en/settings); a
  plugin nobody sets starts enabled unless its manifest sets ``defaultEnabled: false``, which ours
  does not (docs/en/plugins/manifest-reference). Here any ``false`` in a layer that is read counts
  as disabled: the user settings, the managed settings below, and the project and local settings
  of the projects the caller knows;
- managed settings files: ``managed-settings.json`` and ``managed-settings.d/*.json`` in
  ``/Library/Application Support/ClaudeCode`` (macOS) or ``/etc/claude-code`` (Linux and WSL), and
  the server-managed cache ``<profile>/remote-settings.json`` (docs/en/managed-settings,
  docs/en/server-managed-settings). The macOS configuration profile (domain
  ``com.anthropic.claudecode``) is read from ``/Library/Managed Preferences/com.anthropic.claudecode.plist``
  and the per-user ``/Library/Managed Preferences/<user>/`` copy: those paths are where macOS
  usually puts managed preferences and are not confirmed by the Claude Code docs.

Not covered, so such a setup may get a duplicate server (never a missing one): projects the caller
does not know that disable the plugin in their own settings, ``--settings`` flags, Windows registry
policies, a plugin enabled only in a project or local scope, plugins synced from claude.ai
(``@synced``, no install record) and session-only ``--plugin-dir`` plugins. A
``CLAUDE_CONFIG_DIR`` in the ``env`` block of the user settings that names another directory, a
plugins root set two different ways, and any unreadable or unexpected state count as "no plugin".
On Windows the plugin never counts: its ``start-server.sh`` does not run there, so the entry is
always written.
"""
import getpass
import json
import os
from pathlib import Path
import plistlib
import sys

from src.client_paths import absolute_path, client_config_path, client_home


PLUGIN_NAME = "agents-core"
SERVER = "Agents-Core"
FORCE_ENV = "AGENTS_FORCE_CLAUDE_CODE_MCP"
FORCE_FLAG = "--force-claude-code-mcp"
CACHE_ENV = "CLAUDE_CODE_PLUGIN_CACHE_DIR"
CONFIG_ENV = "CLAUDE_CONFIG_DIR"
REMEDY = (f"`claude mcp remove {SERVER} --scope user` removes the user-scope entry "
          f"(or disable the {PLUGIN_NAME} plugin in /plugin to keep it)")
# System directories of the managed settings files (docs/en/managed-settings).
MANAGED_DIRECTORIES = {"darwin": Path("/Library/Application Support/ClaudeCode"),
                       "linux": Path("/etc/claude-code")}
# Where macOS keeps configuration profiles' preferences; not confirmed by the Claude Code docs.
MANAGED_PREFERENCES = Path("/Library/Managed Preferences")
PREFERENCES_DOMAIN = "com.anthropic.claudecode"


class Uncertain(Exception):
    """A state this module cannot settle; the caller writes the entry."""


def profile_for_config(config_path, *, home=None):
    """The Claude Code profile directory of a ``.claude.json``: ``~/.claude`` for ``~/.claude.json``,
    otherwise the file's directory (``CLAUDE_CONFIG_DIR`` keeps ``.claude.json`` inside itself)."""
    path = absolute_path(config_path)
    home = absolute_path(home or Path.home())
    return home / ".claude" if path == home / ".claude.json" else path.parent


def _read_json(path):
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8-sig"))


def _settings(path):
    """A settings document, {} when the file is absent; Uncertain when it is unreadable."""
    try:
        document = _read_json(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise Uncertain(f"{path}: {error}") from None
    if not isinstance(document, dict):
        raise Uncertain(f"{path} is not a JSON object")
    return document


def _plist(path):
    try:
        with open(path, "rb") as handle:
            document = plistlib.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as error:  # noqa: BLE001 - plistlib raises several types
        raise Uncertain(f"{path}: {error}") from None
    if not isinstance(document, dict):
        raise Uncertain(f"{path} is not a dictionary")
    return document


def _env_value(document, name):
    env = document.get("env", {})
    if not isinstance(env, dict):
        raise Uncertain("the settings' env is not a mapping")
    value = env.get(name)
    return value if isinstance(value, str) and value.strip() else None


def plugins_root(profile, *, environ=None, settings=None):
    """The plugins root of a profile; Uncertain when the environment and the settings disagree."""
    env = os.environ if environ is None else environ
    values = {absolute_path(value) for value in (env.get(CACHE_ENV), _env_value(settings or {}, CACHE_ENV))
              if isinstance(value, str) and value.strip()}
    if len(values) > 1:
        raise Uncertain(f"{CACHE_ENV} names two directories")
    return values.pop() if values else absolute_path(profile) / "plugins"


def installed_ids(root):
    """Ids of user-scope installs of the plugin, from ``installed_plugins.json``; [] when unknown."""
    try:
        document = _read_json(Path(root) / "installed_plugins.json")
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


def managed_documents(profile, *, platform=None):
    """The managed settings documents present on this machine for a profile; Uncertain when one is
    present but unreadable."""
    platform = sys.platform if platform is None else platform
    documents = []
    directory = MANAGED_DIRECTORIES.get("darwin" if platform == "darwin" else
                                        "linux" if platform.startswith("linux") else "")
    if directory is not None:
        documents.append(_settings(directory / "managed-settings.json"))
        drop_ins = directory / "managed-settings.d"
        try:
            names = sorted(drop_ins.glob("*.json")) if drop_ins.is_dir() else []
        except OSError as error:
            raise Uncertain(f"{drop_ins}: {error}") from None
        documents.extend(_settings(path) for path in names)
    if platform == "darwin":
        documents.append(_plist(MANAGED_PREFERENCES / f"{PREFERENCES_DOMAIN}.plist"))
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001 - no user name: only the machine-wide profile
            user = None
        if user:
            documents.append(_plist(MANAGED_PREFERENCES / user / f"{PREFERENCES_DOMAIN}.plist"))
    documents.append(_settings(absolute_path(profile) / "remote-settings.json"))
    return documents


def _disabled(document, ids):
    enabled = document.get("enabledPlugins", {})
    if not isinstance(enabled, dict):
        raise Uncertain("enabledPlugins is not a mapping")
    return {plugin_id for plugin_id in ids if enabled.get(plugin_id) is False}


def enabled_ids(profile=None, *, home=None, environ=None, workspaces=(), platform=None):
    """The user-scope installed ids that no read layer disables; [] when anything is uncertain.

    ``profile`` defaults to the environment's (``CLAUDE_CONFIG_DIR`` or ``~/.claude``);
    ``workspaces`` are the projects whose ``.claude/settings.json`` and ``settings.local.json``
    are read as well.
    """
    if os.name == "nt":
        return []  # start-server.sh does not run on Windows: the entry is always written
    env = os.environ if environ is None else environ
    profile = absolute_path(profile) if profile is not None else client_home("claude", home=home, environ=env)
    try:
        user = _settings(profile / "settings.json")
        configured = _env_value(user, CONFIG_ENV)
        if configured and absolute_path(configured) != profile:
            raise Uncertain(f"the user settings set {CONFIG_ENV} to another directory")
        ids = installed_ids(plugins_root(profile, environ=env, settings=user))
        if not ids:
            return []
        layers = [user, *managed_documents(profile, platform=platform)]
        for root in workspaces:
            layers += [_settings(Path(root) / ".claude/settings.json"),
                       _settings(Path(root) / ".claude/settings.local.json")]
        disabled = set()
        for document in layers:
            disabled |= _disabled(document, ids)
    except Uncertain:
        return []
    return [plugin_id for plugin_id in ids if plugin_id not in disabled]


def force_requested(environ=None):
    env = os.environ if environ is None else environ
    return str(env.get(FORCE_ENV, "")).strip().lower() in ("1", "true", "yes")


def skip_reason(*, profile=None, home=None, environ=None, force=False, workspaces=()):
    """Why the Claude Code ``Agents-Core`` registration of ``profile`` is skipped, or None to write it.

    Never raises: any failure to read the plugin state means "write it", today's behaviour.
    """
    if force or force_requested(environ):
        return None
    try:
        ids = enabled_ids(profile, home=home, environ=environ, workspaces=workspaces)
    except Exception:  # noqa: BLE001 - fail safe: unknown state is "no plugin"
        return None
    if not ids:
        return None
    return (f"the {ids[0]} plugin is installed and enabled for Claude Code and starts its own {SERVER} "
            f"server, so no {SERVER} entry is written for Claude Code (instructions and other clients "
            f"are still configured); pass {FORCE_FLAG} or set {FORCE_ENV}=1 to register it anyway")


def registrations(*, config_path=None, home=None, environ=None, workspaces=()):
    """User-, local- and project-level ``Agents-Core`` entries of Claude Code: ``[(path, scope)]``.

    ``config_path`` is the profile's ``.claude.json``, by default the environment's.
    """
    found = []
    env = os.environ if environ is None else environ
    path = absolute_path(config_path) if config_path is not None else client_config_path(
        "claude", home=home, environ=env)
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


def duplicate_warning(*, config_path=None, home=None, environ=None, workspaces=()):
    """A warning when a Claude Code profile has the plugin's server and another ``Agents-Core``
    entry, or None. ``config_path`` is the profile's ``.claude.json``, by default the environment's."""
    env = os.environ if environ is None else environ
    try:
        path = absolute_path(config_path) if config_path is not None else client_config_path(
            "claude", home=home, environ=env)
        profile = profile_for_config(path, home=home) if config_path is not None else client_home(
            "claude", home=home, environ=env)
        ids = enabled_ids(profile, home=home, environ=env, workspaces=workspaces)
        if not ids:
            return None
        others = registrations(config_path=path, home=home, environ=env, workspaces=workspaces)
    except Exception:  # noqa: BLE001 - a check never fails the command it is part of
        return None
    if not others:
        return None
    where = ", ".join(f"{scope} ({entry})" for entry, scope in others)
    return {"warning": "duplicate_claude_code_server", "profile": str(profile),
            "message": (f"Claude Code ({profile}) has the {SERVER} server of the {ids[0]} plugin and also "
                        f"{SERVER} entries in: {where}. It lists every tool twice and starts two servers; "
                        f"keep one: {REMEDY}. A project's .mcp.json or local entry is removed with "
                        f"`claude mcp remove {SERVER} --scope project` or `--scope local` there."),
            "plugin": ids[0], "registrations": [{"path": entry, "scope": scope} for entry, scope in others]}
