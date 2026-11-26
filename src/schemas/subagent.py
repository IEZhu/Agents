"""Closed, bounded data contracts for stateless delegated workers (not personas)."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field, RootModel,
                      StringConstraints, field_validator, model_serializer, model_validator)

from src.schemas.artifacts import ArtifactManifest

PROTOCOL = "agents-core/subagent/1"
MAX_REQUEST_CHARS = 48000
MAX_RESULT_CHARS = 128000
MAX_JSON_DEPTH = 12
MAX_JSON_NODES = 10000
MAX_ERRORS = 20


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000),
                 AfterValidator(_nonblank)]
Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128,
                                             pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
AgentName = Annotated[str, StringConstraints(strict=True, max_length=80,
                                           pattern=r"^[a-z0-9][a-z0-9_]*$")]
Revision = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
WorkmapNodeId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")]
TextList = Annotated[list[Text], Field(max_length=50)]
ResultType = Literal["generic", "review", "implementation", "research"]
# text: plain-text assignment and result (src/engine/subagent_text.py); json: the JSON contract
# and result schema, for structured-output runners such as the autodev launcher.
ResultFormat = Literal["text", "json"]
Complexity = Literal["low", "standard", "high"]
Tier = Literal["light", "standard", "inherit"]
Client = Literal["claude", "codex", "cursor"]
# A model name as both CLIs take it on the command line (src/flow_execution.py MODEL_NAME).
ModelName = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}$")]
Effort = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z]{1,16}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Budgets(StrictModel):
    max_skills: int = Field(default=3, ge=0, le=5)
    max_implants: int = Field(default=0, ge=0, le=2)
    max_context_chars: int = Field(default=16000, ge=2000, le=64000)


class ContractBudgets(StrictModel):
    max_skills: int = Field(ge=0, le=5)
    max_implants: int = Field(ge=0, le=2)
    max_context_chars: int = Field(ge=2000, le=64000)


class PreparationRequest(Budgets):
    agent_name: AgentName
    task: Text
    acceptance_criteria: Annotated[list[Text], Field(min_length=1, max_length=50)]
    task_id: Identifier | None = None
    attempt_id: Identifier = "1"
    context: TextList = Field(default_factory=list)
    scope: TextList = Field(default_factory=list)
    non_goals: TextList = Field(default_factory=list)
    result_type: ResultType = "generic"
    result_format: ResultFormat = "text"
    artifact_manifest: ArtifactManifest | None = None
    complexity: Complexity | None = None
    client: Client | None = None
    workmap_node: WorkmapNodeId | None = None
    workmap_inputs: Annotated[list[WorkmapNodeId], Field(min_length=1, max_length=10)] | None = None

    @model_validator(mode="after")
    def workmap_needs_task_and_text(self) -> PreparationRequest:
        if self.workmap_node is not None or self.workmap_inputs is not None:
            if self.task_id is None:
                raise ValueError("workmap_node and workmap_inputs need task_id, the map's task ID")
            if self.result_format != "text":
                raise ValueError("a work-map package uses result_format text")
        return self

    @field_validator("workmap_inputs")
    @classmethod
    def workmap_inputs_are_distinct(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("workmap_inputs names each node once")
        return value

    @model_validator(mode="after")
    def artifact_task_matches(self) -> PreparationRequest:
        if (self.artifact_manifest is not None and self.task_id is not None
                and self.task_id != self.artifact_manifest.task_id):
            raise ValueError("artifact manifest task_id must match the requested task_id")
        return self


class Criterion(StrictModel):
    id: Identifier
    description: Text


class ContractExecution(StrictModel):
    """The model tier a worker was prepared for; part of the hashed contract."""
    complexity: Complexity
    tier: Tier
    client: Client | None
    model: ModelName | None
    reasoning_effort: Effort | None


class Spawn(StrictModel):
    """Arguments for the host's own worker-launching tool."""
    tool: Literal["Agent", "spawn_agent", "Task"]
    arguments: dict[str, str]


class ExecutionPlan(ContractExecution):
    requested_tier: Tier
    floor_reasons: Annotated[list[str], Field(max_length=50)]
    source: Literal["alias", "catalog", "pinned", "override", "inherit", "disabled"]
    spawn: Spawn | None

    def contract_part(self) -> dict:
        return ContractExecution.model_validate(
            self.model_dump(include=set(ContractExecution.model_fields))).model_dump()


