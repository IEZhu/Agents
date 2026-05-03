"""
One-shot routing-label generator for the evals golden set.

Pulls a deterministic sample from each source dataset (via fetch.DATASETS),
asks Claude Opus 4.7 to assign:
  - expected_agent (one of the 51 specialist agents)
  - expected_tier  (lite / standard / deep)
  - expected_skills (subset of available skill IDs)
  - language, domain, label_confidence, reasoning
…then writes a batch of structured rows to `evals/datasets/routing.jsonl`
(the file is overwritten on each run; see `write_jsonl()`).

Source query texts are NOT written to the jsonl — only the SHA-256 hash
plus enough fetch metadata to re-resolve the row on each eval run.

Modes
-----
--preview N   : sample N rows, print them and the system-prompt size, no API call.
--prepare     : sample full allocation, write evals/datasets/_unlabeled.jsonl with
                query text + source pointers (gitignored). Used by the Agent-tool
                labeling path (no direct API call).
--label       : full pipeline via Anthropic API. Requires ANTHROPIC_API_KEY.
--source KEY  : restrict to a single source (e.g. wildbench).
--limit N     : cap total rows (overrides per-source allocation).
--seed N      : sampling seed (default 42).
--out PATH    : output jsonl path (default evals/datasets/routing.jsonl;
                for --prepare default is evals/datasets/_unlabeled.jsonl).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Make ``src.utils.prompt_loader`` importable when running from repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.utils.prompt_loader import get_agent_metadata  # noqa: E402

# Reuse the canonical lazy loader from fetch.py — keeps the missing-deps
# error message in one place so the two callers can't drift.
from evals.scripts.fetch import DATASETS, DatasetSpec, _require_load_dataset, sha256_short  # noqa: E402

LABELER_MODEL = "claude-opus-4-7"

# Per-source sample allocation. Total = 110.
DEFAULT_ALLOC: dict[str, int] = {
    "wildbench": 60,
    "massive_ru": 10,
    "massive_en": 10,
    "massive_es": 10,
    "clinc_oos": 20,
}

# CLINC150 OOS class label index in `plus` config (verified 2026-05-02).
CLINC_OOS_LABEL_IDX = 42

AGENTS_DIR = REPO_ROOT / "agents"
SKILLS_DIR = REPO_ROOT / "skills"
DEFAULT_OUT = REPO_ROOT / "evals" / "datasets" / "routing.jsonl"
DEFAULT_UNLABELED_OUT = REPO_ROOT / "evals" / "datasets" / "_unlabeled.jsonl"


@dataclass
class Sample:
    """A single source-fetched query waiting to be labeled."""

    source_key: str
    spec: DatasetSpec
    source_idx: int
    query: str
    source_meta: dict[str, Any]


# --------------------------------------------------------------------------- #
# Catalog discovery
# --------------------------------------------------------------------------- #


def list_agent_names() -> list[str]:
    skip = {"common", "capabilities", "schemas"}
    return sorted(
        d.name
        for d in AGENTS_DIR.iterdir()
        if d.is_dir() and d.name not in skip and not d.name.startswith(".")
    )


def list_skill_ids() -> list[str]:
    return sorted(p.stem for p in SKILLS_DIR.glob("skill-*.mdc"))


def build_agent_summary(name: str) -> str:
    meta = get_agent_metadata(name) or {}
    identity = meta.get("identity") or {}
    routing = meta.get("routing") or {}
    role = identity.get("role") or "(no role)"
    keywords = routing.get("domain_keywords") or []
    kw_str = ", ".join(str(k) for k in keywords[:8])
    return f"- {name}: {role}\n    keywords: {kw_str}"


def build_agent_catalog_block(agent_names: list[str]) -> str:
    lines = ["## Agents (one of these is the correct expected_agent)"]
    for name in agent_names:
        lines.append(build_agent_summary(name))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #


def _sample_from_dataset(
    spec: DatasetSpec,
    n: int,
    seed: int,
    *,
    filter_predicate=None,
) -> Iterable[Sample]:
    """Load a split (cached locally) and yield n random samples by source_idx."""
    load_dataset = _require_load_dataset()
    ds = load_dataset(spec.hf_id, name=spec.config, split=spec.split)
    indices = list(range(len(ds)))
    if filter_predicate is not None:
        indices = [i for i in indices if filter_predicate(ds[i])]
    rng = random.Random(seed)
    rng.shuffle(indices)
    chosen = indices[:n]
    for idx in chosen:
        row = ds[idx]
        try:
            query = spec.extract_query(row)
        except Exception as exc:
            print(f"  WARN: extract failed at {spec.key}#{idx}: {exc!r}", file=sys.stderr)
            continue
        meta = spec.extract_meta(row)
        yield Sample(spec.key, spec, idx, query, meta)


def _stable_offset(key: str) -> int:
    """Deterministic per-source seed offset. Python's built-in hash() is
    randomised across processes (PYTHONHASHSEED), so use sha256 to keep
    sampling reproducible across runs and machines.
    """
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big") % 1000


def iter_samples(alloc: dict[str, int], seed: int) -> list[Sample]:
    out: list[Sample] = []
    for key, n in alloc.items():
        if n <= 0:
            continue
        if key not in DATASETS:
            raise ValueError(f"unknown source: {key}")
        spec = DATASETS[key]
        if key == "clinc_oos":
            pred = lambda r: r.get("intent") == CLINC_OOS_LABEL_IDX
        else:
            pred = None
        out.extend(_sample_from_dataset(spec, n, seed=seed + _stable_offset(key), filter_predicate=pred))
    return out


# --------------------------------------------------------------------------- #
# Labeling
# --------------------------------------------------------------------------- #


def build_system_prompt(agent_names: list[str], skill_ids: list[str]) -> str:
    catalog = build_agent_catalog_block(agent_names)
    skills_block = ", ".join(skill_ids)
    return f"""You are a routing-label generator for **Agents-Core**, a multi-agent system.

