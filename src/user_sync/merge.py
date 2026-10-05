"""The conflict policy: a three-way merge of whole files, never of text.

Inputs are trees as maps of relative path to blob id: the common base (empty when two libraries
join without shared history), this machine's commit and the remote's. Per path:

* the same on both sides, or changed on one side only: that version;
* ``.agents-library.json`` created on both sides: the remote marker (only ``format`` matters);
* ``components.json`` and ``.agents-sync/scopes.json``: a three-way merge of their sets, which
  cannot conflict, because each machine can only add or remove entries relative to the base;
* changed differently on both sides: the remote version, because it reached the remote first.
  The local text of a flow becomes a normal ``.history`` version; for other files the record
  carries it. A conflict record goes to ``.agents-sync/conflicts/<id>.json``;
* changed on one side and deleted on the other: the changed version (edits beat deletions), with a
  "deletion undone" record. A flow's text, override marker and persona overlay move as one: a flow
  deleted on one machine and edited on the other comes back whole.

Names in ``.history`` and of conflict records are unique (UTC time to the microsecond plus a
hash), so the union of two machines' entries never collides.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import base64
import hashlib
import json
import re
from typing import Callable

from src.user_library import MARKER
from src.user_sync.scope import COMPONENTS, CONFLICTS_DIR, SCOPES_PATH, merge_scopes, parse_scopes

_FLOW_ID = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_FLOW_DIR = r"common|repos/[a-z0-9_][a-z0-9._-]*"
_FLOW_FILE = re.compile(rf"(?P<dir>{_FLOW_DIR})/(?P<id>{_FLOW_ID})(?P<kind>\.md|\.meta\.json)")
_PERSONA_FILE = re.compile(rf"personas/(?P<dir>{_FLOW_DIR})/(?P<id>{_FLOW_ID})\.json")
_COMPONENT_KINDS = ("rules", "skills", "implants")
_TEXT_LIMIT = 256 * 1024

Tree = dict[str, str]


@dataclass
class MergeResult:
    """``tree`` maps each path to an existing blob id, or to new bytes the caller stores."""

    tree: dict[str, str | bytes] = field(default_factory=dict)
    conflicts: list[dict] = field(default_factory=list)


def flow_unit(path: str) -> tuple[str, str] | None:
    """``(directory, id)`` when ``path`` is a flow's text, override marker or persona overlay."""
    match = _FLOW_FILE.fullmatch(path) or _PERSONA_FILE.fullmatch(path)
    return (match["dir"], match["id"]) if match else None


def unit_members(directory: str, flow_id: str) -> tuple[str, str, str]:
    return (f"{directory}/{flow_id}.md", f"{directory}/{flow_id}.meta.json",
            f"personas/{directory}/{flow_id}.json")


def flow_reference(path: str) -> dict:
    """``{"flow": "user:<id>"}`` or ``{"flow": "repo:<id>", "repo_key": key}`` for a flow's files."""
    unit = flow_unit(path)
    if unit is None:
        return {}
    directory, flow_id = unit
    if directory == "common":
        return {"flow": f"user:{flow_id}"}
    return {"flow": f"repo:{flow_id}", "repo_key": directory.split("/", 1)[1]}


def history_path(directory: str, flow_id: str, data: bytes, now: datetime) -> str:
    """Where ``src.user_flows.FlowLibrary`` keeps an earlier version of this flow."""
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    return f".history/{directory}/{flow_id}/{stamp}-{hashlib.sha256(data).hexdigest()[:12]}.md"


