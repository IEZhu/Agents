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
* removes unchanged session readiness adapters using this installation's interpreter and their
  unchanged copied runtimes; modified adapters and runtimes are reported and kept;
* removes the state, data, cache and log directories, the one-command installation directory
  (``~/.agents-core``), the autodev trusted clone and the autodev sandbox decoys (fake secrets in
  the home directory and the opt-in decoy keychain item);
* removes the credentials it knows (the ``agents-autodev-github`` keychain item and the autodev
  deploy key directory) only after a second, explicit confirmation, or with
  ``--yes --include-credentials``.

``--dry-run`` lists everything and changes nothing; ``--keep-data`` keeps the user library and
the histories. A path is removed only when it is in the inventory, is neither a symbolic link nor
a junction, and resolves inside the home directory; a directory that a variable of Agents-Core
names (``AGENTS_HOME``, ``AGENTS_SERVICE_DIR``, ``FASTEMBED_CACHE_DIR``,
``AGENTS_USER_FLOWS_DIR``, also read from the installation's ``.env`` and the service's
``service.json``) is removed only when its content shows it is Agents-Core's. Nothing removed
may be, or contain, a filesystem root, the home directory, ``~/Documents``, a profile or XDG
base directory, or the checkout the uninstaller runs from (unless that checkout is the
installation directory). Removal never follows a link and never changes a link's target.

Only the standard library is imported, so a broken virtual environment does not stop it; every
OS command goes through an injectable runner, and the platform, home and environment are
parameters, so tests never touch the real account.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import csv
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable, Mapping, Sequence

import tomllib

from src.update_channel import STANDARD_BRANCHES

SERVER = "Agents-Core"
DESKTOP_SERVER = "Agents-Core-Desktop"  # src/client_paths.py
DESKTOP_DENY_RULES = tuple(f"mcp__{name}__*" for name in (DESKTOP_SERVER, DESKTOP_SERVER.replace("-", "_")))
SERVER_NAMES = (SERVER, DESKTOP_SERVER)
READINESS_SERVER = "Agents-Readiness"  # scripts/install_session_readiness.py
READINESS_SCRIPT = "agents-core-session-readiness.py"
READINESS_MATCHER = "^(startup|clear)$"
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
# src/schedules/decoys.py: decoy names are random per installation and only in its registry; these
# are their shapes, and a decoy file must still hold the marker the registry records for it.
DECOY_DIR_NAME = re.compile(r"\.(?:config|cache|local|tokens|auth|creds|keys|session|cloud|api)"
                            r"(?:-[0-9a-f]{8}|_[0-9a-f]{6}|[0-9a-f]{4})")
DECOY_KEYCHAIN_SERVICE = re.compile(r"(?:api-token|cli-credentials|service-token)-[0-9a-f]{12}")
DECOY_KEYCHAIN_ACCOUNT = re.compile(r"user-[0-9a-f]{8}")
DECOY_MARKER = re.compile(r"[A-Za-z0-9]{48,}")
# Labels and task names with the installation's 16-hex id (src/daemon/state.py, src/user_sync/schedule.py);
# names derived from a state directory that is not named so are added from the inventory (state_dirs).
LAUNCH_AGENT = re.compile(r"^local\.agents-core\.[0-9a-f]{16}(?:\.updater|\.sync|\.scheduler)?\.plist$")
LAUNCH_SUFFIXES = ("", ".updater", ".sync", ".scheduler")  # control.py label, autoupdate, user sync, schedules
SYSTEMD_UNIT = re.compile(r"^agents-core-sync-[0-9a-f]{16}\.(service|timer)$")
CRON_LINE = re.compile(rb" agents-core-sync-[0-9a-f]{16}\s*$")
WINDOWS_TASK = re.compile(r"^agents-core-(?:daemon-[0-9a-f]{16}(?:-updater)?|sync-[0-9a-f]{16})$")
STATE_ID = re.compile(r"^[0-9a-f]{16}$")
# inject_claude_md.copy_backup (19 digits) and its older timestamps (10 digits), which its own
# prune_backups also treats as managed: siblings of the instruction files only.
INSTRUCTION_BACKUP = r"\.backup\.(?:[0-9]{10}|[0-9]{19})$"
# backup_file.py (mkstemp): eight characters a user's own name can have too, so only reported.
MKSTEMP_BACKUP = r"\.backup\.[a-z0-9_]{8}$"
KEEP_IN_STATE = ("user-sync", "unresolved")  # the library's sync state and the fallback journal
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)  # a Windows junction or link
DEFAULT_REPOSITORY = "IEZhu/Agents"  # src/schedules/config.py DEFAULTS['repository']
SETTINGS = ("FASTEMBED_CACHE_DIR", "AGENTS_USER_FLOWS_DIR")  # also read from the installation's .env
# What fastembed (Hugging Face hub layout) keeps in its cache directory.
FASTEMBED_ENTRY = re.compile(r"^(?:models--.+|blobs|local|\.locks|tmp.*|CACHEDIR\.TAG|\.DS_Store)$")
LIBRARY_MARKER = ".agents-library.json"  # src/user_library.py MARKER

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


def run_command(argv: Sequence[str], *, input: bytes | None = None,
                env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    """No shell; inherit the environment unless a caller supplies a scoped copy. Missing programs return 127."""
    given = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(list(argv), capture_output=True, timeout=60, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=env, **given)
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
    # SETTINGS from the installation's .env (build_plan); the process environment wins over them.
    settings: dict = field(default_factory=dict)
    # Paths no removal may equal or contain besides protected(): the kept source checkout.
    kept_roots: list = field(default_factory=list)

    def expand(self, value: str) -> Path:
        """``value`` as an absolute path; ``~`` is this context's home, not the process's."""
        if value == "~" or value.startswith(("~/", "~\\")):
            value = str(self.home) + value[1:]
        return Path(os.path.abspath(value))

    def env_path(self, name: str) -> Path | None:
        value = self.environ.get(name, "").strip()
        return self.expand(value) if value else None

    def setting_path(self, name: str) -> Path | None:
        """A variable from the environment, else from the installation's ``.env``."""
        value = self.environ.get(name, "").strip() or str(self.settings.get(name, "")).strip()
        return self.expand(value) if value else None

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    @property
    def backup_root(self) -> Path:
        return self.home / ".agents-core-uninstall-backups" / self.stamp

    def protected(self) -> list[Path]:
        """Directories no removal may be or contain. They never widen where a removal may go."""
        found = [self.home, self.home / "Documents", *self.kept_roots]
        for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                     "XDG_DATA_HOME", "XDG_CACHE_HOME"):
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


