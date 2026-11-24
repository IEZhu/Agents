"""Report each rule's recent miss rate over the thread-close flow's recorded closes.

Reads the ``#thread-close`` reports in one workspace's journal: ``history.md``, a pending
rotation and the ``history/`` archives, never the segments the user library sync shares
from other machines. Each close whose
``metadata.record`` has ``kind: report`` gives every finding slug at most one outcome:

- a miss: a ``missed`` or ``misused`` finding at V1 or above;
- held: a ``used`` or ``caught`` row;
- none: every other row, among them a miss at V0, without a level or at ``refuted``,
  ``stale`` or ``contested``, the ``unavailable``, ``n/a``, ``uncatalogued`` and
  ``no-rule`` rows, and any row the record schema refuses.

Closes written before the flow's "One rule, one slug" (step 4.2) may name one rule under
several slugs. The report reads such old spellings under their canonical slug at reading
time and never writes the journal:

- derived: a two-part slug's second part alone, or after an instruction source
  (``claude-md``, ``agents-md``, ``memory``), as ``mark-chapter`` and
  ``claude-md.mark-chapter`` for ``ccd-session.mark-chapter``, when exactly one slug
  ``<first>.<part>`` whose first part is not a source was written in some close or is a
  target of the table below; a bare catalogue slug or cooperation clause stays canonical;
- table: the spellings in ``ALIAS_TABLE``, which no rule derives;
- clause: a bare ``cooperation`` miss under ``cooperation-<clause>`` when its close's miss
  tags name exactly that one clause; every other bare ``cooperation`` row stays as it is.

Each slug's outcomes are filtered in close order with fading memory
(``src/estimation/rate.py``), at the close's own time, and carried to now. Printed per
slug: the closes that gave it an outcome, the recent miss rate with its 95 percent band,
the all-time number of misses, and the old spellings merged into it, each with its closes.

The report is evidence only, and a merged spelling is an inference, not a recorded slug.
Whether a repeat escalates is decided by the "Repeats escalate" step of
``flows/thread-close.md``; nothing here decides or proposes it.

    python scripts/thread_close_miss_rate.py --workspace /path/to/repository
    python scripts/thread_close_miss_rate.py --workspace /path/to/repository --json
"""
from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator
import datetime as dt
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.estimation import rate  # noqa: E402
from src.memory import record as turn_record  # noqa: E402
from src.memory.history import HistoryEntry, HistoryReader, merge_entries  # noqa: E402

JOURNAL = "history.md"
# Where an interrupted rotation leaves history.md; the reader reads it as the live journal.
PENDING_ROTATION = JOURNAL + ".rotating"
ARCHIVES = "history"
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

FLOW = REPO_ROOT / "flows" / "thread-close.md"
CATALOGUE_MARK = "**Catalogue.**"
# The places an instruction is stated, which step 4.2 keeps out of every slug, so a first
# part among them marks a slug written before that rule. A hook's file has no fixed name.
INSTRUCTION_SOURCES = frozenset({"claude-md", "agents-md", "memory"})
COOPERATION = "cooperation"
COOPERATION_CLAUSES = frozenset({"register", "claim", "ack", "request", "agreement"})
CLAUSE_SLUGS = frozenset(f"{COOPERATION}-{clause}" for clause in COOPERATION_CLAUSES)
MISS_TAG_PREFIXES = ("ac-missed-", "tool-missed-")
# Old spellings that no rule derives, read under the slugs issue #746 gives them.
ALIAS_TABLE = {
    "task-list": "ccd-turn.update-plan",
    "claude-md.task-list-first-call": "ccd-turn.update-plan",
    "claude-md.websearch-standard": "client.websearch",
}
DERIVED, TABLE, CLAUSE = "derived", "table", "clause"


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


def is_valid_finding(finding) -> bool:
    """Whether ``log_interaction`` would accept the row: the record schema's own finding rule.

    A stored record passed that rule when it was written, so a row failing it now was edited
    or damaged afterwards, and it gives no outcome.
    """
    problems: list[str] = []
    turn_record.FINDING.check(finding, "finding", problems)
    return not problems


def valid_findings(record: dict) -> Iterator[dict]:
    """The record's findings that the record schema accepts, in order."""
    findings = record.get("findings")
    for finding in findings if isinstance(findings, list) else ():
        if is_valid_finding(finding):
            yield finding


