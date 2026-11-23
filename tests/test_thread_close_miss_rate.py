"""The per-rule miss-rate report over thread-close records (scripts/thread_close_miss_rate.py)."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import thread_close_miss_rate as report

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


class TestCommandLine:
    def test_json_names_the_workspace_and_every_row(self, workspace, capsys):
        assert report.main(["--workspace", str(workspace), "--json"]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed["workspace"] == str(workspace.resolve())
        assert printed["half_life_days"] == report.HALF_LIFE_DAYS
        assert [row["slug"] for row in printed["rules"]] == ["flows", "log", "cooperation"]

    def test_the_table_says_it_is_evidence_only(self, workspace, capsys):
        assert report.main(["--workspace", str(workspace)]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "4 closes, 2 with a report record; half-life 14 days."
        assert lines[1].split() == ["slug", "closes", "recent", "95%", "band", "misses"]
        assert lines[2].split()[:3] == ["flows", "1", "67%"]
        assert lines[-1].startswith("Evidence only:") and "Repeats escalate" in lines[-1]

    def test_a_workspace_without_a_journal_is_an_error_not_an_empty_report(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as stopped:
            report.main(["--workspace", str(tmp_path)])
        assert stopped.value.code == 2
        assert "no journal" in capsys.readouterr().err

    def test_the_script_runs_from_another_directory(self, workspace, tmp_path):
        done = subprocess.run([sys.executable, str(SCRIPT), "--workspace", str(workspace), "--json"],
                              cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["with_record"] == 2
