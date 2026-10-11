"""Publish every permitted library revision without publishing private Git ancestry.

Each installation owns an append-only ref for each publication policy. Its commits
contain one filtered event, with blob references described by a small metadata
file. They have no parent in the private history. Fetching these refs lets the
library reader expose intermediate revisions without checkout archive files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re

from src.user_sync import scope
from src.user_library import REPO_META

PORTABLE_REFS = "refs/agents-core/portable/"
INCOMING_REFS = "refs/agents-core/incoming/"
RECEIVED_REFS = "refs/agents-core/received/"
EVENT_FILE = ".agents-history-event.json"


def restorable(tree: dict[str, bytes]) -> bool:
    metadata = json.loads(tree[EVENT_FILE])
    return bool(metadata.get("path") and (metadata.get("content_path") if metadata.get("archive")
                                          else metadata.get("previous_path")))


@dataclass
class Publication:
    policy: str
    events: list[tuple[str, dict[str, bytes]]] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)

    def identity(self) -> list:
        return [[source, [[path, hashlib.sha256(data).hexdigest()] for path, data in sorted(tree.items())]]
                for source, tree in self.events]


def publication(events: list[dict], rules: scope.Rules) -> Publication:
    """Filter current and prior bytes of each event using today's publication policy.

    History exclusion applies to the entire event stream. A per-file exclusion
    also excludes that file's old contents. Credentials are checked even if they
    have already disappeared from the working tree.
    """
    policy = json.dumps({"scopes": rules.scopes.dump().decode(), "portable": rules.portable,
                         "held_groups": sorted(rules.held_groups), "held_files": sorted(rules.held_files)},
                        sort_keys=True)
    result = Publication(hashlib.sha256(policy.encode()).hexdigest()[:20])
    if "history" in rules.scopes.exclude or "history" in rules.held_groups:
        return result

    def permitted(path: str) -> bool:
        return isinstance(path, str) and rules.reason(path) is None and not path.startswith(".history/")

    for event in events:
        primary = event.get("path")
        if primary is not None and not permitted(primary):
            continue
        legacy = event.get("legacy_id") or event.get("version")
        if primary and event.get("archive") and isinstance(legacy, str) \
                and re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{12}(?:-deleted)?", legacy):
            directory, _, name = primary.rpartition("/")
            alias = f".history/{directory}/{name.removesuffix('.md')}/{legacy}.md"
            if rules.reason(alias) is not None:
                continue
        source = event["id"]
        tree: dict[str, bytes] = {}
        metadata = {"format": 1, "source_id": source, "version": event.get("version", source),
                    "path": primary, "action": event.get("action", "save"), "time": event.get("time"),
                    "archive": bool(event.get("archive")), "deleted": bool(event.get("deleted")),
                    "previous_path": None, "content_path": None, "changes": {}}

        def put(path: str, data: bytes | None, prefix: str) -> str | None:
            if data is None or not permitted(path):
                return None
            if path.startswith("repos/") and path.count("/") == 2 and path.endswith("/" + REPO_META):
                # A private baseline may predate the origin/path metadata split.
                # Publish the portable origin only, including in historical trees.
                origin = scope.portable_origin(data)
                if origin is None:
                    return None
                data = json.dumps({"origin": origin}, indent=2).encode() + b"\n"
            pattern, digest = scope.scan(data), scope.content_hash(data)
            if len(data) > scope.MAX_FILE_BYTES or (pattern and digest not in rules.scopes.allow):
                result.blocked.append({"path": path, "version": source, "sha256": digest,
                                       "pattern": pattern or "size"})
                return None
            name = f"{prefix}/{path}"
            tree[name] = data
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
