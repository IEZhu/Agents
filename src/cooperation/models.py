"""Closed, byte-bounded contracts for the local cooperation journal.

These schemas describe metadata, not execution authority. Path, capability and
assignment authorization are checked by the service after shape validation.
"""
from __future__ import annotations

import json
import math
from typing import Annotated, Any, ClassVar, Literal, TypeVar

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field, RootModel,
                      StringConstraints, model_validator)

PROTOCOL = "agents-core/cooperation/1"
MAX_REQUEST_BYTES = 32 * 1024
MAX_EVENT_BYTES = MAX_REQUEST_BYTES
MAX_JSON_DEPTH = 8
MAX_ITEMS = 50


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000),
                 AfterValidator(_nonblank)]
Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128,
                                             pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
RevisionNumber = Annotated[int, Field(strict=True, ge=0)]
MailboxCursor = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Generation = Annotated[int, Field(strict=True, ge=1)]
Timestamp = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
TextList = Annotated[list[Text], Field(max_length=MAX_ITEMS)]
IDList = Annotated[list[Identifier], Field(max_length=MAX_ITEMS)]
Protocol = Literal["agents-core/cooperation/1"]
Access = Literal["snapshot_read", "write", "integrate"]
ResourceType = Literal["physical_file", "physical_subtree", "logical_intent",
                       "integration_target", "shared_git", "heavy_execution"]
PriorityValue = Literal["background", "normal", "urgent"]
Pattern = Literal["inform_reuse", "partition_join", "producer_consumer",
                  "single_writer_reviewers", "handoff", "duplicate_adoption",
                  "bounded_dispute"]
