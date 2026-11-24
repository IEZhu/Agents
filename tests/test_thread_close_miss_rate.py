"""The per-rule miss-rate report over thread-close records (scripts/thread_close_miss_rate.py)."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import thread_close_miss_rate as report
from src.memory import history

SCRIPT = Path(report.__file__).resolve()
NOW = dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc).timestamp()


def _block(entry_id: str, timestamp: str, tags: tuple, record: dict | None = None) -> str:
    """One journal entry as the history writer renders it."""
    lines = ["", f"## {timestamp} | {entry_id}", f"**Intent:** {entry_id}", "**Action:** a", "**Outcome:** o",
             "**Tags:** " + " ".join(tags)]
    if record is not None:
        lines.append("**Meta:** " + json.dumps({"record": record}))
    return "\n".join(lines) + "\n"


def _report(*findings: dict) -> dict:
    return {"v": 1, "kind": "report", "findings": list(findings)}


def _miss(slug: str, level: str = "V2", status: str = "missed") -> dict:
    return {"slug": slug, "status": status, "cause": "ignored", "level": level,
            "miss_at": "2026-10-01T00:00:00+00:00"}


@pytest.fixture
def workspace(tmp_path):
    """Two closes with records three weeks apart, one in an archive, and entries that must not count."""
    (tmp_path / "history").mkdir()
    (tmp_path / "history" / "2026-10.md").write_text(_block(
        "aaaaaaaaaaa1", "2026-10-20T10:00:00+00:00", ("#thread-close", "#session-a"),
        _report(_miss("cooperation"), {"slug": "log", "status": "used"}, _miss("flows", level="V0"),
                {"slug": "show-pane", "status": "unavailable"})), encoding="utf-8")
    (tmp_path / "history.md").write_text("---\nrepo: r\n---\n" + "".join([
        # A progress entry of the next close: its record is not the close's.
        _block("bbbbbbbbbbb2", "2026-11-10T09:00:00+00:00", ("#thread-close", "#session-b", "#progress"),
               _report(_miss("log"), _miss("cooperation"))),
        _block("ccccccccccc3", "2026-11-10T10:00:00+00:00", ("#thread-close", "#session-b"),
               _report({"slug": "cooperation", "status": "caught"}, _miss("log", level="V1"), _miss("flows"))),
        # A close from before records existed, and an entry that is not a close's report.
        _block("ddddddddddd4", "2026-11-12T10:00:00+00:00", ("#thread-close", "#session-c")),
        _block("eeeeeeeeeee5", "2026-11-12T11:00:00+00:00", ("#thread-close", "#session-c"),
               {"v": 1, "kind": "patch-apply", "findings": [_miss("log")]}),
        # Another tag that only starts like the close tag.
        _block("fffffffffff6", "2026-11-13T10:00:00+00:00", ("#thread-closed",), _report(_miss("log"))),
    ]), encoding="utf-8")
    return tmp_path


class TestOutcomes:
    def test_a_miss_at_v1_or_above_counts_and_used_or_caught_holds(self):
        found = report.outcomes(_report(
            _miss("a", "V1"), _miss("b", "V4", status="misused"), {"slug": "c", "status": "used"},
            {"slug": "d", "status": "caught"}))
        assert found == {"a": True, "b": True, "c": False, "d": False}

    @pytest.mark.parametrize("finding", [
        _miss("x", "V0"), _miss("x", "refuted"), _miss("x", "stale"), _miss("x", "contested"),
        {"slug": "x", "status": "missed"}, {"slug": "x", "status": "unavailable"}, {"slug": "x", "status": "n/a"},
        {"slug": "x", "status": "uncatalogued", "level": "V2"}, {"slug": "x", "status": "no-rule"},
    ])
    def test_every_other_row_gives_no_outcome(self, finding):
        assert report.outcomes(_report(finding)) == {}

    def test_a_counted_miss_outweighs_a_held_row_of_the_same_close(self):
        assert report.outcomes(_report({"slug": "x", "status": "used"}, _miss("x"))) == {"x": True}
        assert report.outcomes(_report(_miss("x"), {"slug": "x", "status": "caught"})) == {"x": True}

    def test_malformed_findings_are_skipped(self):
        assert report.outcomes({"findings": {"slug": "x"}}) == {}
        assert report.outcomes({"findings": ["x", {"status": "missed", "level": "V2"}, {"slug": 3}]}) == {}

    @pytest.mark.parametrize("finding", [
        {"slug": "x", "status": [], "level": "V2"}, {"slug": "x", "status": {"s": 1}},
        {"slug": "x", "status": "missed", "level": ["V2"]}, {"slug": "x", "status": "missed", "level": {}},
        {"slug": "x", "status": "used", "level": []}, {"slug": "x", "status": "caught", "level": 2},
        {"slug": "x", "status": "used", "level": None}, {"slug": "x", "status": "missed", "level": "V9"},
        {"slug": "Not A Slug", "status": "used"}, {"slug": "x", "status": "used", "note": "free text"},
    ])
    def test_a_row_the_record_schema_refuses_is_skipped_not_raised(self, finding):
        assert report.outcomes(_report(finding, {"slug": "y", "status": "used"})) == {"y": False}


class TestSummary:
    def test_only_close_reports_count_and_archives_are_read(self, workspace):
        summary = report.summarize(report.read_entries(workspace), now=NOW)
        assert (summary["closes"], summary["with_record"]) == (4, 2)
        assert [(row["slug"], row["closes"], row["misses"]) for row in summary["rules"]] == [
            ("flows", 1, 1), ("log", 2, 1), ("cooperation", 2, 1)]

    def test_the_recent_rate_weighs_the_later_close_more(self, workspace):
        """Equal all-time misses, opposite order: the slug whose miss is recent ranks above.

        Hand computation from the uniform prior (strength 2): a first outcome moves the mean by
        a third of the way to it, to 2/3 or 1/3 at strength 3. Three weeks are 1.5 half-lives,
        which would fade the strength below 2, so it stops at 2; the second outcome moves the
        mean by a third again, and time alone never moves a mean.
        """
        rows = {row["slug"]: row["recent"] for row in report.summarize(report.read_entries(workspace), now=NOW)["rules"]}
        assert rows["log"]["rate"] == round(5 / 9, 3)
        assert rows["cooperation"]["rate"] == round(4 / 9, 3)
        assert rows["flows"]["rate"] == round(2 / 3, 3)
        for recent in rows.values():
            assert 0.0 <= recent["low"] < recent["rate"] < recent["high"] <= 1.0

    def test_closes_at_one_instant_give_the_beta_posterior_mean(self, tmp_path):
        stamp = "2026-11-20T10:00:00+00:00"
        (tmp_path / "history.md").write_text("".join(
            _block(f"{index:012x}", stamp, ("#thread-close",), _report(finding))
            for index, finding in enumerate([_miss("x"), _miss("x"), {"slug": "x", "status": "used"}], start=1)),
            encoding="utf-8")
        (row,) = report.summarize(report.read_entries(tmp_path), now=NOW)["rules"]
        # Two misses and one hold on Beta(1, 1): (1 + 2) / (2 + 3).
        assert (row["closes"], row["misses"], row["recent"]["rate"]) == (3, 2, 0.6)


CATALOGUE = report.catalogue_slugs()


def _entry(*tags: str) -> history.HistoryEntry:
    return history.HistoryEntry(id="aaaaaaaaaaa1", timestamp="2026-11-20T10:00:00+00:00", intent="i", action="a",
                                outcome="o", tags=list(tags))


def _journal(tmp_path, *blocks: str) -> Path:
    (tmp_path / report.JOURNAL).write_text("---\nrepo: r\n---\n" + "".join(blocks), encoding="utf-8")
    return tmp_path


def _rows(workspace) -> dict:
    return {row["slug"]: row for row in report.summarize(report.read_entries(workspace), now=NOW)["rules"]}


class TestDerivedAliases:
    def test_a_second_part_alone_or_after_a_source_reads_as_the_one_canonical_slug(self):
        aliases = report.derive_aliases(
            ["ccd-session.mark-chapter", "mark-chapter", "claude-md.mark-chapter", "rule.link2action", "link2action",
             "agents-md.link2action", "memory.link2action"], CATALOGUE)
        assert aliases == {
            "mark-chapter": ("ccd-session.mark-chapter", "derived"),
            "claude-md.mark-chapter": ("ccd-session.mark-chapter", "derived"),
            "link2action": ("rule.link2action", "derived"),
            "agents-md.link2action": ("rule.link2action", "derived"),
            "memory.link2action": ("rule.link2action", "derived"),
        }

    def test_a_spelling_whose_canonical_slug_no_close_wrote_stays(self):
        assert report.derive_aliases(["scratchpad", "claude-md.built-in-browser-first", "language-match"],
                                     CATALOGUE) == {}

    def test_a_part_two_canonical_slugs_share_is_two_rules_and_stays(self):
        """Another server's tool of the same name is another rule: neither absorbs the other or the part."""
        assert report.derive_aliases(["ccd-session.mark-chapter", "other.mark-chapter", "mark-chapter",
                                      "claude-md.mark-chapter"], CATALOGUE) == {}

    def test_a_slug_with_more_than_one_dot_is_neither_a_target_nor_derived(self):
        """The schema accepts ``foo.bar.baz``; step 4.2 never writes it, so nothing merges through it."""
        assert report.derive_aliases(["foo.bar.baz", "claude-md.bar.baz", "bar.baz", "rule.x", "claude-md.rule.x",
                                      "memory.a.x"], CATALOGUE) == {}

    def test_a_bare_catalogue_slug_or_cooperation_clause_is_canonical_as_it_stands(self):
        written = ["rule.history", "history", "ccd-x.log", "log", "rule.cooperation-claim", "cooperation-claim",
                   "rule.cooperation", "cooperation"]
        assert {"history", "log", "cooperation"} <= CATALOGUE
        assert report.derive_aliases(written, CATALOGUE) == {}

    def test_the_table_maps_what_no_rule_derives_and_its_targets_anchor_derivation(self):
        aliases = report.derive_aliases(["task-list", "claude-md.task-list-first-call",
                                         "claude-md.websearch-standard", "update-plan", "websearch"], CATALOGUE)
        assert aliases == {
            "task-list": ("ccd-turn.update-plan", "table"),
            "claude-md.task-list-first-call": ("ccd-turn.update-plan", "table"),
            "claude-md.websearch-standard": ("client.websearch", "table"),
            # Neither target was written here, but the table names both as canonical.
            "update-plan": ("ccd-turn.update-plan", "derived"),
            "websearch": ("client.websearch", "derived"),
        }

    def test_every_table_target_is_a_canonical_two_part_slug(self):
        for target in report.ALIAS_TABLE.values():
            first, dot, part = target.partition(".")
            assert dot and part and "." not in part and first not in report.INSTRUCTION_SOURCES, target


