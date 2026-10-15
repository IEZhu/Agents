"""Cached URL rules participate in activation without changing evaluation inputs."""
from dataclasses import replace

import pytest

from src import component_catalog, component_toggles, flow_persona, user_rules
from src.engine import rules
from src.engine.persona_bundle import ComponentSelection, build_persona_bundle
from src.flows import FlowError
from tests.test_persona_bundle import bundle_tree  # noqa: F401
from tests.test_subagent import worker_tree, prepared  # noqa: F401
from tests.test_component_toggles import editor  # noqa: F401
from tests.test_user_flows import UI, install, login, repo  # noqa: F401


class CachedLibrary:
    """The local-library boundary: no network fixture is involved in bundle reads."""

    def __init__(self):
        self.rule = rules.Rule("user:team", "Team conventions", 5, "engineering", "Use team conventions")
        self.enabled = True
        self.revision = "a" * 64
        self.toggle_calls = []

    def list(self):
        return [{"id": self.rule.name, "description": self.rule.description,
                 "priority": self.rule.priority, "category": self.rule.category,
                 "body": self.rule.body, "enabled": self.enabled,
                 "url": "https://rules.example/team.mdc", "revision": self.revision,
                 "content_sha256": "b" * 64}]

    def rules(self, names=None):
        if names is not None and (names - {self.rule.name} or not self.enabled):
            raise ValueError("Unknown or disabled user rules")
        return [self.rule] if self.enabled else []

    def get(self, rule_id):
        assert rule_id == self.rule.name
        return self.list()[0]

    def set_enabled(self, rule_id, enabled, expected_revision):
        if expected_revision != self.revision:
            raise user_rules.UserRuleError("rule_conflict: stale revision")
        self.toggle_calls.append((rule_id, enabled, expected_revision))
        self.enabled = enabled
        return self.get(rule_id)


@pytest.fixture
def cached_library(monkeypatch, tmp_path):
    library = CachedLibrary()
    monkeypatch.setattr(user_rules, "RuleLibrary", lambda: library)
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "flows"))
    return library


@pytest.mark.asyncio
async def test_default_bundle_uses_cached_rule_and_revision_tracks_content(bundle_tree, cached_library):
    first = await build_persona_bundle("engineer", "Task", tier="deep")
    assert first.rules_loaded == ["truth", "user:team"]
    assert "Use team conventions" in first.rules_block
    cached_library.rule = replace(cached_library.rule, body="Changed team conventions")
    updated = await build_persona_bundle("engineer", "Task", tier="deep")
    assert first.bundle_revision != updated.bundle_revision
    assert "Changed team conventions" in updated.rules_block
    cached_library.enabled = False
    disabled = await build_persona_bundle("engineer", "Task", tier="deep")
    assert disabled.rules_loaded == ["truth"]
    assert disabled.bundle_revision != updated.bundle_revision


def test_default_evaluation_rules_ignore_subscriptions_and_toggles(bundle_tree, cached_library, monkeypatch):
    monkeypatch.setattr(rules, "_cache", None)
    component_toggles.set_enabled("rules", "truth", False)
    assert [rule.name for rule in rules.get_rules()] == ["truth"]
    assert [rule.name for rule in rules.get_rules(fresh=True, strict=True)] == ["truth"]
    assert [rule.name for rule in rules.get_rules(apply_toggles=True)] == ["user:team"]
    # Adding a user rule never mutates the memoized evaluation list.
    assert [rule.name for rule in rules.get_rules()] == ["truth"]


@pytest.mark.asyncio
async def test_exact_selection_empty_and_named_subscription(bundle_tree, cached_library):
    empty = await build_persona_bundle("engineer", "Task", tier="deep",
                                       selection=ComponentSelection(rules=()))
    assert empty.rules_loaded == [] and empty.rules_block == ""
    selected = await build_persona_bundle("engineer", "Task", tier="deep",
                                          selection=ComponentSelection(rules=("user:team",)))
    assert selected.rules_loaded == ["user:team"]
    component_toggles.set_enabled("rules", "truth", False)
    assert [rule.name for rule in rules.load_selected_rules(["truth", "user:team"])] == ["truth", "user:team"]
    cached_library.enabled = False
    with pytest.raises(ValueError, match="disabled"):
        rules.load_selected_rules(["user:team"])
    with pytest.raises(ValueError, match="Unknown"):
        rules.load_selected_rules(["user:missing"])