def _contains(outer: str, inner: str) -> bool:
    """Whether the real path ``outer`` is ``inner`` or one of its parents."""
    return inner == outer or inner.startswith(outer.rstrip(os.sep) + os.sep)


def _is_link(path: Path) -> bool:
    """A symbolic link, or any Windows reparse point such as a junction (``Path.is_junction`` is 3.12+)."""
    try:
        status = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status, "st_file_attributes", 0) & REPARSE_POINT)


def check_removable(ctx: Context, path: Path, *, owned: bool = False) -> None:
    """Refuse a link, a root or protected directory (or a parent of one), and, unless the caller
    showed by its content or exact name that ``path`` is Agents-Core's (``owned``), anything
    outside the home directory."""
    if _is_link(path):
        raise Refused(f"{path} is a symbolic link or junction; remove it yourself after checking where it points")
    real = _real(path)
    if len(Path(real).parts) < 3:
        raise Refused(f"{path} is a root directory, not an Agents-Core path")
    for root in ctx.protected():
        if _contains(real, _real(root)):
            raise Refused(f"{path} is or contains {root}, which is never removed")
    if not owned and not _within(path, [ctx.home]):
        raise Refused(f"{path} resolves outside the home directory")


def _make_writable(directory: Path, bits: int = stat.S_IRWXU) -> None:
    """Give the owner ``bits`` on a real directory (never a link: chmod would follow it).

    Autodev environments are read-only directories (src/schedules/repos.py _read_only), and on
    POSIX deleting a child needs write permission on its parent.
    """
    if os.name == "nt" or _is_link(directory):
        return
    mode = stat.S_IMODE(os.lstat(directory).st_mode)
    if mode & bits != bits:
        if os.chmod in os.supports_follow_symlinks:
            os.chmod(directory, mode | bits, follow_symlinks=False)
        else:
            os.chmod(directory, mode | bits)


def _unlink_file(path: Path) -> None:
    """Remove a regular file; Windows refuses read-only files (Git's objects), so clear that flag."""
    try:
        os.unlink(path)
    except PermissionError:
        if os.name != "nt" or _is_link(path):
            raise
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)


def _unlink_link(path: Path) -> None:
    """Remove a link itself, never what it points to (a directory link or junction on Windows needs rmdir)."""
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError):
        os.rmdir(path)


def _remove_entry(path: Path) -> None:
    """Remove ``path`` and everything below it without following any link or junction."""
    if _is_link(path):
        _unlink_link(path)
    elif path.is_dir():
        _make_writable(path)
        with os.scandir(path) as entries:
            children = [Path(entry.path) for entry in entries]
        for child in children:
            _remove_entry(child)
        try:
            os.rmdir(path)
        except PermissionError:
            if os.name != "nt":
                raise
            os.chmod(path, stat.S_IWRITE)
            os.rmdir(path)
    elif os.path.lexists(path):
        _unlink_file(path)


def remove_path(ctx: Context, path: Path, *, owned: bool = False) -> str:
    check_removable(ctx, path, owned=owned)
    _remove_entry(path)
    return "removed"


def remove_marker(marker: Path) -> str:
    """The one file removed from a kept checkout: its pointer to the service's state directory."""
    if _is_link(marker):
        raise Refused(f"{marker} is a symbolic link; remove it yourself")
    marker.unlink()
    return "removed"


def remove_except(ctx: Context, root: Path, kept: Sequence[Path], *, owned: bool = False) -> str:
    """Remove ``root`` except the ``kept`` paths inside it (and the directories that lead to them)."""
    check_removable(ctx, root, owned=owned)
    kept_real = [_real(path) for path in kept]
    if not kept_real:
        return remove_path(ctx, root, owned=owned)

    def prune(directory: Path) -> None:
        _make_writable(directory)
        for child in list(directory.iterdir()):
            real = _real(child)
            if real in kept_real:
                continue
            if _is_link(child):  # inside a removed tree, e.g. .venv/bin/python: the link goes, its target stays
                _unlink_link(child)
            elif child.is_dir() and any(k.startswith(real + os.sep) for k in kept_real):
                prune(child)
            else:
                _remove_entry(child)

    prune(root)
    return "removed; kept " + ", ".join(str(path) for path in kept)


def _private_dir(path: Path) -> None:
    """Create ``path`` as a directory only its owner can enter (0700), refusing a link."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if _is_link(path) or not path.is_dir():
        raise Refused(f"{path} is not a plain directory; backups are not written through it")
    if os.name != "nt":
        os.chmod(path, 0o700)


def _write_private(path: Path, content: bytes, *, exclusive: bool) -> None:
    """Write ``content`` to a 0600 file, never through a link."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)


class Backups:
    """Copies of every edited file, under ``~/.agents-core-uninstall-backups/<time>``, with a manifest.

    The copies can hold MCP tokens (client configurations, the crontab), so the directories are
    0700 and the files 0600 whatever the umask.
    """

    def __init__(self, ctx: Context):
        self.ctx, self.records = ctx, []

    def save(self, path: Path, content: bytes) -> Path:
        root = self.ctx.backup_root
        _private_dir(root.parent)
        _private_dir(root)
        target = root / f"{len(self.records) + 1:03d}-{path.name}"
        _write_private(target, content, exclusive=True)
        self.records.append({"original": str(path), "backup": str(target)})
        manifest = json.dumps(self.records, indent=2, ensure_ascii=False) + "\n"
        _write_private(root / "manifest.json", manifest.encode("utf-8"), exclusive=False)
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


def _spans(node: _Node, removed: set[int]) -> list[tuple[int, int]]:
    """The text ranges that remove the members ``removed`` of one object or array with their commas."""
    starts, ends = _starts(node), _ends(node)
    kept = [index for index in range(len(starts)) if index not in removed]
    if not kept:
        return [(node.start + 1, node.end - 1)]
    spans = []
    for index in sorted(removed):
        after = next((other for other in kept if other > index), None)
        if after is not None:  # up to the next kept member: this member and the comma after it
            spans.append((starts[index], starts[after]))
        else:  # the last ones: from the end of the last kept member, with the comma before
            spans.append((ends[max(other for other in kept if other < index)], ends[index]))
    return spans


