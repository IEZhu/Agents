"""What the client already holds of the protocol, from its instruction files (#433).

A client such as Claude Code or Codex loads an instruction file whose managed routing section
the installers write from ``scripts/templates/routing-protocol-core.md``. When the workspace's
own file holds the current template, the full server instructions repeat what the model already
reads, so ``initialization_instructions`` keeps only what the file lacks and points to it. Any
doubt (an unknown client, no workspace, an unknown physical checkout, a linked worktree whose
file differs, a project override, a section Codex may cut off, a missing, unreadable or
different section) keeps the full text.

``drift_warnings`` names the managed sections the server can resolve that differ from this
installation's template, with the command that refreshes each, so an outdated file is reported
instead of going unnoticed. Nothing here writes a file.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, NamedTuple

from src.client_paths import client_home
from src.engine import protocol_text
from src.runtime_paths import installation_root

logger = logging.getLogger(__name__)

# The ClientContext of the HTTP request being served (src/daemon/app.py sets it); None over stdio.
REQUEST_CLIENT: ContextVar[Any] = ContextVar("agents_request_client", default=None)

# A managed instruction file is a few tens of kilobytes; a larger one is not read.
MAX_INSTRUCTION_FILE_BYTES = 1_000_000
# The marker pair that older installers wrote; scripts/_helpers/inject_claude_md.py migrates it.
LEGACY_BEGIN = "# >>> Agents-Core Routing Protocol (managed by init_repo.sh) >>>"
LEGACY_END = "# <<< Agents-Core Routing Protocol (managed by init_repo.sh) <<<"

# The file each managed client loads from the workspace root (src/daemon/clients.py APPS).
WORKSPACE_FILES = {"claude-code": "CLAUDE.md", "codex": "AGENTS.md"}
# The clients whose initialize answer may be the pointer. Claude Code shows only the first 4,096
# characters of the instructions (2,048 before v2.1.296), which the full text and the pointer
# spend alike, so it keeps the full text and takes no risk; Codex reads all of them.
POINTER_FILES = {"codex": "AGENTS.md"}
# Codex reads at most this many bytes of a project AGENTS.md (its project_doc_max_bytes default,
# recalled rather than read from Codex; a user setting may lower it). A section that ends later
# may be cut off, so the pointer is not sent.
CODEX_PROJECT_DOC_MAX_BYTES = 32 * 1024
# Managed clients that load no instruction file of their own: they get the protocol from MCP.
CLIENTS_WITHOUT_FILES = frozenset({"cursor", "claude-desktop"})


class SectionState(NamedTuple):
    """``state`` is current, outdated or daemon-outdated; ``end`` is the byte offset where the section ends."""
    state: str
    end: int | None


def _nonempty(path: Path) -> bool:
    try:
        return path.is_file() and bool(path.read_bytes().strip())
    except OSError:
        return False


def _codex_file(directory: Path) -> Path:
    """Codex gives a nonempty AGENTS.override.md precedence over AGENTS.md (install_codex_instructions)."""
    override = directory / "AGENTS.override.md"
    return override if _nonempty(override) else directory / "AGENTS.md"


def _global_files(client: str | None) -> list[Path]:
    """The global instruction files of ``client``, or of every client when it is unknown.

    They resolve through the server's environment (CLAUDE_CONFIG_DIR, CODEX_HOME): a session
    started with another profile loads a different file.
    """
    files: list[Path] = []
    if client in (None, "claude-code"):
        files.append(client_home("claude") / "CLAUDE.md")
    if client in (None, "codex"):
        files.append(_codex_file(client_home("codex")))
    if client in (None, "antigravity"):
        files.append(Path.home() / ".gemini/config/plugins/agents-core/rules/AGENTS.md")
    return files


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=256)
def _state_of(path: str, signature: tuple[int, int], template: str,
              on_disk: tuple[int, int] | None) -> SectionState | None:
    """The section state of ``path`` at ``signature``, against ``template`` and the on-disk template.

    ``on_disk`` is the committed template's signature: part of the key only, so a template
    rewritten on disk is read again.
    """
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_INSTRUCTION_FILE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_INSTRUCTION_FILE_BYTES:
        return None
    end_marker = protocol_text.END_MARKER.encode("utf-8")
    end = data.find(end_marker)
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    section = protocol_text.managed_section(text)
    if section is None:
        legacy = LEGACY_BEGIN in text and LEGACY_END in text.split(LEGACY_BEGIN, 1)[1]
        return SectionState("outdated", None) if legacy else None
    end = end + len(end_marker) if end >= 0 else None
    if section == template:
        return SectionState("current", end)
    try:
        committed = protocol_text.TEMPLATE.read_text(encoding="utf-8").strip()
    except OSError:
        committed = None
    newer = committed is not None and committed != template and section == committed
    return SectionState("daemon-outdated" if newer else "outdated", end)


def section_info(path: Path) -> SectionState | None:
    """The managed section of ``path``, cached per file version, so a repeated check costs a ``stat``.

    ``daemon-outdated`` means the file matches the committed template but this process serves an
    older one. None means no section: a missing, oversized or unreadable file, or no marker pair.
    """
    if not path.is_file():
        return None
    signature = _signature(path)
    if signature is None or signature[1] > MAX_INSTRUCTION_FILE_BYTES:
        return None
    return _state_of(str(path), signature, protocol_text.template().strip(), _signature(protocol_text.TEMPLATE))


def section_state(path: Path) -> str | None:
    info = section_info(path)
    return info.state if info is not None else None


def _root(context) -> Path | None:
    root = getattr(context, "root", None) if context is not None and not getattr(context, "error", None) else None
    return Path(root) if root is not None else None


def _grants(context) -> list[Path]:
    return [Path(path) for path in (getattr(context, "cooperation_roots", None) or ())]


def initialization_instructions(full: str, context=None) -> str:
    """``full``, or the pointer when every checkout's file for this client holds the current template.

    Only workspace files decide: they are the ones this client is known to load. The session's
    physical checkout must be known (a grant registered by an auto-registering bridge): a fixed
    identity may serve a linked worktree whose own file differs from the main checkout's. A
    global file might belong to another profile than the client's.
    """
    context = REQUEST_CLIENT.get() if context is None else context
    client = getattr(context, "client", None)
    name = POINTER_FILES.get(client)
    root, grants = _root(context), _grants(context)
    if name is None or root is None or not grants:
        return full
    checkouts = list(dict.fromkeys([root, *grants]))
    try:
        infos = [section_info(checkout / name) for checkout in checkouts]
        if any(info is None or info.state != "current" for info in infos):
            return full
        if client == "codex":
            if any(_nonempty(checkout / "AGENTS.override.md") for checkout in checkouts):
                return full  # Codex reads a project override instead of AGENTS.md
            if any(info.end is None or info.end > CODEX_PROJECT_DOC_MAX_BYTES for info in infos):
                return full  # Codex may cut the section off
    except Exception:  # never fail an initialize over a pointer
        logger.warning("Could not check the managed sections of %s", checkouts, exc_info=True)
        return full
    return protocol_text.server_pointer()


def install_initialization_pointer(server) -> None:
    """Make ``server`` (a low-level MCP server) answer initialize with ``initialization_instructions``.

    The HTTP daemon sets ``REQUEST_CLIENT`` before the stateless session starts, and the session
    builds its options inside that context; stdio always gets the full text. The SDK builds the
    options for every stateless request, which costs one cached ``stat`` per checkout here.
    """
    create = server.create_initialization_options

    def create_initialization_options(*args, **kwargs):
        options = create(*args, **kwargs)
        instructions = initialization_instructions(options.instructions or "")
        if instructions == options.instructions:
            return options
        return options.model_copy(update={"instructions": instructions})

    server.create_initialization_options = create_initialization_options


def _command(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


def _python(installation: Path) -> str:
    venv = installation / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return str(venv)


def _refresh_command(path: Path) -> str:
    """The command that rewrites the managed section of ``path``."""
    installation = installation_root()
    if path.resolve() == (installation / "CLAUDE.md").resolve():
        return f"{_command([_python(installation), '-m', 'src.engine.protocol_text', '--write'])}, run in {installation}"
    if path in _global_files(None):
        return _command([_python(installation), str(installation / "scripts" / "install_instructions.py")])
    return _command([_python(installation), str(installation / "scripts" / "_helpers" / "inject_claude_md.py"),
                     str(path), str(installation / "scripts" / "templates" / "routing-protocol-core.md")])


def _restart_command() -> str:
    installation = installation_root()
    return f"{_command([_python(installation), '-m', 'src.daemon', 'restart'])}, run in {installation}"


def _drift_candidates(client: str | None, root: Path | None) -> list[Path]:
    """The managed files to check for ``client``, in the registered root only.

    Other sessions' linked worktrees share the identity's grants, so they are not this session's
    files to report. A client with instruction files checks its own; a managed client without them
    checks none; any other name (an unknown client, a stdio bridge) checks every file the server
    can resolve.
    """
    if client in CLIENTS_WITHOUT_FILES:
        return []
    if client in WORKSPACE_FILES:
        names, global_client = [WORKSPACE_FILES[client]], client
    elif client == "antigravity":
        names, global_client = [], client
    else:
        names, global_client = sorted(set(WORKSPACE_FILES.values())), None
    files = []
    if root is not None:
        files = [_codex_file(root) if name == "AGENTS.md" else root / name for name in names]
    return list(dict.fromkeys(files + _global_files(global_client)))


def _shown(path: Path) -> str:
    """``path`` with the home directory written as ``~``."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def drift_warnings(context=None) -> list[str]:
    """One warning per resolvable managed routing section that differs from the shipped template."""
    context = current_client() if context is None else context
    warnings = []
    for path in _drift_candidates(getattr(context, "client", None), _root(context)):
        try:
            state = section_state(path)
        except Exception:
            logger.warning("Could not check the managed section of %s", path, exc_info=True)
            continue
        if state == "outdated":
            warnings.append(f"The managed routing section of {_shown(path)} differs from this installation's "
                            f"protocol (template revision {protocol_text.template_revision()}); refresh it with "
                            f"{_refresh_command(path)}.")
        elif state == "daemon-outdated":
            warnings.append(f"{_shown(path)} holds a newer protocol than the running Agents-Core server; "
                            f"restart the server with {_restart_command()}.")
    return warnings


def current_client():
    """The client context of this call: the HTTP request's, or the stdio process's."""
    context = REQUEST_CLIENT.get()
    if context is not None or os.environ.get("AGENTS_TRANSPORT") == "http":
        return context
    from src.daemon.workspaces import client_context
    try:
        return client_context(None, allow_install_fallback=False)
    except Exception:
        return None
