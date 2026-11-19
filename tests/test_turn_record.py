"""The closed v1 schema of a turn's structured record (#584)."""
import json

import pytest

from src.memory import record
from src.schemas import tool_args

FULL = {
    "v": 1, "kind": "report", "session": "local_0a1b2c3d-4e5f", "close_ordinal": 2,
    "findings": [{"slug": "delegation", "status": "missed", "cause": "ignored",
                  "miss_at": "2026-11-19T14:24:14+01:00", "level": "V2"},
                 {"slug": "cooperation", "status": "used"}, {"slug": "show-pane", "status": "caught"}],
    "patches": [{"id": "1", "layer": "hook", "slug": "delegation", "state": "applied",
                 "applied_at": "2026-11-19T13:30:00Z"}],
    "verifier": {"claims": 12, "confirmed": 9, "refuted": 1, "unconfirmed": 2, "invalid_rounds": 0},
    "prep_failures": 0, "unexecutable": [{"step": "5.3", "code": "stale_generation"}],
    "duration_s": 5400, "responses": 211, "output_tokens": 180_000,
}


def test_a_full_record_is_kept_with_its_times_in_utc():
    stored, problems = record.validate(FULL)
    assert problems == []
    assert stored["findings"][0]["miss_at"] == "2026-11-19T13:24:14+00:00"
    assert stored["patches"][0]["applied_at"] == "2026-11-19T13:30:00+00:00"
    assert {**stored, "findings": FULL["findings"], "patches": FULL["patches"]} == FULL
    assert record.size(stored) <= record.MAX_BYTES


@pytest.mark.parametrize("given,stored", [
    ("0001-01-01T00:00:00+00:00", "0001-01-01T00:00:00+00:00"),
    ("0001-01-01T00:00:00-01:00", "0001-01-01T01:00:00+00:00"),
    ("9999-12-31T23:59:59+01:00", "9999-12-31T22:59:59+00:00"),
])
def test_times_at_the_edges_of_the_range_still_convert(given, stored):
    record_, problems = record.validate({"v": 1, "kind": "report", "findings": [
        {"slug": "x", "status": "missed", "miss_at": given}]})
    assert problems == [] and record_["findings"][0]["miss_at"] == stored


def test_the_smallest_record_is_a_version_and_a_kind():
    assert record.validate({"v": 1, "kind": "status"}) == ({"v": 1, "kind": "status"}, [])


@pytest.mark.parametrize("value,where", [
    ({"v": 1, "kind": "summary"}, "record.kind"),
    ({"v": 1, "kind": "report", "close_ordinal": 0}, "record.close_ordinal"),
    ({"v": 1, "kind": "report", "close_ordinal": True}, "record.close_ordinal"),
    ({"v": 1, "kind": "report", "duration_s": 1.5}, "record.duration_s"),
    ({"v": 1, "kind": "report", "responses": "211"}, "record.responses"),
    ({"v": 1, "kind": "report", "session": "Session One"}, "record.session"),
    ({"v": 1, "kind": "report", "findings": [{"slug": "x", "status": "skipped"}]}, "record.findings[0].status"),
    ({"v": 1, "kind": "report", "findings": [{"slug": "x", "status": "missed", "level": "high"}]},
     "record.findings[0].level"),
    ({"v": 1, "kind": "report", "findings": [{"status": "missed"}]}, "record.findings[0].slug"),
    ({"v": 1, "kind": "report", "findings": [{"slug": "x", "status": "missed",
                                              "miss_at": "2026-11-19T13:24:14"}]}, "record.findings[0].miss_at"),
    # Valid ISO 8601 that leaves datetime's range once moved to UTC.
    ({"v": 1, "kind": "report", "findings": [{"slug": "x", "status": "missed",
                                              "miss_at": "0001-01-01T00:00:00+01:00"}]}, "record.findings[0].miss_at"),
    ({"v": 1, "kind": "report", "patches": [{"layer": "hook", "slug": "x", "state": "applied",
                                             "applied_at": "9999-12-31T23:59:59-01:00"}]}, "record.patches[0].applied_at"),
    ({"v": 1, "kind": "report", "patches": [{"layer": "wiki", "slug": "x", "state": "applied"}]},
     "record.patches[0].layer"),
    ({"v": 1, "kind": "report", "verifier": {"claims": -1}}, "record.verifier.claims"),
    ({"v": 1, "kind": "report", "unexecutable": [{"step": "4"}]}, "record.unexecutable[0].code"),
    ({"v": 1, "kind": "report", "findings": {"slug": "x"}}, "record.findings"),
    ({"v": 1}, "record.kind"),
])
def test_a_value_outside_its_rule_is_refused_by_place(value, where):
    stored, problems = record.validate(value)
    assert stored is None
    assert any(problem.startswith(where + ":") for problem in problems), problems


