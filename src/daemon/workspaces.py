"""Explicit immutable request identity; HTTP never falls back to cwd or env."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from collections import OrderedDict
import asyncio
import logging
import os
import threading
import uuid

from src.file_lock import file_lock
from .state import private_dir, read_json, write_json, state_dir

logger = logging.getLogger(__name__)

# How long a tool waits for the client's roots/list answer; Claude Code and the
# Claude desktop app answer at once.
MCP_ROOTS_TIMEOUT_SECONDS = 5.0

WORKSPACE_HINT = (
    "This server process can serve several sessions, as the one the Claude desktop app "
    "starts for its Code sessions does: pass workspace, the absolute path of your "
    "working directory."
)
NO_ROOTS_HINT = (
    "This client declares no MCP roots, so a workspace argument cannot be checked; set "
    "AGENTS_CLIENT_REPO_ROOT on a per-project registration."
)


class WorkspaceError(ValueError):
    pass


class WorkspaceRegistry:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "workspaces.json"

    def register(self, root):
        root = Path(root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise WorkspaceError("workspace_invalid")
        private_dir(self.directory)
        with file_lock(self.directory / "workspaces.lock"):
            records = read_json(self.path, {})
            for identity, saved in records.items():
                if saved == str(root):
                    self.resolve(identity)
                    return identity
            identity = str(uuid.uuid4())
            if identity in records:
                raise WorkspaceError("workspace_invalid")
            records[identity] = str(root)
            write_json(self.path, records)
            return identity

    def resolve(self, identity):
        if not identity:
            raise WorkspaceError("workspace_required")
        try:
            if str(uuid.UUID(identity)) != identity:
                raise ValueError()
            records = read_json(self.path, {})
            saved = records[identity]
            root = Path(saved)
            if not root.is_absolute() or str(root.resolve(strict=True)) != saved or not root.is_dir():
                raise ValueError()
            if list(records.values()).count(saved) != 1:
                raise ValueError()
            return root
        except (ValueError, KeyError, OSError, TypeError):
            raise WorkspaceError("workspace_invalid") from None


@dataclass(frozen=True)
class ClientContext:
    request_id: str
    transport: str
    workspace_id: str | None = None
    root: Path | None = None
    error: str | None = None
    source: str | None = None
    client: str | None = None  # the app named by X-Agents-Client (usage.py), over HTTP

    def workspace_root(self):
        if self.error or self.root is None:
            raise WorkspaceError(self.error or "workspace_required")
        return self.root

    def require_root(self):
        self.workspace_root()
        for relative in ("history.md", "history", "CLAUDE.md", "data/memory"):
            if not (self.root / relative).resolve().is_relative_to(self.root):
                raise WorkspaceError("workspace_invalid: memory path escapes workspace")
        return self.root

    def target(self, requested=None):
        self.require_root()
        return self.workspace_target(requested)

    def workspace_target(self, requested=None):
        """Resolve a target without imposing memory-file write constraints."""
        root = self.workspace_root()
        try:
            target = Path(requested) if requested is not None else root
            if not target.is_absolute():
                target = root / target
            target = target.resolve()
        except (OSError, ValueError, RuntimeError):
            raise WorkspaceError("repo_path must be an existing directory within workspace") from None
        if not target.is_relative_to(root) or not target.is_dir():
            raise WorkspaceError("repo_path must be an existing directory within workspace")
        return target


def _http_request(ctx):
    """The HTTP request behind an MCP context, or None (stdio, or no context)."""
    try:
        return ctx.request_context.request
    except (AttributeError, ValueError):
        return None


def client_context(ctx=None, *, allow_install_fallback=True):
    request = _http_request(ctx)
    if request is not None:
        context = getattr(request.state, "client_context", None)
        if context is None:
            raise WorkspaceError("workspace_required")
        return context
    if os.environ.get("AGENTS_TRANSPORT") == "http":
        # Prompts/tools must forward their MCP context explicitly.
        raise WorkspaceError("workspace_required")
    from src.engine.config import ClientRootError, get_client_repo_root_info
    try:
        root, source = get_client_repo_root_info(allow_install_fallback=allow_install_fallback)
    except ClientRootError as error:
        # Same contract as an HTTP request without a workspace: memory tools
        # and repository flows fail, routing continues.
        return ClientContext(str(uuid.uuid4()), "stdio", error=f"{error.code}: {error}")
    return ClientContext(str(uuid.uuid4()), "stdio", root=Path(root).resolve(), source=source)


async def resolve_client_context(ctx=None, workspace=None, *, allow_install_fallback=True):
    """`client_context()` for async tools, honouring a stdio call's `workspace`.

    One stdio process can serve several sessions: the Claude desktop app starts
    a single one for all its Code sessions, in C:\\Windows\\System32 and without
    CLAUDE_PROJECT_DIR. A tool call may therefore name its working directory.
    When the client declares MCP roots, that directory is checked against them
    on every call (`config.client_root_from_workspace`) and never pinned for
    the process. HTTP keeps its registered workspace, AGENTS_CLIENT_REPO_ROOT
    stays authoritative, and a client without roots keeps the process root.
    """
    workspace = workspace.strip() if isinstance(workspace, str) and workspace.strip() else None
    stdio = _http_request(ctx) is None and os.environ.get("AGENTS_TRANSPORT") != "http"
    # An override, even a refused one, decides alone; the argument would change nothing.
    session = _roots_session(ctx) if stdio and not os.environ.get("AGENTS_CLIENT_REPO_ROOT") else None
    if workspace is not None and session is not None:
        return await _workspace_context(session, workspace)
    context = client_context(ctx, allow_install_fallback=allow_install_fallback)
    if context.transport == "stdio" and context.error is not None and not os.environ.get("AGENTS_CLIENT_REPO_ROOT"):
        if session is not None:
            context = replace(context, error=f"{context.error} {WORKSPACE_HINT}")
        elif workspace is not None:
            context = replace(context, error=f"{context.error} {NO_ROOTS_HINT}")
    return context


async def _workspace_context(session, workspace):
    from src.engine import config
    request_id = str(uuid.uuid4())
    try:
        answer = await asyncio.wait_for(session.list_roots(), MCP_ROOTS_TIMEOUT_SECONDS)
        root = config.client_root_from_workspace(workspace, (str(item.uri) for item in answer.roots))
    except config.ClientRootError as error:
        return ClientContext(request_id, "stdio", error=f"{error.code}: {error}")
    except Exception as error:  # a failed or malformed answer from the client
        logger.warning("roots/list failed: %r", error)
        return ClientContext(request_id, "stdio", error=(
            f"workspace_invalid: could not read the client's MCP roots to check workspace {workspace!r}: {error!r}"))
    return ClientContext(request_id, "stdio", root=Path(root), source="workspace")


def _roots_session(ctx):
    """The MCP session of a request whose client declared the roots capability, else None."""
    from mcp.types import ClientCapabilities, RootsCapability
    try:
        session = ctx.session
        declared = session.check_client_capability(ClientCapabilities(roots=RootsCapability()))
    except (AttributeError, ValueError):  # no context, or one used outside a request
        return None
    return session if declared is True else None


def workspace_inputs(ctx=None):
    """What a stdio workspace is resolved from, reported with workspace errors; None over HTTP."""
    if _http_request(ctx) is not None or os.environ.get("AGENTS_TRANSPORT") == "http":
        return None
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = None
    try:
        client = ctx.session.client_params.clientInfo.name
    except (AttributeError, ValueError):
        client = None
    return {
        "cwd": cwd, "claude_project_dir": bool(os.environ.get("CLAUDE_PROJECT_DIR")),
        "agents_client_repo_root": bool(os.environ.get("AGENTS_CLIENT_REPO_ROOT")),
        "client": client, "roots": _roots_session(ctx) is not None,
    }


# How the Claude desktop app names its MCP client: `local-agent-mode-<server>` (#231).
DESKTOP_CLIENT_PREFIX = "local-agent-mode-"


def desktop_started_hint(inputs):
    """What a workspace error in a server the Claude desktop app started should add, or None (#231).

    The app starts its servers once, outside any project, and serves all its sessions with them, so
    "start the server in a project" does not apply there.
    """
    if not inputs or not str(inputs.get("client") or "").startswith(DESKTOP_CLIENT_PREFIX):
        return None
    from src.client_paths import DESKTOP_SERVER, client_config_path
    return (f"The Claude desktop app started this server from {client_config_path('desktop')}, outside any "
            "project, and serves all its sessions with it. A Code-tab session should use Claude Code's own "
            "Agents-Core: run setup (scripts/init_repo) or `python -m src.daemon migrate --clients desktop` "
            f"again, which names the app's entry {DESKTOP_SERVER} and denies it in Claude Code, then restart "
            "the app. Any other call needs workspace inside a folder the app shares as an MCP root.")


class HistoryStores:
    """Bounded LRU whose active entries cannot be evicted."""
    def __init__(self, capacity=8):
        self.capacity = capacity
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    @contextmanager
    def acquire(self, context):
        root = context.require_root()
        with self.lock:
            if root not in self.entries:
                while len(self.entries) >= self.capacity:
                    idle = next((key for key, (_, users) in self.entries.items() if not users), None)
                    if idle is None:
                        raise WorkspaceError("busy")
                    del self.entries[idle]
                from src.memory.history import HistoryStore
                import hashlib
                key = hashlib.sha256(str(root).encode()).hexdigest()
                base = (state_dir() / "history" if context.transport == "http"
                        else Path(os.environ.get("AGENTS_DERIVED_DIR", str(root / "data/memory"))))
                self.entries[root] = [HistoryStore(str(root / "history.md"), str(base / key)), 0]
            entry = self.entries[root]
            entry[1] += 1
            self.entries.move_to_end(root)
        try:
            yield entry[0]
        finally:
            with self.lock:
                entry[1] -= 1
