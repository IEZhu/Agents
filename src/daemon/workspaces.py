"""Explicit immutable request identity; HTTP never falls back to cwd or env."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections import OrderedDict
import os
import threading
import uuid

from src.file_lock import file_lock
from .state import private_dir, read_json, write_json, state_dir


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

    def require_root(self):
        if self.error or self.root is None:
            raise WorkspaceError(self.error or "workspace_required")
        for relative in ("history.md", "history", "CLAUDE.md", "data/memory"):
            if not (self.root / relative).resolve().is_relative_to(self.root):
                raise WorkspaceError("workspace_invalid: memory path escapes workspace")
        return self.root

    def target(self, requested=None):
        root = self.require_root()
        target = Path(requested) if requested is not None else root
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
        if not target.is_relative_to(root) or not target.is_dir():
            raise WorkspaceError("repo_path must be an existing directory within workspace")
        return target


def client_context(ctx=None):
    if ctx is not None:
        try:
            request = ctx.request_context.request
        except (AttributeError, ValueError):
            request = None
        if request is not None:
            context = getattr(request.state, "client_context", None)
            if context is None:
                raise WorkspaceError("workspace_required")
            return context
    if os.environ.get("AGENTS_TRANSPORT") == "http":
        # Prompts/tools must forward their MCP context explicitly.
        raise WorkspaceError("workspace_required")
    from src.engine.config import get_client_repo_root
    return ClientContext(str(uuid.uuid4()), "stdio", root=Path(get_client_repo_root()).resolve())


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