CommunicationMode = Literal["mcp", "native", "parent_relay", "checkpoint"]
MessageKind = Literal["notice", "request", "response"]
RequestState = Literal["pending", "accepted", "input_required", "completed",
                       "declined", "failed", "cancelled", "expired"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ResourceSpec(StrictModel):
    resource_type: ResourceType
    resource_key: Text
    access: Access


ResourceList = Annotated[list[ResourceSpec], Field(max_length=MAX_ITEMS)]
RequiredResources = Annotated[list[ResourceSpec], Field(min_length=1, max_length=MAX_ITEMS)]


class OwnedResource(ResourceSpec):
    owner: Identifier


class Priority(StrictModel):
    value: PriorityValue = "normal"
    source: Literal["default", "authorized_user", "parent", "asserted"] = "default"
    evidence_ref: Text | None = None

    @model_validator(mode="after")
    def provenance(self) -> Priority:
        if self.source == "default" and self.value != "normal":
            raise ValueError("default priority must be normal")
        if self.source in {"authorized_user", "parent"} and self.evidence_ref is None:
            raise ValueError("authorized priority requires an evidence reference")
        return self


class CheckpointSpec(StrictModel):
    trigger: Text
    max_interval_seconds: int = Field(default=300, ge=1, le=300)


class TaskBinding(StrictModel):
    participant_id: Identifier
    task_id: Identifier
    attempt_id: Identifier
    generation: Generation
    contract_revision: Digest | None = None
    parent_id: Identifier | None = None


class Dependency(StrictModel):
    """An unaccepted artifact gate, directed from producer to consumer."""

    producer_task: Identifier
    consumer_task: Identifier
    artifact_id: Identifier
    producer_attempt_id: Identifier = "1"
    consumer_attempt_id: Identifier = "1"
    gate: Literal["consumer_accepted"] = "consumer_accepted"


Dependencies = Annotated[list[Dependency], Field(max_length=MAX_ITEMS)]


class ArtifactContract(StrictModel):
    artifact_id: Identifier
    producer: Identifier
    consumer: Identifier
    acceptance: Annotated[list[Text], Field(min_length=1, max_length=MAX_ITEMS)]


class Check(StrictModel):
    name: Text
    status: Literal["passed", "failed", "not_run"]
    evidence_refs: TextList = Field(default_factory=list)


Checks = Annotated[list[Check], Field(max_length=MAX_ITEMS)]


class Artifact(StrictModel):
    artifact_id: Identifier
    producer_task_id: Identifier
    producer_attempt_id: Identifier
    repository_id: Identifier
    base_revision: Text
    head_revision: Text
    path: Text | None = None
    uri: Text | None = None
    digest: Digest
    media_type: Text
    summary: Text
    checks: Checks = Field(default_factory=list)
    limitations: TextList = Field(default_factory=list)
    acceptance_conditions: TextList = Field(default_factory=list)

    @model_validator(mode="after")
    def one_reference(self) -> Artifact:
        if (self.path is None) == (self.uri is None):
            raise ValueError("artifact requires exactly one path or URI")
        if self.path is not None:
            # Filesystem resolution remains in the identity layer; references
            # are never fetched during validation.
            path = self.path.replace("\\", "/")
            if path.startswith("/") or ":" in path or ".." in path.split("/"):
                raise ValueError("artifact path must be relative and contained")
        return self


class AgreementTerms(StrictModel):
    scope: Text
    resources: Annotated[list[OwnedResource], Field(min_length=1, max_length=MAX_ITEMS)]
    dependencies: Dependencies = Field(default_factory=list)
    artifact_contracts: Annotated[list[ArtifactContract], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)
    integrator: Identifier
    priority: Priority = Field(default_factory=Priority)
    checkpoint: CheckpointSpec
    completion_conditions: Annotated[list[Text], Field(min_length=1, max_length=MAX_ITEMS)]
    acknowledgement_timeout_seconds: int = Field(default=120, ge=1, le=120)


class DeclareIntent(StrictModel):
    kind: Literal["declare_intent"]
    resources: ResourceList = Field(default_factory=list)
    dependencies: Dependencies = Field(default_factory=list)
    priority: Priority = Field(default_factory=Priority)
    checkpoint: CheckpointSpec


class Propose(StrictModel):
    kind: Literal["propose"]
    agreement_id: Identifier
    proposal_revision: Generation = 1
    supersedes: Generation | None = None
    pattern: Pattern
    required_participants: Annotated[list[Identifier], Field(min_length=1, max_length=8)]
    task_bindings: Annotated[list[TaskBinding], Field(min_length=1, max_length=8)]
    terms: AgreementTerms

    @model_validator(mode="after")
    def participant_set(self) -> Propose:
        participants = set(self.required_participants)
        bindings = [binding.participant_id for binding in self.task_bindings]
        if len(participants) != len(self.required_participants):
            raise ValueError("required participants must be unique")
        if len(set(bindings)) != len(bindings) or set(bindings) != participants:
            raise ValueError("task bindings must match required participants exactly")
        owners = {resource.owner for resource in self.terms.resources}
        artifact_parties = {party for item in self.terms.artifact_contracts
                            for party in (item.producer, item.consumer)}
        if not owners | artifact_parties | {self.terms.integrator} <= participants:
            raise ValueError("terms may name only required participants")
        if self.proposal_revision == 1 and self.supersedes is not None:
            raise ValueError("initial proposal cannot supersede a revision")
        if self.proposal_revision > 1 and self.supersedes != self.proposal_revision - 1:
            raise ValueError("counterproposal must name its preceding revision")
        return self


class Acknowledge(StrictModel):
    kind: Literal["acknowledge"]
    agreement_id: Identifier
    proposal_revision: Generation
    terms_digest: Digest
    acceptance_mode: Literal["self", "parent_relay"] = "self"
    accepted_checkpoint: Identifier | None = None


class Reject(StrictModel):
    kind: Literal["reject"]
    agreement_id: Identifier
    proposal_revision: Generation
    reason: Text


class Acquire(StrictModel):
    kind: Literal["acquire"]
    resources: RequiredResources
    work_unit_id: Identifier
    checkpoint: CheckpointSpec
    agreement_id: Identifier | None = None


class Heartbeat(StrictModel):
    kind: Literal["heartbeat"]
    checkpoint_id: Identifier | None = None


class Checkpoint(StrictModel):
    kind: Literal["checkpoint"]
    checkpoint_id: Identifier
    artifacts: Annotated[list[Artifact], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)
    checks: Checks = Field(default_factory=list)
    limitations: TextList = Field(default_factory=list)
    release_ready: bool = False
    agreement_id: Identifier | None = None


class ClaimReference(StrictModel):
    claim_id: Identifier
    expected_revision: RevisionNumber


ClaimReferences = Annotated[list[ClaimReference], Field(min_length=1, max_length=MAX_ITEMS)]


class Release(StrictModel):
    kind: Literal["release"]
    claims: ClaimReferences
    reason: Text


class Handoff(StrictModel):
    kind: Literal["handoff"]
    claims: ClaimReferences
    old_owner: Identifier
    new_owner: Identifier
    old_generation: Generation
    new_generation: Generation
    checkpoint_id: Identifier
    agreement_id: Identifier


class AcceptArtifact(StrictModel):
    kind: Literal["accept_artifact"]
    artifact_id: Identifier
    digest: Digest
    head_revision: Text
    producer_task_id: Identifier
    producer_attempt_id: Identifier
    consumer_task_id: Identifier
    consumer_attempt_id: Identifier
    checks: Checks = Field(default_factory=list)
    decision: Literal["accepted", "rejected"] = "accepted"
    reason: Text | None = None


class Cancel(StrictModel):
    kind: Literal["cancel"]
    reason: Text
    agreement_id: Identifier | None = None


class ExecutionEvidence(StrictModel):
    kind: Literal["stopped", "released"]
    reference: Text
    execution_id: Identifier


class Reconciliation(StrictModel):
    checkpoint_id: Identifier | None
    inspected: bool
    summary: Text


class Recover(StrictModel):
    kind: Literal["recover"]
    service_epoch: Generation
    previous_generation: Generation
    execution_evidence: ExecutionEvidence
    reconciliation: Reconciliation
    claims: Annotated[list[ClaimReference], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)


Action = Annotated[DeclareIntent | Propose | Acknowledge | Reject | Acquire |
                   Heartbeat | Checkpoint | Release | Handoff | AcceptArtifact |
                   Cancel | Recover, Field(discriminator="kind")]
ACTION_MODELS = (DeclareIntent, Propose, Acknowledge, Reject, Acquire, Heartbeat,
                 Checkpoint, Release, Handoff, AcceptArtifact, Cancel, Recover)


class RegisterRequest(StrictModel):
    protocol: Protocol
    checkout_path: Text
    root_task_id: Identifier
    task_id: Identifier
    attempt_id: Identifier
    scope: RequiredResources
    contract_revision: Digest | None = None
    parent_id: Identifier | None = None
    parent_capability: Text | None = Field(default=None, repr=False)
    role: Text | None = None
    communication_mode: CommunicationMode = "mcp"
    artifact_access: TextList = Field(default_factory=list)
    execution_mode: Literal["independent", "parent_relay"] = "parent_relay"

    @model_validator(mode="after")
    def parent_credential(self) -> RegisterRequest:
        if (self.parent_id is None) != (self.parent_capability is None):
            raise ValueError("parent identity and capability must be supplied together")
        return self


class ReadRequest(StrictModel):
    protocol: Protocol
    repository_id: Identifier
    cursor: RevisionNumber | None = None
    limit: int = Field(default=50, ge=1, le=100)
    offset: RevisionNumber = 0
    snapshot_time: Timestamp | None = None
    participant_ids: IDList = Field(default_factory=list)
    agreement_ids: IDList = Field(default_factory=list)
    task_ids: IDList = Field(default_factory=list)


class ApplyRequest(StrictModel):
    protocol: Protocol
    repository_id: Identifier
    participant_id: Identifier
    capability: Text = Field(repr=False)
    operation_id: Identifier
    expected_revision: RevisionNumber
    expected_generation: Generation
    subject_id: Identifier | None = None
    action: Action


class MailboxRequest(StrictModel):
    protocol: Protocol
    repository_id: Identifier
    participant_id: Identifier
    capability: Text = Field(repr=False)
    subject_id: Identifier | None = None


class SendRequest(MailboxRequest):
    operation_id: Identifier
    recipient_id: Identifier
    kind: MessageKind = "notice"
    body: Text = Field(repr=False)
    reply_to: Identifier | None = None
    correlation_id: Identifier | None = None
    expires_at: Timestamp | None = None

    @model_validator(mode="after")
    def request_deadline(self) -> SendRequest:
        if self.expires_at is not None and self.kind != "request":
            raise ValueError("Only requests can have a deadline")
        return self


class ReceiveRequest(MailboxRequest):
    after_cursor: MailboxCursor = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=MAX_ITEMS)] = 20
    include_acknowledged: bool = False


