"""Publish every permitted library revision without publishing private Git ancestry.

Each installation owns an append-only ref for each publication policy. Its commits
contain one filtered event, with blob references described by a small metadata
file. They have no parent in the private history. Fetching these refs lets the
library reader expose intermediate revisions without checkout archive files.

Every sync pass filters the whole exported history again, so it reads it lazily
(#535): with ``read``, events hold blob IDs and a permitted file stays ``Stored``
until it is published. What the scanner found in a blob is remembered by its ID,
so each version is read and scanned once per process, not on every pass.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
from typing import Callable, Iterable, Iterator

from src.library_history import HistoryError
from src.user_sync import scope
from src.user_library import REPO_META

PORTABLE_REFS = "refs/agents-core/portable/"
INCOMING_REFS = "refs/agents-core/incoming/"
RECEIVED_REFS = "refs/agents-core/received/"
EVENT_FILE = ".agents-history-event.json"
VERDICT_LIMIT = 65536  # scanned blobs remembered by ID; an entry is a pattern name, a digest and a size

# ``(blob ID, bytes)`` of each requested blob, e.g. ``LibraryHistory.read_blobs``.
Reader = Callable[[list[str]], Iterable[tuple[str, bytes]]]
_verdicts: OrderedDict[tuple[str, str | None], tuple[str | None, str, int]] = OrderedDict()
_verdict_lock = threading.Lock()


@dataclass(frozen=True)
class Stored:
    """A permitted file version still in the library's object store, by blob ID and SHA-256."""

    oid: str
    sha256: str


def clear_cache() -> None:
    """Forget every remembered scan; the next pass reads and scans each blob again."""
    with _verdict_lock:
        _verdicts.clear()


def legacy_event(path: str, identifier: str, raw: bytes) -> dict[str, bytes]:
    """An accepted old remote archive, kept privately until normal publication filtering."""
    content = "content/" + path
    stamp = datetime.strptime(identifier.split("-", 1)[0], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
    metadata = {"format": 1, "source_id": identifier, "version": identifier,
                "path": path, "action": "import", "time": stamp.isoformat(),
                "archive": True, "deleted": identifier.endswith("-deleted"),
                "previous_path": None, "content_path": content, "changes": {}}
    return {content: raw, EVENT_FILE: json.dumps(metadata, sort_keys=True).encode() + b"\n"}


def restorable(tree: dict[str, bytes]) -> bool:
    metadata = json.loads(tree[EVENT_FILE])
    return bool(metadata.get("path") and (metadata.get("content_path") if metadata.get("archive")
                                          else metadata.get("previous_path")))


def _digest(value: bytes | Stored) -> str:
    return value.sha256 if isinstance(value, Stored) else hashlib.sha256(value).hexdigest()


def same(left: dict | None, right: dict | None) -> bool:
    """Two version trees hold the same bytes, whether their files were read or are ``Stored``."""
    if left is None or right is None or left.keys() != right.keys():
        return False
    return all(a == b if isinstance(a, bytes) and isinstance(b, bytes) else _digest(a) == _digest(b)
               for a, b in ((left[name], right[name]) for name in left))


def materialize(tree: dict[str, bytes | Stored], read: Reader) -> dict[str, bytes]:
    """The tree with the bytes of each ``Stored`` file, which must still be the bytes its scan saw.

    Every file is checked, since one blob may sit at paths whose scans were remembered apart. A
    blob that no longer matches loses its remembered scans: the next pass reads and scans it again.
    """
    stored = [value for value in tree.values() if isinstance(value, Stored)]
    contents = dict(read(list(dict.fromkeys(value.oid for value in stored)))) if stored else {}
    digests = {oid: hashlib.sha256(data).hexdigest() for oid, data in contents.items()}
    for value in stored:
        if digests.get(value.oid) != value.sha256:
            with _verdict_lock:
                for key in [key for key in _verdicts if key[0] == value.oid]:
                    del _verdicts[key]
            raise HistoryError(f"flow_history: history blob {value.oid} does not hold the bytes that were scanned")
    return {name: contents[value.oid] if isinstance(value, Stored) else value for name, value in tree.items()}


@dataclass
class Publication:
    policy: str
    events: list[tuple[str, dict[str, bytes | Stored]]] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)

    def identity(self) -> list:
        return [[source, [[path, _digest(data)] for path, data in sorted(tree.items())]]
                for source, tree in self.events]


def _repository_metadata(path: str) -> bool:
    return path.startswith("repos/") and path.count("/") == 2 and path.endswith("/" + REPO_META)


def _scan_key(oid: str, path: str) -> tuple[str, str | None]:
    # MCP records are validated against their path; any other verdict depends on the bytes only.
    return oid, path if path.startswith("mcp/") else None


def _verdict(data: bytes, path: str) -> tuple[str | None, str, int]:
    return scope.scan(data, path=path), scope.content_hash(data), len(data)


def _files(event: dict) -> Iterator[tuple[str, object]]:
    primary = event.get("path")
    if primary is not None:
        if not event.get("archive"):
            yield primary, event.get("previous")
        yield primary, event.get("content")
    yield from event.get("changes", {}).items()


