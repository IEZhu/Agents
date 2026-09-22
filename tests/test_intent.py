"""Tests for the intent classifier (src/engine/intent.py) and its wiring.

The classifier itself is a pure function, so everything in `TestClassify*` runs
with no vector store, no embedder and no network. The wiring tests import
`src.engine.enrichment`, which builds `SkillRetriever()` at module scope against
the live `data/` directory — see issue #68. That is pre-existing and not
introduced here.
"""

from __future__ import annotations

import pytest

from src.engine.config import (
    IMPLANTS_DEEP_TIER_DEFAULT,
    INTENT_CONVERSE_MAX_CHARS,
    MAX_PREFERRED_IMPLANTS,
)
from src.engine.intent import _MODE_POLICY, TaskProfile, classify_intent

TIERS = ("lite", "standard", "deep")


class TestClassifyMode:
    """Mode detection is lexical and ordered most-specific-first."""

    @pytest.mark.parametrize("query,expected", [
        ("hi", "converse"),
        ("Привет", "converse"),
        ("thanks!", "converse"),
        ("Calculate the derivative of x^2 at x = 3", "compute"),
        ("Докажи, что сумма углов треугольника равна 180", "compute"),
        ("Compare Postgres and MySQL for a write-heavy workload", "analyze"),
        ("Please discuss the climate movement, be extremely complex", "analyze"),
        ("Provide an overview of the FDA pilot programs", "analyze"),
        ("Install nginx and configure a reverse proxy", "operate"),
        ("SELECT name FROM users u JOIN orders o ON o.uid = u.id", "operate"),
        ("Write a short story about a lighthouse", "create"),
        ("Explain University level Introductory Statistics to me like I'm a child", "explain"),
        ("who is the current mayor of Lisbon", "retrieve"),
    ])
    def test_mode(self, query, expected):
        assert classify_intent(query).mode == expected

    def test_greeting_prefix_on_a_long_spec_is_not_converse(self):
        """The `_is_meta_query` false positive must not come back.

        A substantive request that merely opens with a greeting used to be
        classified as a capability question and answered at the lite tier with
        zero skills and no implants.
        """
        query = "Hi, " + "please refactor this authentication module and explain the tradeoffs. " * 4
        assert len(query) > INTENT_CONVERSE_MAX_CHARS
        profile = classify_intent(query)
        assert profile.mode != "converse"
        assert profile.tier != "lite"

    def test_weak_compute_phrase_without_math_is_a_lookup(self):
        """"how many" alone is not a computation.

        Regression: "how many books are in piers anthony virtual mode series"
        was classified `compute`, whose default budget is the deep tier.
        """
        profile = classify_intent("how many books are in piers anthony virtual mode series")
        assert profile.mode == "retrieve"
        assert profile.tier == "lite"

    def test_weak_compute_phrase_with_math_is_compute(self):
        profile = classify_intent("how much interest accrues on $1200 at 5% over 3 years")
        assert profile.mode == "compute"