def catalogue_slugs(flow: Path | None = None) -> frozenset[str]:
    """The slugs of the flow's catalogue rows, each canonical without a dot (step 4.2)."""
    flow = FLOW if flow is None else flow
    _, found, rest = flow.read_text(encoding="utf-8").partition(CATALOGUE_MARK)
    table = rest.split("\n## ", 1)[0]
    slugs = frozenset(line.split("|")[1].strip(" `") for line in table.splitlines() if line.startswith("| `"))
    if not found or not slugs:
        raise ValueError(f"no catalogue table after {CATALOGUE_MARK} in {flow}")
    return slugs


def derive_aliases(slugs: Iterable[str], catalogue: frozenset[str]) -> dict[str, tuple[str, str]]:
    """Each old spelling among ``slugs`` with the canonical slug it is read under and the alias kind.

    A two-part slug whose first part is not an instruction source is canonical as written,
    and so is a bare catalogue slug or cooperation clause. A bare slug or a
    ``<source>.<part>`` spelling is derived only when exactly one canonical two-part slug
    ends in that part, among those written and the table's targets: two such slugs are two
    rules, so their shared part stays as written. A bare ``cooperation`` is left to
    ``read_as``, since its clause depends on the close.
    """
    written = set(slugs)
    by_part: dict[str, set[str]] = {}
    for slug in written | set(ALIAS_TABLE.values()):
        first, dot, part = slug.partition(".")
        if dot and first not in INSTRUCTION_SOURCES:
            by_part.setdefault(part, set()).add(slug)
    bare_canonical = catalogue | CLAUSE_SLUGS | {COOPERATION}
    aliases = {slug: (ALIAS_TABLE[slug], TABLE) for slug in written if slug in ALIAS_TABLE}
    for slug in written - aliases.keys():
        first, dot, part = slug.partition(".")
        if (dot and first not in INSTRUCTION_SOURCES) or (not dot and slug in bare_canonical):
            continue
        targets = by_part.get(part if dot else slug, set())
        if len(targets) == 1:
            aliases[slug] = (next(iter(targets)), DERIVED)
    return aliases


def miss_clause(entry: HistoryEntry) -> str | None:
    """The cooperation clause slug the close's miss tags name, or None when they name none or several."""
    named = set()
    for tag in map(_bare, entry.tags):
        for prefix in MISS_TAG_PREFIXES:
            if tag.startswith(prefix) and tag[len(prefix):] in CLAUSE_SLUGS:
                named.add(tag[len(prefix):])
    return named.pop() if len(named) == 1 else None


def read_as(slug: str, missed: bool, aliases: dict[str, tuple[str, str]], clause: str | None) -> tuple[str, str | None]:
    """The slug a row is read under, and its alias kind when that is not the slug as written.

    A miss tag names only the clause that was missed, so a held bare ``cooperation`` row
    keeps its slug.
    """
    if slug == COOPERATION:
        return (clause, CLAUSE) if missed and clause else (slug, None)
    return aliases.get(slug, (slug, None))


def read_close(record: dict, aliases: dict[str, tuple[str, str]] | None = None,
               clause: str | None = None) -> tuple[dict[str, bool], dict[str, dict[str, str]]]:
    """Each slug the record gives an outcome, read through ``aliases``, and the old spellings behind it.

    The outcome is True for a miss and False for held; a slug with both a counted miss and a
    held row in one close is a miss there, also when the two rows are spelled differently.
    The second map gives, per slug, each old spelling that gave it an outcome here and its kind.
    """
    out: dict[str, bool] = {}
    spellings: dict[str, dict[str, str]] = {}
    for finding in valid_findings(record):
        status, level = finding["status"], finding.get("level")
        if status in MISS_STATUSES and level in COUNTED_LEVELS:
            missed = True
        elif status in HELD_STATUSES:
            missed = False
        else:
            continue
        slug, kind = read_as(finding["slug"], missed, aliases or {}, clause)
        out[slug] = out.get(slug, False) or missed
        if kind is not None:
            spellings.setdefault(slug, {})[finding["slug"]] = kind
    return out, spellings


def outcomes(record: dict) -> dict[str, bool]:
    """Each slug the record gives an outcome, as written: True for a miss, False for held."""
    return read_close(record)[0]


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