def _prime(events: list[dict], permitted: Callable[[str], bool], read: Reader
           ) -> tuple[dict[tuple[str, str | None], tuple[str | None, str, int]], dict[str, bytes]]:
    """This pass's verdicts for the events publication keeps, scanning only blobs no earlier pass
    scanned, and the bytes of repository metadata, which publication rewrites."""
    wanted, metadata = {}, set()
    for event in events:
        for path, oid in _files(event):
            if oid is None or not permitted(path):
                continue
            if _repository_metadata(path):
                metadata.add(oid)
            else:
                wanted[_scan_key(oid, path)] = path
    verdicts = {}
    with _verdict_lock:
        for key in wanted:
            if key in _verdicts:
                _verdicts.move_to_end(key)
                verdicts[key] = _verdicts[key]
    missing: dict[str, list[str]] = {}
    for key, path in wanted.items():
        if key not in verdicts:
            missing.setdefault(key[0], []).append(path)
    raw, needed = {}, [*missing, *(metadata - missing.keys())]
    for oid, data in read(needed) if needed else ():
        if oid in metadata:
            raw[oid] = data
        for path in missing.get(oid, ()):
            verdicts[_scan_key(oid, path)] = verdict = _verdict(data, path)
            with _verdict_lock:
                _verdicts[_scan_key(oid, path)] = verdict
                while len(_verdicts) > VERDICT_LIMIT:
                    _verdicts.popitem(last=False)
    return verdicts, raw


def publication(events: list[dict], rules: scope.Rules, *, generation: str | None = None,
                read: Reader | None = None) -> Publication:
    """Filter current and prior bytes of each event using today's publication policy.

    History exclusion applies to the entire event stream. A per-file exclusion
    also excludes that file's old contents. Credentials are checked even if they
    have already disappeared from the working tree. With ``read``, the events hold
    blob IDs (``LibraryHistory.export_index``) and permitted files stay ``Stored``.
    """
    identity = {"scopes": rules.scopes.dump().decode(), "portable": rules.portable,
                "held_groups": sorted(rules.held_groups), "held_files": sorted(rules.held_files)}
    if generation is not None:
        identity["generation"] = generation
    policy = json.dumps(identity, sort_keys=True)
    result = Publication(hashlib.sha256(policy.encode()).hexdigest()[:20])
    if "history" in rules.scopes.exclude or "history" in rules.held_groups:
        return result

    def permitted(path: str) -> bool:
        return isinstance(path, str) and rules.reason(path) is None and not path.startswith(".history/")

    def kept(event: dict) -> bool:
        primary = event.get("path")
        if primary is not None and not permitted(primary):
            return False
        legacy = event.get("legacy_id") or event.get("version")
        if primary and event.get("archive") and isinstance(legacy, str) \
                and re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{12}(?:-deleted)?", legacy):
            directory, _, name = primary.rpartition("/")
            alias = f".history/{directory}/{name.removesuffix('.md')}/{legacy}.md"
            return rules.reason(alias) is None
        return True

    events = [event for event in events if kept(event)]
    verdicts, originals = _prime(events, permitted, read) if read is not None else ({}, {})
    for event in events:
        primary = event.get("path")
        source = event["id"]
        tree: dict[str, bytes | Stored] = {}
        metadata = {"format": 1, "source_id": source, "version": event.get("version", source),
                    "path": primary, "action": event.get("action", "save"), "time": event.get("time"),
                    "archive": bool(event.get("archive")), "deleted": bool(event.get("deleted")),
                    "previous_path": None, "content_path": None, "changes": {}}

        def put(path: str, data: bytes | str | None, prefix: str) -> str | None:
            if data is None or not permitted(path):
                return None
            stored = read is not None and not _repository_metadata(path)
            if read is not None and not stored:
                data = originals[data]
            if _repository_metadata(path):
                # A private baseline may predate the origin/path metadata split.
                # Publish the portable origin only, including in historical trees.
                origin = scope.portable_origin(data)
                if origin is None:
                    return None
                data = json.dumps({"origin": origin}, indent=2).encode() + b"\n"
            pattern, digest, size = verdicts[_scan_key(data, path)] if stored else _verdict(data, path)
            if size > scope.MAX_FILE_BYTES or (pattern and
                    (pattern == "invalid_mcp_record" or digest not in rules.scopes.allow)):
                result.blocked.append({"path": path, "version": source, "sha256": digest,
                                       "pattern": pattern or "size"})
                return None
            name = f"{prefix}/{path}"
            tree[name] = Stored(data, digest) if stored else data
            return name

        if primary is not None:
            if not metadata["archive"]:
                metadata["previous_path"] = put(primary, event.get("previous"), "previous")
            metadata["content_path"] = put(primary, event.get("content"), "content")
        for path, raw in sorted(event.get("changes", {}).items()):
            if not permitted(path):
                continue
            if raw is None:
                metadata["changes"][path] = None
            else:
                value = put(path, raw, "changes")
                if value is not None:
                    metadata["changes"][path] = value
        if not tree and not metadata["changes"]:
            continue
        metadata["paths"] = sorted(metadata["changes"])
        tree[EVENT_FILE] = json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode() + b"\n"
        # Legacy aliases are unique within one flow, not across the library:
        # two flows archived at the same instant may have identical bytes.
        key = hashlib.sha256(json.dumps([primary, source], separators=(",", ":")).encode()).hexdigest()
        result.events.append((key, tree))
    return result