class TestCooperationClause:
    @pytest.mark.parametrize("tags, clause", [
        (("#thread-close", "#ac-missed-cooperation-claim"), "cooperation-claim"),
        (("tool-missed-cooperation-request",), "cooperation-request"),
        (("#ac-missed-cooperation-agreement", "#tool-missed-cooperation-agreement", "#ac-missed-log"),
         "cooperation-agreement"),
        (("#ac-missed-cooperation-claim", "#ac-missed-cooperation-request"), None),
        (("#ac-missed-cooperation",), None),
        (("#ac-missed-cooperation-handoff",), None),
        (("#cooperation-claim", "#used-cooperation-claim"), None),
    ])
    def test_the_miss_tags_name_one_clause_or_none(self, tags, clause):
        assert report.miss_clause(_entry(*tags)) == clause

    def test_only_a_bare_cooperation_miss_takes_the_clause(self):
        assert report.read_as("cooperation", True, {}, "cooperation-claim") == ("cooperation-claim", "clause")
        # A miss tag names the clause that was missed, never the one that held.
        assert report.read_as("cooperation", False, {}, "cooperation-claim") == ("cooperation", None)
        assert report.read_as("cooperation", True, {}, None) == ("cooperation", None)


class TestAliasesInTheReport:
    def test_old_spellings_merge_into_the_canonical_row_and_are_listed(self, tmp_path):
        workspace = _journal(
            tmp_path,
            _block("aaaaaaaaaaa1", "2026-11-18T10:00:00+00:00", ("#thread-close",),
                   _report(_miss("mark-chapter"), {"slug": "claude-md.mark-chapter", "status": "used"},
                           _miss("link2action"))),
            _block("bbbbbbbbbbb2", "2026-11-19T10:00:00+00:00", ("#thread-close",),
                   _report(_miss("claude-md.mark-chapter", level="V1"))),
            _block("ccccccccccc3", "2026-11-24T10:00:00+00:00", ("#thread-close",),
                   _report({"slug": "ccd-session.mark-chapter", "status": "used"},
                           {"slug": "rule.link2action", "status": "unavailable"})))
        rows = _rows(workspace)
        assert set(rows) == {"ccd-session.mark-chapter", "rule.link2action"}
        chapter = rows["ccd-session.mark-chapter"]
        # Two spellings in the first close give it one outcome, and the miss outweighs the hold.
        assert (chapter["closes"], chapter["misses"]) == (3, 2)
        assert chapter["merged"] == [{"slug": "claude-md.mark-chapter", "kind": "derived", "closes": 2},
                                     {"slug": "mark-chapter", "kind": "derived", "closes": 1}]
        # A canonical slug written only on a row without an outcome still anchors its spellings.
        assert (rows["rule.link2action"]["closes"], rows["rule.link2action"]["misses"]) == (1, 1)
        assert rows["rule.link2action"]["merged"] == [{"slug": "link2action", "kind": "derived", "closes": 1}]

    def test_a_bare_cooperation_miss_takes_its_close_clause_and_the_rest_stays(self, tmp_path):
        workspace = _journal(
            tmp_path,
            _block("aaaaaaaaaaa1", "2026-11-18T10:00:00+00:00", ("#thread-close", "#ac-missed-cooperation-request"),
                   _report({"slug": "cooperation", "status": "used"}, _miss("cooperation"))),
            _block("bbbbbbbbbbb2", "2026-11-19T10:00:00+00:00", ("#thread-close", "#ac-missed-cooperation"),
                   _report(_miss("cooperation"))),
            _block("ccccccccccc3", "2026-11-20T10:00:00+00:00",
                   ("#thread-close", "#ac-missed-cooperation-claim", "#ac-missed-cooperation-ack"),
                   _report(_miss("cooperation", status="misused"))),
            _block("ddddddddddd4", "2026-11-21T10:00:00+00:00", ("#thread-close", "#ac-missed-cooperation-claim"),
                   _report(_miss("cooperation", status="misused"), {"slug": "cooperation-claim", "status": "used"})))
        rows = _rows(workspace)
        assert {slug: (row["closes"], row["misses"]) for slug, row in rows.items()} == {
            "cooperation-request": (1, 1), "cooperation-claim": (1, 1), "cooperation": (3, 2)}
        assert rows["cooperation-request"]["merged"] == [{"slug": "cooperation", "kind": "clause", "closes": 1}]
        assert rows["cooperation-claim"]["merged"] == [{"slug": "cooperation", "kind": "clause", "closes": 1}]
        assert rows["cooperation"]["merged"] == []

    def test_the_table_and_json_mark_a_merged_row(self, tmp_path, capsys):
        workspace = _journal(
            tmp_path,
            _block("aaaaaaaaaaa1", "2026-11-18T10:00:00+00:00", ("#thread-close",),
                   _report(_miss("task-list"), _miss("routing"))),
            _block("bbbbbbbbbbb2", "2026-11-19T10:00:00+00:00", ("#thread-close",),
                   _report(_miss("update-plan"), {"slug": "ccd-turn.update-plan", "status": "used"})))
        assert report.main(["--workspace", str(workspace), "--json"]) == 0
        rows = {row["slug"]: row for row in json.loads(capsys.readouterr().out)["rules"]}
        assert rows["ccd-turn.update-plan"]["merged"] == [{"slug": "task-list", "kind": "table", "closes": 1},
                                                          {"slug": "update-plan", "kind": "derived", "closes": 1}]
        assert rows["routing"]["merged"] == []
        assert report.main(["--workspace", str(workspace)]) == 0
        lines = capsys.readouterr().out.splitlines()
        (merged,) = [line for line in lines if line.startswith("ccd-turn.update-plan ")]
        assert merged.endswith("task-list (1), update-plan (1)")
        (routing,) = [line for line in lines if line.startswith("routing ")]
        assert len(routing.split()) == 5
        assert lines[-2].startswith('"merged from":') and "inference" in lines[-2]
        assert lines[-1].startswith("Evidence only:")

    def test_reading_never_writes_the_journal(self, workspace, capsys):
        (workspace / "history.md").write_text((workspace / "history.md").read_text(encoding="utf-8") + _block(
            "777777777777", "2026-11-14T10:00:00+00:00", ("#thread-close", "#ac-missed-cooperation-claim"),
            _report(_miss("mark-chapter"), _miss("cooperation"), {"slug": "ccd-session.mark-chapter",
                                                                   "status": "used"})), encoding="utf-8")
        def journal():
            # The reader's empty lock file holds no entry, so it is not part of the journal.
            return {path: path.read_bytes() for path in workspace.rglob("*")
                    if path.is_file() and not path.name.endswith(".lock")}

        before = journal()
        assert report.main(["--workspace", str(workspace), "--json"]) == 0
        rows = {row["slug"]: row for row in json.loads(capsys.readouterr().out)["rules"]}
        # The aliases were applied, so the comparison below covers a read that merged.
        assert {"slug": "mark-chapter", "kind": "derived", "closes": 1} in rows["ccd-session.mark-chapter"]["merged"]
        assert rows["cooperation-claim"]["merged"] == [{"slug": "cooperation", "kind": "clause", "closes": 1}]
        assert journal() == before and {path.name for path in before} == {"history.md", "2026-10.md"}


