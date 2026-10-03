"""Model-specific input prompts and extra model registrations for the embedder.

fastembed's `query_embed` and `passage_embed` embed the text as given for these
models, although their model cards require a prompt: e5 expects `query: ` and
`passage: `, and instruction-tuned models expect a task instruction on queries
only. The embedder applies the templates below before embedding. They are part of
the index fingerprint (`src/engine/fingerprint.py`), so changing one re-embeds the
skill and implant stores and invalidates the router cache.

Models fastembed does not ship are registered from their ONNX exports before the
first load. Exports with separate weight files load from a plain-file copy (see
`materialize`).
"""

TASK = "Given a request to an AI assistant, retrieve the guidance that helps answer it"

_E5 = ("query: {text}", "passage: {text}")
_QWEN3 = (f"Instruct: {TASK}\nQuery:{{text}}", "{text}")
_HARRIER = (f"Instruct: {TASK}\nQuery: {{text}}", "{text}")
PLAIN = ("{text}", "{text}")

PROMPTS: dict[str, tuple[str, str]] = {
    "intfloat/multilingual-e5-large": _E5,
    "intfloat/multilingual-e5-base": _E5,
    "intfloat/multilingual-e5-small": _E5,
    "Qwen/Qwen3-Embedding-0.6B": _QWEN3,
    "Qwen/Qwen3-Embedding-0.6B-Q": _QWEN3,
    "microsoft/harrier-oss-v1-270m": _HARRIER,
    "microsoft/harrier-oss-v1-0.6b": _HARRIER,
    "google/embeddinggemma-300m": ("task: search result | query: {text}", "title: none | text: {text}"),
}

# onnx-community exports of decoder-only models. Their ONNX graph already ends in the
# pooled `sentence_embedding` output, so fastembed must not pool again.
CUSTOM_MODELS: dict[str, dict] = {
    "microsoft/harrier-oss-v1-270m": {"hf": "onnx-community/harrier-oss-v1-270m-ONNX", "dim": 640,
                                      "revision": "d59c919d0159aea2c19ed7d04288fcdd048d0f9c",
                                      "files": ["onnx/model.onnx_data"], "size_in_gb": 1.1},
    "microsoft/harrier-oss-v1-0.6b": {"hf": "onnx-community/harrier-oss-v1-0.6b-ONNX", "dim": 1024,
                                      "revision": "e4daffa011e666dcd2ff2a3c6c05084090ac314d",
                                      "files": ["onnx/model.onnx_data", "onnx/model.onnx_data_1"], "size_in_gb": 2.4},
}


# Exports whose weights sit in a separate .onnx_data file. ONNX Runtime 1.30 refuses
# such weights when they resolve into another Hugging Face blob directory, so these
# load from a plain-file copy, built-in fastembed models included. Each copy is pinned
# to an export revision (the ones docs/embedding-models-eval-results.md measured); the
# pin is part of the index fingerprint, so changing it re-embeds the stores.
LOCAL_COPIES: dict[str, dict] = {
    **{model: {key: spec[key] for key in ("hf", "revision", "files")} for model, spec in CUSTOM_MODELS.items()},
    "google/embeddinggemma-300m": {"hf": "onnx-community/embeddinggemma-300m-ONNX",
                                   "revision": "5090578d9565bb06545b4552f76e6bc2c93e4a66",
                                   "files": ["onnx/model.onnx_data"]},
    "Qwen/Qwen3-Embedding-0.6B": {"hf": "Qdrant/Qwen3-Embedding-0.6B-onnx",
                                  "revision": "af95f2c416ffe9379369ad64f9113e865db6112c",
                                  "files": ["onnx/model.onnx_data"]},
}
# Written into a copy last, before it is published: a copy without it is not used.
COMPLETE = ".complete"


def _entry(table: dict, model: str) -> tuple[str, object] | None:
    """`table`'s (name, value) for `model`, ignoring case as fastembed does when it resolves names."""
    return next(((name, value) for name, value in table.items() if name.lower() == model.lower()), None)


def templates(model: str) -> tuple[str, str]:
    """(query template, passage template) for `model`; plain text for unknown models.

    EMBEDDING_PROMPTS=off embeds every model's text as given, as before these
    templates existed: a baseline for evals and a fallback.
    """
    import os

    if os.environ.get("EMBEDDING_PROMPTS", "on").strip().lower() == "off":
        return PLAIN
    entry = _entry(PROMPTS, model)
    return entry[1] if entry else PLAIN


def as_query(model: str, text: str) -> str:
    return templates(model)[0].format(text=text)


def as_passage(model: str, text: str) -> str:
    return templates(model)[1].format(text=text)


def pinned_revision(model: str) -> str | None:
    """`<export>@<revision>` of a LOCAL_COPIES model, which its plain-file copy holds exactly."""
    entry = _entry(LOCAL_COPIES, model)
    return f"{entry[1]['hf']}@{entry[1]['revision']}" if entry else None


