"""Keep the client registrations this installation wrote in the form `migrate` writes now (#292).

A registration written before a change of that form stays as it was until someone runs
`migrate` again, and a user should never have to: an installation from before #253 kept an
HTTP user-scope entry without a workspace, and every unpinned project lost its turns. So the
service brings its registrations up to date at every start (`run`).

Only entries this installation wrote change (`managed`), and never one that is missing: a
project without an Agents-Core entry gets none. The layers of one client change together or
not at all, because a client may merge them: Codex merges a server's fields across
~/.codex/config.toml and a project's .codex/config.toml, and a stdio layer over an HTTP one is
an entry it refuses. Claude Code takes an entry whole from one scope, so only its user-scope
entries are kept current here; its project pins stay as `migrate --workspace` wrote them.
"""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import datetime
import json
import logging
import os
from pathlib import Path
import time
import tomllib

from src.client_paths import DESKTOP_SERVER, absolute_path, client_config_path, desktop_config_paths
from src.file_lock import file_lock
from .audit import registered_configs
from .clients import SERVER, TRANSPORT_KEYS, ClientMigration, ConfigChanged, claude_code_plugin_skip, tracked
from .state import read_json, write_json
from .workspaces import WorkspaceError

logger = logging.getLogger(__name__)

STATE_FILE = "registrations.json"
# Claude Code profiles whose Agents-Core entry is kept beside the agents-core plugin (#367).
FORCED_KEY = "claude_code_forced"
CLIENTS = ("claude", "codex", "cursor", "desktop", "antigravity")
# Clients whose project layers are kept current with their user layer.
LAYERED = ("codex", "cursor")
MANAGED_HEADERS = {"Authorization", "X-Agents-Workspace", "X-Agents-Client"}
ATTEMPTS = 3
# A start during an update's probation finds control.lock held by the controller.
LOCK_RETRY_SECONDS = 10
LOCK_RETRY_LIMIT = 60


@dataclass(frozen=True)
class Layer:
    client: str
    path: Path
    workspace: Path | None = None  # the project of a project layer; None for the user layer


class Blocked(Exception):
    """One layer must stay as it is, so none of its client's layers may change."""

    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def find_layers(directory, home=None):
    """The configuration files per client that may hold a registration of this installation.

    User layers: each client's default file and the files `client-configs.json` records without a
    workspace (another Claude profile, for example). Project layers of Codex and Cursor: their
    files in every registered workspace and in every recorded one. Files that do not exist are
    left out; whether a file holds an Agents-Core entry is read when it is upgraded.
    """
    directory = Path(directory)
    home = absolute_path(home or Path.home())
    try:
        records = registered_configs(read_json(directory / "client-configs.json", {}))
    except (OSError, ValueError):
        logger.warning("Ignoring an unreadable client-configs.json", exc_info=True)
        records = []
    roots = set(workspace_roots(directory))
    found = {client: [] for client in CLIENTS}
    seen = set()

    def add(client, path, workspace=None):
        path = Path(path)
        try:
            key = path.resolve()
            exists = path.is_file() or path.is_symlink()
        except (OSError, RuntimeError):
            return
        if key in seen or not exists:
            return
        seen.add(key)
        found[client].append(Layer(client, path, workspace))

    for client in CLIENTS:
        paths = desktop_config_paths(home=home, environ={}) if client == "desktop" and os.name == "nt" else [client_config_path(client, home=home, environ={})]
        for path in paths:
            if path is not None:
                add(client, path)
    for record in records:
        if record["client"] not in CLIENTS:
            continue
        if record.get("workspace") is None:
            add(record["client"], record["path"])
        elif record["client"] in LAYERED:
            roots.add(Path(record["workspace"]))
    for root in sorted(roots):
        try:
            present = root.is_dir()
        except OSError:  # an unmounted volume, for example
            continue
        if present:
            add("codex", root / ".codex/config.toml", root)
            add("cursor", root / ".cursor/mcp.json", root)
    return found


def workspace_roots(directory):
    """The registered workspaces' directories, sorted; [] when workspaces.json is unreadable."""
    try:
        saved = read_json(Path(directory) / "workspaces.json", {})
    except (OSError, ValueError):
        logger.warning("Ignoring an unreadable workspaces.json", exc_info=True)
        return []
    if not isinstance(saved, dict):
        return []
    return sorted({Path(root) for root in saved.values() if isinstance(root, str)})


