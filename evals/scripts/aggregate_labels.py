"""
Aggregate labeled batches into the canonical routing.jsonl golden set.

Reads:
  evals/datasets/_unlabeled.jsonl     - source pointers + raw query text (gitignored)
  evals/datasets/_batches/labels_*.json - JSON arrays from labeling subagents

Writes:
  evals/datasets/routing.jsonl  - canonical golden set committed to git
                                  (no raw query text — only labels + source pointers + sha256)

Validation:
  - expected_agent must be in the on-disk agent catalog (51 valid names)
  - expected_skills filtered to skills that exist on disk; invalid ones logged + dropped
  - missing labels (source row not labeled) reported

Usage:
  python -m evals.scripts.aggregate_labels
  python -m evals.scripts.aggregate_labels --strict   (fail on any invalid agent)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.scripts.label_with_claude import (  # noqa: E402
    DEFAULT_OUT,
    DEFAULT_UNLABELED_OUT,
    LABELER_MODEL,
    list_agent_names,
    list_skill_ids,
)

DEFAULT_BATCH_DIR = REPO_ROOT / "evals" / "datasets" / "_batches"


def default_labeler_tag() -> str:
    """Resolve the labeler provenance string. Precedence:
      1. AGENTS_LABELER_TAG env var (explicit override, e.g. CI)
      2. <LABELER_MODEL>@<today> (matches the convention used by --label)
    Override per-invocation via the --labeler-tag CLI flag.
    """
    return os.environ.get("AGENTS_LABELER_TAG") or f"{LABELER_MODEL}@{dt.date.today().isoformat()}"


def load_unlabeled(path: Path) -> dict[str, dict]:
    """Map id -> source row (with the raw `query` field stripped so it never
    leaks into committed artifacts via downstream record building)."""
    out: dict[str, dict] = {}
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        row.pop("query", None)
        out[row["id"]] = row
    return out


def load_labels(batch_dir: Path) -> list[dict]:
    label_files = sorted(batch_dir.glob("labels_*.json"))
    if not label_files:
        raise FileNotFoundError(f"no labels_*.json files in {batch_dir}")
    out: list[dict] = []
    for path in label_files:
        try:
            data = json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            print(f"ERROR: {path.name} not valid JSON: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, list):
            print(f"ERROR: {path.name} not a JSON array", file=sys.stderr)
            continue
        out.extend(data)
    return out


def make_record(label: dict, source: dict, labeler_tag: str) -> dict:
    return {
        "id": label["id"],
        "source": source["source"],
        "source_config": source.get("source_config"),
        "source_split": source["source_split"],
        "source_idx": source["source_idx"],
        "source_revision": None,
        "source_row_hash": source["source_row_hash"],
        "language": label.get("language"),
        "domain": label.get("domain"),
        "expected_agent": label["expected_agent"],
        "expected_tier": label["expected_tier"],
        "expected_skills": label.get("expected_skills") or [],
        "expected_implants": [],
        "context_hash": None,
        "labeled_by": labeler_tag,
        "label_confidence": label.get("confidence"),
        "human_reviewed": False,
        "notes": label.get("reasoning") or "",
        "tags": ["routing", label.get("language") or "unknown", label["id"].rsplit("-", 1)[0]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aggregate_labels", description=__doc__)
    parser.add_argument("--unlabeled", type=Path, default=DEFAULT_UNLABELED_OUT)
    parser.add_argument("--batch-dir", type=Path, default=DEFAULT_BATCH_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--strict", action="store_true",
                        help="fail (exit 1) if any expected_agent is invalid")
    parser.add_argument("--labeler-tag", default=None,
                        help="provenance string written to `labeled_by` "
                             "(default: $AGENTS_LABELER_TAG or "
                             "<LABELER_MODEL>@<today>)")
    args = parser.parse_args(argv)
    labeler_tag = args.labeler_tag or default_labeler_tag()

    valid_agents = set(list_agent_names())
    valid_skills = set(list_skill_ids())

    sources = load_unlabeled(args.unlabeled)
    labels = load_labels(args.batch_dir)

    seen_ids: set[str] = set()
    invalid_agent_ids: list[tuple[str, str]] = []
    invalid_skills_dropped: Counter[str] = Counter()
    records: list[dict] = []

    for label in labels:
        lid = label.get("id")
        if not lid or lid not in sources:
            print(f"WARN: label has unknown id {lid!r}", file=sys.stderr)
            continue
        if lid in seen_ids:
            print(f"WARN: duplicate label for id {lid!r}", file=sys.stderr)
            continue

        agent = label.get("expected_agent")
        if agent not in valid_agents:
            invalid_agent_ids.append((lid, str(agent)))
            if args.strict:
                # In strict mode this id stays unaccepted so it shows up in
                # `missing = sources - seen_ids` reporting.
                continue
            # downgrade to universal_agent for non-strict mode
            label = dict(label)
            label["expected_agent"] = "universal_agent"
            label["reasoning"] = (
                f"[auto-downgraded: original agent {agent!r} not in catalog] "
                + (label.get("reasoning") or "")
            )[:240]
        seen_ids.add(lid)

        cleaned_skills: list[str] = []
        for skill in label.get("expected_skills") or []:
            if skill in valid_skills:
                cleaned_skills.append(skill)
            else:
                invalid_skills_dropped[skill] += 1
        label = {**label, "expected_skills": cleaned_skills}

        records.append(make_record(label, sources[lid], labeler_tag))

    missing = sorted(set(sources) - seen_ids)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ----- report -----
    print(f"wrote {len(records)} records to {args.out}")
    print(f"sources total: {len(sources)}, labeled: {len(seen_ids)}, missing: {len(missing)}")
    if missing:
        print(f"  missing ids: {missing[:10]}{' ...' if len(missing) > 10 else ''}")

    print(f"\ninvalid expected_agent values: {len(invalid_agent_ids)}")
    for lid, agent in invalid_agent_ids[:10]:
        print(f"  {lid}: {agent!r}")
    if len(invalid_agent_ids) > 10:
        print(f"  ...{len(invalid_agent_ids) - 10} more")

    if invalid_skills_dropped:
        print(f"\ninvalid skills dropped (top 10):")
        for skill, n in invalid_skills_dropped.most_common(10):
            print(f"  {n:3d}× {skill}")

    print("\nper-source counts (from id prefix):")
    src_counts = Counter(rec["id"].rsplit("-", 1)[0] for rec in records)
    for src, n in sorted(src_counts.items()):
        print(f"  {n:3d} {src}")

    print("\nagent distribution (top 15):")
    for agent, n in Counter(r["expected_agent"] for r in records).most_common(15):
        print(f"  {n:3d} {agent}")

    print("\ntier distribution:")
    for tier, n in sorted(Counter(r["expected_tier"] for r in records).items()):
        print(f"  {n:3d} {tier}")

    print("\nlanguage distribution:")
    for lang, n in sorted(Counter(r.get("language") or "?" for r in records).items()):
        print(f"  {n:3d} {lang}")

    print("\nconfidence histogram:")
    bins = Counter()
    for r in records:
        c = r.get("label_confidence") or 0.0
        bins[f"{int(c * 10) / 10:.1f}"] += 1
    for b, n in sorted(bins.items()):
        print(f"  {b}: {'#' * n}")

    if args.strict and (invalid_agent_ids or missing):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
