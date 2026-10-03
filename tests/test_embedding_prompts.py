"""Model prompt templates reach the embedding model and the index fingerprint; no model is loaded."""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from src.engine import embedder, embedding_prompts, fingerprint


class _FakeModel:
    def __init__(self):
        self.queries, self.passages, self.kwargs = [], [], {}

    def query_embed(self, texts):
        self.queries += list(texts)
        return [np.zeros(3) for _ in texts]

    def passage_embed(self, texts, **kwargs):
        self.passages += list(texts)
        self.kwargs = kwargs
        return [np.zeros(3) for _ in texts]


def test_templates_follow_the_model_cards():
    assert embedding_prompts.as_query("intfloat/multilingual-e5-large", "q") == "query: q"
    assert embedding_prompts.as_passage("intfloat/multilingual-e5-large", "d") == "passage: d"
    assert embedding_prompts.as_query("Qwen/Qwen3-Embedding-0.6B-Q", "q").endswith("\nQuery:q")
    assert embedding_prompts.as_query("microsoft/harrier-oss-v1-270m", "q").startswith("Instruct: ")
    assert embedding_prompts.as_passage("microsoft/harrier-oss-v1-270m", "d") == "d"
    assert embedding_prompts.as_passage("google/embeddinggemma-300m", "d") == "title: none | text: d"
    assert embedding_prompts.templates("some/unknown-model") == embedding_prompts.PLAIN


def test_model_names_match_in_any_case_as_fastembed_resolves_them(tmp_path):
    assert embedding_prompts.as_query("INTFLOAT/Multilingual-E5-Large", "q") == "query: q"
    assert embedding_prompts.as_passage("INTFLOAT/Multilingual-E5-Large", "d") == "passage: d"
    mixed, canonical = "Microsoft/Harrier-OSS-v1-270m", "microsoft/harrier-oss-v1-270m"
    assert embedding_prompts.local_copy(mixed, str(tmp_path)) == embedding_prompts.local_copy(canonical, str(tmp_path))
    assert embedding_prompts.pinned_revision(mixed) == embedding_prompts.pinned_revision(canonical) is not None


def test_prompts_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROMPTS", "off")
    assert embedding_prompts.as_query("intfloat/multilingual-e5-large", "q") == "q"