For each user query, decide which single specialist agent should handle it,
which depth tier the query needs, and which skills are most relevant.

Be conservative: when a query does not clearly match any specialist, pick
`universal_agent` and tier `lite` rather than forcing a fit. Confidence below
0.5 is OK and useful — don't over-claim.

{catalog}

## Tier guidance
- **lite**: short, conversational, single-fact, low-effort queries. (Out-of-scope queries usually map here.)
- **standard**: typical task with moderate complexity, single-domain.
- **deep**: complex / architectural / multi-step / cross-domain queries needing careful reasoning.

## Available skill IDs (pick 0-5 most relevant)
{skills_block}

## Task
You will receive one user query. Call `assign_routing_label` with:
  - expected_agent: exact agent name from the catalog above
  - expected_tier:  lite / standard / deep
  - expected_skills: 0-5 skill IDs (most relevant first; empty array OK)
  - language: ISO-639-1 code (en, ru, es, …) or "unknown"
  - domain: short tag (dev, legal, medical, math, smart_home, oos, …)
  - confidence: float in [0,1] reflecting how clearly this maps to the chosen agent
  - reasoning: <=240 chars explaining the choice
"""


def build_label_tool(agent_names: list[str], skill_ids: list[str]) -> dict[str, Any]:
    return {
        "name": "assign_routing_label",
        "description": "Assign routing label for a single user query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expected_agent": {"type": "string", "enum": agent_names},
                "expected_tier": {"type": "string", "enum": ["lite", "standard", "deep"]},
                "expected_skills": {
                    "type": "array",
                    "items": {"type": "string", "enum": skill_ids},
                    "maxItems": 5,
                },
                "language": {"type": "string"},
                "domain": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "reasoning": {"type": "string", "maxLength": 240},
            },
            "required": ["expected_agent", "expected_tier", "confidence", "reasoning"],
        },
    }


def call_labeler(client, system_prompt: str, tool: dict[str, Any], query: str) -> dict[str, Any]:
    """Single labeling API call. Uses prompt caching on system + tool."""
    response = client.messages.create(
        model=LABELER_MODEL,
        max_tokens=512,
        system=[
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
        ],
        tools=[tool],
        tool_choice={"type": "tool", "name": "assign_routing_label"},
        messages=[{"role": "user", "content": f"Query:\n{query}"}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "assign_routing_label":
            return dict(block.input)
    raise RuntimeError(f"no tool_use block in response: stop_reason={response.stop_reason}")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def make_record(sample: Sample, label: dict[str, Any]) -> dict[str, Any]:
    sample_id = f"{sample.source_key}-{sample.source_idx:05d}"
    today = dt.date.today().isoformat()
    return {
        "id": sample_id,
        "source": sample.spec.hf_id,
        "source_config": sample.spec.config,
        "source_split": sample.spec.split,
        "source_idx": sample.source_idx,
        "source_revision": None,
        "source_row_hash": sha256_short(sample.query),
        "language": label.get("language"),
        "domain": label.get("domain"),
        "expected_agent": label["expected_agent"],
        "expected_tier": label["expected_tier"],
        "expected_skills": label.get("expected_skills") or [],
        "expected_implants": [],
        "context_hash": None,
        "labeled_by": f"{LABELER_MODEL}@{today}",
        "label_confidence": label["confidence"],
        "human_reviewed": False,
        "notes": label["reasoning"],
        "tags": ["routing", label.get("language") or "unknown", sample.source_key],
    }


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="label_with_claude", description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", type=int, metavar="N", help="sample N rows, no API call, print them")
    mode.add_argument("--prepare", action="store_true", help="sample + write _unlabeled.jsonl (no API)")
    mode.add_argument("--label", action="store_true", help="full labeling pipeline (requires API key)")
    p.add_argument("--source", choices=sorted(DATASETS), help="restrict to one source")
    p.add_argument("--limit", type=int, help="cap total rows (testing)")
    p.add_argument("--seed", type=int, default=42, help="sampling seed (default 42)")
    p.add_argument("--out", type=Path, default=None, help="output jsonl path (mode-specific default)")
    return p.parse_args(argv)


def resolve_alloc(args: argparse.Namespace) -> dict[str, int]:
    if args.source:
        alloc = {args.source: DEFAULT_ALLOC.get(args.source, 0)}
    else:
        alloc = dict(DEFAULT_ALLOC)
    if args.limit is not None:
        if args.limit <= 0:
            return {k: 0 for k in alloc}
        # Distribute the limit proportionally to default allocation.
        total = sum(alloc.values()) or 1
        scale = args.limit / total
        alloc = {k: max(1, int(round(v * scale))) for k, v in alloc.items() if v > 0}
        # Trim to exactly args.limit by reducing largest buckets.
        while sum(alloc.values()) > args.limit:
            largest = max(alloc, key=alloc.get)
            alloc[largest] -= 1
    return alloc


def cmd_preview(args: argparse.Namespace) -> int:
    alloc = resolve_alloc(args)
    if args.preview is not None:
        if args.preview <= 0:
            alloc = {}
        else:
            # In preview mode the int N overrides the alloc. Use ``args.source``
            # directly instead of ``next(iter(alloc))``: the latter raises
            # ``StopIteration`` when the requested source is in ``DATASETS`` but
            # absent from ``DEFAULT_ALLOC`` (which makes ``resolve_alloc`` return
            # an empty dict after the ``v > 0`` filter). ``argparse`` already
            # constrains ``args.source`` to ``sorted(DATASETS)``, so it's safe.
            alloc = {args.source: args.preview} if args.source else {}
            if not alloc:
                # Distribute exactly N across all configured sources. For N < n_src
                # the trailing sources get 0 (and are dropped). For N >= n_src each
                # source gets floor(N/n_src), with the first (N mod n_src) sources
                # getting one extra. Total always sums to N — never overshoots.
                sources = list(DEFAULT_ALLOC.keys())
                n_src = len(sources)
                per_each, remainder = divmod(args.preview, n_src)
                alloc = {
                    s: per_each + (1 if i < remainder else 0)
                    for i, s in enumerate(sources)
                }
                alloc = {k: v for k, v in alloc.items() if v > 0}
    samples = iter_samples(alloc, seed=args.seed)
    agent_names = list_agent_names()
    skill_ids = list_skill_ids()
    sys_prompt = build_system_prompt(agent_names, skill_ids)

    print(f"agents: {len(agent_names)}, skills: {len(skill_ids)}")
    print(f"system_prompt size: {len(sys_prompt)} chars (~{len(sys_prompt)//4} tokens estimated)")
    print(f"samples: {len(samples)}")
    print()
    for s in samples:
        head = s.query.replace("\n", " ")[:140]
        print(f"[{s.source_key}#{s.source_idx}] meta={s.source_meta} hash={sha256_short(s.query)}")
        print(f"  → {head!r}")
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key.startswith("sk-ant-...") or len(api_key) < 30:
        print("ERROR: ANTHROPIC_API_KEY missing or placeholder.", file=sys.stderr)
        print("Set a real key in .env or via `export ANTHROPIC_API_KEY=sk-ant-...`.", file=sys.stderr)
        return 2

    try:
        from anthropic import Anthropic  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        print("ERROR: 'anthropic' SDK not installed.", file=sys.stderr)
        return 2

    alloc = resolve_alloc(args)
    samples = iter_samples(alloc, seed=args.seed)
    agent_names = list_agent_names()
    skill_ids = list_skill_ids()
    sys_prompt = build_system_prompt(agent_names, skill_ids)
    tool = build_label_tool(agent_names, skill_ids)
    client = Anthropic(api_key=api_key)

    print(f"labeling {len(samples)} samples via {LABELER_MODEL}...")
    records: list[dict[str, Any]] = []
    started = time.time()
    for i, sample in enumerate(samples, 1):
        try:
            label = call_labeler(client, sys_prompt, tool, sample.query)
        except Exception as exc:
            print(f"  [{i}/{len(samples)}] {sample.source_key}#{sample.source_idx} FAIL: {exc!r}", file=sys.stderr)
            continue
        rec = make_record(sample, label)
        records.append(rec)
        elapsed = time.time() - started
        print(
            f"  [{i}/{len(samples)}] {sample.source_key}#{sample.source_idx} "
            f"→ agent={rec['expected_agent']} tier={rec['expected_tier']} "
            f"conf={rec['label_confidence']:.2f} ({elapsed:.0f}s elapsed)"
        )

    write_jsonl(args.out, records)
    print(f"\nWrote {len(records)} records to {args.out}")
    if records:
        from collections import Counter

        agents = Counter(r["expected_agent"] for r in records)
        print("\nAgent distribution:")
        for agent, count in agents.most_common():
            print(f"  {count:3d} {agent}")
    return 0 if len(records) == len(samples) else 1


def cmd_prepare(args: argparse.Namespace) -> int:
    """Sample full allocation, write _unlabeled.jsonl. Includes query text (gitignored file)."""
    out_path = args.out or DEFAULT_UNLABELED_OUT
    alloc = resolve_alloc(args)
    samples = iter_samples(alloc, seed=args.seed)
    rows = []
    for s in samples:
        rows.append(
            {
                "id": f"{s.source_key}-{s.source_idx:05d}",
                "source": s.spec.hf_id,
                "source_config": s.spec.config,
                "source_split": s.spec.split,
                "source_idx": s.source_idx,
                "source_row_hash": sha256_short(s.query),
                "source_meta": s.source_meta,
                "query": s.query,
            }
        )
    write_jsonl(out_path, rows)
    print(f"wrote {len(rows)} unlabeled samples to {out_path}")
    print(f"per-source counts:")
    from collections import Counter

    for src, n in sorted(Counter(s.source_key for s in samples).items()):
        print(f"  {n:3d} {src}")
    print("\nfile is gitignored (evals/datasets/_*.jsonl) — contains raw source query texts.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.preview is not None:
            return cmd_preview(args)
        if args.prepare:
            return cmd_prepare(args)
        if args.label:
            if args.out is None:
                args.out = DEFAULT_OUT
            return cmd_label(args)
        return 0
    except RuntimeError as exc:
        # Catches the missing-`datasets` signal from `_require_load_dataset()`
        # so the CLI exits cleanly (code 2) instead of dumping a stack trace.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
