"""Transactional coordination for consenting local participants, never a shell fence."""
from dataclasses import asdict
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid

from .identity import CooperationIdentity, IdentityError, normalize_resource
from .models import PROTOCOL, EventEnvelope, ApplyResponse
from .policy import resources_conflict, find_cycle, cyclic_components, ordered_ready, effective_priorities
from .store import AdministrationLock, CooperationError, Store, bodies, canonical
from src.daemon.state import private_dir


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _dict(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _task(participant):
    return participant["task_id"] + "/" + participant["attempt_id"]


def _error(error):
    details = dict(error.details)
    if len(details.get("cycle", [])) > 50:
        details.update(cycle=details["cycle"][:50], cycle_truncated=True)
    return {"protocol": PROTOCOL, "status": "error", "error": error.code,
            "message": error.message, **details}


class CooperationService:
    """One lifecycle per daemon; connections and transactions are per call.

    ``priority_verifier`` is an owner/host adapter, never request-supplied code.
    Without that adapter, claimed user urgency is an assertion and ranks normal.
    ``heavy_admission`` must consult the existing host execution lease. A journal
    claim alone never starts or admits a process.
    """
    def __init__(self, directory, *, clock=time.time, priority_verifier=None,
                 heavy_admission=None, retention_days=30):
        self.store = Store(directory)
        self.lock = AdministrationLock(directory)
        self.clock = clock
        self.priority_verifier = priority_verifier or (lambda participant, priority: False)
        self.heavy_admission = heavy_admission
        self.retention_seconds = max(30, retention_days) * 86400
        self.started = False
        self.enabled = True
        self.epoch = 0
        from .notifications import MailboxNotifier
        self.mailbox_notifier = MailboxNotifier()

    def start(self):
        if self.started:
            return
        private_dir(self.store.directory)
        self.lock.acquire()
        try:
            self.store.initialize()
            with self.store.transaction() as db:
                row = db.execute("SELECT value FROM metadata WHERE key='epoch'").fetchone()
                self.epoch = int(row[0]) + 1 if row else 1
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('epoch',?)", (str(self.epoch),))
                for repo in db.execute("SELECT id FROM repositories").fetchall():
                    for participant in bodies(db, "participants", repo[0]):
                        if participant["status"] != "cancelled":
                            self._suspect(db, participant, "coordinator_restart")
                            self._reconciliation_event(db, participant, "coordinator_restart", self.clock())
            self.started = True
        except BaseException:
            self.lock.close()
            raise

    def close(self):
        self.started = False
        self.mailbox_notifier.notify()
        self.lock.close()

    def send(self, request, identity):
        from .mailbox import Mailbox
        return Mailbox(self).send(request, identity)

    def receive(self, request, identity):
        from .mailbox import Mailbox
        return Mailbox(self).receive(request, identity)

    def ack(self, request, identity):
        from .mailbox import Mailbox
        return Mailbox(self).ack(request, identity)

    def request_status(self, request, identity):
        from .mailbox import Mailbox
        return Mailbox(self).request_status(request, identity)

    def request_update(self, request, identity):
        from .mailbox import Mailbox
        return Mailbox(self).request_update(request, identity)

    async def wait(self, request, identity):
        from .notifications import wait_for_notifications
        return await wait_for_notifications(self, request, identity)

    def _available(self):
        if not self.started:
            raise CooperationError("store_unavailable", "Use the installed coordinator; stdio has no independent authority")

    def _epoch_check(self, db):
        self._available()
        current = db.execute("SELECT value FROM metadata WHERE key='epoch'").fetchone()
        if current is None or int(current[0]) != self.epoch:
            raise CooperationError("store_unavailable", "Coordinator lifecycle changed; reconnect to its current authority")

    def _participant(self, db, repository, identity):
        row = db.execute("SELECT body FROM participants WHERE id=? AND repository_id=?", (identity, repository)).fetchone()
        if not row:
            raise CooperationError("unauthorized_actor")
        return json.loads(row[0])

    def _save_participant(self, db, participant):
        db.execute("UPDATE participants SET body=? WHERE id=?", (canonical(participant), participant["participant_id"]))

    def _authenticate(self, db, repository, participant_id, capability, *, allow_terminal=False):
        row = db.execute("SELECT verifier FROM participants WHERE id=? AND repository_id=?", (participant_id, repository)).fetchone()
        if not row or not hmac.compare_digest(row[0], hashlib.sha256(capability.encode()).hexdigest()):
            raise CooperationError("unauthorized_actor")
        participant = self._participant(db, repository, participant_id)
        if participant["status"] == "cancelled" and not allow_terminal:
            raise CooperationError("unauthorized_actor")
        return participant

    def _revision(self, db, repository):
        row = db.execute("SELECT revision FROM repositories WHERE id=?", (repository,)).fetchone()
        if not row:
            raise CooperationError("workspace_invalid")
        return row[0]

    def _normalize(self, resources, identity):
        try:
            normalized = [_dict(normalize_resource(_dict(resource), identity)) for resource in resources]
        except IdentityError as error:
            raise CooperationError(error.code, str(error)) from error
        keys = [canonical(resource) for resource in normalized]
        if len(set(keys)) != len(keys):
            raise CooperationError("scope_denied", "Duplicate resources in work unit")
        return normalized

    @staticmethod
    def _within(resource, scope):
        for bound in scope:
            if resource["access"] != "snapshot_read" and resource["access"] != bound["access"]:
                continue
            same = resource["resource_type"] == bound["resource_type"] and resource["resource_key"] == bound["resource_key"]
            subtree = (bound["resource_type"] == "physical_subtree" and resource["resource_type"] in ("physical_file", "physical_subtree")
                       and (resource["resource_key"] == bound["resource_key"] or resource["resource_key"].startswith(bound["resource_key"].rstrip("/") + "/")
                            or (bound["resource_key"].endswith(":.") and resource["resource_key"].startswith(bound["resource_key"][:-1]))))
            if same or subtree:
                return True
        return False

    def _scope(self, resources, participant):
        if any(not self._within(resource, participant["scope"]) for resource in resources):
            raise CooperationError("scope_denied", "A new assignment requires a new prepared attempt")

    def _live(self, participant):
        if participant["status"] != "active" or participant["service_epoch"] != self.epoch:
            raise CooperationError("stale_generation", "Explicit execution/checkpoint reconciliation required",
                                   generation=participant["generation"], service_epoch=self.epoch)

    def _suspect(self, db, participant, reason):
        changed = participant["status"] != "suspect" or participant.get("reason") != reason
        participant["status"] = "suspect"
        participant["reason"] = reason
        self._save_participant(db, participant)
        repo = participant["repository_id"]
        for claim in bodies(db, "claims", repo):
            if claim["participant_id"] == participant["participant_id"]:
                changed |= not claim["quarantined"]
                claim["quarantined"] = True
                db.execute("UPDATE claims SET body=? WHERE id=?", (canonical(claim), claim["claim_id"]))
        for agreement in bodies(db, "agreements", repo):
            if participant["participant_id"] in agreement["required_participants"] and agreement["state"] in ("committed", "active", "checkpointed"):
                agreement["state"] = "suspended"
                agreement["reason"] = reason
                self._save_agreement(db, repo, agreement)
                changed = True
        return changed

    def _reconciliation_event(self, db, participant, reason, now, agreement=None):
        repo = participant["repository_id"]
        revision = self._revision(db, repo) + 1
        event = {"protocol": PROTOCOL, "event_id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                 "repository_id": repo, "revision": revision, "actor_id": "coordinator",
                 "subject_id": participant["participant_id"], "task_id": participant["task_id"],
                 "attempt_id": participant["attempt_id"], "generation": participant["generation"],
                 "kind": "reconcile", "agreement_id": agreement["agreement_id"] if agreement else None,
                 "proposal_revision": agreement["proposal_revision"] if agreement else None,
                 "reply_to": None, "created_at": now, "payload": {"kind": "reconcile", "reason": reason}}
        EventEnvelope.model_validate(event)
        db.execute("UPDATE repositories SET revision=? WHERE id=?", (revision, repo))
        db.execute("INSERT INTO events(repository_id,agreement_id,body,created_at) VALUES (?,?,?,?)",
                   (repo, event["agreement_id"], canonical(event), now))

    def _reconcile(self, db, repo, now):
        for participant in bodies(db, "participants", repo):
            if participant["status"] == "active" and now - participant["last_seen"] > 120:
                self._suspect(db, participant, "heartbeat_overdue")
                self._reconciliation_event(db, participant, "heartbeat_overdue", now)
        for agreement in bodies(db, "agreements", repo):
            if agreement["state"] == "proposed" and now > agreement["deadline"]:
                agreement["state"] = "expired"
                agreement["terminal_at"] = now
                self._save_agreement(db, repo, agreement)
                participant = self._participant(db, repo, agreement["required_participants"][0])
                self._reconciliation_event(db, participant, "proposal_expired", now, agreement)
        cyclic_tasks = {task for component in cyclic_components(self._declared_edges(db, repo)) for task in component}
        for participant in bodies(db, "participants", repo):
            if _task(participant) in cyclic_tasks and participant["status"] != "cancelled":
                if self._suspect(db, participant, "dependency_cycle"):
                    self._reconciliation_event(db, participant, "dependency_cycle", now)

    def register(self, request, identity):
        try:
            self._available()
            if not self.enabled:
                raise CooperationError("store_unavailable", "New admission is disabled; existing claims are retained")
            data = _dict(request)
            now = self.clock()
            resources = self._normalize(data["scope"], identity)
            with self.store.transaction() as db:
                self._epoch_check(db)
                repo = identity.repository_id
                db.execute("INSERT OR IGNORE INTO repositories(id) VALUES (?)", (repo,))
                self._reconcile(db, repo, now)
                parent_id = data.get("parent_id")
                if parent_id:
                    parent = self._authenticate(db, repo, parent_id, data.get("parent_capability") or "")
                    self._live(parent)
                    if parent["root_task_id"] != data["root_task_id"] or not data.get("contract_revision"):
                        raise CooperationError("scope_denied", "Child needs original contract and root task binding")
                    self._scope(resources, parent)
                    db.execute("INSERT OR IGNORE INTO participant_activity VALUES (?)", (parent_id,))
                elif data.get("parent_capability"):
                    raise CooperationError("unauthorized_actor")
                identifier = str(uuid.uuid4())
                capability = secrets.token_urlsafe(32)
                participant = {key: value for key, value in data.items() if key not in ("parent_capability", "checkout_path", "protocol")}
                participant.update(participant_id=identifier, repository_id=repo, workspace_id=identity.workspace_id,
                                   checkout_id=identity.checkout_id, identity={key: str(value) if key in ("checkout_path", "common_dir") else value for key, value in asdict(identity).items()},
                                   scope=resources, generation=1, service_epoch=self.epoch, status="active",
                                   created_at=now, last_seen=now, ready_since=None, eligible_wait_seconds=0,
                                   ready_sequence=self._revision(db, repo) + 1, base_priority=1, priority_source="default",
                                   intent=[], checkpoint=None, running=False)
                db.execute("INSERT INTO participants VALUES (?,?,?,?)", (identifier, repo, hashlib.sha256(capability.encode()).hexdigest(), canonical(participant)))
                try:
                    db.execute("INSERT INTO attempts VALUES (?,?,?,?,?)", (repo, data["task_id"], data["attempt_id"], identifier, data.get("contract_revision")))
                except sqlite3.IntegrityError as error:
                    raise CooperationError("scope_denied", "Task attempt already bound; register a fresh attempt") from error
                db.execute("UPDATE repositories SET revision=revision+1 WHERE id=?", (repo,))
                return {"protocol": PROTOCOL, "status": "ok", "participant_id": identifier,
                        "repository_id": repo, "workspace_id": identity.workspace_id, "checkout_id": identity.checkout_id,
                        "generation": 1, "service_epoch": self.epoch, "capability": capability,
                        "revision": self._revision(db, repo), "communication_mode": data["communication_mode"],
                        "guidance": "Keep capability private. Read agents-core://cooperation/schema before constructing actions. Claims reserve metadata only; check ownership before each bounded work unit. Never infer consent from expiry."}
        except CooperationError as error:
            return _error(error)

    def _agreement(self, db, repo, identifier):
        row = db.execute("SELECT body FROM agreements WHERE repository_id=? AND id=?", (repo, identifier)).fetchone()
        if not row:
            raise CooperationError("scope_denied", "Unknown agreement")
        return json.loads(row[0])

    def _save_agreement(self, db, repo, agreement):
        db.execute("INSERT INTO agreements VALUES (?,?,?) ON CONFLICT(repository_id,id) DO UPDATE SET body=excluded.body",
                   (agreement["agreement_id"], repo, canonical(agreement)))

    def _dependency_records(self, db, repo, *, exclude_agreement=None):
        for row in db.execute("SELECT agreement_id,body FROM dependencies WHERE repository_id=?", (repo,)).fetchall():
            if row[0] is not None:
                if row[0] == exclude_agreement:
                    continue
                agreement = self._agreement(db, repo, row[0])
                if agreement["state"] in ("cancelled", "completed", "expired", "rejected"):
                    continue
            yield json.loads(row[1])

    def _declared_edges(self, db, repo, *, exclude_agreement=None):
        edges = []
        for dep in self._dependency_records(db, repo, exclude_agreement=exclude_agreement):
            producer = self._attempt(db, repo, dep["producer_task"], dep["producer_attempt_id"])
            consumer = self._attempt(db, repo, dep["consumer_task"], dep["consumer_attempt_id"])
            if consumer["status"] != "cancelled":
                edges.append((_task(producer), _task(consumer)))
        return edges

    @staticmethod
    def _cycles(edges):
        edges = list(edges)
        components = cyclic_components(edges)
        membership = {node: index for index, component in enumerate(components) for node in component}
        internal = [[] for _ in components]
        for producer, consumer in edges:
            index = membership.get(producer)
            if index is not None and membership.get(consumer) == index:
                internal[index].append((producer, consumer))
        # One actual witness per component; membership, not this witness, controls quarantine.
        return [find_cycle(component_edges) for component_edges in internal]

    def _dependencies(self, db, repo):
        edges = []
        for dep in self._dependency_records(db, repo):
            producer = self._attempt(db, repo, dep["producer_task"], dep["producer_attempt_id"])
            consumer = self._attempt(db, repo, dep["consumer_task"], dep["consumer_attempt_id"])
            if consumer["status"] == "cancelled":
                continue
            artifact = db.execute("SELECT body FROM artifacts WHERE repository_id=? AND id=?", (repo, dep["artifact_id"])).fetchone()
            artifact = json.loads(artifact[0]) if artifact else None
            matching_producer = artifact and (artifact["producer_task_id"], artifact["producer_attempt_id"]) == (dep["producer_task"], dep["producer_attempt_id"])
            if not matching_producer or not self._accepted(db, repo, dep["artifact_id"], consumer["participant_id"]):
                edges.append((_task(producer), _task(consumer)))
        return edges

    def _wait_clocks(self, db, repo, now, revision):
        blocked = {consumer for _, consumer in self._dependencies(db, repo)}
        for participant in bodies(db, "participants", repo):
            eligible = bool(participant["intent"]) and _task(participant) not in blocked and participant["status"] == "active" and not participant["running"]
            since = participant["ready_since"]
            if not eligible and since is not None:
                participant["eligible_wait_seconds"] += max(0, now - since)
                participant["ready_since"] = None
                self._save_participant(db, participant)
            elif eligible and since is None:
                participant["ready_since"] = now
                participant["ready_sequence"] = revision
                self._save_participant(db, participant)

    def _attempt(self, db, repo, task, attempt):
        row = db.execute("SELECT participant_id FROM attempts WHERE repository_id=? AND task_id=? AND attempt_id=?", (repo, task, attempt)).fetchone()
        if not row:
            raise CooperationError("scope_denied", "Unknown task attempt")
        return self._participant(db, repo, row[0])

    def _accepted(self, db, repo, artifact, consumer):
        row = db.execute("SELECT body FROM acceptances WHERE repository_id=? AND artifact_id=? AND consumer_id=?", (repo, artifact, consumer)).fetchone()
        return bool(row and json.loads(row[0])["decision"] == "accepted")

    def _queue(self, db, repo, now):
        participants = bodies(db, "participants", repo)
        edges = self._dependencies(db, repo)
        blocked = {consumer for _, consumer in edges}
        # Corrupt/legacy cycle state freezes only cycle participants. Removing
        # internal cycle edges here permits ranking unrelated work without
        # removing the persisted gates or admitting their blocked consumers.
        membership = {node: index for index, component in enumerate(cyclic_components(edges)) for node in component}
        edges = [(a, b) for a, b in edges if a not in membership or membership.get(a) != membership.get(b)]
        tasks = []
        for participant in participants:
            ready = _task(participant) not in blocked and participant["status"] == "active"
            # Only eligible, non-running resource wait accumulates age.
            waiting = participant["eligible_wait_seconds"]
            if ready and not participant["running"] and participant["ready_since"] is not None:
                waiting += max(0, now - participant["ready_since"])
            tasks.append({"id": _task(participant), "base_priority": participant["base_priority"],
                          "eligible_wait_seconds": waiting, "ready_sequence": participant["ready_sequence"],
                          "ready": ready, "running": participant["running"], "participant_id": participant["participant_id"],
                          "resources": participant["intent"]})
        ranks = effective_priorities(tasks, edges)
        return [{**task, "effective_priority": ranks[task["id"]]} for task in ordered_ready(tasks, edges)]

    def read(self, request, identity):
        try:
            self._available()
            data = _dict(request)
            repo = data["repository_id"]
            if repo != identity.repository_id:
                raise CooperationError("workspace_invalid")
            with self.store.transaction() as db:
                self._epoch_check(db)
                # Read maintenance and snapshot share the same transaction and cursor.
                if not db.execute("SELECT 1 FROM repositories WHERE id=?", (repo,)).fetchone():
                    raise CooperationError("workspace_invalid")
                now = self.clock()
                snapshot_time = now if data.get("snapshot_time") is None else data["snapshot_time"]
                if snapshot_time > now:
                    raise CooperationError("invalid_request", "Snapshot time cannot be in the future")
                self._reconcile(db, repo, now)
                minimum = db.execute("SELECT minimum_cursor FROM repositories WHERE id=?", (repo,)).fetchone()[0]
                cursor = data.get("cursor")
                if cursor is not None and cursor < minimum:
                    raise CooperationError("cursor_expired", "Request a new snapshot without a cursor", minimum_cursor=minimum)
                latest = db.execute("SELECT COALESCE(MAX(cursor),0) FROM events WHERE repository_id=?", (repo,)).fetchone()[0]
                rows = db.execute("SELECT cursor,body FROM events WHERE repository_id=? AND cursor>? ORDER BY cursor LIMIT ?", (repo, cursor or 0, data["limit"])).fetchall()
                participants = bodies(db, "participants", repo)
                if data["participant_ids"]:
                    participants = [p for p in participants if p["participant_id"] in data["participant_ids"]]
                if data["task_ids"]:
                    participants = [p for p in participants if p["task_id"] in data["task_ids"]]
                agreements = bodies(db, "agreements", repo)
                if data["agreement_ids"]:
                    agreements = [a for a in agreements if a["agreement_id"] in data["agreement_ids"]]
                # Snapshot pagination is explicit: callers filter narrow participant/task IDs.
                snapshots = {"participants": participants, "agreements": agreements,
                             "claims": bodies(db, "claims", repo), "artifacts": bodies(db, "artifacts", repo),
                             "dependencies": bodies(db, "dependencies", repo), "ready": self._queue(db, repo, snapshot_time)}
                counts = {name: len(items) for name, items in snapshots.items()}
                cycles = self._cycles(self._declared_edges(db, repo))
                offset, limit = data.get("offset", 0), data["limit"]
                more = any(count > offset + limit for count in counts.values())
                return {"protocol": PROTOCOL, "status": "ok", "repository_id": repo,
                        "revision": self._revision(db, repo), "service_epoch": self.epoch,
                        "minimum_cursor": minimum, "snapshot_cursor": latest, "snapshot_time": snapshot_time,
                        "next_cursor": rows[-1][0] if rows else max(cursor or 0, minimum),
                        "has_more_events": bool(rows and rows[-1][0] < latest),
                        **{name: items[offset:offset + limit] for name, items in snapshots.items()},
                        "snapshot_counts": counts, "snapshot_truncated": more or offset > 0,
                        "snapshot_offset": offset, "next_snapshot_offset": offset + limit if more else None,
                        "dependency_cycles": [cycle[:50] for cycle in cycles[:100]],
                        "dependency_cycles_count": len(cycles),
                        "dependency_cycles_truncated": len(cycles) > 100 or any(len(cycle) > 50 for cycle in cycles[:100]),
                        "events": [json.loads(row[1]) for row in rows],
                        "limitations": "Metadata only; artifact references do not grant file access or prove execution. Unregistered shell writes are not fenced."}
        except CooperationError as error:
            return _error(error)

    def apply(self, request, identity):
        try:
            self._available()
            data = _dict(request)
            repo = data["repository_id"]
            if repo != identity.repository_id:
                raise CooperationError("workspace_invalid")
            now = self.clock()
            with self.store.transaction() as db:
                self._epoch_check(db)
                actor = self._authenticate(db, repo, data["participant_id"], data["capability"], allow_terminal=True)
                subject = self._participant(db, repo, data["subject_id"]) if data.get("subject_id") else actor
                if subject is not actor and subject.get("parent_id") != actor["participant_id"]:
                    raise CooperationError("unauthorized_actor", "Parent relay is limited to registered direct children")
                receipt = db.execute("SELECT digest,body FROM receipts WHERE repository_id=? AND participant_id=? AND operation_id=?", (repo, actor["participant_id"], data["operation_id"])).fetchone()
                request_digest = digest(data)
                if receipt:
                    if receipt[0] != request_digest:
                        raise CooperationError("idempotency_conflict")
                    return json.loads(receipt[1])
                action = data["action"]
                kind = action["kind"]
                # Terminal owners may close abandoned metadata, never resume execution or relay.
                terminal_cancel = (actor["status"] == "cancelled" and subject is actor
                                   and kind == "cancel" and action["agreement_id"] is not None)
                if (actor["status"] == "cancelled" or subject["status"] == "cancelled") and not terminal_cancel:
                    raise CooperationError("unauthorized_actor")
                self._reconcile(db, repo, now)
                actor = self._participant(db, repo, actor["participant_id"])
                subject = self._participant(db, repo, subject["participant_id"])
                if data["expected_generation"] != subject["generation"]:
                    raise CooperationError("stale_generation", generation=subject["generation"])
                if terminal_cancel:
                    if subject["running"] or any(c["participant_id"] == subject["participant_id"]
                                                 for c in bodies(db, "claims", repo)):
                        raise CooperationError("scope_denied", "Terminal agreement cleanup requires no running work or held claims")
                elif kind != "recover":
                    self._live(actor)
                    self._live(subject)
                elif subject["participant_id"] != actor["participant_id"]:
                    self._live(actor)
                if not self.enabled and kind not in ("recover", "release", "checkpoint", "cancel"):
                    raise CooperationError("store_unavailable", "Admission disabled; reconcile and release held work")
                revision = self._revision(db, repo)
                # A racing acquisition reports the authoritative physical collision first.
                if kind == "acquire":
                    resources = self._normalize(action["resources"], CooperationIdentity(**subject["identity"]))
                    self._scope(resources, subject)
                    self._claim_conflicts(db, repo, subject, resources)
                if data["expected_revision"] != revision:
                    raise CooperationError("stale_revision", revision=revision)
                revision += 1
                result = getattr(self, "_" + kind)(db, subject, actor, action, now, revision)
                subject["last_seen"] = now
                self._save_participant(db, subject)
                self._wait_clocks(db, repo, now, revision)
                if kind in ("checkpoint", "release", "accept_artifact"):
                    self._complete_agreements(db, repo, now, revision)
                for participant_id in {actor["participant_id"], subject["participant_id"]}:
                    db.execute("INSERT OR IGNORE INTO participant_activity VALUES (?)", (participant_id,))
                db.execute("UPDATE repositories SET revision=? WHERE id=?", (revision, repo))
                event = {"protocol": PROTOCOL, "event_id": str(uuid.uuid4()), "operation_id": data["operation_id"],
                         "repository_id": repo, "revision": revision, "actor_id": actor["participant_id"],
                         "subject_id": subject["participant_id"] if subject["participant_id"] != actor["participant_id"] else None,
                         "task_id": subject["task_id"], "attempt_id": subject["attempt_id"], "generation": subject["generation"],
                         "kind": kind, "agreement_id": action.get("agreement_id"),
                         "proposal_revision": action.get("proposal_revision"), "reply_to": None,
                         "created_at": now, "payload": action}
                EventEnvelope.model_validate(event)
                if len(canonical(event).encode()) > 32768:
                    raise CooperationError("invalid_request", "Event exceeds 32 KiB")
                cursor = db.execute("INSERT INTO events(repository_id,agreement_id,body,created_at) VALUES (?,?,?,?)", (repo, action.get("agreement_id"), canonical(event), now)).lastrowid
                response = {"protocol": PROTOCOL, "status": "ok", "revision": revision,
                            "generation": subject["generation"], "service_epoch": self.epoch,
                            "event": event, "cursor": cursor, **result}
                try:
                    ApplyResponse.model_validate(response)
                except ValueError as error:
                    raise CooperationError("store_unavailable", "Response projection failed; no mutation was committed") from error
                db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?)", (repo, actor["participant_id"], data["operation_id"], request_digest, canonical(response), action.get("agreement_id"), now))
                return response
        except CooperationError as error:
            return _error(error)

    def _claim_conflicts(self, db, repo, participant, resources):
        for claim in bodies(db, "claims", repo):
            if claim["participant_id"] != participant["participant_id"] and any(resources_conflict(resource, claim["resource"]) for resource in resources):
                raise CooperationError("claim_conflict", "Resource held, including quarantined claims", claim_ids=[claim["claim_id"]], owner=claim["participant_id"])

    def _declare_intent(self, db, subject, actor, action, now, revision):
        repo = subject["repository_id"]
        resources = self._normalize(action["resources"], CooperationIdentity(**subject["identity"]))
        self._scope(resources, subject)
        if subject["running"]:
            raise CooperationError("claim_conflict", "Release current work unit before changing intent")
        dependency_ids = set()
        for dependency in action["dependencies"]:
            if (dependency["consumer_task"], dependency["consumer_attempt_id"]) != (subject["task_id"], subject["attempt_id"]):
                raise CooperationError("scope_denied", "Only the consumer can declare its dependency")
            dependency_id = digest([repo, None, dependency])
            if dependency_id in dependency_ids:
                raise CooperationError("invalid_request", "Duplicate dependency")
            dependency_ids.add(dependency_id)
        # Replace only this consumer's gates, preserving other tasks and accumulated wait.
        for row in db.execute("SELECT id,body FROM dependencies WHERE repository_id=? AND agreement_id IS NULL", (repo,)).fetchall():
            dep = json.loads(row[1])
            if (dep["consumer_task"], dep["consumer_attempt_id"]) == (subject["task_id"], subject["attempt_id"]):
                db.execute("DELETE FROM dependencies WHERE id=?", (row[0],))
        for dep in action["dependencies"]:
            self._attempt(db, repo, dep["producer_task"], dep["producer_attempt_id"])
            db.execute("INSERT INTO dependencies VALUES (?,?,?,?)", (digest([repo, None, dep]), repo, None, canonical(dep)))
        edges = self._dependencies(db, repo)
        cycle = find_cycle(self._declared_edges(db, repo))
        if cycle:
            raise CooperationError("dependency_cycle", cycle=cycle)
        was_waiting = subject["ready_since"]
        if was_waiting is not None:
            subject["eligible_wait_seconds"] += max(0, now - was_waiting)
        subject["ready_since"] = None if not resources or _task(subject) in {b for _, b in edges} else now
        if was_waiting is None and subject["ready_since"] is not None:
            subject["ready_sequence"] = revision
        priority = action["priority"]
        verified = self.priority_verifier(subject, priority)
        rank = {"background": 0, "normal": 1, "urgent": 2}[priority["value"]] if verified else 1
        if priority["source"] == "parent" and subject["participant_id"] != actor["participant_id"]:
            rank = actor["base_priority"]
        subject.update(intent=resources, base_priority=rank, priority_source=priority,
                       checkpoint_spec=action["checkpoint"])
        self._save_participant(db, subject)
        ready = self._queue(db, repo, now)
        return {"base_priority": rank, "priority_verified": bool(verified), "ready": ready[:100],
                "ready_count": len(ready), "ready_truncated": len(ready) > 100}

    def _validate_terms(self, db, subject, action):
        repo = subject["repository_id"]
        required = action["required_participants"]
        if subject["participant_id"] not in required or len(set(required)) != len(required):
            raise CooperationError("scope_denied", "Proposer must be a required participant; identities must be unique")
        bindings = {binding["participant_id"]: binding for binding in action["task_bindings"]}
        if set(bindings) != set(required) or len(bindings) != len(action["task_bindings"]):
            raise CooperationError("scope_denied", "Bind every required task exactly once")
        for identifier, binding in bindings.items():
            participant = self._participant(db, repo, identifier)
            self._live(participant)
            for key in ("task_id", "attempt_id", "generation", "contract_revision", "parent_id"):
                if binding.get(key) != participant.get(key):
                    raise CooperationError("stale_generation" if key == "generation" else "scope_denied", "Proposal task binding changed")
        terms = dict(action["terms"])
        if terms["integrator"] not in required:
            raise CooperationError("scope_denied", "Integrator must be a required participant")
        normalized = []
        for resource in terms["resources"]:
            owner = resource["owner"]
            if owner not in required:
                raise CooperationError("scope_denied")
            participant = self._participant(db, repo, owner)
            resource = dict(resource)
            resource.pop("owner")
            resource = self._normalize([resource], CooperationIdentity(**participant["identity"]))[0]
            self._scope([resource], participant)
            for prior in normalized:
                if prior["owner"] != owner and resources_conflict(prior, resource):
                    raise CooperationError("claim_conflict", "Proposal assigns incompatible writers")
            normalized.append({**resource, "owner": owner})
        terms["resources"] = normalized
        edges = self._declared_edges(db, repo, exclude_agreement=action["agreement_id"])
        for dep in terms["dependencies"]:
            producer = self._attempt(db, repo, dep["producer_task"], dep["producer_attempt_id"])
            consumer = self._attempt(db, repo, dep["consumer_task"], dep["consumer_attempt_id"])
            if producer["participant_id"] not in required or consumer["participant_id"] not in required:
                raise CooperationError("scope_denied")
            edges.append((_task(producer), _task(consumer)))
        cycle = find_cycle(edges)
        if cycle:
            raise CooperationError("dependency_cycle", cycle=cycle)
        for contract in terms["artifact_contracts"]:
            if contract["producer"] not in required or contract["consumer"] not in required:
                raise CooperationError("scope_denied")
        return terms

    def _propose(self, db, subject, actor, action, now, revision):
        repo = subject["repository_id"]
        terms = self._validate_terms(db, subject, action)
        previous = db.execute("SELECT body FROM agreements WHERE repository_id=? AND id=?", (repo, action["agreement_id"])).fetchone()
        counters = []
        if previous:
            old = json.loads(previous[0])
            if old["state"] not in ("proposed", "suspended") or action["supersedes"] != old["proposal_revision"] or action["proposal_revision"] != old["proposal_revision"] + 1:
                raise CooperationError("stale_revision", "Counterproposal needs the current proposed revision")
            counters = old["counterproposers"] if old["state"] == "proposed" else []
            if subject["participant_id"] in counters or set(action["required_participants"]) != set(old["required_participants"]):
                raise CooperationError("scope_denied", "Negotiation round cap reached or participant set changed")
            counters = counters + [subject["participant_id"]]
        elif action["proposal_revision"] != 1 or action["supersedes"] is not None:
            raise CooperationError("stale_revision")
        agreement = {**action, "terms": terms, "state": "proposed", "terms_digest": digest(terms),
                     "counterproposers": counters, "deadline": now + terms["acknowledgement_timeout_seconds"],
                     "revision": revision, "terminal_at": None}
        self._save_agreement(db, repo, agreement)
        db.execute("INSERT INTO proposals VALUES (?,?,?,?)", (repo, action["agreement_id"], action["proposal_revision"], canonical(agreement)))
        return {"agreement": agreement}

    def _acknowledge(self, db, subject, actor, action, now, revision):
        repo = subject["repository_id"]
        agreement = self._agreement(db, repo, action["agreement_id"])
        if agreement["state"] == "expired" or now > agreement["deadline"]:
            raise CooperationError("proposal_expired")
        if agreement["state"] != "proposed" or agreement["proposal_revision"] != action["proposal_revision"] or agreement["terms_digest"] != action["terms_digest"]:
            raise CooperationError("stale_revision", "Acknowledge the exact current terms digest")
        relay = subject["participant_id"] != actor["participant_id"]
        if action["acceptance_mode"] != ("parent_relay" if relay else "self"):
            raise CooperationError("unauthorized_actor", "Do not misrepresent a parent relay as child acknowledgement")
        if subject["participant_id"] not in agreement["required_participants"]:
            raise CooperationError("scope_denied")
        acknowledgement = dict(action)
        if action["accepted_checkpoint"] is not None:
            # Consent cannot name an output that does not exist yet. Checkpoint
            # IDs are immutable in the journal, including across later units.
            checkpoints = []
            for identifier in agreement["required_participants"]:
                if identifier == subject["participant_id"]:
                    continue
                owner = self._participant(db, repo, identifier)
                checkpoint = owner.get("checkpoint")
                if checkpoint and checkpoint["checkpoint_id"] == action["accepted_checkpoint"] and checkpoint["release_ready"]:
                    checkpoints.append((identifier, checkpoint))
            if agreement["pattern"] != "handoff" or len(checkpoints) != 1:
                raise CooperationError("scope_denied", "Accept an existing unique release-ready checkpoint after inspection")
            acknowledgement.update(checkpoint_owner=checkpoints[0][0], checkpoint_digest=digest(checkpoints[0][1]))
        # Revalidate generations and original attempts on every acknowledgement.
        self._validate_terms(db, subject, agreement)
        db.execute("INSERT INTO acknowledgements VALUES (?,?,?,?,?) ON CONFLICT DO UPDATE SET body=excluded.body", (repo, agreement["agreement_id"], agreement["proposal_revision"], subject["participant_id"], canonical(acknowledgement)))
        acknowledged = {row[0] for row in db.execute("SELECT participant_id FROM acknowledgements WHERE repository_id=? AND agreement_id=? AND proposal_revision=?", (repo, agreement["agreement_id"], agreement["proposal_revision"]))}
        if acknowledged == set(agreement["required_participants"]):
            for resource in agreement["terms"]["resources"]:
                owner = self._participant(db, repo, resource["owner"])
                if agreement["pattern"] == "handoff":
                    # A committed handoff does not transfer anything yet. The old
                    # owner retains the claim until its release-ready transfer.
                    for claim in bodies(db, "claims", repo):
                        if resources_conflict(resource, claim["resource"]) and claim["participant_id"] not in agreement["required_participants"]:
                            raise CooperationError("claim_conflict")
                else:
                    self._claim_conflicts(db, repo, owner, [resource])
            for other in bodies(db, "agreements", repo):
                if other["agreement_id"] == agreement["agreement_id"] or other["state"] not in ("committed", "active", "checkpointed", "suspended"):
                    continue
                for resource in agreement["terms"]["resources"]:
                    if any(resource["owner"] != prior["owner"] and resources_conflict(resource, prior) for prior in other["terms"]["resources"]):
                        raise CooperationError("claim_conflict", "Competing committed agreement")
            agreement["state"] = "committed"
            db.execute("DELETE FROM dependencies WHERE repository_id=? AND agreement_id=?", (repo, agreement["agreement_id"]))
            for dep in agreement["terms"]["dependencies"]:
                db.execute("INSERT OR IGNORE INTO dependencies VALUES (?,?,?,?)", (digest([repo, agreement["agreement_id"], dep]), repo, agreement["agreement_id"], canonical(dep)))
        agreement["revision"] = revision
        self._save_agreement(db, repo, agreement)
        return {"agreement": agreement, "acknowledged": sorted(acknowledged)}

    def _reject(self, db, subject, actor, action, now, revision):
        agreement = self._agreement(db, subject["repository_id"], action["agreement_id"])
        if subject["participant_id"] not in agreement["required_participants"]:
            raise CooperationError("unauthorized_actor")
        if agreement["state"] != "proposed" or agreement["proposal_revision"] != action["proposal_revision"]:
            raise CooperationError("stale_revision")
        agreement.update(state="rejected", reason=action["reason"], terminal_at=now, revision=revision)
        self._save_agreement(db, subject["repository_id"], agreement)
        return {"agreement": agreement}

    def _acquire(self, db, subject, actor, action, now, revision):
        repo = subject["repository_id"]
        resources = self._normalize(action["resources"], CooperationIdentity(**subject["identity"]))
        self._scope(resources, subject)
        if subject["running"] or any(c["participant_id"] == subject["participant_id"] for c in bodies(db, "claims", repo)):
            raise CooperationError("claim_conflict", "Acquire the complete set once; release before waiting for another set")
        if _task(subject) in {consumer for _, consumer in self._dependencies(db, repo)}:
            raise CooperationError("dependency_wait", "Consumer must accept the exact artifact first")
        for queued in self._queue(db, repo, now):
            if queued["participant_id"] == subject["participant_id"]:
                break
            if any(resources_conflict(resource, waiting) for resource in resources for waiting in queued["resources"]):
                raise CooperationError("priority_wait", "An earlier ready work unit has resource priority", participant_id=queued["participant_id"])
        if any(resource["resource_type"] == "heavy_execution" for resource in resources):
            if self.heavy_admission is None or not self.heavy_admission(subject, resources):
                raise CooperationError("execution_lease_required", "Existing host heavy-execution lease admission is required")
        agreement = None
        if action["agreement_id"]:
            agreement = self._agreement(db, repo, action["agreement_id"])
            if agreement["state"] not in ("committed", "active", "checkpointed"):
                raise CooperationError("scope_denied", "Current committed agreement required")
            owned = [r for r in agreement["terms"]["resources"] if r["owner"] == subject["participant_id"]]
            if any(not self._within(resource, owned) for resource in resources):
                raise CooperationError("scope_denied")
            self._validate_terms(db, subject, agreement)
        for other in bodies(db, "agreements", repo):
            if other["state"] in ("committed", "active", "checkpointed", "suspended"):
                for owned in other["terms"]["resources"]:
                    if owned["owner"] != subject["participant_id"] and any(resources_conflict(resource, owned) for resource in resources):
                        raise CooperationError("claim_conflict", "Resource assigned by a committed agreement")
        self._claim_conflicts(db, repo, subject, resources)
        claims = []
        for resource in resources:
            claim = {"claim_id": str(uuid.uuid4()), "repository_id": repo, "participant_id": subject["participant_id"],
                     "generation": subject["generation"], "agreement_id": action["agreement_id"], "revision": revision,
                     "resource": resource, "work_unit_id": action["work_unit_id"], "quarantined": False,
                     "acquired_at": now, "checkpoint_due": now + action["checkpoint"]["max_interval_seconds"]}
            db.execute("INSERT INTO claims VALUES (?,?,?,?)", (claim["claim_id"], repo, subject["participant_id"], canonical(claim)))
            claims.append(claim)
        subject.update(running=True, ready_since=None, eligible_wait_seconds=0, checkpoint=None)
        if agreement:
            agreement.update(state="active", revision=revision)
            self._save_agreement(db, repo, agreement)
        return {"claims": claims, "execution_admitted": bool(any(r["resource_type"] == "heavy_execution" for r in resources))}

    def _heartbeat(self, db, subject, actor, action, now, revision):
        overdue = [c["claim_id"] for c in bodies(db, "claims", subject["repository_id"]) if c["participant_id"] == subject["participant_id"] and now > c["checkpoint_due"]]
        return {"checkpoint_required": overdue}

    def _checkpoint(self, db, subject, actor, action, now, revision):
        repo = subject["repository_id"]
        if db.execute("SELECT 1 FROM checkpoints WHERE repository_id=? AND participant_id=? AND checkpoint_id=?", (repo, subject["participant_id"], action["checkpoint_id"])).fetchone():
            raise CooperationError("stale_revision", "Checkpoint identifiers are immutable; record a new checkpoint")
        db.execute("INSERT INTO checkpoints VALUES (?,?,?,?)", (repo, subject["participant_id"], action["checkpoint_id"], canonical(action)))
        for artifact in action["artifacts"]:
            if (artifact["producer_task_id"], artifact["producer_attempt_id"], artifact["repository_id"]) != (subject["task_id"], subject["attempt_id"], repo):
                raise CooperationError("scope_denied", "Artifact producer must match original task attempt")
            old = db.execute("SELECT participant_id,body FROM artifacts WHERE repository_id=? AND id=?", (repo, artifact["artifact_id"])).fetchone()
            if old and old[0] != subject["participant_id"]:
                raise CooperationError("unauthorized_actor")
            if old and canonical(artifact) != old[1]:
                db.execute("DELETE FROM acceptances WHERE repository_id=? AND artifact_id=?", (repo, artifact["artifact_id"]))
                for dependency in bodies(db, "dependencies", repo):
                    if dependency["artifact_id"] == artifact["artifact_id"]:
                        consumer = self._attempt(db, repo, dependency["consumer_task"], dependency["consumer_attempt_id"])
                        if consumer["running"]:
                            self._suspect(db, consumer, "accepted_artifact_changed")
            db.execute("INSERT INTO artifacts VALUES (?,?,?,?) ON CONFLICT(repository_id,id) DO UPDATE SET body=excluded.body", (artifact["artifact_id"], repo, subject["participant_id"], canonical(artifact)))
        subject["checkpoint"] = {**action, "revision": revision, "created_at": now}
        if action["agreement_id"]:
            agreement = self._agreement(db, repo, action["agreement_id"])
            if subject["participant_id"] not in agreement["required_participants"]:
                raise CooperationError("unauthorized_actor")
            if agreement["state"] not in ("active", "checkpointed", "committed"):
                raise CooperationError("scope_denied")
            agreement.update(state="checkpointed", revision=revision)
            self._save_agreement(db, repo, agreement)
        return {"checkpoint_id": action["checkpoint_id"]}

    def _claim_set(self, db, subject, references, *, all_owned=False):
        owned = {c["claim_id"]: c for c in bodies(db, "claims", subject["repository_id"]) if c["participant_id"] == subject["participant_id"]}
        identifiers = [ref["claim_id"] for ref in references]
        if len(set(identifiers)) != len(identifiers) or (all_owned and set(identifiers) != set(owned)):
            raise CooperationError("stale_revision", "Reconcile the complete exact held claim set")
        for ref in references:
            if ref["claim_id"] not in owned or owned[ref["claim_id"]]["revision"] != ref["expected_revision"]:
                raise CooperationError("stale_revision", "Claim ownership/revision changed")
        return [owned[identifier] for identifier in identifiers]

    def _complete_agreements(self, db, repo, now, revision):
        claims = bodies(db, "claims", repo)
        for agreement in bodies(db, "agreements", repo):
            if agreement["state"] != "checkpointed" or any(c["agreement_id"] == agreement["agreement_id"] for c in claims):
                continue
            contracts = agreement["terms"]["artifact_contracts"]
            checkpoints = [self._participant(db, repo, identifier).get("checkpoint") for identifier in agreement["required_participants"]]
            completed_checks = all(cp and cp["agreement_id"] == agreement["agreement_id"] and cp["release_ready"] and cp["checks"] and all(check["status"] == "passed" for check in cp["checks"]) for cp in checkpoints)
            accepted_contracts = True
            for contract in contracts:
                artifact = db.execute("SELECT participant_id FROM artifacts WHERE repository_id=? AND id=?", (repo, contract["artifact_id"])).fetchone()
                if not artifact or artifact[0] != contract["producer"] or not self._accepted(db, repo, contract["artifact_id"], contract["consumer"]):
                    accepted_contracts = False
            if completed_checks and accepted_contracts:
                agreement.update(state="completed", terminal_at=now, revision=revision)
                self._save_agreement(db, repo, agreement)

    def _release(self, db, subject, actor, action, now, revision):
        claims = self._claim_set(db, subject, action["claims"])
        if not subject.get("checkpoint") or not subject["checkpoint"]["release_ready"]:
            raise CooperationError("scope_denied", "Record a release-ready checkpoint before relinquishing execution")
        for claim in claims:
            db.execute("DELETE FROM claims WHERE id=?", (claim["claim_id"],))
        remaining = any(c["participant_id"] == subject["participant_id"] for c in bodies(db, "claims", subject["repository_id"]))
        subject.update(running=remaining, ready_since=None, eligible_wait_seconds=0, intent=[])
        self._complete_agreements(db, subject["repository_id"], now, revision)
        return {"released": [c["claim_id"] for c in claims]}

    def _handoff(self, db, subject, actor, action, now, revision):
        repo = subject["repository_id"]
        if subject["participant_id"] != action["old_owner"] or subject["generation"] != action["old_generation"]:
            raise CooperationError("unauthorized_actor")
        new = self._participant(db, repo, action["new_owner"])
        self._live(new)
        if new["running"] or any(c["participant_id"] == new["participant_id"] for c in bodies(db, "claims", repo)):
            raise CooperationError("claim_conflict", "New owner already holds a work unit")
        if _task(new) in {consumer for _, consumer in self._dependencies(db, repo)}:
            raise CooperationError("dependency_wait")
        if new["generation"] != action["new_generation"]:
            raise CooperationError("stale_generation")
        checkpoint = subject.get("checkpoint")
        if not checkpoint or not checkpoint["release_ready"] or checkpoint["checkpoint_id"] != action["checkpoint_id"]:
            raise CooperationError("scope_denied", "Old owner must first record the exact release-ready checkpoint")
        agreement = self._agreement(db, repo, action["agreement_id"])
        if agreement["pattern"] != "handoff" or agreement["state"] not in ("committed", "active", "checkpointed"):
            raise CooperationError("scope_denied")
        # Recovery advances generations without rewriting old consent. Apply
        # the same current task-binding checks used by acknowledgement/acquire.
        self._validate_terms(db, subject, agreement)
        acknowledgement = db.execute("SELECT body FROM acknowledgements WHERE repository_id=? AND agreement_id=? AND proposal_revision=? AND participant_id=?", (repo, agreement["agreement_id"], agreement["proposal_revision"], new["participant_id"])).fetchone()
        accepted = json.loads(acknowledgement[0]) if acknowledgement else {}
        if (accepted.get("accepted_checkpoint") != action["checkpoint_id"] or
                accepted.get("checkpoint_owner") != subject["participant_id"] or accepted.get("checkpoint_digest") != digest(checkpoint)):
            raise CooperationError("scope_denied", "New owner must accept the exact checkpoint")
        claims = self._claim_set(db, subject, action["claims"], all_owned=True)
        self._scope([c["resource"] for c in claims], new)
        owned = [r for r in agreement["terms"]["resources"] if r["owner"] == new["participant_id"]]
        if any(not self._within(c["resource"], owned) for c in claims):
            raise CooperationError("scope_denied")
        for claim in claims:
            claim.update(participant_id=new["participant_id"], generation=new["generation"], revision=revision,
                         agreement_id=agreement["agreement_id"], acquired_at=now,
                         checkpoint_due=now + agreement["terms"]["checkpoint"]["max_interval_seconds"])
            db.execute("UPDATE claims SET participant_id=?,body=? WHERE id=?", (new["participant_id"], canonical(claim), claim["claim_id"]))
        subject.update(running=False, ready_since=None, intent=[])
        new.update(running=True, ready_since=None, eligible_wait_seconds=0, checkpoint=None)
        self._save_participant(db, new)
        agreement.update(state="active", revision=revision)
        self._save_agreement(db, repo, agreement)
        return {"claims": claims}

    def _accept_artifact(self, db, subject, actor, action, now, revision):
        repo = subject["repository_id"]
        row = db.execute("SELECT body FROM artifacts WHERE repository_id=? AND id=?", (repo, action["artifact_id"])).fetchone()
        if not row:
            raise CooperationError("scope_denied", "Artifact not recorded")
        artifact = json.loads(row[0])
        for key in ("digest", "head_revision", "producer_task_id", "producer_attempt_id"):
            if artifact[key] != action[key]:
                raise CooperationError("artifact_mismatch", "Artifact changed; inspect the current revision and digest")
        if (action["consumer_task_id"], action["consumer_attempt_id"]) != (subject["task_id"], subject["attempt_id"]):
            raise CooperationError("scope_denied")
        if action["decision"] == "accepted" and (not action["checks"] or any(check["status"] != "passed" for check in action["checks"])):
            raise CooperationError("scope_denied", "Acceptance requires recorded passing consumer checks")
        db.execute("INSERT INTO acceptances VALUES (?,?,?,?) ON CONFLICT DO UPDATE SET body=excluded.body", (repo, action["artifact_id"], subject["participant_id"], canonical(action)))
        # The common transition updates wait clocks only when readiness changes;
        # accepting unrelated evidence must not reset accumulated eligible wait.
        self._complete_agreements(db, repo, now, revision)
        return {"accepted": action["decision"] == "accepted"}

    def _cancel(self, db, subject, actor, action, now, revision):
        repo = subject["repository_id"]
        if action["agreement_id"]:
            agreement = self._agreement(db, repo, action["agreement_id"])
            if subject["participant_id"] not in agreement["required_participants"]:
                raise CooperationError("unauthorized_actor")
            if agreement["state"] in ("completed", "cancelled", "expired", "rejected", "superseded"):
                raise CooperationError("stale_revision", "Terminal agreements cannot be cancelled again")
            agreement.update(state="cancelled", reason=action["reason"], terminal_at=now, revision=revision)
            self._save_agreement(db, repo, agreement)
            # Cancellation never releases another execution's claims.
            return {"agreement": agreement}
        held = [c for c in bodies(db, "claims", repo) if c["participant_id"] == subject["participant_id"]]
        if held:
            self._suspect(db, subject, "cancel_requested")
            return {"cancel_pending": True, "guidance": "Stop execution, inspect its checkpoint, recover and release exact claims"}
        subject.update(status="cancelled", running=False, intent=[], terminal_at=now)
        return {"cancelled": True}

    def _recover(self, db, subject, actor, action, now, revision):
        if action["service_epoch"] != self.epoch or action["previous_generation"] != subject["generation"]:
            raise CooperationError("stale_generation", generation=subject["generation"], service_epoch=self.epoch)
        reconciliation = action["reconciliation"]
        evidence = action["execution_evidence"]
        if not reconciliation["inspected"] or not evidence["reference"] or not evidence["execution_id"]:
            raise CooperationError("scope_denied", "Execution-stop evidence and inspected checkpoint are required")
        claims = self._claim_set(db, subject, action["claims"], all_owned=True)
        checkpoint = subject.get("checkpoint")
        if checkpoint and checkpoint["checkpoint_id"] != reconciliation["checkpoint_id"]:
            raise CooperationError("stale_revision", "Inspect the latest recorded checkpoint")
        if evidence["kind"] == "released" and (not checkpoint or not checkpoint["release_ready"]):
            raise CooperationError("scope_denied", "Release evidence needs a release-ready checkpoint")
        # Reconciliation releases enumerated claims. Fresh generation must acquire anew.
        for claim in claims:
            db.execute("DELETE FROM claims WHERE id=?", (claim["claim_id"],))
        subject.update(generation=subject["generation"] + 1, service_epoch=self.epoch, status="active",
                       reason=None, running=False, ready_since=None, eligible_wait_seconds=0, intent=[], checkpoint=None)
        return {"released": [c["claim_id"] for c in claims], "reconciled": True,
                "guidance": "Prior agreements retain old generations; create a fresh agreement before conflicting work"}

    def collect_terminal(self):
        """Owner-configured retention; active receipts/events never expire.

        Participant tombstones and proposal history stay durable. Only a contiguous
        terminal event prefix is collected, so a cursor never skips active events.
        """
        self._available()
        cutoff = self.clock() - self.retention_seconds
        with self.store.transaction() as db:
            self._epoch_check(db)
            removed = 0
            for repo in [r[0] for r in db.execute("SELECT id FROM repositories")]:
                participants = bodies(db, "participants", repo)
                held = {claim["participant_id"] for claim in bodies(db, "claims", repo)}
                parents = {participant.get("parent_id") for participant in participants}
                active_parties = {party for agreement in bodies(db, "agreements", repo)
                                  if agreement["state"] not in ("completed", "cancelled", "expired", "rejected")
                                  for party in agreement["required_participants"]}
                retired = False
                for participant in participants:
                    participant_id = participant["participant_id"]
                    used = db.execute("SELECT 1 FROM participant_activity WHERE participant_id=?", (participant_id,)).fetchone()
                    if (participant["status"] == "cancelled" or participant["created_at"] >= cutoff
                            or used or participant_id in held | parents | active_parties):
                        continue
                    # No successful authenticated use ever occurred. This is the
                    # lost-registration-response case, never established ownership.
                    participant.update(status="cancelled", terminal_at=self.clock(),
                                       reason="unused_registration_retired", generation=participant["generation"] + 1)
                    self._save_participant(db, participant)
                    db.execute("UPDATE participants SET verifier='' WHERE id=?", (participant_id,))
                    retired = True
                if retired:
                    db.execute("UPDATE repositories SET revision=revision+1 WHERE id=?", (repo,))
                terminal = {a["agreement_id"] for a in bodies(db, "agreements", repo) if a.get("terminal_at") is not None and a["terminal_at"] < cutoff}
                cancelled = {p["participant_id"] for p in bodies(db, "participants", repo) if p["status"] == "cancelled" and p.get("terminal_at", self.clock()) < cutoff}
                minimum = 0
                for event in db.execute("SELECT cursor,agreement_id,body,created_at FROM events WHERE repository_id=? ORDER BY cursor", (repo,)).fetchall():
                    envelope = json.loads(event[2])
                    actor = envelope["subject_id"] if envelope["kind"] == "reconcile" else envelope["actor_id"]
                    if event[3] >= cutoff or not (event[1] in terminal or (event[1] is None and actor in cancelled)):
                        break
                    db.execute("DELETE FROM events WHERE cursor=?", (event[0],))
                    minimum = event[0]
                    removed += 1
                if minimum:
                    db.execute("UPDATE repositories SET minimum_cursor=MAX(minimum_cursor,?) WHERE id=?", (minimum, repo))
                for row in db.execute("SELECT participant_id,operation_id,agreement_id,created_at FROM receipts WHERE repository_id=?", (repo,)).fetchall():
                    if row[3] < cutoff and (row[2] in terminal or (row[2] is None and row[0] in cancelled)):
                        db.execute("DELETE FROM receipts WHERE repository_id=? AND participant_id=? AND operation_id=?", (repo, row[0], row[1]))
            return removed
