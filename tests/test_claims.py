"""The validated-claims store: the filter's states, contradictions, decay, storage and the two tools."""
import json
import os
from pathlib import Path
import stat
import sys

import pytest

import src.server as server
from src.claims import model
from src.claims.store import (CLAIM_MAX, EVIDENCE_MAX_ITEMS, ClaimError, ClaimStore, claim_key, normalize)
from src.engine import config as engine_config
from src.estimation.kalman import SECONDS_PER_DAY

DAY = SECONDS_PER_DAY
START = 1_800_000_000.0


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, days):
        self.now += days * DAY


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return ClaimStore(tmp_path, clock=clock)


def record(store, verdict="confirmed", level="V2", **extra):
    arguments = {"subject": "src/result_size.py", "claim": "INLINE_LIMIT is 50000", "verdict": verdict,
                 "level": level, **extra}
    return store.record(**arguments)


# --- keys --------------------------------------------------------------------------------------------

def test_the_key_ignores_case_spacing_and_a_trailing_full_stop():
    assert claim_key("src/A.py", "Limit  is 5.") == claim_key("src/a.py", "limit is 5")
    assert claim_key("src/a.py", "limit is 5") != claim_key("src/a.py", "limit is 6")
    assert normalize("  Two\tWords. ") == "two words"


# --- the filter --------------------------------------------------------------------------------------

def test_one_executed_check_supports_load_bearing_reuse_but_not_critical(store):
    result = record(store, level="V2")
    claim = result["claim"]
    assert result["status"] == "recorded" and result["previous"] is None and not result["contradiction"]
    assert claim["state"] == "supported" and claim["level"] == "V2" and claim["families"] == ["execution"]
    assert claim["usable_for"] == ["routine", "load_bearing"]
    assert claim["confidence"] == pytest.approx(model.route_accuracy("V2"), abs=1e-4)


def test_a_read_alone_supports_only_routine_reuse(store):
    assert record(store, level="V1")["claim"]["usable_for"] == ["routine"]


def test_owner_acceptance_supports_critical_reuse(store):
    claim = record(store, level="V4", importance="critical")["claim"]
    assert claim["usable_for"] == ["routine", "load_bearing", "critical"]
    assert claim["importance"] == "critical"


def test_one_family_cannot_compound_past_its_common_mode_accuracy(store):
    for _ in range(12):
        claim = record(store, level="V3")["claim"]
    assert claim["confidence"] == pytest.approx(model.FAMILIES["model"], abs=1e-3)
    assert claim["confidence"] <= model.FAMILIES["model"] + 1e-9


def test_independent_families_do_compound(store):
    record(store, level="V2")
    record(store, level="V3")
    claim = record(store, level="V4")["claim"]
    assert claim["confidence"] > 0.999
    assert claim["families"] == ["execution", "model", "owner"]


def test_the_family_cap_matches_brute_force_enumeration():
    # Twelve V3 confirmations of one family, by enumerating the family reading directly.
    c, b = model.FAMILIES["model"], model.ROUTES["V3"].accuracy
    true_side = c * b ** 12 + (1 - c) * (1 - b) ** 12
    false_side = (1 - c) * b ** 12 + c * (1 - b) ** 12
    verdicts = [model.Verdict(START, "confirmed", "V3", "normal", "routine")] * 12
    assert model.assess(verdicts, START).confidence == pytest.approx(true_side / (true_side + false_side))


def test_confidence_relaxes_to_stale_at_the_revalidation_time(store, clock):
    claim = record(store, level="V2", volatility="normal")["claim"]
    found = store.get(claim["key"])
    due = found.assess(clock()).revalidate_at
    assert due is not None and due > clock()
    before = found.assess(due - DAY)
    after = found.assess(due + DAY)
    assert "load_bearing" in before.usable_for and "load_bearing" not in after.usable_for
    assert after.confidence == pytest.approx(model.NEEDS["load_bearing"].confidence, abs=0.01)
    later = found.assess(due + 365 * DAY)
    assert later.state == "stale" and 0.5 < later.confidence < 0.6


