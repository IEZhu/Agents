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
    from src.engine.onnx_embedding import TextEmbedding
    monkeypatch.setattr(FakeTextEmbedding, "_spec", TextEmbedding._spec, raising=False)
    monkeypatch.setattr("src.engine.onnx_embedding.TextEmbedding", FakeTextEmbedding)
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

    from src.engine.onnx_embedding import InvalidModelCache, TextEmbedding

    class BrokenTextEmbedding:
        _spec = TextEmbedding._spec

        def __init__(self, model_name, cache_dir, **options):
            raise InvalidModelCache(copy, "corrupt weights")

    monkeypatch.setattr("src.engine.onnx_embedding.TextEmbedding", BrokenTextEmbedding)
    monkeypatch.setattr(embedder, "materialize", lambda model, cache_dir: str(copy))
    monkeypatch.setattr(embedder, "local_copy", lambda model, cache_dir: str(copy))
    monkeypatch.setattr(embedder, "register_custom", lambda model: None)
    monkeypatch.setattr(embedder, "_MAX_LOAD_RETRIES", 1)  # the daemon's single attempt
    with pytest.raises(InvalidModelCache, match="corrupt weights"):
        embedder._get_model()
    assert not copy.exists()


@pytest.mark.parametrize("attempts", [1, 2])
@pytest.mark.parametrize("legacy", [True, False])
def test_failed_archive_initialization_clears_managed_cache_even_on_last_attempt(model_cache, monkeypatch, attempts, legacy):
    """A positively identified bad archive is removed, even on the last attempt."""
    from src.engine.onnx_embedding import InvalidModelCache
    model_name = "BAAI/bge-base-en"
    monkeypatch.setattr(embedder, "EMBEDDING_MODEL", model_name)
    monkeypatch.setattr(embedder, "_MAX_LOAD_RETRIES", attempts)
    cache = Path(embedder.FASTEMBED_CACHE_DIR)
    archive = cache / (("fast-" if legacy else "") + "bge-base-en")
    archive.mkdir()
    (archive / "tokenizer_config.json").write_text("{")
    calls = []
    class BrokenTextEmbedding:
        @classmethod
        def _spec(cls, name):
            return {"model": model_name, "sources": {"hf": "BAAI/bge-base-en", "url": "https://example.invalid/model", "_deprecated_tar_struct": legacy}}
        def __init__(self, **options):
            calls.append(archive.exists())
            raise InvalidModelCache(archive, "malformed cached tokenizer")
    monkeypatch.setattr("src.engine.onnx_embedding.TextEmbedding", BrokenTextEmbedding)
    with pytest.raises(InvalidModelCache, match="malformed cached tokenizer"):
        embedder._get_model()
    assert calls == ([True] if attempts == 1 else [True, False])
    assert not archive.exists()
    assert embedder._model is None and embedder._model_fingerprint is None


def test_cache_cleanup_preserves_other_repositories_and_external_paths(model_cache, monkeypatch, tmp_path):
    """Cleanup targets exact managed names and rejects an outside plain copy."""
    cache = Path(embedder.FASTEMBED_CACHE_DIR) / "managed"
    cache.mkdir()
    monkeypatch.setattr(embedder, "FASTEMBED_CACHE_DIR", str(cache))
    selected = cache / "models--qdrant--multilingual-e5-large-onnx"
    unrelated = cache / "models--other--multilingual-e5-large-onnx"
    selected.mkdir()
    unrelated.mkdir()
    embedder.clear_model_cache("intfloat/multilingual-e5-large")
    assert not selected.exists() and unrelated.is_dir()
    outside = tmp_path / "outside-model"
    outside.mkdir()
    monkeypatch.setattr(embedder, "local_copy", lambda *args: str(outside))
    embedder.clear_model_cache("intfloat/multilingual-e5-large")
    assert outside.is_dir() and unrelated.is_dir()


def test_cache_cleanup_never_follows_model_directory_symlinks(model_cache, monkeypatch, tmp_path):
    """A linked cache cannot turn cleanup into deletion outside its root."""
    cache = tmp_path / "managed"
    cache.mkdir()
    monkeypatch.setattr(embedder, "FASTEMBED_CACHE_DIR", str(cache))
    outside = tmp_path / "outside-model"
    outside.mkdir()
    selected = cache / "models--qdrant--multilingual-e5-large-onnx"
    try:
        selected.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")
    embedder.clear_model_cache("intfloat/multilingual-e5-large")
    assert selected.is_symlink() and outside.is_dir()


