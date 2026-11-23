"""Report each rule's recent miss rate over the thread-close flow's recorded closes.

Reads the ``#thread-close`` reports in one workspace's journal: ``history.md``, a pending
rotation and the ``history/`` archives, as ``read_history`` reads them without library
sync, so the segments other machines share are not read. Each close whose
``metadata.record`` has ``kind: report`` gives every finding slug at most one outcome:

- a miss: a ``missed`` or ``misused`` finding at V1 or above;
- held: a ``used`` or ``caught`` row;
- none: every other row, among them a miss at V0, without a level or at ``refuted``,
  ``stale`` or ``contested``, and the ``unavailable``, ``n/a``, ``uncatalogued`` and
  ``no-rule`` rows.

Each slug's outcomes are filtered in close order with fading memory
(``src/estimation/rate.py``), at the close's own time, and carried to now. Printed per
slug: the closes that gave it an outcome, the recent miss rate with its 95 percent band,
and the all-time number of misses.

The report is evidence only. Whether a repeat escalates is decided by the "Repeats
escalate" step of ``flows/thread-close.md``; nothing here decides or proposes it.

    python scripts/thread_close_miss_rate.py --workspace /path/to/repository
    python scripts/thread_close_miss_rate.py --workspace /path/to/repository --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.estimation import rate  # noqa: E402
from src.memory.history import HistoryEntry, HistoryReader  # noqa: E402

CLOSE_TAG = "thread-close"
PROGRESS_TAG = "progress"
REPORT_KIND = "report"
MISS_STATUSES = frozenset({"missed", "misused"})
HELD_STATUSES = frozenset({"used", "caught"})
# V0 rests only on a compacted conversation: the flow never counts it as a repeat.
COUNTED_LEVELS = frozenset({"V1", "V2", "V3", "V4"})
# An outcome weighs half after this many days. Two weeks, as the worker ledger's
# acceptance rate uses: long enough to hold several closes, short enough that misses
# from before a patch stop hiding what happened after it.
HALF_LIFE_DAYS = 14.0


def _bare(tag: str) -> str:
    return tag[1:] if tag.startswith("#") else tag


def is_close_report(entry: HistoryEntry) -> bool:
    """A close's own entry: tagged ``#thread-close`` and not ``#progress``."""
    tags = {_bare(tag) for tag in entry.tags}
    return CLOSE_TAG in tags and PROGRESS_TAG not in tags


def report_record(entry: HistoryEntry) -> dict | None:
    """The entry's ``kind: report`` record, or None for an entry without one."""
    record = entry.metadata.get("record") if isinstance(entry.metadata, dict) else None
    return record if isinstance(record, dict) and record.get("kind") == REPORT_KIND else None


def outcomes(record: dict) -> dict[str, bool]:
    """Each slug the record gives an outcome: True for a miss, False for held.

    A slug with both a counted miss and a held row in one close is a miss there.
    """
    findings = record.get("findings")
    out: dict[str, bool] = {}
    for finding in findings if isinstance(findings, list) else ():
        if not isinstance(finding, dict) or not isinstance(finding.get("slug"), str):
            continue
        slug, status = finding["slug"], finding.get("status")
        if status in MISS_STATUSES and finding.get("level") in COUNTED_LEVELS:
            out[slug] = True
        elif status in HELD_STATUSES:
            out.setdefault(slug, False)
    return out


def _epoch(timestamp: str) -> float | None:
    try:
        moment = dt.datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _recent(estimate, now: float) -> dict:
    current = rate.forward(estimate, max(now, estimate.at), HALF_LIFE_DAYS)
    low, high = rate.band(current)
    return {"rate": round(current.mean, 3), "low": round(low, 3), "high": round(high, 3)}


def summarize(entries: list[HistoryEntry], now: float | None = None) -> dict:
    """The closes among ``entries``, how many carry a report record, and one row per slug.

    Rows come highest recent miss rate first. ``recent`` is None for a slug whose closes
    all lack a readable time: they still count, but do not move the rate.
    """
    now = dt.datetime.now(dt.timezone.utc).timestamp() if now is None else now
    closes = sorted((entry for entry in entries if is_close_report(entry)),
                    key=lambda entry: (_epoch(entry.timestamp) or float("-inf"), entry.id))
    rows: dict[str, dict] = {}
    estimates: dict[str, object] = {}
    recorded = 0
    for entry in closes:
        record = report_record(entry)
        if record is None:
            continue
        recorded += 1
        at = _epoch(entry.timestamp)
        for slug, missed in outcomes(record).items():
            row = rows.setdefault(slug, {"slug": slug, "closes": 0, "misses": 0})
            row["closes"] += 1
            row["misses"] += int(missed)
            if at is not None:
                # The filtered outcome is the miss, so the estimate is a miss rate.
                estimates[slug] = rate.observe(estimates.get(slug) or rate.prior(at), missed, at, HALF_LIFE_DAYS)
    for slug, row in rows.items():
        row["recent"] = _recent(estimates[slug], now) if slug in estimates else None
    ordered = sorted(rows.values(), key=lambda row: (-(row["recent"]["rate"] if row["recent"] else -1.0),
                                                     -row["misses"], row["slug"]))
    return {"closes": len(closes), "with_record": recorded, "half_life_days": HALF_LIFE_DAYS, "rules": ordered}


def read_entries(workspace: Path) -> list[HistoryEntry]:
    """The workspace journal's ``#thread-close`` entries, archives included, newest first."""
    return HistoryReader(str(workspace / "history.md")).read_recent(limit=sys.maxsize, tag=CLOSE_TAG)


def _cells(recent: dict | None) -> tuple[str, str]:
    if not recent:
        return "-", "-"
    return f"{recent['rate']:.0%}", f"{recent['low']:.0%}-{recent['high']:.0%}"


def format_table(summary: dict) -> str:
    header = ("slug", "closes", "recent", "95% band", "misses")
    table = [header] + [(row["slug"], str(row["closes"]), *_cells(row["recent"]), str(row["misses"]))
                        for row in summary["rules"]]
    widths = [max(len(line[column]) for line in table) for column in range(len(header))]
    lines = [f"{summary['closes']} closes, {summary['with_record']} with a report record; "
             f"half-life {summary['half_life_days']:g} days."]
    lines += ["  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip() for line in table]
    lines.append('Evidence only: escalation follows the "Repeats escalate" step of flows/thread-close.md.')
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, default=Path.cwd(),
                        help="the repository whose journal to read (default: the current directory)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    if not ((workspace / "history.md").is_file() or (workspace / "history").is_dir()):
        parser.error(f"no journal in {workspace}: neither history.md nor history/ exists")
    summary = summarize(read_entries(workspace))
    print(json.dumps({"workspace": str(workspace), **summary}, indent=2) if args.json else format_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