def test_the_due_date_is_the_closed_form_crossing():
    p, threshold, rate = 0.97, 0.85, 1 / 120
    days = model.days_until_below(p, threshold, rate)
    assert model.aged(p, days, rate) == pytest.approx(threshold)
    assert model.days_until_below(0.8, 0.85, rate) == 0.0


def test_volatile_claims_go_stale_sooner(store):
    normal = record(store, subject="a", claim="x", volatility="normal")["claim"]
    volatile = record(store, subject="b", claim="y", volatility="volatile")["claim"]
    assert volatile["revalidate_at"] < normal["revalidate_at"]


def test_a_refutation_against_a_confident_claim_is_a_contradiction(store, clock):
    record(store, level="V2")
    record(store, level="V3")
    clock.advance(1)
    result = record(store, verdict="refuted", level="V4")
    assert result["contradiction"] and result["predictive"] < model.SURPRISE
    assert result["previous"]["state"] == "supported"
    claim = result["claim"]
    assert claim["state"] == "contested" and claim["usable_for"] == []
    assert claim["level"] is None and claim["families"] == []
    assert claim["revalidate_at"] == claim["last_checked"]
    # A second refutation from another family is no longer surprising: the claim settles as refuted.
    settled = record(store, verdict="refuted", level="V2")
    assert not settled["contradiction"] and settled["claim"]["state"] == "refuted"


def test_a_same_family_refutation_is_blamed_on_the_route_and_still_contests(store):
    for _ in range(5):
        record(store, level="V2")
    result = record(store, verdict="refuted", level="V2")
    assert result["contradiction"]
    assert result["claim"]["confidence"] > 0.9  # one execution route erring is the likelier story


def test_a_refutation_of_a_long_unchecked_claim_is_change_not_contradiction(store, clock):
    record(store, level="V2")
    clock.advance(200)
    result = record(store, verdict="refuted", level="V2")
    assert not result["contradiction"] and result["previous"]["state"] == "stale"
    assert result["claim"]["state"] == "refuted"


def test_a_refuted_claim_also_goes_stale(store, clock):
    key = record(store, verdict="refuted", level="V2")["claim"]["key"]
    assert store.get(key).assess(clock()).state == "refuted"
    assert store.get(key).assess(clock() + 400 * DAY).state == "stale"


def test_importance_above_the_checked_level_needs_revalidation_now(store):
    claim = record(store, level="V2", importance="critical")["claim"]
    assert "critical" not in claim["usable_for"]
    assert claim["revalidate_at"] == claim["last_checked"]


def test_the_filter_matches_the_exact_hidden_markov_model_on_a_simulated_stream():
    # With one family whose readings never err (c = 1), the filter is the plain two-state forward recursion.
    import random

    random.seed(7)
    original_families = dict(model.FAMILIES)
    try:
        model.FAMILIES.update({name: 1.0 - 1e-12 for name in model.FAMILIES})
        rate, accuracy = model.VOLATILITY["normal"], model.ROUTES["V2"].accuracy
        p, at, verdicts = 0.5, 0.0, []
        for _ in range(40):
            gap = random.uniform(0, 30)
            at += gap
            p = model.aged(p, gap, rate)
            confirmed = random.random() < 0.7
            likelihood = accuracy if confirmed else 1 - accuracy
            p = p * likelihood / (p * likelihood + (1 - p) * (1 - likelihood))
            verdicts.append(model.Verdict(START + at * DAY, "confirmed" if confirmed else "refuted", "V2",
                                          "normal", "routine"))
        assert model.assess(verdicts, START + at * DAY).confidence == pytest.approx(p, abs=1e-6)
    finally:
        model.FAMILIES.clear()
        model.FAMILIES.update(original_families)


def test_predict_does_not_depend_on_how_time_is_split():
    belief, _ = model.update(model.prior_belief(), True, "V3")
    once = model.predict(belief, 14, model.VOLATILITY["volatile"])
    twice = model.predict(model.predict(belief, 7, model.VOLATILITY["volatile"]), 7, model.VOLATILITY["volatile"])
    assert max(abs(once[cell] - twice[cell]) for cell in once) < 1e-12
    assert sum(once.values()) == pytest.approx(1.0)