def test_explicit_model_path_is_never_deleted_after_failed_initialization(model_cache, monkeypatch):
    """Even a proven-corrupt explicit directory remains caller-owned."""
    from src.engine.onnx_embedding import InvalidModelCache
    directory = Path(embedder.FASTEMBED_CACHE_DIR) / "models--qdrant--multilingual-e5-large-onnx"
    directory.mkdir()
    marker = directory / "tokenizer_config.json"
    marker.write_text("{")
    monkeypatch.setenv("AGENTS_MODEL_PATH", str(directory))
    monkeypatch.setattr(embedder, "_MAX_LOAD_RETRIES", 2)
    class BrokenTextEmbedding:
        def __init__(self, **options):
            assert options["specific_model_path"] == str(directory)
            assert options["local_files_only"] is True
            raise InvalidModelCache(directory, "caller-owned invalid tokenizer")
    monkeypatch.setattr("src.engine.onnx_embedding.TextEmbedding", BrokenTextEmbedding)
    with pytest.raises(InvalidModelCache, match="caller-owned invalid tokenizer"):
        embedder._get_model()
    assert marker.read_text() == "{"
    assert embedder._model is None and embedder._model_fingerprint is None


@pytest.mark.parametrize("failure", [MemoryError, RuntimeError, ValueError, OSError])
@pytest.mark.parametrize("attempts", [1, 2])
def test_temporary_load_failure_preserves_offline_snapshots_archive_and_plain_copy(model_cache, monkeypatch, failure, attempts):
    """A failed execution attempt must not erase any usable recovery input."""
    cache = Path(embedder.FASTEMBED_CACHE_DIR)
    snapshot = cache / "models--qdrant--multilingual-e5-large-onnx" / "snapshots" / "cached"
    archive = cache / "fast-multilingual-e5-large"
    copy = cache / "local" / "pinned-copy"
    for path in (snapshot, archive, copy):
        path.mkdir(parents=True)
        (path / "model").write_text("preserved")
    monkeypatch.setattr(embedder, "materialize", lambda *args: str(copy))
    monkeypatch.setattr(embedder, "local_copy", lambda *args: str(copy))
    monkeypatch.setattr(embedder, "_MAX_LOAD_RETRIES", attempts)
    calls = []
    class FailingTextEmbedding:
        def __init__(self, **options):
            calls.append(options["specific_model_path"])
            raise failure("temporary resource or configuration failure")
    monkeypatch.setattr("src.engine.onnx_embedding.TextEmbedding", FailingTextEmbedding)
    with pytest.raises(failure, match="temporary resource"):
        embedder._get_model()
    assert calls == [str(copy)] * attempts
    assert all((path / "model").read_text() == "preserved" for path in (snapshot, archive, copy))
    assert embedder._model is None and embedder._model_fingerprint is None


def test_corrupt_snapshot_recovery_preserves_other_snapshots_archive_and_plain_copy(model_cache, monkeypatch):
    """Proven corruption invalidates only the selected managed model directory."""
    from src.engine.onnx_embedding import InvalidModelCache, TextEmbedding
    cache = Path(embedder.FASTEMBED_CACHE_DIR)
    snapshots = cache / "models--qdrant--multilingual-e5-large-onnx" / "snapshots"
    corrupt, healthy = snapshots / "bad", snapshots / "good"
    archive = cache / "fast-multilingual-e5-large"
    copy = cache / "local" / "pinned-copy"
    for path in (corrupt, healthy, archive, copy):
        path.mkdir(parents=True)
        (path / "model").write_text("preserved")
    monkeypatch.setattr(embedder, "local_copy", lambda *args: str(copy))
    monkeypatch.setattr(embedder, "_MAX_LOAD_RETRIES", 1)
    class CorruptTextEmbedding:
        _spec = TextEmbedding._spec
        def __init__(self, **options):
            raise InvalidModelCache(corrupt, "invalid serialized model")
    monkeypatch.setattr("src.engine.onnx_embedding.TextEmbedding", CorruptTextEmbedding)
    with pytest.raises(InvalidModelCache):
        embedder._get_model()
    assert not corrupt.exists()
    assert all((path / "model").read_text() == "preserved" for path in (healthy, archive, copy))


