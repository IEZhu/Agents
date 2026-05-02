"""
Split evals/datasets/_unlabeled.jsonl into N self-contained batch prompt files
that can be handed off to Claude Code subagents (Agent tool) for labeling.

Each output file contains:
  - The agent catalog (51 specialists)
  - The skill ID list
  - Tier guidance
  - JSON output schema
  - The batch's queries (with id, source, query)

Subagent reads the file, labels each row, returns a JSON array.

Usage:
    python -m evals.scripts.prepare_batches --batches 5
    # writes evals/datasets/_batches/batch_001.md ... batch_005.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.scripts.label_with_claude import (  # noqa: E402
    DEFAULT_UNLABELED_OUT,
    build_system_prompt,
    list_agent_names,
    list_skill_ids,
)

DEFAULT_BATCH_DIR = REPO_ROOT / "evals" / "datasets" / "_batches"
QUERY_TRUNCATE_CHARS = 4000  # WildBench has outlier 10k-char prompts; cap for prompt size sanity.


def split_evenly(items: list, n_batches: int) -> list[list]:
    out: list[list] = [[] for _ in range(n_batches)]
    for i, item in enumerate(items):
        out[i % n_batches].append(item)
    return out


def truncate_query(text: str, limit: int = QUERY_TRUNCATE_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…[truncated, original {len(text)} chars]", True


def build_batch_prompt(batch_idx: int, total_batches: int, rows: list[dict], system_prompt: str) -> str:
    queries_block_parts: list[str] = []
    for r in rows:
        text, truncated = truncate_query(r["query"])
        meta_summary = json.dumps(r.get("source_meta") or {}, ensure_ascii=False)
        queries_block_parts.append(
            f"### {r['id']}  (source: {r['source']}, meta: {meta_summary})\n{text}\n"
        )
    queries_block = "\n".join(queries_block_parts)

    return f"""# Routing-label batch {batch_idx}/{total_batches}

You are a routing-label generator for the Agents-Core multi-agent system.

{system_prompt}

## Output format
Return ONE JSON array with one object per query.  Each object must have keys:

    id              (copy from the query header)
    expected_agent  (exact agent name from the catalog above)
    expected_tier   ("lite" | "standard" | "deep")
    expected_skills (array of 0-5 skill IDs from the list above)
    language        (ISO-639-1 code, e.g. "en" / "ru" / "es" / "unknown")
    domain          (short tag, e.g. "dev" / "legal" / "medical" / "smart_home" / "oos")
    confidence      (float in [0,1])
    reasoning       (<=240 chars)

Wrap the JSON array in a fenced code block tagged ```json so it parses cleanly.
Do NOT add commentary outside the code block.

## Queries to label ({len(rows)} total)

{queries_block}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prepare_batches", description=__doc__)
    parser.add_argument("--unlabeled", type=Path, default=DEFAULT_UNLABELED_OUT)
    parser.add_argument("--batches", type=int, default=5, help="number of batch files to produce")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_BATCH_DIR)
    args = parser.parse_args(argv)

    if not args.unlabeled.exists():
        print(f"ERROR: {args.unlabeled} not found. Run --prepare first.", file=sys.stderr)
        return 2

    rows = [json.loads(line) for line in args.unlabeled.read_text("utf-8").splitlines() if line.strip()]
    if not rows:
        print("ERROR: empty unlabeled file.", file=sys.stderr)
        return 2

    agent_names = list_agent_names()
    skill_ids = list_skill_ids()
    system_prompt = build_system_prompt(agent_names, skill_ids)

    batches = split_evenly(rows, args.batches)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for i, batch in enumerate(batches, 1):
        prompt = build_batch_prompt(i, args.batches, batch, system_prompt)
        path = args.out_dir / f"batch_{i:03d}.md"
        path.write_text(prompt, encoding="utf-8")
        print(f"wrote {path}: {len(batch)} queries, {len(prompt)} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