def test_embedder_applies_the_templates(monkeypatch):
    fake = _FakeModel()
    monkeypatch.setattr(embedder, "_get_model", lambda: fake)
    monkeypatch.setattr(embedder, "EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
    embedder._embed_query("как вернуть налог")
    embedder._embed_texts(["skill text"])
    assert fake.queries == ["query: как вернуть налог"]
    assert fake.passages == ["passage: skill text"]
    assert fake.kwargs == {"batch_size": embedding_prompts.BATCH_SIZE}


def test_a_template_change_changes_the_index_fingerprint(monkeypatch):
    model = "intfloat/multilingual-e5-large"
    fingerprint.fingerprint.cache_clear()
    before = fingerprint.fingerprint(model)
    monkeypatch.setitem(embedding_prompts.PROMPTS, model, ("q {text}", "p {text}"))
    fingerprint.fingerprint.cache_clear()
    try:
        assert fingerprint.fingerprint(model) != before
    finally:
        fingerprint.fingerprint.cache_clear()


def test_custom_models_register_without_extra_pooling(monkeypatch):
    from fastembed import TextEmbedding
    from fastembed.common.model_description import PoolingType

    calls = []
    monkeypatch.setattr(TextEmbedding, "list_supported_models", classmethod(lambda cls: []))
    monkeypatch.setattr(TextEmbedding, "add_custom_model", classmethod(lambda cls, **kw: calls.append(kw)))
    embedding_prompts.register_custom("intfloat/multilingual-e5-large")
    assert calls == []
    embedding_prompts.register_custom("Microsoft/Harrier-OSS-v1-270m")
    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == "microsoft/harrier-oss-v1-270m"  # the CUSTOM_MODELS name, whatever the case asked
    # The export already ends in a pooled sentence_embedding output.
    assert call["pooling"] == PoolingType.DISABLED and call["normalization"] is True
    assert call["dim"] == 640 and call["sources"].hf == "onnx-community/harrier-oss-v1-270m-ONNX"
    assert call["additional_files"] == ["onnx/model.onnx_data"]


@pytest.mark.parametrize("model", sorted(embedding_prompts.CUSTOM_MODELS))
def test_custom_models_have_prompts(model):
    assert embedding_prompts.templates(model) != embedding_prompts.PLAIN


def _hub(calls, fail=False):
    """A snapshot_download stand-in that writes a small export into local_dir."""

    def download(repo, revision, local_dir, allow_patterns):
        calls.append((repo, revision, allow_patterns))
        os.makedirs(os.path.join(local_dir, "onnx"), exist_ok=True)
        with open(os.path.join(local_dir, "onnx", "model.onnx"), "w") as stream:
            stream.write("graph")
        if fail:
            raise ConnectionError("download interrupted")
        with open(os.path.join(local_dir, "tokenizer_config.json"), "w") as stream:
            json.dump({"model_max_length": 1e30}, stream)
        return local_dir

    return download


def _offline(*args, **kwargs):
    raise ConnectionError("no network")


def test_exports_with_weight_files_load_from_a_pinned_plain_copy(monkeypatch, tmp_path):
    import huggingface_hub

    calls = []
    monkeypatch.setattr(huggingface_hub, "snapshot_download", _hub(calls))
    assert embedding_prompts.materialize("intfloat/multilingual-e5-large", str(tmp_path)) is None
    target = embedding_prompts.materialize("google/embeddinggemma-300m", str(tmp_path))
    revision = embedding_prompts.LOCAL_COPIES["google/embeddinggemma-300m"]["revision"]
    assert calls == [("onnx-community/embeddinggemma-300m-ONNX", revision,
                      ["onnx/model.onnx", "onnx/model.onnx_data", "*.json", "tokenizer*"])]
    assert target == os.fspath(tmp_path / "local" / "google--embeddinggemma-300m" / revision)
    assert os.listdir(os.path.dirname(target)) == [revision]  # no staging directory left
    with open(f"{target}/tokenizer_config.json") as stream:
        assert json.load(stream)["model_max_length"] == embedding_prompts.MAX_INPUT_TOKENS

    # A published copy loads without the Hub.
    monkeypatch.setattr(huggingface_hub, "snapshot_download", _offline)
    assert embedding_prompts.materialize("google/embeddinggemma-300m", str(tmp_path)) == target


def test_linked_files_of_a_download_are_published_as_plain_files(monkeypatch, tmp_path):
    import huggingface_hub

    blob = tmp_path / "blob"
    blob.write_text("weights")
    download = _hub([])

    def linking(repo, revision, local_dir, allow_patterns):
        # huggingface_hub before 0.23 links large local_dir files into its blob cache.
        download(repo, revision, local_dir, allow_patterns)
        os.symlink(blob, os.path.join(local_dir, "onnx", "model.onnx_data"))
        return local_dir

    monkeypatch.setattr(huggingface_hub, "snapshot_download", linking)
    target = embedding_prompts.materialize("microsoft/harrier-oss-v1-270m", str(tmp_path / "cache"))
    weights = os.path.join(target, "onnx", "model.onnx_data")
    assert not os.path.islink(weights)
    with open(weights) as stream:
        assert stream.read() == "weights"


def test_a_failed_download_publishes_nothing(monkeypatch, tmp_path):
    import huggingface_hub

    model = "microsoft/harrier-oss-v1-270m"
    monkeypatch.setattr(huggingface_hub, "snapshot_download", _hub([], fail=True))
    with pytest.raises(ConnectionError):
        embedding_prompts.materialize(model, str(tmp_path))
    assert os.listdir(os.path.dirname(embedding_prompts.local_copy(model, str(tmp_path)))) == []


@pytest.mark.parametrize("download_fails", [False, True])
def test_a_copy_another_process_published_first_is_used(monkeypatch, tmp_path, download_fails):
    import huggingface_hub

    model = "microsoft/harrier-oss-v1-270m"
    target = embedding_prompts.local_copy(model, str(tmp_path))
    download = _hub([], fail=download_fails)

    def racing(repo, revision, local_dir, allow_patterns):
        # Another process publishes the same revision while this download runs.
        os.makedirs(target)
        with open(os.path.join(target, embedding_prompts.COMPLETE), "w") as stream:
            stream.write(revision)
        return download(repo, revision, local_dir, allow_patterns)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", racing)
    assert embedding_prompts.materialize(model, str(tmp_path)) == target
    assert os.listdir(os.path.dirname(target)) == [os.path.basename(target)]
    assert not os.path.exists(os.path.join(target, "onnx"))  # the other process's copy stays as it was


def test_a_failed_load_clears_the_plain_copy(monkeypatch, tmp_path):
    model = "microsoft/harrier-oss-v1-270m"
    target = embedding_prompts.local_copy(model, str(tmp_path))
    os.makedirs(target)
    monkeypatch.setattr(embedder, "FASTEMBED_CACHE_DIR", str(tmp_path))
    embedder.clear_model_cache(model)
    assert not os.path.exists(target)


def test_the_pinned_revision_is_part_of_the_index_fingerprint(monkeypatch):
    model = "microsoft/harrier-oss-v1-270m"
    monkeypatch.delenv("AGENTS_MODEL_ARTIFACT", raising=False)
    fingerprint.fingerprint.cache_clear()
    try:
        before = fingerprint.fingerprint(model)
        monkeypatch.setitem(embedding_prompts.LOCAL_COPIES, model,
                            {**embedding_prompts.LOCAL_COPIES[model], "revision": "0" * 40})
        fingerprint.fingerprint.cache_clear()
        assert fingerprint.fingerprint(model) != before
    finally:
        fingerprint.fingerprint.cache_clear()


class _Tokenizer:
    def __init__(self, max_length):
        self.truncation = None if max_length is None else {"max_length": max_length}

    def enable_truncation(self, max_length):
        self.truncation = {"max_length": max_length}


@pytest.mark.parametrize("own, expected", [(32768, 2048), (None, 2048), (512, 512)])
def test_inputs_are_capped_unless_the_model_limit_is_lower(own, expected):
    class Wrapped:
        model = type("Inner", (), {"tokenizer": _Tokenizer(own)})()

    embedding_prompts.cap_tokens(Wrapped, 2048)
    assert Wrapped.model.tokenizer.truncation["max_length"] == expected
