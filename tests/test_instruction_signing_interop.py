"""Pinned reference implementation; key mode only, with no network or enrollment."""
import importlib.metadata
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

pytest.importorskip("model_signing")

from src.instruction_signing import Status, build_seal, fingerprint, public_key_pem, verify_bytes, verify_seal


@pytest.fixture
def reference(tmp_path):
    assert importlib.metadata.version("model-signing") == "1.1.1"
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.md").write_bytes(b"one")
    (root / "z\u00e9.md").write_bytes("Unicode content \u03bb".encode())
    key = ec.generate_private_key(ec.SECP256R1())
    private, public = tmp_path / "key.pem", tmp_path / "key.pub"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    public.write_bytes(public_key_pem(key.public_key()))
    return root, key, private, public, tmp_path / "seal.json"


def run_reference(*args):
    return subprocess.run([sys.executable, "-m", "model_signing", *map(str, args)],
                          capture_output=True, text=True, timeout=30)


def test_our_profile_seal_verified_by_reference_cli(reference):
    root, key, private, public, seal = reference
    files = {p.name: p.read_bytes() for p in root.iterdir()}
    seal.write_bytes(build_seal(files, key, kind="release", root_name=root.name,
                               origin="release", signer="test", signed_at="2026-10-15T21:00:00Z"))
    result = run_reference("verify", "key", root, "--signature", seal, "--public_key", public, "--ignore_unsigned_files")
    assert result.returncode == 0, result.stdout + result.stderr
    (root / "a.md").write_bytes(b"onE")
    result = run_reference("verify", "key", root, "--signature", seal, "--public_key", public, "--ignore_unsigned_files")
    assert result.returncode != 0


def test_reference_cli_seal_verified_as_generic_oms(reference):
    root, key, private, public, seal = reference
    result = run_reference("sign", "key", root, "--signature", seal, "--private_key", private)
    assert result.returncode == 0, result.stdout + result.stderr
    keys = {fingerprint(key.public_key()): public.read_bytes()}
    verified = verify_seal(seal.read_bytes(), keys)
    assert verified.status == Status.VALID
    assert verified.profile is None and verified.seal_kind is None
    assert not verified.authorization_checked
    assert verify_bytes(verified, "a.md", b"one").status == Status.VALID
    assert verify_seal(seal.read_bytes(), keys, expected_kind="release").status == Status.INVALID


def test_reference_signer_preserves_first_resource_profile(reference):
    # The pinned CLI has no profile-extension option. Add it to the reference
    # payload BEFORE the reference signer signs it; never rewrite signed bytes.
    from model_signing import hashing
    from model_signing._signing import sign_ec_key, signing

    root, key, private, public, seal = reference
    payload = signing.Payload(hashing.hash(root))
    payload.statement.pb.predicate["resources"][0]["agent_instructions"] = {
        "profile": "agent-instructions/v1", "seal": "release", "origin": "release",
        "signer": "reference", "signed_at": "2026-10-15T21:00:00Z"}
    sign_ec_key.Signer(private).sign(payload).write(seal)
    verified = verify_seal(seal.read_bytes(), {fingerprint(key.public_key()): public.read_bytes()},
                           expected_kind="release")
    assert verified.status == Status.VALID
    assert verified.seal_kind == "release" and verified.signature_verified
    assert verify_bytes(verified, "a.md", b"one").status == Status.VALID
    assert verify_bytes(verified, "a.md", b"onE").status == Status.TAMPERED