def _find_all(node: _Node, path: Sequence, value_test=None) -> list[tuple[_Node, int]]:
    """Every member at ``path`` (``*``: any key, ``[]``: matching elements, integers: array indices)."""
    if not path:
        return []
    head, rest = path[0], path[1:]
    found: list[tuple[_Node, int]] = []
    if node.kind == "object":
        for index, (key, _, child) in enumerate(node.members):
            if head != "*" and key != head:
                continue
            found += [(node, index)] if not rest else _find_all(child, rest, value_test)
    elif node.kind == "array" and head == "[]" and value_test is not None:
        found += [(node, index) for index, child in enumerate(node.members) if value_test(child)]
    elif node.kind == "array" and isinstance(head, int) and 0 <= head < len(node.members):
        found += [(node, head)] if not rest else _find_all(node.members[head], rest, value_test)
    return found


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
    elif isinstance(document, list) and isinstance(head, int) and 0 <= head < len(document):
        if rest:
            _drop(document[head], rest, value_test)
        else:
            del document[head]


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
    # One parse: every match is cut from the original offsets, from the end of the text backwards.
    root = _Parser(text).value(0)
    removed: dict[int, tuple[_Node, set[int]]] = {}

    def mark(found: list[tuple[_Node, int]]) -> None:
        for node, index in found:
            removed.setdefault(id(node), (node, set()))[1].add(index)

    for path, test in removals:
        _drop(expected, path, (lambda value, test=test: test(json.dumps(value))) if test else None)
        mark(_find_all(root, path, (lambda node, test=test: test(text[node.start:node.end])) if test else None))
    if not removed:
        return content
    for path in drop_if_empty:
        current = expected
        for key in path[:-1]:
            current = current.get(key) if isinstance(current, dict) else None
        if isinstance(current, dict) and current.get(path[-1]) in ({}, []):
            del current[path[-1]]
            mark(_find_all(root, path))
    spans = sorted(span for node, indexes in removed.values() for span in _spans(node, indexes))
    merged: list[list[int]] = []
    for start, stop in spans:  # spans nest or are disjoint: a removed member may hold removed members
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    for start, stop in reversed(merged):
        text = text[:start] + text[stop:]
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
    # Comment and blank lines after the last key of a skipped section: they may introduce the next
    # section (a user's note above it), so they are kept from the first comment on.
    pending: list[str] = []

    def flush() -> None:
        comments = [index for index, line in enumerate(pending) if line.strip().startswith("#")]
        kept.extend(pending[comments[0]:] if comments else [])
        pending.clear()

    for line in lines:
        if re.match(r"\s*\[", line):
            header = line.strip().split("#", 1)[0].strip()
            try:  # tomllib reads the header, so quoted keys mean what they mean in TOML
                probe = tomllib.loads(header + "\n__agents_probe = true\n")
                servers = probe.get("mcp_servers", {})
                section_is_ours = isinstance(servers, dict) and name in servers
            except tomllib.TOMLDecodeError:
                section_is_ours = False
            if skip and not section_is_ours:
                flush()
            pending.clear()
            skip = section_is_ours
        if not skip:
            kept.append(line)
        elif not line.strip() or line.strip().startswith("#"):
            pending.append(line)
        elif not re.match(r"\s*\[", line):
            pending.clear()  # a key of our section: the comments before it were part of the section
    if skip:  # the section ended the file: drop the blank line the migration put before it
        flush()
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


_FENCE = re.compile(rb"(`{3,}|~{3,})")


def marker_lines(content: bytes) -> list[tuple[bytes, int, int]]:
    """(marker, start, end) of each routing marker that is a whole line outside a fenced code block."""
    every = {marker for pair in MARKERS for marker in pair}
    found, fence, offset = [], None, 0
    for line in content.splitlines(keepends=True):
        body = line.rstrip(b"\r\n")
        stripped = body.lstrip(b" ")
        opening = _FENCE.match(stripped) if len(body) - len(stripped) <= 3 else None
        if fence is not None:
            if opening and opening.group(1)[:1] == fence[:1] and len(opening.group(1)) >= len(fence) \
                    and not stripped[len(opening.group(1)):].strip():
                fence = None
        elif opening:
            fence = opening.group(1)
        elif body.rstrip() in every:
            found.append((body.rstrip(), offset, offset + len(body)))
        offset += len(line)
    return found


def remove_routing_block(content: bytes) -> bytes | None:
    """The file without the managed routing section and the newline the installer added after it.

    Only marker lines outside fenced code blocks count: a quoted example of the markers is text.
    """
    markers = marker_lines(content)
    if not markers:
        return content
    present = [(begin, end) for begin, end in MARKERS if any(marker in (begin, end) for marker, _, _ in markers)]
    if len(present) != 1:
        raise ValueError("mixed routing marker versions; remove the section yourself")
    begin, end = present[0]
    begins = [(start, stop) for marker, start, stop in markers if marker == begin]
    ends = [(start, stop) for marker, start, stop in markers if marker == end]
    if len(begins) != 1 or len(ends) != 1 or ends[0][0] < begins[0][0]:
        raise ValueError("incomplete routing markers; remove the section yourself")
    start, stop = begins[0][0], ends[0][1]
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