def claude_profiles(directory, home=None):
    """Each Claude Code profile with its ``.claude.json``: ``{profile: config path}``.

    The default profile (``~/.claude``, or ``CLAUDE_CONFIG_DIR`` of this process) and every
    profile whose file ``find_layers`` finds, such as one ``client-configs.json`` records.
    """
    from src.claude_plugin import profile_for_config
    paths = [client_config_path("claude", home=home)]
    paths += [layer.path for layer in find_layers(directory, home)["claude"]]
    found = {}
    for path in paths:
        found.setdefault(str(profile_for_config(path, home=home)), Path(path))
    return found


def forced_profiles(directory):
    """The Claude Code profiles whose entry is written beside the agents-core plugin (#367)."""
    value = _saved(directory).get(FORCED_KEY, [])
    return {str(item) for item in value if isinstance(item, str)} if isinstance(value, list) else set()


def remember_force(directory, profiles):
    """Keep writing Claude Code's entry for these profiles beside the plugin, also in the upkeep."""
    profiles = {str(absolute_path(profile)) for profile in profiles}
    with _state_lock(directory):
        saved = _saved(directory)
        current = saved.get(FORCED_KEY, [])
        current = {str(item) for item in current if isinstance(item, str)} if isinstance(current, list) else set()
        write_json(Path(directory) / STATE_FILE, {**saved, FORCED_KEY: sorted(current | profiles)})


def read_entry(client, text):
    """The Agents-Core entry of a configuration's text, or None; ValueError when it is malformed."""
    if client == "codex":
        servers = tomllib.loads(text).get("mcp_servers", {})
    else:
        document = json.loads(text)
        if not isinstance(document, dict):
            raise ValueError("configuration is not a JSON object")
        servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("its server table is not a mapping")
    if client == "desktop":
        if SERVER in servers and DESKTOP_SERVER in servers:
            raise ValueError("multiple Agents-Core entries require explicit migration")
        return servers.get(DESKTOP_SERVER, servers.get(SERVER))
    return servers.get(SERVER)


def _headers(entry):
    headers = entry.get("http_headers", entry.get("headers", {}))
    return headers if isinstance(headers, dict) else None


def _is_bridge(migration, entry):
    args = entry.get("args")
    return (isinstance(args, list) and len(args) == 2 and "url" not in entry
            and Path(str(args[0])) in {Path(migration.config["installation"]) / "bridge" / name
                                      for name in ("stdio.mjs", "stdio.py")})


def _pinned(migration, entry):
    """The workspace UUID an entry carries, or None."""
    if _is_bridge(migration, entry):
        name = Path(str(entry["args"][1])).stem
        return name[-36:] if len(name) > 36 and name[-37] == "-" else None
    headers = _headers(entry) or {}
    return headers.get("X-Agents-Workspace")


def managed(migration, entry, workspace=None):
    """Whether this installation wrote the entry: its endpoint with only the headers it sets and its
    token, the header helper it writes for tracked Codex files, its bridge, or its stdio server.

    An entry the user adapted is not: one with a `cwd` or an `AGENTS_CLIENT_REPO_ROOT` override,
    which the upgrade would drop, or a project's entry pinned to another registered workspace.
    """
    if not isinstance(entry, dict):
        return False
    env = entry.get("env")
    if "cwd" in entry or (isinstance(env, dict) and "AGENTS_CLIENT_REPO_ROOT" in env):
        return False
    installation = Path(migration.config["installation"])
    if "url" in entry:
        headers = _headers(entry)
        helper = entry.get("http_headers_helper")
        if entry["url"] != migration.url or headers is None:
            return False
        if helper is not None:
            if not (isinstance(helper, str) and not headers and any(
                    str(installation / "bridge" / name) in helper for name in ("headers.mjs", "headers.py"))):
                return False
        elif not (set(headers) <= MANAGED_HEADERS and headers.get("Authorization") == "Bearer " + migration.token):
            return False
    elif _is_bridge(migration, entry):
        if Path(str(entry["args"][1])).parent != migration.directory / "bridges":
            return False
    else:
        args = entry.get("args")
        if not (isinstance(entry.get("command"), str) and isinstance(args, list) and args
                and Path(str(args[0])) == installation / "src/server.py"):
            return False
    identity = _pinned(migration, entry)
    if workspace is not None and identity:
        try:
            pinned_root = migration.registry.resolve(identity)
        except WorkspaceError:
            return True  # an identity that no longer resolves: re-pinning repairs it
        return pinned_root.resolve() == Path(workspace).resolve()
    return True