def test_a_long_gap_forgets_the_old_family_reading():
    # After many switches nothing of the first verdict remains: a same-family refutation then reads
    # as one verdict alone. The readings are redrawn at every switch, not only on a net change.
    rate = model.VOLATILITY["volatile"]
    verdicts = [model.Verdict(START, "confirmed", "V3", "volatile", "routine"),
                model.Verdict(START + 120 * DAY, "refuted", "V3", "volatile", "routine")]
    assert model.assess(verdicts, START + 120 * DAY).settled == pytest.approx(1 - model.route_accuracy("V3"),
                                                                             abs=1e-6)
    assert rate * 120 > 15


def test_days_until_below_never_reaches_a_bound_at_or_below_one_half():
    assert model.days_until_below(0.9, 0.5, 0.01) is None
    assert model.days_until_below(0.9, 0.4, 0.01) is None


def test_assess_orders_its_verdicts_by_time():
    first = model.Verdict(START, "confirmed", "V2", "normal", "routine")
    second = model.Verdict(START + DAY, "confirmed", "V3", "normal", "routine")
    assert model.assess([second, first], START + DAY) == model.assess([first, second], START + DAY)


def test_assess_needs_a_verdict():
    with pytest.raises(ValueError):
        model.assess([], START)


# --- validation --------------------------------------------------------------------------------------

@pytest.mark.parametrize("change, fragment", [
    ({"level": "V0"}, "V0"),
    ({"level": "V5"}, "level"),
    ({"verdict": "maybe"}, "verdict"),
    ({"importance": "vital"}, "importance"),
    ({"volatility": "fast"}, "volatility"),
    ({"subject": " "}, "subject"),
    ({"claim": "x" * (CLAIM_MAX + 1)}, "claim"),
    ({"evidence": ["e"] * (EVIDENCE_MAX_ITEMS + 1)}, "evidence"),
    ({"evidence": [""]}, "evidence"),
    ({"contract_revision": "abc"}, "contract_revision"),
])
def test_bad_requests_are_refused_and_nothing_is_written(store, change, fragment):
    with pytest.raises(ClaimError, match=fragment) as raised:
        record(store, **change)
    assert raised.value.code == "invalid_request"
    assert not store.path.exists()


# --- storage -----------------------------------------------------------------------------------------

def test_the_store_only_appends(store, clock):
    record(store)
    first = store.path.read_text(encoding="utf-8")
    clock.advance(2)
    record(store, verdict="refuted", level="V1", evidence=["pytest -q: 1 failed"])
    text = store.path.read_text(encoding="utf-8")
    assert text.startswith(first) and len(text.splitlines()) == 2
    event = json.loads(text.splitlines()[1])
    assert event["evidence"] == ["pytest -q: 1 failed"] and event["v"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_store_is_private(store):
    record(store)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.directory).st_mode) == 0o700


def test_malformed_lines_are_skipped(store):
    key = record(store)["claim"]["key"]
    good = json.loads(store.path.read_text(encoding="utf-8"))
    broken = [{**good, "at": "yesterday"}, {**good, "level": "V9"}, {**good, "verdict": None},
              {**good, "volatility": "fast"}, {**good, "subject": 3}, {**good, "v": 2}]
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n[1]\n{\"v\": 1, \"key\": \"nope\"}\n")
        for event in broken:
            handle.write(json.dumps(event) + "\n")
    assert len(store.claims()) == 1 and len(store.get(key).events) == 1
    assert store.get(key).assess(START).state == "supported"


def test_lines_that_could_break_every_read_are_skipped(store):
    key = record(store)["claim"]["key"]
    good = json.loads(store.path.read_text(encoding="utf-8"))
    hostile = [{**good, "at": 123}, {**good, "at": None}, {**good, "at": ["x"]},
               {k: v for k, v in good.items() if k != "id"}, {k: v for k, v in good.items() if k != "attestation"},
               {**good, "attestation": "x"}, {**good, "evidence": [1]}, {**good, "key": good["key"] + "\n"}]
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write("[" * 200_000 + "\n")
        for event in hostile:
            handle.write(json.dumps(event) + "\n")
    assert list(store.claims()) == [key] and len(store.get(key).events) == 1
    assert [found.key for found, _ in store.search()] == [key]
    record(store)  # and the store still takes verdicts