class AckRequest(MailboxRequest):
    operation_id: Identifier
    message_ids: Annotated[list[Identifier], Field(min_length=1, max_length=MAX_ITEMS)]

    @model_validator(mode="after")
    def unique_messages(self) -> AckRequest:
        if len(self.message_ids) != len(set(self.message_ids)):
            raise ValueError("message IDs must be unique")
        return self


class RequestStatusRequest(MailboxRequest):
    message_id: Identifier


class RequestUpdateRequest(RequestStatusRequest):
    operation_id: Identifier
    expected_revision: Generation
    state: Literal["accepted", "input_required", "completed", "declined", "failed", "cancelled"]
    reason: Text | None = None
    response_message_id: Identifier | None = None

    @model_validator(mode="after")
    def outcome_details(self) -> RequestUpdateRequest:
        if self.state in ("input_required", "declined", "failed", "cancelled") and self.reason is None:
            raise ValueError("This outcome requires a reason")
        if self.state == "completed" and self.response_message_id is None:
            raise ValueError("Completion requires a response message")
        if self.state != "completed" and self.response_message_id is not None:
            raise ValueError("Only completion can bind a response message")
        return self


class WaitRequest(MailboxRequest):
    after_cursor: MailboxCursor = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=MAX_ITEMS)] = 20
    timeout_seconds: Annotated[int, Field(strict=True, ge=0, le=30)] = 20


