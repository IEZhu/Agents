"""Tier ledger: routing facts without task text, rotation, failure isolation, the report."""
import datetime as dt
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from src.engine import model_catalogs, subagent_ledger
from src.engine.subagent_validation import assess_result
from tests.test_subagent import prepared, report, worker_tree  # noqa: F401

SECRET_TASK = "Rename helper in classified-project-x"


@pytest.fixture(autouse=True)
def catalogs(monkeypatch):
    monkeypatch.delenv("AGENTS_SUBAGENT_TIERING", raising=False)
    monkeypatch.setattr(model_catalogs, "codex_models", lambda path=None: ["gpt-6-luna", "gpt-6.1-sol"])


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "data/subagent-tiers/ledger.jsonl"
    monkeypatch.setattr(subagent_ledger, "ledger_path", lambda: path)
    monkeypatch.setenv("AGENTS_SUBAGENT_LEDGER", "1")
    return path


def test_disabled_by_default(worker_tree, ledger, monkeypatch):
    monkeypatch.delenv("AGENTS_SUBAGENT_LEDGER")
    subagent_ledger.record_prepared(prepared(complexity="low", client="codex"))
    assert not ledger.exists()


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_records_routing_facts_without_task_text(worker_tree, ledger):
    package = prepared(complexity="low", client="codex", task=SECRET_TASK, task_id="classified-project-x")
    subagent_ledger.record_prepared(package)
    value = report(package, "failed")
    subagent_ledger.record_validation(assess_result(package.contract.model_dump(), value)[1])
    text = ledger.read_text(encoding="utf-8")
    assert "classified" not in text and "Rename" not in text
    prepared_line, validated_line = lines(ledger)
    assert prepared_line["event"] == "prepared" and prepared_line["task"] == validated_line["task"]
    assert {key: prepared_line[key] for key in ("client", "complexity", "tier", "requested_tier", "model",
                                                "reasoning_effort", "source", "floor_reasons", "attempt_id")} == {
        "client": "codex", "complexity": "low", "tier": "light", "requested_tier": "light",
        "model": "gpt-6-luna", "reasoning_effort": "medium", "source": "catalog", "floor_reasons": [],
        "attempt_id": "1"}
    assert {key: validated_line[key] for key in ("event", "valid", "status", "failed_checks", "escalation",
                                                 "escalation_reason")} == {
        "event": "validated", "valid": True, "status": "failed", "failed_checks": 0,
        "escalation": "standard", "escalation_reason": "status_failed"}
    if sys.platform != "win32":
        assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
        assert stat.S_IMODE(ledger.parent.stat().st_mode) == 0o700


def test_untiered_work_is_not_recorded(worker_tree, ledger):
    package = prepared()
    subagent_ledger.record_prepared(package)
    subagent_ledger.record_validation(assess_result(package.contract.model_dump(), report(package))[1])
    assert not ledger.exists()


def test_rotation_keeps_one_previous_file(ledger, monkeypatch):
    monkeypatch.setattr(subagent_ledger, "LEDGER_MAX_BYTES", 200)
    for index in range(6):
        subagent_ledger.append({"event": "prepared", "attempt_id": str(index)})
    rotated = ledger.with_name("ledger.jsonl.1")
    assert rotated.exists() and ledger.stat().st_size <= 200
    ids = [entry["attempt_id"] for entry in subagent_ledger.read_entries()]
    assert ids == sorted(ids, key=int) and ids[-1] == "5"


def test_write_failures_never_raise(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AGENTS_SUBAGENT_LEDGER", "1")
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setattr(subagent_ledger, "ledger_path", lambda: blocker / "ledger.jsonl")
    subagent_ledger.append({"event": "prepared"})
    assert "ledger" in caplog.text


def test_read_skips_malformed_lines(ledger):
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"event": "prepared"}\nnot json\n[1]\n', encoding="utf-8")
    assert subagent_ledger.read_entries() == [{"event": "prepared"}]


def entry(event, client="codex", tier="light", model="gpt-6-luna", **extra):
    return {"event": event, "client": client, "tier": tier, "model": model, **extra}


