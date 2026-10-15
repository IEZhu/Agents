"""URL subscription acceptance, offline use and optimistic local revisions."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import user_rules
from src.user_rules import RuleLibrary, UserRuleError, parse_rule
from src.rule_sources import RuleSourceError

URL = "https://rules.example.org/team.mdc"
CONTENT = "---\nname: team\ndescription: Team rule\npriority: 40\ncategory: corporate\n---\nUse corporate evidence.\n"


@pytest.fixture
def library(monkeypatch):
    monkeypatch.setattr(user_rules, "fetch_rule", lambda *args, **kwargs: CONTENT)
    return RuleLibrary()


def test_subscribe_caches_content_and_reads_without_network(library, monkeypatch):
    created = library.subscribe(URL)
    assert created["id"] == "user:team" and created["enabled"]
    assert created["url"] == URL and created["content"] == CONTENT
    monkeypatch.setattr(user_rules, "fetch_rule", lambda *args, **kwargs: pytest.fail("offline read fetched URL"))
    assert library.get("user:team") == created
    assert library.list()[0]["body"] == "Use corporate evidence."
    assert [rule.name for rule in library.rules()] == ["user:team"]
    assert library.rules(set()) == []


@pytest.mark.parametrize("content", [
    "no frontmatter", "---\nname: team\n---\n", "---\nname: ../team\n---\nBody",
    "---\nname: team\npriority: true\n---\nBody", "---\nname: team\npriority: 1001\n---\nBody",
    "---\nname: team\ndescription: [bad]\n---\nBody", "---\nname: team\ncategory: 5\n---\nBody",
    "---\nname: team\nname: other\n---\nBody", "---\nname: team\napplies_to: all\n---\nBody",
    "---\nname: team\nexclude_agents: []\n---\nBody", "---\nname: team\nexec: command\n---\nBody",
    "---\nname: &n team\ncategory: *n\n---\nBody", CONTENT + "\n@/private/secret.mdc",
    CONTENT.replace("Team rule", "@rules/secret.mdc"), CONTENT + "\x00", CONTENT + "x" * 65536,
])
def test_invalid_documents_never_enter_store(library, monkeypatch, content):
    monkeypatch.setattr(user_rules, "fetch_rule", lambda *args, **kwargs: content)
    with pytest.raises(UserRuleError, match="rule_invalid"):
        library.subscribe(URL)
    assert library.list() == []


def test_initial_fetch_failure_does_not_subscribe(library, monkeypatch):
    def fail(*args, **kwargs):
        raise RuleSourceError("source_unavailable")
    monkeypatch.setattr(user_rules, "fetch_rule", fail)
    with pytest.raises(RuleSourceError):
        library.subscribe(URL)
    assert library.list() == []


@pytest.mark.parametrize("mode", ["network", "invalid", "rename"])
def test_failed_refresh_retains_accepted_rule_and_records_error(library, monkeypatch, mode):
    initial = library.subscribe(URL)
    def replacement(*args, **kwargs):
        if mode == "network":
            raise RuleSourceError("source_unavailable")
        return "bad content" if mode == "invalid" else CONTENT.replace("name: team", "name: renamed")
    monkeypatch.setattr(user_rules, "fetch_rule", replacement)
    result = library.refresh(initial["id"], initial["revision"])
    current = library.get(initial["id"])
    assert result["status"] == "error" and current["last_error"]
    assert current["content"] == initial["content"] and current["content_sha256"] == initial["content_sha256"]
    assert current["enabled"] and current["revision"] != initial["revision"]
    assert library.rules()[0].body == "Use corporate evidence."


def test_refresh_rollback_enable_and_delete_use_current_revision(library, monkeypatch):
    original = library.subscribe(URL)
    monkeypatch.setattr(user_rules, "fetch_rule", lambda *args, **kwargs: CONTENT + "New guidance.")
    changed = library.refresh(original["id"], original["revision"])["rule"]
    assert changed["content_sha256"] != original["content_sha256"] and len(changed["history"]) == 2
    with pytest.raises(UserRuleError, match="rule_conflict"):
        library.set_enabled(original["id"], False, original["revision"])
    disabled = library.set_enabled(original["id"], False, changed["revision"])
    assert library.rules() == []
    with pytest.raises(UserRuleError, match="rule_disabled"):
        library.rules({original["id"]})
    restored = library.rollback(original["id"], original["content_sha256"], disabled["revision"])
    assert restored["content"] == CONTENT and not restored["enabled"]
    enabled = library.set_enabled(original["id"], True, restored["revision"])
    library.delete(original["id"], enabled["revision"])
    assert library.list() == []


def test_duplicate_name_cannot_replace_source(library):
    original = library.subscribe(URL)
    with pytest.raises(UserRuleError, match="rule_exists"):
        library.subscribe("https://unrelated.example.org/team.mdc")
    assert library.get(original["id"]) == original


def test_stale_refresh_cannot_overwrite_concurrent_disable(library, monkeypatch):
    original = library.subscribe(URL)
    def fetch(*args, **kwargs):
        library.set_enabled(original["id"], False, original["revision"])
        return CONTENT + "Changed remotely."
    monkeypatch.setattr(user_rules, "fetch_rule", fetch)
    with pytest.raises(UserRuleError, match="rule_conflict"):
        library.refresh(original["id"], original["revision"])
    current = library.get(original["id"])
    assert not current["enabled"] and current["content"] == CONTENT


def test_concurrent_creation_accepts_one_subscription(library):
    def create():
        try:
            return library.subscribe(URL)["id"]
        except UserRuleError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: create(), range(2))) == ["conflict", "user:team"]


def test_history_is_bounded_and_identical_content_deduplicated(library, monkeypatch):
    current = library.subscribe(URL)
    repeated = library.refresh(current["id"], current["revision"])["rule"]
    assert len(repeated["history"]) == 1
    current = repeated
    for number in range(23):
        monkeypatch.setattr(user_rules, "fetch_rule", lambda *args, n=number, **kwargs: CONTENT + str(n))
        current = library.refresh(current["id"], current["revision"])["rule"]
    assert len(current["history"]) == user_rules.MAX_HISTORY
    assert library.rules()[0].body.endswith("22")


@pytest.mark.parametrize("rule_id", ["team", "user:../team", "user:/team", "user:team.json", "user:"])
def test_invalid_ids_cannot_escape_storage(library, rule_id):
    with pytest.raises(UserRuleError, match="rule_invalid"):
        library.get(rule_id)


def test_symlink_and_corrupt_snapshot_fail_explicitly(library, tmp_path):
    original = library.subscribe(URL)
    path = library.root / "team.json"
    state = json.loads(path.read_text())
    state["history"][0]["content"] += "tampered"
    path.write_text(json.dumps(state))
    with pytest.raises(UserRuleError, match="corrupt subscription"):
        library.rules()
    target = tmp_path / "outside.json"
    target.write_text("untouched")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(UserRuleError, match="symlink"):
        library.delete(original["id"], original["revision"])
    assert target.read_text() == "untouched"


def test_auth_reference_is_forwarded_without_copying_secret(library, monkeypatch):
    monkeypatch.setenv("TEAM_RULE_TOKEN", "private-value")
    seen = []
    def fetch(url, *, auth_env=None):
        seen.append((url, auth_env))
        return CONTENT
    monkeypatch.setattr(user_rules, "fetch_rule", fetch)
    current = library.subscribe(URL, auth_env="TEAM_RULE_TOKEN")
    assert seen == [(URL, "TEAM_RULE_TOKEN")]
    assert "private-value" not in (library.root / "team.json").read_text()
    assert current["auth_env"] == "TEAM_RULE_TOKEN"


@pytest.mark.parametrize("scalar", ["2026-99-99", "2026-01-01 99:99:99", "9" * 5000])
def test_malformed_yaml_scalar_refresh_records_safe_error(library, monkeypatch, scalar):
    initial = library.subscribe(URL)
    malformed = CONTENT.replace("description: Team rule", "description: " + scalar)
    monkeypatch.setattr(user_rules, "fetch_rule", lambda *args, **kwargs: malformed)
    result = library.refresh(initial["id"], initial["revision"])
    assert result["status"] == "error"
    current = result["retained"]
    assert current["last_error"] == "rule_invalid: malformed YAML metadata"
    assert current["content"] == initial["content"] and current["enabled"]
    assert current["revision"] != initial["revision"]