class TrackedRequest(StrictModel):
    message_id: Identifier
    sender_id: Identifier
    recipient_id: Identifier
    state: RequestState
    revision: Generation
    created_at: Timestamp
    updated_at: Timestamp
    expires_at: Timestamp | None
    reason: Text | None
    response_message_id: Identifier | None
    updated_by: Identifier | None
    delivery: Literal["stored", "acknowledged"]
    acknowledged_at: Timestamp | None


class MailboxNotification(StrictModel):
    cursor: MailboxCursor
    kind: Literal["message_received", "message_acknowledged", "request_updated"]
    message_id: Identifier
    created_at: Timestamp
    request_state: RequestState | None
    request_revision: Generation | None


class Message(StrictModel):
    message_id: Identifier
    cursor: MailboxCursor
    repository_id: Identifier
    sender_id: Identifier
    recipient_id: Identifier
    actor_id: Identifier
    kind: MessageKind
    body: Text = Field(repr=False)
    reply_to: Identifier | None
    correlation_id: Identifier | None
    created_at: Timestamp
    acknowledged_at: Timestamp | None
    acknowledged_by: Identifier | None


class Claim(StrictModel):
    claim_id: Identifier
    repository_id: Identifier
    participant_id: Identifier
    generation: Generation
    agreement_id: Identifier | None
    revision: RevisionNumber
    resource: ResourceSpec
    work_unit_id: Identifier
    quarantined: bool
    acquired_at: Timestamp
    checkpoint_due: Timestamp