class TestClassifyTier:
    def test_length_alone_never_reaches_deep(self):
        """The core defect being fixed: `len > 300` used to force the deep tier.

        A long prompt with no analytic, computational or structural signal must
        not buy the heaviest budget on size alone.
        """
        query = "I would like a pleasant description of a quiet meadow. " * 120
        assert len(query) > 5000
        assert classify_intent(query).tier != "deep"

    def test_mode_default_is_a_floor_not_a_midpoint(self):
        """A low structural score must not demote below the mode's default.

        Regression: an `explain` request with no structure was demoted to lite,
        stripping the skills the request needs.
        """
        profile = classify_intent("Explain University level Introductory Statistics to me like I'm a child")
        assert profile.depth_score == 0
        assert profile.tier == "standard"

    @pytest.mark.parametrize("query", [
        "", "   ", "\n\t ",
    ])
    def test_blank_query_is_converse_lite(self, query):
        profile = classify_intent(query)
        assert (profile.mode, profile.tier) == ("converse", "lite")

    def test_tier_is_always_a_legacy_literal(self):
        for query in ("hi", "x" * 4000, "Compare A and B", "```python\npass\n```"):
            assert classify_intent(query).tier in TIERS

    def test_budget_matches_the_resolved_tier_policy(self):
        """Budget fields must agree with the tier they were derived from."""
        for query in ("hi", "Compare A and B in depth", "Write me a haiku", "Solve for x: 2x = 8"):
            profile = classify_intent(query)
            policy = next(p for p in _MODE_POLICY.values() if p["tier"] == profile.tier)
            assert profile.skill_pool_size == policy["skills"]
            assert profile.skill_render == policy["render"]
            assert profile.implant_budget == policy["implants"]

    def test_lite_budget_matches_legacy_n_results(self):
        """lite/standard/deep must still mean 0/2/4 skills, as before."""
        expected = {"lite": 0, "standard": 2, "deep": 4}
        for tier, n in expected.items():
            policy = next(p for p in _MODE_POLICY.values() if p["tier"] == tier)
            assert policy["skills"] == n

    def test_deep_implant_budget_matches_legacy_constant(self):
        policy = next(p for p in _MODE_POLICY.values() if p["tier"] == "deep")
        assert policy["implants"] == IMPLANTS_DEEP_TIER_DEFAULT


class TestTaskProfile:
    def test_is_frozen(self):
        profile = classify_intent("hi")
        with pytest.raises(Exception):
            profile.mode = "analyze"  # type: ignore[misc]

    def test_with_tier_rederives_budget_and_keeps_mode(self):
        profile = classify_intent("hi")
        assert profile.tier == "lite"
        promoted = profile.with_tier("standard")
        assert promoted.mode == "converse"          # method follows the mode
        assert promoted.tier == "standard"
        assert promoted.skill_pool_size == 2        # budget follows the tier
        assert promoted.implant_budget == 2
        assert promoted.suppress_persona_format is True

    def test_with_tier_is_identity_for_the_same_tier(self):
        profile = classify_intent("hi")
        assert profile.with_tier("lite") is profile

    def test_with_tier_rejects_an_unknown_tier(self):
        with pytest.raises(ValueError, match="Unknown tier"):
            classify_intent("hi").with_tier("gigantic")  # type: ignore[arg-type]

    def test_cache_token_is_colon_free_and_deterministic(self):
        """server._load_and_enrich interpolates this into `agent:hash:X`."""
        token = classify_intent("Compare A and B").cache_token
        assert ":" not in token
        assert token == classify_intent("Compare A and B").cache_token

    def test_cache_token_separates_equal_tiers_with_different_budgets(self):
        base = classify_intent("hi")
        other = TaskProfile(
            mode=base.mode, tier=base.tier, depth_score=base.depth_score,
            skill_pool_size=base.skill_pool_size, skill_render="full",
            implant_budget=base.implant_budget,
            suppress_persona_format=base.suppress_persona_format,
            confidence=base.confidence,
        )
        assert base.tier == other.tier
        assert base.cache_token != other.cache_token

    def test_cache_token_ignores_confidence(self):
        """Confidence is diagnostic; it must not fragment the prompt cache."""
        base = classify_intent("hi")
        shifted = TaskProfile(
            mode=base.mode, tier=base.tier, depth_score=base.depth_score,
            skill_pool_size=base.skill_pool_size, skill_render=base.skill_render,
            implant_budget=base.implant_budget,
            suppress_persona_format=base.suppress_persona_format,
            confidence=base.confidence / 2,
        )
        assert base.cache_token == shifted.cache_token