def plan_client(migration, client, layers, home=None, *, transport, automatic=False):
    """The changes that bring a client's layers to ``transport`` (``bridge`` or ``http``), and their files.

    Raises Blocked when one layer has to stay as it is: a symbolic link, an unreadable file or an
    entry this installation did not write. Then none of the client's layers may change. The
    ``automatic`` run also leaves a client alone when a tracked file would change, which would
    leave a diff in the user's repository, and never turns a bridge into HTTP: it reports a
    missing Node instead of writing the token into every project.
    """
    current = []
    for layer in layers:
        if automatic and client == "antigravity" and layer.path.parent.resolve() != (absolute_path(home or Path.home()) / ".gemini/config").resolve():
            raise Blocked("skipped", f"{layer.path}: external profile has no known active workspace-hook location")
        if layer.path.is_symlink():
            raise Blocked("failed", f"{layer.path} is a symbolic link, which migrate does not write through")
        try:
            entry = read_entry(client, layer.path.read_bytes().decode("utf-8"))
        except (OSError, ValueError, UnicodeError) as error:
            raise Blocked("failed", f"{layer.path} is unreadable: {error}") from None
        if entry is None or not (isinstance(entry, dict) and set(entry) & TRANSPORT_KEYS):
            continue  # no entry, or one that only overrides settings and takes the merged transport
        if not managed(migration, entry, layer.workspace):
            raise Blocked("skipped", f"{layer.path}: its {SERVER} entry was not written by this installation")
        current.append((layer, entry))
    if automatic and transport == "http" and any(_is_bridge(migration, entry) for _, entry in current):
        raise Blocked("failed", f"the recorded Node ({migration.config.get('node')}) is missing, so the bridges "
                                "cannot start; install Node at that path again")
    changes, files = [], []
    for layer, entry in current:
        try:
            change = migration.prepare(client, layer.workspace, home=home, config_path=layer.path,
                                       transport=transport)
        except (OSError, ValueError) as error:
            raise Blocked("failed", f"{layer.path}: {error}") from None
        staged = migration.staged_changes.get(str(change[0]), [])
        same_assets = all(path.is_file() and path.read_bytes() == content.encode("utf-8") for path, content, _ in staged)
        if read_entry(client, change[1]) == entry and same_assets:
            continue
        if automatic and tracked(layer.path):
            command = f"migrate --workspace {layer.workspace} --clients {client}" if layer.workspace \
                else f"migrate --clients {client}"
            raise Blocked("skipped", f"{layer.path} is tracked by Git; run `{command}` to change it there")
        changes.append(change)
        files.append(str(layer.path))
    return changes, files


def _plugin_layers(directory, layers, home):
    """Claude Code's layers split by the agents-core plugin (#367): ``(kept, [(layer, reason)])``.

    A profile with the plugin installed and enabled keeps its entries as they are, unless the
    user forced its entry (``remember_force``); each profile is checked on its own.
    """
    from src.claude_plugin import profile_for_config
    forced, roots = forced_profiles(directory), workspace_roots(directory)
    kept, skipped = [], []
    for layer in layers:
        profile = profile_for_config(layer.path, home=home)
        reason = claude_code_plugin_skip(home, profile=profile, force=str(profile) in forced, workspaces=roots)
        if reason:
            skipped.append((layer, reason))
        else:
            kept.append(layer)
    return kept, skipped


def _upgrade_client(directory, client, layers, home, lock):
    skipped = []
    if client == "claude" and layers:
        layers, skipped = _plugin_layers(directory, layers, home)
        if not layers:
            return {"status": "skipped", "files": [], "reason": skipped[0][1]}
    report = _upgrade_layers(directory, client, layers, home, lock)
    if skipped:
        # Other profiles were brought up to date; these stay to the plugin.
        report["plugin"] = [{"path": str(layer.path), "reason": reason} for layer, reason in skipped]
    return report


def _upgrade_layers(directory, client, layers, home, lock):
    migration = ClientMigration(directory)
    transport = "bridge"
    try:
        changes, files = plan_client(migration, client, layers, home, transport=transport, automatic=True)
    except Blocked as blocked:
        return {"status": blocked.status, "files": [], "reason": blocked.reason}
    if not changes:
        return {"status": "current", "files": []}
    with lock():
        backup = migration.apply(changes)
    return {"status": "upgraded", "files": files, "backup": str(backup)}