class OwnerCleanupPayload(StrictModel):
    """Owner-only offline audit record, deliberately absent from ApplyRequest."""

    reason: Text
    inspection: Text
    execution_pid: Annotated[int, Field(strict=True, ge=1, le=2147483647)]
    checkpoint_id: Identifier | None
    released_claims: IDList


class ReconcilePayload(StrictModel):
    """Coordinator-only transition, never a participant mutation action."""

    kind: Literal["reconcile"] = "reconcile"
    reason: Literal["heartbeat_overdue", "dependency_cycle", "proposal_expired", "coordinator_restart"]


class EventEnvelope(StrictModel):
    protocol: Protocol
    event_id: Identifier
    operation_id: Identifier
    repository_id: Identifier
    revision: RevisionNumber
    actor_id: Identifier
    subject_id: Identifier | None
    task_id: Identifier
    attempt_id: Identifier
    generation: Generation
    kind: Literal["declare_intent", "propose", "acknowledge", "reject", "acquire",
                  "heartbeat", "checkpoint", "release", "handoff", "accept_artifact",
                  "cancel", "recover", "owner_cleanup", "reconcile"]
    agreement_id: Identifier | None
    proposal_revision: Generation | None
    reply_to: Identifier | None
    created_at: Timestamp
    payload: Action | OwnerCleanupPayload | ReconcilePayload

    @model_validator(mode="after")
    def event_kind(self) -> EventEnvelope:
        payload_kind = "owner_cleanup" if isinstance(self.payload, OwnerCleanupPayload) else self.payload.kind
        if self.kind != payload_kind:
            raise ValueError("event kind must match its typed payload")
        return self


ErrorCode = Literal["invalid_request", "unsupported_version", "workspace_required",
                    "workspace_invalid", "unauthorized_actor", "scope_denied",
                    "stale_revision", "stale_generation", "claim_conflict",
                    "dependency_cycle", "proposal_expired", "idempotency_conflict",
                    "busy", "cursor_expired", "store_unavailable", "dependency_wait",
                    "priority_wait", "execution_lease_required", "artifact_mismatch", "mailbox_full"]


class ErrorResponse(StrictModel):
    protocol: Protocol = PROTOCOL
    status: Literal["error"] = "error"
    error: ErrorCode
    message: Text
    revision: RevisionNumber | None = None
    generation: Generation | None = None
    minimum_cursor: RevisionNumber | None = None
    cycle: TextList = Field(default_factory=list)
    cycle_truncated: bool = False
    service_epoch: Generation | None = None
    claim_ids: IDList = Field(default_factory=list)
    owner: Identifier | None = None
    participant_id: Identifier | None = None


class IdentityProjection(StrictModel):
    repository_id: Identifier
    workspace_id: Text
    checkout_id: Identifier
    checkout_path: Text
    base_commit: Text | None
    branch: Text | None
    common_dir: Annotated[str, StringConstraints(strict=True, max_length=4000)] = ""


class RecordedCheckpoint(Checkpoint):
    revision: RevisionNumber
    created_at: Timestamp


class Participant(StrictModel):
    """Public journal projection: capability/verifier fields are forbidden."""

    participant_id: Identifier
    repository_id: Identifier
    workspace_id: Text
    checkout_id: Identifier
    root_task_id: Identifier
    task_id: Identifier
    attempt_id: Identifier
    contract_revision: Digest | None
    parent_id: Identifier | None
    role: Text | None
    scope: RequiredResources
    communication_mode: CommunicationMode
    artifact_access: TextList
    execution_mode: Literal["independent", "parent_relay"]
    identity: IdentityProjection
    generation: Generation
    service_epoch: Generation
    status: Literal["active", "suspect", "cancelled"]
    created_at: Timestamp
    last_seen: Timestamp
    ready_since: Timestamp | None
    eligible_wait_seconds: Timestamp
    ready_sequence: RevisionNumber
    base_priority: Annotated[int, Field(strict=True, ge=0, le=2)]
    priority_source: Literal["default"] | Priority
    intent: ResourceList
    checkpoint: RecordedCheckpoint | None
    running: bool
    reason: Text | None = None
    checkpoint_spec: CheckpointSpec | None = None
    terminal_at: Timestamp | None = None