def test_summary_per_client_tier_and_model():
    rows = subagent_ledger.summarize([
        entry("prepared"), entry("prepared"), entry("prepared", tier="standard", model="gpt-6.1-sol"),
        entry("validated", valid=True, status="completed", escalation=None),
        entry("validated", valid=True, status="failed", escalation="standard"),
        entry("validated", valid=False, status=None, escalation="standard"),
        entry("validated", tier="standard", model="gpt-6.1-sol", valid=True, status="completed", escalation=None),
        entry("prepared", client=None, tier="inherit", model=None),
    ])
    assert rows == [
        {"client": "codex", "tier": "light", "model": "gpt-6-luna", "prepared": 2, "validated": 3,
         "accepted": 1, "invalid": 1, "escalations": {"standard": 2}, "recent_acceptance": None},
        {"client": "codex", "tier": "standard", "model": "gpt-6.1-sol", "prepared": 1, "validated": 1,
         "accepted": 1, "invalid": 0, "escalations": {}, "recent_acceptance": None},
        {"client": "unknown", "tier": "inherit", "model": "inherit", "prepared": 1, "validated": 0,
         "accepted": 0, "invalid": 0, "escalations": {}, "recent_acceptance": None},
    ]
    table = subagent_ledger.format_table(rows)
    assert "gpt-6-luna" in table and "standard=2" in table and table.count("\n") == 3


def timed(day, accepted, **extra):
    at = (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=day)).isoformat()
    outcome = {"valid": True, "status": "completed", "escalation": None} if accepted else \
        {"valid": True, "status": "failed", "escalation": "standard"}
    return entry("validated", at=at, **outcome, **extra)


def day_seconds(day):
    return (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=day)).timestamp()


def test_recent_acceptance_follows_a_change_that_the_all_time_share_hides():
    # Forty days of nearly all accepted, then two weeks of mostly escalated after a model change.
    entries = [timed(day, accepted=day % 10 != 0) for day in range(40)]
    entries += [timed(40 + day / 2, accepted=day % 4 == 0) for day in range(28)]
    [row] = subagent_ledger.summarize(entries, now=day_seconds(54))
    all_time = row["accepted"] / row["validated"]
    recent = row["recent_acceptance"]
    assert all_time > 0.6
    assert recent["rate"] < 0.45 < all_time
    assert recent["low"] <= recent["rate"] <= recent["high"]
    assert "%" in subagent_ledger.format_table([row])


def test_recent_acceptance_widens_while_no_outcome_arrives():
    entries = [timed(day, accepted=True) for day in range(30)]
    [fresh] = subagent_ledger.summarize(entries, now=day_seconds(30))
    [quiet] = subagent_ledger.summarize(entries, now=day_seconds(120))
    assert quiet["recent_acceptance"]["rate"] == fresh["recent_acceptance"]["rate"]
    width = lambda row: row["recent_acceptance"]["high"] - row["recent_acceptance"]["low"]
    assert width(quiet) > width(fresh)


def test_an_untimed_validation_counts_but_does_not_move_the_rate():
    entries = [timed(0, accepted=True), entry("validated", valid=True, status="failed", escalation="standard")]
    [row] = subagent_ledger.summarize(entries, now=day_seconds(0))
    assert row["validated"] == 2
    [alone] = subagent_ledger.summarize(entries[:1], now=day_seconds(0))
    assert row["recent_acceptance"] == alone["recent_acceptance"]


def test_report_script_prints_json(ledger):
    subagent_ledger.append(entry("prepared"))
    script = Path(__file__).resolve().parents[1] / "scripts/subagent_tiers_report.py"
    output = subprocess.run([sys.executable, str(script), "--path", str(ledger), "--json"],
                            capture_output=True, text=True, check=True).stdout
    assert json.loads(output)[0]["prepared"] == 1


def test_two_validations_of_one_contract_count_once(worker_tree, ledger):
    """A worker's self-check and the parent's validation are one outcome: the last one counts."""
    package = prepared(complexity="low", client="codex")
    subagent_ledger.record_prepared(package)
    contract = package.contract.model_dump()
    subagent_ledger.record_validation(assess_result(contract, report(package, "failed"))[1])
    subagent_ledger.record_validation(assess_result(contract, report(package))[1])
    entries = subagent_ledger.read_entries()
    assert [entry.get("contract") for entry in entries[1:]] == [package.contract_revision[:16]] * 2
    [row] = subagent_ledger.summarize(entries)
    assert (row["prepared"], row["validated"], row["accepted"], row["escalations"]) == (1, 1, 1, {})


def test_validations_of_different_or_unnamed_contracts_each_count():
    rows = subagent_ledger.summarize([
        entry("validated", contract="a" * 16, valid=True, status="completed", escalation=None),
        entry("validated", contract="b" * 16, valid=False, status=None, escalation="standard"),
        entry("validated", valid=True, status="completed", escalation=None),
        entry("validated", valid=True, status="completed", escalation=None),
        entry("validated", contract=["damaged"], valid=True, status="completed", escalation=None),
    ])
    assert (rows[0]["validated"], rows[0]["accepted"], rows[0]["invalid"]) == (5, 4, 1)
