"""
Shared sample loader for runners.

Loads `evals/datasets/routing.jsonl`, re-fetches query texts via fetch.py,
and yields (label_record, query_text) pairs. Verifies sha256 hash on every
fetch — drift count surfaces in the runner's report.

Optimization: if `evals/datasets/_unlabeled.jsonl` exists locally (developer
machine that ran `--prepare`), prefer that — avoids hitting HF. CI without
the gitignored file falls back to HF fetch automatically.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.scripts.fetch import DATASETS, _require_load_dataset, sha256_short  # noqa: E402

ROUTING_JSONL = REPO_ROOT / "evals" / "datasets" / "routing.jsonl"
UNLABELED_JSONL = REPO_ROOT / "evals" / "datasets" / "_unlabeled.jsonl"


@dataclass
class EvalSample:
    label: dict
    query: str
    drift: bool = False
    fetch_error: str | None = None


@dataclass
class LoaderStats:
    total: int = 0
    drift: int = 0
    fetch_errors: int = 0
    used_local_cache: bool = False
    drift_ids: list[str] = field(default_factory=list)


def _load_local_unlabeled() -> dict[str, str]:
    """Map id -> query text from _unlabeled.jsonl (if present)."""
    if not UNLABELED_JSONL.exists():
        return {}
    out: dict[str, str] = {}
    for line in UNLABELED_JSONL.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[row["id"]] = row["query"]
    return out


def _resolve_dataset_key(label: dict) -> str | None:
    """Map (source hf_id, source_config) back to our DATASETS dict key."""
    source = label.get("source")
    config = label.get("source_config")
    for key, spec in DATASETS.items():
        if spec.hf_id == source and spec.config == config:
            return key
    return None


def _fetch_via_hf(label: dict, cache: dict[tuple[str, str | None, str], object]) -> str:
    """Lazy-import datasets here so the loader stays cheap when unlabeled.jsonl exists.

    Re-uses already-loaded splits via ``cache`` (keyed by (hf_id, config, split))
    so a 110-row eval triggers at most one ``load_dataset`` call per source.

    Routes the import through ``fetch._require_load_dataset()`` so the missing-
    `[evals]`-extra error message is identical across the codebase.
    """
    load_dataset = _require_load_dataset()

    key = _resolve_dataset_key(label)
    if key is None:
        raise KeyError(f"no DatasetSpec match for source={label.get('source')!r} config={label.get('source_config')!r}")
    spec = DATASETS[key]
    cache_key = (spec.hf_id, spec.config, spec.split)
    ds = cache.get(cache_key)
    if ds is None:
        ds = load_dataset(spec.hf_id, name=spec.config, split=spec.split)
        cache[cache_key] = ds
    row = ds[label["source_idx"]]
    return spec.extract_query(row)


def load_samples(routing_path: Path = ROUTING_JSONL) -> tuple[list[EvalSample], LoaderStats]:
    if not routing_path.exists():
        raise FileNotFoundError(f"{routing_path} not found — run aggregate_labels first")

    labels = [json.loads(line) for line in routing_path.read_text("utf-8").splitlines() if line.strip()]
    local = _load_local_unlabeled()
    stats = LoaderStats(total=len(labels), used_local_cache=bool(local))

    samples: list[EvalSample] = []
    hf_loaded: dict[tuple[str, str | None, str], object] = {}

    for label in labels:
        lid = label["id"]
        query: str | None = local.get(lid)

        if query is None:
            try:
                query = _fetch_via_hf(label, hf_loaded)
            except RuntimeError:
                # Missing `[evals]` extra surfaces as RuntimeError from
                # `_require_load_dataset()`. This is a setup error — every
                # subsequent sample will hit the same wall, so bail out
                # immediately instead of silently producing 0 valid samples.
                raise
            except Exception as exc:
                stats.fetch_errors += 1
                samples.append(EvalSample(label=label, query="", fetch_error=str(exc)))
                continue

        actual_hash = sha256_short(query)
        drift = actual_hash != label.get("source_row_hash")
        if drift:
            stats.drift += 1
            stats.drift_ids.append(lid)
        samples.append(EvalSample(label=label, query=query, drift=drift))

    return samples, stats


def iter_valid(samples: list[EvalSample]) -> Iterator[EvalSample]:
    """Yield only samples that fetched cleanly and didn't drift."""
    for s in samples:
        if s.fetch_error:
            continue
        if s.drift:
            continue
        yield s