class Agreement(Propose):
    state: Literal["proposed", "committed", "active", "checkpointed", "completed",
                   "superseded", "rejected", "expired", "suspended", "cancelled"]
    terms_digest: Digest
    counterproposers: Annotated[list[Identifier], Field(max_length=8)]
    deadline: Timestamp
    revision: RevisionNumber
    terminal_at: Timestamp | None
    reason: Text | None = None


class ReadyTask(StrictModel):
    id: Text
    participant_id: Identifier
    base_priority: Annotated[int, Field(strict=True, ge=0, le=2)]
    eligible_wait_seconds: Timestamp
    ready_sequence: RevisionNumber
    ready: bool
    running: bool
    resources: ResourceList
    effective_priority: Annotated[int, Field(strict=True, ge=0, le=2)]


ReadyTasks = Annotated[list[ReadyTask], Field(max_length=100)]
Claims = Annotated[list[Claim], Field(max_length=100)]


class RegisterSuccess(StrictModel):
    protocol: Protocol
    status: Literal["ok"]
    participant_id: Identifier
    repository_id: Identifier
    workspace_id: Text
    checkout_id: Identifier
    generation: Generation
    service_epoch: Generation
    capability: Text = Field(repr=False)
    revision: RevisionNumber
    communication_mode: CommunicationMode
    guidance: Text


class SnapshotCounts(StrictModel):
    participants: RevisionNumber
    agreements: RevisionNumber
    claims: RevisionNumber = 0
    artifacts: RevisionNumber = 0
    dependencies: RevisionNumber = 0
    ready: RevisionNumber = 0


class ReadSuccess(StrictModel):
    protocol: Protocol
    status: Literal["ok"]
    repository_id: Identifier
    revision: RevisionNumber
    service_epoch: Generation
    minimum_cursor: RevisionNumber
    snapshot_cursor: RevisionNumber
    snapshot_time: Timestamp
    next_cursor: RevisionNumber
    has_more_events: bool
    participants: Annotated[list[Participant], Field(max_length=100)]
    agreements: Annotated[list[Agreement], Field(max_length=100)]
    snapshot_counts: SnapshotCounts
    snapshot_truncated: bool
    snapshot_offset: RevisionNumber = 0
    next_snapshot_offset: RevisionNumber | None = None
    claims: Claims
    artifacts: Annotated[list[Artifact], Field(max_length=100)]
    dependencies: Annotated[list[Dependency], Field(max_length=100)]
    dependency_cycles: Annotated[list[TextList], Field(max_length=100)] = Field(default_factory=list)
    dependency_cycles_count: RevisionNumber = 0
    dependency_cycles_truncated: bool = False
    ready: ReadyTasks
    events: Annotated[list[EventEnvelope], Field(max_length=100)]
    limitations: Text


class SendSuccess(StrictModel):
    protocol: Protocol
    status: Literal["ok"]
    repository_id: Identifier
    message: Message


class ReceiveSuccess(StrictModel):
    protocol: Protocol
    status: Literal["ok"]
    repository_id: Identifier
    participant_id: Identifier
    messages: Annotated[list[Message], Field(max_length=MAX_ITEMS)]
    next_cursor: MailboxCursor
    has_more: bool


class AckSuccess(StrictModel):
    protocol: Protocol
    status: Literal["ok"]
    repository_id: Identifier
    participant_id: Identifier
    message_ids: Annotated[list[Identifier], Field(min_length=1, max_length=MAX_ITEMS)]
    acknowledged_at: Timestamp


