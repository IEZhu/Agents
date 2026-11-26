"""Ties prepared packages to work-map nodes and records what their workers submit."""
from __future__ import annotations

import asyncio
from functools import partial

from pydantic import ValidationError

from src.artifacts import tools as artifact_tools
from src.artifacts.store import ArtifactError
from src.daemon.workspaces import WorkspaceError, resolve_client_context
from src.engine.subagent_validation import canonical_json, parsed_report
from src.schemas.subagent import PreparationRequest, ValidationResponse, WorkmapReceipt
from src.workmap.store import QUESTION_MAX, SUMMARY_MAX, WorkmapError, WorkmapStore

TEXT_MEDIA_TYPE = "text/markdown"


async def prepare(ctx, arguments: dict, node: str | None, inputs: list | None,
                  artifact_manifest: dict | None) -> tuple[str, dict | None, dict | None]:
    """Check the request, the node and its inputs; return the workspace root, the inputs' manifest and pins.

    The request's fields, workmap_node and workmap_inputs included, are validated before anything is
    published, and so are the node and its inputs; prepare_subagent's own checks (the agent profile,
    the size and the context budget) run after the inputs are published. The pins are
    {input node: the sha256 of the stored result that was published for it}.
    """
    task_id = arguments.get("task_id")
    if task_id is None:
        raise WorkmapError("invalid_request", "workmap_node and workmap_inputs need task_id, the map's task ID")
    if inputs is not None and node is None:
        raise WorkmapError("invalid_request", "workmap_inputs needs workmap_node")
    if inputs is not None and artifact_manifest is not None:
        raise WorkmapError("invalid_request", "workmap_inputs builds the artifact manifest; "
                                              "do not pass artifact_manifest too")
    if inputs is not None and not isinstance(inputs, list):
        raise WorkmapError("invalid_request", "workmap_inputs is a list of node IDs")
    PreparationRequest.model_validate({**arguments, "workmap_node": node, "workmap_inputs": inputs})
    client = await resolve_client_context(ctx, None)
    root = client.require_root()
    client.require_workspace_match()
    try:
        texts = await asyncio.to_thread(partial(WorkmapStore(root).check_preparable, task_id, node, inputs or [],
                                                result_type=arguments.get("result_type") or "generic"))
    except OSError:
        raise WorkmapError("workmap_unavailable", "The work map could not be read; try again.") from None
    manifest, pins = await _pin(ctx, task_id, texts) if texts else (None, None)
    return str(root), manifest, pins


async def _pin(ctx, task_id: str, texts: list[tuple[str, str]]) -> tuple[dict, dict]:
    """Publish each input node's stored result and pin them in one manifest for the task."""
    owned, service = False, None
    try:
        client, service, owned = await artifact_tools.resolve_service(ctx)
        entries, pins = [], {}
        for node_id, text in texts:
            published = _data(await service.call("artifact_publish", client, {
                "content": text, "name": f"workmap-{node_id}.md", "media_type": TEXT_MEDIA_TYPE}))
            entries.append({"artifact_id": published["artifact_id"], "sha256": published["sha256"],
                            "required": True, "purpose": f"Stored result of work-map node {node_id}"})
            pins[node_id] = published["sha256"]
        created = _data(await service.call("artifact_manifest_create", client,
                                           {"task_id": task_id, "entries": entries}))
        return await service.verify_snapshot(client, created), pins
    except (ArtifactError, WorkspaceError):
        raise
    except (OSError, ValueError, ValidationError):
        raise ArtifactError("artifact_unavailable", "The artifact store could not pin the inputs.") from None
    finally:
        if owned and service is not None:
            await service.close()


def _data(answer: dict) -> dict:
    if answer.get("status") != "ok":
        error = answer.get("error") or {}
        raise ArtifactError(error.get("code", "artifact_unavailable"),
                            error.get("message", "Artifact storage is unavailable."))
    return answer["data"]


def _escalation(response: ValidationResponse) -> dict | None:
    """The escalation the validation named, as the map keeps it: the complexity a new attempt
    passes, the tier it gets and why."""
    escalation = response.escalation
    return ({"complexity": escalation.complexity, "tier": escalation.tier, "reason": escalation.reason}
            if escalation is not None else None)


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def outcome_of(report, escalation: dict | None = None) -> dict:
    """What a valid result tells the map: status, level, summary and counts, never the text.

    Besides the node's verdict it keeps the worker's own status, its question and the escalation the
    validation named, so the parent sees why a node needs attention without reading the result.
    """
    checks = {"passed": 0, "failed": 0, "not_run": 0}
    for check in report.checks:
        checks[check.status] += 1
    if report.status == "completed" and checks["failed"] == 0:
        status, level = "validated", "V2" if checks["passed"] else "V1"
    else:
        status, level = "needs_attention", "V0"
    findings = None
    if hasattr(report.result, "findings"):
        findings = {}
        for finding in report.result.findings:
            findings[finding.severity] = findings.get(finding.severity, 0) + 1
    return {"status": status, "level": level, "summary": _cut(report.result.summary, SUMMARY_MAX),
            "checks": checks, "findings": findings, "limitations": len(report.limitations),
            "result_status": report.status,
            "question": _cut(report.question, QUESTION_MAX) if report.question else None,
            "escalation": escalation}


def record_submission(contract: dict, result, response: ValidationResponse, *, handback: str) -> ValidationResponse:
    """Record a submission against a contract the server stored; a refusal leaves the result's validity alone."""
    binding = contract["workmap"]
    store = WorkmapStore(binding["root"])
    try:
        if not response.valid:
            store.record_invalid(contract["task_id"], binding["node"], contract_revision=contract["contract_revision"],
                                 codes=[error.code for error in response.errors],
                                 escalation=_escalation(response))
            return response
        text = result if isinstance(result, str) else canonical_json(result)
        receipt = store.record_result(contract["task_id"], binding["node"],
                                      contract_revision=contract["contract_revision"],
                                      attempt_id=contract["attempt_id"], text=text, sha256=response.result_sha256,
                                      outcome=outcome_of(parsed_report(contract, result), _escalation(response)),
                                      handback=handback)
    except WorkmapError as error:
        return _refused(response, contract, store, error.code)
    except OSError:
        return _refused(response, contract, store, "workmap_unavailable")
    return response.model_copy(update={"workmap": WorkmapReceipt(**receipt)})


def _refused(response: ValidationResponse, contract: dict, store: WorkmapStore, code: str) -> ValidationResponse:
    """A valid result the map did not take stays valid; the receipt names the refusal and the node's state."""
    if not response.valid:
        return response
    held: dict = {}
    try:
        workmap = store.load(contract["task_id"])
        node = workmap.nodes.get(contract["workmap"]["node"]) if workmap is not None else None
        if node is not None:
            held = {"status": node["status"], "level": node["level"], "revision": workmap.revision}
    except (OSError, WorkmapError):
        pass
    return response.model_copy(update={"workmap": WorkmapReceipt(
        task_id=contract["task_id"], node=contract["workmap"]["node"], refused=code, **held)})