def upgrade(directory, home=None, *, attempts=ATTEMPTS, lock=nullcontext):
    """Bring every client's registrations to the current form; a report per client.

    Each report has ``status`` ``current``, ``upgraded`` (``files``, ``backup`` for
    ``restore-clients``), ``skipped`` (a layer that has to stay as it is; ``reason``) or
    ``failed`` (``reason``). A client that changes a file while it is upgraded is prepared again.
    ``lock`` is held while files are written; a BlockingIOError from it ends the run.
    """
    layers = find_layers(directory, home)
    report = {}
    for client in CLIENTS:
        for attempt in range(attempts):
            try:
                report[client] = _upgrade_client(directory, client, layers[client], home, lock)
                break
            except ConfigChanged as error:
                if attempt == attempts - 1:
                    report[client] = {"status": "failed", "files": [], "reason": str(error)}
                else:
                    time.sleep(0.2)
            except BlockingIOError:
                raise
            except Exception as error:  # one client's failure must not hold the others back
                logger.warning("Upgrading the %s registrations failed", client, exc_info=True)
                report[client] = {"status": "failed", "files": [], "reason": f"{type(error).__name__}: {error}"}
                break
    return report


def _saved(directory):
    try:
        saved = read_json(Path(directory) / STATE_FILE, {})
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def state(directory):
    """The saved switch and the last run's report."""
    saved = _saved(directory)
    return {**{key: value for key, value in saved.items() if key != "auto"}, "auto": saved.get("auto", True) is not False}


def duplicate_warnings(directory, home=None):
    """Warnings about the setup, for `registrations status` and the web UI: a Claude Code profile
    with both the agents-core plugin's server and another Agents-Core entry (#367). Each profile
    is checked; never raises."""
    from src.claude_plugin import duplicate_warning
    try:
        roots = workspace_roots(directory)
        profiles = claude_profiles(directory, home)
    except Exception:  # noqa: BLE001 - a check never fails what it is part of
        logger.warning("The Claude Code profiles were not checked", exc_info=True)
        return []
    warnings = []
    for path in profiles.values():
        warning = duplicate_warning(config_path=path, home=home, workspaces=roots)
        if warning:
            warnings.append(warning)
    return warnings


@contextmanager
def _state_lock(directory):
    with file_lock(Path(directory) / "registrations.lock"):
        yield


def set_auto(directory, enabled):
    with _state_lock(directory):
        write_json(Path(directory) / STATE_FILE, {**_saved(directory), "auto": bool(enabled)})
    return state(directory)


@contextmanager
def _control(directory):
    """The controller's lock while files are written; maintenance counts as busy."""
    from .bootstrap import assert_service_safe
    with file_lock(Path(directory) / "control.lock", blocking=False):
        try:
            assert_service_safe(directory)
        except RuntimeError as error:
            raise BlockingIOError(str(error)) from None
        yield


def run(directory, home=None, *, force=False, force_claude_code=False):
    """Upgrade and save the report, unless the user turned it off.

    ``force_claude_code`` (``--force-claude-code-mcp``) keeps every Claude Code profile's entry
    current beside the agents-core plugin, in this run and in the later ones (#367). Files are
    written under the controller's lock; raises BlockingIOError while the controller holds it (an
    update's probation) or the service is in maintenance.
    """
    directory = Path(directory)
    if force_claude_code:
        remember_force(directory, claude_profiles(directory, home))
    if not force and not state(directory)["auto"]:
        return state(directory)
    report = upgrade(directory, home, lock=lambda: _control(directory))
    with _state_lock(directory):
        write_json(directory / STATE_FILE, {
            **_saved(directory), "clients": report,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
    return state(directory)


def keep_current(directory, *, sleep=time.sleep):
    """At service start: `run`, waiting while the controller holds its lock. Never raises."""
    for _ in range(LOCK_RETRY_LIMIT):
        try:
            result = run(directory)
        except BlockingIOError:
            sleep(LOCK_RETRY_SECONDS)
            continue
        except Exception:
            logger.warning("Client registrations were not brought up to date", exc_info=True)
            return None
        changed = {client: item for client, item in result.get("clients", {}).items() if item["status"] != "current"}
        if changed:
            logger.info("Client registrations: %s", changed)
        return result
    logger.warning("Client registrations were not brought up to date: control.lock stayed busy")
    return None
