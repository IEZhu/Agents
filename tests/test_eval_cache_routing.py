"""The cache-routing eval's metrics and the loader's inline query texts; no model is loaded."""
from __future__ import annotations

import json

import numpy as np
import pytest

from evals.runners import run_cache_routing as cache
from evals.runners._loader import load_samples


def _pair_vectors() -> np.ndarray:
    # Rows 0 and 1 are near-identical; rows 2 and 3 are only loosely related.
    return np.array([[1.0, 0.0, 0.0], [0.99, 0.14, 0.0], [0.0, 1.0, 0.0], [0.0, 0.6, 0.8]])


def test_nearest_skips_the_row_itself():
    idx, sim = cache.nearest(_pair_vectors())
    assert list(idx) == [1, 0, 3, 2]
    assert sim[0] == pytest.approx(0.99 / np.hypot(0.99, 0.14))
    assert sim[2] == pytest.approx(0.6)


def test_cache_metrics_report_coverage_precision_and_cross_language_hits():
    idx, sim = cache.nearest(_pair_vectors())
    result = cache.cache_metrics(["a", "a", "b", "c"], ["en", "ru", "en", "ru"], idx, sim, threshold=0.95)
    assert result["all"]["queries"] == 4
    assert result["all"]["nn_accuracy"] == 0.5          # 2 and 3 point at each other with different agents
    assert result["all"]["coverage"] == 0.5             # only the 0-1 pair passes 0.95
    assert result["all"]["precision"] == 1.0
    assert result["all"]["at_coverage"]["50%"]["precision"] == 1.0
    assert result["all"]["at_coverage"]["10%"]["similarity"] == pytest.approx(sim[0])
    assert result["ru"]["queries"] == 2
    assert result["cross_language"] == {"queries": 4, "nn_accuracy": 0.5}
    markdown = cache.to_markdown({"model": "m", "threshold": 0.95, "samples": 4, "sets": result})
    assert "| all | 4 | 50% | 50% | 100% |" in markdown


def test_loader_reads_inline_queries_without_a_fetch(tmp_path):
    path = tmp_path / "routing_ru.jsonl"
    rows = [{"id": "ru-1", "language": "ru", "expected_agent": "lawyer", "query": "Как вернуть НДФЛ за квартиру?"},
            {"id": "ru-2", "language": "ru", "expected_agent": "lawyer", "query": "Текст", "source_row_hash": "sha256:0"}]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    samples, stats = load_samples(path)
    assert [s.query for s in samples] == ["Как вернуть НДФЛ за квартиру?", "Текст"]
    assert [s.drift for s in samples] == [False, True]  # a stated hash is still checked
    assert stats.fetch_errors == 0
