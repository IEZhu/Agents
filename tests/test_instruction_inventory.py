"""Protected coverage binds immutable bytes, all paths and portable metadata."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from src.instruction_signing.common import Limits, Status
from src.instruction_signing.inventory import (INVENTORY_PATH, INVENTORY_SCHEMA,
    ObservedFile, verify_protected_set)
from src.instruction_signing import inventory as inventory_module
from src.instruction_signing.oms import (build_seal, fingerprint, public_key_pem,
                                        verify_seal)


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


@pytest.fixture
def protected():
    content = {"src/runtime.py": b"runtime = 1\n", "scripts/start": b"#!/bin/sh\n",
               "pyproject.toml": b"[project]\n", "agents/default.md": b"instructions\n"}
    paths = sorted([INVENTORY_PATH, *content])
    inventory = {"schema": INVENTORY_SCHEMA, "protected_roots": ["agents", "scripts", "src"],
                 "files": [{"path": path, "class": (
                     "runtime" if path.startswith(("src/", "scripts/")) else
                     "instruction" if path.startswith("agents/") else "configuration"),
                     "type": "regular", "executable": path == "scripts/start"} for path in paths],
                 "exclusions": [{"path": "src/cache", "type": "directory", "reason": "Generated cache",
                                 "load_isolation": "outside-load-path"}],
                 "dependencies": {"environment": {"boundary": "external", "identity": "a" * 64},
                                  "artifacts": [{"path": "packages/dependency.whl", "sha256": "b" * 64}]}}
    key = ec.generate_private_key(ec.SECP256R1())

    def make(*, metadata=None, files=None, inventory_bytes=None, seal_files=None,
             observations=None, **options):
        metadata = deepcopy(inventory if metadata is None else metadata)
        actual = dict(content if files is None else files)
        raw = _encode(metadata) if inventory_bytes is None else inventory_bytes
        actual[INVENTORY_PATH] = raw
        signed = actual if seal_files is None else seal_files
        bundle = build_seal(signed, key, kind="release", root_name="installation", origin="release",
                            signer="fixture", signed_at="2026-10-15T00:00:00Z")
        seal = verify_seal(bundle, {fingerprint(key.public_key()): public_key_pem(key.public_key())},
                           expected_kind="release")
        records = tuple(ObservedFile(path, data, "regular", path == "scripts/start")
                        for path, data in sorted(actual.items())) if observations is None else observations
        kwargs = dict(enumeration_complete=True, minimum_paths=("src/runtime.py",), minimum_roots=("src",))
        kwargs.update(options)
        return verify_protected_set(seal, raw, records, **kwargs), seal, raw, records

    return inventory, content, make


def _reasons(result):
    return {(issue.reason, issue.path) for issue in result.issues}


def _verify(seal, raw, records, **kwargs):
    options = dict(enumeration_complete=True, minimum_paths=("src/runtime.py",), minimum_roots=("src",))
    options.update(kwargs)
    return verify_protected_set(seal, raw, records, **options)


def test_complete_snapshot_authenticates_exact_immutable_inventory_and_authorizes_nothing(protected):
    _, _, make = protected
    result, seal, raw, records = make()
    assert result.status is Status.VALID
    assert result.issues == () and result.seal.data is raw
    assert result.files is records
    assert result.inventory_digest == hashlib.sha256(raw).hexdigest()
    assert result.seal.fingerprint == seal.fingerprint
    assert result.seal.signature_verified is True
    assert result.authorization_checked is False
    assert result.enumeration_complete is True
    with pytest.raises(FrozenInstanceError):
        records[0].path = "different"
    with pytest.raises(FrozenInstanceError):
        result.status = Status.UNTRUSTED


@pytest.mark.parametrize("path", ["src/runtime.py", "scripts/start", "pyproject.toml", "agents/default.md"])
def test_changed_bytes_in_each_class_are_tampered(protected, path):
    _, _, make = protected
    _, seal, raw, records = make()
    changed = tuple(replace(record, data=record.data + b"!") if record.path == path else record for record in records)
    result = _verify(seal, raw, changed)
    assert result.status is Status.TAMPERED
    assert ("digest_mismatch", path) in _reasons(result)


@pytest.mark.parametrize("addition", ["src/untracked.py", "src/untracked.pyc", "src/native.so", "src/sitecustomize.py"])
def test_every_addition_is_unsigned_even_with_favorable_sealed_subset(protected, addition):
    _, _, make = protected
    _, seal, raw, records = make()
    result = _verify(seal, raw, records + (ObservedFile(addition, b"new", "regular", False),))
    assert result.status is Status.UNSIGNED
    assert ("observed_path_not_covered", addition) in _reasons(result)


@pytest.mark.parametrize("explicit_missing", [True, False])
def test_missing_signed_file_cannot_be_omitted_from_comparison(protected, explicit_missing):
    _, _, make = protected
    _, seal, raw, records = make()
    changed = tuple(replace(record, data=None) if record.path == "src/runtime.py" else record
                    for record in records if explicit_missing or record.path != "src/runtime.py")
    result = _verify(seal, raw, changed)
    assert result.status is Status.TAMPERED
    assert ("file_missing", "src/runtime.py") in _reasons(result)


def test_rename_preserves_both_missing_and_unsigned_diagnostics(protected):
    _, _, make = protected
    _, seal, raw, records = make()
    changed = tuple(replace(record, path="src/renamed.py") if record.path == "src/runtime.py" else record
                    for record in records)
    result = _verify(seal, raw, changed)
    assert result.status is Status.TAMPERED
    assert {("file_missing", "src/runtime.py"), ("observed_path_not_covered", "src/renamed.py")} <= _reasons(result)


def test_metadata_digest_missing_and_addition_failures_all_survive(protected):
    _, _, make = protected
    _, seal, raw, records = make()
    changed = tuple(replace(record, data=b"changed", executable=False) if record.path == "scripts/start" else record
                    for record in records if record.path != "src/runtime.py")
    result = _verify(seal, raw, changed + (ObservedFile("src/new.py", b"new", "regular", False),))
    assert result.status is Status.TAMPERED
    assert {("digest_mismatch", "scripts/start"), ("executable_mismatch", "scripts/start"),
            ("file_missing", "src/runtime.py"), ("observed_path_not_covered", "src/new.py")} <= _reasons(result)


def test_inventory_bytes_must_authenticate_before_schema_can_be_used(protected):
    _, _, make = protected
    _, seal, raw, records = make()
    changed = b"{bad JSON"
    result = _verify(seal, changed, records)
    assert result.status is Status.TAMPERED
    assert ("digest_mismatch", INVENTORY_PATH) in _reasons(result)
    assert ("inventory_invalid", INVENTORY_PATH) not in _reasons(result)
    assert result.seal.data is None
    missing = _verify(seal, None, records)
    assert missing.status is Status.TAMPERED


def test_inventory_observation_cannot_substitute_for_authenticated_parameter(protected):
    _, _, make = protected
    _, seal, raw, records = make()
    changed = tuple(replace(r, data=b"other") if r.path == INVENTORY_PATH else r for r in records)
    result = _verify(seal, raw, changed)
    assert result.status is Status.TAMPERED
    assert ("inventory_observation_mismatch", INVENTORY_PATH) in _reasons(result)


@pytest.mark.parametrize("complete", [False, None, 1, "true"])
def test_enumeration_must_be_explicitly_complete(protected, complete):
    _, _, make = protected
    result, _, _, _ = make(enumeration_complete=complete)
    assert result.status is Status.INVALID
    assert ("enumeration_incomplete", None) in _reasons(result)


def test_minimum_scope_is_independent_of_signed_inventory(protected):
    inventory, content, make = protected
    reduced = deepcopy(inventory)
    reduced["files"] = [e for e in reduced["files"] if e["path"] != "src/runtime.py"]
    reduced["protected_roots"].remove("src")
    result, _, _, _ = make(metadata=reduced, files={p: b for p, b in content.items() if p != "src/runtime.py"})
    assert result.status is Status.UNSIGNED
    assert {("minimum_path_not_covered", "src/runtime.py"), ("minimum_root_not_covered", "src")} <= _reasons(result)


@pytest.mark.parametrize("field", ["minimum_paths", "minimum_roots"])
@pytest.mark.parametrize("value", [(), [], ("../escape",), ("src", "SRC")])
def test_caller_minima_must_be_explicit_bounded_unambiguous_tuples(protected, field, value):
    _, _, make = protected
    result, _, _, _ = make(**{field: value})
    assert result.status is Status.INVALID
    assert ("minimum_scope_invalid", None) in _reasons(result)


def test_missing_inventory_resource_is_unsigned_even_if_other_signed_files_verify(protected):
    _, content, make = protected
    result, _, _, _ = make(seal_files=content)
    assert result.status is Status.UNSIGNED
    assert ("path_not_covered", INVENTORY_PATH) in _reasons(result)


def test_inventory_must_cover_entire_protected_release_resource_set(protected):
    inventory, content, make = protected
    result, _, _, _ = make(files={**content, "src/unlisted.py": b"signed, unlisted"})
    assert result.status is Status.INVALID
    assert ("signed_path_not_in_inventory", "src/unlisted.py") in _reasons(result)
    expanded = deepcopy(inventory)
    expanded["files"].append({"path": "src/unsigned.py", "class": "runtime", "type": "regular", "executable": False})
    expanded["files"].sort(key=lambda e: e["path"])
    result, _, _, _ = make(metadata=expanded)
    assert result.status is Status.TAMPERED
    assert {("inventory_path_not_signed", "src/unsigned.py"), ("file_missing", "src/unsigned.py")} <= _reasons(result)


def test_release_documentation_outside_protected_roots_need_not_be_inventoried_or_observed(protected):
    _, content, make = protected
    result, seal, raw, records = make(files={**content, "README.md": b"release documentation"})
    assert result.status is Status.VALID
    without_docs = tuple(r for r in records if r.path != "README.md")
    assert _verify(seal, raw, without_docs).status is Status.VALID
    changed_docs = tuple(replace(r, data=b"changed") if r.path == "README.md" else r for r in records)
    assert _verify(seal, raw, changed_docs).status is Status.TAMPERED


def test_signed_resource_in_explicit_isolated_exclusion_need_not_be_inventoried(protected):
    _, content, make = protected
    result, seal, raw, records = make(files={**content, "src/cache/generated.pyc": b"signed cache"})
    assert result.status is Status.VALID
    without_cache = tuple(r for r in records if r.path != "src/cache/generated.pyc")
    assert _verify(seal, raw, without_cache).status is Status.VALID


def test_signed_directory_alias_is_refused_before_inventory_comparison(protected):
    _, content, make = protected
    with pytest.raises(ValueError, match="aliasing"):
        make(files={**content, "SRC/unlisted.py": b"directory alias"})


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(schema="agents-core/protected-files/2"),
    lambda v: v.update(extra=True),
    lambda v: v.pop("dependencies"),
    lambda v: v.update(files=list(reversed(v["files"]))),
    lambda v: v["files"].append(v["files"][-1]),
    lambda v: v.update(files=[e for e in v["files"] if e["path"] != INVENTORY_PATH]),
    lambda v: v["files"][0].update(executable=1),
    lambda v: v["files"][0].pop("executable"),
    lambda v: v["files"][0].update(path="agents/../escape"),
    lambda v: v["files"][-1].update(path="SRC/runtime.py"),
    lambda v: v["files"][0].update(type="symlink"),
    lambda v: v["files"][0].update(**{"class": "unknown"}),
    lambda v: v["files"][0].update(extra="ignored"),
    lambda v: v.update(protected_roots=["src", "src/nested"]),
    lambda v: v["dependencies"]["environment"].update(boundary="implicit-site-packages"),
    lambda v: v["dependencies"]["environment"].update(identity="not-content-identity"),
    lambda v: v["dependencies"].update(artifacts=[]),
    lambda v: v["dependencies"]["artifacts"][0].update(sha256="A" * 64),
    lambda v: v["exclusions"][0].update(reason=""),
    lambda v: v["exclusions"][0].update(path="src/*.py"),
    lambda v: v["exclusions"][0].update(load_isolation="allow-imports"),
    lambda v: v["exclusions"][0].update(path="src"),
    lambda v: v["exclusions"][0].update(path="src/runtime.py", type="regular"),
])
def test_strict_finite_inventory_schema(protected, mutation):
    inventory, _, make = protected
    candidate = deepcopy(inventory)
    mutation(candidate)
    result, _, _, _ = make(metadata=candidate)
    assert result.status is Status.INVALID
    assert ("inventory_invalid", INVENTORY_PATH) in _reasons(result)


@pytest.mark.parametrize("bad_json", [b'{"schema":1,"schema":2}', b'{"bad":NaN}', b'\xff', b'[]'])
def test_signed_malformed_json_is_invalid(protected, bad_json):
    _, _, make = protected
    result, _, _, _ = make(inventory_bytes=bad_json)
    assert result.status is Status.INVALID
    assert ("inventory_invalid", INVENTORY_PATH) in _reasons(result)


@pytest.mark.parametrize("path", ["/absolute", "C:/drive", "//server/share", "src/../escape", "src/./file",
                                "src//file", "src\\file", "src/file.", "src/file ", "src/NUL.py",
                                "src/file:stream", "src/e\u0301.py", "src/\ud800.py"])
def test_unsafe_observed_paths_are_invalid(protected, path):
    _, _, make = protected
    _, seal, raw, records = make()
    result = _verify(seal, raw, records + (ObservedFile(path, b"new", "regular", False),))
    assert result.status is Status.INVALID
    assert ("observed_path_invalid", path) in _reasons(result)


@pytest.mark.parametrize("path", ["src/RUNTIME.py", "SRC/new.py", "src/runtime.py/child"])
def test_observed_aliases_and_file_directory_substitutions_are_invalid(protected, path):
    _, _, make = protected
    _, seal, raw, records = make()
    result = _verify(seal, raw, records + (ObservedFile(path, b"new", "regular", False),))
    assert result.status is Status.INVALID


@pytest.mark.parametrize("file_type", ["directory", "symlink", "reparse", "socket", "fifo", "device", "unknown"])
def test_only_regular_file_type_supported(protected, file_type):
    _, _, make = protected
    _, seal, raw, records = make()
    changed = tuple(replace(r, file_type=file_type) if r.path == "src/runtime.py" else r for r in records)
    result = _verify(seal, raw, changed)
    assert result.status is Status.INVALID
    assert ("unsupported_file_type", "src/runtime.py") in _reasons(result)


@pytest.mark.parametrize("field", ["symlink_components", "reparse_components"])
@pytest.mark.parametrize("components", [("src",), ("src/runtime.py",), ("other",), ("../escape",), ("src", "src")])
def test_link_or_reparse_in_any_component_cannot_load(protected, field, components):
    _, _, make = protected
    _, seal, raw, records = make()
    changed = tuple(replace(r, **{field: components}) if r.path == "src/runtime.py" else r for r in records)
    result = _verify(seal, raw, changed)
    assert result.status is Status.INVALID


def test_windows_ignores_posix_policy_only_with_explicit_absent_posix_observation(protected):
    _, _, make = protected
    _, seal, raw, records = make()
    windows = tuple(replace(r, executable=None) for r in records)
    assert _verify(seal, raw, windows, platform="windows").status is Status.VALID
    assert _verify(seal, raw, records, platform="windows").status is Status.INVALID
    assert _verify(seal, raw, windows, platform="posix").status is Status.INVALID
    assert _verify(seal, raw, windows, platform="unknown").status is Status.INVALID


def test_posix_executable_change_alone_is_tampered(protected):
    _, _, make = protected
    _, seal, raw, records = make()
    changed = tuple(replace(r, executable=False) if r.path == "scripts/start" else r for r in records)
    result = _verify(seal, raw, changed)
    assert result.status is Status.TAMPERED
    assert result.issues == (result.issues[0],)
    assert ("executable_mismatch", "scripts/start") in _reasons(result)


def test_explicit_isolated_directory_exclusion_is_not_an_implicit_glob(protected):
    _, _, make = protected
    _, seal, raw, records = make()
    isolated = records + (ObservedFile("src/cache/generated.pyc", b"cache", "regular", False),)
    assert _verify(seal, raw, isolated).status is Status.VALID
    outside = isolated + (ObservedFile("src/cache-other/extra.py", b"new", "regular", False),)
    assert _verify(seal, raw, outside).status is Status.UNSIGNED
    unsafe = isolated[:-1] + (replace(isolated[-1], reparse_components=("src/cache",)),)
    assert _verify(seal, raw, unsafe).status is Status.INVALID


def test_dependency_identity_and_environment_boundary_are_not_signer_authority(protected):
    inventory, content, make = protected
    metadata = deepcopy(inventory)
    dependency = b"wheel contents"
    metadata["dependencies"] = {"environment": {"boundary": "protected-set", "identity": "c" * 64},
        "artifacts": [{"path": "src/dependency.whl", "sha256": hashlib.sha256(dependency).hexdigest()}]}
    metadata["files"].append({"path": "src/dependency.whl", "class": "dependency", "type": "regular", "executable": False})
    metadata["files"].sort(key=lambda e: e["path"])
    files = {**content, "src/dependency.whl": dependency}
    result, _, _, _ = make(metadata=metadata, files=files)
    assert result.status is Status.VALID and result.authorization_checked is False
    metadata["dependencies"]["artifacts"][0]["sha256"] = "f" * 64
    result, _, _, _ = make(metadata=metadata, files=files)
    assert result.status is Status.INVALID
    assert ("dependency_identity_mismatch", "src/dependency.whl") in _reasons(result)
    result, _, _, _ = make(metadata=metadata)
    assert result.status is Status.TAMPERED


@pytest.mark.parametrize("mutation", [dict(profile=None, seal_kind=None), dict(seal_kind="library"),
                                      dict(signature_verified=False)])
def test_generic_or_library_profile_and_unverified_results_cannot_establish_release_coverage(protected, mutation):
    _, _, make = protected
    _, seal, raw, records = make()
    result = _verify(replace(seal, **mutation), raw, records)
    assert result.status is Status.INVALID
    assert ("release_profile_required", INVENTORY_PATH) in _reasons(result)


@pytest.mark.parametrize("status", [Status.UNSIGNED, Status.UNTRUSTED, Status.INVALID])
def test_prior_seal_failure_and_unchecked_authority_are_preserved(protected, status):
    _, _, make = protected
    _, seal, raw, records = make()
    result = _verify(replace(seal, status=status, reason="prior_failure", signature_verified=False), raw, records)
    assert result.status is status
    assert result.authorization_checked is False
    assert ("prior_failure", INVENTORY_PATH) in _reasons(result)


def test_resource_count_file_total_path_and_json_bounds_are_enforced(protected):
    _, _, make = protected
    _, seal, raw, records = make()
    assert _verify(seal, raw, records, limits=Limits(resources=1)).status is Status.INVALID
    assert _verify(seal, raw, records, limits=Limits(file_bytes=len(raw) - 1)).status is Status.INVALID
    assert _verify(seal, raw, records, limits=Limits(total_bytes=sum(len(r.data) for r in records) - 1)).status is Status.INVALID
    assert _verify(seal, raw, records, limits=Limits(path_bytes=10)).status is Status.INVALID
    assert _verify(seal, raw, records, limits=Limits(payload_bytes=len(raw) - 1)).status is Status.INVALID


def test_aggregate_byte_bound_refuses_before_hashing_observation_payloads(protected, monkeypatch):
    _, _, make = protected
    _, seal, raw, records = make()
    actual = inventory_module.verify_bytes
    calls = []

    def bounded_verify(seal_result, path, data, **options):
        calls.append(path)
        assert path == INVENTORY_PATH and len(calls) == 1
        return actual(seal_result, path, data, **options)

    monkeypatch.setattr(inventory_module, "verify_bytes", bounded_verify)
    result = _verify(seal, raw, records, limits=Limits(total_bytes=1))
    assert result.status is Status.INVALID
    assert ("total_size_limit", None) in _reasons(result)
    assert calls == [INVENTORY_PATH]


def test_component_count_bound_is_call_wide(protected):
    _, _, make = protected
    _, seal, raw, records = make()
    maximum = len(records)
    changed = tuple(replace(r, symlink_components=(r.path,), reparse_components=(r.path,)) for r in records)
    result = _verify(seal, raw, changed, limits=Limits(resources=maximum))
    assert result.status is Status.INVALID
    assert any(reason == "component_count_limit" for reason, _ in _reasons(result))


def test_observation_container_and_fields_are_immutable_and_strict(protected):
    _, _, make = protected
    _, seal, raw, records = make()
    assert _verify(seal, raw, list(records)).status is Status.INVALID
    assert _verify(seal, raw, records + (object(),)).status is Status.INVALID
    assert _verify(seal, raw, records + (records[0],)).status is Status.INVALID
    for kwargs in (dict(data=bytearray(b"mutable")), dict(symlink_components=[]), dict(executable=1)):
        values = dict(path="src/new.py", data=b"immutable", file_type="regular", executable=False)
        values.update(kwargs)
        with pytest.raises(TypeError):
            ObservedFile(**values)
