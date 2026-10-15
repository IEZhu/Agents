"""Pinned artifact contracts stay strict, immutable, bounded and storage-free."""
import copy
import hashlib
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from src.engine import rules, subagent
from src.engine.subagent import prepare_subagent
from src.engine.subagent_validation import contract_fingerprint, validate_result
from src.schemas.artifacts import ArtifactDescriptor, ArtifactManifest
from src.schemas.subagent import PreparedPackage
from tests.test_subagent import codes, report, request, worker_tree


def seal_manifest(value):
    value = copy.deepcopy(value)
    value.pop("sha256", None)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    value["sha256"] = hashlib.sha256(encoded).hexdigest()
    return value


def manifest(*, task_id="task-1", count=1, required=True):
    entries = []
    for index in range(count):
        sha256 = hashlib.sha256(f"Artifact {index}".encode()).hexdigest()
        entries.append({"artifact": {"artifact_id": "art_" + sha256, "sha256": sha256,
                                     "size_bytes": 10, "media_type": "text/markdown",
                                     "name": f"Reference {index}.md"},
                        "required": required, "purpose": "Inspect the original source"})
    return seal_manifest({"schema": "agents-core/artifact-manifest/1",
                          "manifest_id": "manifest_" + "1" * 32,
                          "workspace_sha256": "2" * 64, "task_id": task_id,
                          "created_at": "2026-10-15T12:00:00Z",
                          "expires_at": "2026-10-15T12:05:00Z", "entries": entries})


@pytest.fixture
def artifact_worker_tree(worker_tree):
    source = Path(__file__).resolve().parents[1] / "rules/subagents/rule-worker-artifacts.mdc"
    destination = worker_tree / "rules/subagents/rule-worker-artifacts.mdc"
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return worker_tree


def prepare_manifest(value=None, **extra):
    package = prepare_subagent(request(artifact_manifest=manifest() if value is None else value,
                                       **extra))
    assert isinstance(package, PreparedPackage), package
    return package


def test_manifest_exact_shape_canonical_unicode_hash_and_immutability():
    source = manifest()
    source["entries"][0]["purpose"] = "Read café source"
    source = seal_manifest(source)
    parsed = ArtifactManifest.model_validate(source)
    assert parsed.model_dump(mode="json", by_alias=True) == source
    assert parsed.model_dump() == source
    assert isinstance(parsed.entries, tuple)
    with pytest.raises(ValidationError):
        parsed.task_id = "changed"
    with pytest.raises(ValidationError):
        parsed.entries[0].artifact.name = "changed.md"
    with pytest.raises(ValidationError):
        parsed.entries[0].required = False
    source["entries"][0]["purpose"] = "Caller mutation"
    assert parsed.entries[0].purpose == "Read café source"


@pytest.mark.parametrize("field,value", [
    ("size_bytes", True), ("size_bytes", "10"), ("size_bytes", -1),
    ("size_bytes", 4194305), ("media_type", "application/octet-stream"),
    ("name", "../source.md"), ("name", "folder\\source.md"),
    ("name", "source\n.md"), ("name", "\u0085"), ("name", " "),
    ("name", "x" * 121), ("artifact_id", "art_" + "0" * 64),
    ("sha256", "A" * 64), ("content", "inline bodies are forbidden"),
])
def test_descriptor_rejects_unsafe_or_coerced_fields(field, value):
    descriptor = manifest()["entries"][0]["artifact"]
    descriptor[field] = value
    with pytest.raises(ValidationError):
        ArtifactDescriptor.model_validate(descriptor)


@pytest.mark.parametrize("field,value", [
    ("schema", "agents-core/artifact-manifest/2"),
    ("manifest_id", "manifest_../lookup"), ("workspace_sha256", "x" * 64),
    ("task_id", "../task"), ("created_at", "2026-10-15T12:00:00+01:00"),
    ("created_at", "2026-99-15T12:00:00Z"),
    ("expires_at", "2026-10-15T12:00:00Z"),
    ("expires_at", "2026-10-15T11:59:59Z"),
    ("entries", []), ("entries", manifest(count=33)["entries"]),
    ("extra", "forbidden"),
])
def test_manifest_rejects_invalid_identity_time_bounds_or_shape(field, value):
    source = manifest()
    source[field] = value
    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(seal_manifest(source))