def test_targeted_cleanup_rejects_intermediate_symlink_to_an_unrelated_cache(model_cache, monkeypatch, tmp_path):
    """A symlink inside the cache root cannot alias another model's directory."""
    cache = tmp_path / "managed"
    cache.mkdir()
    monkeypatch.setattr(embedder, "FASTEMBED_CACHE_DIR", str(cache))
    victim = cache / "unrelated" / "revision"
    victim.mkdir(parents=True)
    link = cache / "local"
    try:
        link.symlink_to(victim.parent, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")
    candidate = link / "revision"
    monkeypatch.setattr(embedder, "local_copy", lambda *args: str(candidate))
    embedder.clear_model_cache("intfloat/multilingual-e5-large", failed_model_dir=candidate)
    assert victim.is_dir() and link.is_symlink()


@pytest.fixture
def hub_blob_snapshot(model_cache):
    """A failed snapshot linking to a managed Hub blob in a temporary cache."""
    repository = Path(embedder.FASTEMBED_CACHE_DIR) / "models--qdrant--multilingual-e5-large-onnx"
    snapshot = repository / "snapshots" / "bad"
    snapshot.mkdir(parents=True)
    blob = repository / "blobs" / "model-content"
    blob.parent.mkdir()
    blob.write_bytes(b"corrupt weights")
    try:
        (snapshot / "model.onnx").symlink_to(blob)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"File symlinks are unavailable: {error}")
    return repository, snapshot, blob


@pytest.mark.parametrize("shared", [False, True])
def test_failed_hub_snapshot_removes_only_unreferenced_blobs(hub_blob_snapshot, shared):
    """Other snapshots retain their links and data while orphan corruption goes."""
    repository, snapshot, blob = hub_blob_snapshot
    healthy = repository / "snapshots" / "good"
    healthy.mkdir()
    other = repository / "blobs" / "other-content"
    other.write_bytes(b"healthy weights")
    (healthy / "model.onnx").symlink_to(blob if shared else other)
    unrelated = repository.parent / "models--unrelated" / "blobs" / "keep"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"unrelated weights")

    embedder.clear_model_cache("intfloat/multilingual-e5-large", failed_model_dir=snapshot)

    assert not snapshot.exists()
    assert blob.exists() == shared
    assert (healthy / "model.onnx").read_bytes() == (b"corrupt weights" if shared else b"healthy weights")
    assert other.read_bytes() == b"healthy weights"
    assert unrelated.read_bytes() == b"unrelated weights"


def test_failed_snapshot_never_deletes_an_outside_blob_target(hub_blob_snapshot):
    """A file link escaping the repository cannot turn cleanup into data loss."""
    repository, snapshot, blob = hub_blob_snapshot
    outside = repository.parent / "caller-owned-weights"
    outside.write_bytes(b"caller-owned")
    link = snapshot / "model.onnx"
    link.unlink()
    link.symlink_to(outside)

    embedder.clear_model_cache("intfloat/multilingual-e5-large", failed_model_dir=snapshot)

    assert not snapshot.exists()
    assert outside.read_bytes() == b"caller-owned"
    assert blob.read_bytes() == b"corrupt weights"


def test_failed_snapshot_preserves_blobs_when_other_snapshot_is_aliased(hub_blob_snapshot, tmp_path):
    """Unknown references behind a directory link prevent blob collection."""
    repository, snapshot, blob = hub_blob_snapshot
    outside = tmp_path / "external-snapshot"
    outside.mkdir()
    (outside / "model.onnx").symlink_to(blob)
    alias = repository / "snapshots" / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")

    embedder.clear_model_cache("intfloat/multilingual-e5-large", failed_model_dir=snapshot)

    assert not snapshot.exists()
    assert blob.read_bytes() == b"corrupt weights"
    assert alias.is_symlink() and (outside / "model.onnx").is_file()


def test_failed_snapshot_preserves_blobs_when_reference_scan_fails(hub_blob_snapshot, monkeypatch):
    """An unreadable snapshot tree is unknown, not proof of no references."""
    repository, snapshot, blob = hub_blob_snapshot
    walk = os.walk
    def unreadable(directory, *args, **kwargs):
        if Path(directory) == repository / "snapshots":
            raise PermissionError("snapshot tree is unreadable")
        return walk(directory, *args, **kwargs)
    monkeypatch.setattr(os, "walk", unreadable)

    embedder.clear_model_cache("intfloat/multilingual-e5-large", failed_model_dir=snapshot)

    assert not snapshot.exists()
    assert blob.read_bytes() == b"corrupt weights"