def local_copy(model: str, cache_dir: str) -> str | None:
    """Directory of the plain-file copy of a LOCAL_COPIES export; None for other models."""
    entry = _entry(LOCAL_COPIES, model)
    if entry is None:
        return None
    import os

    name, spec = entry
    return os.path.join(cache_dir, "local", name.replace("/", "--"), spec["revision"])


def materialize(model: str, cache_dir: str) -> str | None:
    """A plain-file copy of a LOCAL_COPIES export, for fastembed's specific_model_path.

    The Hugging Face cache keeps each file as a symlink into its own blob directory,
    and ONNX Runtime refuses external weights outside the model file's directory
    ("External data path escapes model directory"). A local_dir download holds real
    files side by side. The pinned revision downloads into a private staging
    directory that is renamed into place only when complete, so a published copy
    never mixes revisions or writers, and a failed download leaves earlier copies as
    they were. A published copy loads without the Hub (offline, HF_HUB_OFFLINE).
    Returns None for other models.
    """
    target = local_copy(model, cache_dir)
    if target is None:
        return None
    import os
    import shutil
    import tempfile

    if os.path.isfile(os.path.join(target, COMPLETE)):
        return target
    from huggingface_hub import snapshot_download

    spec = _entry(LOCAL_COPIES, model)[1]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".partial-", dir=os.path.dirname(target))
    try:
        snapshot_download(spec["hf"], revision=spec["revision"], local_dir=staging,
                          allow_patterns=["onnx/model.onnx", *spec["files"], "*.json", "tokenizer*"])
        _plain_files(staging)
        _cap_max_length(os.path.join(staging, "tokenizer_config.json"), MAX_INPUT_TOKENS)
        with open(os.path.join(staging, COMPLETE), "w", encoding="utf-8") as stream:
            stream.write(spec["revision"])
        os.rename(staging, target)
    except Exception:
        # Another process may have published this revision meanwhile: its copy then
        # serves, whether this download failed or lost the rename.
        if not os.path.isfile(os.path.join(target, COMPLETE)):
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


# Input length and document batch size bound the embedder's memory. Attention memory
# grows with batch × length², and fastembed's default batch of 256 padded to the
# longest skill text (about 2.8k tokens) exhausted a 36 GB laptop with a 270M model
# on 2026-10-03. 2048 tokens covers 121 of the 127 skill and implant files whole.
MAX_INPUT_TOKENS = 2048
BATCH_SIZE = 4


def batch_size() -> int:
    """Documents per embedding batch; EMBEDDING_BATCH_SIZE overrides it."""
    import os

    return max(1, int(os.environ.get("EMBEDDING_BATCH_SIZE", BATCH_SIZE)))


def cap_tokens(text_embedding, limit: int = MAX_INPUT_TOKENS) -> None:
    """Truncate inputs at `limit` tokens unless the model's own limit is lower.

    Covers models whose tokenizer allows far more, such as Qwen3-Embedding's 32768.
    """
    tokenizer = getattr(getattr(text_embedding, "model", None), "tokenizer", None)
    if tokenizer is None:
        return
    current = (tokenizer.truncation or {}).get("max_length")
    if current is None or current > limit:
        tokenizer.enable_truncation(max_length=limit)


def _plain_files(root: str) -> None:
    """Replace symlinks under `root` with copies of their targets.

    huggingface_hub before 0.23 links large files of a local_dir download into its
    blob cache, where ONNX Runtime refuses external weights.
    """
    import os
    import shutil

    for folder, _subfolders, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            if os.path.islink(path):
                target = os.path.realpath(path)
                os.unlink(path)
                shutil.copyfile(target, path)


def _cap_max_length(path: str, limit: int) -> None:
    """Cap the tokenizer limit in a copy that is not yet published, so no other process reads it."""
    import json

    with open(path, encoding="utf-8") as stream:
        config = json.load(stream)
    if 0 < int(config.get("model_max_length") or 0) <= limit:
        return
    config["model_max_length"] = limit
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=1)


def register_custom(model: str) -> None:
    """Register a model from CUSTOM_MODELS with fastembed once; other models are left alone.

    The model registers under its CUSTOM_MODELS name; fastembed finds it by any case.
    """
    entry = _entry(CUSTOM_MODELS, model)
    if entry is None:
        return
    model, spec = entry
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    if any(listed["model"].lower() == model.lower() for listed in TextEmbedding.list_supported_models()):
        return
    TextEmbedding.add_custom_model(
        model=model, pooling=PoolingType.DISABLED, normalization=True,
        sources=ModelSource(hf=spec["hf"]), dim=spec["dim"], model_file="onnx/model.onnx",
        additional_files=spec["files"], license="mit", size_in_gb=spec["size_in_gb"],
        description="Multilingual text embeddings, pooled inside the ONNX graph, 32768 input tokens; "
                    "queries need a task instruction.")
