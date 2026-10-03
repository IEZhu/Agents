"""Embedder batching and the fingerprint of the loaded model (#157)."""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from src.engine import embedder

ROOT = Path(__file__).resolve().parents[1]


class RecordingModel:
    def __init__(self):
        self.calls = []

    def passage_embed(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        return ([float(len(text)), 1.0] for text in texts)


def test_embed_texts_passes_configured_batch_size(monkeypatch):
    model = RecordingModel()
    monkeypatch.setattr(embedder, "_get_model", lambda: model)
    monkeypatch.setattr(embedder, "EMBEDDING_BATCH_SIZE", 3)

    vectors = embedder._embed_texts(["a", "bb", "ccc", "dddd"])

    assert vectors.shape == (4, 2)
    assert model.calls == [(["a", "bb", "ccc", "dddd"], {"batch_size": 3})]


def _configured_batch_size(value):
    env = {key: item for key, item in os.environ.items() if key != "EMBEDDING_BATCH_SIZE"}
    if value is not None:
        env["EMBEDDING_BATCH_SIZE"] = value
    result = subprocess.run(
        [sys.executable, "-c", "from src.engine.config import EMBEDDING_BATCH_SIZE; print(EMBEDDING_BATCH_SIZE)"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True,
    )
    return int(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 4), ("16", 16), ("1", 1), ("256", 256), ("0", 4), ("257", 4), ("many", 4)],
)
def test_batch_size_setting(value, expected):
    assert _configured_batch_size(value) == expected


class FakeTextEmbedding:
    def __init__(self, model_name, cache_dir, **options):
        self.model_name = model_name


@pytest.fixture
def model_cache(tmp_path, monkeypatch):
    """An unloaded embedder whose model cache is a temporary snapshot ref."""
    import src.engine.config as config
    from src.engine.fingerprint import fingerprint

    monkeypatch.setattr(config, "FASTEMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(embedder, "FASTEMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTS_MODEL_ARTIFACT", raising=False)
    monkeypatch.delenv("AGENTS_MODEL_PATH", raising=False)
    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeTextEmbedding))
    monkeypatch.setattr(embedder, "_model", None)
    monkeypatch.setattr(embedder, "_model_fingerprint", None)
    fingerprint.cache_clear()
    yield tmp_path / ("models--org--" + config.EMBEDDING_MODEL.split("/")[-1]) / "refs" / "main"
    fingerprint.cache_clear()


def _set_snapshot(ref, revision):
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(revision)


def test_model_fingerprint_names_the_loaded_snapshot(model_cache):
    from src.engine.fingerprint import fingerprint

    _set_snapshot(model_cache, "old-revision")
    embedder._get_model()
    loaded = embedder.model_fingerprint()

    # Another process downloads a newer snapshot into the shared cache.
    _set_snapshot(model_cache, "new-revision")
    newer = fingerprint.__wrapped__(embedder.EMBEDDING_MODEL)

    assert newer != loaded
    assert embedder.model_fingerprint() == loaded

    embedder.reset_model()
    assert embedder._model_fingerprint is None
    embedder._get_model()
    assert embedder.model_fingerprint() == newer


def test_model_fingerprint_before_the_first_load(model_cache):
    from src.engine.fingerprint import fingerprint

    _set_snapshot(model_cache, "only-revision")

    assert embedder.model_fingerprint() == fingerprint(embedder.EMBEDDING_MODEL)