class Escalation(StrictModel):
    """The next tier for a new attempt after a light or standard worker fell short."""
    complexity: Complexity
    tier: Tier
    reason: Literal["invalid_result", "status_failed", "status_partial", "check_failed"]


def _omit_none(data: dict, *keys: str) -> dict:
    for key in keys:
        if data.get(key) is None:
            data.pop(key, None)
    return data


class WorkmapBinding(StrictModel):
    """The node a contract's result is recorded on. The server sets root from the workspace it
    resolved at preparation; a caller never supplies it."""

    node: WorkmapNodeId
    root: Text


class TaskContract(StrictModel):
    protocol: Literal["agents-core/subagent/1"]
    task_id: Identifier
    attempt_id: Identifier
    agent: AgentName
    bundle_revision: Revision
    contract_revision: Revision
    task: Text
    context: TextList
    scope: TextList
    non_goals: TextList
    acceptance_criteria: Annotated[list[Criterion], Field(min_length=1, max_length=50)]
    result_type: ResultType
    # Absent from JSON contracts, so their fingerprints stay as they were before text existed.
    result_format: ResultFormat = "json"
    budgets: ContractBudgets
    artifact_manifest: ArtifactManifest | None = None
    execution: ContractExecution | None = None
    workmap: WorkmapBinding | None = None

    @model_validator(mode="after")
    def artifact_task_matches(self) -> TaskContract:
        if self.artifact_manifest is not None and self.task_id != self.artifact_manifest.task_id:
            raise ValueError("artifact manifest task_id must match the retained contract")
        return self

    @model_serializer(mode="wrap")
    def omit_absent_optionals(self, handler):
        data = _omit_none(handler(self), "artifact_manifest", "execution", "workmap")
        if data.get("result_format") == "json":
            data.pop("result_format")
        return data


class Evidence(StrictModel):
    id: Identifier
    reference: Text
    description: Text


class Artifact(StrictModel):
    path: Text
    purpose: Text


EvidenceIDs = Annotated[list[Identifier], Field(max_length=100)]


class Check(StrictModel):
    name: Text
    status: Literal["passed", "failed", "not_run"]
    evidence_ids: EvidenceIDs


class GenericPayload(StrictModel):
    summary: Text
    items: TextList


class Finding(StrictModel):
    severity: Literal["critical", "high", "medium", "low", "info"]
    location: Text
    explanation: Text
    recommendation: Text
    evidence_ids: EvidenceIDs


class ReviewPayload(StrictModel):
    summary: Text
    findings: Annotated[list[Finding], Field(max_length=100)]


class Change(StrictModel):
    path: Text
    description: Text


class ImplementationPayload(StrictModel):
    summary: Text
    changes: Annotated[list[Change], Field(max_length=100)]


class Conclusion(StrictModel):
    claim: Text
    evidence_ids: EvidenceIDs


class ResearchPayload(StrictModel):
    summary: Text
    conclusions: Annotated[list[Conclusion], Field(max_length=100)]


class ResultEnvelope(StrictModel):
    protocol: Literal["agents-core/subagent/1"]
    task_id: Identifier
    attempt_id: Identifier
    bundle_revision: Revision
    contract_revision: Revision
    status: Literal["completed", "input_required", "partial", "failed"]
    evidence: Annotated[list[Evidence], Field(max_length=100)]
    artifacts: Annotated[list[Artifact], Field(max_length=100)]
    checks: Annotated[list[Check], Field(max_length=100)]
    limitations: TextList
    remaining_work: TextList
    question: Text | None


class GenericResult(ResultEnvelope):
    result: GenericPayload


class ReviewResult(ResultEnvelope):
    result: ReviewPayload


class ImplementationResult(ResultEnvelope):
    result: ImplementationPayload


class ResearchResult(ResultEnvelope):
    result: ResearchPayload


RESULT_MODELS = {"generic": GenericResult, "review": ReviewResult,
                 "implementation": ImplementationResult, "research": ResearchResult}


class Diagnostic(StrictModel):
    path: str = Field(max_length=256)
    code: str = Field(max_length=80)
    message: str = Field(max_length=240)


class Truncation(StrictModel):
    """Present only when the cap dropped diagnostics this validation had already produced.

    Validation runs in stages — the contract, then the line format, then the schema, then the
    status invariants and evidence references — and reports the first stage that failed. So the
    marker's absence states that `errors` holds every problem that stage found, which a response
    could not state before: a parent could not tell the first twenty problems from all of them,
    and fixing the twenty it saw earned a fresh set (#589). It is not a statement about the whole
    result: a later stage may still report its own problems once these are corrected.

    `produced` counts the diagnostics that stage produced, not distinct problems. One habit
    repeated down a list counts once per diagnostic it caused, so a hundred findings that each
    omit the same five fields produce 500.
    """
    produced: int = Field(ge=1)


