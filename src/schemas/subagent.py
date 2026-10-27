"""Closed, bounded data contracts for stateless delegated workers (not personas)."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field, RootModel,
                      StringConstraints, model_serializer, model_validator)

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

    @model_validator(mode="after")
    def artifact_task_matches(self) -> TaskContract:
        if self.artifact_manifest is not None and self.task_id != self.artifact_manifest.task_id:
            raise ValueError("artifact manifest task_id must match the retained contract")
        return self

    @model_serializer(mode="wrap")
    def omit_absent_optionals(self, handler):
        data = _omit_none(handler(self), "artifact_manifest", "execution")
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


class ValidationResponse(StrictModel):
    valid: bool
    errors: Annotated[list[Diagnostic], Field(max_length=MAX_ERRORS)]
    verification: Literal["structural_only"] = "structural_only"
    escalation: Escalation | None = None
    # SHA-256 of the exact result text (or canonical JSON of a result object) that was checked,
    # so a parent can cite the validated bytes without quoting them (#453).
    result_sha256: Revision | None = None

    @model_serializer(mode="wrap")
    def omit_absent_escalation(self, handler):
        return _omit_none(handler(self), "escalation", "result_sha256")


class WorkerDescriptor(StrictModel):
    agent: AgentName
    bundle_revision: Revision
    skills_loaded: list[str]
    implants_loaded: list[str]
    rules_loaded: list[str]


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

    @model_serializer(mode="wrap")
    def omit_absent_execution(self, handler):
        return _omit_none(handler(self), "execution")


class PreparationError(StrictModel):
    protocol: Literal["agents-core/subagent/1"] = PROTOCOL
    status: Literal["error"] = "error"
    errors: Annotated[list[Diagnostic], Field(max_length=MAX_ERRORS)]


class PreparationResponse(RootModel[PreparedPackage | PreparedReceipt | PreparationError]):
    # MCP output schemas must describe an object. Each alternative is closed.
    model_config = ConfigDict(json_schema_extra={"type": "object"})