def local_work(ctx: Context, checkout: Path) -> str | None:
    """Why ``checkout`` may hold someone's work, or None for an install.sh clone.

    install.sh clones a published channel and never commits. Keep dirty/custom-branch
    checkouts, commits outside known remote-tracking history, and ignored material outside
    the installer's .env and .venv assets. Git failures also keep the whole checkout.
    """
    base = ["git", "-C", str(checkout)]
    # These offline safety inspections must use this checkout, not a caller's Git context.
    git_env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}

    def git(*arguments: str) -> subprocess.CompletedProcess:
        """Inspect the selected checkout without inherited Git environment overrides."""
        return ctx.runner([*base, *arguments], env=git_env)

    status = git("status", "--porcelain", "--untracked-files=normal")
    branches = git("for-each-ref", "--format=%(refname:short)", "refs/heads")
    if status.returncode != 0 or branches.returncode != 0:
        return "git could not check it for uncommitted work"
    if status.stdout.strip():
        return "it has uncommitted changes or untracked files"
    others = sorted(set(branches.stdout.decode("utf-8", "replace").split()) - set(STANDARD_BRANCHES))
    if others:
        return "it has local branches besides main and dev (" + ", ".join(others[:5]) + ")"
    # Include stashes, tags and reflogs as well as branches and detached HEAD.
    commits = git("rev-list", "--count", "--all", "--reflog", "HEAD", "--not", "--remotes")
    count = commits.stdout.strip()
    if commits.returncode != 0 or not count.isdigit():
        return "git could not check it for local work"
    if int(count):
        return "it has local commits outside known remote-tracking history"
    ignored = git("ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    if ignored.returncode != 0 or (ignored.stdout and not ignored.stdout.endswith(b"\0")):
        return "git could not check it for local work"
    # init_repo creates these two assets. Other ignore patterns (including arbitrary
    # dotfiles and archives) do not establish that their contents belong to the installer.
    if any(entry != b".env" and not entry.startswith(b".venv/")
           for entry in ignored.stdout.split(b"\0") if entry):
        return "it has ignored files outside the installer's .env and .venv assets"
    return None


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


def owns_state(path: Path) -> bool:
    """A service state directory by its content: ``service.json`` as Controller.install writes it."""
    config = _read_json(path / "service.json") if path.is_dir() and not _is_link(path) else None
    return isinstance(config, dict) and isinstance(config.get("installation"), str)


def owns_cache(path: Path) -> bool:
    """A fastembed cache by its content: model directories and nothing else."""
    try:
        names = [child.name for child in path.iterdir()]
    except OSError:
        return False
    return any(name.startswith("models--") for name in names) and all(FASTEMBED_ENTRY.match(name) for name in names)


def owns_library(path: Path) -> bool:
    """The user library by its marker file (src/user_library.py)."""
    marker = path / LIBRARY_MARKER
    return marker.is_file() and not _is_link(marker)


def read_settings(path: Path, keys: Sequence[str] = SETTINGS) -> dict[str, str]:
    """``keys`` from a ``.env`` file (python-dotenv's simple form). Other lines are never kept or shown."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() not in keys:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    return values


def state_dirs(ctx: Context, installations: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    for root in state_roots(ctx):
        # On Windows %USERPROFILE%\.agents-core holds the states and can also be the installation.
        if root.is_dir():
            found += [child for child in sorted(root.iterdir()) if STATE_ID.match(child.name) and child.is_dir()]
    override = ctx.env_path("AGENTS_SERVICE_DIR")
    if override and owns_state(override):
        found.append(override)
    for installation in installations:
        try:
            marker = json.loads((installation / "data/.shared-service.json").read_text(encoding="utf-8"))
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


def _git_layout(path: Path) -> tuple[bool, list[str]] | None:
    """(bare, remote URLs) of a Git repository at ``path``, or None when it is not one."""
    if (path / ".git/config").is_file():
        config, bare = path / ".git/config", False
    elif (path / "config").is_file() and (path / "HEAD").is_file() and (path / "objects").is_dir():
        config, bare = path / "config", True
    else:
        return None
    if _is_link(config):
        return None
    section, urls = "", []
    for raw in config.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        header = re.match(r"\[\s*([A-Za-z0-9.-]+)", line)
        if header:
            section = header.group(1).lower()
            continue
        key, separator, value = line.partition("=")
        if separator and section == "remote" and key.strip().lower() == "url":
            urls.append(value.strip().strip('"'))
        if separator and section == "core" and key.strip().lower() == "bare":
            bare = bare and value.strip().lower() in ("true", "yes", "on", "1")
    return bare, urls


def _same_repository(url: str, repository: str) -> bool:
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?",
                         url, re.IGNORECASE)
    return bool(match) and match.group(1).lower() == repository.lower()


def clone_problem(ctx: Context, path: Path, repository: str, installations: Sequence[Path]) -> str | None:
    """Why ``path`` is not the autodev trusted clone to remove, or None when it is.

    Like src/schedules/config.py runner_location, it must lie neither under nor above
    ``~/Documents``, an installation or the checkout the uninstaller runs from, and it must not
    contain the home directory. It must also have the runner's layout: a bare repository that keeps
    main in ``refs/remotes/origin/main`` and has no remote other than ``repository`` on GitHub
    (src/schedules/repos.py TrustedRepository fetches by URL and refuses a working tree). A working
    tree is someone's own clone, with work the runner never made, so it is never removed.
    """
    real = _real(path)
    if _contains(real, _real(ctx.home)):
        return "it is or contains the home directory"
    for name, root in (("~/Documents", ctx.home / "Documents"), ("the checkout the uninstaller runs from", ctx.source_root),
                       *(("the installation", installation) for installation in installations)):
        other = _real(root)
        if _contains(real, other) or _contains(other, real):
            return f"it lies under or contains {name} ({root})"
    layout = _git_layout(path)
    if layout is None:
        return "it is not a Git repository"
    bare, urls = layout
    if not bare:
        return "it is a working tree, not the runner's bare repository"
    if not all(_same_repository(url, repository) for url in urls):
        return f"its remote is not {repository}"
    pinned = path / "refs/remotes/origin/main"
    packed = path / "packed-refs"
    if bare and (pinned.is_file() or (packed.is_file() and b" refs/remotes/origin/main" in packed.read_bytes())):
        return None
    return "it has no refs/remotes/origin/main, as the runner's bare repository has"


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
    """Check readable file bytes for Agents-Core server names or routing markers."""
    try:
        content = path.read_bytes()
    except OSError:
        return False
    return any(name.encode() in content for name in (*SERVER_NAMES, "agents-core")) or any(
        begin in content for begin, _ in MARKERS)


# --- optional session readiness adapters -------------------------------------------------------

def _readiness_python(value: str, installation: Path) -> bool:
    """Match the venv path lexically: its Python is commonly a link to a system interpreter."""
    if not isinstance(value, str) or not os.path.isabs(value):
        return False
    path = Path(os.path.abspath(value))
    parents = [installation / ".venv/bin", installation / ".venv/Scripts"]
    return (any(os.path.normcase(str(path.parent)) == os.path.normcase(os.path.abspath(parent)) for parent in parents)
            and re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?(?:w?\.exe)?", path.name, re.IGNORECASE) is not None)


def _readiness_arguments(command: str, *, windows: bool) -> list[str] | None:
    """Decode only the exact PowerShell wrapper the readiness installer generates."""
    if not isinstance(command, str):
        return None
    try:
        arguments = shlex.split(command, posix=not windows)
    except ValueError:
        return None
    if arguments[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]:
        if len(arguments) != 5:
            return None
        try:
            decoded = base64.b64decode(arguments[4], validate=True).decode("utf-16-le")
        except (binascii.Error, UnicodeDecodeError):
            return None
        match = re.search(r"\$readinessInputJson \| & (.+); exit \$LASTEXITCODE$", decoded)
        if not match:
            return None
        arguments = [value.replace("''", "'") for value in re.findall(r"'((?:[^']|'')*)'", match[1])]
        invocation = " ".join("'" + value.replace("'", "''") + "'" for value in arguments)
        expected = ("[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); "
                    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                    "$OutputEncoding = [Console]::OutputEncoding; "
                    "$readinessInputJson = [Console]::In.ReadToEnd(); "
                    "$readinessInputJson | & " + invocation + "; exit $LASTEXITCODE")
        if decoded != expected:
            return None
    return arguments


def _readiness_handler(handler, client: str, script: Path, installation: Path, *, windows: bool) -> bool:
    """Match an unchanged generated handler using the selected installation's interpreter."""
    if not isinstance(handler, dict):
        return False
    expected = {"command": handler.get("command"), "timeout": 5}
    if client != "cursor":
        expected["type"] = "command"
    if handler != expected:
        return False
    arguments = _readiness_arguments(handler.get("command"), windows=windows)
    options = ["--client", client] if client in ("cursor", "antigravity") else []
    return (arguments is not None and len(arguments) == 2 + len(options)
            and _readiness_python(arguments[0], installation)
            and arguments[1] == str(script) and arguments[2:] == options)


def remove_readiness(content: bytes, client: str, script: Path, installation: Path, *, windows: bool) -> bytes | None:
    """Remove exact generated adapters, preserving mixed groups and user-modified entries."""
    document = json.loads(content.decode("utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError("the file's root is not a JSON object; edit it yourself")
    removals, empty = [], []
    if client == "desktop":
        servers = document.get("mcpServers")
        entry = servers.get(READINESS_SERVER) if isinstance(servers, dict) else None
        helper = script.with_name("session_readiness_mcp.py")
        if (isinstance(entry, dict) and entry == {"command": entry.get("command"), "args": [str(helper)]}
                and _readiness_python(entry.get("command"), installation)):
            removals.append((("mcpServers", READINESS_SERVER), None))
        empty = [("mcpServers",)]
    else:
        if client == "antigravity":
            prefix = ("agents-core-readiness", "SessionStart")
            entry = document.get(prefix[0])
            items = entry.get(prefix[1]) if isinstance(entry, dict) else None
        else:
            prefix = ("hooks", "sessionStart" if client == "cursor" else "SessionStart")
            hooks = document.get("hooks")
            items = hooks.get(prefix[1]) if isinstance(hooks, dict) else None
        if isinstance(items, list):
            # Descending indices keep the expected parsed edit aligned with original text offsets.
            for index in range(len(items) - 1, -1, -1):
                item = items[index]
                if client in ("claude", "codex"):
                    if (not isinstance(item, dict) or set(item) != {"matcher", "hooks"}
                            or item["matcher"] != READINESS_MATCHER or not isinstance(item["hooks"], list)):
                        continue
                    owned = [i for i, handler in enumerate(item["hooks"])
                             if _readiness_handler(handler, client, script, installation, windows=windows)]
                    if owned and len(owned) == len(item["hooks"]):
                        removals.append(((*prefix, index), None))
                    else:
                        removals += [((*prefix, index, "hooks", i), None) for i in reversed(owned)]
                elif _readiness_handler(item, client, script, installation, windows=windows):
                    removals.append(((*prefix, index), None))
            if client == "antigravity" and removals and len(removals) == len(items) \
                    and set(entry) == {"enabled", "SessionStart"} and entry["enabled"] is True:
                removals = [((prefix[0],), None)]
        empty = [prefix] + ([("hooks",)] if client != "antigravity" else [])
    return remove_json_members(content, removals, drop_if_empty=empty, delete_when=[{}])


def _readiness_referenced(content: bytes | None, path: Path) -> bool:
    """Decode quoted commands without running them; uncertain references keep the runtime."""
    if content is None:
        return False
    try:
        document = json.loads(content.decode("utf-8-sig"))
    except ValueError:
        return True
    if not isinstance(document, dict):
        return True

    def refers(value, *, command=False) -> bool:
        """Scan nested values for the runtime path, treating uncertain commands as references."""
        if isinstance(value, dict):
            return any(refers(child, command=key == "command") for key, child in value.items())
        if isinstance(value, list):
            return any(refers(child, command=command) for child in value)
        if isinstance(value, str):
            if str(path) in value:
                return True
            if command:
                # PowerShell single-quoted literals escape apostrophes by doubling them.
                if any(str(path) in literal.replace("''", "'")
                       for literal in re.findall(r"'((?:[^']|'')*)'", value)):
                    return True
                encoded = re.search(r"-EncodedCommand\b", value, re.IGNORECASE) is not None
                if encoded and not value.startswith("powershell.exe -NoProfile -NonInteractive -EncodedCommand "):
                    return True  # an unfamiliar wrapper cannot establish that the runtime is unused
                arguments = _readiness_arguments(value, windows=encoded)
                return arguments is None or any(str(path) in argument for argument in arguments)
        return False
    return refers(document)


def readiness_items(ctx: Context, backups: Backups, installation: Path) -> list[Item]:
    """Inventory readiness separately: its MCP server name alone does not establish ownership."""
    source = "scripts/install_session_readiness.py"
    profiles = [("claude", ctx.env_path("CLAUDE_CONFIG_DIR") or ctx.home / ".claude", "settings.json"),
                ("codex", ctx.env_path("CODEX_HOME") or ctx.home / ".codex", "hooks.json"),
                ("cursor", ctx.home / ".cursor", "hooks.json"),
                ("antigravity", ctx.home / ".gemini/config", "hooks.json")]
    targets = [(client, directory / name, directory / "hooks" / READINESS_SCRIPT)
               for client, directory, name in profiles]
    targets += [("desktop", path, path.parent / "agents-core-readiness/hooks" / READINESS_SCRIPT)
                for client, path in client_files(ctx) if client == "desktop"]
    items, seen = [], set()
    configurations, remaining, candidates = [], [], []
    uncertain = False
    for client, target, script in targets:
        if _real(target) in seen:
            continue
        seen.add(_real(target))
        configurations.append(target)  # include missing files in the execution-time recheck
        transform = lambda content, client=client, script=script: remove_readiness(
            content, client, script, installation, windows=ctx.windows)
        try:
            original = target.read_bytes()
            updated = transform(original)
        except FileNotFoundError:
            uncertain |= os.path.lexists(target)
            continue
        except (OSError, ValueError, TypeError):
            uncertain = True
            continue  # an invalid or unreadable client configuration never proves ownership
        uncertain |= _is_link(target)
        remaining.append(updated)
        changed = updated != original
        if changed:
            items.append(Item("registration", str(target), "edit", source,
                              "unchanged readiness adapters using this installation; a backup first",
                              lambda target=target, transform=transform: edit_file(ctx, backups, target, transform)))
        elif _readiness_referenced(original, script if client != "desktop" else script.with_name("session_readiness_mcp.py")):
            items.append(Item("registration", str(target), "keep", source,
                              "readiness adapter is modified or belongs to another installation; remove it yourself"))
        files = [(script, "session_readiness.py")] if client != "desktop" else [
            (script.with_name(name), name) for name in ("session_readiness.py", "session_readiness_mcp.py")]
        candidates.append((client, target, script, changed, files))

    # A retained adapter in any client can use another client's copied runtime.
    for client, target, script, changed, files in candidates:
        for runtime, name in files:
            if not os.path.lexists(runtime):
                continue
            references = [runtime]
            if client == "desktop":
                references.append(script.with_name("session_readiness_mcp.py"))
            try:
                expected = (installation / "scripts" / name).read_bytes()
                owned = (changed and not _is_link(target) and not _is_link(runtime) and runtime.read_bytes() == expected
                         and not uncertain and not any(_readiness_referenced(content, path)
                                                       for content in remaining for path in references))
            except (OSError, ValueError):
                owned = False
            if not owned:
                items.append(Item("data", str(runtime), "keep", source,
                                  "readiness runtime is modified, still referenced, or its ownership is unverified"))
                continue

            def remove_runtime(runtime=runtime, expected=expected, references=references):
                """Recheck runtime bytes and every configuration before removing an unreferenced copy."""
                check_removable(ctx, runtime)
                if runtime.read_bytes() != expected:
                    raise Refused("readiness runtime changed; remove it yourself")
                for configuration in configurations:
                    if _is_link(configuration):
                        raise Refused("readiness configuration became a symbolic link; remove the runtime yourself")
                    try:
                        current = configuration.read_bytes()
                    except FileNotFoundError:
                        if not os.path.lexists(configuration):
                            continue
                        raise Refused("readiness configuration cannot be read; remove the runtime yourself")
                    except OSError as error:
                        raise Refused("readiness configuration cannot be read; remove the runtime yourself") from error
                    if any(_readiness_referenced(current, path) for path in references):
                        raise Refused("readiness runtime is still referenced or its use is uncertain; remove it yourself")
                return remove_path(ctx, runtime)
            items.append(Item("data", str(runtime), "remove", source, "unchanged copied readiness runtime", remove_runtime))
    return items


def _is_decoy(path: Path, marker: str) -> bool:
    """A small regular file, never a link, that holds its registered decoy marker."""
    try:
        status = os.lstat(path)
        return stat.S_ISREG(status.st_mode) and status.st_size < 4096 and marker.encode() in path.read_bytes()
    except OSError:
        return False


def _decoy_registries(states: Sequence[Path]) -> list[dict]:
    return [registry for registry in (_read_json(state / "schedules/decoys.json") for state in states)
            if isinstance(registry, dict)]


def decoy_items(ctx: Context, states: Sequence[Path]) -> list[Item]:
    """The autodev sandbox decoys each state's ``schedules/decoys.json`` registers: the random
    decoy directory directly in the home directory, the files outside it that still hold their
    registered marker, and the opt-in decoy keychain item. They are fake secrets, so they go with
    the data, not the credentials."""
    items: list[Item] = []
    source = "src/schedules/decoys.py ensure (autodev sandbox decoys)"
    registries = _decoy_registries(states)
    directories: list[Path] = []
    for registry in registries:
        value = registry.get("directory")
        if not isinstance(value, str) or not os.path.isabs(value):
            continue
        directory = Path(value)
        if DECOY_DIR_NAME.fullmatch(directory.name) and _real(directory.parent) == _real(ctx.home) \
                and not _is_link(directory) and directory.is_dir() and directory not in directories:
            directories.append(directory)
    listed: set[str] = set()
    keychains: list[tuple[str, str]] = []
    for registry in registries:
        item = registry.get("keychain")
        if isinstance(item, dict) and isinstance(item.get("service"), str) and isinstance(item.get("account"), str) \
                and DECOY_KEYCHAIN_SERVICE.fullmatch(item["service"]) and DECOY_KEYCHAIN_ACCOUNT.fullmatch(item["account"]):
            if (item["service"], item["account"]) not in keychains:
                keychains.append((item["service"], item["account"]))
        for entry in registry.get("files") if isinstance(registry.get("files"), list) else []:
            value = entry.get("path") if isinstance(entry, dict) else None
            marker = entry.get("marker") if isinstance(entry, dict) else None
            if not isinstance(value, str) or not os.path.isabs(value) or not isinstance(marker, str) \
                    or not DECOY_MARKER.fullmatch(marker):
                continue
            path = Path(value)
            if _real(path) in listed or _within(path, directories) or not _within(path, [ctx.home]) \
                    or not _is_decoy(path, marker):
                continue
            listed.add(_real(path))
            items.append(Item("data", str(path), "remove", source, "an autodev sandbox decoy (a fake secret)",
                              lambda path=path: remove_path(ctx, path)))
    for directory in directories:
        items.append(Item("data", str(directory), "remove", source, "the autodev sandbox decoys (fake secrets)",
                          lambda directory=directory: remove_path(ctx, directory)))
    if ctx.platform != "darwin":
        return items
    for service, account in keychains:  # only a registry that recorded the opt-in item names it
        found = ctx.runner(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account])
        if found.returncode != 0:
            continue

        def unkey(service=service, account=account):
            result = ctx.runner(["/usr/bin/security", "delete-generic-password", "-s", service, "-a", account])
            if result.returncode:
                raise RuntimeError(f"security delete-generic-password exited {result.returncode}")
            return "removed"
        items.append(Item("data", f"keychain item {service}", "remove", source,
                          "the opt-in decoy keychain item (a fake secret)", unkey))
    return items


def build_plan(ctx: Context, *, keep_data: bool = False) -> list[Item]:
    """Inventory uninstall actions and deferred operations without applying them."""
    items: list[Item] = []
    backups = Backups(ctx)
    installation = installation_dir(ctx)
    installations = [path for path in dict.fromkeys([installation, ctx.source_root]) if is_checkout(path)]
    if is_checkout(ctx.source_root) and _real(ctx.source_root) != _real(installation):
        ctx.kept_roots = [ctx.source_root]  # never removed, nor anything that contains it
    for checkout_root in installations:  # the settings the server reads from .env (src/server.py)
        for key, value in read_settings(checkout_root / ".env").items():
            ctx.settings.setdefault(key, value)
    states = state_dirs(ctx, installations)

    # --- services
    if ctx.platform == "darwin":
        agents = ctx.home / "Library/LaunchAgents"
        # src/daemon/control.py label, autoupdate.label, src/schedules/launchagent.label: the state directory's name
        derived = {f"local.agents-core.{state.name}{suffix}.plist" for state in states for suffix in LAUNCH_SUFFIXES}
        for plist in sorted(agents.iterdir()) if agents.is_dir() else []:
            if not (LAUNCH_AGENT.match(plist.name) or plist.name in derived):
                continue
            label = plist.name[:-len(".plist")]

            def stop(plist=plist, label=label):
                check_removable(ctx, plist)  # a linked plist (nix, home-manager) stays loaded and in place
                ctx.runner(["/bin/launchctl", "bootout", f"gui/{ctx.uid}/{label}"])
                return remove_path(ctx, plist)
            items.append(Item("service", str(plist), "remove",
                              "src/daemon/service.py Launchd.write, src/daemon/autoupdate.py write_plist, "
                              "src/user_sync/schedule.py LaunchAgent, src/schedules/launchagent.py Scheduler.install",
                              f"launchctl bootout gui/{ctx.uid}/{label}", stop))
    elif ctx.windows:
        query = ctx.runner(["schtasks", "/Query", "/FO", "CSV", "/NH"])
        names = []
        # src/daemon/service.py TaskScheduler.name and updater_name: the state directory's name
        derived = {f"agents-core-daemon-{state.name}{suffix}" for state in states for suffix in ("", "-updater")}
        if query.returncode == 0:
            text = (query.stdout or b"").decode("utf-8", "replace")
            for row in csv.reader(io.StringIO(text)):
                name = row[0].strip() if row else ""
                bare = name[1:] if name.startswith("\\") else name
                if (WINDOWS_TASK.match(bare) or bare in derived) and name not in names:
                    names.append(name)
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
        links = sorted(path for path in wants.glob("agents-core-sync-*.timer")
                       if SYSTEMD_UNIT.match(path.name)) if wants.is_dir() else []
        if files or links:
            def disable(files=files, links=links):
                for path in files:  # all checked before systemctl touches any of them
                    check_removable(ctx, path, owned=True)  # the exact unit name shows it is ours
                for path in links:  # `systemctl enable` links to our unit: the link goes, nothing else
                    check_removable(ctx, path.parent, owned=True)
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
    items += readiness_items(ctx, backups, installation)
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
        if path.is_file() and marker_lines(path.read_bytes()):
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
    override = ctx.env_path("AGENTS_SERVICE_DIR")
    for state in states:
        if any(_within(state, [root]) for root in covered):
            continue
        kept = [state / name for name in KEEP_IN_STATE if (state / name).exists()] if keep_data else []
        owned = override is not None and _real(state) == _real(override)  # its service.json shows it is ours
        items.append(Item("data", str(state), "remove", "src/daemon/control.py install --state / AGENTS_SERVICE_DIR",
                          "a service state directory outside the default location",
                          lambda state=state, kept=kept, owned=owned: remove_except(ctx, state, kept, owned=owned)))
    if override and override.is_dir() and not any(_real(override) == _real(state) for state in states):
        items.append(Item("data", str(override), "keep", "AGENTS_SERVICE_DIR",
                          "named by AGENTS_SERVICE_DIR, but it has no service.json of Agents-Core; delete it yourself"))
    flows = ctx.setting_path("AGENTS_USER_FLOWS_DIR")
    if flows and (flows.is_dir() or _is_link(flows)):
        if keep_data:
            items.append(Item("data", str(flows), "keep", "src/user_flows.py (AGENTS_USER_FLOWS_DIR)",
                              "the user library (--keep-data)"))
        elif owns_library(flows) or _is_link(flows):
            items.append(Item("data", str(flows), "remove", "src/user_flows.py (AGENTS_USER_FLOWS_DIR)",
                              "the user library and its version history",
                              lambda: remove_path(ctx, flows, owned=True)))
        else:
            items.append(Item("data", str(flows), "keep", "src/user_flows.py (AGENTS_USER_FLOWS_DIR)",
                              f"named by AGENTS_USER_FLOWS_DIR, but it has no {LIBRARY_MARKER}; delete it yourself"))
    for root in workspace_roots(states):
        written = [name for name in ("history.md", "history", "data/memory", "logs") if (root / name).exists()]
        if written:
            items.append(Item("data", str(root), "keep", "src/memory (log_interaction, describe_repo)",
                              "project files, possibly tracked by Git: " + ", ".join(written)
                              + " and the Repository Memory section of CLAUDE.md; delete them yourself"))
    autodev = ctx.home / ".local/share/agents-autodev"
    clones = [autodev] if autodev.is_dir() or _is_link(autodev) else []
    for state in states:
        configured = _read_json(state / "schedules/autodev.json")
        clone = configured.get("clone") if isinstance(configured, dict) else None
        if not isinstance(clone, str) or not clone.strip():
            continue
        repository = configured.get("repository")
        repository = repository if isinstance(repository, str) and "/" in repository else DEFAULT_REPOSITORY
        path = ctx.expand(clone.strip()) if clone.strip().startswith("~") or os.path.isabs(clone.strip()) else None
        if path is None:
            items.append(Item("data", clone, "keep", "src/schedules/config.py (autodev.json clone)",
                              "not an absolute path, so not the autodev trusted repository"))
            continue
        if not (path.exists() or _is_link(path)) or any(_within(path, [other]) for other in clones):
            continue
        problem = clone_problem(ctx, path, repository, installations)
        if problem:
            items.append(Item("data", str(path), "keep", "src/schedules/config.py (autodev.json clone)",
                              f"named as the autodev clone, but {problem}; delete it yourself if it is"))
        else:
            clones.append(path)
    for clone in clones:
        items.append(Item("data", str(clone), "remove", "src/schedules/config.py DEFAULTS['clone'] (autodev trusted clone)",
                          "the autodev trusted repository", lambda clone=clone: remove_path(ctx, clone)))
    items += decoy_items(ctx, states)

    # --- caches: the default, the one FASTEMBED_CACHE_DIR names (environment or .env), each service's
    default_cache = ctx.home / ".cache/fastembed"
    caches = [ctx.setting_path("FASTEMBED_CACHE_DIR") or default_cache]
    for state in states:
        config = _read_json(state / "service.json")
        if isinstance(config, dict) and isinstance(config.get("model_cache"), str) and config["model_cache"].strip():
            caches.append(ctx.expand(config["model_cache"].strip()))  # src/daemon/control.py install
    for cache in {_real(cache): cache for cache in caches}.values():
        if not (cache.is_dir() or _is_link(cache)):
            continue
        source = "src/engine/config.py FASTEMBED_CACHE_DIR, scripts/init_repo.sh (pre-download)"
        if _real(cache) == _real(default_cache) or _is_link(cache) or owns_cache(cache):
            owned = _real(cache) != _real(default_cache)
            items.append(Item("cache", str(cache), "remove", source, "embedding models; another program that set "
                              "the same directory downloads its models again",
                              lambda cache=cache, owned=owned: remove_path(ctx, cache, owned=owned)))
        else:
            items.append(Item("cache", str(cache), "keep", source, "configured as the embedding cache, but it holds "
                              "more than fastembed models; delete the models yourself"))

    # --- installation
    unsaved = local_work(ctx, installation) if is_checkout(installation) and ctx.env_path("AGENTS_HOME") else None
    if unsaved:
        items.append(Item("installation", str(installation), "keep", "AGENTS_HOME",
                          f"named by AGENTS_HOME, but {unsaved}, so it may be your own checkout; delete it yourself"))
    elif is_checkout(installation):
        owned = ctx.env_path("AGENTS_HOME") is not None  # is_checkout shows the named directory is ours
        keep = [installation / "flows/.user"] if keep_data and (installation / "flows/.user").exists() else []
        if keep_data:  # states inside the checkout (Windows) keep user sync and the fallback journal too
            keep += [state / name for state in states if _within(state, [installation])
                     for name in KEEP_IN_STATE if (state / name).exists()]
        def remove_installation():
            """Recheck selected-checkout work after confirmation, immediately before removal."""
            check_removable(ctx, installation, owned=owned)
            if owned:
                reason = local_work(ctx, installation)
                if reason:
                    raise Refused(f"checkout changed since planning: {reason}; kept, delete it yourself")
            return remove_except(ctx, installation, keep, owned=owned)

        items.append(Item("installation", str(installation), "remove", "install.sh (git clone into AGENTS_HOME, "
                          "default ~/.agents-core), scripts/init_repo.sh (.venv, .env, data/)",
                          "the one-command installation" + ("; keeps " + ", ".join(
                              str(path.relative_to(installation)) for path in keep) + " (--keep-data)" if keep else ""),
                          remove_installation))
    elif ctx.env_path("AGENTS_HOME") and installation.is_dir():
        items.append(Item("installation", str(installation), "keep", "AGENTS_HOME",
                          "named by AGENTS_HOME, but it is not an Agents-Core checkout; delete it yourself"))
    if ctx.kept_roots:
        items.append(Item("installation", str(ctx.source_root), "keep", "a checkout you cloned yourself",
                          "the uninstaller never deletes the checkout it runs from; delete it yourself"))
        marker = ctx.source_root / "data/.shared-service.json"
        if marker.is_file():
            items.append(Item("data", str(marker), "remove", "src/daemon/control.py Controller.install",
                              "the kept checkout's pointer to the removed service",
                              lambda: remove_marker(marker)))

    # --- installer backups next to the files it edited
    instruction_files = [claude_home / "CLAUDE.md", codex_home / "AGENTS.md", codex_home / "AGENTS.override.md",
                         reminder, index]  # inject_claude_md.write_with_backup
    config_files = [path for _, path in client_files(ctx)]  # scripts/_helpers/backup_file.py
    listed: set[str] = set()
    for files, suffix, ours in ((instruction_files, INSTRUCTION_BACKUP, True), (config_files, MKSTEMP_BACKUP, False)):
        for path in files:
            if not path.parent.is_dir():
                continue
            pattern = re.compile(re.escape(path.name) + suffix)
            for child in sorted(path.parent.iterdir()):
                if not pattern.match(child.name) or not child.is_file() or _is_link(child) or _real(child) in listed:
                    continue
                listed.add(_real(child))
                if ours:
                    items.append(Item("backups", str(child), "remove", "scripts/_helpers/inject_claude_md.py copy_backup",
                                      "a copy the installer made before its edit",
                                      lambda child=child: remove_path(ctx, child)))
                else:
                    items.append(Item("backups", str(child), "keep", "scripts/_helpers/backup_file.py",
                                      "named like the installer's backup, but such a name can be your own; "
                                      "delete it yourself when you no longer need it"))

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