class TestCatalogue:
    def test_the_flow_catalogue_is_read_and_holds_no_dotted_slug(self):
        assert {"routing", "log", "history", "flows", "cooperation", "mcp-proxy"} <= CATALOGUE
        assert [slug for slug in CATALOGUE if "." in slug or slug.startswith("cooperation-")] == []

    def test_the_named_sources_and_clauses_are_the_flows_own(self):
        text = " ".join(report.FLOW.read_text(encoding="utf-8").split())
        sources = ", ".join(f"`{source}`" for source in ("claude-md", "agents-md", "memory"))
        assert f"The instruction's source ({sources}, a hook's file) never enters a slug" in text
        assert report.INSTRUCTION_SOURCES == {"claude-md", "agents-md", "memory"}
        assert "its clauses `register`, `claim`, `ack`, `request` and `agreement`" in text
        assert report.COOPERATION_CLAUSES == {"register", "claim", "ack", "request", "agreement"}

    def test_a_flow_without_the_catalogue_stops_the_report(self, workspace, tmp_path, monkeypatch, capsys):
        flow = tmp_path / "thread-close.md"
        flow.write_text("# A flow\n\nNo catalogue here.\n", encoding="utf-8")
        with pytest.raises(ValueError):
            report.catalogue_slugs(flow)
        monkeypatch.setattr(report, "FLOW", tmp_path / "missing.md")
        with pytest.raises(SystemExit) as stopped:
            report.main(["--workspace", str(workspace)])
        assert stopped.value.code == 2
        assert "catalogue" in capsys.readouterr().err


