"""Summarize the subagent tier ledger per client, tier and model.

Shows how many tiered workers were prepared and validated, how many were accepted
without escalation, and where escalations went. Reads the installation's
``data/subagent-tiers/ledger.jsonl`` unless ``--path`` names another file.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.engine import subagent_ledger  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", type=Path, help="ledger file (default: the installation's ledger)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    rows = subagent_ledger.summarize(subagent_ledger.read_entries(args.path))
    print(json.dumps(rows, indent=2) if args.json else subagent_ledger.format_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