def summarize(entries: list[HistoryEntry], now: float | None = None,
              catalogue: frozenset[str] | None = None) -> dict:
    """The closes among ``entries``, how many carry a report record, and one row per canonical slug.

    Rows come highest recent miss rate first. ``recent`` is None for a slug whose closes
    all lack a readable time: they still count, but do not move the rate. ``merged`` lists
    the old spellings read under the row's slug, most closes first, and is empty for a row
    whose closes all wrote that slug. ``catalogue`` defaults to the flow's catalogue slugs.
    """
    now = dt.datetime.now(dt.timezone.utc).timestamp() if now is None else now
    catalogue = catalogue_slugs() if catalogue is None else catalogue
    closes = sorted((entry for entry in entries if is_close_report(entry)),
                    key=lambda entry: (_epoch(entry.timestamp) or float("-inf"), entry.id))
    reports = [(entry, record) for entry in closes if (record := report_record(entry)) is not None]
    aliases = derive_aliases((finding["slug"] for _, record in reports for finding in valid_findings(record)),
                             catalogue)
    rows: dict[str, dict] = {}
    estimates: dict[str, object] = {}
    for entry, record in reports:
        at = _epoch(entry.timestamp)
        read, spellings = read_close(record, aliases, miss_clause(entry))
        for slug, missed in read.items():
            row = rows.setdefault(slug, {"slug": slug, "closes": 0, "misses": 0, "merged": {}})
            row["closes"] += 1
            row["misses"] += int(missed)
            for spelling, kind in spellings.get(slug, {}).items():
                merged = row["merged"].setdefault(spelling, {"slug": spelling, "kind": kind, "closes": 0})
                merged["closes"] += 1
            if at is not None:
                # The filtered outcome is the miss, so the estimate is a miss rate.
                estimates[slug] = rate.observe(estimates.get(slug) or rate.prior(at), missed, at, HALF_LIFE_DAYS)
    for slug, row in rows.items():
        row["recent"] = _recent(estimates[slug], now) if slug in estimates else None
        row["merged"] = sorted(row.pop("merged").values(), key=lambda merged: (-merged["closes"], merged["slug"]))
    ordered = sorted(rows.values(), key=lambda row: (-(row["recent"]["rate"] if row["recent"] else -1.0),
                                                     -row["misses"], row["slug"]))
    return {"closes": len(closes), "with_record": len(reports), "half_life_days": HALF_LIFE_DAYS, "rules": ordered}


def read_entries(workspace: Path) -> list[HistoryEntry]:
    """This checkout's journal, a pending rotation and the archives included, one entry per id.

    The journal alone: the segments the user library sync adds to ``read_history`` stay out
    even in a process that has the sync installed.
    """
    reader = HistoryReader(str(workspace / JOURNAL))
    return merge_entries([(None, reader.journal_entries())])


def has_journal(workspace: Path, entries: list[HistoryEntry]) -> bool:
    """A live journal file, even an empty one, or archives the reader found entries in.

    A ``history/`` directory alone proves nothing: other projects use that name too.
    """
    return bool(entries) or (workspace / JOURNAL).is_file() or (workspace / PENDING_ROTATION).is_file()


def _cells(recent: dict | None) -> tuple[str, str]:
    if not recent:
        return "-", "-"
    return f"{recent['rate']:.0%}", f"{recent['low']:.0%}-{recent['high']:.0%}"


def _merged_cell(merged: list[dict]) -> str:
    return ", ".join(f"{spelling['slug']} ({spelling['closes']})" for spelling in merged)


def format_table(summary: dict) -> str:
    header = ("slug", "closes", "recent", "95% band", "misses", "merged from")
    table = [header] + [(row["slug"], str(row["closes"]), *_cells(row["recent"]), str(row["misses"]),
                         _merged_cell(row["merged"])) for row in summary["rules"]]
    widths = [max(len(line[column]) for line in table) for column in range(len(header))]
    lines = [f"{summary['closes']} closes, {summary['with_record']} with a report record; "
             f"half-life {summary['half_life_days']:g} days."]
    lines += ["  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip() for line in table]
    if any(row["merged"] for row in summary["rules"]):
        lines.append('"merged from": old spellings read under the row\'s slug, with their closes; '
                     "an inference, not a recorded slug.")
    lines.append('Evidence only: escalation follows the "Repeats escalate" step of flows/thread-close.md.')
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, default=Path.cwd(),
                        help="the repository whose journal to read (default: the current directory)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    entries = read_entries(workspace)
    if not has_journal(workspace, entries):
        parser.error(f"no journal in {workspace}: no {JOURNAL}, no {PENDING_ROTATION} and no entry under {ARCHIVES}/")
    try:
        catalogue = catalogue_slugs()
    except (OSError, ValueError) as error:
        parser.error(f"cannot read the catalogue slugs the alias map keeps canonical: {error}")
    summary = summarize(entries, catalogue=catalogue)
    print(json.dumps({"workspace": str(workspace), **summary}, indent=2) if args.json else format_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
