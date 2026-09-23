"""Implant layer: trigger parsing, index-text modes and the z-score gate."""
from __future__ import annotations

from types import SimpleNamespace

import src.engine.config as cfg
from src.engine import implants
from src.engine.implants import ImplantRetriever, _index_text, _normalize_triggers, _trigger_hit, _when_to_use

BODY = "## Pattern\n1. Do it.\n\n## When to Use\n- Comparing options\n- Risky launches\n\n## Limitations\n- Slow\n"


def test_normalize_triggers_tolerates_bad_input():
    assert _normalize_triggers(None) == []
    assert _normalize_triggers("What Could Go Wrong") == ["what could go wrong"]
    assert _normalize_triggers(["  Risks ", 3, "", "Что пойдёт не так"]) == ["risks", "что пойдёт не так"]


def test_when_to_use_extracts_only_that_section():
    assert _when_to_use(BODY) == "- Comparing options\n- Risky launches"
    assert _when_to_use("## Pattern\nno section") == ""


def test_index_text_modes(monkeypatch):
    monkeypatch.setattr(cfg, "IMPLANT_INDEX_MODE", "legacy")
    assert "## Pattern" in _index_text("Desc", BODY, ["risks"])
    monkeypatch.setattr(cfg, "IMPLANT_INDEX_MODE", "triggers")
    text = _index_text("Desc", BODY, ["risks", "before we commit"])
    assert "risks; before we commit" in text and "Comparing options" in text
    assert "## Pattern" not in text and "Slow" not in text


def test_trigger_hit_respects_word_boundaries():
    assert _trigger_hit(["risks"], "what are the risks here?")
    assert not _trigger_hit(["risk"], "asterisks everywhere")
    assert _trigger_hit(["какие риски"], "скажи, какие риски у плана")


def _retriever_with(distances: dict[str, float], triggers: dict[str, list[str]] | None = None) -> ImplantRetriever:
    ids = sorted(distances, key=distances.get)
    triggers = triggers or {}
    store = SimpleNamespace(
        count=lambda: len(ids),
        query=lambda query_embedding, n_results: SimpleNamespace(
            ids=ids[:n_results],
            distances=[distances[i] for i in ids[:n_results]],
            metadatas=[{"body": f"body {i}", "triggers": triggers.get(i, [])} for i in ids[:n_results]],
            documents=[f"doc {i}" for i in ids[:n_results]],
        ),
    )
    r = ImplantRetriever.__new__(ImplantRetriever)
    r.store = store
    return r


def test_zscore_gate_can_select_nothing(monkeypatch):
    # An evenly spread band has no outlier: its closest item sits at z ≈ -1.57.
    monkeypatch.setattr(cfg, "IMPLANT_GATE_Z", 2.0)
    flat = {f"i{k}.mdc": 0.18 + 0.001 * k for k in range(10)}
    assert _retriever_with(flat)._zscore_candidates("hello", None, 3, set()) == []


def test_zscore_gate_keeps_the_outlier_and_skips_preferred(monkeypatch):
    monkeypatch.setattr(cfg, "IMPLANT_GATE_Z", 1.5)
    dists = {f"i{k}.mdc": 0.20 for k in range(9)} | {"star.mdc": 0.10}
    got = _retriever_with(dists)._zscore_candidates("q", None, 3, set())
    assert [g["filename"] for g in got] == ["star.mdc"]
    assert _retriever_with(dists)._zscore_candidates("q", None, 3, {"star.mdc"}) == []


def test_trigger_boost_can_lift_an_implant_over_the_gate(monkeypatch):
    monkeypatch.setattr(cfg, "IMPLANT_GATE_Z", 2.0)
    monkeypatch.setattr(cfg, "IMPLANT_TRIGGER_BOOST", 0.5)
    dists = {f"i{k}.mdc": 0.20 + 0.001 * k for k in range(10)}
    r = _retriever_with(dists, {"i5.mdc": ["what could go wrong"]})
    assert r._zscore_candidates("tell me what could go wrong", None, 3, set())[0]["filename"] == "i5.mdc"
    assert r._zscore_candidates("tell me a joke", None, 3, set()) == []


def test_every_implant_declares_triggers():
    # The trigger vocabulary is the implant layer's own keyword set; an implant
    # without one can only be reached by topic similarity.
    import glob
    import os
    import yaml
    from src.utils.prompt_loader import split_frontmatter

    for path in glob.glob(os.path.join(implants.IMPLANTS_DIR, "implant-*.mdc")):
        with open(path, encoding="utf-8") as f:
            fm = yaml.safe_load(split_frontmatter(f.read())[0]) or {}
        assert _normalize_triggers(fm.get("triggers")), f"{os.path.basename(path)} has no triggers"


def test_need_gate_off_keeps_legacy_tier_rule(monkeypatch):
    from src.engine import enrichment

    monkeypatch.setattr(cfg, "IMPLANT_NEED_GATE", "off")
    assert enrichment.implants_needed("hi", "lite") is False
    assert enrichment.implants_needed("hi", "standard") is True
    assert enrichment.implants_needed("hi", "deep") is True


def test_need_gate_intent_adds_the_budget_without_changing_tier(monkeypatch):
    from src.engine import enrichment

    monkeypatch.setattr(cfg, "IMPLANT_NEED_GATE", "intent")
    budgets = {"q-none": 0, "q-some": 2}
    monkeypatch.setattr(
        enrichment, "classify_intent",
        lambda q: SimpleNamespace(implant_budget=budgets[q]),
    )
    assert enrichment.implants_needed("q-none", "deep") is False
    assert enrichment.implants_needed("q-some", "standard") is True
    # lite stays closed regardless of the classifier
    assert enrichment.implants_needed("q-some", "lite") is False


def test_profile_budget_wins_over_the_need_gate(monkeypatch):
    from src.engine import enrichment

    monkeypatch.setattr(cfg, "IMPLANT_NEED_GATE", "off")
    assert enrichment.implants_needed("x", "deep", SimpleNamespace(implant_budget=0)) is False
    assert enrichment.implants_needed("x", "lite", SimpleNamespace(implant_budget=1)) is True


def test_choice_env_rejects_unknown_mode(monkeypatch, caplog):
    # A planned-but-unshipped mode must warn, not silently run the default.
    monkeypatch.setenv("IMPLANT_GATING", "margin")
    with caplog.at_level("WARNING"):
        assert cfg._choice_env("IMPLANT_GATING", "legacy", ("legacy", "zscore")) == "legacy"
    assert "IMPLANT_GATING" in caplog.text
    monkeypatch.setenv("IMPLANT_GATING", " ZScore ")
    assert cfg._choice_env("IMPLANT_GATING", "legacy", ("legacy", "zscore")) == "zscore"
    monkeypatch.delenv("IMPLANT_GATING")
    assert cfg._choice_env("IMPLANT_GATING", "legacy", ("legacy", "zscore")) == "legacy"
