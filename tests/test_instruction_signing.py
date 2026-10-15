"""Observable key-mode OMS and exact-byte verification contract."""
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from src.instruction_signing import (Limits, Status, build_seal, fingerprint,
                                     public_key_pem, verify_bytes, verify_seal)
from src.instruction_signing.common import validate_path, validate_paths
from src.instruction_signing.oms import pae


@pytest.fixture
def key():
    return ec.generate_private_key(ec.SECP256R1())


def keys(key):
    return {fingerprint(key.public_key()): public_key_pem(key.public_key())}


def signed(key, files=None, kind="library"):
    return build_seal(files or {"common/a.md": b"hello"}, key, kind=kind,
                      root_name="original-root", origin="release" if kind == "release" else "cli",
                      signer="display only", signed_at="2026-10-15T21:00:00Z")


def mutate(seal, key, edit):
    bundle = json.loads(seal)
    statement = json.loads(base64.b64decode(bundle["dsseEnvelope"]["payload"]))
    edit(statement)
    payload = json.dumps(statement, ensure_ascii=False).encode()
    bundle["dsseEnvelope"]["payload"] = base64.b64encode(payload).decode()
    bundle["dsseEnvelope"]["signatures"][0]["sig"] = base64.b64encode(
        key.sign(pae(payload), ec.ECDSA(hashes.SHA256()))).decode()
    return json.dumps(bundle).encode()


def test_exact_five_statuses():
    assert {s.value for s in Status} == {"valid", "unsigned", "tampered", "untrusted", "invalid"}


def test_roundtrip_returns_same_checked_bytes_and_identity(key):
    data = "bounded immutable evidence".encode()
    seal = signed(key, {"common/a.md": data})
    result = verify_seal(seal, keys(key), expected_kind="library", expected_path="common/a.md")
    checked = verify_bytes(result, "common/a.md", data)
    assert checked.status == Status.VALID
    assert checked.data is data
    assert checked.fingerprint == fingerprint(key.public_key())
    assert checked.signature_verified
    assert checked.seal_kind == "library"
    assert checked.authorization_checked is False


def test_no_result_cache_can_authorize_changed_bytes(key):
    result = verify_seal(signed(key), keys(key), expected_kind="library")
    assert verify_bytes(result, "common/a.md", b"hello").status == Status.VALID
    changed = verify_bytes(result, "common/a.md", b"hellO")
    assert changed.status == Status.TAMPERED and changed.data is None
    assert verify_bytes(result, "common/a.md", None).status == Status.TAMPERED
    assert verify_bytes(result, "common/a.md", bytearray(b"hello")).status == Status.INVALID


def test_missing_uncovered_and_renamed_paths(key):
    assert verify_seal(None, keys(key)).status == Status.UNSIGNED
    result = verify_seal(signed(key), keys(key), expected_kind="library")
    for path in ("common/b.md", "repos/another/a.md"):
        assert verify_bytes(result, path, b"hello").status == Status.UNSIGNED
        assert verify_seal(signed(key), keys(key), expected_kind="library",
                           expected_path=path).status == Status.UNSIGNED


def test_unknown_key_does_not_claim_signature_checked(key):
    result = verify_seal(signed(key), {})
    assert result.status == Status.UNTRUSTED
    assert result.signature_verified is False and result.fingerprint is None


def test_bad_signature_and_wrong_key_material(key):
    seal = json.loads(signed(key))
    seal["dsseEnvelope"]["signatures"][0]["sig"] = base64.b64encode(b"bad DER").decode()
    assert verify_seal(json.dumps(seal).encode(), keys(key)).status == Status.INVALID
    other = ec.generate_private_key(ec.SECP256R1())
    assert verify_seal(signed(key), {fingerprint(key.public_key()): public_key_pem(other.public_key())}).status == Status.INVALID


@pytest.mark.parametrize("raw", [b"{}", b"null", b"[]", b"{", b'{"a":1,"a":2}', b'{"x":NaN}', b"\xff"])
def test_malformed_json_never_passes(raw, key):
    assert verify_seal(raw, keys(key)).status == Status.INVALID


@pytest.mark.parametrize("edit", [
    lambda s: s.update(_type="unknown"),
    lambda s: s.update(predicateType="unknown"),
    lambda s: s["subject"][0].update(digest={"sha256": "0" * 64}),
    lambda s: s["predicate"].update(unrecognized=True),
    lambda s: s["predicate"]["serialization"].update(allow_symlinks=True),
    lambda s: s["predicate"]["resources"][0].update(algorithm="md5"),
    lambda s: s["predicate"]["resources"][0].update(digest="not hex"),
    lambda s: s["predicate"]["resources"][0].update(name="../escape"),
    lambda s: s["predicate"]["resources"][0]["agent_instructions"].update(profile="unknown"),
    lambda s: s["predicate"]["resources"][0]["agent_instructions"].update(seal="release"),
    lambda s: s["predicate"]["resources"][0]["agent_instructions"].update(signed_at="yesterday"),
    lambda s: s["predicate"]["resources"][0]["agent_instructions"].update(origin="release"),
])
def test_signed_malformed_or_wrong_scope_statements_are_invalid(edit, key):
    result = verify_seal(mutate(signed(key), key, edit), keys(key), expected_kind="library")
    assert result.status == Status.INVALID


