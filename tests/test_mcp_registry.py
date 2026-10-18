"""HTTP registry validation, library writes and machine-local consent."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from src import user_library
from src.mcp_registry import Registry, RegistryError, validate_definition


def definition(alias="example"):
    return {"alias": alias, "title": "Example", "transport": {"type": "http", "url": "https://example.test/mcp"},
            "auth": {"type": "bearer"}, "secrets": [{"name": "Authorization", "target": "header", "required": True}]}


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "library", tmp_path / "state")


def write_external(registry, draft):
    data = validate_definition(draft)
    path = registry.library_root / "mcp" / "servers" / f"{data['alias']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def test_round_trip_defaults_revision_and_notifications(registry):
    notifications = []
    stop = user_library.subscribe(lambda root, paths: notifications.append((root, paths)))
    try:
        draft = definition()
        draft["extension"] = {"future": True}
        saved = registry.save(draft)
        assert saved["format"] == 1
        assert len(saved["revision"]) == 64
        assert saved["transport"]["headers"] == {}
        assert saved["limits"] == {"call_timeout_seconds": 60, "idle_seconds": 600}
        assert registry.get("example") == saved
        assert saved["extension"] == {"future": True}
        assert registry.list()[0]["confirmed"] is True
        assert registry.list()[0]["status"] == "disabled"
        assert registry.resolved("codex") == []
        assert "mcp/servers/example.json" in notifications[0][1]
        assert notifications[0][0] == registry.library_root
        assert registry.save(saved) == saved
        assert len(notifications) == 1
        assert not list(registry.library_root.rglob(".tmp-*"))
    finally:
        stop()


def test_revision_is_canonical_and_tracks_extension_changes():
    draft = definition()
    saved = validate_definition(draft)
    assert validate_definition(dict(reversed(list(saved.items())))) == saved
    draft["description"] = "Changed"
    assert validate_definition(draft)["revision"] != saved["revision"]


@pytest.mark.parametrize("alias", ["../escape", "a/b", "Upper", "a__b", "", "toolongalias12", "a.b", None])
def test_alias_validation_precedes_writes(registry, alias):
    with pytest.raises(RegistryError):
        registry.save(definition(alias))
    assert not registry.library_root.exists()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:pass@example.test", "https://example.test/?api_key=abc", "https://example.test/?access_token=abc", "https://example.test/#secret", "https://example.test:bad/mcp", "https://exa mple.test/mcp", "https://example.test/\n", "http://example.test:0"])
def test_unsafe_urls_are_rejected(registry, url):
    draft = definition()
    draft["transport"]["url"] = url
    with pytest.raises(RegistryError):
        registry.save(draft)


@pytest.mark.parametrize("mutate", [
    lambda d: d["transport"].update(type="stdio", command="sh"),
    lambda d: d["transport"].update(headers={"Authorization": "Bearer abc"}),
    lambda d: d["transport"].update(headers={"X-API-Key": "abc"}),
    lambda d: d["transport"].update(headers={"Host": "elsewhere.test"}),
    lambda d: d["transport"].update(headers={"X-Agents-Client": "codex"}),
    lambda d: d["transport"].update(headers={"X-Test": "abc\r\nInjected: yes"}),
    lambda d: d["transport"].update(headers={"Accept": "a", "accept": "b"}),
    lambda d: d.update(extension={"token": "abc"}),
    lambda d: d.update(instructions="ghp_" + "A" * 36),
    lambda d: d["secrets"][0].update(value="inline"),
    lambda d: d["secrets"][0].update(target="env"),
    lambda d: d["secrets"][0].update(required="yes"),
    lambda d: d.update(secrets=[]),
    lambda d: d.update(auth={"type": "oauth"}),
    lambda d: d.update(limits={"idle_seconds": True}),
    lambda d: d.update(tools={"expose": "one"}),
    lambda d: d.update(format=2),
])
def test_invalid_definitions_leave_no_library(registry, mutate):
    draft = definition()
    mutate(draft)
    with pytest.raises(RegistryError):
        registry.save(draft)
    assert not registry.library_root.exists()


def test_exact_revision_and_per_app_machine_local_scopes(registry):
    data = write_external(registry, definition())
    assert registry.list()[0]["confirmed"] is False
    assert registry.resolved("codex") == []
    with pytest.raises(RegistryError) as error:
        registry.enable("example", ["codex"], "0" * 64)
    assert error.value.code == "stale_revision"
    registry.enable("example", ["codex", "cursor"], data["revision"])
    assert registry.resolved("codex") == [data]
    assert registry.resolved("cursor") == [data]
    assert registry.resolved("claude-code") == []
    assert registry.resolved("unknown") == []
    assert Registry(registry.library_root, registry.state_dir.parent / "other-state").resolved("codex") == []
    draft = definition()
    draft["transport"]["url"] = "https://changed.test/mcp"
    changed = write_external(registry, draft)
    assert registry.list()[0]["status"] == "unconfirmed"
    assert registry.resolved("codex") == []
    with pytest.raises(RegistryError):
        registry.enable("example", ["codex"], data["revision"])
    registry.enable("example", ["codex"], changed["revision"])
    assert registry.resolved("codex") == [changed]
    registry.enable("example", [], changed["revision"])
    assert registry.resolved("codex") == []
    assert not (registry.library_root / "mcp" / "registry.json").exists()


def test_local_edit_confirms_new_revision_without_expanding_apps(registry):
    first = registry.save(definition())
    registry.enable("example", ["codex"], first["revision"])
    first["description"] = "Local review"
    updated = registry.save(first)
    assert registry.resolved("codex") == [updated]
    assert registry.resolved("cursor") == []


def test_malformed_record_is_isolated_and_does_not_echo_contents(registry):
    data = registry.save(definition())
    registry.enable("example", ["codex"], data["revision"])
    directory = registry.library_root / "mcp" / "servers"
    (directory / "broken.json").write_text('{"token":"do-not-echo"', encoding="utf-8")
    wrong_alias = {**data, "alias": "wrong"}
    (directory / "mismatch.json").write_text(json.dumps(wrong_alias), encoding="utf-8")
    records = registry.list()
    assert len([r for r in records if r["status"] == "invalid"]) == 2
    assert "do-not-echo" not in json.dumps(records)
    assert registry.resolved("codex") == [data]


def test_stored_revision_must_match_and_duplicate_json_is_rejected(registry):
    data = registry.save(definition())
    path = registry.library_root / "mcp" / "servers" / "example.json"
    path.write_text(json.dumps({**data, "revision": "0" * 64}), encoding="utf-8")
    assert registry.list()[0]["status"] == "invalid"
    path.write_text('{"alias":"example","alias":"other"}', encoding="utf-8")
    with pytest.raises(RegistryError):
        registry.get("example")


def test_symlink_paths_are_refused_without_touching_target(registry, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    registry.library_root.mkdir()
    (registry.library_root / "mcp").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RegistryError):
        registry.save(definition())
    assert list(outside.iterdir()) == []


def test_corrupt_machine_state_fails_closed(registry):
    data = registry.save(definition())
    registry.enable("example", ["codex"], data["revision"])
    (registry.state_dir / "mcp" / "registry.json").write_text("[]", encoding="utf-8")
    with pytest.raises(RegistryError):
        registry.resolved("codex")


def test_concurrent_enables_preserve_other_entries(registry):
    definitions = [registry.save(definition(f"server{i}")) for i in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda data: registry.enable(data["alias"], ["codex"], data["revision"]), definitions))
    assert len(registry.resolved("codex")) == 6


def test_resolved_returns_checked_snapshot_without_a_second_read(registry, monkeypatch):
    saved = registry.save(definition())
    registry.enable("example", ["codex"], saved["revision"])
    original = registry.get
    calls = []

    def read_then_change(alias):
        result = original(alias)
        calls.append(alias)
        draft = definition()
        draft["transport"]["url"] = "https://unconfirmed.test/mcp"
        write_external(registry, draft)
        return result

    monkeypatch.setattr(registry, "get", read_then_change)
    assert registry.resolved("codex") == [saved]
    assert calls == ["example"]


def test_oversized_and_nonordinary_records_are_isolated(registry):
    registry.save(definition())
    directory = registry.library_root / "mcp" / "servers"
    (directory / "large.json").write_bytes(b"x" * (64 * 1024 + 1))
    (directory / "folder.json").mkdir()
    assert len([record for record in registry.list() if record["status"] == "invalid"]) == 2
