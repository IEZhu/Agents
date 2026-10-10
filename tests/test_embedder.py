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
    # A model without a passage prompt, so the texts reach fastembed as given.
    monkeypatch.setattr(embedder, "EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

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
    """Opens the snapshot that refs/main names when the load starts, like fastembed."""

    during_load = None  # set by a test: runs after the snapshot is chosen

    def __init__(self, model_name, cache_dir, **options):
        repo = Path(cache_dir) / _repo_dir(model_name)
        commit = (repo / "refs" / "main").read_text()
        self.model = types.SimpleNamespace(_model_dir=repo / "snapshots" / commit)
        if FakeTextEmbedding.during_load:
            FakeTextEmbedding.during_load()


def _repo_dir(model_name):
    return "models--org--" + model_name.split("/")[-1]


@pytest.fixture
def model_cache(tmp_path, monkeypatch):
    """An unloaded embedder whose model cache is a temporary snapshot ref."""
    import src.engine.config as config
    from src.engine.fingerprint import fingerprint

    # Use a snapshot-backed model regardless of the installation's configured
    # model; plain-file exports have pinned fingerprints that ignore cache refs.
    model_name = "intfloat/multilingual-e5-large"
    monkeypatch.setattr(config, "EMBEDDING_MODEL", model_name)
    monkeypatch.setattr(embedder, "EMBEDDING_MODEL", model_name)
    monkeypatch.setattr(config, "FASTEMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(embedder, "FASTEMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTS_MODEL_ARTIFACT", raising=False)
    monkeypatch.delenv("AGENTS_MODEL_PATH", raising=False)
    monkeypatch.setattr(embedder, "register_custom", lambda model: None)
    monkeypatch.setattr(embedder, "materialize", lambda model, cache_dir: None)
    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeTextEmbedding))
    monkeypatch.setattr(FakeTextEmbedding, "during_load", None)
    monkeypatch.setattr(embedder, "_model", None)
    monkeypatch.setattr(embedder, "_model_fingerprint", None)
    fingerprint.cache_clear()
    yield tmp_path / _repo_dir(config.EMBEDDING_MODEL) / "refs" / "main"
    fingerprint.cache_clear()


def _set_snapshot(ref, revision):
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(revision)


def test_model_fingerprint_names_the_snapshot_that_was_loaded(model_cache, monkeypatch):
    from src.engine.fingerprint import compute_fingerprint

    _set_snapshot(model_cache, "old-revision")
    loaded = compute_fingerprint(embedder.EMBEDDING_MODEL)
    # Another process moves refs/main while this process is still loading.
    monkeypatch.setattr(FakeTextEmbedding, "during_load", lambda: _set_snapshot(model_cache, "new-revision"))

    embedder._get_model()

    newer = compute_fingerprint(embedder.EMBEDDING_MODEL)
    assert newer != loaded
    assert embedder.model_fingerprint() == loaded

    monkeypatch.setattr(FakeTextEmbedding, "during_load", None)
    embedder.reset_model()
    assert embedder._model_fingerprint is None
    embedder._get_model()
    assert embedder.model_fingerprint() == newer


def test_loaded_fingerprint_reads_the_opened_snapshot_not_refs(model_cache):
    from src.engine.fingerprint import compute_fingerprint

    _set_snapshot(model_cache, "refs-revision")
    repo = model_cache.parent.parent
    model = types.SimpleNamespace(model=types.SimpleNamespace(_model_dir=repo / "snapshots" / "opened-revision"))

    label = embedder._loaded_fingerprint(model)

    assert label == compute_fingerprint(embedder.EMBEDDING_MODEL, revision=f"{repo.name}:opened-revision")
    assert label != compute_fingerprint(embedder.EMBEDDING_MODEL)


def test_missing_model_directory_is_reported(model_cache, caplog):
    from src.engine.fingerprint import compute_fingerprint

    _set_snapshot(model_cache, "refs-revision")

    with caplog.at_level("WARNING", logger=embedder.logger.name):
        label = embedder._loaded_fingerprint(types.SimpleNamespace(model=types.SimpleNamespace()))

    assert label == compute_fingerprint(embedder.EMBEDDING_MODEL)
    assert "exposes no model directory" in caplog.text


def test_model_fingerprint_falls_back_to_refs_for_other_directories(model_cache, monkeypatch):
    from src.engine.fingerprint import compute_fingerprint

    _set_snapshot(model_cache, "only-revision")
    monkeypatch.setattr(FakeTextEmbedding, "during_load", None)
    original_init = FakeTextEmbedding.__init__

    def local_directory(self, model_name, cache_dir, **options):
        original_init(self, model_name, cache_dir, **options)
        self.model._model_dir = Path(cache_dir) / "local-model"

    monkeypatch.setattr(FakeTextEmbedding, "__init__", local_directory)
    embedder._get_model()

    assert embedder.model_fingerprint() == compute_fingerprint(embedder.EMBEDDING_MODEL)


def test_pinned_artifact_takes_precedence(model_cache, monkeypatch):
    from src.engine.fingerprint import compute_fingerprint

    _set_snapshot(model_cache, "only-revision")
    unpinned = compute_fingerprint(embedder.EMBEDDING_MODEL)
    monkeypatch.setenv("AGENTS_MODEL_ARTIFACT", "pinned-artifact")
    pinned = compute_fingerprint(embedder.EMBEDDING_MODEL)

    embedder._get_model()

    assert pinned != unpinned
    assert embedder.model_fingerprint() == pinned
    assert compute_fingerprint(embedder.EMBEDDING_MODEL, revision="other") == pinned


def test_model_fingerprint_before_the_first_load(model_cache):
    from src.engine.fingerprint import compute_fingerprint, fingerprint

    _set_snapshot(model_cache, "only-revision")

    assert embedder.model_fingerprint() == fingerprint(embedder.EMBEDDING_MODEL)
    assert fingerprint(embedder.EMBEDDING_MODEL) == compute_fingerprint(embedder.EMBEDDING_MODEL)


def test_a_plain_file_copy_that_fails_to_load_is_removed_on_the_last_attempt(model_cache, monkeypatch, tmp_path):
    copy = tmp_path / "local" / "copy"
    copy.mkdir(parents=True)

    class BrokenTextEmbedding:
        def __init__(self, model_name, cache_dir, **options):
            raise RuntimeError("corrupt weights")

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=BrokenTextEmbedding))
    monkeypatch.setattr(embedder, "materialize", lambda model, cache_dir: str(copy))
    monkeypatch.setattr(embedder, "register_custom", lambda model: None)
    monkeypatch.setattr(embedder, "_MAX_LOAD_RETRIES", 1)  # the daemon's single attempt
    with pytest.raises(RuntimeError):
        embedder._get_model()
    assert not copy.exists()
