"""What syncs: scope rules, shared exclusions, the working-tree snapshot and the secret scanner.

Synced by default: ``common/**``, ``personas/**``, ``components.json``, ``.history/**`` and
``repos/<key>/**`` for keys derived from an ``origin``, plus the library files
(``.agents-library.json``, ``.gitignore``, ``.gitattributes``) and ``.agents-sync/**`` (shared
exclusions and conflict records). Never synced: machine-local repository groups, ``.lock``,
``.tmp-*``, caches, file-manager junk, symlinks, files over 5 MiB, names that some platform cannot
store, and anything else at the library root.

Each path belongs to one or two scope groups: ``common``, ``personas``, ``history`` or
``components``, and for per-repository data (``repos/<key>/``, ``personas/repos/<key>/``,
``.history/repos/<key>/``) also ``repos/<key>``. Excluding any of its groups keeps a path out of
the tree. Exclusions live in the tracked ``.agents-sync/scopes.json``, so all machines agree;
excluding takes a path out of the tree but never deletes it from any working tree.

A repository group is portable when its ``.repo.json`` holds an origin: its key then means the
same repository on every machine. A group without one has a key made from this machine's path
(``src.user_flows.repo_key``) and never syncs, with its personas and history.

The scanner looks for credentials in every file about to be committed. A hit keeps that file out
of the commit until the user edits it or allows that exact content by its SHA-256.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from src.user_library import MARKER, REPO_LOCAL, REPO_META

MAX_FILE_BYTES = 5 * 1024 * 1024
SYNC_DIR = ".agents-sync"
SCOPES_PATH = f"{SYNC_DIR}/scopes.json"
CONFLICTS_DIR = f"{SYNC_DIR}/conflicts"
LIBRARY_FILES = frozenset({MARKER, ".gitignore", ".gitattributes"})
COMPONENTS = "components.json"
KIND_GROUPS = ("common", "personas", "history", "components")

_JUNK_NAMES = frozenset({".lock", ".DS_Store", "Thumbs.db", "desktop.ini", "__pycache__", REPO_LOCAL})
_WINDOWS_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", re.I)
_BAD_CHARACTERS = re.compile(r'[\x00-\x1f\x7f\\<>:"|?*]')
_REPO_KEY = re.compile(r"[a-z0-9_][a-z0-9._-]*")  # what src.user_flows.repo_key can generate

SECRET_PATTERNS = (
    ("private_key", re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("github_pat", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{22,}")),
    ("gitlab_token", re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(rb"\bxox[a-z]-[A-Za-z0-9-]{10,}")),
    ("api_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}")),
    # password=x, PASSWORD="x", DB_PASSWORD='x', export PGPASSWORD=x, "password": "x"; not
    # placeholders such as password=<…> or password=${…}
    ("password", re.compile(rb"(?i)passw(?:or)?d[\"']?\s*=\s*[\"']?[^\s'\"<>$]")),
    ("password", re.compile(rb"(?i)[\"']passw(?:or)?d[\"']\s*:\s*[\"'][^\s'\"<>$]")),
)


class ScopeError(ValueError):
    """``.agents-sync/scopes.json`` is malformed; syncing it as "exclude nothing" could leak."""


def blob_id(data: bytes) -> str:
    """Git's SHA-1 object id of a blob with these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- shared exclusions ------------------------------------------------------------------


@dataclass(frozen=True)
class Scopes:
    """Groups and files kept out of sync, and contents the scanner may let through."""

    exclude: frozenset[str] = frozenset()
    exclude_files: frozenset[str] = frozenset()
    allow: frozenset[str] = frozenset()

    def dump(self) -> bytes:
        return json.dumps({"exclude": sorted(self.exclude), "exclude_files": sorted(self.exclude_files),
                           "allow": sorted(self.allow)}, indent=2).encode() + b"\n"


def parse_scopes(data: bytes | None) -> Scopes:
    """The stored exclusions; a missing file excludes nothing, a malformed one raises."""
    if data is None:
        return Scopes()
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ScopeError(f"{SCOPES_PATH} is not valid JSON ({error})") from None
    if not isinstance(value, dict) or set(value) - {"exclude", "exclude_files", "allow"}:
        raise ScopeError(f"{SCOPES_PATH} must be an object with exclude, exclude_files and allow")
    parsed = {}
    for name in ("exclude", "exclude_files", "allow"):
        items = value.get(name, [])
        if not isinstance(items, list) or not all(isinstance(item, str) and item for item in items):
            raise ScopeError(f"{SCOPES_PATH}: {name} must be a list of strings")
        parsed[name] = frozenset(items)
    for group in parsed["exclude"]:
        if not is_group(group):
            raise ScopeError(f"{SCOPES_PATH}: unknown scope group {group!r}")
    return Scopes(**parsed)