@pytest.mark.parametrize("text", [
    "/Users/someone/repos/project", "C:\\work\\project", "~/notes", "a free sentence", "../up",
    "UPPER", "a" * 65, "dash-", "-dash", "two..dots",
])
def test_paths_and_free_text_are_not_slugs(text):
    stored, problems = record.validate({"v": 1, "kind": "report", "session": text})
    assert stored is None and problems[0].startswith("record.session:")
    assert text not in json.dumps(problems)


def test_a_slug_that_looks_like_a_credential_is_refused_without_echoing_it():
    token = "ghp_" + "a1" * 18
    stored, problems = record.validate({"v": 1, "kind": "report", "session": token})
    assert stored is None and "credential" in problems[0]
    assert token not in json.dumps(problems)


def test_unknown_keys_are_counted_not_named():
    secret_name = "/home/someone/.ssh/id_ed25519"
    stored, problems = record.validate({"v": 1, "kind": "report", secret_name: 1, "note": "free text"})
    assert stored is None
    assert problems == ["record: 2 key(s) not in schema v1; allowed: " + ", ".join(record.SCHEMA_V1.fields)]
    nested, problems = record.validate({"v": 1, "kind": "report", "findings": [{"slug": "x", "status": "used",
                                                                                 "note": "free"}]})
    assert nested is None and problems[0].startswith("record.findings[0]: 1 key(s)")


@pytest.mark.parametrize("value", [None, "text", [], 7])
def test_a_record_is_an_object(value):
    assert record.validate(value)[0] is None


@pytest.mark.parametrize("version", [None, 0, 2, "1", True])
def test_only_version_one_is_read(version):
    value = {"kind": "report", "anything": 1} if version is None else {"v": version, "kind": "report", "anything": 1}
    assert record.validate(value) == (None, ["record.v: required, 1 (the only schema version)"])


def test_a_record_over_the_byte_cap_is_refused():
    findings = [{"slug": f"tool-{n:02d}", "status": "missed", "cause": "not-in-context",
                 "miss_at": "2026-11-19T13:24:14+00:00", "level": "V2"} for n in range(32)]
    patches = [{"id": str(n), "layer": "client-instruction", "slug": f"tool-{n:02d}", "state": "proposed"}
               for n in range(32)]
    stored, problems = record.validate({"v": 1, "kind": "report", "findings": findings, "patches": patches})
    assert stored is None
    assert problems[0].startswith("record: ") and f"at most {record.MAX_BYTES}" in problems[0]


def test_problems_are_capped():
    findings = [{"slug": "BAD", "status": "bad", "level": "bad"} for _ in range(30)]
    stored, problems = record.validate({"v": 1, "kind": "report", "findings": findings})
    assert stored is None and len(problems) == record.MAX_PROBLEMS + 1
    assert problems[-1] == f"... and {90 - record.MAX_PROBLEMS} more"


def test_the_advertised_properties_are_the_schema_fields():
    assert list(tool_args.RECORD_PROPERTIES) == list(record.SCHEMA_V1.fields)
    for name in ("findings", "patches", "unexecutable"):
        assert tool_args.RECORD_PROPERTIES[name]["type"] == "array"


def test_the_description_names_every_enumerated_value():
    text = record.describe()
    for values in (record.KINDS, record.FINDING_STATUSES, record.CAUSES, record.LEVELS, record.LAYERS,
                   record.PATCH_STATES):
        assert all(value in text for value in values)
    assert str(record.MAX_BYTES) in text
