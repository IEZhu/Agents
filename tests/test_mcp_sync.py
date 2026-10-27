"""MCP inventories travel through real library Git sync without transferring consent."""
import json

import pytest

from src.mcp_registry import Registry
from src.user_sync import scope
from tests.test_mcp_registry import definition
from tests.test_user_sync import machine, pair, records, remote, remote_files, remote_objects  # noqa: F401


@pytest.mark.parametrize("path,expected", [
    ("mcp/servers/example.json", ("mcp",)),
    ("mcp/machines/" + "a" * 32 + ".json", ("mcp",)),
    ("mcp/servers/Upper.json", None), ("mcp/servers/a__b.json", None),
    ("mcp/servers/example.json.bak", None), ("mcp/servers/example/token.json", None),
    ("mcp/machines/host-name.json", None), ("mcp/registry.json", None),
    ("mcp/secrets/example.json", None), ("mcp/machine.json", None),
    ("mcp/catalog.json", None), ("mcp/servers/.tmp-example.json", None),
])
def test_only_definition_and_host_records_sync(path, expected):
    assert scope.groups(path) == expected


def test_two_hosts_sync_inventory_without_transferring_consent(pair):
    a, b = pair
    first, second = Registry(a.lib, a.state.parent / "daemon"), Registry(b.lib, b.state.parent / "daemon")
    data = first.save(definition())
    first.enable("example", ["codex"], data["revision"])
    assert a.run()["status"] == "synced"
    assert b.run()["status"] == "synced"
    assert second.resolved("codex") == []
    [host] = second.machines()
    assert host["current"] is False and host["servers"]["example"]["apps"] == ["codex"]
    second.enable("example", ["cursor"], data["revision"])
    b.run()
    a.run()
    assert first.resolved("codex") == [data]
    assert first.resolved("cursor") == []
    assert second.resolved("cursor") == [data]
    assert len(first.machines()) == len(second.machines()) == 2
    published = remote_files(a.remote)
    assert len([p for p in published if p.startswith("mcp/machines/")]) == 2
    assert "mcp/registry.json" not in published
    assert "mcp/machine.json" not in published


def test_synced_definition_edit_needs_new_local_confirmation(pair):
    a, b = pair
    first, second = Registry(a.lib, a.state.parent / "daemon"), Registry(b.lib, b.state.parent / "daemon")
    data = first.save(definition())
    first.enable("example", ["codex"], data["revision"])
    a.run()
    b.run()
    second.enable("example", ["codex"], data["revision"])
    draft = definition()
    draft["transport"]["url"] = "https://changed.test/mcp"
    first.save(draft)
    a.run()
    b.run()
    assert second.list()[0]["status"] == "unconfirmed"
    assert second.resolved("codex") == []


def test_mcp_scope_can_keep_all_records_local(tmp_path):
    registry = Registry(tmp_path / "library", tmp_path / "state")
    registry.save(definition())
    snapshot = scope.snapshot(registry.library_root, scope.Scopes(exclude=frozenset({"mcp"})))
    assert not any(path.startswith("mcp/") for path in snapshot.files)
    assert any(path.startswith("mcp/") for path in snapshot.outside)


@pytest.mark.parametrize("target", ["server", "machine"])
def test_hand_edited_credentials_are_blocked_before_upload(pair, target):
    a, _ = pair
    registry = Registry(a.lib, a.state.parent / "daemon")
    registry.save(definition())
    if target == "server":
        path = a.lib / "mcp/servers/example.json"
        data = json.loads(path.read_text())
        data["transport"]["headers"]["Authorization"] = "Bearer unknown-credential-value"
    else:
        [host] = registry.machines()
        path = a.lib / f"mcp/machines/{host['id']}.json"
        data = json.loads(path.read_text())
        data["token"] = "unknown-credential-value"
    path.write_text(json.dumps(data))
    preview = scope.snapshot(a.lib, scope.Scopes())
    assert preview.blocked[path.relative_to(a.lib).as_posix()]["pattern"] == "invalid_mcp_record"
    approved = scope.Scopes(allow=frozenset({scope.content_hash(path.read_bytes())}))
    assert path.relative_to(a.lib).as_posix() in scope.snapshot(a.lib, approved).blocked
    a.run()
    assert b"unknown-credential-value" not in remote_objects(a.remote)


def test_duplicate_fields_cannot_hide_credentials_from_scanner():
    data = b'{"headers":{"Authorization":"Bearer unknown"},"headers":{}}'
    assert scope.scan(data, path="mcp/servers/example.json") == "invalid_mcp_record"


def test_concurrent_definition_edits_keep_local_conflict_copy(pair):
    a, b = pair
    first, second = Registry(a.lib, a.state.parent / "daemon"), Registry(b.lib, b.state.parent / "daemon")
    first.save(definition())
    a.run()
    b.run()
    local = definition()
    local["description"] = "Second host edit"
    second.save(local)
    incoming = definition()
    incoming["description"] = "First host edit"
    first.save(incoming)
    a.run()
    b.run()
    [conflict] = [item for item in records(b) if item["path"] == "mcp/servers/example.json"]
    assert conflict["kept"] == "remote"
    assert json.loads(conflict["local_content"])["description"] == "Second host edit"
    assert second.get("example")["description"] == "First host edit"
