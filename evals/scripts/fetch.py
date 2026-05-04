"""
Fetch-on-demand source loader for evals golden set.

Usage:
    python -m evals.scripts.fetch --validate
        Probe all configured datasets, confirm schema, print sample query for each.
        Side effects: HF cache populated under ~/.cache/huggingface/. No git-tracked output.

    python -m evals.scripts.fetch --dataset wildbench --idx 42
        Fetch a single row by source_idx. Prints query text + sha256.

Storage policy: source query texts are NEVER persisted by this script.
HF library caches downloads under ~/.cache/huggingface/ which is system-wide
and gitignored at the user level — we don't manage it.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterator

# datasets is an optional dep (project.optional-dependencies.evals). We import
# it lazily so this module — and the constants it exposes (DATASETS,
# DatasetSpec, sha256_short) — can be imported in environments that only
# need the local label-pipeline helpers without HF access.


def _require_load_dataset():
    """Lazy import of ``datasets.load_dataset``.

    Raises ``RuntimeError`` instead of ``sys.exit`` so callers can handle the
    missing dependency cleanly (e.g. tests, `--help`, or downstream importers
    of ``DATASETS`` that never call into HF).
    """
    try:
        from datasets import load_dataset  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "'datasets' not installed. Run: pip install -e '.[evals]'"
        ) from exc
    return load_dataset


@dataclass(frozen=True)
class DatasetSpec:
    """Single source dataset: where to fetch, how to extract the query."""

    key: str
    hf_id: str
    config: str | None
    split: str
    extract_query: Callable[[dict[str, Any]], str]
    extract_meta: Callable[[dict[str, Any]], dict[str, Any]]
    license: str
    license_url: str
    notes: str


def _extract_wildbench_query(row: dict[str, Any]) -> str:
    """WildBench: conversation_input is a list of {role, content}; first user turn is the query."""
    convo = row.get("conversation_input") or []
    for turn in convo:
        if isinstance(turn, dict) and turn.get("role") == "user":
            content = turn.get("content")
            if isinstance(content, str) and content.strip():
                return content
    raise ValueError(f"no user turn in conversation_input: keys={list(row)}")


def _extract_wildbench_meta(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": row.get("session_id"),
        "intent": row.get("intent"),
        "appropriate": row.get("appropriate"),
    }


def _extract_massive_query(row: dict[str, Any]) -> str:
    text = row.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"missing 'text' field: keys={list(row)}")
    return text


def _extract_massive_meta(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "lang": row.get("lang"),
        "label": row.get("label"),
        "label_text": row.get("label_text"),
    }


def _extract_clinc_query(row: dict[str, Any]) -> str:
    text = row.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"missing 'text' field: keys={list(row)}")
    return text


def _extract_clinc_meta(row: dict[str, Any]) -> dict[str, Any]:
    return {"intent": row.get("intent")}


DATASETS: dict[str, DatasetSpec] = {
    "wildbench": DatasetSpec(
        key="wildbench",
        hf_id="allenai/WildBench",
        config="v2",
        split="test",
        extract_query=_extract_wildbench_query,
        extract_meta=_extract_wildbench_meta,
        license="AI2 ImpACT (non-commercial-derivatives by default)",
        license_url="https://huggingface.co/datasets/allenai/WildBench",
        notes="Curated hard real-user prompts from Chatbot Arena. Multi-turn possible; we use first user turn.",
    ),
    "massive_ru": DatasetSpec(
        key="massive_ru",
        hf_id="mteb/amazon_massive_intent",
        config="ru",
        split="test",
        extract_query=_extract_massive_query,
        extract_meta=_extract_massive_meta,
        license="CC-BY-4.0 (MTEB parquet mirror of AmazonScience/MASSIVE)",
        license_url="https://huggingface.co/datasets/mteb/amazon_massive_intent",
        notes="Russian utterances with intent labels. Parquet mirror — works with new datasets lib (the original AmazonScience/massive uses deprecated script loader).",
    ),
    "massive_en": DatasetSpec(
        key="massive_en",
        hf_id="mteb/amazon_massive_intent",
        config="en",
        split="test",
        extract_query=_extract_massive_query,
        extract_meta=_extract_massive_meta,
        license="CC-BY-4.0 (MTEB parquet mirror of AmazonScience/MASSIVE)",
        license_url="https://huggingface.co/datasets/mteb/amazon_massive_intent",
        notes="English utterances. Parquet mirror.",
    ),
    "massive_es": DatasetSpec(
        key="massive_es",
        hf_id="mteb/amazon_massive_intent",
        config="es",
        split="test",
        extract_query=_extract_massive_query,
        extract_meta=_extract_massive_meta,
        license="CC-BY-4.0 (MTEB parquet mirror of AmazonScience/MASSIVE)",
        license_url="https://huggingface.co/datasets/mteb/amazon_massive_intent",
        notes="Spanish utterances. Parquet mirror.",
    ),
    "clinc_oos": DatasetSpec(
        key="clinc_oos",
        hf_id="clinc_oos",
        config="plus",
        split="test",
        extract_query=_extract_clinc_query,
        extract_meta=_extract_clinc_meta,
        license="CC-BY-3.0",
        license_url="https://huggingface.co/datasets/clinc_oos",
        notes="Out-of-scope subset filtered downstream where intent label corresponds to 'oos'.",
    ),
}


def _stream_first(spec: DatasetSpec, n: int = 1) -> Iterator[dict[str, Any]]:
    """Stream first n rows of a dataset (no full download)."""
    load_dataset = _require_load_dataset()
    ds = load_dataset(spec.hf_id, name=spec.config, split=spec.split, streaming=True)
    count = 0
    for row in ds:
        if count >= n:
            return
        yield row
        count += 1


def sha256_short(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def validate_one(spec: DatasetSpec) -> dict[str, Any]:
    """Probe a single dataset: fetch first row, extract query, return diagnostic dict."""
    try:
        rows = list(_stream_first(spec, n=1))
    except Exception as exc:
        return {"key": spec.key, "ok": False, "error": f"load failed: {exc!r}"}

    if not rows:
        return {"key": spec.key, "ok": False, "error": "empty dataset"}

    row = rows[0]
    keys = sorted(row.keys())
    try:
        query = spec.extract_query(row)
    except Exception as exc:
        return {
            "key": spec.key,
            "ok": False,
            "error": f"extract_query failed: {exc!r}",
            "row_keys": keys,
        }

    try:
        meta = spec.extract_meta(row)
    except Exception as exc:
        meta = {"error": repr(exc)}

    preview = query[:120] + ("..." if len(query) > 120 else "")
    return {
        "key": spec.key,
        "ok": True,
        "hf_id": spec.hf_id,
        "config": spec.config,
        "split": spec.split,
        "row_keys": keys,
        "extracted_query_preview": preview,
        "extracted_query_hash": sha256_short(query),
        "extracted_meta": meta,
        "license": spec.license,
    }


def cmd_validate() -> int:
    print("=" * 70)
    print("FETCH VALIDATE: probing source datasets")
    print("=" * 70)
    fail_count = 0
    for spec in DATASETS.values():
        result = validate_one(spec)
        ok = result.get("ok")
        marker = "[OK]  " if ok else "[FAIL]"
        print(f"\n{marker} {result['key']}: {spec.hf_id} (config={spec.config}, split={spec.split})")
        if not ok:
            print(f"   error: {result.get('error')}")
            if "row_keys" in result:
                print(f"   row_keys: {result['row_keys']}")
            fail_count += 1
            continue
        print(f"   row_keys: {result['row_keys']}")
        print(f"   query preview: {result['extracted_query_preview']!r}")
        print(f"   query hash: {result['extracted_query_hash']}")
        print(f"   meta: {result['extracted_meta']}")
        print(f"   license: {result['license']}")

    print("\n" + "=" * 70)
    if fail_count:
        print(f"FAILED: {fail_count}/{len(DATASETS)} datasets unreachable or malformed")
        return 1
    print(f"OK: {len(DATASETS)}/{len(DATASETS)} datasets reachable")
    return 0


def cmd_fetch_one(dataset_key: str, idx: int) -> int:
    if dataset_key not in DATASETS:
        print(f"ERROR: unknown dataset {dataset_key!r}; known: {sorted(DATASETS)}", file=sys.stderr)
        return 2
    spec = DATASETS[dataset_key]
    # Use non-streaming load + direct index. The previous streaming +
    # list(_stream_first(..., n=idx+1)) was O(idx) network/buffer work and
    # could be very slow for large idx (e.g. CLINC samples in the thousands).
    load_dataset = _require_load_dataset()
    ds = load_dataset(spec.hf_id, name=spec.config, split=spec.split)
    if idx >= len(ds):
        print(f"ERROR: only {len(ds)} rows available, idx={idx} out of range", file=sys.stderr)
        return 2
    row = ds[idx]
    query = spec.extract_query(row)
    meta = spec.extract_meta(row)
    print(f"dataset: {spec.hf_id} (config={spec.config}, split={spec.split})")
    print(f"idx: {idx}")
    print(f"hash: {sha256_short(query)}")
    print(f"meta: {meta}")
    print(f"query:\n{query}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fetch", description=__doc__)
    parser.add_argument("--validate", action="store_true", help="probe all datasets, exit non-zero on failure")
    parser.add_argument("--dataset", choices=sorted(DATASETS), help="single dataset to fetch one row from")
    parser.add_argument("--idx", type=int, default=0, help="row index for --dataset mode")
    args = parser.parse_args(argv)

    try:
        if args.validate:
            return cmd_validate()
        if args.dataset:
            return cmd_fetch_one(args.dataset, args.idx)
    except RuntimeError as exc:
        # Catches the missing-`datasets` signal from `_require_load_dataset()`
        # so the CLI exits cleanly (code 2) instead of dumping a stack trace.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
