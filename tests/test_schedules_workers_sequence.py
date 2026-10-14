"""The worker service across the implement and submit stages (#347): several verification reviews,
the importer swap, the requested profile in attempt records, and the pre-mortem risks the run
sequence reads. The fake claude worker and the World of test_schedules_workers; no model runs.
"""
import json
from pathlib import Path

import pytest

from src.daemon.state import write_json
from src.schedules import broker, records, repos, worker_launch, workers
from src.schedules.socket_service import RequestRefused
from tests.test_schedules_workers import (FINDINGS, ISSUE, PATH, RUN_ID, attempt_records, config, finish,  # noqa: F401
                                          head_of, keychain, repo, world)  # repo, world: fixtures

SECURITY = {"severity": "medium", "location": "src/app.py:3", "explanation": "The token is logged in plain text."}
PLAIN = {"severity": "medium", "location": "src/app.py:1", "explanation": "No docstring on run()."}


def make_service(world, *, max_verifications=workers.MAX_VERIFICATIONS, importer=None):
    """`World.service` with the service's own options (World.service passes its options to the launcher)."""
    binding = workers.Binding(RUN_ID, ISSUE, world.repo.base, world.repo.trusted, world.state, log_copy=world.copy_dir,
                              criteria=("Workers are validated against the retained contract.",))
    launcher = worker_launch.Launcher(claude=world.claude, path=PATH, home=world.home, state_dir=world.state,
                                      run_id=RUN_ID, decoys=world.planted, command_runner=keychain)
    return workers.WorkerService(binding, config(), launcher, prepare=world.prepare, importer=importer,
                                 max_verifications=max_verifications)


def premortem_done(world, service, risks):
    """A clean review-1 and an accepted pre-mortem with ``risks``; returns the reviewed head."""
    world.scenario({"outcomes": [{"findings": []}, {"findings": risks}, {"findings": []}]})
    head = head_of(world)
    assert finish(service, service.review({"head": head}))["status"] == "accepted"
    assert finish(service, service.premortem({"head": head}))["status"] == "accepted"
    return head


# --- several verification reviews ------------------------------------------------------------------

def test_each_bot_round_gets_a_verification_review_up_to_the_limit(world):
    service = make_service(world, max_verifications=3)
    head = premortem_done(world, service, [SECURITY])
    [risk] = broker.read_premortem(world.run_dir)
    service.decide({"key": risk, "decision": "fixed"})
    assert workers.open_security_risks(world.run_dir) == [risk]  # fixed, not yet verified
    heads = [head]
    for number in (1, 2, 3):
        heads.append(head_of(world))
        state = finish(service, service.review({"head": heads[-1], "verification": True}))
        assert state["status"] == "accepted" and state["handle"] == f"verify-{number}"
        assert state["range"] == f"{heads[-2]}..{heads[-1]}"  # each covers the commits since the previous one
        assert broker.validated_review_head(world.run_dir) == heads[-1]
        if number == 1:
            assert workers.open_security_risks(world.run_dir) == []
    with pytest.raises(RequestRefused, match="the 3 verification reviews of this run already ran"):
        service.review({"head": head_of(world), "verification": True})


def test_a_second_verification_of_the_same_head_has_nothing_to_verify(world):
    service = make_service(world, max_verifications=2)
    premortem_done(world, service, [])
    verified = head_of(world)
    finish(service, service.review({"head": verified, "verification": True}))
    with pytest.raises(RequestRefused, match="no commits since the last verification"):
        service.review({"head": verified, "verification": True})


def test_the_default_is_one_verification(world):
    service = world.service()
    assert service.max_verifications == workers.MAX_VERIFICATIONS == 1
    premortem_done(world, service, [])
    finish(service, service.review({"head": head_of(world), "verification": True}))
    with pytest.raises(RequestRefused, match="the verification review already ran for this run"):
        service.review({"head": head_of(world), "verification": True})


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_max_verifications_must_be_a_positive_integer(world, value):
    with pytest.raises(ValueError, match="max_verifications"):
        make_service(world, max_verifications=value)


# --- the importer swap and the requested profile ---------------------------------------------------

def test_rebind_importer_switches_the_import_source(world):
    calls = []

    def importer(name):
        def run(binding):
            calls.append(name)
            return None  # no bundle handed over: the head must already be an imported tip
        return run
    service = make_service(world, importer=importer("implement"))
    head = head_of(world)
    finish(service, service.review({"head": head}))
    service.rebind_importer(importer("submit"))
    finish(service, service.review({"head": head_of(world)}))
    assert calls == ["implement", "submit"]
    with pytest.raises(ValueError, match="not a branch of issue #345"):
        service.rebind_importer(workers.Handover(repos.TrustedRepository(world.repo.trusted, str(world.repo.trusted)),
                                                 world.base, "claude/autodev-346-x"))
    service.rebind_importer(None)
    assert service.importer is None


