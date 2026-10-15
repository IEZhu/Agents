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
from src.cooperation.models import PROTOCOL, ApplyResponse, ReadResponse, parse_apply, parse_read, parse_register
from src.cooperation.service import CooperationService
from src.cooperation.store import CooperationError, Store, canonical


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
    h.now += 121
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
        db.execute("DROP TABLE message_receipts")
        db.execute("DROP TABLE messages")
        db.execute("PRAGMA user_version=1")
    h.service.start()
    h.now += 31 * 86400
    h.service.collect_terminal()
    assert h.read()["participants"][0]["status"] == "suspect"
    with h.service.store.transaction(write=False) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT participant_id FROM participant_activity").fetchone()[0] == actor["participant_id"]
