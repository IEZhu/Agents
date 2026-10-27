"""HTTP MCP credentials in a native OS store, without a file fallback.

Existing native stores accept printable tokens of at most 1024 bytes. A compact
JSON header map is base64 encoded to satisfy that API; oversize maps fail before
the store is written. No credential or encoded credential is written to disk.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from src.file_lock import file_lock
from src.mcp_registry import RegistryError, _safe_path, validate_alias, validate_header
from src.user_sync.github import FileStore, default_store

MAX_ENCODED_BYTES = 1024


class SecretsError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Secrets:
    """``store_factory(name)`` may inject a fake store for isolated tests."""

    def __init__(self, state_dir: str | Path, store_factory=None):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self._store_factory = store_factory

    def _store(self, alias):
        try:
            validate_alias(alias)
        except RegistryError as error:
            raise SecretsError(error.code, error.message) from None
        installation = hashlib.sha256(str(self.state_dir).encode("utf-8")).hexdigest()[:24]
        name = f"agents-core-mcp-{installation}-{alias}"
        try:
            store = self._store_factory(name) if self._store_factory else default_store(self.state_dir, name)
        except Exception:
            raise SecretsError("no_secret_store", "The native OS secret store is unavailable; unlock or install an OS secret store.") from None
        if isinstance(store, FileStore) or getattr(store, "backend", None) == "file":
            raise SecretsError("no_secret_store", "MCP credentials require a native OS secret store; file storage is disabled.")
        return store

    @staticmethod
    def _validate_name(name):
        try:
            return validate_header(name)
        except RegistryError as error:
            raise SecretsError(error.code, error.message) from None

    @staticmethod
    def _validate_value(value):
        if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise SecretsError("invalid", "Secret values must be nonempty printable ASCII without control characters.")

    def _load(self, store) -> dict[str, str]:
        try:
            encoded = store.load()
        except Exception:
            raise SecretsError("storage", "The native OS secret store could not read MCP credentials.") from None
        if encoded is None:
            return {}
        try:
            if not isinstance(encoded, str) or len(encoded) > MAX_ENCODED_BYTES:
                raise ValueError
            data = json.loads(base64.b64decode(encoded.encode("ascii"), validate=True))
            if not isinstance(data, dict) or len(data) > 32:
                raise ValueError
            names = set()
            for name, value in data.items():
                self._validate_name(name)
                self._validate_value(value)
                if name.lower() in names:
                    raise ValueError
                names.add(name.lower())
            return data
        except (ValueError, TypeError, UnicodeError):
            raise SecretsError("storage", "The native OS secret store holds an invalid MCP credential record.") from None

    def load(self, alias: str) -> dict[str, str]:
        return self._load(self._store(alias))

    def set(self, alias: str, name: str, value: str) -> dict:
        name = self._validate_name(name)
        self._validate_value(value)
        store = self._store(alias)
        with file_lock(_safe_path(self.state_dir, "mcp/.secrets.lock")):
            values = {key: item for key, item in self._load(store).items() if key.lower() != name.lower()}
            values[name] = value
            if len(values) > 32:
                raise SecretsError("invalid", "At most 32 secret headers can be stored per server.")
            encoded = base64.b64encode(json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).decode("ascii")
            if len(encoded) > MAX_ENCODED_BYTES:
                raise SecretsError("invalid", "The combined MCP credential headers exceed this native-store adapter's 1024-byte encoded limit.")
            try:
                store.save(encoded)
                if self._load(store) != values:
                    raise SecretsError("storage", "The native OS secret store did not keep MCP credentials.")
            except Exception:
                raise SecretsError("storage", "The native OS secret store could not save MCP credentials.") from None
        return self._status(store, values)

    @staticmethod
    def _status(store, values):
        return {"backend": store.backend, "names": sorted(values), "configured": {name: True for name in sorted(values)}}

    def status(self, alias: str) -> dict:
        try:
            store = self._store(alias)
            return self._status(store, self._load(store))
        except SecretsError as error:
            return {"backend": None, "names": [], "configured": {}, "error": error.code}