def merge_scopes(base: Scopes, local: Scopes, remote: Scopes) -> Scopes:
    """Three-way merge of each set: a machine can only add or remove entries relative to the base."""
    def merged(name):
        b, l, r = (getattr(s, name) for s in (base, local, remote))
        return frozenset((r | (l - b)) - (b - l))
    return Scopes(merged("exclude"), merged("exclude_files"), merged("allow"))


def is_group(name: str) -> bool:
    if name in KIND_GROUPS:
        return True
    prefix, _, key = name.partition("/")
    return prefix == "repos" and bool(_REPO_KEY.fullmatch(key))


# --- path rules -------------------------------------------------------------------------


def portable_name(path: str) -> bool:
    """A relative POSIX path that every platform can store as it is."""
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or _BAD_CHARACTERS.search(part) or part[-1] in ". ":
            return False
        if _WINDOWS_RESERVED.fullmatch(part):
            return False
    return True


def is_junk(path: str) -> bool:
    """Temporary, lock, cache and file-manager files, at any depth."""
    parts = path.split("/")
    name = parts[-1]
    return (any(part in _JUNK_NAMES or part.startswith(".tmp-") for part in parts)
            or name.endswith((".pyc", "~", ".swp")))


def groups(path: str) -> tuple[str, ...] | None:
    """The scope groups of ``path``; ``()`` for library files that always sync; None when never synced."""
    if is_junk(path) or not portable_name(path):
        return None
    parts = path.split("/")
    top = parts[0]
    if len(parts) == 1:
        if path in LIBRARY_FILES:
            return ()
        return ("components",) if path == COMPONENTS else None
    if top == SYNC_DIR:
        return ()
    if top == "common":
        return ("common",)
    if top == "repos":
        return (f"repos/{parts[1]}",) if len(parts) > 2 and _REPO_KEY.fullmatch(parts[1]) else None
    if top in ("personas", ".history"):
        kind = "personas" if top == "personas" else "history"
        if parts[1] == "repos":
            if len(parts) > 3 and _REPO_KEY.fullmatch(parts[2]):
                return (kind, f"repos/{parts[2]}")
            return None
        return (kind,)
    return None


def is_content(path: str) -> bool:
    """A path the owner edits (flows, personas, switches, other library files), not history or records."""
    found = groups(path)
    return found is not None and not path.startswith((".history/", f"{SYNC_DIR}/")) and path not in LIBRARY_FILES


def is_link(info: os.stat_result) -> bool:
    """A symlink, or on Windows any reparse point such as an NTFS junction, which ``lstat`` does not flag."""
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def repo_group(path: str) -> str | None:
    """``repos/<key>`` when ``path`` holds per-repository data."""
    found = groups(path) or ()
    return next((group for group in found if group.startswith("repos/")), None)