class WorkmapReceipt(StrictModel):
    """What the work map holds for a submission; ``refused`` is the code when it was not recorded.

    A refusal leaves the result's validity alone: the result is structurally valid, the map did not
    take it. status, level and revision then describe the node as the map holds it, when it could be read.
    """
    task_id: Identifier
    node: WorkmapNodeId
    status: str | None = None
    level: str | None = None
    revision: int | None = None
    duplicate: bool = False
    refused: str | None = None

    @model_serializer(mode="wrap")
    def omit_absent_fields(self, handler):
        return _omit_none(handler(self), "status", "level", "revision", "refused")


class ValidationResponse(StrictModel):
    valid: bool
    errors: Annotated[list[Diagnostic], Field(max_length=MAX_ERRORS)]
    verification: Literal["structural_only"] = "structural_only"
    escalation: Escalation | None = None
    # SHA-256 of the exact result text (or canonical JSON of a result object) that was checked,
    # so a parent can cite the validated bytes without quoting them (#453).
    result_sha256: Revision | None = None
    truncated: Truncation | None = None
    workmap: WorkmapReceipt | None = None

    @model_serializer(mode="wrap")
    def omit_absent_escalation(self, handler):
        return _omit_none(handler(self), "escalation", "result_sha256", "truncated", "workmap")


class WorkerDescriptor(StrictModel):
    agent: AgentName
    bundle_revision: Revision
    skills_loaded: list[str]
    implants_loaded: list[str]
    rules_loaded: list[str]


# The reason a component was dropped to fit the context budget. The preparer writes it onto the
# decision and the receipt's `omitted` list is built by matching it, so the two modules join on
# this one string: it has a name here rather than a literal at each end, because a rename that
# reached only one of them would leave `omitted` permanently absent, which a parent cannot tell
# apart from a package that lost nothing.
CONTEXT_BUDGET_REASON = "context_budget"


class SelectionDecision(StrictModel):
    kind: Literal["skills", "implants", "rules"]
    id: str
    selected: bool
    reason: str


class PreparedPackage(StrictModel):
    protocol: Literal["agents-core/subagent/1"] = PROTOCOL
    status: Literal["prepared"] = "prepared"
    task_id: Identifier
    attempt_id: Identifier
    bundle_id: Identifier
    descriptor: WorkerDescriptor
    contract: TaskContract
    contract_revision: Revision
    worker_context: str
    result_schema: dict[str, Any]
    context_chars: int
    selection: list[SelectionDecision]
    execution: ExecutionPlan | None = None

    @model_serializer(mode="wrap")
    def omit_absent_execution(self, handler):
        return _omit_none(handler(self), "execution")


class PreparedReceipt(StrictModel):
    """What the parent gets when the server delivers the package as a file (the default).

    The worker reads ``context_path`` itself, and the server keeps the contract, so the parent
    holds neither: it launches the worker with ``launch`` and validates by ``contract_revision``.
    """
    protocol: Literal["agents-core/subagent/1"] = PROTOCOL
    status: Literal["prepared"] = "prepared"
    delivery: Literal["file"] = "file"
    task_id: Identifier
    attempt_id: Identifier
    contract_revision: Revision
    context_path: str
    context_chars: int
    context_sha256: Revision
    launch: str
    descriptor: WorkerDescriptor
    execution: ExecutionPlan | None = None
    # "<kind>/<id>" of every component the context budget cost this package, absent when none was
    # dropped: the receipt carries no selection list, so without it the trade is invisible.
    omitted: list[str] | None = None

    @model_serializer(mode="wrap")
    def omit_absent_execution(self, handler):
        return _omit_none(handler(self), "execution", "omitted")


class PreparationError(StrictModel):
    protocol: Literal["agents-core/subagent/1"] = PROTOCOL
    status: Literal["error"] = "error"
    errors: Annotated[list[Diagnostic], Field(max_length=MAX_ERRORS)]


class PreparationResponse(RootModel[PreparedPackage | PreparedReceipt | PreparationError]):
    # MCP output schemas must describe an object. Each alternative is closed.
    model_config = ConfigDict(json_schema_extra={"type": "object"})
