"""Tests for the RepoDescriber: hashing, refresh logic, prompt rendering, write_summary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.memory import managed_section
from src.memory.config import DESCRIBE_MARKER_BEGIN, DESCRIBE_MARKER_END
from src.memory.describer import (
    DESCRIBE_PROMPT_TEMPLATE,
    DescribeDecision,
    RepoDescriber,
)


def _seed_repo(root: Path) -> None:
    """Minimal but realistic repo structure."""
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hi')\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_main.py").write_text("def test(): pass\n")
    (root / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\n"
    )
    (root / "README.md").write_text(
        "# Demo\n\nA tiny demo repo for tests.\n" + ("line\n" * 10)
    )


@pytest.fixture
def describer(tmp_path):
    _seed_repo(tmp_path)
    return RepoDescriber(repo_path=str(tmp_path))


def _valid_summary(marker: str = "v1") -> str:
    """Build a summary that passes write_summary's sanity gate (>=200 words + ##)."""
    words = " ".join(["demo"] * 250)
    return f"## Project Identity\n\n{marker} {words}"


class TestHash:
    def test_deterministic(self, describer):
        h1 = describer.compute_repo_hash()
        h2 = describer.compute_repo_hash()
        assert h1 == h2 and len(h1) == 32

    def test_changes_on_new_top_level_file(self, describer, tmp_path):
        before = describer.compute_repo_hash()
        (tmp_path / "Makefile").write_text("all:\n\techo hi\n")
        after = describer.compute_repo_hash()
        assert before != after

    def test_changes_on_manifest_edit(self, describer, tmp_path):
        before = describer.compute_repo_hash()
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = \"demo\"\nversion = \"0.2.0\"\n"
        )
        after = describer.compute_repo_hash()
        assert before != after

    def test_excluded_dirs_dont_affect_hash(self, describer, tmp_path):
        before = describer.compute_repo_hash()
        # Adding files inside an excluded dir must not bust the hash
        excluded = tmp_path / "data"
        excluded.mkdir()
        (excluded / "anything.bin").write_text("noise")
        after = describer.compute_repo_hash()
        assert before == after


class TestPlan:
    def test_first_run_needs_refresh(self, describer):
        decision = describer.plan()
        assert decision.needs_refresh is True
        assert decision.cached_hash is None
        assert decision.cached_summary is None

    def test_cached_after_write(self, describer):
        decision1 = describer.plan()
        describer.write_summary(_valid_summary(), decision1.current_hash)
        decision2 = describer.plan()
        assert decision2.needs_refresh is False
        assert decision2.cached_hash == decision1.current_hash
        assert decision2.cached_summary is not None

    def test_force_refresh_overrides_cache(self, describer):
        decision1 = describer.plan()
        describer.write_summary(_valid_summary(), decision1.current_hash)
        decision2 = describer.plan(force_refresh=True)
        assert decision2.needs_refresh is True

    def test_refresh_when_section_manually_deleted(self, describer):
        decision1 = describer.plan()
        describer.write_summary(_valid_summary(), decision1.current_hash)
        # User wipes the managed section by hand
        managed_section.remove_section(
            describer.claude_md_path, DESCRIBE_MARKER_BEGIN, DESCRIBE_MARKER_END
        )
        decision2 = describer.plan()
        assert decision2.needs_refresh is True
        assert decision2.cached_hash is not None  # hash file still present


class TestPromptRendering:
    def test_prompt_contains_repo_name_and_bundle(self, describer, tmp_path):
        prompt = describer.build_prompt()
        assert tmp_path.name in prompt
        # CONTEXT_BUNDLE expansion includes the manifest content
        assert "demo" in prompt
        # And the tree
        assert "src/" in prompt or "src\n" in prompt
        # And the README header
        assert "# Demo" in prompt
        # No leftover placeholders
        assert "{CONTEXT_BUNDLE}" not in prompt
        assert "{REPO_NAME}" not in prompt
        assert "{WORD_MIN}" not in prompt

    def test_prompt_template_is_a_const_string(self):
        # Sanity: keep the template string immutable so callers can introspect it
        assert "{CONTEXT_BUNDLE}" in DESCRIBE_PROMPT_TEMPLATE
        assert "{REPO_NAME}" in DESCRIBE_PROMPT_TEMPLATE

    def test_excluded_dirs_pruned_from_tree(self, describer, tmp_path):
        # Add a vendor-ish dir; make sure it does NOT appear in the tree
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "junk.js").write_text("x")
        prompt = describer.build_prompt()
        assert "node_modules" not in prompt


