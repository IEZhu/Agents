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


class WorkspaceMismatch(WorkspaceError):
    """A call named a workspace the registered root does not cover (#576).

    The grant still decides the root: an argument never widens it. A tool that answers
    *about* a repository raises this instead of answering for the registered one, so the
    caller learns that its argument was not followed and how to reach the other project.
    """

    def __init__(self, registered, requested):
        self.registered, self.requested = str(registered), str(requested)
        super().__init__(
            f"workspace_mismatch: this session is registered for the workspace {self.registered}, "
            f"and {self.requested} is outside it. A workspace argument never widens that grant, so "
            f"nothing was answered for {self.requested}.")

    def details(self):
        """The fields a tool adds beside the message: both roots and what the user can do.

        A refusal that enforces a grant must not hand back the command that widens it. The
        remedy therefore names ``workspace register``, which only records a root
        (`control.py`), and not ``migrate``, whose ``--clients`` defaults to
        ``codex,claude,cursor`` and whose ``ClientMigration.apply`` rewrites those three
        configuration files: an unattended run acting on text in a tool result would
        reconfigure three AI clients and be granted, next session, the repository this very
        refusal denied. It is addressed to the user for the same reason, and the requested
        path is not interpolated into a command line either — it is a caller-supplied string,
        and ``requested_workspace`` beside it already reports it as data.
        """
        return {"code": "workspace_mismatch", "registered_workspace": self.registered,
                "requested_workspace": self.requested, "remedy": (
                    "Nothing a session can call widens this grant. The user registers the "
                    "requested workspace with `python -m src.daemon workspace register` and "
                    "reconnects MCP, or starts a session in that repository. Until then read "
                    "that repository's own CLAUDE.md or AGENTS.md instead.")}