def portable_origin(meta: bytes | None) -> str | None:
    """The origin stored in a group's ``.repo.json``, or None (machine-local or unreadable)."""
    if meta is None:
        return None
    try:
        value = json.loads(meta.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    origin = value.get("origin") if isinstance(value, dict) else None
    return origin if isinstance(origin, str) and origin else None


@dataclass
class Rules:
    """Decides membership given the exclusions and which repository groups are portable.

    ``held_groups`` and ``held_files`` wait for this machine's approval: repository groups that
    are new to the library when the owner asked to be asked, and groups or files another machine
    included again while this one still has its own copies of them.
    """

    scopes: Scopes
    portable: dict[str, str]                 # "repos/<key>" -> origin
    held_groups: frozenset[str] = frozenset()
    held_files: frozenset[str] = frozenset()

    def reason(self, path: str) -> str | None:
        """None when ``path`` syncs, else why not: never, excluded, local or pending."""
        found = groups(path)
        if found is None:
            return "never"
        if path in self.scopes.exclude_files or any(group in self.scopes.exclude for group in found):
            return "excluded"
        group = next((g for g in found if g.startswith("repos/")), None)
        if group is not None and group not in self.portable:
            return "local"
        if path in self.held_files or any(g in self.held_groups for g in found):
            return "pending"
        return None

    def syncs(self, path: str) -> bool:
        return self.reason(path) is None


def scan(data: bytes) -> str | None:
    """The name of the first credential pattern found in ``data``."""
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(data):
            return name
    return None


# --- the working tree -------------------------------------------------------------------


@dataclass
class Snapshot:
    """The library's files as sync sees them at one moment.

    ``files`` may be committed. ``blocked`` hold a credential; ``held`` are too large or not
    regular files. ``outside`` are library files whose group is excluded, machine-local or
    waiting for approval: they stay on this machine. Every map is keyed by relative POSIX path.
    """

    files: dict[str, str] = field(default_factory=dict)        # path -> blob id
    blocked: dict[str, dict] = field(default_factory=dict)     # path -> {blob, sha256, pattern}
    held: dict[str, dict] = field(default_factory=dict)        # path -> {reason, size}
    outside: dict[str, dict] = field(default_factory=dict)     # path -> {blob, reason}
    sizes: dict[str, int] = field(default_factory=dict)
    portable: dict[str, str] = field(default_factory=dict)     # "repos/<key>" -> origin
    unreadable: set[str] = field(default_factory=set)           # directories that could not be listed

    def hides(self, path: str) -> bool:
        """True when ``path`` may exist although the snapshot could not see it."""
        return path in self.held or any(path.startswith(f"{directory}/") for directory in self.unreadable)

    def state(self) -> tuple:
        """Everything a concurrent write could change; equal states mean an unchanged tree."""
        return (self.files, {p: v["blob"] for p, v in self.blocked.items()},
                {p: v["blob"] for p, v in self.outside.items()}, self.held, self.unreadable)

    def local_blob(self, path: str) -> str | None:
        """The blob this machine holds at ``path`` in any category but ``held``."""
        for source in (self.files, self.blocked, self.outside):
            if path in source:
                value = source[path]
                return value if isinstance(value, str) else value["blob"]
        return None


def read_portable(root: Path) -> dict[str, str]:
    """Portable repository groups in the working tree: ``repos/<key>`` -> origin."""
    portable = {}
    try:
        entries = list(os.scandir(root / "repos"))
    except OSError:
        return portable
    for entry in entries:
        if not _REPO_KEY.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
            continue
        meta = Path(entry.path) / REPO_META
        try:
            data = None if meta.is_symlink() else meta.read_bytes()
        except OSError:
            data = None
        origin = portable_origin(data)
        if origin:
            portable[f"repos/{entry.name}"] = origin
    return portable


def walk(root: Path, unreadable: set[str] | None = None):
    """``(relative POSIX path, os.DirEntry)`` of every file and symlink below ``root`` except ``.git``.

    Directory symlinks are reported, never followed. Directories that cannot be listed go to
    ``unreadable``: their files are unknown, not deleted.
    """
    stack = [("", root)]
    while stack:
        prefix, directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except FileNotFoundError:
            if not prefix:
                return  # no library yet
            continue
        except OSError:
            if not prefix:
                raise  # an unreadable library is not an empty one
            if unreadable is not None:
                unreadable.add(prefix.rstrip("/"))
            continue
        for entry in entries:
            relative = f"{prefix}{entry.name}"
            if not prefix and entry.name == ".git":
                continue
            try:
                if entry.is_dir(follow_symlinks=False) and not is_link(entry.stat(follow_symlinks=False)):
                    stack.append((relative + "/", entry.path))
                    continue
            except OSError:
                if unreadable is not None:
                    unreadable.add(relative)
                continue
            yield relative, entry


def snapshot(root: Path, scopes: Scopes, *, held_groups: frozenset[str] = frozenset(),
             held_files: frozenset[str] = frozenset()) -> Snapshot:
    """Read every library file once: classify it, check its size and scan what may be committed."""
    result = Snapshot(portable=read_portable(root))
    rules = Rules(scopes, result.portable, held_groups, held_files)
    for relative, entry in walk(root, result.unreadable):
        reason = rules.reason(relative)
        if reason == "never":
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue  # deleted while listing
        except OSError:
            result.held[relative] = {"reason": "unreadable", "size": 0}
            continue
        if not stat.S_ISREG(info.st_mode) or is_link(info):
            result.held[relative] = {"reason": "not_regular", "size": 0}
            continue
        if info.st_size > MAX_FILE_BYTES:
            result.held[relative] = {"reason": "size", "size": info.st_size}
            continue
        try:
            with open(entry.path, "rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            continue
        except OSError:  # locked by another program, permissions: unknown content, not a deletion
            result.held[relative] = {"reason": "unreadable", "size": info.st_size}
            continue
        if len(data) > MAX_FILE_BYTES:  # grew while reading
            result.held[relative] = {"reason": "size", "size": len(data)}
            continue
        blob = blob_id(data)
        result.sizes[relative] = len(data)
        if reason is not None:
            result.outside[relative] = {"blob": blob, "reason": reason}
            continue
        pattern = scan(data)
        digest = content_hash(data)
        if pattern and digest not in scopes.allow:
            result.blocked[relative] = {"blob": blob, "sha256": digest, "pattern": pattern}
            continue
        result.files[relative] = blob
    return result