class RequestStatusSuccess(StrictModel):
    protocol: Protocol
    status: Literal["ok"]
    repository_id: Identifier
    request: TrackedRequest


class WaitSuccess(StrictModel):
    protocol: Protocol
    status: Literal["ok"]
    repository_id: Identifier
    participant_id: Identifier
    events: Annotated[list[MailboxNotification], Field(max_length=MAX_ITEMS)]
    next_cursor: MailboxCursor
    has_more: bool
    timed_out: bool
    service_epoch: Generation


class ApplySuccess(StrictModel):
    """Shared receipt fields; concrete subclasses add only their action result."""

    protocol: Protocol
    status: Literal["ok"]
    revision: RevisionNumber
    generation: Generation
    service_epoch: Generation
    event: EventEnvelope
    cursor: RevisionNumber
    action_kinds: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def matching_action(self) -> ApplySuccess:
        if self.event.kind not in self.action_kinds:
            raise ValueError("Receipt result does not match its event action")
        return self


class DeclareIntentSuccess(ApplySuccess):
    action_kinds = frozenset({"declare_intent"})
    base_priority: Annotated[int, Field(strict=True, ge=0, le=2)]
    priority_verified: bool
    ready: ReadyTasks
    ready_count: RevisionNumber = 0
    ready_truncated: bool = False


class AgreementSuccess(ApplySuccess):
    action_kinds = frozenset({"propose", "reject", "cancel"})
    agreement: Agreement


class AcknowledgeSuccess(ApplySuccess):
    action_kinds = frozenset({"acknowledge"})
    agreement: Agreement
    acknowledged: Annotated[list[Identifier], Field(max_length=8)]


class AcquireSuccess(ApplySuccess):
    action_kinds = frozenset({"acquire"})
    claims: Claims
    execution_admitted: bool


class HeartbeatSuccess(ApplySuccess):
    action_kinds = frozenset({"heartbeat"})
    checkpoint_required: IDList


class CheckpointSuccess(ApplySuccess):
    action_kinds = frozenset({"checkpoint"})
    checkpoint_id: Identifier


class ReleaseSuccess(ApplySuccess):
    action_kinds = frozenset({"release"})
    released: IDList


class HandoffSuccess(ApplySuccess):
    action_kinds = frozenset({"handoff"})
    claims: Claims


class AcceptArtifactSuccess(ApplySuccess):
    action_kinds = frozenset({"accept_artifact"})
    accepted: bool


class CancelPendingSuccess(ApplySuccess):
    action_kinds = frozenset({"cancel"})
    cancel_pending: Literal[True]
    guidance: Text


class CancelSuccess(ApplySuccess):
    action_kinds = frozenset({"cancel"})
    cancelled: Literal[True]


class RecoverSuccess(ApplySuccess):
    action_kinds = frozenset({"recover"})
    released: IDList
    reconciled: Literal[True]
    guidance: Text


ApplyResult = (DeclareIntentSuccess | AgreementSuccess | AcknowledgeSuccess |
               AcquireSuccess | HeartbeatSuccess | CheckpointSuccess | ReleaseSuccess |
               HandoffSuccess | AcceptArtifactSuccess | CancelPendingSuccess |
               CancelSuccess | RecoverSuccess)


