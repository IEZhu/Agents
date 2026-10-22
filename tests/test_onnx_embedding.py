"""Dense ONNX boundary regressions without model downloads or inference."""
import io
from http.client import IncompleteRead
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
import weakref
import zlib

import numpy as np
import pytest
from tokenizers import Tokenizer, models

from src.engine import onnx_embedding as runtime


@pytest.fixture
def model_dir(tmp_path):
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "[PAD]": 1, "hello": 2}, unk_token="[UNK]"))
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"model_max_length": 8, "pad_token": "[PAD]"}))
    return tmp_path


def test_catalog_preserves_all_dense_model_variants():
    catalog = runtime.TextEmbedding.list_supported_models()
    assert len(catalog) == 37
    assert len({m["model"].lower() for m in catalog}) == 37
    assert runtime.TextEmbedding.get_embedding_size("INTFLOAT/MULTILINGUAL-E5-LARGE") == 1024
    assert runtime.TextEmbedding.get_embedding_size("google/embeddinggemma-300m") == 768
    assert runtime.TextEmbedding.get_embedding_size("Qwen/Qwen3-Embedding-0.6B-Q") == 1024
    catalog[0]["sources"]["hf"] = "mutated"
    assert runtime.TextEmbedding.list_supported_models()[0]["sources"]["hf"] != "mutated"
    with pytest.raises(ValueError, match="not supported"):
        runtime.TextEmbedding._spec("missing/model")


@pytest.mark.parametrize("pooling,normalized,expected", [
    (runtime.PoolingType.CLS, False, [[3, 4], [2, 0]]),
    (runtime.PoolingType.MEAN, False, [[4, 2], [4, 0]]),
    (runtime.PoolingType.LAST_TOKEN, False, [[5, 0], [6, 0]]),
    (runtime.PoolingType.CLS, True, [[0.6, 0.8], [1, 0]]),
])
def test_pooling_and_masking_match_model_conventions(pooling, normalized, expected):
    output = np.array([[[3, 4], [5, 0], [999, 999]], [[2, 0], [4, 0], [6, 0]]], dtype=np.float32)
    mask = np.array([[1, 1, 0], [1, 1, 1]], dtype=np.int64)
    np.testing.assert_allclose(runtime._postprocess(output, mask, pooling, normalized), expected)


def test_last_token_pooling_handles_left_padding_and_zero_norm():
    output = np.array([[[999, 999], [3, 4], [0, 0]]], dtype=np.float32)
    mask = np.array([[0, 1, 1]])
    np.testing.assert_array_equal(runtime._postprocess(output, mask, runtime.PoolingType.LAST_TOKEN, True), [[0, 0]])


def test_tokenizer_adds_special_tokens_before_resolving_pad_id(model_dir):
    (model_dir / "special_tokens_map.json").write_text(json.dumps({"additional_special_tokens": ["[EXTRA]"], "pad_token": "[PAD]"}))
    tokenizer = runtime._load_tokenizer(model_dir)
    assert tokenizer.padding["pad_id"] == 1
    assert tokenizer.token_to_id("[EXTRA]") is not None
    assert tokenizer.truncation["max_length"] == 8
    assert tokenizer.padding["length"] is None


@pytest.mark.parametrize("limit", [0, None, True, 10**30])
def test_tokenizer_rejects_unknown_context(model_dir, limit):
    (model_dir / "tokenizer_config.json").write_text(json.dumps({"model_max_length": limit, "pad_token": "[PAD]"}))
    with pytest.raises(ValueError, match="maximum context"):
        runtime._load_tokenizer(model_dir)


@pytest.fixture
def session(monkeypatch, model_dir):
    import onnxruntime
    calls = []

    class Session:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def get_inputs(self):
            return [SimpleNamespace(name=n) for n in ("input_ids", "attention_mask", "token_type_ids")]

        def run(self, names, inputs):
            calls.append((names, inputs))
            batch, length = inputs["input_ids"].shape
            return [np.ones((batch, length, 1024), dtype=np.float32)]

    monkeypatch.setattr(onnxruntime, "InferenceSession", Session)
    monkeypatch.setattr(onnxruntime, "get_available_providers", lambda: ["CPUExecutionProvider"])
    return calls


def test_jina_query_and_passage_use_different_task_ids(session, model_dir):
    model = runtime.TextEmbedding("jinaai/jina-embeddings-v3", specific_model_path=str(model_dir))
    assert list(model.query_embed("hello"))[0].shape == (1024,)
    assert session[-1][1]["task_id"] == 0
    assert session[-1][1]["task_id"].shape == ()
    assert session[-1][1]["task_id"].dtype == np.int64
    assert list(model.passage_embed(["hello"]))[0].shape == (1024,)
    assert session[-1][1]["task_id"] == 1
    assert session[-1][1]["token_type_ids"].dtype == np.int64
    assert not session[-1][1]["token_type_ids"].any()
    assert session[0][1]["providers"] == ["CPUExecutionProvider"]


