"""Real journal acceptance scenarios; no model, live daemon or filesystem edits."""
from dataclasses import asdict
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import uuid

import pytest

from src.cooperation.admin import cleanup
from src.cooperation.identity import CooperationIdentity
from src.cooperation import quota
from src.cooperation.models import PROTOCOL, ApplyResponse, ReadResponse, parse_apply, parse_read, parse_register
from src.cooperation.service import CooperationService, _published_next_free
from src.cooperation.store import CooperationError, Store, canonical

#: The default and largest checkpoint interval: a holder that makes no further
#: call stays live this long after its acquire.
WINDOW = 300.0
#: Minutes of a peer's heartbeats that outlast a silent holder's window.
PAST_WINDOW_MINUTES = int(WINDOW // 60) + 1


def resource(key="file.py", kind="physical_file", access="write"):
    return {"resource_type": kind, "resource_key": key, "access": access}


class Harness:
    def __init__(self, tmp_path):
        self.now = 1000.0
        self.checkout = tmp_path / "checkout"
        self.checkout.mkdir()
        self.directory = tmp_path / "state"
        self.identity = CooperationIdentity("repository_test", "workspace_test", "checkout_test", str(self.checkout), "a" * 40, "dev")
        self.service = CooperationService(self.directory, clock=lambda: self.now,
                                          priority_verifier=lambda participant, priority: priority["source"] == "authorized_user")
        self.service.start()

    def register(self, task, *, identity=None, scope=None, parent=None, **extra):
        args = {"protocol": PROTOCOL, "checkout_path": str(self.checkout), "task_id": task, "attempt_id": "1", "root_task_id": task,
                "scope": scope or [resource(".", "physical_subtree"), resource("dev", "integration_target", "integrate"), resource("api", "logical_intent"), resource("pytest", "heavy_execution")]}
        if parent:
            args.update(parent_id=parent["participant_id"], parent_capability=parent["capability"], root_task_id=parent["task"], contract_revision="a" * 64)
        args.update(extra)
        result = self.service.register(parse_register(args), identity or self.identity)
        assert result["status"] == "ok", result
        return {**result, "task": task, "identity": identity or self.identity, "contract_revision": args.get("contract_revision"), "parent_id": args.get("parent_id")}

    def read(self, **extra):
        response = self.service.read(parse_read({"protocol": PROTOCOL, "repository_id": self.identity.repository_id, **extra}), self.identity)
        ReadResponse.model_validate(response)
        return response

    def request(self, actor, action, **extra):
        args = {"protocol": PROTOCOL, "repository_id": self.identity.repository_id, "participant_id": actor["participant_id"], "capability": actor["capability"],
                "operation_id": str(uuid.uuid4()), "expected_revision": self.read()["revision"], "expected_generation": actor["generation"], "action": action}
        args.update(extra)
        return parse_apply(args)

    def apply(self, actor, action, **extra):
        result = self.service.apply(self.request(actor, action, **extra), actor["identity"])
        ApplyResponse.model_validate(result)
        if result["status"] == "ok" and not extra.get("subject_id"):
            actor["generation"] = result["generation"]
        return result

    def acquire(self, actor, *resources, **extra):
        return self.apply(actor, {"kind": "acquire", "resources": list(resources) or [resource()], "work_unit_id": "unit", "checkpoint": {"trigger": "unit done"}, **extra})

    def checkpoint(self, actor, *, name="cp", **extra):
        return self.apply(actor, {"kind": "checkpoint", "checkpoint_id": name, "release_ready": True, **extra})

    def proposal(self, actors, *, name="agreement", owner=None, pattern="single_writer_reviewers", resources=None, **terms):
        owner = owner or actors[0]
        return {"kind": "propose", "agreement_id": name, "pattern": pattern,
                "required_participants": [a["participant_id"] for a in actors],
                "task_bindings": [{"participant_id": a["participant_id"], "task_id": a["task"], "attempt_id": "1", "generation": a["generation"], "contract_revision": a["contract_revision"], "parent_id": a["parent_id"]} for a in actors],
                "terms": {"scope": "bounded file change", "resources": resources or [{**resource(), "owner": owner["participant_id"]}], "integrator": owner["participant_id"],
                          "checkpoint": {"trigger": "unit done"}, "completion_conditions": ["accepted checks and release"], **terms}}

    def ack(self, actor, agreement, **extra):
        action = {"kind": "acknowledge", "agreement_id": agreement["agreement_id"], "proposal_revision": agreement["proposal_revision"], "terms_digest": agreement["terms_digest"], **extra}
        return self.apply(actor, action)

    def recover(self, actor, *, subject=None, **extra):
        owner = subject or actor
        claims = [c for c in self.read()["claims"] if c["participant_id"] == owner["participant_id"]]
        action = {"kind": "recover", "service_epoch": self.service.epoch, "previous_generation": owner["generation"],
                  "execution_evidence": {"kind": "stopped", "reference": "host exit receipt", "execution_id": "execution-1"},
                  "reconciliation": {"checkpoint_id": None, "inspected": True, "summary": "dirty state inspected"},
                  "claims": [{"claim_id": c["claim_id"], "expected_revision": c["revision"]} for c in claims], **extra}
        return self.apply(actor, action, **({"subject_id": subject["participant_id"], "expected_generation": subject["generation"]} if subject else {}))


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.service.close()


def test_atomic_set_and_idempotent_receipts_c02_c04_c05(h):
    a, b = h.register("a"), h.register("b")
    request = h.request(a, {"kind": "acquire", "resources": [resource()], "work_unit_id": "one", "checkpoint": {"trigger": "done"}})
    first = h.service.apply(request, h.identity)
    assert first["status"] == "ok"
    assert h.acquire(b, resource("free.py"), resource())["error"] == "claim_conflict"
    assert len(h.read()["claims"]) == 1
    assert h.service.apply(request, h.identity) == first
    changed = request.model_dump()
    changed["action"]["work_unit_id"] = "changed"
    assert h.service.apply(parse_apply(changed), h.identity)["error"] == "idempotency_conflict"
    with h.service.store.transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def _racing_apply(directory, identity, request, barrier, output):
    service = CooperationService(directory, clock=lambda: 1000.0)
    # These are separate DB clients of one authority, not separate lifecycle owners.
    service.started, service.epoch = True, 1
    barrier.wait(timeout=10)
    output.put(service.apply(parse_apply(request), CooperationIdentity(**identity)))


def test_multiprocess_same_resource_exactly_one_winner_c01(h):
    a, b = h.register("a"), h.register("b")
    action = {"kind": "acquire", "resources": [resource()], "work_unit_id": "unit", "checkpoint": {"trigger": "done"}}
    requests = [h.request(actor, action).model_dump() for actor in (a, b)]
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_racing_apply, args=(str(h.directory), asdict(h.identity), request, barrier, output)) for request in requests]
    for process in processes:
        process.start()
    results = [output.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sorted(r.get("error", r["status"]) for r in results) == ["claim_conflict", "ok"]
    assert len(h.read()["claims"]) == 1


def test_counterproposal_generation_and_deadline_c03_c18_c27(h):
    a, b = h.register("a"), h.register("b")
    proposed = h.apply(a, h.proposal([a, b]))["agreement"]
    assert h.ack(a, proposed)["agreement"]["state"] == "proposed"
    counter = h.proposal([a, b], owner=b)
    counter.update(proposal_revision=2, supersedes=1)
    req = h.request(b, counter)
    updated = h.service.apply(req, h.identity)
    assert updated["status"] == "ok"
    assert h.ack(a, proposed)["error"] == "stale_revision"
    assert h.ack(a, updated["agreement"])["agreement"]["state"] == "proposed"
    assert h.ack(b, updated["agreement"])["agreement"]["state"] == "committed"
    assert h.acquire(a)["error"] == "claim_conflict"
    assert h.acquire(b, agreement_id="agreement")["status"] == "ok"
    c, d = h.register("c"), h.register("d")
    pending = h.apply(c, h.proposal([c, d], name="expired", resources=[{**resource("other.py"), "owner": c["participant_id"]}], acknowledgement_timeout_seconds=10))["agreement"]
    h.now += 11
    assert h.ack(d, pending)["error"] == "proposal_expired"


def test_stale_restart_recovery_keeps_quarantine_c06_c07_c27(h):
    a = h.register("a")
    assert h.acquire(a)["status"] == "ok"
    h.now += WINDOW + 1
    b = h.register("b")
    assert h.read()["claims"][0]["quarantined"]
    assert h.acquire(b)["error"] == "claim_conflict"
    assert h.apply(a, {"kind": "heartbeat"})["error"] == "stale_generation"
    before = h.service.epoch
    h.service.close()
    h.service.start()
    assert h.service.epoch == before + 1
    assert h.recover(a)["status"] == "ok"
    assert h.apply(a, {"kind": "heartbeat"}, expected_generation=1)["error"] == "stale_generation"
    assert h.recover(b)["status"] == "ok"
    assert h.acquire(b)["status"] == "ok"


def test_parent_authority_scope_and_relay_c11_c12_c13(h):
    parent, peer = h.register("parent"), h.register("peer")
    child = h.register("child", parent=parent, scope=[resource()])
    assert h.acquire(child, resource("else.py"))["error"] == "scope_denied"
    assert h.apply(peer, {"kind": "heartbeat"}, subject_id=child["participant_id"])["error"] == "unauthorized_actor"
    assert h.apply(peer, {"kind": "heartbeat"}, participant_id=parent["participant_id"])["error"] == "unauthorized_actor"
    proposal = h.apply(parent, h.proposal([parent, child]))["agreement"]
    h.ack(parent, proposal)
    ack = {"kind": "acknowledge", "agreement_id": "agreement", "proposal_revision": 1, "terms_digest": proposal["terms_digest"], "acceptance_mode": "parent_relay"}
    result = h.apply(parent, ack, subject_id=child["participant_id"])
    assert result["agreement"]["state"] == "committed"
    assert result["event"]["actor_id"] == parent["participant_id"]
    assert result["event"]["subject_id"] == child["participant_id"]
    assert h.recover(peer, subject=child)["error"] == "unauthorized_actor"
    assert h.recover(parent, subject=child)["status"] == "ok"
    serialized = canonical(h.read())
    for participant in (parent, peer, child):
        assert participant["capability"] not in serialized


def _intent(resources=(), dependencies=(), priority="normal"):
    return {"kind": "declare_intent", "resources": list(resources), "dependencies": list(dependencies),
            "priority": {"value": priority, "source": "authorized_user", "evidence_ref": "host verified instruction"}, "checkpoint": {"trigger": "done"}}


def _dependency(producer, consumer, artifact="result"):
    return {"producer_task": producer["task"], "consumer_task": consumer["task"], "artifact_id": artifact}


def test_duplicate_intent_dependencies_are_rejected_without_mutation(h):
    producer, consumer = h.register("producer"), h.register("consumer")
    assert h.apply(consumer, _intent([resource("old.py")], [_dependency(producer, consumer, "old")]))["status"] == "ok"
    before = h.read()
    dependency = _dependency(producer, consumer, "new")
    request = h.request(consumer, _intent([resource("new.py")], [dependency, dependency]))
    result = h.service.apply(request, h.identity)
    assert result["error"] == "invalid_request"
    assert h.read() == before
    with h.service.store.transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM receipts WHERE operation_id=?", (request.operation_id,)).fetchone()[0] == 0
    corrected = request.model_dump()
    corrected["action"]["dependencies"].pop()
    assert h.service.apply(parse_apply(corrected), h.identity)["status"] == "ok"


def _artifact(h, producer, artifact="result", head="b" * 40):
    return {"artifact_id": artifact, "producer_task_id": producer["task"], "producer_attempt_id": "1", "repository_id": h.identity.repository_id,
            "base_revision": "a" * 40, "head_revision": head, "path": "file.py", "digest": "c" * 64,
            "media_type": "text/plain", "summary": "implementation", "checks": [{"name": "focused", "status": "passed"}]}


def _accept(artifact, consumer):
    return {"kind": "accept_artifact", **{key: artifact[key] for key in ("artifact_id", "digest", "head_revision", "producer_task_id", "producer_attempt_id")},
            "consumer_task_id": consumer["task"], "consumer_attempt_id": "1", "checks": [{"name": "hash and revision verified", "status": "passed"}]}


def test_cycles_dependency_inheritance_and_artifact_gate_c14_c15_c20(h):
    producer, consumer, ordinary = [h.register(name) for name in ("producer", "consumer", "ordinary")]
    assert h.apply(producer, _intent([resource()]))["status"] == "ok"
    assert h.apply(ordinary, _intent([resource()]))["status"] == "ok"
    assert h.apply(consumer, _intent([resource("consumer.py")], [_dependency(producer, consumer)], "urgent"))["status"] == "ok"
    queue = h.read()["ready"]
    assert queue[0]["participant_id"] == producer["participant_id"] and queue[0]["effective_priority"] == 2
    assert h.acquire(consumer, resource("consumer.py"))["error"] == "dependency_wait"
    cyclic = h.apply(producer, _intent([resource()], [_dependency(consumer, producer, "other")]))
    assert cyclic["error"] == "dependency_cycle" and cyclic["cycle"][0] == cyclic["cycle"][-1]
    assert h.acquire(ordinary)["error"] == "priority_wait"
    artifact = _artifact(h, producer)
    assert h.checkpoint(producer, artifacts=[artifact])["status"] == "ok"
    bad = _accept(artifact, consumer)
    bad["head_revision"] = "changed"
    assert h.apply(consumer, bad)["error"] == "artifact_mismatch"
    assert h.apply(consumer, _accept(artifact, consumer))["status"] == "ok"
    assert h.acquire(consumer, resource("consumer.py"))["status"] == "ok"
    queue = h.read()["ready"]
    assert next(t for t in queue if t["participant_id"] == producer["participant_id"])["effective_priority"] == 1
    updated = _artifact(h, producer, head="d" * 40)
    h.checkpoint(producer, name="cp2", artifacts=[updated])
    assert next(p for p in h.read()["participants"] if p["participant_id"] == consumer["participant_id"])["status"] == "suspect"


def test_aging_nonpreemption_and_heavy_gate_c16_c17_c30(h):
    a, b = h.register("a"), h.register("b")
    h.apply(a, _intent([resource()], priority="background"))
    # Keep the participants alive without extending or preempting claims.
    for _ in range(6):
        h.now += 100
        h.apply(a, {"kind": "heartbeat"})
        h.apply(b, {"kind": "heartbeat"})
    h.apply(b, _intent([resource()]))
    queue = h.read()["ready"]
    assert queue[0]["participant_id"] == a["participant_id"]
    assert h.acquire(a)["status"] == "ok"
    h.apply(b, _intent([resource()], priority="urgent"))
    assert h.acquire(b)["error"] == "claim_conflict"
    for _ in range(4):
        h.now += 100
        heartbeat = h.apply(a, {"kind": "heartbeat"})
        h.apply(b, {"kind": "heartbeat"})
    assert heartbeat["checkpoint_required"]
    heavy = h.register("heavy")
    assert h.acquire(heavy, resource("pytest", "heavy_execution"))["error"] == "execution_lease_required"


def test_handoff_exact_checkpoint_atomic_transfer_c19(h):
    old, new = h.register("old"), h.register("new")
    claims = h.acquire(old)["claims"]
    proposal = h.apply(old, h.proposal([old, new], owner=new, pattern="handoff"))["agreement"]
    h.ack(old, proposal)
    assert h.ack(new, proposal, accepted_checkpoint="cp")["error"] == "scope_denied"
    action = {"kind": "handoff", "claims": [{"claim_id": c["claim_id"], "expected_revision": c["revision"]} for c in claims],
              "old_owner": old["participant_id"], "new_owner": new["participant_id"], "old_generation": 1, "new_generation": 1,
              "checkpoint_id": "cp", "agreement_id": "agreement"}
    assert h.apply(old, action)["error"] == "scope_denied"
    assert h.checkpoint(old)["status"] == "ok"
    assert h.ack(new, proposal, accepted_checkpoint="cp")["agreement"]["state"] == "committed"
    assert h.checkpoint(old)["error"] == "stale_revision"
    transferred = h.apply(old, action)
    assert transferred["status"] == "ok", transferred
    assert [c["participant_id"] for c in h.read()["claims"]] == [new["participant_id"]]


@pytest.mark.parametrize("recovered_party", ["recipient", "observer"])
def test_handoff_requires_fresh_generation_bound_consent_after_recovery(h, recovered_party):
    old, new, observer = h.register("old"), h.register("new"), h.register("observer")
    parties = [old, new, observer]
    claims = h.acquire(old)["claims"]
    proposal = h.apply(old, h.proposal(parties, owner=new, pattern="handoff"))["agreement"]
    assert h.ack(old, proposal)["status"] == "ok"
    assert h.ack(observer, proposal)["status"] == "ok"
    assert h.checkpoint(old)["status"] == "ok"
    assert h.ack(new, proposal, accepted_checkpoint="cp")["agreement"]["state"] == "committed"
    assert h.recover(new if recovered_party == "recipient" else observer)["status"] == "ok"
    action = {"kind": "handoff", "claims": [{"claim_id": c["claim_id"], "expected_revision": c["revision"]} for c in claims],
              "old_owner": old["participant_id"], "new_owner": new["participant_id"],
              "old_generation": old["generation"], "new_generation": new["generation"],
              "checkpoint_id": "cp", "agreement_id": "agreement"}
    request = h.request(old, action)
    before = h.read()
    denied = h.service.apply(request, h.identity)
    assert denied["status"] == "error", denied
    assert denied["error"] == "stale_generation", denied
    assert h.read() == before
    with h.service.store.transaction(write=False) as db:
        assert db.execute("SELECT 1 FROM receipts WHERE operation_id=?", (request.operation_id,)).fetchone() is None

    # Fresh terms and acknowledgements, including checkpoint acceptance, restore
    # admission without borrowing any consent from the previous generation.
    assert h.apply(old, {"kind": "cancel", "agreement_id": "agreement", "reason": "rebind recovered execution"})["status"] == "ok"
    fresh = h.apply(old, h.proposal(parties, name="fresh", owner=new, pattern="handoff"))["agreement"]
    action["agreement_id"] = "fresh"
    assert h.ack(old, fresh)["status"] == "ok"
    assert h.ack(observer, fresh)["status"] == "ok"
    assert h.apply(old, action)["error"] == "scope_denied"
    assert h.ack(new, fresh, accepted_checkpoint="cp")["agreement"]["state"] == "committed"
    accepted_request = h.request(old, action)
    transferred = h.service.apply(accepted_request, h.identity)
    assert transferred["status"] == "ok", transferred
    assert [c["participant_id"] for c in h.read()["claims"]] == [new["participant_id"]]
    assert h.recover(new)["status"] == "ok"
    assert h.service.apply(accepted_request, h.identity) == transferred
    assert h.read()["claims"] == []  # Receipt replay never transfers ownership again.


def test_disablement_retention_and_unfenced_shell_c21_c22_c23_c24_c26(h):
    a, b = h.register("duplicate-a"), h.register("duplicate-b")
    h.apply(a, {"kind": "heartbeat"})
    h.apply(b, {"kind": "heartbeat"})
    h.apply(a, {"kind": "cancel", "reason": "own task complete"})
    assert next(p for p in h.read()["participants"] if p["participant_id"] == b["participant_id"])["status"] == "active"
    h.now += 31 * 86400
    assert h.service.collect_terminal() == 1
    assert h.read(cursor=0)["error"] == "cursor_expired"
    assert h.read()["minimum_cursor"] > 0
    h.recover(b)
    h.acquire(b)
    # Cooperating metadata does not intercept arbitrary writes, intentionally.
    (h.checkout / "file.py").write_text("nonparticipant write")
    assert h.read()["claims"]
    h.service.enabled = False
    assert h.apply(b, {"kind": "heartbeat"})["error"] == "store_unavailable"
    assert h.read()["claims"]
    other = CooperationService(h.directory)
    with pytest.raises(CooperationError, match="running"):
        other.start()


def test_offline_owner_cleanup_c07a_c11a_c11b(h):
    a = h.register("a")
    claims = h.acquire(a)["claims"]
    args = dict(repository_id=h.identity.repository_id, participant_id=a["participant_id"], expected_generation=1,
                claims=[{"claim_id": c["claim_id"], "expected_revision": c["revision"]} for c in claims], execution_pid=999999,
                checkpoint_id=None, inspection="inspected dirty checkout", reason="lost capability", owner_authorized=True,
                stopped_probe=lambda pid: True)
    with pytest.raises(CooperationError, match="running"):
        cleanup(h.directory, **args)
    h.service.close()
    with pytest.raises(CooperationError):
        cleanup(h.directory, **{**args, "owner_authorized": False})
    with pytest.raises(CooperationError):
        cleanup(h.directory, **{**args, "stopped_probe": lambda pid: False})
    with pytest.raises(CooperationError):
        cleanup(h.directory, **{**args, "claims": []})
    assert cleanup(h.directory, **args)["released"] == [claims[0]["claim_id"]]
    h.service.start()
    assert not h.read()["claims"]
    assert h.apply(a, {"kind": "heartbeat"})["error"] == "unauthorized_actor"
    assert not list(h.checkout.iterdir())


def _crash_transaction(path, identity, request, after_commit):
    service = CooperationService(path, clock=lambda: 1000.0)
    service.started, service.epoch = True, 1
    original = service.store.transaction
    @contextmanager
    def crash_boundary(*args, **kwargs):
        with original(*args, **kwargs) as db:
            yield db
            if not after_commit:
                os._exit(37)
        os._exit(37)
    service.store.transaction = crash_boundary
    service.apply(parse_apply(request), CooperationIdentity(**identity))


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_crash_transaction_boundary_c07(h, after_commit):
    actor = h.register("crash")
    request = h.request(actor, {"kind": "acquire", "resources": [resource()], "work_unit_id": "crash-unit", "checkpoint": {"trigger": "done"}})
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_crash_transaction, args=(str(h.directory), asdict(h.identity), request.model_dump(), after_commit))
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 37
    with h.service.store.transaction(write=False) as db:
        for table in ("claims", "events", "receipts"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == int(after_commit)
    h.service.close()
    h.service.start()
    if after_commit:
        assert h.read()["claims"][0]["quarantined"]
        # Original committed receipt remains recoverable after a restart.
        assert h.service.apply(request, h.identity)["status"] == "ok"


def test_busy_and_unknown_schema_preserve_data(h):
    a = h.register("a")
    with h.service.store.transaction() as db:
        result = h.service.apply(parse_apply({"protocol": PROTOCOL, "repository_id": h.identity.repository_id, "participant_id": a["participant_id"], "capability": a["capability"],
                                             "operation_id": "busy", "expected_revision": 1, "expected_generation": 1, "action": {"kind": "heartbeat"}}), h.identity)
        assert result["error"] == "busy"
    h.service.close()
    with sqlite3.connect(h.service.store.path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(CooperationError, match="Unsupported"):
        h.service.start()
    with sqlite3.connect(h.service.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 1


def test_concurrent_counterproposals_c03(h):
    a, b = h.register("a"), h.register("b")
    proposal = h.apply(a, h.proposal([a, b]))["agreement"]
    h.ack(a, proposal)
    counter = h.proposal([a, b], owner=b)
    counter.update(proposal_revision=2, supersedes=1)
    requests = [h.request(actor, counter).model_dump() for actor in (a, b)]
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_racing_apply, args=(str(h.directory), asdict(h.identity), request, barrier, output)) for request in requests]
    for process in processes:
        process.start()
    results = [output.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sorted(r.get("error", r["status"]) for r in results) == ["ok", "stale_revision"]
    assert h.read()["agreements"][0]["state"] == "proposed"
    assert h.ack(b, proposal)["error"] == "stale_revision"


def test_private_checkout_edits_and_shared_integration_c08(h, tmp_path):
    other_path = tmp_path / "other"
    other_path.mkdir()
    identity = CooperationIdentity(h.identity.repository_id, "other-workspace", "checkout_other", str(other_path), "a" * 40, "feature")
    a, b = h.register("a"), h.register("b", identity=identity)
    assert h.acquire(a, resource(), resource("api", "logical_intent"))["status"] == "ok"
    assert h.acquire(b, resource(), resource("api", "logical_intent"))["status"] == "ok"
    for actor in (a, b):
        h.checkpoint(actor)
        held = [c for c in h.read()["claims"] if c["participant_id"] == actor["participant_id"]]
        assert h.apply(actor, {"kind": "release", "claims": [{"claim_id": c["claim_id"], "expected_revision": c["revision"]} for c in held], "reason": "done"})["status"] == "ok"
    assert h.acquire(a, resource("dev", "integration_target", "integrate"))["status"] == "ok"
    assert h.acquire(b, resource("dev", "integration_target", "integrate"))["error"] == "claim_conflict"


def test_checkpoint_cannot_release_a_later_unit(h):
    actor = h.register("a")
    first = h.acquire(actor)["claims"]
    h.checkpoint(actor)
    release = lambda claims: {"kind": "release", "claims": [{"claim_id": c["claim_id"], "expected_revision": c["revision"]} for c in claims], "reason": "done"}
    assert h.apply(actor, release(first))["status"] == "ok"
    second = h.acquire(actor)["claims"]
    assert h.apply(actor, release(second))["error"] == "scope_denied"
    h.checkpoint(actor, name="new-unit")
    assert h.apply(actor, release(second))["status"] == "ok"


def test_cancelled_or_wrong_producer_does_not_open_gate(h):
    producer, consumer, stranger = [h.register(name) for name in ("producer", "consumer", "stranger")]
    h.apply(consumer, _intent([resource("consume.py")], [_dependency(producer, consumer)]))
    artifact = _artifact(h, stranger)
    h.checkpoint(stranger, artifacts=[artifact])
    assert h.apply(consumer, _accept(artifact, consumer))["status"] == "ok"
    assert h.acquire(consumer, resource("consume.py"))["error"] == "dependency_wait"
    h.apply(producer, {"kind": "cancel", "reason": "user stopped this producer"})
    assert h.acquire(consumer, resource("consume.py"))["error"] == "dependency_wait"


def test_idle_and_blocked_time_do_not_age(h):
    producer, idle, blocked = [h.register(name) for name in ("producer", "idle", "blocked")]
    h.apply(blocked, _intent([resource()], [_dependency(producer, blocked)], "background"))
    for _ in range(12):
        h.now += 100
        for actor in (producer, idle, blocked):
            h.apply(actor, {"kind": "heartbeat"})
    h.apply(idle, _intent([resource()], priority="background"))
    artifact = _artifact(h, producer)
    h.checkpoint(producer, artifacts=[artifact])
    h.apply(blocked, _accept(artifact, blocked))
    queued = {t["participant_id"]: t for t in h.read()["ready"]}
    assert queued[idle["participant_id"]]["effective_priority"] == 0
    assert queued[blocked["participant_id"]]["effective_priority"] == 0


def test_reconciliation_invalidates_paginated_snapshot_and_emits_events(h):
    h.register("a")
    h.register("b")
    first = h.read(limit=1)
    h.now += 121
    second = h.read(limit=1, offset=1)
    assert second["revision"] > first["revision"]
    assert second["snapshot_cursor"] > first["snapshot_cursor"]
    events = h.read(cursor=first["snapshot_cursor"])["events"]
    assert len(events) == 2
    assert all(e["kind"] == "reconcile" and e["payload"]["reason"] == "heartbeat_overdue" for e in events)
    assert h.read()["revision"] == second["revision"]


def test_snapshot_time_pins_aging_across_queue_pages(h):
    for task in ("a", "b"):
        actor = h.register(task)
        h.apply(actor, {"kind": "declare_intent", "resources": [resource(task)], "checkpoint": {"trigger": "done"}})
    # Set an eligible-wait boundary without waiting long enough for liveness expiry.
    with h.service.store.transaction() as db:
        from src.cooperation.store import bodies
        for participant in bodies(db, "participants", h.identity.repository_id):
            participant["eligible_wait_seconds"] = 599
            h.service._save_participant(db, participant)
    full = h.read(limit=100)
    first = h.read(limit=1)
    h.now += 2
    second = h.read(limit=1, offset=1, snapshot_time=first["snapshot_time"])
    assert first["revision"] == second["revision"]
    assert first["snapshot_time"] == second["snapshot_time"]
    assert first["ready"] + second["ready"] == full["ready"]
    assert h.read()["ready"][0]["effective_priority"] > first["ready"][0]["effective_priority"]
    assert h.read(snapshot_time=h.now + 1)["error"] == "invalid_request"


def test_proposal_expiry_and_restart_advance_snapshot_revision(h):
    a, b = h.register("a"), h.register("b")
    proposal = h.proposal([a, b])
    proposal["terms"]["acknowledgement_timeout_seconds"] = 1
    assert h.apply(a, proposal)["status"] == "ok"
    before = h.read()
    h.now += 2
    expired = h.read(cursor=before["snapshot_cursor"])
    assert expired["revision"] > before["revision"]
    assert expired["events"][0]["payload"]["reason"] == "proposal_expired"
    h.service.close()
    h.service.start()
    restarted = h.read(cursor=expired["snapshot_cursor"])
    assert restarted["revision"] > expired["revision"]
    assert all(e["payload"]["reason"] == "coordinator_restart" for e in restarted["events"])


def test_task_attempt_composites_and_snapshot_pagination(h):
    h.register("a:b", attempt_id="c")
    h.register("a", attempt_id="b:c")
    for index in range(4):
        h.register("task" + str(index))
    first = h.read(limit=2)
    second = h.read(limit=2, offset=first["next_snapshot_offset"])
    assert first["snapshot_counts"]["participants"] == 6
    assert first["snapshot_truncated"] and second["snapshot_offset"] == 2
    assert not {p["participant_id"] for p in first["participants"]} & {p["participant_id"] for p in second["participants"]}
    assert len(h.read()["ready"]) == 6


def test_owner_cleanup_oversize_audit_rolls_back(h):
    actor = h.register("a")
    claim = h.acquire(actor)["claims"][0]
    h.service.close()
    with pytest.raises(CooperationError, match="bounds"):
        cleanup(h.directory, repository_id=h.identity.repository_id, participant_id=actor["participant_id"], expected_generation=1,
                claims=[{"claim_id": claim["claim_id"], "expected_revision": claim["revision"]}], execution_pid=123,
                checkpoint_id=None, inspection="x" * 4001, reason="test", owner_authorized=True, stopped_probe=lambda pid: True)
    h.service.start()
    assert len(h.read()["claims"]) == 1


def test_accepted_edges_cannot_reopen_a_cycle(h):
    a, b = h.register("a"), h.register("b")
    h.apply(b, _intent([resource("b.py")], [_dependency(a, b, "x")]))
    artifact = _artifact(h, a, "x")
    h.checkpoint(a, artifacts=[artifact])
    h.apply(b, _accept(artifact, b))
    assert h.apply(a, _intent([resource()], [_dependency(b, a, "y")]))["error"] == "dependency_cycle"
    h.checkpoint(a, name="changed", artifacts=[_artifact(h, a, "x", head="d" * 40)])
    assert h.read()["status"] == "ok"
    assert h.acquire(b, resource("b.py"))["error"] == "dependency_wait"


def test_committed_gate_cannot_be_removed_by_intent(h):
    a, b = h.register("a"), h.register("b")
    proposal = h.apply(a, h.proposal([a, b], owner=b, pattern="producer_consumer", resources=[{**resource("b.py"), "owner": b["participant_id"]}], dependencies=[_dependency(a, b)]))["agreement"]
    h.ack(a, proposal)
    assert h.ack(b, proposal)["agreement"]["state"] == "committed"
    assert h.acquire(b, resource("b.py"), agreement_id="agreement")["error"] == "dependency_wait"
    assert h.apply(b, _intent([resource("b.py")]))["status"] == "ok"
    assert h.acquire(b, resource("b.py"), agreement_id="agreement")["error"] == "dependency_wait"


def test_large_ready_response_is_bounded_before_commit(h):
    actors = [h.register("task" + str(index)) for index in range(101)]
    result = h.apply(actors[0], _intent([resource()]))
    assert result["status"] == "ok"
    assert len(result["ready"]) == 100 and result["ready_count"] == 101 and result["ready_truncated"]


def test_reconciliation_freezes_cycle_component_only(h):
    a, b, independent = [h.register(name) for name in ("a", "b", "independent")]
    # Simulate corruption or an imported older journal, not an allowed API path.
    with h.service.store.transaction() as db:
        for index, dependency in enumerate((_dependency(a, b), _dependency(b, a, "other"))):
            dependency.update(producer_attempt_id="1", consumer_attempt_id="1", gate="consumer_accepted")
            db.execute("INSERT INTO dependencies VALUES (?,?,NULL,?)", ("corrupt" + str(index), h.identity.repository_id, canonical(dependency)))
    snapshot = h.read()
    assert snapshot["dependency_cycles"] == [["a/1", "b/1", "a/1"]]
    assert {p["task_id"] for p in snapshot["participants"] if p["status"] == "suspect"} == {"a", "b"}
    assert h.acquire(independent, resource("independent.py"))["status"] == "ok"


def test_final_reviewer_checkpoint_completes_released_agreement(h):
    writer, reviewer, next_writer = [h.register(name) for name in ("writer", "reviewer", "next")]
    agreement = h.apply(writer, h.proposal([writer, reviewer]))["agreement"]
    h.ack(writer, agreement)
    h.ack(reviewer, agreement)
    claims = h.acquire(writer, agreement_id="agreement")["claims"]
    h.checkpoint(writer, agreement_id="agreement", checks=[{"name": "writer checks", "status": "passed"}])
    h.apply(writer, {"kind": "release", "claims": [{"claim_id": c["claim_id"], "expected_revision": c["revision"]} for c in claims], "reason": "done"})
    assert h.read()["agreements"][0]["state"] == "checkpointed"
    h.checkpoint(reviewer, agreement_id="agreement", checks=[{"name": "review", "status": "passed"}])
    assert h.read()["agreements"][0]["state"] == "completed"
    assert h.acquire(next_writer)["status"] == "ok"


def test_unrelated_artifact_acceptance_preserves_eligible_age(h):
    producer, consumer = h.register("producer"), h.register("consumer")
    h.apply(consumer, _intent([resource()], priority="background"))
    artifact = _artifact(h, producer)
    h.checkpoint(producer, artifacts=[artifact])
    for _ in range(6):
        h.now += 100
        h.apply(consumer, {"kind": "heartbeat"})
        h.apply(producer, {"kind": "heartbeat"})
    h.apply(consumer, _accept(artifact, consumer))
    task = next(t for t in h.read()["ready"] if t["participant_id"] == consumer["participant_id"])
    assert task["effective_priority"] == 1 and task["eligible_wait_seconds"] == 600


def test_unblocked_task_joins_after_already_ready_task(h):
    producer, blocked, ready = [h.register(name) for name in ("producer", "blocked", "ready")]
    h.apply(blocked, _intent([resource()], [_dependency(producer, blocked)]))
    h.apply(ready, _intent([resource()]))
    artifact = _artifact(h, producer)
    h.checkpoint(producer, artifacts=[artifact])
    h.apply(blocked, _accept(artifact, blocked))
    queue = [task["participant_id"] for task in h.read()["ready"] if task["resources"]]
    assert queue == [ready["participant_id"], blocked["participant_id"]]
    assert h.acquire(ready)["status"] == "ok"


def _inject_dependencies(h, edges):
    with h.service.store.transaction() as db:
        for producer, consumer in edges:
            dependency = {**_dependency(producer, consumer), "producer_attempt_id": "1",
                          "consumer_attempt_id": "1", "gate": "consumer_accepted"}
            db.execute("INSERT INTO dependencies VALUES (?,?,NULL,?)",
                       (str(uuid.uuid4()), h.identity.repository_id, canonical(dependency)))


def test_intersecting_cycles_quarantine_entire_component_and_held_claim(h):
    a, b, c, independent = [h.register(name) for name in ("a", "b", "c", "independent")]
    assert h.acquire(c, resource("c.py"))["status"] == "ok"
    _inject_dependencies(h, [(a, b), (b, a), (a, c), (c, b)])
    snapshot = h.read()
    assert {p["task_id"] for p in snapshot["participants"] if p["status"] == "suspect"} == {"a", "b", "c"}
    assert snapshot["claims"][0]["quarantined"] is True
    assert snapshot["dependency_cycles_count"] == 1
    assert h.acquire(independent, resource("independent.py"))["status"] == "ok"


def test_long_cycle_errors_and_reconciliation_have_bounded_diagnostics(h):
    actors = [h.register(f"task{index:03}") for index in range(51)]
    _inject_dependencies(h, zip(actors, actors[1:]))
    rejected = h.apply(actors[0], _intent([resource()], [_dependency(actors[-1], actors[0])]))
    assert rejected["error"] == "dependency_cycle"
    assert len(rejected["cycle"]) == 50 and rejected["cycle_truncated"] is True
    # The rejected mutation rolled back; simulate a legacy corrupted closing edge.
    assert h.read()["dependency_cycles_count"] == 0
    _inject_dependencies(h, [(actors[-1], actors[0])])
    snapshot = h.read(limit=100)
    assert len(snapshot["dependency_cycles"][0]) == 50
    assert snapshot["dependency_cycles_count"] == 1 and snapshot["dependency_cycles_truncated"]
    assert all(p["status"] == "suspect" for p in snapshot["participants"])


def test_many_cycle_diagnostics_report_total_and_truncation(h):
    actors = [h.register(f"task{index:03}") for index in range(101)]
    _inject_dependencies(h, [(actor, actor) for actor in actors])
    snapshot = h.read(limit=100)
    assert len(snapshot["dependency_cycles"]) == 100
    assert snapshot["dependency_cycles_count"] == 101 and snapshot["dependency_cycles_truncated"]
    assert all(p["status"] == "suspect" for p in snapshot["participants"])


def test_cancel_receipt_replays_without_reauthorizing_cancelled_actor(h):
    actor = h.register("cancelled")
    request = h.request(actor, {"kind": "cancel", "reason": "task ended"})
    result = h.service.apply(request, h.identity)
    assert result["status"] == "ok" and result["cancelled"]
    assert h.service.apply(request, h.identity) == result
    assert h.apply(actor, {"kind": "heartbeat"})["error"] == "unauthorized_actor"


@pytest.mark.parametrize("state", ["completed", "cancelled", "expired", "rejected", "superseded"])
def test_terminal_agreement_cannot_be_cancelled_again(h, state):
    a, b = h.register("a"), h.register("b")
    agreement = h.apply(a, h.proposal([a, b]))["agreement"]
    # Exercise every stored terminal state, including historical superseded rows.
    agreement.update(state=state, terminal_at=h.now, reason="original outcome")
    with h.service.store.transaction() as db:
        h.service._save_agreement(db, h.identity.repository_id, agreement)
    h.now += 1
    before = h.read()
    request = h.request(b, {"kind": "cancel", "agreement_id": agreement["agreement_id"], "reason": "new outcome"})
    assert h.service.apply(request, h.identity)["error"] == "stale_revision"
    assert h.read() == before
    with h.service.store.transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM receipts WHERE operation_id=?", (request.operation_id,)).fetchone()[0] == 0


def test_agreement_cancel_receipt_replays_after_terminal_guard(h):
    a, b = h.register("a"), h.register("b")
    agreement = h.apply(a, h.proposal([a, b]))["agreement"]
    request = h.request(a, {"kind": "cancel", "agreement_id": agreement["agreement_id"], "reason": "task ended"})
    result = h.service.apply(request, h.identity)
    assert result["status"] == "ok" and result["agreement"]["state"] == "cancelled"
    h.now += 1
    assert h.service.apply(request, h.identity) == result
    assert h.apply(b, {"kind": "cancel", "agreement_id": agreement["agreement_id"], "reason": "later cancellation"})["error"] == "stale_revision"


def test_inert_registration_retention_never_releases_established_ownership(h):
    unused, failed, used, owner, parent = [h.register(name) for name in ("unused", "failed", "used", "owner", "parent")]
    child = h.register("child", parent=parent)
    assert h.apply(used, {"kind": "heartbeat"})["status"] == "ok"
    assert h.acquire(owner)["status"] == "ok"
    assert h.apply(failed, {"kind": "heartbeat"}, capability="wrong-capability")["error"] == "unauthorized_actor"
    h.now += 30 * 86400
    h.service.collect_terminal()
    assert all(p["status"] != "cancelled" for p in h.read()["participants"])
    h.now += 1
    h.service.collect_terminal()
    snapshot = h.read()
    states = {p["task_id"]: p for p in snapshot["participants"]}
    assert {name for name, p in states.items() if p["status"] == "cancelled"} == {"unused", "failed", "child"}
    assert snapshot["claims"][0]["participant_id"] == owner["participant_id"]
    assert snapshot["claims"][0]["quarantined"] is True
    assert states["unused"]["generation"] == unused["generation"] + 1
    assert h.apply(unused, {"kind": "heartbeat"})["error"] == "unauthorized_actor"
    revision = snapshot["revision"]
    h.service.collect_terminal()
    assert h.read()["revision"] == revision
    assert h.register("unused", attempt_id="2")["participant_id"] != unused["participant_id"]


def test_old_journal_migration_never_guesses_unused_registration(h):
    actor = h.register("unknown-history")
    h.service.close()
    with sqlite3.connect(h.service.store.path) as db:
        db.execute("DROP TABLE participant_activity")
        db.execute("DROP TABLE mailbox_notifications")
        db.execute("DROP TABLE mailbox_requests")
        db.execute("DROP TABLE message_receipts")
        db.execute("DROP TABLE messages")
        db.execute("PRAGMA user_version=1")
    h.service.start()
    h.now += 31 * 86400
    h.service.collect_terminal()
    assert h.read()["participants"][0]["status"] == "suspect"
    with h.service.store.transaction(write=False) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.execute("SELECT participant_id FROM participant_activity").fetchone()[0] == actor["participant_id"]


def _cancelled_owner_with_open_agreement(h):
    owner = h.register("cancelled-owner")
    resources = [{**r, "owner": owner["participant_id"]} for r in
                 (resource(), resource("api", "logical_intent"))]
    agreement = h.apply(owner, h.proposal([owner], resources=resources))["agreement"]
    assert h.ack(owner, agreement)["agreement"]["state"] == "committed"
    claims = h.acquire(owner, resource(), resource("api", "logical_intent"),
                       agreement_id="agreement")["claims"]
    assert h.checkpoint(owner, agreement_id="agreement",
                        checks=[{"name": "publication", "status": "failed"}])["status"] == "ok"
    assert h.apply(owner, {"kind": "release", "claims": [
        {"claim_id": c["claim_id"], "expected_revision": c["revision"]} for c in claims],
        "reason": "execution stopped"})["status"] == "ok"
    assert h.apply(owner, {"kind": "cancel", "reason": "task ended"})["cancelled"]
    assert h.read()["agreements"][0]["state"] == "checkpointed"
    return owner


def test_cancelled_owner_can_close_failed_agreement_without_reactivation(h):
    owner = _cancelled_owner_with_open_agreement(h)
    before = next(p for p in h.read()["participants"] if p["participant_id"] == owner["participant_id"])
    successor = h.register("successor")
    resources = [{**r, "owner": successor["participant_id"]} for r in
                 (resource(), resource("api", "logical_intent"))]
    fresh = h.apply(successor, h.proposal([successor], name="fresh", resources=resources))["agreement"]
    assert h.ack(successor, fresh)["error"] == "claim_conflict"

    request = h.request(owner, {"kind": "cancel", "agreement_id": "agreement", "reason": "close failed work"})
    result = h.service.apply(request, h.identity)
    assert result["status"] == "ok", result
    assert result["agreement"]["state"] == "cancelled"
    assert result["agreement"]["terminal_at"] == h.now
    after = next(p for p in h.read()["participants"] if p["participant_id"] == owner["participant_id"])
    for key in ("status", "generation", "running", "intent", "terminal_at", "checkpoint"):
        assert after[key] == before[key]
    assert after["checkpoint"]["checks"][0]["status"] == "failed"
    assert h.ack(successor, fresh)["agreement"]["state"] == "committed"
    assert h.acquire(successor, resource(), resource("api", "logical_intent"),
                     agreement_id="fresh")["status"] == "ok"
    revision = h.read()["revision"]
    assert h.service.apply(request, h.identity) == result
    assert h.read()["revision"] == revision
    assert h.apply(owner, {"kind": "cancel", "agreement_id": "agreement",
                           "reason": "new cancellation"})["error"] == "stale_revision"


@pytest.mark.parametrize("invalid", ["generation", "revision", "capability"])
def test_terminal_agreement_cancel_keeps_authentication_and_cas(h, invalid):
    owner = _cancelled_owner_with_open_agreement(h)
    before = h.read()
    extra = {"generation": {"expected_generation": owner["generation"] + 1},
             "revision": {"expected_revision": before["revision"] - 1},
             "capability": {"capability": "0" * 64}}[invalid]
    result = h.apply(owner, {"kind": "cancel", "agreement_id": "agreement", "reason": "close"},
                     **extra)
    assert result["error"] == {"generation": "stale_generation", "revision": "stale_revision",
                               "capability": "unauthorized_actor"}[invalid]
    after = h.read()
    assert after["revision"] == before["revision"]
    assert after["agreements"] == before["agreements"]


@pytest.mark.parametrize("action", [
    {"kind": "heartbeat"},
    {"kind": "acquire", "resources": [resource()], "work_unit_id": "new", "checkpoint": {"trigger": "done"}},
    {"kind": "checkpoint", "checkpoint_id": "new", "release_ready": True},
    {"kind": "release", "claims": [{"claim_id": "foreign-claim", "expected_revision": 1}], "reason": "not owned"},
    {"kind": "cancel", "reason": "cancel task again"},
])
def test_terminal_agreement_cleanup_grants_no_execution_actions(h, action):
    owner = _cancelled_owner_with_open_agreement(h)
    assert h.apply(owner, action)["error"] == "unauthorized_actor"
    assert h.recover(owner)["error"] == "unauthorized_actor"


def test_terminal_agreement_cancel_preserves_other_execution_claims(h):
    owner = _cancelled_owner_with_open_agreement(h)
    other = h.register("other")
    agreement = h.apply(other, h.proposal([other], name="foreign",
        resources=[{**resource("foreign.py"), "owner": other["participant_id"]}]))["agreement"]
    h.ack(other, agreement)
    held = h.acquire(other, resource("foreign.py"), agreement_id="foreign")["claims"]
    assert h.apply(owner, {"kind": "cancel", "agreement_id": "foreign",
                           "reason": "not my agreement"})["error"] == "unauthorized_actor"
    assert h.apply(owner, {"kind": "cancel", "agreement_id": "agreement",
                           "reason": "close my agreement"})["status"] == "ok"
    assert h.read()["claims"] == held
    assert next(a for a in h.read()["agreements"] if a["agreement_id"] == "foreign")["state"] == "active"


@pytest.mark.parametrize("cancelled", ["parent", "child"])
def test_terminal_agreement_cancel_cannot_use_parent_relay(h, cancelled):
    parent = h.register("parent")
    child = h.register("child", parent=parent)
    agreement = h.apply(child, h.proposal([child]))["agreement"]
    h.ack(child, agreement)
    actor = parent if cancelled == "parent" else child
    assert h.apply(actor, {"kind": "cancel", "reason": "task ended"})["cancelled"]
    before = h.read()
    result = h.apply(parent, {"kind": "cancel", "agreement_id": "agreement", "reason": "relay"},
                     subject_id=child["participant_id"], expected_generation=child["generation"])
    assert result["error"] == "unauthorized_actor"
    assert h.read()["revision"] == before["revision"]
    assert h.read()["agreements"] == before["agreements"]


def test_terminal_agreement_cancel_rejects_revoked_capability(h):
    owner = _cancelled_owner_with_open_agreement(h)
    # Model revoked credentials in this test's private journal, never live state.
    with h.service.store.transaction() as db:
        db.execute("UPDATE participants SET verifier=? WHERE id=?", ("revoked", owner["participant_id"]))
    assert h.apply(owner, {"kind": "cancel", "agreement_id": "agreement",
                           "reason": "close"})["error"] == "unauthorized_actor"


def test_terminal_agreement_cancel_survives_restart_and_disablement(h):
    owner = _cancelled_owner_with_open_agreement(h)
    h.service.close()
    h.service.start()
    h.service.enabled = False
    assert h.apply(owner, {"kind": "cancel", "agreement_id": "agreement",
                           "reason": "close metadata only"})["status"] == "ok"
    assert h.apply(owner, {"kind": "heartbeat"})["error"] == "unauthorized_actor"


@pytest.mark.parametrize("inconsistent", ["running", "claim"])
def test_terminal_agreement_cancel_refuses_inconsistent_execution_state(h, inconsistent):
    owner = _cancelled_owner_with_open_agreement(h)
    # Simulate an imported inconsistent journal in this test's private state.
    with h.service.store.transaction() as db:
        if inconsistent == "running":
            participant = h.service._participant(db, h.identity.repository_id, owner["participant_id"])
            participant["running"] = True
            h.service._save_participant(db, participant)
    if inconsistent == "claim":
        other = h.register("claim-source")
        claim = h.acquire(other, resource("other.py"))["claims"][0]
        with h.service.store.transaction() as db:
            claim["participant_id"] = owner["participant_id"]
            db.execute("UPDATE claims SET body=? WHERE id=?", (canonical(claim), claim["claim_id"]))
    before = h.read()
    assert h.apply(owner, {"kind": "cancel", "agreement_id": "agreement",
                           "reason": "close"})["error"] == "scope_denied"
    after = h.read()
    assert after["revision"] == before["revision"]
    assert after["claims"] == before["claims"]
    assert after["agreements"] == before["agreements"]


# A reviewer identity this source knows nothing about, used to prove that the
# allowance queue needs no entry for the reviewer it is protecting.
UNKNOWN_REVIEWER = "Nebula-Reviewer[bot]"


def _allowance(reviewer, account="IEZhu", host="github.com"):
    return quota.resource(quota.quota_key(host, account, reviewer))


def _release(h, actor, *, reason="round complete"):
    claims = [c for c in h.read()["claims"] if c["participant_id"] == actor["participant_id"]]
    return h.apply(actor, {"kind": "release", "reason": reason,
                           "claims": [{"claim_id": c["claim_id"], "expected_revision": c["revision"]} for c in claims]})


def test_unknown_reviewer_allowance_queues_sessions_on_a_published_default_backoff(h):
    allowance = _allowance(UNKNOWN_REVIEWER)
    first, second = h.register("pr-653", scope=[allowance]), h.register("pr-661", scope=[allowance])
    acquired = h.acquire(first, allowance)
    assert acquired["status"] == "ok"
    assert acquired["claims"][0]["resource"]["resource_key"] == "github.com/iezhu:nebula-reviewer"
    # Another pull request, another session, the same account-wide allowance,
    # named from a different platform surface: still one claimed resource.
    # A raw key from a client that never called the helper: the service is what
    # makes two spellings of one reviewer the same claimed allowance.
    refused = h.acquire(second, {"resource_type": "external_quota", "access": "write",
                                 "resource_key": "GitHub.com/IEZhu:Nebula-Reviewer[bot]"})
    assert refused["error"] == "claim_conflict"
    assert refused["owner"] == first["participant_id"]
    assert refused["resource_key"] == allowance["resource_key"]
    assert refused.get("next_free_at") is None
    pause = quota.observed_pause(UNKNOWN_REVIEWER, "Nebula: this account's review budget is spent.", observed_at=h.now)
    assert (pause.source, pause.wait_seconds) == ("default", quota.DEFAULT_BACKOFF_SECONDS)
    assert h.checkpoint(first, external_quota=[quota.pause_observation(allowance["resource_key"], pause)])["status"] == "ok"
    published = h.acquire(second, allowance)
    assert published["error"] == "claim_conflict"
    assert published["next_free_at"] == h.now + quota.DEFAULT_BACKOFF_SECONDS
    assert _release(h, first, reason="pause published")["status"] == "ok"
    assert h.acquire(second, allowance)["status"] == "ok"


def test_two_reviewers_on_one_pull_request_are_held_independently(h):
    known, unknown = _allowance("coderabbitai[bot]"), _allowance(UNKNOWN_REVIEWER)
    scope = [known, unknown]
    a, b, c = h.register("a", scope=scope), h.register("b", scope=scope), h.register("c", scope=scope)
    assert h.acquire(a, known)["status"] == "ok"
    assert h.acquire(b, unknown)["status"] == "ok"
    assert [claim["resource"]["resource_key"] for claim in h.read()["claims"]] == [
        known["resource_key"], unknown["resource_key"]]
    assert h.acquire(c, known)["error"] == "claim_conflict"
    assert h.acquire(c, unknown)["error"] == "claim_conflict"
    assert h.checkpoint(a, name="round-a")["status"] == "ok"
    assert _release(h, a)["status"] == "ok"
    assert h.acquire(c, known)["status"] == "ok"


def test_heartbeat_expiry_frees_an_allowance_without_freeing_a_file(h):
    allowance, file = _allowance(UNKNOWN_REVIEWER), resource("src/a.py")
    dead, live = h.register("dead", scope=[allowance, file]), h.register("live", scope=[allowance, file])
    assert h.acquire(dead, allowance, file)["status"] == "ok"
    for _ in range(PAST_WINDOW_MINUTES):
        h.now += 60
        assert h.apply(live, {"kind": "heartbeat"})["status"] == "ok"
    quarantined = {c["resource"]["resource_key"]: c["quarantined"] for c in h.read()["claims"]}
    assert quarantined == {allowance["resource_key"]: True, "checkout_test:src/a.py": True}
    # The dead session's edits are still in the checkout, so its file claim holds.
    assert h.acquire(live, file)["error"] == "claim_conflict"
    taken = h.acquire(live, allowance)
    assert taken["status"] == "ok", taken
    # A session that comes back has to acquire again, and now finds it held.
    assert h.recover(dead)["status"] == "ok"
    assert h.acquire(dead, allowance)["error"] == "claim_conflict"


def test_a_quarantined_holder_that_is_still_alive_keeps_its_allowance(h):
    allowance = _allowance(UNKNOWN_REVIEWER)
    holder, other = h.register("holder", scope=[allowance]), h.register("other", scope=[allowance])
    assert h.acquire(holder, allowance)["status"] == "ok"
    assert h.apply(holder, {"kind": "cancel", "reason": "stop this round"})["cancel_pending"] is True
    assert [c["quarantined"] for c in h.read()["claims"]] == [True]
    assert h.acquire(other, allowance)["error"] == "claim_conflict"


def test_an_automatic_review_nobody_requested_is_published_as_consumed_allowance(h):
    allowance = _allowance("coderabbitai[bot]")
    observer = h.register("observer", scope=[allowance])
    record = quota.consumed_observation(allowance["resource_key"],
                                        "CodeRabbit reviewed the push automatically; nobody requested it.",
                                        observed_at=h.now)
    assert h.checkpoint(observer, external_quota=[record])["status"] == "ok"
    participant = [p for p in h.read()["participants"] if p["participant_id"] == observer["participant_id"]][0]
    assert participant["checkpoint"]["external_quota"] == [record]
    assert not h.read()["claims"]


def test_an_allowance_registered_as_a_snapshot_read_is_refused(h):
    allowance = _allowance(UNKNOWN_REVIEWER)
    reading = {**allowance, "access": "snapshot_read"}
    # A snapshot read conflicts with nothing, so a scope that declares one for
    # an allowance would let its session acquire the key a writer is holding.
    refused = h.service.register(parse_register(
        {"protocol": PROTOCOL, "checkout_path": str(h.checkout), "task_id": "reader",
         "attempt_id": "1", "root_task_id": "reader", "scope": [reading]}), h.identity)
    assert refused["error"] == "scope_denied"
    holder = h.register("holder", scope=[allowance])
    assert h.acquire(holder, allowance)["status"] == "ok"
    reader = h.register("reader", scope=[allowance])
    assert h.acquire(reader, reading)["error"] == "scope_denied"
    assert h.acquire(reader, {**allowance, "access": "integrate"})["error"] == "scope_denied"
    assert [c["participant_id"] for c in h.read()["claims"]] == [holder["participant_id"]]


def test_a_published_pause_cannot_push_other_sessions_past_the_capped_wait(h):
    allowance = _allowance(UNKNOWN_REVIEWER)
    holder, other = h.register("holder", scope=[allowance]), h.register("other", scope=[allowance])
    assert h.acquire(holder, allowance)["status"] == "ok"
    capped = quota.pause_observation(allowance["resource_key"], quota.observed_pause(
        UNKNOWN_REVIEWER, "Nebula is out of budget, retry after 999999999 minutes", observed_at=h.now))
    assert capped["next_free_at"] == h.now + quota.MAX_WAIT_SECONDS
    assert h.checkpoint(holder, name="capped", external_quota=[capped])["status"] == "ok"
    assert h.acquire(other, allowance)["next_free_at"] == h.now + quota.MAX_WAIT_SECONDS
    # A record a client builds itself is refused at the boundary, so the time
    # the service hands to every other session stays inside the cap.
    with pytest.raises(ValueError, match="maximum"):
        h.checkpoint(holder, name="absurd",
                     external_quota=[{**capped, "next_free_at": h.now + quota.MAX_WAIT_SECONDS + 1}])


def _participant(h, actor):
    return [p for p in h.read()["participants"] if p["participant_id"] == actor["participant_id"]][0]


def test_a_published_pause_outlives_the_work_unit_that_published_it(h):
    allowance = _allowance(UNKNOWN_REVIEWER)
    holder, other = h.register("holder", scope=[allowance]), h.register("other", scope=[allowance])
    assert h.acquire(holder, allowance)["status"] == "ok"
    pause = quota.observed_pause(UNKNOWN_REVIEWER, "Nebula: this account is out of review budget.", observed_at=h.now)
    assert h.checkpoint(holder, name="round-1",
                        external_quota=[quota.pause_observation(allowance["resource_key"], pause)])["status"] == "ok"
    assert _release(h, holder, reason="pause published")["status"] == "ok"
    # The next work unit clears the checkpoint while the reviewer stays paused,
    # so the time every other session reads cannot live only in it.
    assert h.acquire(holder, allowance)["status"] == "ok"
    assert _participant(h, holder)["checkpoint"] is None
    refused = h.acquire(other, allowance)
    assert refused["error"] == "claim_conflict"
    assert refused["next_free_at"] == pause.next_free_at
    assert [(o["kind"], o["next_free_at"]) for o in _participant(h, holder)["external_quota"]] == [
        ("paused", pause.next_free_at)]


def test_a_lapsed_allowance_frees_an_agreement_assignment(h):
    allowance, file = _allowance(UNKNOWN_REVIEWER), resource("src/a.py")
    scope = [allowance, file]
    dead, live = h.register("dead", scope=scope), h.register("live", scope=scope)
    assigned = [{**allowance, "owner": dead["participant_id"]}, {**file, "owner": dead["participant_id"]}]
    agreement = h.apply(dead, h.proposal([dead, live], resources=assigned))["agreement"]
    h.ack(dead, agreement)
    assert h.ack(live, agreement)["agreement"]["state"] == "committed"
    assert h.acquire(dead, allowance, file, agreement_id="agreement")["status"] == "ok"
    for _ in range(PAST_WINDOW_MINUTES):
        h.now += 60
        assert h.apply(live, {"kind": "heartbeat"})["status"] == "ok"
    # The assignment outlives the holder's heartbeat, so the allowance has to
    # stop excluding here too, while the file assignment still holds.
    assert h.acquire(live, file)["error"] == "claim_conflict"
    assert h.acquire(live, allowance)["status"] == "ok"


def _pause(h, reviewer, *, observed_at=None, wait=3600.0):
    key = quota.quota_key("github.com", "IEZhu", reviewer)
    pause = quota.observed_pause(reviewer, "limit reached for this account", observed_at=observed_at or h.now,
                                 default_seconds=wait)
    return quota.pause_observation(key, pause)


def test_an_observation_dated_past_the_coordinator_clock_is_refused(h):
    allowance = _allowance(UNKNOWN_REVIEWER)
    holder = h.register("holder", scope=[allowance])
    assert h.acquire(holder, allowance)["status"] == "ok"
    # A client reads the platform's own time, so some skew is tolerated.
    near = _pause(h, UNKNOWN_REVIEWER, observed_at=h.now + 60)
    assert h.checkpoint(holder, name="near", external_quota=[near])["status"] == "ok"
    future = _pause(h, UNKNOWN_REVIEWER, observed_at=h.now + 86400)
    refused = h.checkpoint(holder, name="future", external_quota=[future])
    assert refused["error"] == "invalid_request"
    other = h.register("other", scope=[allowance])
    assert h.acquire(other, allowance)["next_free_at"] == near["next_free_at"]


def test_the_newest_evidence_wins_not_the_newest_publication(h):
    allowance = _allowance(UNKNOWN_REVIEWER)
    holder, other = h.register("holder", scope=[allowance]), h.register("other", scope=[allowance])
    assert h.acquire(holder, allowance)["status"] == "ok"
    newer = _pause(h, UNKNOWN_REVIEWER, observed_at=h.now, wait=3600.0)
    older = _pause(h, UNKNOWN_REVIEWER, observed_at=h.now - 100, wait=60.0)
    assert h.checkpoint(holder, name="newer", external_quota=[newer])["status"] == "ok"
    # A session may read an older response after a newer one; the later
    # publication must not shorten the wait every other session repeats.
    assert h.checkpoint(holder, name="older", external_quota=[older])["status"] == "ok"
    assert h.acquire(other, allowance)["next_free_at"] == newer["next_free_at"]
    published = _participant(h, holder)["external_quota"]
    assert [observation["observed_at"] for observation in published] == [newer["observed_at"]]


def test_the_published_bound_drops_spent_records_before_a_live_pause(h):
    allowance = _allowance(UNKNOWN_REVIEWER)
    others = [_allowance(f"reviewer-{index}") for index in range(49)]
    scope = [allowance, *others]
    holder, other = h.register("holder", scope=scope), h.register("other", scope=scope)
    assert h.acquire(holder, allowance)["status"] == "ok"
    spent = [quota.consumed_observation(record["resource_key"], "an automatic review nobody requested",
                                        observed_at=h.now + index)
             for index, record in enumerate([allowance, *others])]
    assert h.checkpoint(holder, name="spent", external_quota=spent)["status"] == "ok"
    live = _pause(h, UNKNOWN_REVIEWER)
    assert h.checkpoint(holder, name="live", external_quota=[live])["status"] == "ok"
    published = _participant(h, holder)["external_quota"]
    # Fifty keys and fifty one records, because a pause and a spent record of
    # one allowance are two: the record that goes is never the live pause.
    assert len(published) == 50
    assert live in published
    assert h.acquire(other, allowance)["next_free_at"] == live["next_free_at"]
    crowd = [_pause(h, f"reviewer-{index}", observed_at=h.now + index) for index in range(49)]
    assert h.checkpoint(holder, name="crowd", external_quota=crowd)["status"] == "ok"
    assert len([o for o in _participant(h, holder)["external_quota"] if o["kind"] == "paused"]) == 50


def test_a_lapsed_assignee_does_not_block_a_new_agreement(h):
    allowance, file = _allowance(UNKNOWN_REVIEWER), resource("src/a.py")
    scope = [allowance, file]
    dead, peer = h.register("dead", scope=scope), h.register("peer", scope=scope)
    old = h.apply(dead, h.proposal([dead, peer], resources=[{**allowance, "owner": dead["participant_id"]},
                                                            {**file, "owner": dead["participant_id"]}]))["agreement"]
    h.ack(dead, old)
    assert h.ack(peer, old)["agreement"]["state"] == "committed"
    fresh_a, fresh_b = h.register("fresh-a", scope=scope), h.register("fresh-b", scope=scope)
    for _ in range(3):
        h.now += 60
        for live in (peer, fresh_a, fresh_b):
            assert h.apply(live, {"kind": "heartbeat"})["status"] == "ok"
    # The lapsed session's assignment cannot keep agreement coordination shut
    # for the allowance, while its file assignment still does.
    blocked = h.apply(fresh_a, h.proposal([fresh_a, fresh_b], name="files",
                                          resources=[{**file, "owner": fresh_a["participant_id"]}]))["agreement"]
    h.ack(fresh_a, blocked)
    assert h.ack(fresh_b, blocked)["error"] == "claim_conflict"
    allowed = h.apply(fresh_a, h.proposal([fresh_a, fresh_b], name="allowance",
                                          resources=[{**allowance, "owner": fresh_a["participant_id"]}]))["agreement"]
    h.ack(fresh_a, allowed)
    assert h.ack(fresh_b, allowed)["agreement"]["state"] == "committed"


def test_only_a_pause_contributes_a_published_next_free_time():
    key = "github.com/iezhu:coderabbitai"
    holder = {"external_quota": [
        {"resource_key": key, "kind": "consumed", "evidence": "spent", "observed_at": 1000.0,
         "next_free_at": 9000.0, "wait_source": None},
        {"resource_key": key, "kind": "paused", "evidence": "limit reached", "observed_at": 1000.0,
         "next_free_at": 4600.0, "wait_source": "default"}]}
    # The model refuses a consumed record that carries a time, and the reader
    # does not lean on that alone: a row stored before the rule would still be
    # read, and only a pause may say when the reviewer is free again.
    assert _published_next_free(holder, key) == 4600.0


def test_a_lapsed_allowance_does_not_block_a_handoff_commit(h):
    allowance, file = _allowance(UNKNOWN_REVIEWER), resource("src/a.py")
    scope = [allowance, file]
    dead = h.register("dead", scope=scope)
    assert h.acquire(dead, allowance, file)["status"] == "ok"
    old, new = h.register("old", scope=scope), h.register("new", scope=scope)
    assert h.checkpoint(old, name="cp")["status"] == "ok"
    for _ in range(PAST_WINDOW_MINUTES):
        h.now += 60
        for live in (old, new):
            assert h.apply(live, {"kind": "heartbeat"})["status"] == "ok"
    # The handoff branch checks claims of its own, so the lapse exception has
    # to hold there as well: a dead session's allowance claim excludes nobody,
    # while its file claim still does, because its edits are in the checkout.
    files = h.apply(old, h.proposal([old, new], name="files", owner=old, pattern="handoff",
                                    resources=[{**file, "owner": old["participant_id"]}]))["agreement"]
    h.ack(old, files)
    assert h.ack(new, files, accepted_checkpoint="cp")["error"] == "claim_conflict"
    quota_handoff = h.apply(old, h.proposal([old, new], name="allowance", owner=old, pattern="handoff",
                                            resources=[{**allowance, "owner": old["participant_id"]}]))["agreement"]
    h.ack(old, quota_handoff)
    assert h.ack(new, quota_handoff, accepted_checkpoint="cp")["agreement"]["state"] == "committed"


def test_an_observation_about_an_unregistered_allowance_is_refused(h):
    allowance, foreign = _allowance(UNKNOWN_REVIEWER), _allowance("coderabbitai[bot]")
    holder = h.register("holder", scope=[allowance])
    assert h.acquire(holder, allowance)["status"] == "ok"
    # Every session reads published observations and holds its request on them,
    # so a session could otherwise pause a reviewer it never registered and
    # suppress allowance it has nothing to do with.
    refused = h.checkpoint(holder, name="foreign", external_quota=[
        quota.pause_observation(foreign["resource_key"], quota.observed_pause(
            "coderabbitai[bot]", "limit reached", observed_at=h.now))])
    assert refused["error"] == "scope_denied"
    assert h.checkpoint(holder, name="own", external_quota=[_pause(h, UNKNOWN_REVIEWER)])["status"] == "ok"
    assert not _participant(h, holder)["external_quota"] or [
        o["resource_key"] for o in _participant(h, holder)["external_quota"]] == [allowance["resource_key"]]


def test_the_live_pause_bound_cannot_be_exceeded_while_scope_shares_it():
    # Publishing is scope bound and a scope holds at most MAX_ITEMS resources,
    # so a participant cannot carry more live pauses than the published list
    # allows. That is why the bound check in _checkpoint cannot be reached, and
    # this pins the two facts that make it so: if either bound moves on its
    # own, the guard becomes reachable and this test says where to look.
    from src.cooperation import models, service
    assert service.MAX_ITEMS is models.MAX_ITEMS
    scope = [quota.resource(quota.quota_key("github.com", "IEZhu", f"reviewer-{index}"))
             for index in range(models.MAX_ITEMS + 1)]
    with pytest.raises(ValueError, match="scope"):
        parse_register({"protocol": PROTOCOL, "checkout_path": "/tmp", "task_id": "too-wide",
                        "attempt_id": "1", "root_task_id": "too-wide", "scope": scope})
    assert len(parse_register({"protocol": PROTOCOL, "checkout_path": "/tmp", "task_id": "at-the-bound",
                               "attempt_id": "1", "root_task_id": "at-the-bound",
                               "scope": scope[:-1]}).scope) == models.MAX_ITEMS


def test_a_recovered_holder_does_not_block_the_session_that_took_its_allowance(h):
    allowance = _allowance(UNKNOWN_REVIEWER)
    dead, peer = h.register("dead", scope=[allowance]), h.register("peer", scope=[allowance])
    held = h.apply(dead, h.proposal([dead, peer], resources=[
        {**allowance, "owner": dead["participant_id"]}]))["agreement"]
    h.ack(dead, held)
    assert h.ack(peer, held)["agreement"]["state"] == "committed"
    assert h.acquire(dead, allowance, agreement_id="agreement")["status"] == "ok"
    taker, witness = h.register("taker", scope=[allowance]), h.register("witness", scope=[allowance])
    for _ in range(PAST_WINDOW_MINUTES):
        h.now += 60
        for live in (peer, taker, witness):
            assert h.apply(live, {"kind": "heartbeat"})["status"] == "ok"
    fresh = h.apply(taker, h.proposal([taker, witness], name="fresh", resources=[
        {**allowance, "owner": taker["participant_id"]}]))["agreement"]
    h.ack(taker, fresh)
    assert h.ack(witness, fresh)["agreement"]["state"] == "committed"
    # The holder comes back: recovery advances its generation and leaves the
    # old agreement suspended behind it. Its assignment must stop excluding
    # the session that took the allowance while it was away, or the two block
    # each other for good, since the holder cannot acquire through a suspended
    # agreement either.
    assert h.recover(dead)["status"] == "ok"
    assert h.apply(dead, {"kind": "heartbeat"})["status"] == "ok"
    assert h.acquire(taker, allowance, agreement_id="fresh")["status"] == "ok"
    # A file assignment from the same recovered holder keeps excluding.
    file = resource("src/a.py")
    other_dead = h.register("other-dead", scope=[file])
    neighbour = h.register("neighbour", scope=[file])
    files = h.apply(other_dead, h.proposal([other_dead, neighbour], name="files", resources=[
        {**file, "owner": other_dead["participant_id"]}]))["agreement"]
    h.ack(other_dead, files)
    assert h.ack(neighbour, files)["agreement"]["state"] == "committed"
    assert h.acquire(other_dead, file, agreement_id="files")["status"] == "ok"
    assert h.recover(other_dead)["status"] == "ok"
    assert h.acquire(neighbour, file)["error"] == "claim_conflict"


# Two sessions that act only at their turn boundaries, as every Claude Code
# session does, are never both live inside one 120-second window: each comes
# back past the heartbeat deadline and recovers into a new generation before it
# can act. On 2026-11-26 agreement docs-status-vs-workmap-readme-1 expired after
# one acknowledgement, and its second revision was refused with stale_generation.
TURN_GAP = 600.0  # ten minutes between one session's turns


def _bindings(agreement):
    return {binding["participant_id"]: binding["generation"] for binding in agreement["task_bindings"]}


def test_turn_based_sessions_commit_an_agreement_minutes_apart_across_a_recover(h):
    peer = h.register("peer")
    h.now += TURN_GAP
    proposer = h.register("proposer")
    # The proposer proposes and consents in one batch while its peer is quiet.
    proposed = h.apply(proposer, h.proposal([proposer, peer]))
    assert proposed["status"] == "ok", proposed
    proposed = proposed["agreement"]
    assert h.ack(proposer, proposed)["acknowledged"] == [proposer["participant_id"]]
    # The peer's next turn comes minutes later: it recovers first, then consents.
    h.now += TURN_GAP
    assert h.recover(peer)["status"] == "ok"
    assert peer["generation"] == 2
    committed = h.ack(peer, proposed)
    assert committed["status"] == "ok", committed
    assert sorted(committed["acknowledged"]) == sorted([proposer["participant_id"], peer["participant_id"]])
    agreement = committed["agreement"]
    # The proposer has gone quiet meanwhile, so the agreement is born suspended,
    # as a live commit would become two minutes later, and it records the
    # generation each party consented from.
    assert (agreement["state"], agreement["reason"]) == ("suspended", "heartbeat_overdue")
    assert _bindings(agreement) == {proposer["participant_id"]: 1, peer["participant_id"]: 2}
    # Its assignment holds against everyone else and admits its owner, who
    # recovers at its own next turn and acquires without naming the agreement.
    other = h.register("other")
    assert h.acquire(other)["error"] == "claim_conflict"
    h.now += TURN_GAP
    assert h.recover(proposer)["status"] == "ok"
    assert h.acquire(proposer)["status"] == "ok"


def test_a_peers_quiet_spell_does_not_void_consent_inside_the_window(h):
    # The same half without the window: a coordinator restart makes both
    # parties suspect before the default deadline. The earlier consent still
    # counts, and the peer that recovered first consents from its new generation.
    proposer, peer = h.register("proposer"), h.register("peer")
    proposed = h.apply(proposer, h.proposal([proposer, peer]))["agreement"]
    assert h.ack(proposer, proposed)["status"] == "ok"
    h.service.close()
    h.service.start()
    assert h.recover(peer)["status"] == "ok"
    committed = h.ack(peer, proposed)
    assert committed["status"] == "ok", committed
    assert (committed["agreement"]["state"], committed["agreement"]["reason"]) == ("suspended", "coordinator_restart")


def test_a_recovery_after_consenting_leaves_that_consent_behind(h):
    proposer, peer = h.register("proposer"), h.register("peer")
    proposed = h.apply(proposer, h.proposal([proposer, peer]))["agreement"]
    assert h.ack(proposer, proposed)["status"] == "ok"
    h.now += TURN_GAP
    # The proposer comes back first and recovers; its new generation has not
    # consented to anything, so the peer cannot commit on the old consent.
    assert h.recover(proposer)["status"] == "ok"
    assert h.recover(peer)["status"] == "ok"
    before = h.read()["agreements"]
    refused = h.ack(peer, proposed)
    assert (refused.get("error"), refused.get("participant_id")) == ("stale_generation", proposer["participant_id"]), refused
    assert h.read()["agreements"] == before
    # Consenting again from the new generation is what lets the peer commit.
    assert h.ack(proposer, proposed)["status"] == "ok"
    committed = h.ack(peer, proposed)
    assert committed["agreement"]["state"] == "committed", committed
    assert _bindings(committed["agreement"]) == {proposer["participant_id"]: 2, peer["participant_id"]: 2}


def test_an_allowance_assigned_to_a_peer_that_recovered_before_consenting_still_excludes(h):
    # The commit binds the consenting generation, which the assignment checks
    # read: the proposal's older reading would free the allowance at once.
    allowance = _allowance(UNKNOWN_REVIEWER)
    proposer, owner = h.register("proposer", scope=[allowance]), h.register("owner", scope=[allowance])
    proposed = h.apply(proposer, h.proposal([proposer, owner], resources=[
        {**allowance, "owner": owner["participant_id"]}]))["agreement"]
    assert h.ack(proposer, proposed)["status"] == "ok"
    assert h.recover(owner)["status"] == "ok"
    committed = h.ack(owner, proposed)
    assert committed["agreement"]["state"] == "committed", committed
    taker = h.register("taker", scope=[allowance])
    assert h.acquire(taker, allowance)["error"] == "claim_conflict"
    assert h.acquire(owner, allowance, agreement_id="agreement")["status"] == "ok"


def test_a_party_that_recovers_after_the_commit_cannot_execute_on_its_old_consent(h):
    writer, reviewer = h.register("writer"), h.register("reviewer")
    proposed = h.apply(writer, h.proposal([writer, reviewer]))["agreement"]
    assert h.ack(writer, proposed)["status"] == "ok"
    assert h.ack(reviewer, proposed)["agreement"]["state"] == "committed"
    # A recovery from a live state leaves the agreement committed, but the new
    # generation never consented to it, so it cannot work under it.
    assert h.recover(writer)["status"] == "ok"
    refused = h.acquire(writer, agreement_id="agreement")
    assert (refused.get("error"), refused.get("participant_id")) == ("stale_generation", writer["participant_id"]), refused


def test_two_recovered_consenters_can_each_renew_their_consent(h):
    # Renewing consent never waits on another party's recovery; only the final
    # acknowledgement needs every earlier consent current.
    first, second, last = h.register("first"), h.register("second"), h.register("last")
    proposed = h.apply(first, h.proposal([first, second, last]))["agreement"]
    assert h.ack(first, proposed)["status"] == "ok"
    assert h.ack(second, proposed)["status"] == "ok"
    h.now += TURN_GAP
    for party in (first, second, last):
        assert h.recover(party)["status"] == "ok"
    refused = h.ack(last, proposed)
    assert refused.get("error") == "stale_generation", refused
    assert refused.get("participant_id") in (first["participant_id"], second["participant_id"]), refused
    assert h.ack(first, proposed)["acknowledged"] == sorted([first["participant_id"], second["participant_id"]])
    assert h.ack(second, proposed)["status"] == "ok"
    committed = h.ack(last, proposed)
    assert committed["agreement"]["state"] == "committed", committed
    assert set(_bindings(committed["agreement"]).values()) == {2}


# acquire advertises checkpoint_due, max_interval_seconds after the call, and a
# caller plans its work unit against that time. Liveness used to be a fixed 120
# seconds after the last successful call, so a holder that checkpointed or
# released two to five minutes after its acquire, inside the advertised window,
# was already suspect and was refused with stale_generation until it recovered
# (#628).
@pytest.mark.parametrize(("checkpoint_at", "release_at"), [(150.0, 150.0), (0.0, 290.0), (220.0, 290.0)])
def test_a_holder_checkpoints_and_releases_inside_the_advertised_window(h, checkpoint_at, release_at):
    holder = h.register("holder")
    start = h.now
    claim = h.acquire(holder)["claims"][0]
    assert claim["checkpoint_due"] == start + WINDOW
    h.now = start + checkpoint_at
    # Another session's read reconciles the journal first, as on a busy checkout.
    assert h.read()["claims"][0]["quarantined"] is False
    checkpoint = h.checkpoint(holder)
    assert checkpoint["status"] == "ok", checkpoint
    h.now = start + release_at
    assert h.read()["claims"][0]["quarantined"] is False
    released = _release(h, holder)
    assert released["status"] == "ok", released
    assert _participant(h, holder)["status"] == "active"


def test_releasing_early_does_not_shorten_the_window(h):
    holder = h.register("holder")
    start = h.now
    assert h.acquire(holder)["status"] == "ok"
    assert h.checkpoint(holder)["status"] == "ok"
    assert _release(h, holder)["status"] == "ok"
    # Past 120 seconds after the release, still inside the released unit's window:
    # finishing sooner must not cost the participant the next acquire.
    h.now = start + 200
    acquired = h.acquire(holder, resource("next.py"))
    assert acquired["status"] == "ok", acquired


def test_past_its_window_a_holder_keeps_the_heartbeat_cadence(h):
    holder = h.register("holder")
    start = h.now
    assert h.acquire(holder)["status"] == "ok"
    h.now = start + 250
    assert h.apply(holder, {"kind": "heartbeat"})["status"] == "ok"
    # Past checkpoint_due a call within 120 seconds of the last one still keeps
    # the holder live, and the heartbeat says that the checkpoint is overdue.
    h.now = start + 250 + 120
    late = h.apply(holder, {"kind": "heartbeat"})
    assert late["status"] == "ok", late
    assert late["checkpoint_required"]
    # A heartbeat does not renew the whole window: 121 quiet seconds are too many.
    h.now += 121
    assert h.apply(holder, {"kind": "heartbeat"})["error"] == "stale_generation"


def test_a_window_shorter_than_the_heartbeat_deadline_keeps_the_deadline(h):
    holder = h.register("holder")
    start = h.now
    claim = h.acquire(holder, checkpoint={"trigger": "unit done", "max_interval_seconds": 60})["claims"][0]
    assert claim["checkpoint_due"] == start + 60
    h.now = start + 120
    assert h.apply(holder, {"kind": "heartbeat"})["status"] == "ok"
    h.now += 121
    assert h.apply(holder, {"kind": "heartbeat"})["error"] == "stale_generation"


def test_a_holder_that_stops_calling_is_suspected_when_its_window_closes(h):
    allowance, file = _allowance(UNKNOWN_REVIEWER), resource("src/a.py")
    dead, live = h.register("dead", scope=[allowance, file]), h.register("live", scope=[allowance, file])
    due = h.acquire(dead, allowance, file)["claims"][0]["checkpoint_due"]
    while h.now + 60 < due:
        h.now += 60
        assert h.apply(live, {"kind": "heartbeat"})["status"] == "ok"
    h.now = due
    assert h.apply(live, {"kind": "heartbeat"})["status"] == "ok"
    # Up to its checkpoint_due a silent holder may still be at work, so even
    # its allowance keeps excluding.
    assert _participant(h, dead)["status"] == "active"
    assert h.acquire(live, allowance)["error"] == "claim_conflict"
    h.now = due + 1
    assert h.apply(live, {"kind": "heartbeat"})["status"] == "ok"
    suspect = _participant(h, dead)
    assert (suspect["status"], suspect["reason"]) == ("suspect", "heartbeat_overdue")
    assert all(claim["quarantined"] for claim in h.read()["claims"])
    # From here on exactly as before: the file claim holds, the allowance frees.
    assert h.acquire(live, file)["error"] == "claim_conflict"
    assert h.acquire(live, allowance)["status"] == "ok"
    # The refusal names whose liveness lapsed and why, not only the generation.
    refused = h.checkpoint(dead)
    assert (refused["error"], refused["participant_id"], refused["reason"]) == (
        "stale_generation", dead["participant_id"], "heartbeat_overdue"), refused


def test_a_quarantined_allowance_excludes_until_its_holders_window_closes(h):
    allowance = _allowance(UNKNOWN_REVIEWER)
    holder, other = h.register("holder", scope=[allowance]), h.register("other", scope=[allowance])
    start = h.now
    assert h.acquire(holder, allowance)["status"] == "ok"
    assert h.apply(holder, {"kind": "cancel", "reason": "stop this round"})["cancel_pending"] is True
    # Quarantined at once, yet the holder may still be finishing the round it
    # declared: its allowance excludes for as long as that window is open.
    for at in (100.0, 200.0):
        h.now = start + at
        assert h.apply(other, {"kind": "heartbeat"})["status"] == "ok"
        assert h.acquire(other, allowance)["error"] == "claim_conflict"
    h.now = start + WINDOW + 1
    assert h.acquire(other, allowance)["status"] == "ok"


def test_recovery_ends_the_work_units_window(h):
    holder = h.register("holder")
    start = h.now
    assert h.acquire(holder)["status"] == "ok"
    h.service.close()
    h.service.start()
    assert h.recover(holder)["status"] == "ok"
    # The recovered generation holds nothing and never acquired: the released
    # unit's due time cannot keep it live.
    h.now = start + 121
    refused = h.apply(holder, {"kind": "heartbeat"})
    assert (refused["error"], refused["reason"]) == ("stale_generation", "heartbeat_overdue"), refused


def test_a_handoff_gives_the_new_owner_the_handed_units_window(h):
    old, new = h.register("old"), h.register("new")
    claims = h.acquire(old)["claims"]
    proposal = h.apply(old, h.proposal([old, new], owner=new, pattern="handoff"))["agreement"]
    h.ack(old, proposal)
    assert h.checkpoint(old)["status"] == "ok"
    assert h.ack(new, proposal, accepted_checkpoint="cp")["agreement"]["state"] == "committed"
    h.now += 100
    action = {"kind": "handoff", "claims": [{"claim_id": c["claim_id"], "expected_revision": c["revision"]} for c in claims],
              "old_owner": old["participant_id"], "new_owner": new["participant_id"], "old_generation": 1, "new_generation": 1,
              "checkpoint_id": "cp", "agreement_id": "agreement"}
    transferred = h.apply(old, action)
    assert transferred["status"] == "ok", transferred
    handed_at = h.now
    assert {claim["checkpoint_due"] for claim in transferred["claims"]} == {handed_at + WINDOW}
    # The new owner's own last call was the acknowledgement, 300 seconds ago by
    # now; the window it was handed is still open.
    h.now = handed_at + 200
    checkpoint = h.checkpoint(new, name="taken-over")
    assert checkpoint["status"] == "ok", checkpoint
