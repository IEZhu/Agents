"""Authenticate Antigravity's native workspace hint for one standalone request.

The hook's signature is transport metadata, consumed before tool validation.
Only an installation-bound private key can turn a hint into request context.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat

from src import runtime_paths


AUTH_CONFIG_ENV = "AGENTS_WORKSPACE_AUTH_CONFIG"
MAX_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True)
class RequestWorkspace:
    root: Path | None = None
    error: str | None = None


request_workspace: ContextVar[RequestWorkspace | None] = ContextVar("stdio_request_workspace", default=None)


def load_auth_config(path, *, installation=None) -> dict:
    """Read a bounded, owner-private signing file for exactly this installation.

    Errors deliberately contain no input values or authentication material.
    The installer supplies its target installation before that runtime starts.
    """
    if not isinstance(path, (str, Path)) or not str(path).strip() or not Path(path).is_absolute():
        raise ValueError("Absolute workspace authentication config required")
    path = Path(path)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("Regular workspace authentication config required")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Regular workspace authentication config required")
        if os.name == "nt":
            from src.daemon import acl
            if not acl.owned_by_user_or_admins(path) or not acl.is_private(path):
                raise ValueError("Owner-private workspace authentication config required")
        elif info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Owner-private workspace authentication config required")
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("Workspace authentication config too large")
    try:
        config = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid workspace authentication config") from None
    if not isinstance(config, dict) or config.get("transport") != "stdio":
        raise ValueError("Standalone workspace authentication config required")
    root = config.get("installation")
    expected = Path(installation if installation is not None else runtime_paths.installation_root()).resolve()
    if not isinstance(root, str) or not root or not Path(root).is_absolute() or Path(root).resolve() != expected:
        raise ValueError("Workspace authentication installation mismatch")
    authorization = config.get("headers", {}).get("Authorization") if isinstance(config.get("headers"), dict) else None
    if not isinstance(authorization, str) or not re.fullmatch(r"Bearer [!-~]+", authorization):
        raise ValueError("Workspace authentication token required")
    return config


def _authenticated_workspace(arguments, signature) -> RequestWorkspace:
    invalid = RequestWorkspace(error="workspace_invalid: native workspace proof is invalid")
    workspace = arguments.get("workspace")
    if (not isinstance(signature, str) or re.fullmatch(r"[0-9a-f]{64}", signature) is None
            or not isinstance(workspace, str) or not workspace.strip() or "\x00" in workspace):
        return invalid
    try:
        config = load_auth_config(os.environ.get(AUTH_CONFIG_ENV))
        token = config["headers"]["Authorization"][len("Bearer "):]
        expected = hmac.new(token.encode("utf-8"), ("workspace:" + workspace).encode("utf-8"), hashlib.sha256).hexdigest()
    except (OSError, ValueError, TypeError, RuntimeError, UnicodeError):
        return invalid
    if not hmac.compare_digest(expected, signature):
        return invalid
    # A valid proof is private transport metadata even when the named directory
    # is unsafe; never forward the authenticated hint into a tool's arguments.
    arguments.pop("workspace", None)
    from src.engine import config as engine_config
    try:
        root = engine_config.client_root_from_directory(workspace, launch_directory=False)
    except engine_config.ClientRootError as error:
        return RequestWorkspace(error=f"{error.code}: native workspace directory is unavailable or unsafe")
    except (OSError, ValueError, RuntimeError):
        return RequestWorkspace(error="workspace_invalid: native workspace directory is unavailable or unsafe")
    return RequestWorkspace(root=Path(root))


@contextmanager
def bind(arguments, *, enabled=True):
    """Yield sanitized tool arguments with isolated, finally-reset request context."""
    sanitized = dict(arguments) if isinstance(arguments, dict) else arguments
    binding = None
    if isinstance(sanitized, dict):
        signature = sanitized.pop("workspace_signature", None)
        if enabled and signature is not None:
            binding = _authenticated_workspace(sanitized, signature)
    reset = request_workspace.set(binding)
    try:
        yield sanitized
    finally:
        request_workspace.reset(reset)
