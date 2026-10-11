"""Git-backed versions of the personal library, independent of sync's HEAD and index.

All mutating calls, including ``ensure`` and ``import_legacy``, require the caller
to hold the library's ``.lock``. A prepared commit is pinned before any working
file changes; its trees and metadata are the write-ahead journal. Recovery can
finish an interrupted operation without keeping another copy of file contents.
Portable history commits are read from incoming refs but never checked out.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from typing import Mapping
import uuid

from src.flows import FlowError
from src.user_sync import gitcmd
from src.user_sync.gitcmd import Git, GitError
from src.user_sync.scope import blob_id, is_link

HISTORY_REF = "refs/agents-core/history"
PENDING_REF = "refs/agents-core/history-pending"
ABANDONED_PREFIX = "refs/agents-core/abandoned/"
INCOMING_PREFIX = "refs/agents-core/incoming/"
RECEIVED_PREFIX = "refs/agents-core/received/"
LEGACY_SUPPORT_REF = "refs/agents-core/legacy-support"
LEGACY_SUPPORT_MANIFEST = "legacy-support.json"
ACCEPTED_PREFIX = "refs/agents-core/accepted/"
MANAGED_KEY = "agents-sync.managed"
MESSAGE_PREFIX = "Agents-Core-History: "
PORTABLE_METADATA = ".agents-history-event.json"
ZERO = "0" * 40
_LEGACY_VERSION = re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{12}(?:-deleted)?")
_VERSION = re.compile(r"(?:git-[0-9a-f]{40}|\d{8}T\d{12}Z-[0-9a-f]{12}(?:-deleted)?)")
_OID = re.compile(r"[0-9a-f]{40}")
_FLOW_PATH = re.compile(r"(?:common|repos/[a-z0-9_][a-z0-9._-]*)/[a-z0-9]+(?:-[a-z0-9]+)*\.md")
_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", re.I)


class HistoryError(FlowError):
    """A refused input or recoverable Git/working-tree operation failure."""


def commit_message(metadata: dict) -> bytes:
    """Encode one local history event. Public rewrites retain ``source_id``."""
    return ("library history\n\n" + MESSAGE_PREFIX +
            json.dumps({"format": 1, **metadata}, sort_keys=True, ensure_ascii=True) + "\n").encode()


def _git_errors(function):
    @wraps(function)
    def wrapped(self, *arguments, **options):
        try:
            return function(self, *arguments, **options)
        except GitError as error:
            raise self._fail(str(error)) from None
    return wrapped


def _is_link(path: Path) -> bool:
    """Include Windows junctions/name-surrogate reparse points without following them."""
    try:
        return is_link(path.lstat(), path)
    except FileNotFoundError:
        return False


class LibraryHistory:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.directory = self.root / ".git" / "agents-core-history"
        self.git = Git(self.root, config=self.directory / "gitconfig", ssh=None)
        self._trees: dict[str, dict[str, str]] = {}
        self._blobs: dict[str, bytes] = {}
        self._commit_trees: dict[str, str] = {}
        self._raw_trees: dict[str, list[tuple[str, str, str]]] = {}
        self._ready = False
        self._migrating = False

    def _fail(self, message: str) -> HistoryError:
        return HistoryError("flow_history: " + message)

    def _lexical_path(self, path: str) -> Path:
        """Validate a portable Git path without inspecting the current working tree."""
        if not isinstance(path, str) or not path or "\\" in path \
                or any(ord(char) < 32 or ord(char) == 127 or char in ':<>"|?*' for char in path):
            raise HistoryError("flow_invalid: invalid history path")
        try:
            path.encode("utf-8")
        except UnicodeError:
            raise HistoryError("flow_invalid: history path is not UTF-8") from None
        parts = path.split("/")
        if any(part in ("", ".", "..") or part[-1:] in (".", " ") or _RESERVED.fullmatch(part)
               for part in parts) or parts[0] in (".git", ".history"):
            raise HistoryError("flow_invalid: invalid history path")
        target = self.root.joinpath(*parts)
        if not target.is_relative_to(self.root):
            raise HistoryError("flow_invalid: history path escapes the library")
        return target

    def _safe_path(self, path: str, *, deleting: bool = False) -> Path:
        target = self._lexical_path(path)
        for ancestor in list(target.parents):
            if ancestor == self.root:
                break
            if _is_link(ancestor):
                raise HistoryError("flow_invalid: history path has a symlink directory")
        if _is_link(target) and (not deleting or not target.is_symlink()):
            raise HistoryError("flow_invalid: history cannot follow a symlink or reparse point")
        if target.exists() and target.is_dir() and not target.is_symlink():
            raise HistoryError("flow_invalid: history target is a directory")
        return target

    def _disk(self, path: str, *, deleting: bool = False) -> bytes | None:
        target = self._safe_path(path, deleting=deleting)
        if target.is_symlink():
            return None  # deleting removes the link, never reads its target
        try:
            return target.read_bytes()
        except FileNotFoundError:
            return None

    def _rev(self, ref: str) -> str | None:
        result = self.git.run("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def _tree(self, commit: str | None) -> dict[str, str]:
        if commit is None:
            return {}
        if commit in self._trees:
            return dict(self._trees[commit])
        result = {}
        for entry in self.git.run("ls-tree", "-r", "-z", "--full-tree", commit).stdout.split(b"\0"):
            if not entry:
                continue
            header, separator, raw = entry.partition(b"\t")
            try:
                mode, kind, oid = header.decode("ascii").split()
                path = raw.decode("utf-8")
            except (UnicodeError, ValueError):
                raise self._fail("a history tree has an invalid entry") from None
            if separator and mode in ("100644", "100755") and kind == "blob":
                result[path] = oid
        self._trees[commit] = result
        return dict(result)

    def _blob(self, oid: str | None) -> bytes | None:
        if oid is None:
            return None
        if oid not in self._blobs:
            self._blobs[oid] = self.git.run("cat-file", "blob", oid).stdout
        return self._blobs[oid]

    def _objects(self, ids: list[str], kind: str) -> dict[str, bytes]:
        """Read exact object bytes with one plumbing process, not one per object."""
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        raw = self.git.run("cat-file", "--batch", input=("\n".join(ids) + "\n").encode()).stdout
        cursor, result = 0, {}
        for expected in ids:
            try:
                line_end = raw.index(b"\n", cursor)
                header = raw[cursor:line_end].decode("ascii").split()
                if len(header) != 3 or header[:2] != [expected, kind]:
                    raise ValueError
                size = int(header[2])
                end = line_end + 1 + size
                if size < 0 or end >= len(raw) or raw[end:end + 1] != b"\n":
                    raise ValueError
            except (ValueError, UnicodeError):
                raise self._fail("a history object has invalid data") from None
            result[expected] = raw[line_end + 1:end]
            cursor = end + 1
        return result

    def _prime_trees(self, commits: list[str]) -> None:
        """Batch unique trees by directory depth and flatten them in memory."""
        commits = list(dict.fromkeys(commit for commit in commits if commit not in self._trees))
        for commit in commits:
            if commit not in self._commit_trees:
                self._tree(commit)
        pending = {self._commit_trees[commit] for commit in commits if commit in self._commit_trees}
        visited = set()
        while pending:
            missing = sorted(pending - self._raw_trees.keys())
            children = set()
            for oid, raw in self._objects(missing, "tree").items():
                cursor, entries = 0, []
                while cursor < len(raw):
                    try:
                        end = raw.index(b"\0", cursor)
                        mode, name = raw[cursor:end].split(b" ", 1)
                        mode, name = mode.decode("ascii"), name.decode("utf-8")
                        if end + 21 > len(raw):
                            raise ValueError
                        child = raw[end + 1:end + 21].hex()
                    except (ValueError, UnicodeError):
                        raise self._fail("a history tree has an invalid entry") from None
                    entries.append((mode, name, child))
                    cursor = end + 21
                    if mode == "40000":
                        children.add(child)
                self._raw_trees[oid] = entries
            for oid in pending:
                children.update(child for mode, _, child in self._raw_trees[oid] if mode == "40000")
            visited.update(pending)
            pending = children - visited
        for commit in commits:
            if commit in self._trees:
                continue
            result, stack = {}, [("", self._commit_trees[commit])]
            while stack:
                prefix, oid = stack.pop()
                for mode, name, child in self._raw_trees[oid]:
                    path = prefix + name
                    if mode == "40000":
                        stack.append((path + "/", child))
                    elif mode in ("100644", "100755"):
                        result[path] = child
            self._trees[commit] = result

    def _prime_blobs(self, ids: list[str]) -> None:
        self._blobs.update(self._objects([oid for oid in ids if oid not in self._blobs], "blob"))

    def _prime_events(self, records: list[tuple[str, str | None, dict]]) -> None:
        self._prime_trees([commit for record in records for commit in record[:2] if commit])
        blobs = []
        for commit, parent, metadata in records:
            tree, before = self._tree(commit), self._tree(parent)
            if metadata.get("portable"):
                paths = [metadata.get("previous_path"), metadata.get("content_path")]
                changes = metadata.get("changes", {})
                if isinstance(changes, dict):
                    paths.extend(changes.values())
                blobs.extend(tree[path] for path in paths if isinstance(path, str) and path in tree)
            else:
                path = metadata.get("path")
                if isinstance(path, str):
                    blobs.extend(oid for oid in (tree.get(path), before.get(path)) if oid)
                paths = metadata.get("paths", [])
                if isinstance(paths, list):
                    blobs.extend(tree[path] for path in paths if isinstance(path, str) and path in tree)
                archive_paths = metadata.get("archive_paths", [])
                if isinstance(archive_paths, list):
                    for path in archive_paths:
                        if isinstance(path, str):
                            blobs.extend(oid for oid in (tree.get(path), before.get(path)) if oid)
        self._prime_blobs(blobs)

    def _make_tree(self, parent: str | None, changes: Mapping[str, bytes | None]) -> str:
        index = self.directory / ("index-" + uuid.uuid4().hex)
        try:
            self.git.run("read-tree", parent or "--empty", index=index)
            records = []
            for path, content in sorted(changes.items()):
                oid = ZERO if content is None else self.git.text("hash-object", "-w", "--stdin", input=content)
                records.append((f"{'0' if content is None else '100644'} {oid}\t{path}").encode() + b"\0")
            if records:
                self.git.run("update-index", "-z", "--index-info", index=index, input=b"".join(records))
            return self.git.text("write-tree", index=index)
        finally:
            index.unlink(missing_ok=True)
            Path(str(index) + ".lock").unlink(missing_ok=True)

    def _commit(self, changes: Mapping[str, bytes | None], metadata: dict,
                *, parent: str | None = None, update: bool = True) -> str:
        if parent is None:
            parent = self._rev(HISTORY_REF)
        tree = self._make_tree(parent, changes)
        arguments = ["commit-tree", tree]
        if parent:
            arguments += ["-p", parent]
        commit = self.git.text(*arguments, input=commit_message(metadata))
        if update:
            self.git.run("update-ref", HISTORY_REF, commit, parent or ZERO)
        return commit

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def _scan(self) -> dict[str, bytes]:
        """Current owned library files, excluding archives and machine/transaction records."""
        from src.user_library import MARKER, REPO_LOCAL
        result = {}
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = [name for name in dirs if name not in (".git", ".history", ".agents-sync", "__pycache__")
                       and not _is_link(Path(directory) / name)]
            for name in files:
                full = Path(directory) / name
                relative = full.relative_to(self.root).as_posix()
                if _is_link(full) or name in (".lock", REPO_LOCAL, ".DS_Store", "Thumbs.db") \
                        or name.startswith(".tmp-") or name.endswith((".pyc", "~", ".swp")):
                    continue
                if relative.split("/")[0] not in ("common", "repos", "personas") \
                        and relative not in (MARKER, ".gitignore", ".gitattributes", "components.json"):
                    continue
                try:
                    self._safe_path(relative)
                except HistoryError:
                    continue  # a non-portable manually added filename stays outside history
                result[relative] = full.read_bytes()
        return result

    @_git_errors
    def validate_repository(self) -> None:
        """Read-only ownership/path preflight before creating even ``root/.lock``.

        An absent repository is allowed. Existing state must belong to
        Agents-Core and use supported marker formats and ordinary support paths.
        ``ensure`` repeats this check while the caller holds the library lock.
        """
        from src import user_library
        if self.root.exists() and not self.root.is_dir():
            raise self._fail("the library root is not a directory")

        def ordinary(path: Path, *, directory: bool = False) -> None:
            if _is_link(path):
                raise self._fail(f"the library support path is a symlink or reparse point: {path.relative_to(self.root)}")
            try:
                value = path.lstat()
            except FileNotFoundError:
                return
            expected = stat.S_ISDIR if directory else stat.S_ISREG
            if not expected(value.st_mode):
                raise self._fail(f"the library support path has an invalid file type: {path.relative_to(self.root)}")

        for name in (user_library.MARKER, ".gitignore", ".gitattributes", ".lock"):
            ordinary(self.root / name)
        marker = self.root / user_library.MARKER
        if marker.exists():
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                raise self._fail("the library marker cannot be read") from None
            version = value.get("format") if isinstance(value, dict) else None
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise self._fail("the library marker has an invalid format")
            if version > user_library.FORMAT:
                raise self._fail("the library format is newer than this installation")
        dot_git = self.root / ".git"
        ordinary(dot_git, directory=True)
        if not dot_git.exists():
            return
        ordinary(dot_git / "config")
        ordinary(self.directory, directory=True)
        ordinary(self.git.config)
        ordinary(self.directory / "hooks", directory=True)
        if self.git.text("config", "--local", "--get", MANAGED_KEY, check=False) != "true":
            raise self._fail("the library's .git was not created by Agents-Core")

    def _ensure_repository(self) -> None:
        from src import user_library
        self.validate_repository()
        dot_git = self.root / ".git"
        if self._ready and dot_git.exists():
            return
        wanted = gitcmd.MIN_GIT_VERSION
        version = gitcmd.git_version()
        if version is None or version < wanted:
            raise self._fail("Git " + ".".join(map(str, wanted)) + " or newer is required")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not dot_git.exists():
            self.git.run("init", "--quiet", "--initial-branch=main", str(self.root), repository=False)
            self.git.run("config", MANAGED_KEY, "true")
        self.directory.mkdir(exist_ok=True, mode=0o700)
        hooks = self.directory / "hooks"
        hooks.mkdir(exist_ok=True, mode=0o700)
        config = gitcmd.isolated_config(name="Agents-Core", email="agents-core@localhost", hooks=hooks).encode()
        if not self.git.config.exists() or self.git.config.read_bytes() != config:
            user_library.atomic_write(self.git.config, config)
        user_library.ensure_root_files(self.root)
        self._ready = True

    def ensure(self) -> dict:
        """Initialize/reuse managed Git, recover, and migrate. Caller holds ``root/.lock``."""
        try:
            self._ensure_repository()
            self.recover()
            if self._rev(HISTORY_REF) is None:
                files = self._scan()
                self._commit(files, {"action": "baseline", "path": None, "paths": sorted(files),
                                     "archive": False, "deleted": False, "time": self._now()})
            return self.import_legacy()
        except GitError as error:
            raise self._fail(str(error)) from None

    def _commits(self, refs: list[str]) -> list[tuple[str, str | None, dict]]:
        if not refs:
            return []
        # Retained history tips can exceed Windows' command-line limit.
        ids = self.git.text("rev-list", "--reverse", "--stdin",
                            input=("\n".join(refs) + "\n").encode()).splitlines()
        if not ids:
            return []
        records = []
        objects = self._objects(ids, "commit")
        for expected in ids:
            data = objects[expected]
            headers, _, body = data.partition(b"\n\n")
            try:
                fields = headers.splitlines()
                self._commit_trees[expected] = next(line[5:].decode("ascii") for line in fields
                                                    if line.startswith(b"tree "))
                parents = [line[7:].decode("ascii") for line in fields if line.startswith(b"parent ")]
            except (StopIteration, UnicodeError):
                raise self._fail("a history commit has invalid headers") from None
            metadata = None
            for line in body.decode("utf-8", "replace").splitlines():
                if line.startswith(MESSAGE_PREFIX):
                    try:
                        metadata = json.loads(line[len(MESSAGE_PREFIX):])
                    except ValueError:
                        pass
                    break
            records.append((expected, parents[0] if parents else None, metadata))
        unknown = [commit for commit, _, metadata in records
                   if not isinstance(metadata, dict) or metadata.get("format") != 1]
        self._prime_trees(unknown)
        self._prime_blobs([self._tree(commit)[PORTABLE_METADATA] for commit in unknown
                           if PORTABLE_METADATA in self._tree(commit)])
        commits = []
        for expected, parent, metadata in records:
            if not isinstance(metadata, dict) or metadata.get("format") != 1:
                tree = self._tree(expected)
                if PORTABLE_METADATA in tree:
                    try:
                        metadata = json.loads(self._blob(tree[PORTABLE_METADATA]).decode("utf-8"))
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(metadata, dict) and metadata.get("format") == 1:
                        commits.append((expected, parent, {**metadata, "portable": True}))
            else:
                commits.append((expected, parent, metadata))
        return commits

    def _refs(self) -> list[str]:
        refs = self.git.text("for-each-ref", "--format=%(refname)", INCOMING_PREFIX,
                             RECEIVED_PREFIX, ACCEPTED_PREFIX).splitlines()
        if self._rev(HISTORY_REF):
            refs.append(HISTORY_REF)
        return refs

    def _event(self, commit: str, parent: str | None, metadata: dict) -> dict:
        path = metadata.get("path")
        if path is not None:
            self._lexical_path(path)
        if not isinstance(metadata.get("time", ""), str) or not isinstance(metadata.get("archive", False), bool) \
                or not isinstance(metadata.get("deleted", False), bool):
            raise self._fail("a history event has invalid metadata")
        tree, before = self._tree(commit), self._tree(parent)
        source_id = metadata.get("source_id") or metadata.get("version") or "git-" + commit
        if not isinstance(source_id, str) or not _VERSION.fullmatch(source_id):
            raise self._fail("a history event has an invalid version")
        archive = bool(metadata.get("archive"))
        if metadata.get("portable"):
            def data(name):
                entry = metadata.get(name)
                if entry is None:
                    return None
                if not isinstance(entry, str) or entry not in tree:
                    raise self._fail("a portable history event names a missing blob")
                return self._blob(tree[entry])
            previous, content = data("previous_path"), data("content_path")
            changes = metadata.get("changes", {})
            if not isinstance(changes, dict):
                raise self._fail("a portable history event has invalid changes")
            resolved = {}
            for target, stored in changes.items():
                self._lexical_path(target)
                if stored is not None and (not isinstance(stored, str) or stored not in tree):
                    raise self._fail("a portable history change names a missing blob")
                resolved[target] = self._blob(tree.get(stored)) if stored is not None else None
        else:
            previous = self._blob(before.get(path)) if path else None
            content = self._blob(tree.get(path)) if path else None
            paths = metadata.get("paths", [])
            if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
                raise self._fail("a history event has invalid changes")
            resolved = {}
            for target in paths:
                self._lexical_path(target)
                resolved[target] = self._blob(tree.get(target))
        return {"id": source_id, "version": source_id, "commit": commit, "parent": parent,
                "action": metadata.get("action"), "path": path, "paths": sorted(resolved),
                "time": metadata.get("time", ""), "archive": archive,
                "deleted": bool(metadata.get("deleted")), "content": content, "previous": previous,
                "changes": resolved, "legacy_id": metadata.get("legacy_id") or metadata.get("version")}

    def _combine_events(self, preferred: dict, other: dict, *, native: bool) -> dict:
        """Combine filtered copies of an event, refusing contradictory bytes."""
        def conflict():
            raise self._fail("conflicting copies of a history event: " + preferred["id"])
        if preferred["deleted"] != other["deleted"]:
            conflict()
        first, second = self._archived(preferred), self._archived(other)
        if first is not None and second is not None and first != second:
            conflict()
        if preferred["archive"] != other["archive"]:
            # A locally imported source is an archive of the original event's
            # predecessor. Its content/changes have a different meaning from
            # the original transition's successor, but the version bytes agree.
            if native or first is not None:
                return preferred
            return other if second is not None else preferred
        merged = dict(preferred)
        for field in ("content", "previous"):
            if field == "previous" and preferred["archive"]:
                continue  # an archive's parent tree is unrelated bookkeeping
            left, right = preferred[field], other[field]
            if left is not None and right is not None and left != right:
                conflict()
            if not native and left is None:
                merged[field] = right
        changes = dict(preferred["changes"])
        for path, raw in other["changes"].items():
            if native and path != preferred["path"]:
                continue  # portable repository metadata may be normalized
            if path in changes and changes[path] != raw:
                conflict()
            if not native:
                changes[path] = raw
        if not native:
            merged["changes"], merged["paths"] = changes, sorted(changes)
        return merged

    def _events(self, records: list[tuple[str, str | None, dict]]) -> list[dict]:
        self._prime_events(records)
        result = {}
        for commit, parent, metadata in records:
            expanded = [self._event(commit, parent, metadata)]
            if not metadata.get("portable") and self._adoption_data(metadata) is not None:
                tree = self._tree(commit)
                for path in metadata["archive_paths"]:
                    expanded.append(self._event(commit, parent, {**metadata, "path": path, "paths": [],
                                          "deleted": path not in tree}))
            for event in expanded:
                key = (event["path"], event["id"])
                portable = bool(metadata.get("portable"))
                if key in result:
                    previous, previous_portable = result[key]
                    if not portable and previous_portable:
                        event = self._combine_events(event, previous, native=True)
                    else:
                        event = self._combine_events(previous, event, native=not previous_portable)
                        portable = previous_portable
                result[key] = event, portable
        return sorted((event for event, _ in result.values()),
                      key=lambda event: (event["time"], event["commit"]))

    @_git_errors
    def events(self, refs: list[str] | None = None) -> list[dict]:
        """All exact versions, including discarded history retained privately."""
        self._ensure_repository()
        return self._events(self._commits(self._refs() if refs is None else refs))

    def _adoption_data(self, metadata: dict) -> dict | None:
        if "adoption" not in metadata:
            return None
        value = metadata["adoption"]
        if not isinstance(value, dict) or value.get("decision") != "replace" \
                or metadata.get("action") != "adopt" or metadata.get("path") is not None:
            raise self._fail("an adoption boundary has invalid metadata")
        tips = value.get("accepted_tips")
        if not isinstance(tips, dict):
            raise self._fail("an adoption boundary has invalid accepted tips")
        self._validate_tips(tips)
        remote_head = value.get("remote_head")
        if remote_head is not None and (not isinstance(remote_head, str) or not _OID.fullmatch(remote_head)):
            raise self._fail("an adoption boundary has an invalid remote head")
        base = value.get("base_head")
        if base is not None and (not isinstance(base, str) or not _OID.fullmatch(base)):
            raise self._fail("an adoption boundary has an invalid base head")
        for name in ("decision_hash", "remote_identity"):
            item = value.get(name)
            if item is not None and (not isinstance(item, str) or not item or len(item) > 2048
                                     or any(ord(char) < 32 for char in item)):
                raise self._fail("an adoption boundary has invalid " + name)
        paths = metadata.get("archive_paths")
        changed = metadata.get("paths", [])
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths) \
                or len(paths) != len(set(paths)) or not isinstance(changed, list) \
                or not all(isinstance(path, str) for path in changed) \
                or any(not isinstance(path, str) or not _FLOW_PATH.fullmatch(path) or path not in changed
                       for path in paths):
            raise self._fail("an adoption boundary has invalid archived paths")
        retained = value.get("retained_paths", {})
        if not isinstance(retained, dict):
            raise self._fail("an adoption boundary has invalid retained paths")
        for path, reason in retained.items():
            self._lexical_path(path)
            if reason not in ("unsafe_remote", "unreadable", "not_regular", "size") \
                    or any(target == path or target.startswith(path + "/") for target in changed):
                raise self._fail("an adoption boundary has invalid retained paths")
        unchanged = value.get("unchanged_before", {})
        if not isinstance(unchanged, dict):
            raise self._fail("an adoption boundary has invalid unchanged expectations")
        for path, oid in unchanged.items():
            self._lexical_path(path)
            if (oid is not None and (not isinstance(oid, str) or not _OID.fullmatch(oid))) \
                    or path in changed \
                    or any(path == kept or path.startswith(kept + "/") for kept in retained):
                raise self._fail("an adoption boundary has invalid unchanged expectations")
        return value

    def _validate_tips(self, tips: Mapping[str, str], *, objects: bool = False) -> None:
        for name, oid in tips.items():
            if not isinstance(name, str) or not isinstance(oid, str) or not _OID.fullmatch(oid) \
                    or self.git.run("check-ref-format", ACCEPTED_PREFIX + ZERO + "/tips/" + name,
                                    check=False).returncode:
                raise HistoryError("flow_invalid: invalid accepted history tip")
            if objects and self._rev(oid) != oid:
                raise self._fail("an accepted history tip is not a local commit")

    def _completed_adoption(self) -> tuple[str, str | None, dict] | None:
        if self._rev(HISTORY_REF) is None:
            return None
        return next((record for record in reversed(self._commits([HISTORY_REF]))
                     if self._adoption_data(record[2]) is not None), None)

    @_git_errors
    def adoption(self) -> dict | None:
        """Read the pending Replace, or the latest completed Replace, without writes."""
        self.validate_repository()
        if not (self.root / ".git").exists():
            return None
        pending = self._rev(PENDING_REF)
        record = None
        if pending is not None:
            records = self._commits([PENDING_REF])
            if records and records[-1][0] == pending and self._adoption_data(records[-1][2]) is not None:
                record = records[-1]
        is_pending = record is not None
        record = record or self._completed_adoption()
        if record is None:
            return None
        commit, _, metadata = record
        tree = self._tree(commit)
        return {**metadata["adoption"], "boundary": commit, "id": "git-" + commit,
                "pending": is_pending, "archive_paths": list(metadata["archive_paths"]),
                "changes": {path: self._blob(tree.get(path)) for path in metadata["paths"]},
                "time": metadata["time"]}

    @_git_errors
    def generation(self) -> str | None:
        """The latest completed Replace boundary; pending intent is not published."""
        self.validate_repository()
        if not (self.root / ".git").exists():
            return None
        record = self._completed_adoption()
        return record[0] if record else None

    @_git_errors
    def accepted_refs(self) -> list[str]:
        """Only the current generation's explicitly selected remote history tips."""
        boundary = self.generation()
        if boundary is None:
            return []
        prefix = ACCEPTED_PREFIX + boundary + "/"
        return self.git.text("for-each-ref", "--format=%(refname)", prefix + "tips/",
                             prefix + "retained/").splitlines()

    @_git_errors
    def accept(self, tips: Mapping[str, str]) -> int:
        """Retain currently fetched tips in the active publication generation.

        Caller holds ``.lock`` and validates the connected remote's identity.
        Immutable old tips preserve versions through upstream rewrite/pruning;
        unrelated received refs and earlier generations remain private only.
        """
        self.ensure()
        boundary = self.generation()
        if boundary is None:
            return 0
        if not isinstance(tips, Mapping):
            raise HistoryError("flow_invalid: invalid accepted history tips")
        self._validate_tips(tips, objects=True)
        for name, oid in tips.items():
            if self._rev(INCOMING_PREFIX + name) != oid:
                raise self._fail("an accepted history tip is not the current incoming tip")
        self._events(self._commits([*self._refs(), *tips.values()]))
        changed = 0
        for name, oid in sorted(tips.items()):
            ref = ACCEPTED_PREFIX + boundary + "/tips/" + name
            previous = self._rev(ref)
            if previous == oid:
                continue
            if previous is not None:
                retained = ACCEPTED_PREFIX + boundary + "/retained/" + previous
                pinned = self._rev(retained)
                if pinned not in (None, previous):
                    raise self._fail("a retained history ref names different data")
                if pinned is None:
                    self.git.run("update-ref", retained, previous, ZERO)
            self.git.run("update-ref", ref, oid, previous or ZERO)
            changed += 1
        return changed

    @_git_errors
    def export_events(self) -> list[dict]:
        """Accepted remote versions and native edits after the completed boundary."""
        self._ensure_repository()
        if self._rev(HISTORY_REF) is None:
            return []
        boundary = self.generation()
        refs = [HISTORY_REF, *self.accepted_refs()]
        if boundary:
            refs.append("^" + boundary)
        return self._events(self._commits(refs))

    def _archived(self, event: dict) -> bytes | None:
        if event["archive"]:
            return event["content"]
        if event["action"] in ("baseline", "initialize"):
            return None
        return event["previous"]

    def history(self, path: str) -> list[dict]:
        self._lexical_path(path)
        versions = []
        for event in reversed(self.events()):
            raw = self._archived(event)
            if event["path"] == path and raw is not None:
                versions.append({"version": event["id"], "revision_prefix": hashlib.sha256(raw).hexdigest()[:12],
                                 "deleted": event["deleted"], "time": event["time"]})
        return versions

    def read(self, path: str, version: str) -> bytes:
        self._lexical_path(path)
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise HistoryError("flow_invalid: unknown version")
        for event in self.events():
            if event["path"] == path and version in (event["id"], event["legacy_id"]):
                raw = self._archived(event)
                if raw is not None:
                    return raw
        raise HistoryError("flow_not_found: version not found")

    def _reconcile(self, paths: list[str], *, flow_path: str | None = None) -> None:
        parent = self._rev(HISTORY_REF)
        tree = self._tree(parent)
        changes = {path: self._disk(path, deleting=True) for path in paths}
        changes = {path: raw for path, raw in changes.items() if raw != self._blob(tree.get(path))}
        if not changes:
            return
        primary = flow_path if flow_path in changes else None
        self._commit(changes, {"action": "external", "path": primary, "paths": sorted(changes),
                               "archive": False, "deleted": bool(primary and changes[primary] is None),
                               "time": self._now()})

    @_git_errors
    def reconcile(self) -> int:
        """Capture direct edits/deletions before sync; do not change working files.

        Each observed flow transition gets its own version event. Other library
        changes form one bookkeeping event. Caller holds ``root/.lock``.
        """
        self.ensure()
        actual = self._scan()
        tree = self._tree(self._rev(HISTORY_REF))
        # An explicit mutation may also track machine-local metadata; retain its
        # actual bytes while scanning instead of fabricating a deletion.
        for path in set(tree) - set(actual):
            try:
                actual[path] = self._disk(path, deleting=True)
            except HistoryError:
                # A skipped link/directory is unknown, not a removed file. Keep
                # its prior Git state; mutations still preflight and refuse it.
                actual[path] = self._blob(tree[path])
        changed = [path for path in sorted(set(tree) | set(actual))
                   if actual.get(path) != self._blob(tree.get(path))]
        others = []
        for path in changed:
            if _FLOW_PATH.fullmatch(path):
                self._reconcile([path], flow_path=path)
            else:
                others.append(path)
        if others:
            self._reconcile(others)
        return len(changed)

    def _pin_adoption(self, boundary: str, data: dict) -> None:
        self._validate_tips(data["accepted_tips"], objects=True)
        refs = {ACCEPTED_PREFIX + boundary + "/tips/" + name: oid
                for name, oid in data["accepted_tips"].items()}
        remote = data.get("remote_head")
        if remote is not None:
            if self._rev(remote) != remote:
                raise self._fail("the adopted remote head is not a local commit")
            refs[ACCEPTED_PREFIX + boundary + "/remote-head"] = remote
        for ref, oid in sorted(refs.items()):
            current = self._rev(ref)
            if current not in (None, oid):
                raise self._fail("an accepted history ref names different data")
            if current is None:
                self.git.run("update-ref", ref, oid, ZERO)

    @_git_errors
    def adopt(self, changes: Mapping[str, bytes | None], *,
              expected_before: Mapping[str, str | None], accepted_tips: Mapping[str, str],
              decision: str = "replace", remote_head: str | None = None,
              decision_hash: str | None = None, remote_identity: str | None = None,
              retained_paths: Mapping[str, str] | None = None) -> str:
        """Apply a confirmed Replace and start a new publication generation.

        Caller holds ``.lock`` and verifies the whole displayed preview. Expected
        values are Git blob IDs, with None for an absent path. Even a zero-write
        Replace creates a boundary. Old native versions stay private; accepted
        tips and every discarded current flow remain reachable after recovery.
        """
        self.ensure()
        if decision != "replace" or not isinstance(changes, Mapping) or not isinstance(accepted_tips, Mapping) \
                or not isinstance(expected_before, Mapping) \
                or (retained_paths is not None and not isinstance(retained_paths, Mapping)):
            raise HistoryError("flow_invalid: invalid adoption decision")
        proposed, expected, tips = dict(changes), dict(expected_before), dict(accepted_tips)
        if not set(proposed) <= set(expected):
            raise HistoryError("flow_invalid: adoption is missing expected before values")
        self._validate_tips(tips, objects=True)
        actual = {}
        for path in set(proposed) | set(expected):
            self._safe_path(path)  # unsafe paths are retained, never part of Replace
            raw = proposed.get(path)
            oid = expected[path]
            if path in proposed and raw is not None and not isinstance(raw, bytes):
                raise HistoryError("flow_invalid: history changes must contain bytes")
            if oid is not None and (not isinstance(oid, str) or not _OID.fullmatch(oid)):
                raise HistoryError("flow_invalid: invalid adoption before value")
            actual[path] = self._disk(path)
            if (blob_id(actual[path]) if actual[path] is not None else None) != oid:
                raise self._fail("the confirmed adoption target changed externally: " + path)
        # Refuse ambiguous source/version identities before any working write,
        # preserving the existing compatible filtered-copy/native precedence rules.
        self._events(self._commits([*self._refs(), *tips.values()]))
        changed = {path: raw for path, raw in proposed.items() if raw != actual[path]}
        parent = base = self._rev(HISTORY_REF)
        data = {"decision": decision, "accepted_tips": tips, "remote_head": remote_head, "base_head": base,
                "decision_hash": decision_hash, "remote_identity": remote_identity,
                "retained_paths": dict(retained_paths or {}),
                "unchanged_before": {path: oid for path, oid in expected.items() if path not in changed}}
        metadata = {"action": "adopt", "path": None, "paths": sorted(changed),
                    "archive": False, "deleted": False, "time": self._now(),
                    "archive_paths": sorted(path for path in changed if _FLOW_PATH.fullmatch(path)
                                             and actual[path] is not None), "adoption": data}
        self._adoption_data(metadata)
        if remote_head is not None and self._rev(remote_head) != remote_head:
            raise self._fail("the adopted remote head is not a local commit")
        # Capture the bytes that passed the confirmed expectation, without a
        # second disk read or an intermediate published history tip. A later
        # external edit then differs from the pending parent's tree and blocks.
        tree = self._tree(parent)
        observed = {path: raw for path, raw in actual.items() if raw != self._blob(tree.get(path))}
        others = {}
        for path, raw in sorted(observed.items()):
            if _FLOW_PATH.fullmatch(path):
                parent = self._commit({path: raw}, {"action": "external", "path": path, "paths": [path],
                    "archive": False, "deleted": raw is None, "time": self._now()}, parent=parent, update=False)
            else:
                others[path] = raw
        if others:
            parent = self._commit(others, {"action": "external", "path": None, "paths": sorted(others),
                "archive": False, "deleted": False, "time": self._now()}, parent=parent, update=False)
        metadata["time"] = self._now()
        commit = self._commit(changed, metadata, parent=parent, update=False)
        # Pin remote objects before the intent: metadata alone is not a Git edge
        # and would not keep an accepted tip alive through garbage collection.
        self._pin_adoption(commit, data)
        self.git.run("update-ref", PENDING_REF, commit, ZERO)
        self.recover()
        return "git-" + commit

    def _check_unchanged_adoption(self, data: dict) -> None:
        for path, expected in data.get("unchanged_before", {}).items():
            raw = self._disk(path)
            actual = blob_id(raw) if raw is not None else None
            if actual != expected:
                raise self._fail("an unchanged adoption target changed externally: " + path)

    @_git_errors
    def apply(self, changes: Mapping[str, bytes | None], *, action: str = "save",
              flow_path: str | None = None) -> str | None:
        """Durably record and apply the supplied file changes. Caller holds ``.lock``.

        The returned ID names the transition; historical reads return the old
        flow bytes. A creation has an ID but no archived predecessor. No-op is None.
        A failed multi-file write stays pinned and is completed by ``recover``.
        """
        self.ensure()
        proposed = dict(changes)
        if flow_path is not None:
            self._safe_path(flow_path, deleting=proposed.get(flow_path) is None)
        links = {}
        for path, raw in proposed.items():
            if raw is not None and not isinstance(raw, bytes):
                raise HistoryError("flow_invalid: history changes must contain bytes")
            target = self._safe_path(path, deleting=raw is None)
            if target.is_symlink():
                links[path] = os.readlink(target)
        actual = {path: self._disk(path, deleting=raw is None) for path, raw in proposed.items()}
        changed = {path: raw for path, raw in proposed.items() if raw != actual[path] or path in links}
        self._reconcile(list(proposed), flow_path=flow_path)
        if not changed:
            return None
        primary = flow_path if flow_path in changed else None
        parent = self._rev(HISTORY_REF)
        metadata = {"action": action, "path": primary, "paths": sorted(changed),
                    "archive": False, "deleted": bool(primary and changed[primary] is None),
                    "time": self._now(), "unlink_links": links}
        commit = self._commit(changed, metadata, parent=parent, update=False)
        self.git.run("update-ref", PENDING_REF, commit, ZERO)
        self.recover()
        return "git-" + commit

    @_git_errors
    def recover(self) -> bool:
        """Finish a pinned mutation, refusing unexpected external bytes or links."""
        self._ensure_repository()
        self._clear_stale_ref_locks()
        pending = self._rev(PENDING_REF)
        if pending is None:
            return False
        records = self._commits([PENDING_REF])
        if not records or records[-1][0] != pending:
            raise self._fail("the pending history transaction is invalid")
        _, parent, metadata = records[-1]
        old, new = self._tree(parent), self._tree(pending)
        head = self._rev(HISTORY_REF)
        adoption = self._adoption_data(metadata)
        base = adoption.get("base_head") if adoption is not None else parent
        if head not in (base, pending):
            raise self._fail("history changed while a pending transaction was open")
        if adoption is not None:
            self._check_unchanged_adoption(adoption)
            self._pin_adoption(pending, adoption)
        links = metadata.get("unlink_links", {})
        if not isinstance(links, dict):
            raise self._fail("the pending history transaction has invalid links")
        paths = metadata.get("paths", [])
        if not isinstance(paths, list):
            raise self._fail("the pending history transaction has invalid paths")
        operations = []
        for path in paths:
            target = self._safe_path(path, deleting=path not in new)
            before, after = self._blob(old.get(path)), self._blob(new.get(path))
            if target.is_symlink():
                if path not in links or after is not None or os.readlink(target) != links[path]:
                    raise self._fail("a pending target was replaced by an unexpected symlink")
                operations.append((target, None))
                continue
            actual = self._disk(path, deleting=True)
            if actual == after:
                continue
            if actual != before:
                raise self._fail(f"a pending target changed externally: {path}")
            operations.append((target, after))
        from src.user_library import atomic_write
        for target, after in operations:
            if after is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, after)
        if adoption is not None:
            self._check_unchanged_adoption(adoption)
        if head != pending:
            self.git.run("update-ref", HISTORY_REF, pending, base or ZERO)
        self.git.run("update-ref", "-d", PENDING_REF, pending)
        return True

    def _clear_stale_ref_locks(self) -> None:
        """Remove only old private ref locks. Caller holds the library ``.lock``."""
        directory = self.root / ".git" / "refs" / "agents-core"
        if _is_link(directory.parent) or _is_link(directory):
            return
        now = time.time()
        for name in ("history.lock", "history-pending.lock", "legacy-support.lock"):
            path = directory / name
            try:
                value = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(value.st_mode) and not is_link(value, path) \
                    and now - value.st_mtime >= gitcmd.STALE_GIT_LOCK_SECONDS:
                path.unlink(missing_ok=True)

    @_git_errors
    def abandon_pending(self) -> bool:
        """Preserve a caught failed write's intent without changing working files.

        Sync calls this while holding ``.lock`` after a caught working-file write
        failure. The private proposal ref survives garbage collection but is
        excluded from successful history and publication. Unhandled crashes use
        strict ``recover`` instead, which finishes their prepared transaction.
        """
        self._ensure_repository()
        self._clear_stale_ref_locks()
        pending = self._rev(PENDING_REF)
        if pending is None:
            return False
        records = self._commits([PENDING_REF])
        if not records or records[-1][0] != pending:
            raise self._fail("the pending history transaction is invalid")
        if self._adoption_data(records[-1][2]) is not None:
            raise self._fail("a pending adoption must be recovered or explicitly resolved")
        parent = records[-1][1]
        if self._rev(HISTORY_REF) not in (parent, pending):
            raise self._fail("history changed while a pending transaction was open")
        archived = ABANDONED_PREFIX + pending
        if self._rev(archived) != pending:
            self.git.run("update-ref", archived, pending, ZERO)
        self.git.run("update-ref", "-d", PENDING_REF, pending)
        return True

    @_git_errors
    def record(self, path: str, raw: bytes, *, deleted: bool = False,
               version: str | None = None) -> str:
        """Pin an archived value in Git without writing its working file.

        ``version`` retains a legacy ID, including conflict aliases; repeated
        imports of the same path/ID/bytes are a no-op. Caller holds ``.lock``.
        """
        self._ensure_repository()
        self.recover()
        self._lexical_path(path)
        if not isinstance(raw, bytes) or (version is not None and not _VERSION.fullmatch(version)):
            raise HistoryError("flow_invalid: invalid archived version")
        if version:
            for event in self.events([HISTORY_REF]):
                if event["path"] == path and version in (event["id"], event["legacy_id"]):
                    if self._archived(event) != raw or event["deleted"] != deleted:
                        raise self._fail("an archived version ID already names different bytes")
                    return version
        stamp = self._now()
        if version and _LEGACY_VERSION.fullmatch(version):
            try:
                stamp = datetime.strptime(version.split("-")[0], "%Y%m%dT%H%M%S%fZ").replace(
                    tzinfo=timezone.utc).isoformat(timespec="microseconds")
            except ValueError:
                raise self._fail("an archived version has an invalid timestamp") from None
        metadata = {"action": "archive", "path": path, "paths": [path], "archive": True,
                    "deleted": deleted, "time": stamp}
        if version:
            metadata.update(version=version, legacy_id=version)
        parent = self._rev(HISTORY_REF)
        previous = self._blob(self._tree(parent).get(path))
        commit = self._commit({path: raw}, metadata, parent=parent, update=False)
        restored = self._commit({path: previous},
            {"action": "baseline", "path": None, "paths": [path], "archive": False,
             "deleted": False, "time": self._now()}, parent=commit, update=False)
        # Publish both commits at once: an interrupted archive cannot change the
        # current native tree or feed archive bytes into a later external event.
        self.git.run("update-ref", HISTORY_REF, restored, parent or ZERO)
        return version or "git-" + commit

    def _store_legacy_support(self, entries: list[tuple[Path, bytes]]) -> None:
        """Keep unconfirmed editor/OS leftovers reachable on a private-only ref."""
        if not entries:
            return
        legacy = self.root / ".history"
        parent = self._rev(LEGACY_SUPPORT_REF)
        tree = self._tree(parent)
        records, files = [], {}
        for target, raw in sorted(entries):
            relative = target.relative_to(legacy).as_posix()
            stored = "files/" + hashlib.sha256(os.fsencode(relative) + b"\0" + raw).hexdigest()
            records.append({"path": relative, "stored": stored})
            files[stored] = raw
        files[LEGACY_SUPPORT_MANIFEST] = (json.dumps({"format": 1, "files": records},
                                                   sort_keys=True, ensure_ascii=True) + "\n").encode()
        changes = {path: raw for path, raw in files.items() if self._blob(tree.get(path)) != raw}
        if changes:
            arguments = ["commit-tree", self._make_tree(parent, changes)]
            if parent:
                arguments += ["-p", parent]
            commit = self.git.text(*arguments, input=b"preserve legacy history support files\n")
            self.git.run("update-ref", LEGACY_SUPPORT_REF, commit, parent or ZERO)
            tree = self._tree(commit)
        if any(self._blob(tree.get(path)) != raw for path, raw in files.items()):
            raise self._fail("legacy support file verification failed")

    def _legacy_bytes(self, target: Path) -> bytes:
        legacy = self.root / ".history"
        for path in (target, *target.parents):
            if _is_link(path):
                raise self._fail("legacy history contains a symlink")
            if path == legacy:
                break
        if not stat.S_ISREG(target.lstat().st_mode):
            raise self._fail("legacy history contains a non-regular file")
        return target.read_bytes()

    def import_legacy(self) -> dict:
        """Import and verify known archives before removing them; retries are idempotent."""
        if self._migrating:
            return {"status": "migrating", "imported": 0}
        self._ensure_repository()
        self.recover()
        legacy = self.root / ".history"
        if _is_link(legacy):
            raise self._fail("the legacy history directory is a symlink")
        self._migrating = True
        imported, support = [], []
        try:
            if legacy.exists():
                for directory, dirs, files in os.walk(legacy, followlinks=False):
                    if any(_is_link(Path(directory) / name) for name in dirs + files):
                        raise self._fail("legacy history contains a symlink")
                    for name in files:
                        target = Path(directory) / name
                        relative = target.relative_to(legacy)
                        if name in (".DS_Store", "Thumbs.db") or name.startswith(".tmp-") \
                                or name.endswith(("~", ".swp")):
                            support.append((target, self._legacy_bytes(target)))
                            continue
                        version = Path(name).stem
                        if len(relative.parts) < 3:
                            raise self._fail(f"legacy history contains an unrecognized file: {relative.as_posix()}")
                        path = (PurePosixPath(*relative.parts[:-2]) / (relative.parts[-2] + ".md")).as_posix()
                        if not name.endswith(".md") or not _LEGACY_VERSION.fullmatch(version) \
                                or not _FLOW_PATH.fullmatch(path):
                            raise self._fail(f"legacy history contains an unrecognized file: {relative.as_posix()}")
                        raw = self._legacy_bytes(target)
                        expected = version.split("-")[1]
                        if hashlib.sha256(raw).hexdigest()[:12] != expected:
                            raise self._fail(f"legacy archive checksum differs: {relative.as_posix()}")
                        imported.append((version, path, target, raw))
                self._store_legacy_support(support)
                for version, path, _, raw in sorted(imported):
                    self.record(path, raw, version=version, deleted=version.endswith("-deleted"))
                for version, path, _, raw in imported:
                    if self.read(path, version) != raw:
                        raise self._fail("legacy history verification failed")
                for target, raw in [(target, raw) for _, _, target, raw in imported] + support:
                    if self._legacy_bytes(target) != raw:
                        raise self._fail("a legacy history file changed before removal")
                for target, _ in [(target, raw) for _, _, target, raw in imported] + support:
                    target.unlink()
                for directory, _, _ in os.walk(legacy, topdown=False):
                    Path(directory).rmdir()
            self._upgrade_marker()
            return {"status": "ready", "imported": len(imported)}
        except GitError as error:
            raise self._fail(str(error)) from None
        finally:
            self._migrating = False

    def _upgrade_marker(self) -> None:
        from src import user_library
        marker = self.root / user_library.MARKER
        value = json.loads(marker.read_text(encoding="utf-8"))
        if value["format"] < user_library.FORMAT:
            value["format"] = user_library.FORMAT
            user_library.atomic_write(marker, (json.dumps(value, indent=2) + "\n").encode())
            self._reconcile([user_library.MARKER])
