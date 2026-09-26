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
    "serve-the-request",
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
    """The v1 set is the contract — adding/removing rules is a deliberate change.

    Rules are always-on for every agent, so any addition or removal alters the
    behavior of the whole system. The strict-equality check forces a paired
    update of ``_EXPECTED_RULE_NAMES`` whenever ``rules/`` changes — that
    paper trail is the point.
    """
    files = _list_rule_files()
    names = {f.stem.removeprefix("rule-") for f in files}
    missing = _EXPECTED_RULE_NAMES - names
    unexpected = names - _EXPECTED_RULE_NAMES
    assert names == _EXPECTED_RULE_NAMES, (
        f"Rule set changed. "
        f"Missing v1 rules: {sorted(missing)}. "
        f"Unexpected rule files: {sorted(unexpected)}. "
        f"If this addition/removal is intentional, update _EXPECTED_RULE_NAMES "
        f"in this test file as part of the same commit."
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
    promote it to a skill in the agent's ``core_skills``/``preferred_skills``.
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


def test_format_rules_strips_leading_h1_only_with_horizontal_space():
    """Regression guard for the H1-stripping regex.

    `_LEADING_H1_RE` used to match `^#\\s+...` where `\\s+` greedily included
    newlines. A rule body starting with a bare `#` line followed by real
    content would have had that next content line silently swallowed when
    rendered. The regex now requires `[ \\t]+` (horizontal whitespace only),
    so a bare `#\\n` is left alone — the only thing dropped is a real
    Markdown H1 like `# Title`.
    """
    rule_real_h1 = rules_module.Rule(
        name="r1", description="d", priority=10, category="x",
        body="# Real H1 heading\n\nbody after",
        filename="rule-r1.mdc",
    )
    rule_bare_hash = rules_module.Rule(
        name="r2", description="d", priority=20, category="x",
        body="#\nFirst content line must survive\nrest",
        filename="rule-r2.mdc",
    )

    rendered = rules_module.format_rules_for_prompt([rule_real_h1, rule_bare_hash])

    # Real H1 is stripped — body underneath remains.
    assert "Real H1 heading" not in rendered
    assert "body after" in rendered

    # Bare `#` + newline is NOT a valid H1; the first content line must survive.
    assert "First content line must survive" in rendered


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


def test_enrichment_degrades_gracefully_when_rules_layer_raises(monkeypatch, caplog):
    """A failure inside the rules layer must not break routing.

    Regression guard for the cursor finding: ``get_dynamic_context_string``
    used to call ``get_rules()`` outside any try/except, so an unexpected
    error (FS hiccup after ``invalidate_cache()``, formatter bug, etc.)
    would propagate and fail the whole request — even though the rules
    layer is the ironically-named "always-on" guardrail. Skills/implants
    already degrade to "fewer layers" on error; rules now do too.
    """
    import asyncio

    from src.engine import enrichment as enrichment_module

    def _boom():
        raise RuntimeError("simulated rules-layer failure")

    monkeypatch.setattr(enrichment_module, "get_rules", _boom)

    async def _run():
        return await enrichment_module.get_dynamic_context_string(
            agent_name="universal_agent",
            query="hello",
            tier="lite",
        )

    with caplog.at_level("ERROR"):
        result = asyncio.run(_run())

    assert result.rules_loaded == [], "Rules failure must not populate rules_loaded"
    assert any("Failed to load rules layer" in rec.message for rec in caplog.records)
    # Routing continues — caller still gets a (possibly empty) prompt back.
    assert isinstance(result.prompt, str)


def test_loader_skips_rule_with_non_integer_priority(tmp_path, monkeypatch, caplog):
    """A malformed rule (e.g. ``priority: high``) must not crash the pipeline.

    Regression guard: previously ``int(fm.get("priority"))`` raised ``ValueError``
    that propagated through ``get_rules`` → ``get_dynamic_context_string`` and
    failed every request. Now the bad file is skipped with an error log; well-
    formed rules in the same directory continue to load.
    """
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "rule-bad-priority.mdc").write_text(
        textwrap.dedent(
            """\
            ---
            name: bad-priority
            description: Should be skipped, not crash.
            priority: high
            category: accuracy
            ---
            # Bad priority
            Non-numeric priority used to take down the whole pipeline.
            """
        ),
        encoding="utf-8",
    )
    (rules_dir / "rule-good.mdc").write_text(
        textwrap.dedent(
            """\
            ---
            name: good
            description: Survives the malformed sibling.
            priority: 5
            category: accuracy
            ---
            Body.
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(rules_module, "RULES_DIR", str(rules_dir))
    rules_module.invalidate_cache()

    with caplog.at_level("ERROR"):
        loaded = rules_module.load_all_rules()

    assert [r.name for r in loaded] == ["good"], "Malformed rule must be skipped, not crash"
    assert any("non-integer 'priority'" in rec.message for rec in caplog.records)


# --- Content contract: phrases the rest of the system depends on -----------------

_NO_FABRICATION_LOAD_BEARING_PHRASES = (
    # scope qualifier: without it the rule applies to every incidental number/path
    "load-bearing",
    # the settled-knowledge carve-out must keep its object
    "in-scope specific",
    # the confirmation requirement and its non-negotiable clarification
    "confirmed this turn",
    "memory is not confirmation",
    # the decision to search must live here, where it fires at the lite tier
    "search",
    "fetch the web",
    # the inline marker that evals/scripts/compare_rules.py's checks look for
    "recalled, not verified",
    # the best-effort carve-out
    "never omit or refuse",
    # generating vs asserting
    "generating ≠ asserting",
)


def test_no_fabrication_keeps_its_load_bearing_phrases():
    """A compression must not quietly weaken the quality-bearing rule.

    The first compression pass lost "search / fetch the web" and was caught by a
    single-phrase test; this widens that net to every phrase another part of the
    system (tests, the A/B harness, the lite-tier behaviour) relies on. Wording
    around them is free to change; the phrases themselves are not.
    """
    path = Path(RULES_DIR) / "rule-no-fabrication.mdc"
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    body = body.lower()
    missing = [p for p in _NO_FABRICATION_LOAD_BEARING_PHRASES if p not in body]
    assert not missing, f"no-fabrication lost load-bearing phrase(s): {missing}"


def test_compressed_fixture_matches_live_no_fabrication_rule():
    """The A/B candidate fixture must be the rule that actually ships."""
    live = Path(RULES_DIR) / "rule-no-fabrication.mdc"
    fixture = Path(RULES_DIR).parent / "evals" / "fixtures" / "rule-no-fabrication.compressed.mdc"
    if not fixture.exists():
        pytest.skip("compressed fixture not present in this checkout")
    assert fixture.read_bytes() == live.read_bytes()
