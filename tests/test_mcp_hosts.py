"""Public host metadata never grants machine-local MCP execution authority."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import platform
import re
import socket
import stat

import pytest

from src import mcp_registry, user_library
from src.mcp_registry import Registry, RegistryError, validate_definition, validate_machine


def definition(alias="example"):
    return {"alias": alias, "transport": {"type": "http", "url": "https://example.test/mcp"}}


def identity(registry):
    return json.loads((registry.state_dir / "mcp/machine.json").read_text())


def machine_path(registry):
    return registry.library_root / "mcp/machines" / (identity(registry)["id"] + ".json")


def local_state(registry, servers):
    path = registry.state_dir / "mcp/registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"format": 1, "servers": servers, "private_path": str(registry.state_dir)}))


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "library", tmp_path / "private-state")


def test_machine_listing_does_not_create_state_or_library(registry):
    assert registry.machines() == []
    assert not registry.state_dir.exists()
    assert not registry.library_root.exists()


def test_existing_state_migration_publishes_only_allowed_metadata(registry, monkeypatch):
    saved = validate_definition(definition())
    state = {"example": {"enabled": True, "apps": ["cursor", "codex", "codex"],
                         "revision": saved["revision"], "token": "private-value",
                         "path": str(registry.state_dir), "hostname": "private-hostname"}}
    local_state(registry, state)
    monkeypatch.setattr(socket, "gethostname", lambda: pytest.fail("must not read hostname"))
    monkeypatch.setattr(platform, "node", lambda: pytest.fail("must not read hostname"))
    monkeypatch.setattr(mcp_registry.sys, "platform", "darwin")
    published = registry.publish_machine()
    private = identity(registry)
    assert re.fullmatch(r"[0-9a-f]{32}", private["id"])
    assert re.fullmatch(r"mac-[0-9a-f]{4}", private["label"])
    if os.name != "nt":
        assert stat.S_IMODE((registry.state_dir / "mcp/machine.json").stat().st_mode) == 0o600
    assert published == {**private, "servers": {"example": {
        "enabled": True, "apps": ["codex", "cursor"], "revision": saved["revision"]}}}
    assert json.loads(machine_path(registry).read_text()) == published
    public = json.dumps(registry.machines())
    assert not any(value in public for value in ("private-value", "private-hostname", str(registry.state_dir)))
    assert registry.machines() == [{**published, "current": True}]
    # Migration describes existing state; it neither creates definitions nor
    # substitutes metadata for the exact locally reviewed definition.
    assert registry.resolved("codex") == []
    assert json.loads((registry.state_dir / "mcp/registry.json").read_text())["servers"] == state
    reloaded = Registry(registry.library_root, registry.state_dir)
    assert reloaded.publish_machine() == published
    assert identity(reloaded) == private


def test_safe_user_sync_label_is_reused_once(registry):
    settings = registry.state_dir / "user-sync/user-sync.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"label": "travel-laptop", "remote": "private-remote", "token": "private-token"}))
    published = registry.publish_machine()
    assert published["label"] == "travel-laptop"
    settings.write_text(json.dumps({"label": "later-label"}))
    assert registry.publish_machine() == published
    assert "private-remote" not in machine_path(registry).read_text()
    assert "private-token" not in machine_path(registry).read_text()


@pytest.mark.parametrize("label", ["local", "../escape", "Upper", "", "a" * 33, "sk-" + "a" * 25, None, []])
def test_invalid_user_sync_labels_use_neutral_default(registry, label):
    settings = registry.state_dir / "user-sync/user-sync.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"label": label}))
    assert re.fullmatch(r"(?:mac|windows|linux)-[0-9a-f]{4}", registry.publish_machine()["label"])


def test_symlink_user_sync_settings_are_not_followed(registry, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"label":"sensitive-label"}')
    settings = registry.state_dir / "user-sync/user-sync.json"
    settings.parent.mkdir(parents=True)
    settings.symlink_to(outside)
    assert registry.publish_machine()["label"] != "sensitive-label"


def test_two_hosts_share_metadata_without_sharing_consent(registry, tmp_path):
    other = Registry(registry.library_root, tmp_path / "other-state")
    saved = registry.save(definition())
    registry.enable("example", ["codex"], saved["revision"])
    before = machine_path(registry).read_bytes()
    assert other.resolved("codex") == []
    assert other.machines()[0]["current"] is False
    assert not other.state_dir.exists()
    other.save(saved)
    other.enable("example", ["cursor"], saved["revision"])
    assert machine_path(registry).read_bytes() == before
    assert identity(registry)["id"] != identity(other)["id"]
    assert registry.resolved("codex") == [saved]
    assert registry.resolved("cursor") == []
    assert other.resolved("codex") == []
    assert other.resolved("cursor") == [saved]
    assert len(registry.machines()) == 2
    assert [record["id"] for record in other.machines() if record["current"]] == [identity(other)["id"]]
    other_bytes = machine_path(other).read_bytes()
    registry.enable("example", [], saved["revision"])
    assert machine_path(other).read_bytes() == other_bytes
    local_public = next(record for record in registry.machines() if record["current"])
    assert local_public["servers"]["example"] == {"enabled": False, "apps": [], "revision": saved["revision"]}
    assert registry.resolved("codex") == []


def test_synced_current_host_record_cannot_change_local_authority(registry):
    saved = registry.save(definition())
    public = json.loads(machine_path(registry).read_text())
    public["servers"]["example"].update(enabled=True, apps=["codex"])
    machine_path(registry).write_text(json.dumps(public))
    assert registry.machines()[0]["servers"]["example"]["enabled"] is True
    assert registry.list()[0]["enabled"] is False
    assert registry.resolved("codex") == []
    registry.publish_machine()
    assert registry.machines()[0]["servers"]["example"]["enabled"] is False
    assert registry.get("example") == saved


def test_publication_preserves_stale_consent_and_local_save_updates_snapshot(registry):
    first = registry.save(definition())
    registry.enable("example", ["codex"], first["revision"])
    changed = validate_definition({**definition(), "description": "Synced change"})
    path = registry.library_root / "mcp/servers/example.json"
    path.write_text(json.dumps(changed))
    published = registry.publish_machine()
    assert published["servers"]["example"]["revision"] == first["revision"]
    assert registry.resolved("codex") == []
    registry.save(changed)
    assert registry.publish_machine()["servers"]["example"] == {
        "enabled": True, "apps": ["codex"], "revision": changed["revision"]}
    assert registry.resolved("codex") == [changed]
    assert registry.resolved("cursor") == []


def test_publication_is_idempotent_and_notifies_only_changed_paths(registry):
    notifications = []
    stop = user_library.subscribe(lambda root, paths: notifications.append((root, paths)))
    try:
        saved = registry.save(definition())
        relative = machine_path(registry).relative_to(registry.library_root).as_posix()
        assert len(notifications) == 1
        assert {"mcp/servers/example.json", relative} <= set(notifications[0][1])
        original_mtime = machine_path(registry).stat().st_mtime_ns
        registry.save(saved)
        registry.publish_machine()
        assert len(notifications) == 1
        assert machine_path(registry).stat().st_mtime_ns == original_mtime
        registry.enable("example", ["codex"], saved["revision"])
        assert notifications[-1] == (registry.library_root, (relative,))
        assert len(notifications) == 2
        registry.enable("example", ["codex"], saved["revision"])
        registry.publish_machine()
        assert len(notifications) == 2
        registry.enable("example", [], saved["revision"])
        assert notifications[-1] == (registry.library_root, (relative,))
        assert len(notifications) == 3
    finally:
        stop()


def test_concurrent_updates_keep_both_hosts_and_all_local_servers(registry, tmp_path):
    other = Registry(registry.library_root, tmp_path / "other-state")
    definitions = [registry.save(definition(f"server{i}")) for i in range(6)]

    def enable(args):
        host, data = args
        Registry(host.library_root, host.state_dir).enable(data["alias"], ["codex"], data["revision"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(enable, [(host, data) for host in (registry, other) for data in definitions]))
        list(pool.map(lambda host: host.publish_machine(), [registry, other] * 4))
    assert len(registry.resolved("codex")) == len(other.resolved("codex")) == 6
    assert len(registry.machines()) == 2
    assert all(len(host["servers"]) == 6 and all(server["enabled"] for server in host["servers"].values())
               for host in registry.machines())


def test_foreign_records_are_isolated_and_arbitrary_fields_never_escape(registry, tmp_path):
    registry.save(definition())
    directory = registry.library_root / "mcp/machines"
    public = {"format": 1, "id": "a" * 32, "label": "other-host", "password": "do-not-echo",
              "servers": {"safe": {"enabled": True, "apps": ["codex"], "revision": "b" * 64,
                                   "Authorization": "do-not-echo"},
                          "invalid": {"enabled": True, "apps": ["unknown"], "revision": "b" * 64},
                          "../escape": {"enabled": True, "apps": ["codex"], "revision": "b" * 64}}}
    (directory / ("a" * 32 + ".json")).write_text(json.dumps(public))
    (directory / ("b" * 32 + ".json")).write_text('{"token":"do-not-echo"')
    (directory / ("c" * 32 + ".json")).write_text(json.dumps({**public, "id": "d" * 32}))
    (directory / ("d" * 32 + ".json")).write_bytes(b"x" * (mcp_registry.MAX_BYTES + 1))
    (directory / ("e" * 32 + ".json")).mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({**public, "id": "f" * 32}))
    (directory / ("f" * 32 + ".json")).symlink_to(outside)
    (directory / "hostname-is-not-an-id.json").write_text(json.dumps(public))
    records = registry.machines()
    assert len(records) == 2
    foreign = next(record for record in records if not record["current"])
    assert foreign == {"format": 1, "id": "a" * 32, "label": "other-host", "current": False,
                       "servers": {"safe": {"enabled": True, "apps": ["codex"], "revision": "b" * 64}}}
    assert "do-not-echo" not in json.dumps(records)
    assert registry.resolved("codex") == []


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(password="secret"),
    lambda d: d["servers"]["example"].update(Authorization="Bearer secret"),
    lambda d: d["servers"]["example"].update(enabled=1),
    lambda d: d["servers"]["example"].update(apps=[{}]),
    lambda d: d.update(format=True),
    lambda d: d.update(id="b" * 32),
    lambda d: d.update(label="../escape"),
])
def test_strict_sync_validator_rejects_extras_and_invalid_values(mutate):
    data = {"format": 1, "id": "a" * 32, "label": "test-host",
            "servers": {"example": {"enabled": True, "apps": ["codex"], "revision": "c" * 64}}}
    assert validate_machine(data, "a" * 32) == data
    mutate(data)
    with pytest.raises(RegistryError):
        validate_machine(data, "a" * 32)


def test_symlinked_current_metadata_or_identity_never_modifies_target(registry, tmp_path):
    registry.save(definition())
    outside = tmp_path / "outside.json"
    outside.write_text("untouched")
    path = machine_path(registry)
    path.unlink()
    path.symlink_to(outside)
    assert registry.machines() == []
    with pytest.raises(RegistryError):
        registry.publish_machine()
    assert outside.read_text() == "untouched"
    path.unlink()
    private = registry.state_dir / "mcp/machine.json"
    private.unlink()
    private.symlink_to(outside)
    with pytest.raises(RegistryError):
        registry.publish_machine()
    assert outside.read_text() == "untouched"