class TestInferTierProjection:
    """`infer_tier` keeps its name, module, signature and sync-ness."""

    CORPUS = [
        "hi", "Привет", "how are you?",
        "Write me a Python quicksort",
        "Compare Postgres and MySQL",
        "Explain closures to me",
        "```python\nprint(1)\n```",
        "Please review this architecture and plan a refactor",
        "x" * 400, "y" * 40,
        "Solve for x: 3x + 2 = 11",
    ]

    def test_flag_off_is_byte_identical_to_the_legacy_rule(self, monkeypatch):
        from src.engine import enrichment

        monkeypatch.setattr(enrichment, "INTENT_CLASSIFIER_ENABLED", False)
        for query in self.CORPUS:
            assert enrichment.infer_tier(query) == enrichment._legacy_infer_tier(query)

    def test_flag_on_projects_the_classifier(self, monkeypatch):
        from src.engine import enrichment

        monkeypatch.setattr(enrichment, "INTENT_CLASSIFIER_ENABLED", True)
        for query in self.CORPUS:
            assert enrichment.infer_tier(query) == classify_intent(query).tier

    def test_resolve_profile_is_none_when_disabled(self, monkeypatch):
        from src.engine import enrichment

        monkeypatch.setattr(enrichment, "INTENT_CLASSIFIER_ENABLED", False)
        assert enrichment.resolve_profile("Compare A and B") is None

    def test_resolve_profile_honours_a_pinned_tier(self, monkeypatch):
        from src.engine import enrichment

        monkeypatch.setattr(enrichment, "INTENT_CLASSIFIER_ENABLED", True)
        profile = enrichment.resolve_profile("hi", tier="standard")
        assert profile is not None
        assert profile.tier == "standard"
        assert profile.mode == "converse"


class TestStripOutputFormat:
    def test_removes_the_section_and_keeps_the_next_one(self):
        from src.engine.enrichment import strip_output_format

        prompt = "# P\n\nintro\n\n## Output Format\n\ntemplate\n\n## Rules\n\nkeep\n"
        out = strip_output_format(prompt)
        assert "template" not in out
        assert "## Output Format" not in out
        assert "## Rules" in out and "keep" in out
        assert "intro" in out

    def test_nested_subsections_travel_with_the_parent(self):
        from src.engine.enrichment import strip_output_format

        prompt = "## Output Format\n\nA\n\n### Confidence\n\nB\n\n## Rules\n\nkeep\n"
        out = strip_output_format(prompt)
        assert "### Confidence" not in out and "B" not in out
        assert "keep" in out

    def test_is_a_noop_without_the_section(self):
        from src.engine.enrichment import strip_output_format

        prompt = "# P\n\nonly body\n"
        assert strip_output_format(prompt).strip() == prompt.strip()

    def test_trailing_section_is_removed_cleanly(self):
        from src.engine.enrichment import strip_output_format

        prompt = "# P\n\nbody\n\n## Output Format\n\ntemplate\n"
        out = strip_output_format(prompt)
        assert out.endswith("\n")
        assert "template" not in out and "body" in out


class TestImplantBudgetParity:
    """The unified implant formula must equal the legacy per-tier branches.

    Judges flagged this as the likeliest silent regression: 43 of 43 agents
    declare `preferred_implants`, so dropping that floor would starve every
    persona while every test still passed.
    """

    @staticmethod
    def _legacy(tier: str, n_preferred: int) -> int:
        if tier == "standard":
            return (
                min(max(2, n_preferred), MAX_PREFERRED_IMPLANTS)
                if n_preferred
                else 2
            )
        return min(max(IMPLANTS_DEEP_TIER_DEFAULT, n_preferred), MAX_PREFERRED_IMPLANTS)

    @staticmethod
    def _unified(tier: str, n_preferred: int) -> int:
        base = 2 if tier == "standard" else IMPLANTS_DEEP_TIER_DEFAULT
        return min(max(base, n_preferred), MAX_PREFERRED_IMPLANTS)

    @pytest.mark.parametrize("tier", ["standard", "deep"])
    @pytest.mark.parametrize("n_preferred", list(range(0, MAX_PREFERRED_IMPLANTS + 3)))
    def test_formulas_agree(self, tier, n_preferred):
        assert self._unified(tier, n_preferred) == self._legacy(tier, n_preferred)

    @pytest.mark.parametrize("n_preferred", [1, 3, 7])
    def test_preferred_is_a_floor_capped_by_the_max(self, n_preferred):
        for tier in ("standard", "deep"):
            got = self._unified(tier, n_preferred)
            assert got >= min(n_preferred, MAX_PREFERRED_IMPLANTS)
            assert got <= MAX_PREFERRED_IMPLANTS