def test_attempt_records_keep_the_requested_profile(world):
    world.scenario({"outcome": {"findings": FINDINGS[1:2]}})
    service = world.service()
    finish(service, service.review({"head": head_of(world)}))
    finish(service, service.review({"head": head_of(world)}))
    first, second = attempt_records(world, "review-1")[0], attempt_records(world, "review-2")[0]
    assert first["requested"] == {"client": "claude", "model": "opus", "effort": "high",
                                  "fallback": {"model": "opus", "effort": "high"}}
    assert second["requested"]["effort"] == "medium"  # effort_followup
    assert (first["client"]["client_applied"], first["model"], first["effort"]) == ("claude", "opus", "high")


# --- the pre-mortem risks the run sequence reads -----------------------------------------------------

NOW = "2026-10-14T12:00:00+00:00"
LATER = "2026-10-14T12:30:00+00:00"
EARLIER = "2026-10-14T11:30:00+00:00"


def risk(key, level="medium", title="A risk", security=False):
    return {"id": key, "level": level, "title": title, "body": "What could break.", "security": security}


def decision(key, value, time=NOW, handle="premortem-1", history=()):
    return {"key": key, "decision": value, "reason": "why" if value != "fixed" else None, "handle": handle,
            "kind": "premortem" if handle.startswith("premortem") else "review", "time": time,
            "history": list(history)}


def write_records(run_dir: Path, risks, decisions=(), verifications=()):
    write_json(run_dir / broker.PREMORTEM_RECORD, {"validated": True, "findings": risks})
    write_json(run_dir / workers.DECISIONS_NAME, {"run_id": RUN_ID, "decisions": list(decisions)})
    write_json(run_dir / workers.STATE_NAME, {"run_id": RUN_ID, "tasks": [
        {"handle": f"verify-{number}", "kind": "verify", "status": status, "blocked": blocked, "started": started}
        for number, (status, blocked, started) in enumerate(verifications, 1)]})


@pytest.fixture
def run_dir(tmp_path):
    return records.run_dir(RUN_ID, tmp_path)


@pytest.mark.parametrize("decisions, verifications, still_open", [
    ([], [], True),
    ([decision("s1", "known_risk")], [], True),
    ([decision("s1", "mitigated")], [], False),
    ([decision("s1", "fixed")], [], True),
    ([decision("s1", "fixed")], [("accepted", False, LATER)], False),
    ([decision("s1", "fixed")], [("accepted", False, EARLIER)], True),
    ([decision("s1", "fixed")], [("accepted", True, LATER)], True),  # the verification ended the stage blocked
    ([decision("s1", "fixed")], [("failed", False, LATER)], True),
    # A later decision on the same key by a review does not count; the pre-mortem's in the history does.
    ([decision("s1", "declined", LATER, "review-2", [decision("s1", "mitigated")])], [], False),
    ([decision("s1", "fixed", LATER, "review-2")], [("accepted", False, LATER)], True),
])
def test_open_security_risks(run_dir, decisions, verifications, still_open):
    write_records(run_dir, [risk("s1", security=True), risk("p1")], decisions, verifications)
    assert workers.open_security_risks(run_dir) == (["s1"] if still_open else [])


def test_a_risk_with_a_security_keyword_counts_without_the_mark(run_dir):
    write_records(run_dir, [risk("k1", title="The deploy key is logged")])
    assert workers.open_security_risks(run_dir) == ["k1"]
    assert workers.risk_candidates(run_dir) == []


def test_no_premortem_record_has_no_risks(run_dir):
    assert workers.open_security_risks(run_dir) == [] and workers.risk_candidates(run_dir) == []


def test_risk_candidates(run_dir):
    risks = [risk("m1", "medium"), risk("h1", "high"), risk("l1", "low"), risk("s1", "high", security=True),
             risk("f1", "critical"), risk("u1", "medium")]
    decisions = [decision(key, "known_risk") for key in ("m1", "h1", "l1", "s1")] + [decision("f1", "fixed")]
    write_records(run_dir, risks, decisions)
    assert workers.risk_candidates(run_dir) == ["m1", "h1"]


def test_open_security_risks_from_a_real_premortem(world):
    service = world.service()
    premortem_done(world, service, [SECURITY, PLAIN])
    keys = {workers.finding_key(item["location"], item["explanation"]): item for item in (SECURITY, PLAIN)}
    secure = next(key for key, item in keys.items() if item is SECURITY)
    plain = next(key for key, item in keys.items() if item is PLAIN)
    assert workers.open_security_risks(world.run_dir) == [secure]
    service.decide({"key": plain, "decision": "known_risk", "reason": "Docstrings come later."})
    service.decide({"key": secure, "decision": "mitigated", "reason": "The logger masks tokens (test_mask)."})
    assert workers.open_security_risks(world.run_dir) == []
    assert workers.risk_candidates(world.run_dir) == [plain]
    assert json.loads((world.run_dir / workers.DECISIONS_NAME).read_text())["decisions"][0]["handle"] == "premortem-1"