class RegisterResponse(RootModel[RegisterSuccess | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class ReadResponse(RootModel[ReadSuccess | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class ApplyResponse(RootModel[ApplyResult | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class SendResponse(RootModel[SendSuccess | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class ReceiveResponse(RootModel[ReceiveSuccess | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class AckResponse(RootModel[AckSuccess | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class RequestStatusResponse(RootModel[RequestStatusSuccess | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class WaitResponse(RootModel[WaitSuccess | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class InvalidRequest(ValueError):
    """A deliberately bounded error that never includes request values."""

    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRequest("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise InvalidRequest("Non-finite JSON number")


def bounded_object(value: dict[str, Any] | str) -> dict[str, Any]:
    """Decode without coercion, duplicate keys, nonfinite values or oversized trees."""
    try:
        if isinstance(value, str):
            if len(value.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise InvalidRequest("Request exceeds 32768 UTF-8 bytes")
            value = json.loads(value, object_pairs_hook=_object_pairs,
                               parse_constant=_reject_constant)
        if type(value) is not dict:
            raise InvalidRequest("Request must be a JSON object")
        stack = [(value, 1)]
        nodes = 0
        while stack:
            current, depth = stack.pop()
            nodes += 1
            if nodes > MAX_REQUEST_BYTES:
                raise InvalidRequest("Request has too many values")
            if type(current) in (dict, list):
                if depth > MAX_JSON_DEPTH:
                    raise InvalidRequest("Request exceeds maximum JSON depth 8")
                if len(current) > MAX_REQUEST_BYTES:
                    raise InvalidRequest("Request has too many values")
                if type(current) is dict:
                    if not all(type(key) is str for key in current):
                        raise InvalidRequest("JSON object keys must be strings")
                    children = current.values()
                else:
                    children = current
                stack.extend((item, depth + 1) for item in children)
            elif type(current) is float:
                if not math.isfinite(current):
                    raise InvalidRequest("Non-finite JSON number")
            elif type(current) not in (str, int, bool, type(None)):
                raise InvalidRequest("Request contains a non-JSON value")
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise InvalidRequest("Request exceeds 32768 UTF-8 bytes")
    except (UnicodeError, RecursionError, OverflowError, json.JSONDecodeError) as exc:
        raise InvalidRequest("Malformed JSON request") from exc
    if value.get("protocol") != PROTOCOL:
        raise InvalidRequest("Unsupported cooperation protocol", "unsupported_version")
    return value


ModelT = TypeVar("ModelT", bound=StrictModel)


def _parse(model: type[ModelT], value: dict[str, Any] | str) -> ModelT:
    return model.model_validate(bounded_object(value))


def parse_register(value: dict[str, Any] | str) -> RegisterRequest:
    return _parse(RegisterRequest, value)


def parse_read(value: dict[str, Any] | str) -> ReadRequest:
    return _parse(ReadRequest, value)


def parse_apply(value: dict[str, Any] | str) -> ApplyRequest:
    return _parse(ApplyRequest, value)


def parse_send(value: dict[str, Any] | str) -> SendRequest:
    return _parse(SendRequest, value)


def parse_receive(value: dict[str, Any] | str) -> ReceiveRequest:
    return _parse(ReceiveRequest, value)


def parse_ack(value: dict[str, Any] | str) -> AckRequest:
    return _parse(AckRequest, value)


def parse_request_status(value: dict[str, Any] | str) -> RequestStatusRequest:
    return _parse(RequestStatusRequest, value)


def parse_request_update(value: dict[str, Any] | str) -> RequestUpdateRequest:
    return _parse(RequestUpdateRequest, value)


def parse_wait(value: dict[str, Any] | str) -> WaitRequest:
    return _parse(WaitRequest, value)


def json_schemas() -> dict[str, Any]:
    """Export self-contained roots; every nested object is a closed model."""
    models = (RegisterRequest, ReadRequest, ApplyRequest, EventEnvelope, Claim,
              Artifact, AgreementTerms, Agreement, ErrorResponse, RegisterResponse,
              ReadResponse, ApplyResponse, SendRequest, ReceiveRequest, AckRequest,
              Message, SendResponse, ReceiveResponse, AckResponse,
              RequestStatusRequest, RequestUpdateRequest, WaitRequest, TrackedRequest,
              MailboxNotification, RequestStatusResponse, WaitResponse, *ACTION_MODELS)
    return {"protocol": PROTOCOL, "max_request_bytes": MAX_REQUEST_BYTES,
            "max_json_depth": MAX_JSON_DEPTH,
            "schemas": {model.__name__: model.model_json_schema() for model in models}}
