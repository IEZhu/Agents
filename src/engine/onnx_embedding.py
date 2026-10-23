"""Dense text embeddings using maintained ONNX Runtime, Tokenizers and Hub APIs.

The catalog and numerical conventions retain FastEmbed 0.8.1's 37 dense models.
Only the text inference boundary used by Agents-Core lives here; image handling
belongs to OCR's independently patched Pillow dependency. See THIRD_PARTY_NOTICES.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import Enum
from importlib.resources import files
from http.client import HTTPException
from itertools import islice
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import urllib.request
import zlib

import numpy as np


class PoolingType(str, Enum):
    CLS = "CLS"
    MEAN = "MEAN"
    LAST_TOKEN = "LAST_TOKEN"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class ModelSource:
    hf: str | None = None
    url: str | None = None
    _deprecated_tar_struct: bool = False

    def __post_init__(self):
        if not (self.hf or self.url):
            raise ValueError("At least one model source is required")


_CATALOG = json.loads(files("src.engine").joinpath("embedding_catalog.json").read_text(encoding="utf-8"))
# A change to numerical conventions must bump this and invalidate derived indexes.
EMBEDDING_CONTRACT = "onnx-dense-v1"


class InvalidModelCache(ValueError):
    """A positively identified artifact fault, distinct from execution failure."""

    def __init__(self, model_dir, detail):
        """Retain the affected directory for narrowly scoped recovery."""
        self.model_dir = Path(model_dir)
        super().__init__(f"{detail} in {self.model_dir}")


def _json(path, default=None):
    """Read model metadata, classifying malformed UTF-8, JSON or object structure."""
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidModelCache(path.parent, f"Malformed {path.name}") from error
    if not isinstance(value, dict):
        raise InvalidModelCache(path.parent, f"Expected a JSON object in {path.name}")
    return value


def _load_tokenizer(model_dir):
    """Keep tokenizer artifact faults distinct from native or resource failures."""
    from tokenizers import AddedToken, Tokenizer

    config = _json(model_dir / "config.json", {})
    tokenizer_config = _json(model_dir / "tokenizer_config.json")
    if tokenizer_config is None:
        raise InvalidModelCache(model_dir, "Could not find tokenizer_config.json")
    limits = [v for v in (tokenizer_config.get("model_max_length"), tokenizer_config.get("max_length"))
              if type(v) is int and v > 0]
    if not limits:
        raise InvalidModelCache(model_dir, "Could not determine the maximum context length")
    tokenizer_path = model_dir / "tokenizer.json"
    if not tokenizer_path.is_file():
        raise InvalidModelCache(model_dir, "Could not find tokenizer.json")
    try:
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
    except (OSError, MemoryError):
        raise
    except Exception:
        # Rust errors have a generic Python type. Probe JSON only on failure,
        # without a second vocabulary parse on the successful loading path.
        _json(tokenizer_path)
        raise
    # Huge positive limits are the tokenizer's unbounded sentinel. Leave those
    # to cap_tokens rather than passing an overflowing length to Tokenizers.
    if min(limits) <= sys.maxsize:
        tokenizer.enable_truncation(max_length=min(limits))
    for value in _json(model_dir / "special_tokens_map.json", {}).values():
        for token in value if isinstance(value, list) else [value]:
            tokenizer.add_special_tokens([AddedToken(**token) if isinstance(token, dict) else token])
    padding = tokenizer.padding or {}
    pad_token = padding.get("pad_token") or tokenizer_config.get("pad_token")
    if pad_token is None:
        raise InvalidModelCache(model_dir, "Could not find a pad token")
    pad_id = padding.get("pad_id", config.get("pad_token_id"))
    if pad_id is None:
        pad_id = tokenizer.token_to_id(pad_token)
    if pad_id is None:
        raise InvalidModelCache(model_dir, f"Could not resolve an id for pad token {pad_token!r}")
    tokenizer.enable_padding(direction=padding.get("direction", "right"), pad_id=pad_id,
                             pad_type_id=padding.get("pad_type_id", 0), pad_token=pad_token,
                             pad_to_multiple_of=padding.get("pad_to_multiple_of"), length=None)
    return tokenizer


def _model_files_complete(path, spec):
    """Require weights and the tokenizer files that initialization always opens."""
    required = [spec["model_file"], *spec["additional_files"], "tokenizer.json", "tokenizer_config.json"]
    return all((path / name).is_file() for name in required)


def _archive_model(spec, cache_dir, offline):
    """Retain the official GCS fallback and its existing cache directory names."""
    source = spec["sources"]
    name = ("fast-" if source.get("_deprecated_tar_struct") else "") + spec["model"].split("/")[-1]
    target = cache_dir / name
    def complete(path):
        return _model_files_complete(path, spec)

    if complete(target):
        return target
    if offline or not source.get("url"):
        raise ValueError(f"Could not find model {spec['model']} in cache {cache_dir}")
    staging_root = cache_dir / "tmp"
    if staging_root.is_symlink():
        raise ValueError("Refusing to stage model downloads through a symlink")
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=name + "-", dir=staging_root))
    try:
        archive = staging / "model.tar.gz"
        with urllib.request.urlopen(source["url"], timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with tarfile.open(archive, "r:gz") as tar:
            # Model exports contain only files/directories. Reject links and portable
            # path escapes before extraction, including on Python without data_filter.
            for member in tar.getmembers():
                path = PureWindowsPath(member.name)
                if path.drive or path.root or ".." in path.parts or not (member.isfile() or member.isdir()):
                    raise ValueError(f"Unsafe model archive member: {member.name}")
            if hasattr(tarfile, "data_filter"):
                tar.extractall(staging, filter="data")
            else:
                tar.extractall(staging)
            while tar.fileobj.read(1 << 20):
                pass  # Verify the gzip trailer as well as tar headers.
        extracted = staging / name
        if not complete(extracted):
            raise ValueError("Downloaded model archive is incomplete")
        from src.file_lock import file_lock

        # A competing publisher may have completed the target while downloading.
        # Serialize only publication, and replace a partial cache as a directory:
        # model and external weights must never be accepted from separate attempts.
        with file_lock(cache_dir / ".locks" / (name + ".lock")):
            if complete(target):
                return target
            previous = staging / "incomplete-cache"
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_dir():
                    raise ValueError(f"Refusing to replace a non-directory model cache: {target}")
                target.rename(previous)
            try:
                extracted.rename(target)
            except OSError:
                # Keep an existing cache recoverable if publication itself fails.
                if previous.exists() and not target.exists():
                    previous.rename(target)
                raise
        return target
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _model_directory(spec, cache_dir, offline):
    from httpx import TransportError
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import HfHubHTTPError

    source = spec["sources"]
    patterns = ["config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
                spec["model_file"], *spec["additional_files"]]
    def hub(local_only):
        path = Path(snapshot_download(repo_id=source["hf"], cache_dir=str(cache_dir),
                                      allow_patterns=patterns, local_files_only=local_only))
        if not _model_files_complete(path, spec):
            raise ValueError("Downloaded Hub snapshot is incomplete")
        return path

    if source.get("hf"):
        try:
            return hub(True)
        except (OSError, ValueError, HfHubHTTPError, TransportError):
            pass
    if offline:
        return _archive_model(spec, cache_dir, True)

    # Keep the previous runtime's three source rounds, so a transient outage
    # does not prevent startup. Offline cache lookup above never retries online.
    last_error = None
    for attempt in range(3):
        if source.get("hf"):
            try:
                return hub(False)
            except (OSError, ValueError, HfHubHTTPError, TransportError) as error:
                last_error = error
        if source.get("url"):
            try:
                return _archive_model(spec, cache_dir, False)
            except (OSError, ValueError, tarfile.TarError, EOFError, zlib.error, HTTPException) as error:
                last_error = error
        if attempt < 2:
            time.sleep(3 * 3**attempt)
    raise ValueError(f"Could not load model {spec['model']} from any source") from last_error


def _postprocess(output, mask, pooling, normalization):
    """Use the same dtype promotion, masking and norm floor as the old runtime."""
    if pooling == PoolingType.CLS:
        output = output[:, 0] if output.ndim == 3 else output
    elif pooling == PoolingType.MEAN:
        expanded = np.tile(np.expand_dims(mask, -1).astype(np.int64), (1, 1, output.shape[-1]))
        output = np.sum(output * expanded, axis=1) / np.maximum(np.sum(expanded, axis=1), 1e-9)
    elif pooling == PoolingType.LAST_TOKEN:
        indices = mask.shape[1] - 1 - np.argmax(mask[:, ::-1], axis=1)
        output = output[np.arange(output.shape[0]), indices]
    if output.ndim != 2:
        raise ValueError(f"Unsupported embedding shape: {output.shape}")
    if normalization:
        output = output / np.maximum(np.linalg.norm(output, ord=2, axis=1, keepdims=True), 1e-12)
    return output


class TextEmbedding:
    """Agents' dense embedding boundary, with the previous catalog and model APIs."""

    _custom_models = []

    @classmethod
    def list_supported_models(cls):
        return [{k: deepcopy(v) for k, v in s.items() if k not in {"runtime_class", "pooling", "normalization"}}
                for s in [*_CATALOG, *cls._custom_models]]

    @classmethod
    def add_custom_model(cls, model, pooling, normalization, sources, dim, model_file="onnx/model.onnx",
                         description="", license="", size_in_gb=0.0, additional_files=None):
        if any(s["model"].lower() == model.lower() for s in cls.list_supported_models()):
            raise ValueError(f"Model {model} is already registered")
        cls._custom_models.append(dict(model=model, pooling=PoolingType(pooling), normalization=normalization,
                                       sources=asdict(sources), dim=dim, model_file=model_file,
                                       description=description, license=license, size_in_GB=size_in_gb,
                                       additional_files=list(additional_files or []), tasks={}, runtime_class="Custom"))

    @classmethod
    def get_embedding_size(cls, model_name):
        return cls._spec(model_name)["dim"]

    @classmethod
    def _spec(cls, name):
        for spec in [*_CATALOG, *cls._custom_models]:
            if spec["model"].lower() == name.lower():
                return deepcopy(spec)
        raise ValueError(f"Model {name} is not supported; use TextEmbedding.list_supported_models()")

    def __init__(self, model_name="BAAI/bge-small-en-v1.5", cache_dir=None, threads=None, providers=None,
                 specific_model_path=None, local_files_only=False):
        import onnxruntime as ort
        from onnxruntime.capi.onnxruntime_pybind11_state import InvalidProtobuf

        self.model_name = model_name
        self.spec = self._spec(model_name)
        self.embedding_size = self.spec["dim"]
        cache = Path(cache_dir or os.environ.get("FASTEMBED_CACHE_DIR") or "~/.cache/fastembed").expanduser()
        cache.mkdir(parents=True, exist_ok=True)
        offline = local_files_only or os.environ.get("HF_HUB_OFFLINE", "").upper() in {"1", "TRUE", "YES", "ON"}
        self._model_dir = Path(specific_model_path) if specific_model_path else _model_directory(self.spec, cache, offline)
        self.tokenizer = _load_tokenizer(self._model_dir)
        # Retain cap_tokens/loaded-snapshot access without a self-cycle that would
        # keep a discarded native inference session alive until cyclic GC runs.
        self.model = SimpleNamespace(_model_dir=self._model_dir, tokenizer=self.tokenizer)
        kind = self.spec["runtime_class"]
        self.pooling, self.normalization = PoolingType.CLS, True
        self.output_names, self.output_index = None, 0
        if kind in {"PooledEmbedding", "PooledNormalizedEmbedding", "JinaEmbeddingV3"}:
            self.pooling = PoolingType.MEAN
            self.normalization = kind != "PooledEmbedding"
        elif kind == "LastTokenNormalizedEmbedding":
            self.pooling = PoolingType.LAST_TOKEN
        elif kind in {"CLIPOnnxEmbedding", "BuiltinSentenceEmbedding"}:
            self.pooling, self.normalization = PoolingType.DISABLED, False
            self.output_index = 1 if kind == "BuiltinSentenceEmbedding" else 0
        elif kind == "SiglipOnnxTextEmbedding":
            if self.tokenizer.truncation is None:
                raise InvalidModelCache(self._model_dir, "SigLIP requires a finite maximum context length")
            self.output_names = ["pooler_output"]
            padding = self.tokenizer.padding
            self.tokenizer.enable_padding(**{k: v for k, v in padding.items() if k != "length"},
                                          length=self.tokenizer.truncation["max_length"])
        elif kind == "Custom":
            self.pooling, self.normalization = self.spec["pooling"], self.spec["normalization"]
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads is not None:
            options.intra_op_num_threads = options.inter_op_num_threads = threads
        available = ort.get_available_providers()
        providers = list(providers) if providers is not None else [
            "CUDAExecutionProvider" if "CUDAExecutionProvider" in available else "CPUExecutionProvider"]
        for provider in providers:
            name = provider if isinstance(provider, str) else provider[0]
            if name not in available:
                raise ValueError(f"Provider {name} is not available. Available providers: {available}")
        try:
            self._session = ort.InferenceSession(str(self._model_dir / self.spec["model_file"]),
                                                 providers=providers, sess_options=options)
        except InvalidProtobuf as error:
            raise InvalidModelCache(self._model_dir, "Invalid ONNX protobuf") from error
        self._input_names = {node.name for node in self._session.get_inputs()}

    def embed(self, documents, batch_size=256, *, task_id=1):
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        iterator = iter([documents] if isinstance(documents, str) else documents)
        while batch := list(islice(iterator, batch_size)):
            encoded = self.tokenizer.encode_batch(batch)
            ids = np.array([e.ids for e in encoded], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
            inputs = {"input_ids": ids}
            if "attention_mask" in self._input_names:
                inputs["attention_mask"] = mask
            if "token_type_ids" in self._input_names:
                inputs["token_type_ids"] = np.zeros_like(ids)
            if self.spec["runtime_class"] == "JinaEmbeddingV3":
                inputs["task_id"] = np.array(task_id, dtype=np.int64)
            output = self._session.run(self.output_names, inputs)[self.output_index]
            vectors = _postprocess(output, mask, self.pooling, self.normalization)
            if vectors.shape != (len(batch), self.embedding_size):
                raise ValueError(f"Unexpected embedding dimensions: {vectors.shape}; expected {(len(batch), self.embedding_size)}")
            yield from vectors

    def query_embed(self, query, **kwargs):
        yield from self.embed(query, task_id=0, **kwargs)

    def passage_embed(self, texts, **kwargs):
        yield from self.embed(texts, task_id=1, **kwargs)

    def token_count(self, texts, batch_size=1024):
        iterator = iter([texts] if isinstance(texts, str) else texts)
        count = 0
        while batch := list(islice(iterator, batch_size)):
            count += sum(sum(e.attention_mask) for e in self.tokenizer.encode_batch(batch))
        return count
