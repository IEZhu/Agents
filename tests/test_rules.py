"""Tests for the universal rules layer.

The rules layer must remain agent-agnostic — every rule applies to every agent.
That invariant is the layer's only architectural reason to exist (otherwise it
collapses into the skills layer). These tests guard the invariant and the
loader contract used by ``src/engine/enrichment.py``.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml

from src.engine import rules as rules_module
from src.engine.config import RULES_DIR
from src.utils.prompt_loader import split_frontmatter


_FORBIDDEN_FIELDS = ("applies_to", "exclude_agents")
_EXPECTED_RULE_NAMES = {
    "no-fabrication",
    "honest-uncertainty",
    "anti-sycophancy",
    "language-match",
}


@pytest.fixture(autouse=True)
def _reset_rules_cache():
    """Each test gets a clean cache so fixtures from other tests don't leak."""
    rules_module.invalidate_cache()
    yield
    rules_module.invalidate_cache()


def _list_rule_files() -> list[Path]:
    return sorted(Path(RULES_DIR).glob("rule-*.mdc"))


def test_rules_directory_exists():
    assert os.path.isdir(RULES_DIR), f"rules/ should exist at {RULES_DIR}"


def test_v1_rule_files_present():
    """The v1 set is the contract — adding/removing rules is a deliberate change."""
    files = _list_rule_files()
    names = {f.stem.removeprefix("rule-") for f in files}
    assert _EXPECTED_RULE_NAMES <= names, (
        f"Missing v1 rules: {_EXPECTED_RULE_NAMES - names}"
    )


def test_all_rule_files_parse():
    rules = rules_module.load_all_rules()
    expected_count = len(_list_rule_files())
    assert len(rules) == expected_count, (
        "load_all_rules dropped some files — check logs for parsing errors"
    )


def test_invariant_no_per_agent_fields_in_any_rule_file():
    """Architectural invariant: rules are universal, no opt-in/opt-out fields.

    A rule that needs ``applies_to`` or ``exclude_agents`` is not a rule —
    promote it to a skill via ``agents/capabilities/registry.yaml``.
    """
    for path in _list_rule_files():
        content = path.read_text(encoding="utf-8")
        fm_str, _ = split_frontmatter(content)
        assert fm_str is not None, f"{path} has no frontmatter"
        fm = yaml.safe_load(fm_str) or {}
        present = [k for k in _FORBIDDEN_FIELDS if k in fm]
        assert not present, (
            f"{path.name} contains forbidden fields {present}. "
            "Per-agent guidance belongs in skills, not rules."
        )


def test_get_rules_sorted_by_priority():
    rules = rules_module.get_rules()
    priorities = [r.priority for r in rules]
    assert priorities == sorted(priorities), "Rules must be sorted by priority asc"


def test_get_rules_contains_v1_set():
    names = {r.name for r in rules_module.get_rules()}
    assert _EXPECTED_RULE_NAMES <= names


def test_get_rules_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(rules_module, "RULES_ENABLED", False)
    rules_module.invalidate_cache()
    assert rules_module.get_rules() == []


def test_get_rules_caches_between_calls():
    first = rules_module.get_rules()
    second = rules_module.get_rules()
    assert first is second, "get_rules should memoize until invalidate_cache()"


def test_invalidate_cache_forces_reload():
    first = rules_module.get_rules()
    rules_module.invalidate_cache()
    second = rules_module.get_rules()
    assert first is not second
    assert [r.name for r in first] == [r.name for r in second]


def test_format_rules_for_prompt_includes_each_name():
    rules = rules_module.get_rules()
    rendered = rules_module.format_rules_for_prompt(rules)
    assert rendered.startswith("## Rules"), "Block should be marked as ## Rules"
    for r in rules:
        assert r.name in rendered, f"Rule {r.name} missing from rendered block"


def test_format_rules_for_prompt_empty_list():
    assert rules_module.format_rules_for_prompt([]) == ""


def test_loader_rejects_synthetic_rule_with_forbidden_field(tmp_path, monkeypatch, caplog):
    """A rule file shipping ``exclude_agents`` must be rejected at load time."""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "rule-bad.mdc").write_text(
        textwrap.dedent(
            """\
            ---
            name: bad-rule
            description: Should be rejected.
            priority: 5
            category: accuracy
            exclude_agents: [literary_writer]
            ---
            # Bad rule
            Per-agent guidance — does not belong in rules/.
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(rules_module, "RULES_DIR", str(rules_dir))
    rules_module.invalidate_cache()

    with caplog.at_level("ERROR"):
        loaded = rules_module.load_all_rules()

    assert loaded == [], "Forbidden-field rule must not be loaded"
    assert any("forbidden fields" in rec.message for rec in caplog.records)


def test_loader_skips_files_without_frontmatter(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "rule-empty.mdc").write_text("just a body, no frontmatter\n", encoding="utf-8")

    monkeypatch.setattr(rules_module, "RULES_DIR", str(rules_dir))
    rules_module.invalidate_cache()

    assert rules_module.load_all_rules() == []


def test_loader_handles_missing_directory(tmp_path, monkeypatch):
    nonexistent = tmp_path / "no-rules-here"
    monkeypatch.setattr(rules_module, "RULES_DIR", str(nonexistent))
    rules_module.invalidate_cache()
    assert rules_module.load_all_rules() == []