class WorkspaceRegistry:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "workspaces.json"
        self.checkout_path = self.directory / "checkout-access.json"

    def register(self, root, *, physical_root=None):
        root = Path(root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise WorkspaceError("workspace_invalid")
        physical = Path(physical_root if physical_root is not None else root).expanduser().resolve(strict=True)
        if not physical.is_dir():
            raise WorkspaceError("workspace_invalid")
        private_dir(self.directory)
        with file_lock(self.directory / "workspaces.lock"):
            records = read_json(self.path, {})
            for identity, saved in records.items():
                if saved == str(root):
                    self.resolve(identity)
                    break
            else:
                identity = str(uuid.uuid4())
                if identity in records:
                    raise WorkspaceError("workspace_invalid")
                records[identity] = str(root)
                write_json(self.path, records)
            try:
                self._grant_checkout(identity, physical)
            except (ValueError, OSError) as error:
                # Optional checkout authorization must not disable ordinary memory.
                # Keep invalid grants untouched and require explicit valid grants
                # in cooperation_roots; never substitute the memory root on failure.
                logger.warning("Cooperation checkout grant unavailable: %s", type(error).__name__)
            return identity

    def _grant_checkout(self, identity, physical):
        """Called only under the registration lock, never from an MCP tool."""
        grants = read_json(self.checkout_path, {})
        if not isinstance(grants, dict):
            raise WorkspaceError("workspace_invalid")
        saved = grants.get(identity, [])
        current = [str(path) for path in self._existing_checkouts(saved)]
        if str(physical) not in current:
            if len(current) >= 50:
                raise WorkspaceError("workspace_invalid")
            current.append(str(physical))
        if current != saved:
            grants[identity] = current
            write_json(self.checkout_path, grants)

    @staticmethod
    def _existing_checkouts(saved):
        """Discard removed checkouts, but reject corrupt or retargeted grants."""
        try:
            if not isinstance(saved, list) or len(saved) > 50:
                raise ValueError()
            result = []
            for value in saved:
                if not isinstance(value, str):
                    raise ValueError()
                path = Path(value)
                if not path.is_absolute() or str(path.resolve()) != value:
                    raise ValueError()
                try:
                    resolved = path.resolve(strict=True)
                except FileNotFoundError:
                    continue
                if str(resolved) != value or not path.is_dir():
                    raise ValueError()
                result.append(path)
            return tuple(result)
        except (ValueError, TypeError, OSError, RuntimeError):
            raise WorkspaceError("workspace_invalid") from None

    def cooperation_roots(self, identity):
        """Owner-registered physical grants; memory root normalization is unchanged."""
        self.resolve(identity)
        try:
            grants = read_json(self.checkout_path, {})
            if not isinstance(grants, dict):
                raise ValueError()
            # Missing grants can also mean registration could not save them.
            # Owner re-registration upgrades older memory-only registrations.
            saved = grants.get(identity)
            if not isinstance(saved, list) or not 1 <= len(saved) <= 50:
                raise ValueError()
            return self._existing_checkouts(saved)
        except (ValueError, TypeError, OSError, RuntimeError):
            raise WorkspaceError("workspace_invalid") from None

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
    # The directory the client named when no workspace resolved (X-Agents-Claimed-Workspace, the
    # call's workspace, or a stdio server's start directory). Unverified: the fallback journal
    # records it with the turn and nothing is written there (#292).
    claimed: str | None = None
    # Explicit physical grants retained before history normalizes linked worktrees.
    # Cooperation resolves a checkout in them, and `_workspace_argument` accepts one as
    # covering a named workspace; `workspace_target` continues to enforce root alone.
    cooperation_roots: tuple[Path, ...] = ()
    # The `workspace` a call named that no authorized root of this session covers (#576).
    # Recorded, never followed: tools that answer about a repository refuse, history tools
    # note it. A linked worktree of the registered repository is covered by its grant.
    mismatch: str | None = None
    # The authorized root that covers the `workspace` this call named: an owner-registered
    # physical checkout, such as the worktree a session runs in and history normalizes to its
    # main checkout, or the memory root itself. Only a repository *report* follows it, so that
    # the description and hash belong to the tree the call named and not to another branch's
    # (#576 pre-mortem P-C2); every write and target still resolves inside `root`. Set
    # whenever a named workspace matched, including a match on the memory root, because that
    # is an explicit choice and must not fall into the fallback for a call that named nothing
    # (CR1 F1).
    covering_root: Path | None = None
    # The `workspace` a call named that is an authorized root under another spelling: the
    # same real directory, which `Path.resolve()` does not normalize the case of. Noted like
    # an ignored argument, never refused, so one respelling cannot cost a session its
    # repository tools (#576 pre-mortem P-C5).
    respelled: str | None = None

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

    def require_workspace_match(self):
        """Refuse to answer about a repository the call named but the grant does not cover (#576)."""
        if self.mismatch is not None:
            raise WorkspaceMismatch(self.workspace_root(), self.mismatch)

    def workspace_note(self):
        """For a call whose named workspace was not followed, what was used instead (#576).

        The history tools keep answering for the registered root; this sentence is how they say
        so. A respelling of an authorized root is noted the same way and refused nowhere, so
        every tool keeps answering for the one directory both spellings name. None when the
        argument matched, or no workspace was named.
        """
        named = self.mismatch if self.mismatch is not None else self.respelled
        if named is None:
            return None
        return (f"The workspace argument {named} was ignored: this session is registered for "
                f"{self.workspace_root()}, and that is the workspace used.")

    def report_root(self):
        """The checkout a repository report describes: the root that covers what was named.

        `workspace_root` is the memory root, which is the main checkout for a worktree session
        because history normalizes a linked worktree to it. A description of that root would
        be another branch's, so a report follows the authorized physical checkout the call
        named instead (#576 pre-mortem P-C2). Targets, writes and history are unchanged.

        Only a call that named no workspace falls back: it still has one checkout when the
        registration carries a single physical grant beside the memory root, which is the
        directory the registration was made from. Several grants name no one checkout, so the
        memory root stands. A call that named the memory root recorded it as `covering_root`
        and is answered about it, because naming a root is a choice and the fallback stands in
        for the absence of one (CR1 F1).
        """
        root = self.workspace_root()
        if self.covering_root is not None:
            return self.covering_root
        grants = [grant for grant in self.cooperation_roots if grant != root]
        return grants[0] if len(grants) == 1 else root

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
    native = _stdio_hook_context()
    if native is not None:
        return native
    from src.engine.config import ClientRootError, get_client_repo_root_info
    try:
        root, source = get_client_repo_root_info(allow_install_fallback=allow_install_fallback)
    except ClientRootError as error:
        # Same contract as an HTTP request without a workspace: memory tools
        # and repository flows fail, routing continues.
        return ClientContext(str(uuid.uuid4()), "stdio", error=f"{error.code}: {error}", claimed=_start_directory())
    return ClientContext(str(uuid.uuid4()), "stdio", root=Path(root).resolve(), source=source)


def _stdio_hook_context():
    """A verified native proof belongs to this call, never the server process."""
    if os.environ.get("AGENTS_CLIENT_REPO_ROOT"):
        return None
    from src.stdio_workspace import request_workspace
    native = request_workspace.get()
    if native is None:
        return None
    return ClientContext(str(uuid.uuid4()), "stdio", root=native.root, error=native.error, source="hook")


def _start_directory():
    try:
        return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    except OSError:
        return None


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
    native = _stdio_hook_context() if stdio else None
    if native is not None:
        return native
    # An override, even a refused one, decides alone; the argument would change nothing.
    session = _roots_session(ctx) if stdio and not os.environ.get("AGENTS_CLIENT_REPO_ROOT") else None
    if session is not None:
        context = await _workspace_context(session, workspace)
        return replace(context, claimed=workspace) if context.error else context
    context = client_context(ctx, allow_install_fallback=allow_install_fallback)
    if context.transport == "stdio" and context.error is not None and not os.environ.get("AGENTS_CLIENT_REPO_ROOT"):
        if session is not None:
            context = replace(context, error=f"{context.error} {WORKSPACE_HINT}")
        elif workspace is not None:
            context = replace(context, error=f"{context.error} {NO_ROOTS_HINT}")
    if (context.error is not None or context.root is None) and workspace is not None:
        context = replace(context, claimed=workspace)  # what the call names says more than the start directory
    if fields := _workspace_argument(context, workspace):
        context = replace(context, **fields)
    return context


def _workspace_argument(context, workspace):
    """How a named `workspace` stands to this session's authorized roots, as fields (#576).

    An authorized root is the registered memory root or one of the owner-registered physical
    checkouts beside it. Both are needed because history normalizes a linked worktree to its
    main checkout (`config._main_checkout`): the memory root of a worktree session is the main
    checkout, while the directory the session actually runs in is kept as a grant, which this
    same request already reads the project's instruction files from. A worktree session naming
    its own working directory, as the parameter asks it to, therefore names one repository and
    not another one; only a path outside every authorized root is a `mismatch`.

    The authorized root that covers the named path is reported as `covering_root`, which only
    a repository report follows, and a path that names an authorized root under another
    spelling as `respelled`, which is noted and never refused. The memory root is recorded
    like any other match, so that an explicit choice of it is not left to `report_root`'s
    fallback for a call that named nothing (CR1 F1). An empty mapping leaves the context as it
    is.

    Only HTTP keeps a registered root against the argument; a stdio workspace is checked
    against the client's own MCP roots above and answered as named, so it never mismatches.
    """
    if context.transport != "http" or workspace is None or context.error is not None or context.root is None:
        return {}
    authorized = (context.root, *context.cooperation_roots)
    for root in authorized:
        if _covers(root, workspace):
            return {"covering_root": root}
    for root in authorized:
        if _same_directory(root, workspace):
            return {"respelled": workspace, "covering_root": root}
    return {"mismatch": workspace}


def _covers(root, requested):
    """True when *requested* names *root* itself or a directory inside it.

    *root* is compared unresolved on purpose: every root reaching this point is already
    strictly resolved — `WorkspaceRegistry.resolve` accepts a record only when the stored
    string equals its own `resolve(strict=True)`, and `_existing_checkouts` holds a grant to
    the same equality — so resolving it again could only differ if that invariant broke.
    """
    try:
        target = Path(requested).expanduser()
        if not target.is_absolute():
            target = root / target
        return target.resolve().is_relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return False


def _same_directory(root, requested):
    """True when *requested* is *root*, or a directory in it, under another spelling (#576).

    `_covers` decides by exact containment of resolved paths, and `Path.resolve()` does not
    normalize case, so on a case-insensitive filesystem one real directory named under
    another spelling is not covered — and the whole session then loses `describe_repo`,
    `write_repo_summary`, `run_flow` and a `scope=repo` listing over two paths the user reads
    as identical. This decides note-instead-of-refusal for that case only, and nothing else
    compares case-insensitively.

    Both sides are resolved first, and the casefolded match is then confirmed against the
    filesystem with `samefile`, because a case-sensitive filesystem can hold two different
    directories whose names differ only in case: there the identity check fails and the
    refusal stands.

    What `samefile` confirms is the ancestor slice, not the whole requested path, so a
    directory inside the root that does not exist yet is accepted through the root it sits
    in. That is deliberate: such a path still names a place inside the registered
    repository, and refusing it would take the session's repository-scoped tools away over
    a directory it is about to create. A path whose ancestor slice is not the root is
    refused whether it exists or not, which is the case this function is here to keep.
    """
    try:
        target = Path(requested).expanduser()
        if not target.is_absolute():
            return False
        target = target.resolve()
        prefix, named = str(root).casefold(), str(target).casefold()
        if named != prefix and not named.startswith(prefix + os.sep):
            return False
        ancestor = Path(*target.parts[:len(root.parts)])
        return ancestor.samefile(root)
    except (OSError, ValueError, RuntimeError):
        return False


async def _workspace_context(session, workspace=None):
    from src.engine import config
    request_id = str(uuid.uuid4())
    try:
        answer = await asyncio.wait_for(session.list_roots(), MCP_ROOTS_TIMEOUT_SECONDS)
        uris = [str(item.uri) for item in answer.roots]
        if workspace is None:
            paths = {path.resolve() for path in map(config._root_uri_path, uris) if path is not None}
            if len(paths) != 1:
                return ClientContext(request_id, "stdio", error=(
                    "workspace_required: the client has no unique workspace; " + WORKSPACE_HINT))
            workspace = str(paths.pop())
        root = config.client_root_from_workspace(workspace, uris)
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


def calling_app(ctx=None):
    """The app behind an MCP request: ``X-Agents-Client`` over HTTP, else MCP ``clientInfo.name``."""
    request = _http_request(ctx)
    context = getattr(getattr(request, "state", None), "client_context", None)
    if getattr(context, "client", None):
        return context.client
    try:
        return ctx.session.client_params.clientInfo.name
    except (AttributeError, ValueError):
        return None


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
