"""Portable HTTP MCP definitions and machine-local, exact-revision consent.

Definitions live in ``<library>/mcp/servers``. Enabled apps and confirmations
live only in ``<state>/mcp/registry.json``. Reading never enables a definition.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import stat
from urllib.parse import parse_qsl, unquote, urlsplit

from src import user_library
from src.file_lock import file_lock
from src.library_history import HistoryError, LibraryHistory
from src.user_sync.scope import SECRET_PATTERNS

APPS = ("claude-code", "codex", "cursor", "claude-desktop", "antigravity", "stdio")
MAX_BYTES = 64 * 1024
_ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,11}")
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_TOOL = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_REVISION = re.compile(r"[0-9a-f]{64}")
_CREDENTIAL = re.compile(r"authorization|authentication|cookie|token|secret|password|passwd|(?:^|[-_])(?:api[-_]?)?key(?:$|[-_])", re.I)
_HOP_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate"}


class RegistryError(ValueError):
    """A safe error with a stable code; never includes definition values."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def validate_alias(alias: str) -> str:
    if not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or "__" in alias:
        raise RegistryError("invalid", "Alias must be 1-12 lowercase letters, digits, hyphens or underscores, without '__'.")
    return alias


def validate_header(name: str) -> str:
    if not isinstance(name, str) or len(name) > 128 or not _HEADER.fullmatch(name):
        raise RegistryError("invalid", "Secret and header names must be valid HTTP header names.")
    if name.lower() in _HOP_HEADERS or name.lower().startswith("x-agents-"):
        raise RegistryError("invalid", "Routing and Agents-Core headers cannot be configured upstream.")
    return name


def _invalid(message: str):
    raise RegistryError("invalid", message)


def _json(data) -> bytes:
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        _invalid("Definition must be a JSON object with finite values.")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid("Duplicate JSON keys are not allowed.")
        result[key] = value
    return result


def _object(value, label):
    if not isinstance(value, dict):
        _invalid(f"{label} must be an object.")
    return value


def _text(value, label, maximum=4096):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in "\n\t" for c in value):
        _invalid(f"{label} must be printable text of at most {maximum} characters.")
    return value


def _tools(value, label):
    if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(v, str) or not _TOOL.fullmatch(v) for v in value):
        _invalid(f"{label} must be a list of valid tool names.")
    return list(dict.fromkeys(value))


