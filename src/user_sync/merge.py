"""The conflict policy: a three-way merge of whole files, and of the lines of text files.

Inputs are trees as maps of relative path to blob id: the common base (empty when two libraries
join without shared history), this machine's commit and the remote's. Per path:

* the same on both sides, or changed on one side only: that version;
* ``.agents-library.json`` created on both sides: the remote marker (only ``format`` matters);
* ``components.json`` and ``.agents-sync/scopes.json``: a three-way merge of their sets, which
  cannot conflict, because each machine can only add or remove entries relative to the base;
* a text file the owner edits (a flow's Markdown, a script; not JSON, history or records) changed
  differently on both sides: a line-based three-way merge (``merge_text``, the diff3 algorithm)
  against the common base. When the changes touch different lines, the merged text goes to both
  sides; a flow's local text before the merge becomes a normal ``.history`` version, and
  ``MergeResult.merged`` lists the file;
* changed differently on both sides otherwise (the changes overlap, there is no common base, or
  the file is not mergeable text): the remote version, because it reached the remote first. The
  local text of a flow becomes a normal ``.history`` version; for other files the record carries
  it. A conflict record goes to ``.agents-sync/conflicts/<id>.json``, with the ``reason`` the
  text could not be merged (``overlapping_changes`` with the number of ``overlaps``,
  ``no_common_base``, ``not_text``, ``too_large`` or ``too_complex``, when the changed lines are too
  many to match within ``MATCH_STEPS``; the engine adds ``changed_during_sync`` and
  ``held_open`` for files it could not write);
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
from src.user_sync.scope import COMPONENTS, CONFLICTS_DIR, SCOPES_PATH, is_content, merge_scopes, parse_scopes

_FLOW_ID = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_FLOW_DIR = r"common|repos/[a-z0-9_][a-z0-9._-]*"
_FLOW_FILE = re.compile(rf"(?P<dir>{_FLOW_DIR})/(?P<id>{_FLOW_ID})(?P<kind>\.md|\.meta\.json)")
_PERSONA_FILE = re.compile(rf"personas/(?P<dir>{_FLOW_DIR})/(?P<id>{_FLOW_ID})\.json")
_COMPONENT_KINDS = ("rules", "skills", "implants")
_TEXT_LIMIT = 256 * 1024  # also the largest text merged line by line (flows are at most 256 KiB)
# The most steps the search for one side's changed lines may take. The search (Myers) costs
# about (N + M) * D steps for N and M lines that differ in D lines, whatever repeats; a merge
# runs under the library lock, and this bound keeps a side at about 0.2 s (measured on an Apple
# M-series laptop), while a real flow of 500 lines with edits on both sides merges in milliseconds.
MATCH_STEPS = 1_000_000

Tree = dict[str, str]


@dataclass
class MergeResult:
    """``tree`` maps each path to an existing blob id, or to new bytes the caller stores.

    ``merged`` lists the text files whose changes from both sides were merged line by line.
    """

    tree: dict[str, str | bytes] = field(default_factory=dict)
    conflicts: list[dict] = field(default_factory=list)
    merged: list[dict] = field(default_factory=list)


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


# --- text ---------------------------------------------------------------------------------


def _lines(text: str) -> list[str]:
    """Lines with their ``\n``; only ``\n`` ends a line, so every other character round-trips."""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _common(a: list[str], b: list[str], budget: int) -> list[tuple[int, int]] | None:
    """The line pairs a shortest edit script between ``a`` and ``b`` keeps, in order (Myers, 1986).

    Round d finds, for each diagonal k = x - y in -d, -d + 2, ..., d, the furthest point a path
    with d insertions and deletions reaches, so the search takes about (N + M) * D steps for D
    differing lines, whatever repeats; it gives up with None after ``budget`` steps. Each round
    keeps its predecessor's points (D * D numbers at most) to walk back along the path.
    """
    n, m = len(a), len(b)
    if not n or not m:
        return []
    previous = [0, 0]   # round d - 1's furthest x of diagonal k at index k + d; round -1: x 0 on k 1
    rounds, steps = [], 0
    for d in range(n + m + 1):
        rounds.append(previous)
        current = [0] * (2 * d + 3)  # this round's furthest x of diagonal k at index k + d + 1
        steps += d + 1
        for k in range(-d, d + 1, 2):
            # The inline form of _down: this loop is the hot path.
            if d == 0 or k == -d or (k != d and previous[k - 1 + d] < previous[k + 1 + d]):
                x = previous[k + 1 + d]       # from diagonal k + 1: a line of b inserted
            else:
                x = previous[k - 1 + d] + 1   # from diagonal k - 1: a line of a deleted
            y = x - k
            run = x
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            steps += x - run
            if steps > budget:
                return None
            current[k + d + 1] = x
            if x >= n and y >= m:
                return _walk_back(rounds, n, m)
        previous = current
    return None  # not reached: round n + m always ends at (n, m)


def _down(previous: list[int], d: int, k: int) -> bool:
    """Whether round ``d`` reaches diagonal ``k`` from diagonal k + 1 (an insertion)."""
    return d == 0 or k == -d or (k != d and previous[k - 1 + d] < previous[k + 1 + d])


def _walk_back(rounds: list[list[int]], x: int, y: int) -> list[tuple[int, int]]:
    """The kept line pairs of the path ``_common`` found, walking from its end to its start."""
    pairs = []
    for d in range(len(rounds) - 1, -1, -1):
        previous, k = rounds[d], x - y
        if d == 0:
            arrival, back = 0, (0, 0)
        else:
            down = _down(previous, d, k)
            from_k = k + 1 if down else k - 1
            from_x = previous[from_k + d]
            arrival, back = (from_x if down else from_x + 1), (from_x, from_x - from_k)
        while x > arrival:  # the diagonal run this round followed after its move
            x, y = x - 1, y - 1
            pairs.append((x, y))
        x, y = back
    pairs.reverse()
    return pairs


def _matches(base: list[str], other: list[str]) -> dict[int, int] | None:
    """Base line -> the other side's line, for the lines a diff keeps (increasing on both sides).

    The common start and end match as they are; the lines between them go to ``_common``. None
    when that search would take more than ``MATCH_STEPS``.
    """
    shortest = min(len(base), len(other))
    start = next((i for i in range(shortest) if base[i] != other[i]), shortest)
    end = next((k for k in range(shortest - start) if base[-1 - k] != other[-1 - k]), shortest - start)
    middle = _common(base[start:len(base) - end], other[start:len(other) - end], MATCH_STEPS)
    if middle is None:
        return None
    pairs = {i: i for i in range(start)}
    pairs.update((start + i, start + j) for i, j in middle)
    pairs.update((len(base) - end + k, len(other) - end + k) for k in range(end))
    return pairs


def merge_lines(base: list[str], local: list[str], remote: list[str]) -> tuple[list[str] | None, int]:
    """The diff3 merge: ``(lines, 0)``, or ``(None, overlaps)`` when both sides changed one region;
    ``(None, 0)`` when matching a side's changes would take too long (``MATCH_STEPS``).

    Lines kept in the same order by both sides split the texts into stable runs and the regions
    between them. A region only one side changed takes that side's text; one both sides changed
    the same way is taken once; one both sides changed differently, or where both inserted
    different lines at one place, overlaps. Changes on adjacent lines, with no unchanged line
    between them, form one region, so they overlap too.
    """
    if local == base or local == remote:
        return remote, 0
    if remote == base:
        return local, 0
    to_local = _matches(base, local)
    to_remote = _matches(base, remote) if to_local is not None else None
    if to_local is None or to_remote is None:
        return None, 0
    merged: list[str] = []
    overlaps = 0
    o = a = b = 0  # the next line of base, local and remote
    while True:
        stable = 0
        while o + stable < len(base) and to_local.get(o + stable) == a + stable \
                and to_remote.get(o + stable) == b + stable:
            stable += 1
        if stable:
            merged += base[o:o + stable]
            o, a, b = o + stable, a + stable, b + stable
            continue
        end = next((j for j in range(o, len(base)) if to_local.get(j, -1) >= a and to_remote.get(j, -1) >= b),
                   len(base))
        end_a, end_b = (to_local[end], to_remote[end]) if end < len(base) else (len(local), len(remote))
        region, ours, theirs = base[o:end], local[a:end_a], remote[b:end_b]
        if not (region or ours or theirs):
            break
        if ours == region or ours == theirs:
            merged += theirs
        elif theirs == region:
            merged += ours
        else:
            overlaps += 1
        o, a, b = end, end_a, end_b
    return (None, overlaps) if overlaps else (merged, 0)


def text_merge_refusal(path: str, base: bytes | None, local: bytes, remote: bytes) -> str | None:
    """Why ``path`` cannot be merged line by line, or None when it can.

    Only text the owner edits merges: flows, scripts and other files of ``common/`` and
    ``repos/<key>/``; JSON (persona overlays, override markers) would merge into invalid JSON.
    """
    if not is_content(path) or path.endswith(".json"):
        return "not_text"
    if base is None:
        return "no_common_base"
    if max(len(base), len(local), len(remote)) > _TEXT_LIMIT:
        return "too_large"
    try:
        texts = [data.decode("utf-8") for data in (base, local, remote)]
    except UnicodeDecodeError:
        return "not_text"
    return "not_text" if any("\0" in text for text in texts) else None


def merge_text(base: bytes, local: bytes, remote: bytes) -> tuple[bytes | None, int]:
    """``merge_lines`` on UTF-8 text: ``(merged bytes, 0)``, or ``(None, overlaps)``; no overlaps
    with None means the changes were too large to match."""
    lines, overlaps = merge_lines(*(_lines(data.decode("utf-8")) for data in (base, local, remote)))
    return (None, overlaps) if lines is None else ("".join(lines).encode("utf-8"), 0)


# --- records ------------------------------------------------------------------------------


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

    def _keep_merged_away(self, path: str, data: bytes) -> dict:
        """Keep a flow's local text that a merge replaces as a ``.history`` version, once.

        A version with the same content already in this machine's history (an earlier cycle that
        stopped half way, or a save) is enough: it is named after the content's hash.
        """
        directory, flow_id = flow_unit(path)
        prefix, suffix = f".history/{directory}/{flow_id}/", f"-{hashlib.sha256(data).hexdigest()[:12]}.md"
        known = next((name for name in self.local if name.startswith(prefix) and name.endswith(suffix)), None)
        return {"local_version": known} if known else self._keep_local_text(path, data)

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
        if path == SCOPES_PATH:
            # Losing the file must not lift every exclusion on every machine: a missing side
            # counts as unchanged.
            l, r = (b if l is None else l), (b if r is None else r)
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
            self._both_changed(path, b, l, r)

    def _both_changed(self, path: str, b: str | None, l: str, r: str) -> None:
        """Merge the lines of a text file both sides changed, else keep the remote version."""
        local = self.read(l)
        remote = self.read(r)
        reason = text_merge_refusal(path, self._bytes(b), local, remote)
        overlaps = 0
        if reason is None:
            merged, overlaps = merge_text(self.read(b), local, remote)
            if merged is not None:
                self._put(path, l if merged == local else r if merged == remote else merged)
                details = {}
                if flow_unit(path) and path.endswith(".md") and merged != local:
                    details = self._keep_merged_away(path, local)
                self.result.merged.append({"path": path, **flow_reference(path), **details})
                return
            reason = "overlapping_changes" if overlaps else "too_complex"
        self._put(path, r)
        self._record(path, "both_changed", "remote", reason=reason,
                     **({"overlaps": overlaps} if overlaps else {}), **self._keep_local_text(path, local))


def merge(base: Tree, local: Tree, remote: Tree, read: Callable[[str], bytes], *, label: str,
          now: datetime, local_commit: str | None = None, remote_commit: str | None = None) -> MergeResult:
    return Merger(base, local, remote, read, label=label, now=now,
                  local_commit=local_commit, remote_commit=remote_commit).run()
