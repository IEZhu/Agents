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
from dataclasses import dataclass
import datetime
import json
import logging
from pathlib import Path
import time
import tomllib

from src.client_paths import absolute_path, client_config_path
from src.file_lock import file_lock
from .audit import registered_configs
from .clients import SERVER, ClientMigration, ConfigChanged
from .state import read_json, write_json

logger = logging.getLogger(__name__)

STATE_FILE = "registrations.json"
CLIENTS = ("claude", "codex", "cursor")
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
    roots = set()
    for saved in read_json(directory / "workspaces.json", {}).values():
        roots.add(Path(saved))
    found = {client: [] for client in CLIENTS}
    seen = set()

    def add(client, path, workspace=None):
        path = Path(path)
        try:
            key = path.resolve()
        except (OSError, RuntimeError):
            return
        if key in seen or not path.is_file():
            return
        seen.add(key)
        found[client].append(Layer(client, path, workspace))

    for client in CLIENTS:
        add(client, client_config_path(client, home=home, environ={}))
    for record in records:
        if record["client"] not in CLIENTS:
            continue
        if record.get("workspace") is None:
            add(record["client"], record["path"])
        elif record["client"] in LAYERED:
            roots.add(Path(record["workspace"]))
    for root in sorted(roots):
        if root.is_dir():
            add("codex", root / ".codex/config.toml", root)
            add("cursor", root / ".cursor/mcp.json", root)
    return found


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
    return servers.get(SERVER)


def managed(migration, entry):
    """Whether this installation wrote the entry: its endpoint with only the headers it sets and its
    token, the header helper it used for tracked Codex files, its bridge, or its stdio server."""
    if not isinstance(entry, dict):
        return False
    installation = Path(migration.config["installation"])
    if "url" in entry:
        if entry["url"] != migration.url:
            return False
        headers = entry.get("http_headers", entry.get("headers", {}))
        helper = entry.get("http_headers_helper")
        if helper is not None:
            return isinstance(helper, str) and str(installation / "bridge/headers.mjs") in helper and not headers
        return (isinstance(headers, dict) and set(headers) <= MANAGED_HEADERS
                and headers.get("Authorization") == "Bearer " + migration.token)
    args = entry.get("args")
    if not isinstance(entry.get("command"), str) or not isinstance(args, list) or not args:
        return False
    first = Path(str(args[0]))
    if first == installation / "bridge/stdio.mjs":
        return len(args) == 2 and Path(str(args[1])).parent == migration.directory / "bridges"
    return first == installation / "src/server.py"


def _upgrade_client(directory, client, layers, home):
    migration = ClientMigration(directory)
    changes, files = [], []
    for layer in layers:
        try:
            current = read_entry(client, layer.path.read_bytes().decode("utf-8"))
        except (OSError, ValueError, UnicodeError) as error:
            return {"status": "failed", "files": [], "reason": f"{layer.path} is unreadable: {error}"}
        if current is None:
            continue  # never add a registration
        if not managed(migration, current):
            return {"status": "skipped", "files": [],
                    "reason": f"{layer.path}: its {SERVER} entry was not written by this installation"}
        try:
            change = migration.prepare(client, layer.workspace, home=home, config_path=layer.path)
        except (OSError, ValueError) as error:
            return {"status": "failed", "files": [], "reason": f"{layer.path}: {error}"}
        if read_entry(client, change[1]) == current:
            continue
        changes.append(change)
        files.append(str(layer.path))
    if not changes:
        return {"status": "current", "files": []}
    backup = migration.apply(changes)
    return {"status": "upgraded", "files": files, "backup": str(backup)}


def upgrade(directory, home=None, *, attempts=ATTEMPTS):
    """Bring every client's registrations to the current form; a report per client.

    Each report has ``status`` ``current``, ``upgraded`` (``files``, ``backup`` for
    ``restore-clients``), ``skipped`` (an entry this installation did not write; ``reason``) or
    ``failed`` (``reason``). A client that changes a file while it is upgraded is prepared again.
    """
    layers = find_layers(directory, home)
    report = {}
    for client in CLIENTS:
        for attempt in range(attempts):
            try:
                report[client] = _upgrade_client(directory, client, layers[client], home)
                break
            except ConfigChanged as error:
                if attempt == attempts - 1:
                    report[client] = {"status": "failed", "files": [], "reason": str(error)}
                else:
                    time.sleep(0.2)
    return report


def state(directory):
    """The saved switch and the last run's report."""
    saved = read_json(Path(directory) / STATE_FILE, {})
    return {"auto": saved.get("auto", True) is not False, **{k: v for k, v in saved.items() if k != "auto"}}


def set_auto(directory, enabled):
    saved = read_json(Path(directory) / STATE_FILE, {})
    write_json(Path(directory) / STATE_FILE, {**saved, "auto": bool(enabled)})
    return state(directory)


def run(directory, home=None, *, force=False):
    """Upgrade under the controller's lock and save the report, unless the user turned it off.

    Raises BlockingIOError while the controller holds control.lock (an update's probation).
    """
    from .bootstrap import assert_service_safe
    directory = Path(directory)
    if not force and not state(directory)["auto"]:
        return state(directory)
    with file_lock(directory / "control.lock", blocking=False):
        assert_service_safe(directory)
        report = upgrade(directory, home)
        saved = read_json(directory / STATE_FILE, {})
        write_json(directory / STATE_FILE, {
            **saved, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "clients": report})
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
        changed = [client for client, item in result.get("clients", {}).items() if item["status"] != "current"]
        if changed:
            logger.info("Client registrations: %s", {client: result["clients"][client] for client in changed})
        return result
    logger.warning("Client registrations were not brought up to date: control.lock stayed busy")
    return None