def _components(data: bytes | None) -> dict[str, frozenset[str]] | None:
    """The disabled sets of a ``components.json``; None when malformed (that side changes nothing)."""
    if data is None:
        return {kind: frozenset() for kind in _COMPONENT_KINDS}
    try:
        disabled = json.loads(data.decode("utf-8"))["disabled"]
        result = {}
        for kind in _COMPONENT_KINDS:
            values = disabled.get(kind, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                return None
            result[kind] = frozenset(values)
        return result
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _merge_components(b: bytes | None, l: bytes | None, r: bytes | None) -> bytes:
    base = _components(b) or {kind: frozenset() for kind in _COMPONENT_KINDS}
    local = _components(l) or base
    remote = _components(r) or base
    merged = {kind: sorted((remote[kind] | (local[kind] - base[kind])) - (base[kind] - local[kind]))
              for kind in _COMPONENT_KINDS}
    # The format of src.component_toggles, so a later switch rewrites nothing.
    return json.dumps({"disabled": merged}, indent=2).encode("utf-8") + b"\n"


def conflict_record(path: str, kind: str, kept: str, *, label: str, now: datetime,
                    local_blob: str | None, remote_blob: str | None, **details) -> dict:
    """A record for ``.agents-sync/conflicts/<id>.json``; ids from two machines never collide."""
    seed = f"{label}\0{path}\0{local_blob}\0{remote_blob}".encode()
    record_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{hashlib.sha256(seed).hexdigest()[:10]}"
    return {"id": record_id, "path": path, **flow_reference(path), "kind": kind, "kept": kept,
            "machine": label, "time": now.isoformat(timespec="seconds"), **details}


def record_file(record: dict) -> tuple[str, bytes]:
    """``(path, bytes)`` of a conflict record."""
    return (f"{CONFLICTS_DIR}/{record['id']}.json",
            json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")


def keep_local(path: str, data: bytes, now: datetime) -> tuple[dict, dict[str, bytes]]:
    """Where a losing local version goes: ``(record details, new files)``.

    A flow's text becomes a normal ``.history`` version, so the editor lists it and "use mine" is
    an ordinary restore; any other file is carried inside the record.
    """
    unit = flow_unit(path)
    if unit and path.endswith(".md"):
        archived = history_path(*unit, data, now)
        return {"local_version": archived}, {archived: data}
    if len(data) <= _TEXT_LIMIT:
        try:
            return {"local_content": data.decode("utf-8")}, {}
        except UnicodeDecodeError:
            pass
    return {"local_content_base64": base64.b64encode(data).decode("ascii")}, {}


class Merger:
    """One three-way merge; ``read(blob_id)`` returns a blob's bytes."""

    def __init__(self, base: Tree, local: Tree, remote: Tree, read: Callable[[str], bytes], *,
                 label: str, now: datetime, local_commit: str | None, remote_commit: str | None):
        self.base, self.local, self.remote = base, local, remote
        self.read, self.label, self.now = read, label, now
        self.commits = {"local_commit": local_commit, "remote_commit": remote_commit}
        self.result = MergeResult()

    def run(self) -> MergeResult:
        paths = set(self.base) | set(self.local) | set(self.remote)
        handled = self._flow_units(paths)
        for path in sorted(paths - handled):
            self._merge_path(path)
        return self.result

    # --- helpers --------------------------------------------------------------------------

    def _bytes(self, blob: str | None) -> bytes | None:
        return None if blob is None else self.read(blob)

    def _put(self, path: str, value: str | bytes | None) -> None:
        if value is None:
            self.result.tree.pop(path, None)
        else:
            self.result.tree[path] = value

    def _record(self, path: str, kind: str, kept: str, **details) -> dict:
        record = conflict_record(path, kind, kept, label=self.label, now=self.now,
                                 local_blob=self.local.get(path), remote_blob=self.remote.get(path),
                                 **self.commits, **details)
        self.result.conflicts.append(record)
        self._put(*record_file(record))
        return record

    def _keep_local_text(self, path: str, data: bytes) -> dict:
        details, files = keep_local(path, data, self.now)
        for name, content in files.items():
            self._put(name, content)
        return details

    # --- flows as units -------------------------------------------------------------------

    def _flow_units(self, paths: set[str]) -> set[str]:
        """Undo the deletion of a whole flow that the other side edited; returns the handled paths.

        The deleting side's missing members count as unchanged, then each member merges by the
        ordinary rules, so no member's change from either side is lost.
        """
        handled = set()
        for unit in sorted({unit for unit in map(flow_unit, paths) if unit}):
            members = unit_members(*unit)
            text = members[0]
            if text not in self.base or (text in self.local) == (text in self.remote):
                continue  # a new flow, or deleted on both or neither side: the ordinary rules apply
            deleted_on = "local" if text not in self.local else "remote"
            deleting, editing = (self.local, self.remote) if deleted_on == "local" else (self.remote, self.local)
            if all(editing.get(m) == self.base.get(m) for m in members):
                continue  # deleted on one side and untouched on the other: the deletion stands
            restored = {m: deleting[m] if m in deleting else self.base.get(m) for m in members}
            for member in members:
                local, remote = (restored[member], self.remote.get(member)) if deleted_on == "local" \
                    else (self.local.get(member), restored[member])
                self._merge_path(member, local=local, remote=remote)
            self._record(text, "deletion_undone", "remote" if deleted_on == "local" else "local",
                         deleted_on=deleted_on)
            handled.update(members)
        return handled

    # --- one path -------------------------------------------------------------------------

    _UNSET = object()

    def _merge_path(self, path: str, *, local=_UNSET, remote=_UNSET) -> None:
        b = self.base.get(path)
        l = self.local.get(path) if local is self._UNSET else local
        r = self.remote.get(path) if remote is self._UNSET else remote
        if l == r:
            self._put(path, l)
        elif l == b:
            self._put(path, r)
        elif r == b:
            self._put(path, l)
        elif path == MARKER:
            self._put(path, r if r is not None else l)
        elif path == COMPONENTS:
            remote_bytes = self._bytes(r)
            merged = _merge_components(self._bytes(b), self._bytes(l), remote_bytes)
            self._put(path, r if remote_bytes == merged else merged)
        elif path == SCOPES_PATH:
            merged = merge_scopes(*(parse_scopes(self._bytes(blob)) for blob in (b, l, r)))
            same = r is not None and parse_scopes(self.read(r)) == merged
            self._put(path, r if same else merged.dump())
        elif l is None:
            self._put(path, r)
            self._record(path, "deletion_undone", "remote", deleted_on="local")
        elif r is None:
            self._put(path, l)
            self._record(path, "deletion_undone", "local", deleted_on="remote")
        else:
            self._put(path, r)
            self._record(path, "both_changed", "remote", **self._keep_local_text(path, self.read(l)))


def merge(base: Tree, local: Tree, remote: Tree, read: Callable[[str], bytes], *, label: str,
          now: datetime, local_commit: str | None = None, remote_commit: str | None = None) -> MergeResult:
    return Merger(base, local, remote, read, label=label, now=now,
                  local_commit=local_commit, remote_commit=remote_commit).run()