def test_global_rules_off_validates_named_user_rule(bundle_tree, cached_library, monkeypatch):
    monkeypatch.setattr(rules, "RULES_ENABLED", False)
    assert rules.get_rules(apply_toggles=True) == []
    assert rules.load_selected_rules(["user:team"]) == []
    cached_library.enabled = False
    with pytest.raises(ValueError, match="disabled"):
        rules.load_selected_rules(["user:team"])


def test_only_rule_selections_accept_user_ids():
    assert flow_persona.normalize({"agent": "engineer", "rules": ["user:team", "truth"]}) == {
        "agent": "engineer", "rules": ["user:team", "truth"]}
    for kind in ("skills", "implants"):
        with pytest.raises(FlowError, match="component IDs"):
            flow_persona.normalize({"agent": "engineer", kind: ["user:team"]})
    for rule_id in ("repo:team", "user:../team", "user:", "user:user:team"):
        with pytest.raises(FlowError, match="component IDs"):
            flow_persona.normalize({"agent": "engineer", "rules": [rule_id]})


def test_catalog_retains_disabled_user_rules_for_reenable(bundle_tree, cached_library):
    cached_library.enabled = False
    listing = {item["id"]: item for item in component_catalog.list_components("rules")}
    assert set(listing) == {"truth", "user:team"}
    assert listing["user:team"]["body"] == cached_library.rule.body
    assert listing["user:team"]["enabled"] is False
    assert listing["user:team"]["revision"] == cached_library.revision
    assert listing["user:team"]["source"] == "user"
    assert "user:team" in component_catalog.known_ids("rules")


def test_compact_worker_uses_cached_rules_and_content_revision(worker_tree, cached_library, monkeypatch):
    first = prepared()
    assert "user:team" in first.descriptor.rules_loaded
    assert "Use team conventions" in first.worker_context
    cached_library.rule = replace(cached_library.rule, body="Changed team conventions")
    updated = prepared()
    assert first.descriptor.bundle_revision != updated.descriptor.bundle_revision
    cached_library.enabled = False
    assert "user:team" not in prepared().descriptor.rules_loaded
    cached_library.enabled = True
    monkeypatch.setattr(rules, "RULES_ENABLED", False)
    assert "user:team" not in prepared().descriptor.rules_loaded


@pytest.mark.asyncio
async def test_ui_toggles_user_library_instead_of_flow_settings(editor, cached_library):
    http, _ = editor
    await login(http)
    response = await http.put("/ui/api/component", json={"kind": "rules", "id": "user:team", "enabled": False}, headers=UI)
    assert response.status_code == 200, response.text
    assert cached_library.toggle_calls == [("user:team", False, "a" * 64)]
    assert component_toggles.disabled("rules") == frozenset()
    listing = (await http.get("/ui/api/components", params={"kind": "rules"})).json()
    assert next(item for item in listing["items"] if item["id"] == "user:team")["enabled"] is False


@pytest.mark.asyncio
async def test_ui_rejects_stale_user_rule_revision(editor, cached_library):
    http, _ = editor
    await login(http)
    response = await http.put("/ui/api/component", json={
        "kind": "rules", "id": "user:team", "enabled": False,
        "expected_revision": "stale",
    }, headers=UI)
    assert response.status_code == 409, response.text
    assert cached_library.enabled is True and cached_library.toggle_calls == []


@pytest.mark.asyncio
async def test_real_cached_subscription_loads_without_fetch(bundle_tree, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTS_USER_RULES_DIR", str(tmp_path / "subscriptions"))
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "flows"))
    source = "---\nname: team\npriority: 5\n---\nAccepted guidance\n"
    monkeypatch.setattr(user_rules, "fetch_rule", lambda *args, **kwargs: source)
    library = user_rules.RuleLibrary()
    subscribed = library.subscribe("https://rules.example/team.mdc")

    def no_fetch(*args, **kwargs):
        raise AssertionError("bundle and catalog reads must not fetch")

    monkeypatch.setattr(user_rules, "fetch_rule", no_fetch)
    first = await build_persona_bundle("engineer", "Task", tier="deep")
    assert first.rules_loaded == ["truth", "user:team"]
    assert "Accepted guidance" in first.rules_block
    listing = {item["id"]: item for item in component_catalog.list_components("rules")}
    assert listing["user:team"]["revision"] == subscribed["revision"]
    library.set_enabled("user:team", False, subscribed["revision"])
    disabled = await build_persona_bundle("engineer", "Task", tier="deep")
    assert disabled.rules_loaded == ["truth"]
    with pytest.raises(user_rules.UserRuleError, match="rule_disabled"):
        rules.load_selected_rules(["user:team"])
