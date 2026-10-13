"""Remove every trace of Agents-Core from this user account (#362).

``scripts/uninstall.sh`` (macOS, Linux) and ``scripts/uninstall.ps1`` (Windows) run
``python -m src.uninstall``. It builds an inventory from what the installers and the service
create, prints it grouped by kind, asks once, and then:

* stops and removes the scheduled jobs: the LaunchAgents ``local.agents-core.*`` (the service,
  its auto-updater, user sync and the autodev scheduler), the ``systemd --user`` units and the
  crontab line of user sync, and the Task Scheduler tasks ``agents-core-*``;
* removes the ``Agents-Core`` and ``Agents-Core-Desktop`` entries from each client
  configuration, the managed routing section from global instruction files, and the generated
  routing reminder, after a backup of every file it edits; everything else in those files stays
  byte for byte;
* removes the state, data, cache and log directories, the one-command installation directory
  (``~/.agents-core``) and the autodev trusted clone;
* removes the credentials it knows (the ``agents-autodev-github`` keychain item and the autodev
  deploy key directory) only after a second, explicit confirmation, or with
  ``--yes --include-credentials``.

``--dry-run`` lists everything and changes nothing; ``--keep-data`` keeps the user library and
the histories. A path is removed only when it is in the inventory, is not a symbolic link and
resolves inside the home directory or a directory an environment variable of Agents-Core names.
The checkout the uninstaller runs from is kept unless it is that installation directory.

Only the standard library is imported, so a broken virtual environment does not stop it; every
OS command goes through an injectable runner, and the platform, home and environment are
parameters, so tests never touch the real account.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable, Mapping, Sequence

import tomllib

SERVER = "Agents-Core"
DESKTOP_SERVER = "Agents-Core-Desktop"  # src/client_paths.py
DESKTOP_DENY_RULES = tuple(f"mcp__{name}__*" for name in (DESKTOP_SERVER, DESKTOP_SERVER.replace("-", "_")))
SERVER_NAMES = (SERVER, DESKTOP_SERVER)
# scripts/_helpers/inject_claude_md.py
MARKERS = (
    (b"# >>> Agents-Core Routing Protocol (managed by init_repo) >>>",
     b"# <<< Agents-Core Routing Protocol (managed by init_repo) <<<"),
    (b"# >>> Agents-Core Routing Protocol (managed by init_repo.sh) >>>",
     b"# <<< Agents-Core Routing Protocol (managed by init_repo.sh) <<<"),
)
# scripts/_helpers/migrate_routing_memory.py
MEMORY_FILE = "feedback_agents_core_routing.md"
MEMORY_INDEX_ENTRIES = (
    "- [Agents-Core persona continuity](feedback_agents_core_routing.md) — assess locally; route only when the current role no longer fits",
    "- [Agents-Core routing is mandatory](feedback_agents_core_routing.md) — always call route_and_load() before any response, no exceptions",
)
KEYCHAIN_SERVICE = "agents-autodev-github"  # src/schedules/github.py
LAUNCH_AGENT = re.compile(r"^local\.agents-core\.[A-Za-z0-9._-]+\.plist$")
SYSTEMD_UNIT = re.compile(r"^agents-core-sync-[0-9a-f]{16}\.(service|timer)$")
CRON_LINE = re.compile(rb" agents-core-sync-[0-9a-f]{16}\s*$")
WINDOWS_TASK = re.compile(r"^\\?agents-core-(daemon|sync)-[0-9a-f]{16}(-updater)?$")
STATE_ID = re.compile(r"^[0-9a-f]{16}$")
# Siblings that backup_file.py (mkstemp) and inject_claude_md.py (a timestamp) leave next to a file.
INSTALLER_BACKUP = r"\.backup\.(?:[a-z0-9_]{8}|[0-9]{10}|[0-9]{19})$"
KEEP_IN_STATE = ("user-sync", "unresolved")  # the library's sync state and the fallback journal

KINDS = (
    ("service", "Services and scheduled jobs"),
    ("registration", "Client registrations"),
    ("instructions", "Managed instructions"),
    ("data", "State, data and logs"),
    ("cache", "Caches"),
    ("installation", "Installation"),
    ("backups", "Installer backups"),
    ("credential", "Credentials (removed only after a separate confirmation)"),
)

Runner = Callable[..., subprocess.CompletedProcess]
CODE_ROOT = Path(__file__).resolve().parents[1]  # the templates the installer generated the reminder from


def run_command(argv: Sequence[str], *, input: bytes | None = None) -> subprocess.CompletedProcess:
    """No shell, bytes in and out; a missing program is an exit code 127, not an exception."""
    given = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(list(argv), capture_output=True, timeout=60, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **given)
    except (FileNotFoundError, PermissionError) as error:
        return subprocess.CompletedProcess(list(argv), 127, b"", str(error).encode())
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, b"", b"timed out")


class Refused(RuntimeError):
    """A path the uninstaller must not touch: a symlink, or outside every allowed root."""


@dataclass
class Context:
    home: Path
    platform: str = sys.platform
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Runner = run_command
    source_root: Path = Path(__file__).resolve().parents[1]
    uid: int = field(default_factory=lambda: os.getuid() if hasattr(os, "getuid") else 0)
    stamp: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S"))

    def env_path(self, name: str) -> Path | None:
        value = self.environ.get(name, "").strip()
        return Path(os.path.abspath(os.path.expanduser(value))) if value else None

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    @property
    def backup_root(self) -> Path:
        return self.home / ".agents-core-uninstall-backups" / self.stamp

    def roots(self) -> list[Path]:
        """Where a removal may resolve: the home and the directories Agents-Core's variables name."""
        found = [self.home]
        for name in ("AGENTS_HOME", "AGENTS_SERVICE_DIR", "FASTEMBED_CACHE_DIR", "AGENTS_USER_FLOWS_DIR",
                     "XDG_STATE_HOME", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            path = self.env_path(name)
            if path is not None:
                found.append(path)
        return found


@dataclass
class Item:
    kind: str
    target: str
    action: str  # remove, edit, stop, keep
    source: str  # where the installation creates it
    detail: str = ""
    apply: Callable[[], str | None] | None = None

    def line(self) -> str:
        text = f"  [{self.action}] {self.target}"
        if self.detail:
            text += f" — {self.detail}"
        return text


# --- safe file operations ---------------------------------------------------------------------

def _real(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _within(path: Path, roots: Iterable[Path]) -> bool:
    real = _real(path)
    for root in roots:
        base = _real(root)
        if real == base or real.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def _is_link(path: Path) -> bool:
    return os.path.islink(path) or bool(getattr(path, "is_junction", lambda: False)())


def check_removable(ctx: Context, path: Path) -> None:
    if _is_link(path):
        raise Refused(f"{path} is a symbolic link; remove it yourself after checking where it points")
    if not _within(path, ctx.roots()):
        raise Refused(f"{path} resolves outside the home directory and every configured root")
    if _real(path) in {_real(root) for root in ctx.roots()} or len(Path(_real(path)).parts) < 3:
        raise Refused(f"{path} is a root directory, not an Agents-Core path")


def _writable_retry(function, path, _info):
    """Git keeps objects read-only, which Windows refuses to delete: make it writable, try again."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    function(path)


def _unlink_link(path: Path) -> None:
    """Remove a link itself, never what it points to (a directory link on Windows needs rmdir)."""
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError):
        os.rmdir(path)


def remove_path(ctx: Context, path: Path) -> str:
    check_removable(ctx, path)
    if path.is_dir():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_writable_retry)  # links inside are removed, not followed
        else:
            shutil.rmtree(path, onerror=_writable_retry)
    elif os.path.lexists(path):
        path.unlink()
    return "removed"


def remove_marker(marker: Path) -> str:
    """The one file removed from a kept checkout: its pointer to the service's state directory."""
    if _is_link(marker):
        raise Refused(f"{marker} is a symbolic link; remove it yourself")
    marker.unlink()
    return "removed"


def remove_except(ctx: Context, root: Path, kept: Sequence[Path]) -> str:
    """Remove ``root`` except the ``kept`` paths inside it (and the directories that lead to them)."""
    check_removable(ctx, root)
    kept_real = [_real(path) for path in kept]
    if not kept_real:
        return remove_path(ctx, root)

    def prune(directory: Path) -> None:
        for child in directory.iterdir():
            real = _real(child)
            if real in kept_real:
                continue
            if _is_link(child):  # inside a removed tree, e.g. .venv/bin/python: the link goes, its target stays
                _unlink_link(child)
            elif child.is_dir() and not _is_link(child) and any(k.startswith(real + os.sep) for k in kept_real):
                prune(child)
            else:
                remove_path(ctx, child)

    prune(root)
    return "removed; kept " + ", ".join(str(path) for path in kept)


class Backups:
    """Copies of every edited file, under ``~/.agents-core-uninstall-backups/<time>``, with a manifest."""

    def __init__(self, ctx: Context):
        self.ctx, self.records = ctx, []

    def save(self, path: Path, content: bytes) -> Path:
        root = self.ctx.backup_root
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{len(self.records) + 1:03d}-{path.name}"
        target.write_bytes(content)
        self.records.append({"original": str(path), "backup": str(target)})
        (root / "manifest.json").write_text(json.dumps(self.records, indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
        return target


def write_atomic(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def edit_file(ctx: Context, backups: Backups, path: Path, transform: Callable[[bytes], bytes | None]) -> str:
    """Back the file up, then write ``transform``'s bytes, or delete it when they are None (only ours was left)."""
    if _is_link(path):
        raise Refused(f"{path} is a symbolic link; remove the Agents-Core entries from its target yourself")
    original = path.read_bytes()
    updated = transform(original)
    if updated == original:
        return "unchanged"
    backup = backups.save(path, original)
    if updated is None:
        path.unlink()
        return f"removed (only Agents-Core content was left); backup {backup}"
    write_atomic(path, updated)
    return f"edited; backup {backup}"


# --- JSON: remove members without re-serializing the rest ---------------------------------------

@dataclass
class _Node:
    kind: str  # object, array, scalar
    start: int
    end: int
    members: list = field(default_factory=list)  # object: (key, key_start, value_node); array: value_node


class _Parser:
    def __init__(self, text: str):
        self.text = text

    def skip(self, i: int) -> int:
        while i < len(self.text) and self.text[i] in " \t\r\n":
            i += 1
        return i

    def string(self, i: int) -> int:
        i += 1
        while self.text[i] != '"':
            i += 2 if self.text[i] == "\\" else 1
        return i + 1

    def value(self, i: int) -> _Node:
        i = self.skip(i)
        char = self.text[i]
        if char == "{":
            node, i = _Node("object", i, -1), self.skip(i + 1)
            if self.text[i] == "}":
                node.end = i + 1
                return node
            while True:
                key_start = self.skip(i)
                key_end = self.string(key_start)
                key = json.loads(self.text[key_start:key_end])
                i = self.skip(key_end)
                if self.text[i] != ":":
                    raise ValueError("expected ':'")
                child = self.value(i + 1)
                node.members.append((key, key_start, child))
                i = self.skip(child.end)
                if self.text[i] == ",":
                    i += 1
                    continue
                if self.text[i] != "}":
                    raise ValueError("expected '}'")
                node.end = i + 1
                return node
        if char == "[":
            node, i = _Node("array", i, -1), self.skip(i + 1)
            if self.text[i] == "]":
                node.end = i + 1
                return node
            while True:
                child = self.value(i)
                node.members.append(child)
                i = self.skip(child.end)
                if self.text[i] == ",":
                    i += 1
                    continue
                if self.text[i] != "]":
                    raise ValueError("expected ']'")
                node.end = i + 1
                return node
        if char == '"':
            return _Node("scalar", i, self.string(i))
        match = re.compile(r"-?[0-9.eE+-]+|true|false|null").match(self.text, i)
        if not match:
            raise ValueError(f"unexpected character at {i}")
        return _Node("scalar", i, match.end())


def _starts(node: _Node) -> list[int]:
    return [key_start for _, key_start, _ in node.members] if node.kind == "object" else [m.start for m in node.members]


def _ends(node: _Node) -> list[int]:
    return [child.end for _, _, child in node.members] if node.kind == "object" else [m.end for m in node.members]


def _cut(text: str, node: _Node, index: int) -> str:
    """Remove one member of an object or array with its comma, leaving every other byte as it was."""
    starts, ends = _starts(node), _ends(node)
    if len(starts) == 1:
        return text[:node.start + 1] + text[node.end - 1:]
    if index < len(starts) - 1:
        return text[:starts[index]] + text[starts[index + 1]:]
    return text[:ends[index - 1]] + text[ends[index]:]


Match = Callable[[list], "tuple[_Node, int] | None"]


def _find(node: _Node, path: Sequence, value_test=None):
    """The (container, index) of the first member at ``path`` (``*`` matches any key)."""
    if not path:
        return None
    head, rest = path[0], path[1:]
    if node.kind == "object":
        for index, (key, _, child) in enumerate(node.members):
            if head != "*" and key != head:
                continue
            if not rest:
                return node, index
            found = _find(child, rest, value_test)
            if found:
                return found
    if node.kind == "array" and head == "[]" and value_test is not None:
        for index, child in enumerate(node.members):
            if value_test(child):
                return node, index
    return None


def _drop(document, path: Sequence, value_test=None) -> None:
    """The same removal on the parsed document, to check the text edit against."""
    head, rest = path[0], path[1:]
    if isinstance(document, dict):
        for key in list(document):
            if head in ("*", key):
                if rest:
                    _drop(document[key], rest, value_test)
                else:
                    del document[key]
    elif isinstance(document, list) and head == "[]":
        document[:] = [value for value in document if not value_test(value)]


def remove_json_members(content: bytes, removals: Sequence[tuple[Sequence, Callable | None]],
                        *, drop_if_empty: Sequence[Sequence[str]] = (), delete_when=None) -> bytes | None:
    """Remove the members at each path, checking the result parses to the same edit of the document.

    ``removals`` holds ``(path, value_test)``: a path of keys (``*`` for any key), ending in ``[]``
    with a ``value_test`` on the raw element text for array elements. ``drop_if_empty`` names
    containers removed once an edit left them empty. Returns None when the result would equal
    ``delete_when`` (a file that only held Agents-Core).
    """
    bom = content.startswith(b"\xef\xbb\xbf")
    text = content[3:].decode("utf-8") if bom else content.decode("utf-8")
    expected = json.loads(text)
    if not isinstance(expected, dict):
        raise ValueError("the file's root is not a JSON object; edit it yourself")
    changed = False
    for path, test in removals:
        _drop(expected, path, (lambda value, test=test: test(json.dumps(value))) if test else None)
        while True:
            root = _Parser(text).value(0)
            found = _find(root, path, (lambda node, test=test: test(text[node.start:node.end])) if test else None)
            if not found:
                break
            text, changed = _cut(text, *found), True
    if not changed:
        return content
    for path in drop_if_empty:
        current = expected
        for key in path[:-1]:
            current = current.get(key) if isinstance(current, dict) else None
        if isinstance(current, dict) and current.get(path[-1]) in ({}, []):
            del current[path[-1]]
            found = _find(_Parser(text).value(0), path)
            if found:
                text = _cut(text, *found)
    if json.loads(text) != expected:
        raise ValueError("the edit did not leave the rest of the file as it was; edit it yourself")
    if delete_when is not None and expected in delete_when:
        return None
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def _named_server(text: str) -> bool:
    return text in SERVER_NAMES


def mcp_removals(*, projects: bool = False) -> list:
    paths = [("mcpServers", name) for name in SERVER_NAMES]
    if projects:
        paths += [("projects", "*", "mcpServers", name) for name in SERVER_NAMES]
    return [(path, None) for path in paths]


def deny_removal():
    def ours(raw: str) -> bool:
        try:
            return json.loads(raw) in DESKTOP_DENY_RULES
        except ValueError:
            return False
    return [(("permissions", "deny", "[]"), ours)]


# --- TOML (Codex) and Markdown ----------------------------------------------------------------

def remove_toml_server(content: bytes, name: str = SERVER) -> bytes:
    """Remove ``[mcp_servers.<name>]`` and its subtables, keeping every other line byte for byte."""
    text = content.decode("utf-8")
    lines, kept, skip = text.splitlines(keepends=True), [], False
    for line in lines:
        if re.match(r"\s*\[", line):
            header = line.strip().split("#", 1)[0].strip()
            try:  # tomllib reads the header, so quoted keys mean what they mean in TOML
                probe = tomllib.loads(header + "\n__agents_probe = true\n")
                servers = probe.get("mcp_servers", {})
                skip = isinstance(servers, dict) and name in servers
            except tomllib.TOMLDecodeError:
                skip = False
        if not skip:
            kept.append(line)
    if skip:  # the section ended the file: drop the blank line the migration put before it
        while kept and not kept[-1].strip():
            kept.pop()
    updated = "".join(kept)
    if name in tomllib.loads(updated).get("mcp_servers", {}):
        raise ValueError(f"{name} is defined inline; remove it from the file yourself")
    original = tomllib.loads(text)
    original.get("mcp_servers", {}).pop(name, None)
    if original.get("mcp_servers") == {} and "mcp_servers" not in tomllib.loads(updated):
        original.pop("mcp_servers")
    if tomllib.loads(updated) != original:
        raise ValueError("the edit did not leave the rest of the file as it was; edit it yourself")
    return updated.encode("utf-8")


def remove_routing_block(content: bytes) -> bytes | None:
    """The file without the managed routing section and the newline the installer added after it."""
    present = [(begin, end) for begin, end in MARKERS if begin in content or end in content]
    if not present:
        return content
    if len(present) != 1:
        raise ValueError("mixed routing marker versions; remove the section yourself")
    begin, end = present[0]
    if content.count(begin) != 1 or content.count(end) != 1 or content.index(end) < content.index(begin):
        raise ValueError("incomplete routing markers; remove the section yourself")
    start, stop = content.index(begin), content.index(end) + len(end)
    for ending in (b"\r\n", b"\n"):
        if content.startswith(ending, stop):
            stop += len(ending)
            break
    updated = content[:start] + content[stop:]
    return None if not updated.strip() else updated


def remove_index_entries(content: bytes) -> bytes | None:
    entries = {entry.encode() for entry in MEMORY_INDEX_ENTRIES}
    lines = content.splitlines(keepends=True)
    kept = [line for line in lines if line.rstrip(b"\r\n") not in entries]
    updated = b"".join(kept)
    return None if not updated.strip() else updated


# --- inventory ---------------------------------------------------------------------------------

def is_checkout(path: Path) -> bool:
    return (path / ".git").exists() and (path / "scripts/init_repo.sh").is_file() and (path / "src/server.py").is_file()


def installation_dir(ctx: Context) -> Path:
    return ctx.env_path("AGENTS_HOME") or ctx.home / ".agents-core"


def state_roots(ctx: Context) -> list[Path]:
    """Directories that hold only per-installation state directories (``<16 hex>``)."""
    if ctx.platform == "darwin":
        return [ctx.home / "Library/Application Support/Agents-Core"]
    if ctx.windows:
        profile = ctx.env_path("USERPROFILE") or ctx.home
        local = ctx.env_path("LOCALAPPDATA") or ctx.home / "AppData/Local"
        roots = [profile / ".agents-core", local / "Agents-Core"]
        try:
            roots += [package / "LocalCache/Local/Agents-Core" for package in sorted((local / "Packages").glob("Claude_*"))]
        except OSError:
            pass
        return roots
    base = ctx.env_path("XDG_STATE_HOME") or ctx.home / ".local/state"
    return [base / "agents-core"]


def looks_like_state(path: Path) -> bool:
    return path.is_dir() and any((path / name).exists() for name in ("service.json", "token", *KEEP_IN_STATE, "schedules"))


def state_dirs(ctx: Context, installations: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    for root in state_roots(ctx):
        if root.is_dir() and not is_checkout(root):
            found += [child for child in sorted(root.iterdir()) if STATE_ID.match(child.name) and child.is_dir()]
    override = ctx.env_path("AGENTS_SERVICE_DIR")
    if override and looks_like_state(override):
        found.append(override)
    for installation in installations:
        try:
            marker = json.loads((installation / "data/.shared-service.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(marker, dict) and isinstance(marker.get("directory"), str):
            directory = Path(marker["directory"])
            if looks_like_state(directory):
                found.append(directory)
    unique: dict[str, Path] = {}
    for path in found:
        unique.setdefault(_real(path), path)
    return list(unique.values())


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def workspace_roots(states: Sequence[Path]) -> list[Path]:
    roots: set[Path] = set()
    for state in states:
        workspaces = _read_json(state / "workspaces.json")
        if isinstance(workspaces, dict):
            roots |= {Path(value) for value in workspaces.values() if isinstance(value, str)}
    return sorted(root for root in roots if root.is_dir())


def project_layers(states: Sequence[Path]) -> list[tuple[str, Path]]:
    """Project configuration files of every workspace a service registered (src/daemon/registrations.py)."""
    layers: list[tuple[str, Path]] = []
    for state in states:
        workspaces = _read_json(state / "workspaces.json")
        roots = {Path(value) for value in workspaces.values() if isinstance(value, str)} if isinstance(workspaces, dict) else set()
        records = _read_json(state / "client-configs.json")
        for record in (records or {}).get("configs", []) if isinstance(records, dict) else []:
            if isinstance(record, dict) and isinstance(record.get("path"), str) and Path(record["path"]).is_absolute():
                layers.append((str(record.get("client")), Path(record["path"])))
                if isinstance(record.get("workspace"), str):
                    roots.add(Path(record["workspace"]))
        for root in sorted(roots):
            layers += [("claude-project", root / ".mcp.json"), ("codex", root / ".codex/config.toml"),
                       ("cursor", root / ".cursor/mcp.json")]
    return layers


def client_files(ctx: Context) -> list[tuple[str, Path]]:
    """The user-level client configurations init_repo.sh/.bat and `src.daemon migrate` write."""
    env = ctx.environ
    claude_home = ctx.env_path("CLAUDE_CONFIG_DIR") or ctx.home / ".claude"
    codex_home = ctx.env_path("CODEX_HOME") or ctx.home / ".codex"
    files = [("claude", claude_home / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else ctx.home / ".claude.json"),
             ("claude-settings", claude_home / "settings.json"),
             ("codex", codex_home / "config.toml"),
             ("cursor", ctx.env_path("AGENTS_CURSOR_MCP_CONFIG") or ctx.home / ".cursor/mcp.json"),
             ("antigravity", ctx.env_path("AGENTS_ANTIGRAVITY_MCP_CONFIG") or ctx.home / ".gemini/config/mcp_config.json"),
             ("antigravity-registry", ctx.home / ".gemini/config/config.json")]
    if ctx.env_path("AGENTS_CLAUDE_DESKTOP_CONFIG"):
        files.append(("desktop", ctx.env_path("AGENTS_CLAUDE_DESKTOP_CONFIG")))
    if ctx.windows:
        appdata = ctx.env_path("APPDATA") or ctx.home / "AppData/Roaming"
        local = ctx.env_path("LOCALAPPDATA") or ctx.home / "AppData/Local"
        files.append(("desktop", appdata / "Claude/claude_desktop_config.json"))
        try:
            files += [("desktop", package / "LocalCache/Roaming/Claude/claude_desktop_config.json")
                      for package in sorted((local / "Packages").glob("Claude_*"))]
        except OSError:
            pass
    elif ctx.platform == "darwin":
        files.append(("desktop", ctx.home / "Library/Application Support/Claude/claude_desktop_config.json"))
    else:
        files.append(("desktop", (ctx.env_path("XDG_CONFIG_HOME") or ctx.home / ".config") / "Claude/claude_desktop_config.json"))
    return files


SOURCES = {
    "claude": "scripts/init_repo.sh inject_mcp_config (Claude Code), scripts/_helpers/inject_mcp.py, src/daemon/clients.py prepare('claude')",
    "claude-project": "src/daemon/clients.py prepare('claude-project') (src.daemon migrate --workspace)",
    "claude-settings": "scripts/_helpers/deny_desktop_mcp.py, src/daemon/clients.py prepare('claude-deny-desktop')",
    "codex": "src/daemon/clients.py replace_toml_server (src.daemon migrate)",
    "cursor": "scripts/init_repo.sh inject_mcp_config (Cursor), src/daemon/clients.py prepare('cursor')",
    "antigravity": "scripts/init_repo.sh inject_mcp_config (Antigravity), src/daemon/clients.py prepare('antigravity')",
    "antigravity-registry": "src/daemon/clients.py provision_antigravity_plugin (plugins.agents-core)",
    "desktop": "scripts/init_repo.sh inject_mcp_config (Claude Desktop), src/daemon/clients.py prepare('desktop')",
}


def _transform_for(client: str) -> Callable[[bytes], bytes | None] | None:
    if client == "codex":
        def codex(content: bytes) -> bytes | None:
            updated = remove_toml_server(remove_toml_server(content, SERVER), DESKTOP_SERVER)
            return None if updated != content and not updated.strip() else updated
        return codex
    if client == "claude-settings":
        return lambda content: remove_json_members(content, deny_removal(),
                                                   drop_if_empty=[("permissions", "deny"), ("permissions",)])
    if client == "antigravity-registry":
        return lambda content: remove_json_members(content, [(("plugins", "agents-core"), None)],
                                                   drop_if_empty=[("plugins",)])
    if client in ("claude", "claude-project", "cursor", "antigravity", "desktop"):
        return lambda content: remove_json_members(
            content, mcp_removals(projects=client == "claude"), drop_if_empty=[("mcpServers",)],
            delete_when=None if client == "claude" else [{}])
    return None


def _mentions_us(path: Path) -> bool:
    try:
        content = path.read_bytes()
    except OSError:
        return False
    return any(name.encode() in content for name in (*SERVER_NAMES, "agents-core")) or any(
        begin in content for begin, _ in MARKERS)


def build_plan(ctx: Context, *, keep_data: bool = False) -> list[Item]:
    items: list[Item] = []
    backups = Backups(ctx)
    installation = installation_dir(ctx)
    installations = [path for path in dict.fromkeys([installation, ctx.source_root]) if is_checkout(path)]
    states = state_dirs(ctx, installations)

    # --- services
    if ctx.platform == "darwin":
        agents = ctx.home / "Library/LaunchAgents"
        for plist in sorted(agents.glob("local.agents-core.*.plist")) if agents.is_dir() else []:
            if not LAUNCH_AGENT.match(plist.name):
                continue
            label = plist.name[:-len(".plist")]

            def stop(plist=plist, label=label):
                ctx.runner(["/bin/launchctl", "bootout", f"gui/{ctx.uid}/{label}"])
                return remove_path(ctx, plist)
            items.append(Item("service", str(plist), "remove",
                              "src/daemon/service.py Launchd.write, src/daemon/autoupdate.py write_plist, "
                              "src/user_sync/schedule.py LaunchAgent, src/schedules/launchagent.py Scheduler.install",
                              f"launchctl bootout gui/{ctx.uid}/{label}", stop))
    elif ctx.windows:
        query = ctx.runner(["schtasks", "/Query", "/FO", "CSV", "/NH"])
        names = []
        if query.returncode == 0:
            text = (query.stdout or b"").decode("utf-8", "replace")
            for row in csv.reader(io.StringIO(text)):
                if row and WINDOWS_TASK.match(row[0].strip()) and row[0].strip() not in names:
                    names.append(row[0].strip())
        for name in names:
            def delete(name=name):
                ctx.runner(["schtasks", "/End", "/TN", name])
                result = ctx.runner(["schtasks", "/Delete", "/TN", name, "/F"])
                if result.returncode:
                    raise RuntimeError(f"schtasks /Delete {name} failed with exit code {result.returncode}")
                return "removed"
            items.append(Item("service", f"Task Scheduler: {name}", "remove",
                              "src/daemon/service.py TaskScheduler (daemon, -updater), src/user_sync/schedule.py "
                              "TaskSchedulerJob", "schtasks /End, /Delete", delete))
    else:
        units = (ctx.env_path("XDG_CONFIG_HOME") or ctx.home / ".config") / "systemd/user"
        files = sorted(path for path in units.glob("agents-core-sync-*") if SYSTEMD_UNIT.match(path.name)) if units.is_dir() else []
        wants = units / "timers.target.wants"
        links = sorted(path for path in wants.glob("agents-core-sync-*.timer")) if wants.is_dir() else []
        if files or links:
            def disable(files=files, links=links):
                for path in files:
                    check_removable(ctx, path)
                for path in links:  # `systemctl enable` links to our unit: the link goes, nothing else
                    check_removable(ctx, path.parent)
                for timer in [path.name for path in files if path.suffix == ".timer"]:
                    ctx.runner(["systemctl", "--user", "disable", "--now", timer])
                for path in links + files:
                    if os.path.lexists(path):
                        path.unlink()  # a unit link in the wants directory points at our own unit file
                ctx.runner(["systemctl", "--user", "daemon-reload"])
                return "removed"
            items.append(Item("service", ", ".join(str(path) for path in links + files), "remove",
                              "src/user_sync/schedule.py SystemdTimer.enable", "systemctl --user disable --now", disable))
        listing = ctx.runner(["crontab", "-l"])
        if listing.returncode == 0 and any(CRON_LINE.search(line.rstrip(b"\r\n"))
                                           for line in (listing.stdout or b"").splitlines()):
            def uncron(content=listing.stdout):
                lines = content.splitlines(keepends=True)
                kept = [line for line in lines if not CRON_LINE.search(line.rstrip(b"\r\n"))]
                backups.save(Path("crontab"), content)
                table = b"".join(kept)
                result = ctx.runner(["crontab", "-"], input=table) if table else ctx.runner(["crontab", "-r"])
                if result.returncode:
                    raise RuntimeError(f"crontab failed with exit code {result.returncode}")
                return "removed the user sync line; backup in " + str(ctx.backup_root)
            items.append(Item("service", "crontab line agents-core-sync-<id>", "edit",
                              "src/user_sync/schedule.py Cron.enable", "other crontab lines stay", uncron))

    # --- client registrations
    seen: set[str] = set()
    for client, path in client_files(ctx) + project_layers(states):
        key = _real(path)
        transform = _transform_for(client)
        if key in seen or transform is None or not (path.is_file() or _is_link(path)) or not _mentions_us(path):
            continue
        seen.add(key)
        items.append(Item("registration", str(path), "edit", SOURCES.get(client, client),
                          "only the Agents-Core entries; a backup first",
                          lambda path=path, transform=transform: edit_file(ctx, backups, path, transform)))
    plugin = ctx.home / ".gemini/config/plugins/agents-core"
    if plugin.exists() or _is_link(plugin):
        items.append(Item("registration", str(plugin), "remove",
                          "src/daemon/clients.py provision_antigravity_plugin", "the Antigravity plugin",
                          lambda: remove_path(ctx, plugin)))

    # --- managed instructions
    claude_home = ctx.env_path("CLAUDE_CONFIG_DIR") or ctx.home / ".claude"
    codex_home = ctx.env_path("CODEX_HOME") or ctx.home / ".codex"
    for path, source in ((claude_home / "CLAUDE.md", "scripts/init_repo.sh (inject_claude_md.py), scripts/install_instructions.py"),
                         (codex_home / "AGENTS.md", "scripts/_helpers/install_codex_instructions.py"),
                         (codex_home / "AGENTS.override.md", "scripts/_helpers/install_codex_instructions.py")):
        if path.is_file() and any(begin in path.read_bytes() for begin, _ in MARKERS):
            items.append(Item("instructions", str(path), "edit", source,
                              "only the section between the routing markers; a backup first",
                              lambda path=path: edit_file(ctx, backups, path, remove_routing_block)))
    memory = claude_home / "memory"
    reminder = memory / MEMORY_FILE
    if reminder.is_file():
        known = {CODE_ROOT / "scripts/templates/memory-routing.md"}
        known |= set((CODE_ROOT / "scripts/templates/legacy").glob("memory-routing-*.md"))
        generated = {path.read_bytes() for path in known if path.is_file()}
        if reminder.read_bytes() in generated:
            def drop_reminder():
                backups.save(reminder, reminder.read_bytes())
                return remove_path(ctx, reminder)
            items.append(Item("instructions", str(reminder), "remove", "scripts/_helpers/migrate_routing_memory.py",
                              "the generated routing reminder", drop_reminder))
        else:
            items.append(Item("instructions", str(reminder), "keep", "scripts/_helpers/migrate_routing_memory.py",
                              "edited by the user, so it is not the generated reminder; remove it yourself"))
    index = memory / "MEMORY.md"
    if index.is_file() and MEMORY_FILE.encode() in index.read_bytes():
        items.append(Item("instructions", str(index), "edit", "scripts/_helpers/migrate_routing_memory.py",
                          "only the routing reminder's index line; a backup first",
                          lambda: edit_file(ctx, backups, index, remove_index_entries)))

    # --- state, data, logs
    library_keep = []
    for root in state_roots(ctx):
        if not root.is_dir() or is_checkout(root):
            continue
        kept = [child / name for child in root.iterdir() if STATE_ID.match(child.name)
                for name in KEEP_IN_STATE if (child / name).exists()] if keep_data else []
        library_keep += kept
        items.append(Item("data", str(root), "remove", "src/daemon/state.py default_state_dir, "
                          "src/user_sync/engine.py default_state_dir, src/memory/fallback.py journal_root",
                          "service state, token, logs, workspace registry, history indexes, user sync state"
                          + ("; keeps user sync and the fallback journal (--keep-data)" if kept else ""),
                          lambda root=root, kept=kept: remove_except(ctx, root, kept)))
    covered = [root for root in state_roots(ctx)]
    for state in states:
        if any(_within(state, [root]) for root in covered):
            continue
        kept = [state / name for name in KEEP_IN_STATE if (state / name).exists()] if keep_data else []
        items.append(Item("data", str(state), "remove", "src/daemon/control.py install --state / AGENTS_SERVICE_DIR",
                          "a service state directory outside the default location",
                          lambda state=state, kept=kept: remove_except(ctx, state, kept)))
    flows = ctx.env_path("AGENTS_USER_FLOWS_DIR")
    if flows and flows.is_dir():
        if keep_data:
            items.append(Item("data", str(flows), "keep", "src/user_flows.py (AGENTS_USER_FLOWS_DIR)",
                              "the user library (--keep-data)"))
        else:
            items.append(Item("data", str(flows), "remove", "src/user_flows.py (AGENTS_USER_FLOWS_DIR)",
                              "the user library and its version history", lambda: remove_path(ctx, flows)))
    for root in workspace_roots(states):
        written = [name for name in ("history.md", "history", "data/memory", "logs") if (root / name).exists()]
        if written:
            items.append(Item("data", str(root), "keep", "src/memory (log_interaction, describe_repo)",
                              "project files, possibly tracked by Git: " + ", ".join(written)
                              + " and the Repository Memory section of CLAUDE.md; delete them yourself"))
    autodev = ctx.home / ".local/share/agents-autodev"
    clones = [autodev] if autodev.is_dir() else []
    for state in states:
        configured = _read_json(state / "schedules/autodev.json")
        clone = configured.get("clone") if isinstance(configured, dict) else None
        if isinstance(clone, str):
            path = Path(os.path.expanduser(clone)) if not clone.startswith("~") else ctx.home / clone[2:]
            if (path / ".git").exists() and not any(_within(path, [other]) for other in clones):
                clones.append(path)
    for clone in clones:
        items.append(Item("data", str(clone), "remove", "src/schedules/config.py DEFAULTS['clone'] (autodev trusted clone)",
                          "the autodev trusted repository", lambda clone=clone: remove_path(ctx, clone)))

    # --- caches
    cache = ctx.env_path("FASTEMBED_CACHE_DIR") or ctx.home / ".cache/fastembed"
    if cache.is_dir():
        items.append(Item("cache", str(cache), "remove", "src/engine/config.py FASTEMBED_CACHE_DIR, scripts/init_repo.sh "
                          "(pre-download)", "embedding models; another program that set the same directory "
                          "downloads its models again", lambda: remove_path(ctx, cache)))

    # --- installation
    if is_checkout(installation):
        keep = [installation / "flows/.user"] if keep_data and (installation / "flows/.user").exists() else []
        items.append(Item("installation", str(installation), "remove", "install.sh (git clone into AGENTS_HOME, "
                          "default ~/.agents-core), scripts/init_repo.sh (.venv, .env, data/)",
                          "the one-command installation" + ("; keeps flows/.user (--keep-data)" if keep else ""),
                          lambda: remove_except(ctx, installation, keep)))
    if is_checkout(ctx.source_root) and _real(ctx.source_root) != _real(installation):
        items.append(Item("installation", str(ctx.source_root), "keep", "a checkout you cloned yourself",
                          "the uninstaller never deletes the checkout it runs from; delete it yourself"))
        marker = ctx.source_root / "data/.shared-service.json"
        if marker.is_file():
            items.append(Item("data", str(marker), "remove", "src/daemon/control.py Controller.install",
                              "the kept checkout's pointer to the removed service",
                              lambda: remove_marker(marker)))

    # --- installer backups next to the files it edited
    candidates = [path for _, path in client_files(ctx)] + [claude_home / "CLAUDE.md", codex_home / "AGENTS.md",
                                                            codex_home / "AGENTS.override.md", index]
    pattern_paths: list[Path] = []
    for path in candidates:
        if path.parent.is_dir():
            pattern = re.compile(re.escape(path.name) + INSTALLER_BACKUP)
            pattern_paths += [child for child in sorted(path.parent.iterdir())
                              if pattern.match(child.name) and child.is_file() and not _is_link(child)]
    for path in pattern_paths:
        items.append(Item("backups", str(path), "remove", "scripts/_helpers/backup_file.py, "
                          "scripts/_helpers/inject_claude_md.py copy_backup",
                          "a copy the installer made before its edit", lambda path=path: remove_path(ctx, path)))

    # --- credentials
    if ctx.platform == "darwin":
        found = ctx.runner(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE])
        if found.returncode == 0:
            def unkey():
                result = ctx.runner(["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE])
                if result.returncode:
                    raise RuntimeError(f"security delete-generic-password exited {result.returncode}")
                return "removed"
            items.append(Item("credential", f"keychain item {KEYCHAIN_SERVICE}", "remove",
                              "src/schedules/github.py KEYCHAIN_SERVICE (autodev token)", "", unkey))
    keys = ctx.home / ".config/agents-autodev"
    if keys.is_dir() or _is_link(keys):
        items.append(Item("credential", str(keys), "remove", "src/schedules/config.py DEFAULTS['github']['deploy_key']",
                          "the autodev deploy key; revoke it on GitHub too", lambda: remove_path(ctx, keys)))
    return items


# --- running -----------------------------------------------------------------------------------

def render(items: Sequence[Item], *, dry_run: bool) -> str:
    lines = ["Agents-Core uninstall" + (" — dry run, nothing is changed" if dry_run else "")]
    for kind, title in KINDS:
        group = [item for item in items if item.kind == kind]
        if group:
            lines.append("")
            lines.append(title + ":")
            lines += [item.line() for item in group]
    if not items:
        lines.append("Nothing of Agents-Core was found.")
    return "\n".join(lines)


def execute(items: Sequence[Item], *, credentials: bool) -> dict[str, list[str]]:
    report: dict[str, list[str]] = {"done": [], "kept": [], "refused": [], "failed": []}
    for item in items:
        if item.action == "keep" or item.apply is None:
            report["kept"].append(f"{item.target}: {item.detail}")
            continue
        if item.kind == "credential" and not credentials:
            report["kept"].append(f"{item.target}: credentials were not confirmed")
            continue
        try:
            note = item.apply()
            report["done"].append(f"{item.target}: {note or 'done'}")
        except Refused as error:
            report["refused"].append(f"{item.target}: {error}")
        except Exception as error:  # one failure never stops the rest
            report["failed"].append(f"{item.target}: {type(error).__name__}: {error}")
    return report


def render_report(report: dict[str, list[str]], ctx: Context) -> str:
    titles = {"done": "Removed or edited", "kept": "Kept", "refused": "Refused", "failed": "Failed"}
    lines = []
    for key, title in titles.items():
        if report[key]:
            lines.append(f"{title}:")
            lines += [f"  {entry}" for entry in report[key]]
    if ctx.backup_root.is_dir():
        lines.append(f"Backups of edited files: {ctx.backup_root} (manifest.json maps each to its original)")
    lines.append("Restart your MCP clients so they stop starting Agents-Core.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, ctx: Context | None = None,
         ask: Callable[[str], str] | None = None, interactive: bool | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uninstall", description="Remove Agents-Core from this user account.")
    parser.add_argument("--dry-run", action="store_true", help="list everything that would be removed; change nothing")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask before removing")
    parser.add_argument("--keep-data", action="store_true", help="keep the user library and histories")
    parser.add_argument("--include-credentials", action="store_true",
                        help="with --yes, also remove the credentials it lists")
    args = parser.parse_args(argv)
    ctx = ctx or Context(home=Path(os.path.abspath(Path.home())))
    ask = ask or input
    interactive = sys.stdin.isatty() if interactive is None else interactive

    items = build_plan(ctx, keep_data=args.keep_data)
    print(render(items, dry_run=args.dry_run))
    if args.dry_run or not any(item.action != "keep" for item in items):
        return 0
    if not args.yes:
        if not interactive:
            print("No terminal to confirm on: run again with --yes.", file=sys.stderr)
            return 2
        if not ask("\nRemove everything listed above? [y/N]: ").strip().lower().startswith("y"):
            print("Nothing was changed.")
            return 1
    credentials = False
    if any(item.kind == "credential" for item in items):
        if args.yes:
            credentials = args.include_credentials
        elif interactive:
            credentials = ask("Also remove the credentials listed above? [y/N]: ").strip().lower().startswith("y")
    report = execute(items, credentials=credentials)
    print()
    print(render_report(report, ctx))
    return 1 if report["failed"] or report["refused"] else 0


if __name__ == "__main__":
    # Leave the directories being removed: Windows cannot delete a process's working directory.
    os.chdir(Path.home())
    sys.exit(main())