def _no_inline_credentials(value):
    """Check extension fields too; secret declarations name headers without values."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _CREDENTIAL.search(key) and item not in (None, "", [], {}):
                _invalid("Put credentials in the OS secret store, not in a definition.")
            _no_inline_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _no_inline_credentials(item)


def validate_definition(definition: dict, *, stored=False) -> dict:
    """Validate and normalize a definition; preserve non-secret extension fields."""
    _object(definition, "Definition")
    raw = _json(definition)
    if len(raw) > MAX_BYTES:
        _invalid("Definition exceeds 64 KiB.")
    data = json.loads(raw)
    claimed = data.pop("revision", None)
    alias = validate_alias(data.get("alias"))
    if type(data.get("format", 1)) is not int or data.get("format", 1) != 1:
        _invalid("Unsupported MCP definition format.")
    data["format"] = 1
    for key, default in (("title", alias), ("description", ""), ("instructions", "")):
        data[key] = _text(data.get(key, default), key)
    source = _object(data.setdefault("source", {"kind": "manual"}), "source")
    if source.get("kind") not in ("manual", "catalog", "import"):
        _invalid("Unsupported definition source.")
    transport = _object(data.get("transport"), "transport")
    if transport.get("type") != "http" or set(transport) - {"type", "url", "headers"}:
        _invalid("This release supports HTTP transport with url and headers only.")
    url = _text(transport.get("url"), "transport.url", 8192)
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in ("http", "https") and parsed.hostname and parsed.port != 0
    except ValueError:
        valid = False
    if not valid or parsed.username is not None or parsed.password is not None or parsed.fragment or any(c.isspace() for c in url):
        _invalid("Use an HTTP or HTTPS URL without user information, whitespace or fragments.")
    if any(_CREDENTIAL.search(name) for name, _ in parse_qsl(parsed.query)) or re.search(r"(?i)(?:bearer|basic)\s", unquote(url)):
        _invalid("URL credentials must be supplied through the OS secret store.")
    headers = _object(transport.setdefault("headers", {}), "transport.headers")
    seen = set()
    for name, value in headers.items():
        validate_header(name)
        if name.lower() in seen or _CREDENTIAL.search(name):
            _invalid("Credential headers must be supplied through the OS secret store; duplicate header names are not allowed.")
        seen.add(name.lower())
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            _invalid("Header values must be printable ASCII without control characters.")
    auth = _object(data.setdefault("auth", {"type": "none"}), "auth")
    if auth.get("type") not in ("none", "bearer", "header") or set(auth) - {"type", "name"}:
        _invalid("Authentication must be none, bearer or header; credentials belong in secrets.")
    declarations = data.setdefault("secrets", [])
    if not isinstance(declarations, list) or len(declarations) > 32:
        _invalid("secrets must contain at most 32 header declarations.")
    names = set()
    for declaration in declarations:
        _object(declaration, "secret declaration")
        if set(declaration) - {"name", "target", "required", "description"} or declaration.get("target") != "header":
            _invalid("Secrets declare header names, target=header, required and optional description only.")
        name = validate_header(declaration.get("name"))
        if name.lower() in names or name.lower() in seen:
            _invalid("A header must be declared once and cannot also have an inline value.")
        names.add(name.lower())
        if type(declaration.get("required", True)) is not bool:
            _invalid("Secret required must be a boolean.")
        declaration.setdefault("required", True)
        if "description" in declaration:
            _text(declaration["description"], "secret description")
    if parsed.scheme == "http" and declarations:
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if not loopback:
            _invalid("Credentialed servers require HTTPS unless they listen on loopback.")
    required = {entry["name"].lower() for entry in declarations if entry["required"]}
    if auth["type"] == "bearer" and "authorization" not in required:
        _invalid("Bearer authentication requires a required Authorization header secret containing the complete Bearer header.")
    if auth["type"] == "header" and validate_header(auth.get("name")).lower() not in required:
        _invalid("Header authentication requires its named header as a required secret.")
    tools = _object(data.setdefault("tools", {}), "tools")
    expose = tools.setdefault("expose", "all")
    if expose != "all":
        tools["expose"] = _tools(expose, "tools.expose")
    for key in ("deny", "always_load"):
        tools[key] = _tools(tools.get(key, []), f"tools.{key}")
    limits = _object(data.setdefault("limits", {}), "limits")
    for key, default, maximum in (("call_timeout_seconds", 60, 3600), ("idle_seconds", 600, 86400)):
        value = limits.setdefault(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            _invalid(f"{key} must be an integer from 1 to {maximum}.")
    # Declarations carry names only and are validated above; scan all other fields.
    _no_inline_credentials({key: value for key, value in data.items() if key != "secrets"})
    canonical = _json(data)
    if any(pattern.search(canonical) for _, pattern in SECRET_PATTERNS):
        _invalid("Put credentials in the OS secret store, not in a definition.")
    revision = hashlib.sha256(canonical).hexdigest()
    if stored and claimed != revision:
        _invalid("Definition revision does not match its content; save or confirm the current definition.")
    return {**data, "revision": revision}


def _safe_path(root: Path, relative: str) -> Path:
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise RegistryError("storage", "MCP storage paths must not be symlinks.")
    if not path.resolve().is_relative_to(root):
        raise RegistryError("storage", "MCP storage path escapes its root.")
    return path


def _read_json(path: Path) -> dict:
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        _invalid("MCP records must be ordinary files.")
    if metadata.st_size > MAX_BYTES:
        _invalid("MCP record exceeds 64 KiB.")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            _invalid("MCP record exceeds 64 KiB.")
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, ValueError, RecursionError):
        _invalid("MCP record is malformed JSON.")


class Registry:
    def __init__(self, library_root: str | Path, state_dir: str | Path):
        self.library_root = Path(library_root).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()

    def get(self, alias: str) -> dict:
        alias = validate_alias(alias)
        try:
            data = validate_definition(_read_json(_safe_path(self.library_root, f"mcp/servers/{alias}.json")), stored=True)
        except FileNotFoundError:
            raise RegistryError("not_found", "MCP server does not exist.") from None
        except OSError:
            raise RegistryError("storage", "MCP definition cannot be read.") from None
        if data["alias"] != alias:
            _invalid("Definition alias must match its filename.")
        return data

    def _local(self) -> dict:
        try:
            data = _read_json(_safe_path(self.state_dir, "mcp/registry.json"))
        except FileNotFoundError:
            return {}
        except OSError:
            raise RegistryError("storage", "Machine-local MCP state cannot be read.") from None
        if not isinstance(data, dict) or type(data.get("format")) is not int or data.get("format") != 1 or not isinstance(data.get("servers"), dict):
            _invalid("Machine-local MCP state is invalid; no server can be enabled.")
        result = {}
        for alias, record in data["servers"].items():
            try:
                validate_alias(alias)
                if not isinstance(record, dict) or type(record.get("enabled")) is not bool or not isinstance(record.get("apps"), list) or any(app not in APPS for app in record["apps"]) or not isinstance(record.get("revision"), str) or not _REVISION.fullmatch(record["revision"]):
                    continue
                result[alias] = record
            except RegistryError:
                continue
        return result

    def _write_local(self, state: dict):
        user_library.atomic_write(_safe_path(self.state_dir, "mcp/registry.json"), _json({"format": 1, "servers": state}) + b"\n")

    @contextmanager
    def _library_lock(self):
        history = LibraryHistory(self.library_root)
        try:
            history.validate_repository()
            with file_lock(self.library_root / ".lock"):
                history.validate_repository()
                yield
        except HistoryError:
            raise RegistryError("storage", "The library is not a writable Agents-Core library.") from None

    def save(self, definition: dict) -> dict:
        data = validate_definition(definition)
        relative = f"mcp/servers/{data['alias']}.json"
        changed = []
        try:
            with self._library_lock():
                path = _safe_path(self.library_root, relative)
                payload = _json(data) + b"\n"
                if not path.exists() or path.read_bytes() != payload:
                    user_library.atomic_write(path, payload)
                    changed.append(relative)
                changed += user_library.ensure_root_files(self.library_root)
                with file_lock(_safe_path(self.state_dir, "mcp/.lock")):
                    state = self._local()
                    previous = state.get(data["alias"], {"enabled": False, "apps": []})
                    state[data["alias"]] = {**previous, "revision": data["revision"]}
                    self._write_local(state)
        except OSError:
            raise RegistryError("storage", "MCP definition or machine-local confirmation could not be saved.") from None
        finally:
            user_library.notify(self.library_root, changed)
        return data

    def list(self) -> list[dict]:
        local = self._local()
        directory = _safe_path(self.library_root, "mcp/servers")
        records = []
        for path in sorted(directory.glob("*.json")):
            alias = path.stem
            try:
                data = self.get(alias)
                state = local.get(alias, {})
                enabled = state.get("enabled", False)
                confirmed = state.get("revision") == data["revision"]
                records.append({**data, "enabled": enabled, "apps": state.get("apps", []), "confirmed": confirmed,
                                "status": "disabled" if not enabled else "ready" if confirmed else "unconfirmed"})
            except RegistryError as error:
                records.append({"alias": alias, "status": "invalid", "error": error.message, "enabled": False, "apps": [], "confirmed": False})
        return records

    def enable(self, alias: str, apps: list[str], revision: str) -> dict:
        validate_alias(alias)
        if not isinstance(apps, list) or any(not isinstance(app, str) or app not in APPS for app in apps):
            _invalid("apps must be a list of supported local MCP clients.")
        try:
            with self._library_lock():
                data = self.get(alias)
                if revision != data["revision"]:
                    raise RegistryError("stale_revision", "The definition changed; review and confirm its current revision.")
                with file_lock(_safe_path(self.state_dir, "mcp/.lock")):
                    state = self._local()
                    state[alias] = {"enabled": bool(apps), "apps": sorted(set(apps)), "revision": revision}
                    self._write_local(state)
        except OSError:
            raise RegistryError("storage", "Machine-local MCP consent could not be saved.") from None
        return next(record for record in self.list() if record["alias"] == alias)

    def resolved(self, app: str) -> list[dict]:
        if app not in APPS:
            return []
        # Return the exact snapshot whose revision was checked. Re-reading here
        # could return a newly synced, unconfirmed definition after that check.
        local_fields = {"enabled", "apps", "confirmed", "status"}
        return [{key: value for key, value in record.items() if key not in local_fields}
                for record in self.list() if record["status"] == "ready" and app in record["apps"]]
