"""Model prompt templates reach the embedding model and the index fingerprint; no model is loaded."""
from __future__ import annotations

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
    embedding_prompts.register_custom("microsoft/harrier-oss-v1-270m")
    assert len(calls) == 1
    call = calls[0]
    # The export already ends in a pooled sentence_embedding output.
    assert call["pooling"] == PoolingType.DISABLED and call["normalization"] is True
    assert call["dim"] == 640 and call["sources"].hf == "onnx-community/harrier-oss-v1-270m-ONNX"
    assert call["additional_files"] == ["onnx/model.onnx_data"]


@pytest.mark.parametrize("model", sorted(embedding_prompts.CUSTOM_MODELS))
def test_custom_models_have_prompts(model):
    assert embedding_prompts.templates(model) != embedding_prompts.PLAIN


def test_exports_with_weight_files_load_from_a_plain_copy(monkeypatch, tmp_path):
    import json

    import huggingface_hub

    calls = []

    def fake_download(repo, local_dir, allow_patterns):
        calls.append((repo, allow_patterns))
        (tmp_path / "local").mkdir(exist_ok=True)
        import os
        os.makedirs(local_dir, exist_ok=True)
        with open(os.path.join(local_dir, "tokenizer_config.json"), "w") as stream:
            json.dump({"model_max_length": 1e30}, stream)
        return local_dir

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_download)
    assert embedding_prompts.materialize("intfloat/multilingual-e5-large", str(tmp_path)) is None
    target = embedding_prompts.materialize("google/embeddinggemma-300m", str(tmp_path))
    assert calls == [("onnx-community/embeddinggemma-300m-ONNX",
                      ["onnx/model.onnx", "onnx/model.onnx_data", "*.json", "tokenizer*"])]
    with open(f"{target}/tokenizer_config.json") as stream:
        assert json.load(stream)["model_max_length"] == embedding_prompts.MAX_INPUT_TOKENS


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


def test_an_earlier_complete_copy_is_used_without_the_hub(monkeypatch, tmp_path):
    import json
    import os

    import huggingface_hub

    def offline(*args, **kwargs):
        raise ConnectionError("no network")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", offline)
    with pytest.raises(ConnectionError):  # nothing downloaded yet
        embedding_prompts.materialize("microsoft/harrier-oss-v1-270m", str(tmp_path))
    target = tmp_path / "local" / "microsoft--harrier-oss-v1-270m"
    (target / "onnx").mkdir(parents=True)
    for name in ("onnx/model.onnx", "onnx/model.onnx_data", "tokenizer.json"):
        (target / name).write_text("x")
    (target / "tokenizer_config.json").write_text(json.dumps({"model_max_length": 2048}))
    assert embedding_prompts.materialize("microsoft/harrier-oss-v1-270m", str(tmp_path)) == os.fspath(target)