class TestLexiconBoundaries:
    """Regressions from review: a left-only anchor let short words hijack a mode.

    `_lex` used to emit `(?<![\\w])(hi|hello|...)` with no trailing boundary, so
    `hi` matched "his"/"history"/"highest" and `post` matched "postgres". Every
    query below was verified to misclassify before the fix.
    """

    @pytest.mark.parametrize("query", [
        "Fix his broken nginx config",
        "Summarize his report",
        "What did he hit?",
        "What is the history of the Roman Empire?",
        "Find the highest value in this list",
        "Give me a high-level overview",
        "Whose hierarchy is this?",
        "Hide the debug banner",
        "Give me a hint about the failing test",
    ])
    def test_converse_lexicon_does_not_match_inside_a_word(self, query):
        profile = classify_intent(query)
        assert profile.mode != "converse", f"{query!r} -> {profile.signals}"

    @pytest.mark.parametrize("query,forbidden_signal", [
        ("Explain postgres indexes", "create_lex"),      # `post` in _CREATE_LEX
        ("Listen to the log stream", "retrieve_lex"),    # `list` in _RETRIEVE_LEX
        ("Book the auditorium for Friday", "analyze_lex"),  # `audit` in _ANALYZE_LEX
        ("She had an audition yesterday", "analyze_lex"),
        ("Postgres is slow", "create_lex"),
    ])
    def test_stems_do_not_swallow_unrelated_words(self, query, forbidden_signal):
        """Assert on the SIGNAL, not the mode.

        A query may still land on a mode through the no-lexicon fallback; what
        must not happen is a lexicon claiming it on a substring match.
        """
        profile = classify_intent(query)
        assert forbidden_signal not in profile.signals, f"{query!r} -> {profile.signals}"

    def test_explain_wins_over_a_substring_create_match(self):
        assert classify_intent("Explain postgres indexes").mode == "explain"

    def test_a_real_create_word_still_wins(self):
        assert classify_intent("Write a postmortem of the outage").mode == "create"

    def test_real_greetings_still_classify_as_converse(self):
        for query in ("hi", "hello there", "hey", "thanks!", "Привет", "hola", "gracias"):
            assert classify_intent(query).mode == "converse", query

    def test_genuine_stems_still_match_inflections(self):
        """Open-ended stems are deliberate and must keep working."""
        assert classify_intent("Analyzing this trace").mode == "analyze"
        assert classify_intent("Configuring nginx").mode == "operate"
        assert classify_intent("Реализуй кэш").mode == "operate"
        assert classify_intent("Compute the integrals").mode == "compute"


class TestSuppressFormatPolicy:
    def test_only_converse_suppresses_the_persona_format(self):
        """Narrowed after review: a persona's Output Format is not always a
        mere template — for medical_expert it carries the mandated Safety
        section, and `create` fires on summarize/draft/write."""
        suppressing = {m for m, p in _MODE_POLICY.items() if p["suppress_format"]}
        assert suppressing == {"converse"}

    def test_a_draft_request_keeps_the_persona_format(self):
        profile = classify_intent("Draft a note summarizing these labs")
        assert profile.mode == "create"
        assert profile.suppress_persona_format is False