@pytest.mark.parametrize("field,value", [("required", "true"), ("required", 1),
                                         ("purpose", " "), ("purpose", "x" * 1001),
                                         ("extra", "forbidden")])
def test_manifest_entry_is_strict_and_bounded(field, value):
    source = manifest()
    source["entries"][0][field] = value
    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(seal_manifest(source))


def test_manifest_rejects_duplicate_entries_and_tampering():
    source = manifest()
    source["entries"].append(copy.deepcopy(source["entries"][0]))
    with pytest.raises(ValidationError, match="unique"):
        ArtifactManifest.model_validate(seal_manifest(source))
    source = manifest()
    source["entries"][0]["purpose"] = "Altered after sealing"
    with pytest.raises(ValidationError, match="SHA256"):
        ArtifactManifest.model_validate(source)


def test_preparer_pins_exact_manifest_and_reader_calls(artifact_worker_tree):
    source = manifest(count=2)
    source["entries"][1]["required"] = False
    source = seal_manifest(source)
    package = prepare_manifest(source)
    assert package.contract.model_dump()["artifact_manifest"] == source
    assert package.model_dump(mode="json")["contract"]["artifact_manifest"] == source
    assert contract_fingerprint(package.contract.model_dump()) == package.contract_revision
    assert package.context_chars == len(package.worker_context)
    required_call = (f'artifact_read(manifest_id="{source["manifest_id"]}", '
                     f'manifest_sha256="{source["sha256"]}", '
                     f'artifact_id="{source["entries"][0]["artifact"]["artifact_id"]}", '
                     'offset=0, max_bytes=16384)')
    assert required_call in package.worker_context
    optional_call = required_call.replace(source["entries"][0]["artifact"]["artifact_id"],
                                           source["entries"][1]["artifact"]["artifact_id"])
    assert optional_call not in package.worker_context
    assert "Do not use local_generate" in package.worker_context
    assert subagent.ARTIFACT_WORKER_RULE_ID in package.descriptor.rules_loaded


def test_omitted_task_id_uses_manifest_task_and_mismatched_task_refuses(artifact_worker_tree):
    arguments = request(artifact_manifest=manifest(task_id="manifest-task"))
    arguments.pop("task_id")
    package = prepare_subagent(arguments)
    assert isinstance(package, PreparedPackage), package
    assert package.task_id == package.contract.task_id == "manifest-task"
    refused = prepare_subagent(request(artifact_manifest=manifest(task_id="other-task")))
    assert refused.status == "error"
    assert "worker_context" not in refused.model_dump()


def test_manifest_is_bound_to_retained_contract_revision_and_task(artifact_worker_tree):
    package = prepare_manifest()
    retained = package.contract.model_dump()
    retained["artifact_manifest"]["entries"][0]["purpose"] = "Another purpose"
    retained["artifact_manifest"] = seal_manifest(retained["artifact_manifest"])
    assert "contract_revision_mismatch" in codes(validate_result(retained, report(package)))
    retained = package.contract.model_dump()
    retained["artifact_manifest"]["task_id"] = "other-task"
    retained["artifact_manifest"] = seal_manifest(retained["artifact_manifest"])
    retained["contract_revision"] = contract_fingerprint(retained)
    assert not validate_result(retained, report(package)).valid


def test_artifact_rule_is_conditional_mandatory_and_revisioned(artifact_worker_tree, monkeypatch):
    plain = prepare_subagent(request())
    assert isinstance(plain, PreparedPackage)
    assert subagent.ARTIFACT_WORKER_RULE_ID not in plain.descriptor.rules_loaded
    assert "Pinned artifact reads" not in plain.worker_context
    assert "artifact_read(" not in plain.worker_context
    assert "artifact_manifest" not in plain.contract.model_dump()
    pinned = prepare_manifest()
    path = artifact_worker_tree / "rules/subagents/rule-worker-artifacts.mdc"
    path.write_text(path.read_text() + "\nChanged artifact guidance.\n")
    assert prepare_manifest().descriptor.bundle_revision != pinned.descriptor.bundle_revision
    assert prepare_subagent(request()).descriptor.bundle_revision == plain.descriptor.bundle_revision
    monkeypatch.setattr(subagent.component_toggles, "disabled",
                        lambda kind: frozenset({subagent.ARTIFACT_WORKER_RULE_ID}))
    assert subagent.ARTIFACT_WORKER_RULE_ID in prepare_manifest().descriptor.rules_loaded
    path.unlink()
    assert "worker_artifact_rule_missing" in codes(prepare_subagent(
        request(artifact_manifest=manifest())))
    assert isinstance(prepare_subagent(request()), PreparedPackage)


