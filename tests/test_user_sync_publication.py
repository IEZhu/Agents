"""Publication of stored history: read lazily, each blob scanned once per process (#535)."""
from __future__ import annotations

import json

import pytest

from src.library_history import HistoryError
from src.user_sync import scope, versions
from src.user_sync.scope import blob_id
from tests.test_user_sync import TOKEN

GROUP = "repos/github.com-owner-project"
RULES = scope.Rules(scope.Scopes(), {GROUP: "github.com/owner/project"})
METADATA = f"{GROUP}/.repo.json"
LEGACY = "20260101T000000000000Z-0123456789ab"  # an archive's legacy ID, as .history/<flow>/<ID>.md named it


@pytest.fixture(autouse=True)
def unscanned():
    """No remembered scans before or after the test."""
    versions.clear_cache()
    yield
    versions.clear_cache()


@pytest.fixture
def scans(monkeypatch):
    """The path of every file the scanner reads."""
    calls, original = [], versions._verdict
    def spy(data, path):
        calls.append(path)
        return original(data, path)
    monkeypatch.setattr(versions, "_verdict", spy)
    return calls


class Store:
    """Blobs by ID, as in the library's object store, and the IDs each read asked for."""

    def __init__(self):
        self.blobs, self.reads = {}, []

    def put(self, data):
        if data is None:
            return None
        oid = blob_id(data)
        self.blobs[oid] = data
        return oid

    def read(self, oids):
        self.reads.append(list(oids))
        return [(oid, self.blobs[oid]) for oid in oids]

    def stored(self, events):
        """The events as ``LibraryHistory.export_index`` returns them: blob IDs in place of bytes."""
        return [{**event, "content": self.put(event["content"]), "previous": self.put(event["previous"]),
                 "changes": {path: self.put(raw) for path, raw in event["changes"].items()}}
                for event in events]


def event(number, path, content=None, previous=None, changes=None):
    commit = f"{number:040x}"
    return {"id": "git-" + commit, "version": "git-" + commit, "commit": commit, "parent": None,
            "action": "save", "path": path, "paths": sorted(changes or {}), "archive": False,
            "time": f"2026-10-01T00:00:{number:02d}+00:00", "deleted": False, "content": content,
            "previous": previous, "changes": changes or {}, "legacy_id": None}


def history():
    shared, secret = "common/shared.md", f"# Secret\n{TOKEN}\n".encode()
    metadata = json.dumps({"origin": "github.com/owner/project", "path": "/private/clone"}).encode()
    return [
        event(1, shared, b"# B\n", b"# A\n", {shared: b"# B\n"}),
        event(2, shared, secret, b"# B\n", {shared: secret}),
        event(3, shared, b"# C\n", secret, {shared: b"# C\n"}),
        event(4, None, changes={METADATA: metadata, f"{GROUP}/deploy.md": b"# Deploy\n"}),
        event(5, "common/large.md", b"x" * (scope.MAX_FILE_BYTES + 1), None,
              {"common/large.md": b"x" * (scope.MAX_FILE_BYTES + 1)}),
        event(6, None, changes={"common/notes.md": b"{}\n", "mcp/servers/demo.json": b"{}\n"}),
        event(7, "common/shared.md", None, b"# C\n", {shared: None}),
        archived(8, "common/archived.md", b"# Archived\n"),
    ]


def archived(number, path, content):
    return {**event(number, path, content), "archive": True, "legacy_id": LEGACY}


def assert_same(lazy, plain, read):
    assert lazy.policy == plain.policy
    assert lazy.identity() == plain.identity()
    assert lazy.blocked == plain.blocked
    assert [source for source, _ in lazy.events] == [source for source, _ in plain.events]
    for (_, stored), (_, raw) in zip(lazy.events, plain.events):
        assert versions.same(stored, raw) and versions.same(raw, stored)
        assert versions.materialize(stored, read) == raw


def test_stored_history_publishes_exactly_what_its_bytes_would():
    store = Store()
    plain = versions.publication(history(), RULES)
    lazy = versions.publication(store.stored(history()), RULES, read=store.read)
    assert_same(lazy, plain, store.read)
    assert {item["pattern"] for item in plain.blocked} == {"github_token", "size", "invalid_mcp_record"}
    # Only rewritten repository metadata is held as bytes; every other file stays in the store.
    held = {name for _, tree in lazy.events for name, value in tree.items() if not isinstance(value, versions.Stored)}
    assert held == {versions.EVENT_FILE, f"changes/{METADATA}"}
    assert b"/private/clone" not in b"".join(tree[f"changes/{METADATA}"] for _, tree in lazy.events
                                             if f"changes/{METADATA}" in tree)