class TestJournalScope:
    def test_an_entry_in_both_a_pending_rotation_and_history_md_counts_once(self, tmp_path):
        close = _block("aaaaaaaaaaa1", "2026-11-20T10:00:00+00:00", ("#thread-close",), _report(_miss("x")))
        (tmp_path / report.PENDING_ROTATION).write_text(close, encoding="utf-8")
        (tmp_path / report.JOURNAL).write_text(close, encoding="utf-8")
        summary = report.summarize(report.read_entries(tmp_path), now=NOW)
        assert (summary["closes"], summary["rules"][0]["closes"]) == (1, 1)

    def test_a_synced_segment_stays_out_even_with_the_sync_installed(self, workspace, tmp_path):
        segment = tmp_path / "segment-2026-11.md"
        segment.write_text(_block("99999999999a", "2026-11-14T10:00:00+00:00", ("#thread-close",),
                                  _report(_miss("synced-only"))) + "**Machine:** machine-b\n", encoding="utf-8")

        class Sync:
            def machines(self, history_path):
                return history.MachineHistory("this-mac", (("machine-b", str(segment)),))

            def appended(self, history_path, entry_id):
                pass

        previous = history.set_sync(Sync())
        try:
            # The merged read does see the segment, so the exclusion below is not vacuous.
            merged = history.HistoryReader(str(workspace / report.JOURNAL)).read_recent(limit=50)
            assert "99999999999a" in {entry.id for entry in merged}
            rows = report.summarize(report.read_entries(workspace), now=NOW)["rules"]
        finally:
            history.set_sync(previous)
        assert "synced-only" not in {row["slug"] for row in rows}