def test_failed_snapshot_never_collects_through_an_aliased_blobs_directory(hub_blob_snapshot, tmp_path):
    """Replacing the blobs directory with an alias preserves its external files."""
    repository, snapshot, blob = hub_blob_snapshot
    blobs = repository / "blobs"
    blobs.rename(repository / "preserved-blobs")
    outside = tmp_path / "external-blobs"
    outside.mkdir()
    external = outside / blob.name
    external.write_bytes(b"external weights")
    try:
        blobs.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")

    embedder.clear_model_cache("intfloat/multilingual-e5-large", failed_model_dir=snapshot)

    assert not snapshot.exists()
    assert external.read_bytes() == b"external weights"
    assert (repository / "preserved-blobs" / blob.name).read_bytes() == b"corrupt weights"



def test_hub_blob_cleanup_rescans_after_acquiring_the_publication_lock(hub_blob_snapshot, monkeypatch):
    """A snapshot published at lock handoff keeps its blob-backed file intact."""
    from contextlib import contextmanager
    import huggingface_hub.utils as hub_utils

    repository, snapshot, blob = hub_blob_snapshot
    real_lock = hub_utils.WeakFileLock
    calls = []
    healthy = repository / "snapshots" / "published-at-handoff"

    @contextmanager
    def publication_handoff(lock_path, **options):
        calls.append(Path(lock_path))
        with real_lock(lock_path, **options):
            healthy.mkdir()
            (healthy / "model.onnx").symlink_to(blob)
            yield

    monkeypatch.setattr(hub_utils, "WeakFileLock", publication_handoff)
    embedder.clear_model_cache("intfloat/multilingual-e5-large", failed_model_dir=snapshot)
    assert not snapshot.exists()
    assert calls == [repository.parent / ".locks" / repository.name / (blob.name + ".lock")]
    assert (healthy / "model.onnx").read_bytes() == b"corrupt weights"
    assert blob.read_bytes() == b"corrupt weights"


def test_hub_blob_cleanup_preserves_a_busy_upstream_blob(hub_blob_snapshot):
    """A current Hub publisher wins over optional automatic blob collection."""
    from huggingface_hub.utils import WeakFileLock

    repository, snapshot, blob = hub_blob_snapshot
    lock_path = repository.parent / ".locks" / repository.name / (blob.name + ".lock")
    lock_path.parent.mkdir(parents=True)
    with WeakFileLock(lock_path, timeout=1):
        embedder.clear_model_cache("intfloat/multilingual-e5-large", failed_model_dir=snapshot)
    assert not snapshot.exists()
    assert blob.read_bytes() == b"corrupt weights"


def test_hub_blob_cleanup_never_follows_an_aliased_lock_directory(hub_blob_snapshot):
    """Unknown lock ownership cannot authorize a write through a cache alias."""
    repository, snapshot, blob = hub_blob_snapshot
    outside = repository.parent / "outside-lock-directory"
    outside.mkdir()
    lock_root = repository.parent / ".locks"
    lock_root.mkdir()
    try:
        (lock_root / repository.name).symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")
    embedder.clear_model_cache("intfloat/multilingual-e5-large", failed_model_dir=snapshot)
    assert not snapshot.exists()
    assert blob.read_bytes() == b"corrupt weights"
    assert list(outside.iterdir()) == []



def test_hub_blob_lock_wait_budget_excludes_reference_scan_work(hub_blob_snapshot, monkeypatch):
    """Slow metadata scans cannot consume the budget before a second free lock."""
    from contextlib import contextmanager
    import huggingface_hub.utils as hub_utils

    repository, snapshot, blob = hub_blob_snapshot
    other = repository / "blobs" / "tokenizer-content"
    other.write_bytes(b"{}")
    (snapshot / "tokenizer.json").symlink_to(other)
    clock = [0.0]
    real_scan = embedder._hub_blob_targets
    locks = []

    def slow_scan(*args):
        result = real_scan(*args)
        clock[0] += 1.0
        return result

    @contextmanager
    def available_lock(path, *, timeout):
        assert timeout > 0
        locks.append(Path(path))
        clock[0] += 0.01
        yield

    monkeypatch.setattr(embedder, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(embedder, "_hub_blob_targets", slow_scan)
    monkeypatch.setattr(hub_utils, "WeakFileLock", available_lock)
    embedder.clear_model_cache("intfloat/multilingual-e5-large", failed_model_dir=snapshot)
    assert not snapshot.exists()
    assert len(locks) == 2
    assert not blob.exists()
    assert not other.exists()