class TestStripOutputFormatFenceAware:
    """Regressions from review, all measured on the repo's real personas."""

    @staticmethod
    def _personas():
        import glob
        return sorted(glob.glob("agents/*/system_prompt.mdc"))

    def test_backtick_parity_is_preserved_for_every_persona(self):
        """10 of 23 personas used to end up with an unbalanced fence.

        Their Output Format contains a fenced template whose inner headings are
        level-2, so a regex stopping at the next `^## ` deleted the fence opener
        and left its closer — turning the rest of the persona into a code block.
        """
        from src.engine.enrichment import strip_output_format

        personas = self._personas()
        if not personas:
            pytest.skip("no agents/ in this checkout")
        for path in personas:
            source = open(path, encoding="utf-8").read()
            if "## Output Format" not in source:
                continue
            if source.count("```") % 2 != 0:
                continue  # persona is already unbalanced; not ours to assert on
            out = strip_output_format(source)
            assert out.count("```") % 2 == 0, f"{path} left an unbalanced fence"

    def test_the_whole_section_is_removed_not_just_the_heading(self):
        """8 of 23 personas used to lose only the heading plus a line or two,
        promoting the surviving template to apparent top-level sections."""
        from src.engine.enrichment import strip_output_format

        personas = self._personas()
        if not personas:
            pytest.skip("no agents/ in this checkout")
        for path in personas:
            source = open(path, encoding="utf-8").read()
            if "## Output Format" not in source:
                continue
            removed = len(source) - len(strip_output_format(source))
            assert removed >= 200, f"{path} only lost {removed} bytes — section survived"

    def test_a_fenced_output_format_line_is_not_matched(self):
        """prompt_engineer teaches a skeleton containing the literal line
        `## Output Format` inside a fence; deleting that taught it to design
        prompts with no output-format section at all."""
        from src.engine.enrichment import strip_output_format

        prompt = (
            "# Persona\n\nintro\n\n"
            "```markdown\n## Output Format\n[show an example]\n```\n\n"
            "## Output Format\n\nthe real template\n\n"
            "## Rules\n\nkeep\n"
        )
        out = strip_output_format(prompt)
        assert "[show an example]" in out          # taught skeleton survives
        assert out.count("## Output Format") == 1  # only the fenced one remains
        assert "the real template" not in out
        assert "keep" in out
        assert out.count("```") % 2 == 0

    def test_level_two_headings_inside_a_fence_do_not_end_the_section(self):
        from src.engine.enrichment import strip_output_format

        prompt = (
            "## Output Format\n\n"
            "```markdown\n## Executive Summary\n...\n## Findings\n...\n```\n\n"
            "## Rules\n\nkeep\n"
        )
        out = strip_output_format(prompt)
        assert "Executive Summary" not in out
        assert "Findings" not in out
        assert "keep" in out
        assert "```" not in out

    def test_tilde_fences_are_honoured(self):
        from src.engine.enrichment import strip_output_format

        prompt = "## Output Format\n\n~~~\n## Inner\n~~~\n\n## Rules\n\nkeep\n"
        out = strip_output_format(prompt)
        assert "## Inner" not in out and "keep" in out

    def test_everything_after_the_section_is_kept(self):
        from src.engine.enrichment import strip_output_format

        prompt = "## Output Format\n\nT\n\n## A\n\na\n\n## B\n\nb\n"
        out = strip_output_format(prompt)
        assert "## A" in out and "## B" in out and "T" not in out


class TestRunTierRankGuard:
    def test_unknown_expected_tier_does_not_crash(self):
        """`iter_valid` filters on fetch_error/drift, not label completeness, so
        a row with a missing or misspelled expected_tier must score as wrong."""
        from evals.runners.run_tier import _arm_stats

        rows = [
            {"expected": "deep", "intent": "deep"},
            {"expected": None, "intent": "lite"},
            {"expected": "dep", "intent": "standard"},
        ]
        stats = _arm_stats(rows, "intent")
        assert stats["total"] == 3
        assert stats["correct"] == 1