def test_siglip_keeps_fixed_padding_and_named_output(session, model_dir):
    model = runtime.TextEmbedding("google/siglip2-base-patch16-224", specific_model_path=str(model_dir))
    assert model.tokenizer.padding["length"] == 8
    assert model.output_names == ["pooler_output"]


def test_gemma_uses_the_sentence_embedding_output(session, model_dir):
    model = runtime.TextEmbedding("google/embeddinggemma-300m", specific_model_path=str(model_dir))
    assert model.output_index == 1
    assert model.pooling == runtime.PoolingType.DISABLED and not model.normalization


@pytest.mark.parametrize("name,index,names,normalized", [
    ("Qdrant/clip-ViT-B-32-text", 0, None, False),
    ("google/embeddinggemma-300m", 1, None, False),
    ("google/siglip2-base-patch16-224", 0, ["pooler_output"], True),
])
def test_graph_output_selection_and_normalization(session, model_dir, name, index, names, normalized):
    model = runtime.TextEmbedding(name, specific_model_path=str(model_dir))
    vector = np.arange(1, model.embedding_size + 1, dtype=np.float32)[None, :]
    calls = []
    def run(output_names, inputs):
        calls.append(output_names)
        return [np.full_like(vector, 999), vector] if index == 1 else [vector]
    model._session.run = run
    actual = np.array(list(model.embed(["hello"])))
    expected = vector / np.linalg.norm(vector, axis=1, keepdims=True) if normalized else vector
    np.testing.assert_array_equal(actual, expected)
    assert calls == [names]


def test_dimension_mismatch_and_invalid_batches_are_rejected(session, model_dir):
    model = runtime.TextEmbedding("BAAI/bge-small-en-v1.5", specific_model_path=str(model_dir))
    with pytest.raises(ValueError, match="dimensions"):
        list(model.embed(["hello"]))
    with pytest.raises(ValueError, match="batch_size"):
        list(model.embed(["hello"], batch_size=0))


def test_unavailable_provider_is_rejected(session, model_dir):
    with pytest.raises(ValueError, match="not available"):
        runtime.TextEmbedding(specific_model_path=str(model_dir), providers=["CUDAExecutionProvider"])


def test_discarding_model_releases_its_native_session_without_cyclic_gc(session, model_dir):
    model = runtime.TextEmbedding(specific_model_path=str(model_dir))
    model_ref, session_ref = weakref.ref(model), weakref.ref(model._session)
    del model
    assert model_ref() is None and session_ref() is None


def test_offline_cache_uses_exact_hub_snapshot_without_network(monkeypatch, tmp_path):
    import huggingface_hub
    spec = runtime.TextEmbedding._spec("BAAI/bge-small-en-v1.5")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / spec["model_file"]).write_bytes(b"model")
    calls = []
    def download(**kwargs):
        calls.append(kwargs)
        return str(snapshot)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    assert runtime._model_directory(spec, tmp_path, True) == snapshot
    assert len(calls) == 1 and calls[0]["local_files_only"] is True
    assert calls[0]["repo_id"] == spec["sources"]["hf"]


def test_existing_archive_cache_can_be_used_offline(tmp_path):
    spec = runtime.TextEmbedding._spec("BAAI/bge-base-en")
    target = tmp_path / "fast-bge-base-en"
    target.mkdir()
    (target / spec["model_file"]).write_bytes(b"model")
    assert runtime._archive_model(spec, tmp_path, True) == target


@pytest.mark.parametrize("failure", [
    OSError("503 service unavailable"), EOFError("truncated gzip"),
    zlib.error("corrupt gzip stream"), IncompleteRead(b"partial", 100),
])
def test_online_sources_retry_after_transient_archive_failure(tmp_path, monkeypatch, failure):
    import huggingface_hub
    spec = runtime.TextEmbedding._spec("BAAI/bge-base-en")
    hub_calls, archive_calls, sleeps = [], [], []
    def hub(**kwargs):
        hub_calls.append(kwargs["local_files_only"])
        raise OSError("Hub unavailable")
    def archive(spec, cache, offline):
        archive_calls.append(offline)
        if len(archive_calls) == 1:
            raise failure
        return tmp_path / "complete"
    monkeypatch.setattr(huggingface_hub, "snapshot_download", hub)
    monkeypatch.setattr(runtime, "_archive_model", archive)
    monkeypatch.setattr(runtime.time, "sleep", sleeps.append)
    assert runtime._model_directory(spec, tmp_path, False) == tmp_path / "complete"
    assert hub_calls == [True, False, False]
    assert archive_calls == [False, False]
    assert sleeps == [3]