def test_each_blob_is_read_and_scanned_once(scans):
    store = Store()
    first = versions.publication(store.stored(history()), RULES, read=store.read)
    assert sorted(scans) == sorted(["common/shared.md"] * 4 + ["common/large.md", f"{GROUP}/deploy.md", METADATA,
                                    "common/notes.md", "mcp/servers/demo.json", "common/archived.md"])
    scans.clear()
    store.reads.clear()
    again = versions.publication(store.stored(history()), RULES, read=store.read)
    assert scans == [METADATA]  # rewritten from its bytes on every pass, which are tiny
    assert store.reads == [[store.put(dict(history()[3]["changes"])[METADATA])]]
    assert again.identity() == first.identity() and again.blocked == first.blocked


def test_an_allowed_secret_is_published_without_another_scan(scans):
    store = Store()
    versions.publication(store.stored(history()), RULES, read=store.read)
    secret = next(item["sha256"] for item in versions.publication(history(), RULES).blocked
                  if item["pattern"] == "github_token")
    allowed = scope.Rules(scope.Scopes(allow=frozenset({secret})), RULES.portable)
    scans.clear()
    lazy = versions.publication(store.stored(history()), allowed, read=store.read)
    assert scans == [METADATA]
    assert_same(lazy, versions.publication(history(), allowed), store.read)
    assert all(item["pattern"] != "github_token" for item in lazy.blocked)


@pytest.mark.parametrize("order", [("common/notes.md", "mcp/servers/demo.json"),
                                   ("mcp/servers/demo.json", "common/notes.md")])
def test_an_mcp_verdict_belongs_to_its_path(order):
    """The same bytes are a valid note and an invalid MCP record, whichever is scanned first."""
    events = [event(number, None, changes={path: b"{}\n"}) for number, path in enumerate(order, start=1)]
    store = Store()
    for _ in range(2):
        lazy = versions.publication(store.stored(events), RULES, read=store.read)
        assert_same(lazy, versions.publication(events, RULES), store.read)
        assert [item["path"] for item in lazy.blocked] == ["mcp/servers/demo.json"]


def test_materialize_refuses_bytes_other_than_those_scanned_and_the_next_pass_scans_them():
    store = Store()
    events = store.stored(history())
    tree = versions.publication(events, RULES, read=store.read).events[0][1]
    oid = next(value.oid for value in tree.values() if isinstance(value, versions.Stored))
    store.blobs[oid] = b"# Replaced\n"
    with pytest.raises(HistoryError, match="does not hold the bytes that were scanned"):
        versions.materialize(tree, store.read)
    assert all(key[0] != oid for key in versions._verdicts)
    store.reads.clear()
    tree = versions.publication(events, RULES, read=store.read).events[0][1]
    assert oid in store.reads[0]
    assert b"# Replaced\n" in versions.materialize(tree, store.read).values()


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_materialize_checks_every_file_of_a_blob(order):
    """One blob at two paths whose scans were remembered apart: each file must match its own scan."""
    store = Store()
    oid = store.put(b"# Now\n")
    files = [versions.Stored(oid, scope.content_hash(b"# Earlier\n")), versions.Stored(oid, scope.content_hash(b"# Now\n"))]
    tree = {f"changes/common/{name}": files[index] for name, index in zip(("a.md", "b.md"), order)}
    with pytest.raises(HistoryError, match="does not hold the bytes that were scanned"):
        versions.materialize(tree, store.read)


def test_a_pass_keeps_its_verdicts_when_the_cache_is_smaller(monkeypatch, scans):
    monkeypatch.setattr(versions, "VERDICT_LIMIT", 2)
    store = Store()
    lazy = versions.publication(store.stored(history()), RULES, read=store.read)
    assert_same(lazy, versions.publication(history(), RULES), store.read)
    assert len(versions._verdicts) == 2


def test_the_least_recently_used_scan_is_forgotten_first(monkeypatch):
    monkeypatch.setattr(versions, "VERDICT_LIMIT", 2)
    store = Store()
    one, two, three = (event(number, None, changes={f"common/{number}.md": f"# {number}\n".encode()})
                       for number in (1, 2, 3))
    versions.publication(store.stored([one, two]), RULES, read=store.read)
    versions.publication(store.stored([one, three]), RULES, read=store.read)  # uses one's scan again
    assert [oid for oid, _ in versions._verdicts] == [store.put(b"# 1\n"), store.put(b"# 3\n")]


def test_unpermitted_files_are_never_read():
    store = Store()
    archive = archived(3, "common/archived.md", b"# Archived\n")
    events = [event(1, "common/private.md", b"# Private\n", b"# Old\n", {"common/private.md": b"# Private\n"}),
              event(2, None, changes={"repos/github.com-other-tool/notes.md": b"# Local only\n"}), archive]
    published = versions.publication(store.stored([archive]), RULES, read=store.read)
    assert len(published.events) == 1 and store.reads  # without its legacy alias excluded
    versions.clear_cache()
    store.reads.clear()
    excluded = scope.Rules(scope.Scopes(exclude_files=frozenset({"common/private.md",
                                                                f".history/common/archived/{LEGACY}.md"})),
                           RULES.portable)
    lazy = versions.publication(store.stored(events), excluded, read=store.read)
    assert lazy.events == [] and lazy.blocked == []
    assert store.reads == []