class TestWriteSummary:
    def test_writes_managed_section_with_header(self, describer):
        decision = describer.plan()
        result = describer.write_summary(_valid_summary(), decision.current_hash)
        assert result["status"] == "refreshed"
        assert result["action"] in ("created", "appended", "replaced")

        section = managed_section.read_section(
            describer.claude_md_path, DESCRIBE_MARKER_BEGIN, DESCRIBE_MARKER_END
        )
        assert section is not None
        # Header injected by write_summary
        assert "Auto-generated by `describe_repo`" in section
        assert "Hash:" in section
        # Body preserved
        assert "## Project Identity" in section

    def test_word_count_reported(self, describer):
        decision = describer.plan()
        body = "## Project Identity\n\n" + " ".join(["word"] * 1000)
        result = describer.write_summary(body, decision.current_hash)
        # Heading ("##", "Project", "Identity" = 3 tokens) + 1000 body words
        assert result["word_count"] == 1003
        # 1003 ∈ [DESCRIBE_WORD_MIN=800, DESCRIBE_WORD_MAX=1500]
        assert result["in_word_budget"] is True

    def test_word_count_below_budget_still_persists(self, describer):
        # 200-799 words: below DESCRIBE_WORD_MIN but above the persistence floor,
        # so we still write and flag via in_word_budget.
        decision = describer.plan()
        body = "## Project Identity\n\n" + " ".join(["ok"] * 250)
        result = describer.write_summary(body, decision.current_hash)
        assert result["status"] == "refreshed"
        assert result["in_word_budget"] is False

    def test_short_summary_rejected(self, describer):
        """Summaries below MIN_PERSIST_WORD_COUNT must not overwrite CLAUDE.md."""
        decision = describer.plan()
        # Has a heading but only a handful of words — likely a failed sample.
        result = describer.write_summary("## Heading\n\nonly a few words", decision.current_hash)
        assert result["status"] == "rejected"
        assert result["word_count"] < RepoDescriber.MIN_PERSIST_WORD_COUNT

    def test_summary_without_heading_rejected(self, describer):
        """Missing any ## heading => looks like a refusal/garbage, do not persist."""
        decision = describer.plan()
        body = " ".join(["word"] * 500)  # enough words but no structure
        result = describer.write_summary(body, decision.current_hash)
        assert result["status"] == "rejected"
        assert result["has_heading"] is False

    def test_rejected_summary_does_not_touch_cache(self, describer):
        """Sanity check failure must leave any existing cache intact."""
        decision = describer.plan()
        # Seed a good summary first.
        describer.write_summary(_valid_summary("good"), decision.current_hash)
        # Attempt a bad overwrite.
        describer.write_summary("trash", decision.current_hash)
        # Managed section still carries the good summary.
        section = managed_section.read_section(
            describer.claude_md_path, DESCRIBE_MARKER_BEGIN, DESCRIBE_MARKER_END
        )
        assert section is not None and "good" in section

    def test_idempotent_replace(self, describer):
        decision = describer.plan()
        describer.write_summary(_valid_summary("one"), decision.current_hash)
        describer.write_summary(_valid_summary("two"), decision.current_hash)
        # Exactly one managed section after the second write
        with open(describer.claude_md_path) as f:
            content = f.read()
        assert content.count(DESCRIBE_MARKER_BEGIN) == 1
        assert content.count(DESCRIBE_MARKER_END) == 1
        # Old body gone
        assert "one demo" not in content
        assert "two demo" in content


class TestUpToDateResponse:
    def test_includes_preview(self, describer):
        decision = describer.plan()
        describer.write_summary(_valid_summary(), decision.current_hash)
        decision2 = describer.plan()
        payload = describer.up_to_date_response(decision2)
        assert payload["status"] == "up-to-date"
        # First heading line of _valid_summary()
        assert "Project Identity" in payload["summary_preview"]


class TestPathOverrideSafety:
    def test_does_not_clobber_global_claude_md(self, tmp_path):
        """When repo_path is overridden, CLAUDE.md/hash default to that subtree."""
        _seed_repo(tmp_path)
        d = RepoDescriber(repo_path=str(tmp_path))
        assert d.claude_md_path.startswith(str(tmp_path))
        assert d.hash_file.startswith(str(tmp_path))