def test_online_retry_budget_is_bounded_and_keeps_final_cause(tmp_path, monkeypatch):
    import huggingface_hub
    spec = runtime.TextEmbedding._spec("BAAI/bge-base-en")
    hub_calls, archive_calls, sleeps = [], [], []
    def hub(**kwargs):
        hub_calls.append(kwargs["local_files_only"])
        raise OSError("Hub unavailable")
    def archive(*args):
        archive_calls.append(args[-1])
        raise OSError("archive unavailable")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", hub)
    monkeypatch.setattr(runtime, "_archive_model", archive)
    monkeypatch.setattr(runtime.time, "sleep", sleeps.append)
    with pytest.raises(ValueError, match="from any source") as error:
        runtime._model_directory(spec, tmp_path, False)
    assert str(error.value.__cause__) == "archive unavailable"
    assert hub_calls == [True, False, False, False]
    assert archive_calls == [False, False, False]
    assert sleeps == [3, 9]


def test_partial_archive_target_is_replaced_with_complete_model_and_weights(tmp_path, monkeypatch):
    spec = runtime.TextEmbedding._spec("BAAI/bge-base-en")
    spec["additional_files"] = ["model.onnx_data"]
    name = "fast-bge-base-en"
    target = tmp_path / name
    target.mkdir()
    (target / spec["model_file"]).write_bytes(b"old partial model")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for filename, data in [(spec["model_file"], b"new model"), ("model.onnx_data", b"weights")]:
            member = tarfile.TarInfo(name + "/" + filename)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(archive.getvalue()))
    assert runtime._archive_model(spec, tmp_path, False) == target
    assert (target / spec["model_file"]).read_bytes() == b"new model"
    assert (target / "model.onnx_data").read_bytes() == b"weights"
    assert not list((tmp_path / "tmp").iterdir())


def test_archive_publication_failure_restores_partial_cache(tmp_path, monkeypatch):
    spec = runtime.TextEmbedding._spec("BAAI/bge-base-en")
    spec["additional_files"] = ["model.onnx_data"]
    target = tmp_path / "fast-bge-base-en"
    target.mkdir()
    (target / spec["model_file"]).write_bytes(b"original")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for filename in [spec["model_file"], "model.onnx_data"]:
            member = tarfile.TarInfo(target.name + "/" + filename)
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(archive.getvalue()))
    rename = Path.rename
    def fail_publish(path, destination):
        if path.name == target.name and path.parent != tmp_path:
            raise OSError("cannot publish")
        return rename(path, destination)
    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(OSError, match="cannot publish"):
        runtime._archive_model(spec, tmp_path, False)
    assert (target / spec["model_file"]).read_bytes() == b"original"
    assert not list((tmp_path / "tmp").iterdir())


def test_legacy_python_extractall_receives_no_filter_keyword(tmp_path, monkeypatch):
    spec = runtime.TextEmbedding._spec("BAAI/bge-base-en")
    class LegacyTar:
        fileobj = io.BytesIO()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def getmembers(self):
            return []
        def extractall(self, path):  # Early Python 3.11 has no filter keyword.
            target = path / "fast-bge-base-en"
            target.mkdir()
            (target / spec["model_file"]).write_bytes(b"model")
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    monkeypatch.setattr(tarfile, "open", lambda *args, **kwargs: LegacyTar())
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO())
    assert runtime._archive_model(spec, tmp_path, False) == tmp_path / "fast-bge-base-en"


def test_offline_missing_snapshot_cannot_download_an_archive(tmp_path, monkeypatch):
    import huggingface_hub
    calls = []
    def download(**kwargs):
        calls.append(kwargs)
        raise OSError("not cached")
    def forbidden(*args, **kwargs):
        pytest.fail("offline mode attempted a network request")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    monkeypatch.setattr(runtime.urllib.request, "urlopen", forbidden)
    with pytest.raises(ValueError, match="Could not find model"):
        runtime._model_directory(runtime.TextEmbedding._spec("BAAI/bge-base-en"), tmp_path, True)
    assert len(calls) == 1 and calls[0]["local_files_only"] is True


def test_malformed_cached_tokenizer_fails_clearly(session, model_dir):
    (model_dir / "tokenizer_config.json").write_text("{")
    with pytest.raises(json.JSONDecodeError):
        runtime.TextEmbedding(specific_model_path=str(model_dir), local_files_only=True)


@pytest.mark.parametrize("member_name,is_link", [("../escape", False), ("C:/escape", False), ("link", True)])
def test_archive_download_rejects_escapes_and_links(tmp_path, monkeypatch, member_name, is_link):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        member = tarfile.TarInfo(member_name)
        if is_link:
            member.type = tarfile.SYMTYPE
            member.linkname = "../escape"
        tar.addfile(member)
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(archive.getvalue()))
    with pytest.raises(ValueError, match="Unsafe"):
        runtime._archive_model(runtime.TextEmbedding._spec("BAAI/bge-base-en"), tmp_path, False)
    assert not (tmp_path.parent / "escape").exists()
    assert not list((tmp_path / "tmp").iterdir())