def test_complete_manifest_and_instructions_are_mandatory_budget_data(artifact_worker_tree):
    plain = prepare_subagent(request(max_skills=0, max_context_chars=64000))
    assert isinstance(plain, PreparedPackage)
    response = prepare_subagent(request(artifact_manifest=manifest(), max_skills=0,
                                        max_context_chars=plain.context_chars + 20))
    assert response.status == "error" and "context_budget_exceeded" in codes(response)
    assert "worker_context" not in response.model_dump()
    baseline = prepare_manifest(max_skills=0, max_context_chars=64000)
    package = prepare_manifest(max_context_chars=baseline.context_chars + 35)
    assert package.contract.artifact_manifest == baseline.contract.artifact_manifest
    assert "Pinned artifact reads" in package.worker_context
    assert package.context_chars <= package.contract.budgets.max_context_chars
    assert any(decision.reason == "context_budget" for decision in package.selection)


@pytest.mark.parametrize("common_rules_enabled", [True, False])
def test_artifact_and_cooperation_rules_remain_mandatory_together(
        artifact_worker_tree, monkeypatch, common_rules_enabled):
    mandatory = {subagent.ARTIFACT_WORKER_RULE_ID, "worker-cooperation"}
    monkeypatch.setattr(subagent.component_toggles, "disabled",
                        lambda kind: frozenset(mandatory | {"agent-cooperation"})
                        if kind == "rules" else frozenset())
    monkeypatch.setattr(rules, "RULES_ENABLED", common_rules_enabled)
    package = prepare_manifest()
    assert mandatory <= set(package.descriptor.rules_loaded)
    assert "agent-cooperation" not in package.descriptor.rules_loaded
    for name in mandatory:
        decision = next(item for item in package.selection if item.id == name)
        assert decision.selected and decision.reason == "mandatory_worker_rule"
        assert f"## Rule: {name}\n" in package.worker_context
    assert package.contract.model_dump()["artifact_manifest"] == manifest()
    assert validate_result(package.contract.model_dump(), report(package)).valid


@pytest.mark.parametrize("rule_id", ["worker-cooperation", subagent.ARTIFACT_WORKER_RULE_ID])
def test_artifact_and_cooperation_rules_cannot_be_dropped_for_combined_budget(
        artifact_worker_tree, rule_id):
    path = artifact_worker_tree / f"rules/subagents/rule-{rule_id}.mdc"
    path.write_text(path.read_text() + "\n" + "Mandatory instruction. " * 800)
    response = prepare_subagent(request(artifact_manifest=manifest()))
    assert response.status == "error" and "context_budget_exceeded" in codes(response)
    assert "worker_context" not in response.model_dump()


def test_artifact_free_contracts_keep_original_shape_and_structural_validation(artifact_worker_tree,
                                                                            monkeypatch):
    plain = prepare_subagent(request())
    assert isinstance(plain, PreparedPackage)
    old_contract = plain.contract.model_dump()
    assert "artifact_manifest" not in old_contract
    assert "artifact_manifest" not in plain.model_dump()["contract"]
    assert contract_fingerprint(old_contract) == plain.contract_revision
    pinned = prepare_manifest()

    def no_storage_io(*args, **kwargs):
        pytest.fail("Structural result validation attempted storage I/O")

    monkeypatch.setattr(Path, "open", no_storage_io)
    monkeypatch.setattr(Path, "read_bytes", no_storage_io)
    monkeypatch.setattr("builtins.open", no_storage_io)
    assert validate_result(old_contract, report(plain)).valid
    response = validate_result(pinned.contract.model_dump(), report(pinned))
    assert response.valid and response.verification == "structural_only"


def test_prepared_worker_has_scoped_output_artifact_handoff_instructions(artifact_worker_tree):
    package = prepare_manifest(max_context_chars=64000)
    assert "artifact_publish" in package.worker_context
    assert "artifact_manifest_create" in package.worker_context
    assert "artifact://<manifest_id>/<manifest_sha256>/<artifact_id>" in package.worker_context
    assert "Never replace the parent-retained input manifest" in package.worker_context
    assert "Only when the assignment scope authorizes" in package.worker_context
    assert "Keep output manifests active" in package.worker_context