class TestCommandLine:
    def test_json_names_the_workspace_and_every_row(self, workspace, capsys):
        assert report.main(["--workspace", str(workspace), "--json"]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed["workspace"] == str(workspace.resolve())
        assert printed["half_life_days"] == report.HALF_LIFE_DAYS
        assert [row["slug"] for row in printed["rules"]] == ["flows", "log", "cooperation"]
        assert [row["merged"] for row in printed["rules"]] == [[], [], []]

    def test_the_table_says_it_is_evidence_only(self, workspace, capsys):
        assert report.main(["--workspace", str(workspace)]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "4 closes, 2 with a report record; half-life 14 days."
        assert lines[1].split() == ["slug", "closes", "recent", "95%", "band", "misses", "merged", "from"]
        assert lines[2].split()[:3] == ["flows", "1", "67%"]
        assert lines[-1].startswith("Evidence only:") and "Repeats escalate" in lines[-1]
        # Nothing here was merged, so no row carries a spelling and no note explains one.
        assert not any(line.startswith('"merged from"') for line in lines)
        assert all(len(line.split()) == 5 for line in lines[2:-1])

    def test_a_pending_rotation_alone_is_a_journal(self, tmp_path, capsys):
        (tmp_path / report.PENDING_ROTATION).write_text(
            _block("aaaaaaaaaaa1", "2026-11-20T10:00:00+00:00", ("#thread-close",), _report(_miss("x"))),
            encoding="utf-8")
        assert report.main(["--workspace", str(tmp_path), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["with_record"] == 1

    @pytest.mark.parametrize("unrelated", [(), ("notes.md", "2026-11-draft.md")])
    def test_a_workspace_without_a_journal_is_an_error_not_an_empty_report(self, tmp_path, capsys, unrelated):
        if unrelated:
            # Another project's own history/ folder, holding no journal archive.
            (tmp_path / report.ARCHIVES).mkdir()
            for name in unrelated:
                (tmp_path / report.ARCHIVES / name).write_text("# not a journal\n", encoding="utf-8")
        with pytest.raises(SystemExit) as stopped:
            report.main(["--workspace", str(tmp_path)])
        assert stopped.value.code == 2
        assert "no journal" in capsys.readouterr().err

    def test_an_empty_live_journal_is_a_journal(self, tmp_path, capsys):
        (tmp_path / report.JOURNAL).write_text("---\nrepo: r\n---\n", encoding="utf-8")
        assert report.main(["--workspace", str(tmp_path), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["closes"] == 0

    def test_the_script_runs_from_another_directory(self, workspace, tmp_path):
        done = subprocess.run([sys.executable, str(SCRIPT), "--workspace", str(workspace), "--json"],
                              cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["with_record"] == 2