def test_a_line_whose_key_or_size_does_not_fit_its_text_is_skipped(store):
    key = record(store)["claim"]["key"]
    good = json.loads(store.path.read_text(encoding="utf-8"))
    other = claim_key("another", "claim")
    forged = [{**good, "key": other},  # a verdict reattached to another claim
              {**good, "claim": "x" * (CLAIM_MAX + 1), "key": claim_key(good["subject"], "x" * (CLAIM_MAX + 1))},
              {**good, "evidence": ["e"] * (EVIDENCE_MAX_ITEMS + 1)},
              {**good, "attestation": {"contract_revision": "nope", "result_sha256": None}},
              {**good, "attestation": {"extra": None}}]
    with store.path.open("a", encoding="utf-8") as handle:
        for event in forged:
            handle.write(json.dumps(event) + "\n")
    assert list(store.claims()) == [key] and len(store.get(key).events) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_an_existing_permissive_store_is_made_private_on_write(store):
    store.directory.mkdir(parents=True, mode=0o755)
    os.chmod(store.directory, 0o755)
    store.path.write_text("", encoding="utf-8")
    os.chmod(store.path, 0o644)
    record(store)
    assert stat.S_IMODE(os.stat(store.directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_a_directory_created_by_another_process_first_is_accepted(store, monkeypatch):
    import src.claims.store as module

    real_mkdir = module.os.mkdir

    def mkdir(path, *args, **kwargs):
        # A concurrent writer creates the directory just before this one does.
        real_mkdir(path, *args, **kwargs)
        raise FileExistsError(path)

    monkeypatch.setattr(module.os, "mkdir", mkdir)
    assert record(store)["status"] == "recorded"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor-relative opens")
def test_a_directory_swapped_for_a_link_after_the_checks_does_not_redirect_the_write(store, tmp_path,
                                                                                      monkeypatch):
    import src.claims.store as module

    record(store, subject="first", claim="one")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    moved = store.directory.with_name("claims-moved")
    real_open = module.os.open

    def open_after_swap(path, flags, *args, **kwargs):
        if path == module.STORE_FILE and not moved.exists():
            store.directory.rename(moved)  # the directory the store already holds open
            store.directory.symlink_to(elsewhere, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", open_after_swap)
    record(store, subject="second", claim="two")
    assert list(elsewhere.iterdir()) == []
    assert len((moved / module.STORE_FILE).read_text(encoding="utf-8").splitlines()) == 2


def test_a_torn_last_line_does_not_swallow_the_next_verdict(store):
    first = record(store, subject="a", claim="x")["claim"]["key"]
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write('{"v": 1, "key": "torn')  # a write cut short, without its newline
    second = record(store, subject="b", claim="y")["claim"]["key"]
    assert set(store.claims()) == {first, second}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symbolic links")
def test_a_linked_store_file_is_refused_and_its_target_untouched(store, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    store.directory.mkdir(parents=True)
    store.path.symlink_to(outside)
    with pytest.raises(ClaimError) as raised:
        record(store)
    assert raised.value.code == "unsafe_store"
    with pytest.raises(ClaimError):
        store.claims()
    assert outside.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symbolic links")
def test_a_linked_store_directory_is_refused(store, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.directory.parent.mkdir(parents=True)
    store.directory.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ClaimError) as raised:
        record(store)
    assert raised.value.code == "unsafe_store"
    assert list(elsewhere.iterdir()) == []


def test_text_that_utf8_cannot_encode_is_an_invalid_request(store):
    for change in ({"subject": "bad \ud800"}, {"evidence": ["bad \ud800"]}):
        with pytest.raises(ClaimError) as raised:
            record(store, **change)
        assert raised.value.code == "invalid_request"


def test_search_filters_by_text_and_state_and_orders_a_state_as_a_queue(store, clock):
    record(store, subject="alpha", claim="one", volatility="normal")
    record(store, subject="beta", claim="two", volatility="volatile")
    record(store, subject="gamma", claim="three", verdict="refuted")
    assert [claim.subject for claim, _ in store.search(query="ALPHA")] == ["alpha"]
    clock.advance(400)
    stale = store.search(state="stale")
    # The refuted claim was due at once (nothing confirms it), the volatile one next.
    assert [claim.subject for claim, _ in stale] == ["gamma", "beta", "alpha"]
    with pytest.raises(ClaimError):
        store.search(state="unknown")


def test_attestation_is_kept_with_the_verdict(store):
    revision, digest = "a" * 64, "b" * 64
    key = record(store, contract_revision=revision, result_sha256=digest)["claim"]["key"]
    [event] = store.get(key).events
    assert event["attestation"] == {"contract_revision": revision, "result_sha256": digest}


# --- the tools ---------------------------------------------------------------------------------------

@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    engine_config._reset_client_repo_root_cache()
    yield tmp_path
    engine_config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_the_tools_record_and_look_up_a_claim(served):
    recorded = json.loads(await server.claim_record(subject="src/a.py", claim="X is 1", verdict="confirmed",
                                                    level="V2", evidence=["src/a.py:3"]))
    assert recorded["status"] == "recorded" and recorded["workspace"]["root"] == str(served)
    key = recorded["claim"]["key"]
    assert (served / "data" / "claims" / "claims.jsonl").exists()
    by_key = json.loads(await server.claim_lookup(key=key.upper()))
    by_text = json.loads(await server.claim_lookup(subject="SRC/A.PY", claim="x is 1."))
    assert by_key["claims"] == by_text["claims"] and by_key["total"] == 1
    assert by_key["verdicts"][0]["evidence"] == ["src/a.py:3"]
    listing = json.loads(await server.claim_lookup(query="x is"))
    assert listing["mode"] == "listing" and listing["claims"][0]["key"] == key
    missing = json.loads(await server.claim_lookup(key="0" * 16))
    assert missing["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"subject": "only"},
    {"key": "xyz"},
    {"state": "fresh"},
])
async def test_the_lookup_tool_refuses_bad_arguments(served, arguments):
    payload = json.loads(await server.claim_lookup(**arguments))
    assert payload["status"] == "error" and payload["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_json_looking_text_is_looked_up_as_it_was_recorded(served):
    recorded = json.loads(await server.claim_record(subject="config", claim='{"timeout":30}',
                                                    verdict="confirmed", level="V2"))
    found = json.loads(await server.claim_lookup(subject="config", claim='{"timeout":30}'))
    assert found["total"] == 1 and found["claims"][0]["key"] == recorded["claim"]["key"]
    listed = json.loads(await server.claim_lookup(query='{"timeout":30}'))
    assert listed["total"] == 1


@pytest.mark.asyncio
async def test_a_plain_evidence_string_stays_one_item(served):
    output = "ran pytest: 3 passed, 1 skipped, 0 failed"
    recorded = json.loads(await server.claim_record(subject="s", claim="c", verdict="confirmed", level="V2",
                                                    evidence=output))
    found = json.loads(await server.claim_lookup(key=recorded["claim"]["key"]))
    assert found["verdicts"][0]["evidence"] == [output]


@pytest.mark.asyncio
async def test_the_record_tool_reports_a_refused_verdict(served):
    payload = json.loads(await server.claim_record(subject="s", claim="c", verdict="confirmed", level="V0"))
    assert payload["status"] == "error" and payload["code"] == "invalid_request" and "V0" in payload["error"]


@pytest.mark.asyncio
async def test_a_listing_stays_under_the_result_limit(served):
    for index in range(25):
        await server.claim_record(subject=f"s{index}", claim='"' * CLAIM_MAX, verdict="confirmed", level="V2")
    payload = json.loads(await server.claim_lookup(limit=20))
    assert payload["total"] == 25
    assert len(payload["claims"]) + payload.get("omitted", 0) == 20
    assert server.shown_size(server.to_json(payload)) <= server.CLAIM_RESULT_BUDGET