def test_library_cannot_cover_two_files(key):
    seal = signed(key, {"a": b"a", "b": b"b"}, kind="release")
    def edit(s):
        s["predicate"]["resources"][0]["agent_instructions"].update(seal="library", origin="cli")
    assert verify_seal(mutate(seal, key, edit), keys(key), expected_kind="library").status == Status.INVALID


def test_resource_order_duplicates_and_misplaced_extension(key):
    seal = signed(key, {"a": b"a", "b": b"b"}, kind="release")
    edits = [
        lambda s: s["predicate"]["resources"].reverse(),
        lambda s: s["predicate"]["resources"][1].update(name="a"),
        lambda s: s["predicate"]["resources"][1].update(agent_instructions=s["predicate"]["resources"][0]["agent_instructions"]),
    ]
    for edit in edits:
        assert verify_seal(mutate(seal, key, edit), keys(key), expected_kind="release").status == Status.INVALID


def test_subject_name_is_not_directory_authority(key):
    seal = mutate(signed(key), key, lambda s: s["subject"][0].update(name="a different basename"))
    assert verify_seal(seal, keys(key), expected_kind="library").status == Status.VALID


def test_display_label_does_not_authorize_release(key):
    seal = signed(key, kind="release")
    seal = mutate(seal, key, lambda s: s["predicate"]["resources"][0]["agent_instructions"].update(signer="trusted release admin"))
    result = verify_seal(seal, keys(key), expected_kind="release")
    assert result.status == Status.VALID and result.signature_verified
    assert result.fingerprint == fingerprint(key.public_key())
    assert result.authorization_checked is False


@pytest.mark.parametrize("path", ["", "/abs", "C:/drive", "//host/file", "a\\b", "a//b", "./a",
    "a/../b", "a/.", "a/", "a. ", "con.py", "LPT¹.log", "a:b", "a\x00b", "e\u0301.txt", "\ud800"])
def test_nonportable_paths_refused(path):
    with pytest.raises((ValueError, UnicodeError)):
        validate_path(path)


@pytest.mark.parametrize("paths", [["a", "a"], ["A.py", "a.py"], ["A", "a/b"], ["Src/a.py", "src/b.py"]])
def test_platform_alias_and_file_directory_collisions(paths):
    with pytest.raises(ValueError):
        validate_paths(paths)


def test_ed25519_and_other_curves_refused():
    for key in (ed25519.Ed25519PrivateKey.generate(), ec.generate_private_key(ec.SECP384R1())):
        with pytest.raises(ValueError, match="P-256"):
            signed(key)


def test_bounds(key):
    seal = signed(key)
    for limits in (Limits(seal_bytes=len(seal)-1), Limits(payload_bytes=1), Limits(resources=1, path_bytes=2)):
        assert verify_seal(seal, keys(key), limits=limits).status == Status.INVALID
    assert verify_seal(seal, keys(key), limits=Limits(seal_bytes=len(seal))).status == Status.VALID
    with pytest.raises(ValueError):
        build_seal(
            {"a": b"123", "b": b"456"}, key, kind="release", root_name="root",
            origin="release", signer="test", signed_at="2026-10-15T21:00:00Z", limits=Limits(total_bytes=5))
    result = verify_seal(seal, keys(key))
    assert verify_bytes(result, "common/a.md", b"hello", limits=Limits(file_bytes=4)).status == Status.INVALID
    assert verify_bytes(result, "common/a.md", b"hello", limits=Limits(file_bytes=5)).status == Status.VALID


@pytest.mark.parametrize("edit", [
    lambda b: b.update(messageSignature={}),
    lambda b: b["verificationMaterial"].update(tlogEntries=False),
    lambda b: b["verificationMaterial"]["publicKey"].update(extra="ambiguous"),
    lambda b: b["dsseEnvelope"].update(payload="%%%invalid"),
    lambda b: b["dsseEnvelope"].update(signatures=[]),
    lambda b: b["dsseEnvelope"]["signatures"].append(b["dsseEnvelope"]["signatures"][0]),
    lambda b: b["dsseEnvelope"]["signatures"][0].update(keyid="another key"),
])
def test_malformed_envelopes(edit, key):
    bundle = json.loads(signed(key))
    edit(bundle)
    assert verify_seal(json.dumps(bundle).encode(), keys(key)).status == Status.INVALID


def test_present_null_profile_and_local_workspace_are_invalid(key):
    for edit in [
        lambda s: s["predicate"]["resources"][0].update(agent_instructions=None),
        lambda s: s["predicate"]["resources"][0]["agent_instructions"].update(workspace_key="/private/repo"),
    ]:
        assert verify_seal(mutate(signed(key), key, edit), keys(key)).status == Status.INVALID


def test_duplicate_statement_keys_and_nonfinite_numbers(key):
    from src.instruction_signing.common import load_json
    for raw in [b'{"a":1,"a":2}', b'{"x":1e999}']:
        with pytest.raises(ValueError):
            load_json(raw, 100)


def test_runtime_import_has_no_reference_or_sigstore_dependency():
    import subprocess
    import sys
    from pathlib import Path
    result = subprocess.run([sys.executable, "-c",
        "import src.instruction_signing; import sys; "
        "assert not any(n.split('.')[0] in {'model_signing', 'sigstore'} for n in sys.modules)"],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
