"""OMS 1.0 key-mode seals. No filesystem reads, trust mutation or result cache."""
from __future__ import annotations

import base64
import binascii
from bisect import bisect_left
import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime
from typing import Mapping

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .common import (DEFAULT_LIMITS, HEX_DIGEST, Limits, Resource, SealResult,
                     Status, load_json, text, validate_path, validate_paths)

BUNDLE_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://model_signing/signature/v1.0"
PROFILE = "agent-instructions/v1"


def public_key_pem(key) -> bytes:
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Only ECDSA P-256 keys are supported")
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def fingerprint(key) -> str:
    return hashlib.sha256(public_key_pem(key)).hexdigest()


def pae(payload: bytes) -> bytes:
    kind = PAYLOAD_TYPE.encode("utf-8")
    return b"DSSEv1 " + str(len(kind)).encode() + b" " + kind + b" " + str(len(payload)).encode() + b" " + payload


def _profile(value: object, kind: str | None, count: int) -> dict:
    if not isinstance(value, dict) or value.get("profile") != PROFILE:
        raise ValueError("Unknown or missing agent-instructions profile")
    seal_kind = value.get("seal")
    if seal_kind not in {"release", "library"} or (kind is not None and seal_kind != kind):
        raise ValueError("Seal kind does not match expected root kind")
    if seal_kind == "library" and count != 1:
        raise ValueError("Library seals require exactly one resource")
    origin = value.get("origin")
    if ((seal_kind == "release" and origin != "release") or
        (seal_kind == "library" and origin not in {"mcp", "ui", "cli", "sync-merge", "adopt"})):
        raise ValueError("Invalid origin for seal kind")
    text(value.get("signer"), maximum=256)
    stamp = text(value.get("signed_at"), maximum=64)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", stamp):
        raise ValueError("signed_at must be RFC 3339 UTC")
    datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if origin == "mcp" or "workspace_key" in value:
        workspace = text(value.get("workspace_key"), maximum=256)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", workspace):
            raise ValueError("workspace_key must be a repository key, not a local path")
    return value


def _resources(statement, expected_kind: str | None, limits: Limits):
    if not isinstance(statement, dict) or statement.get("_type") != STATEMENT_TYPE or statement.get("predicateType") != PREDICATE_TYPE:
        raise ValueError("Unsupported in-toto statement or OMS predicate")
    subjects = statement["subject"]
    if not isinstance(subjects, list) or len(subjects) != 1:
        raise ValueError("Expected exactly one subject")
    text(subjects[0]["name"], maximum=limits.path_bytes)
    predicate = statement["predicate"]
    if not isinstance(predicate, dict) or set(predicate) != {"serialization", "resources"}:
        raise ValueError("Unsupported OMS predicate fields")
    config = predicate["serialization"]
    if (not isinstance(config, dict) or config.get("method") != "files" or
        config.get("hash_type") != "sha256" or config.get("allow_symlinks") is not False or
        set(config) - {"method", "hash_type", "allow_symlinks", "ignore_paths"}):
        raise ValueError("Unsupported resource serialization")
    ignored = config.get("ignore_paths", [])
    if not isinstance(ignored, list):
        raise ValueError("ignore_paths must be an explicit path list")
    validate_paths(ignored, limits)
    entries = predicate["resources"]
    if not isinstance(entries, list) or not entries or len(entries) > limits.resources:
        raise ValueError("Expected a bounded nonempty resource list")
    paths = validate_paths([item["name"] for item in entries], limits)
    if list(paths) != sorted(paths):
        raise ValueError("Resources must be sorted by name")
    resources = []
    for item in entries:
        digest = item["digest"]
        if item.get("algorithm") != "sha256" or not isinstance(digest, str) or not HEX_DIGEST.fullmatch(digest):
            raise ValueError("Expected lowercase SHA-256 resource digest")
        resources.append(Resource(item["name"], digest))
    root = hashlib.sha256(b"".join(bytes.fromhex(r.digest) for r in resources)).hexdigest()
    if subjects[0]["digest"] != {"sha256": root}:
        raise ValueError("Subject digest does not match resources")
    if any("agent_instructions" in entry for entry in entries[1:]):
        raise ValueError("Profile extension must appear only on first resource")
    extension = entries[0].get("agent_instructions")
    if "agent_instructions" in entries[0] or expected_kind is not None:
        extension = _profile(extension, expected_kind, len(resources))
    return tuple(resources), extension


def _decode(value: object, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
        raise ValueError("Encoded input exceeds limit")
    data = base64.b64decode(value, validate=True)
    if len(data) > limit:
        raise ValueError("Decoded input exceeds limit")
    return data


def verify_seal(seal: bytes | None, public_keys: Mapping[str, bytes], *,
                expected_kind: str | None = None, expected_path: str | None = None,
                limits: Limits = DEFAULT_LIMITS) -> SealResult:
    """Verify recognized-key signatures; the caller separately authorizes kind/root.

    Without expected_kind, plain OMS seals are supported and have profile/kind
    None. Such results cannot establish protected-inventory validity.
    """
    signer = None
    signature_verified = False
    try:
        if expected_kind not in {None, "release", "library"}:
            raise ValueError("Unsupported expected kind")
        if expected_path is not None:
            validate_path(expected_path, limits)
        if seal is None:
            return SealResult(Status.UNSIGNED, "seal_missing")
        bundle = load_json(seal, limits.seal_bytes)
        if set(bundle) != {"mediaType", "verificationMaterial", "dsseEnvelope"} or bundle["mediaType"] != BUNDLE_TYPE:
            raise ValueError("Unsupported bundle media type")
        material = bundle["verificationMaterial"]
        if (set(material) - {"publicKey", "tlogEntries"} or material.get("tlogEntries", []) != [] or
            set(material["publicKey"]) != {"hint"}):
            raise ValueError("Only bare public-key bundles without transparency entries are supported")
        hint = material["publicKey"]["hint"]
        if not isinstance(hint, str) or not HEX_DIGEST.fullmatch(hint):
            raise ValueError("Invalid public-key hint")
        envelope = bundle["dsseEnvelope"]
        if set(envelope) != {"payloadType", "payload", "signatures"} or envelope["payloadType"] != PAYLOAD_TYPE:
            raise ValueError("Unsupported DSSE payload type")
        signatures = envelope["signatures"]
        if (not isinstance(signatures, list) or len(signatures) != 1 or
            set(signatures[0]) - {"sig", "keyid"} or signatures[0].get("keyid", "") != ""):
            raise ValueError("Expected one bare-key DSSE signature")
        payload = _decode(envelope["payload"], limits.payload_bytes)
        signature = _decode(signatures[0]["sig"], 256)
        if hint not in public_keys:
            return SealResult(Status.UNTRUSTED, "unknown_key")
        pem = public_keys[hint]
        if type(pem) is not bytes or len(pem) > 16 * 1024:
            raise ValueError("Invalid public-key input")
        key = serialization.load_pem_public_key(pem)
        if fingerprint(key) != hint:
            raise ValueError("Public key does not match its fingerprint")
        key.verify(signature, pae(payload), ec.ECDSA(hashes.SHA256()))
        signer, signature_verified = hint, True
        resources, extension = _resources(load_json(payload, limits.payload_bytes), expected_kind, limits)
        result = SealResult(Status.VALID, "signature_and_manifest_valid", signer,
                            extension["seal"] if extension else None, resources, True,
                            PROFILE if extension else None)
        if expected_path is not None and expected_path not in {r.name for r in resources}:
            return replace(result, status=Status.UNSIGNED, reason="path_not_covered")
        return result
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError,
            InvalidSignature, UnsupportedAlgorithm, binascii.Error) as error:
        reason = "signature_invalid" if isinstance(error, InvalidSignature) else "seal_invalid"
        return SealResult(Status.INVALID, reason, fingerprint=signer, signature_verified=signature_verified)


def verify_bytes(seal: SealResult, path: str, data: bytes | None, *,
                 limits: Limits = DEFAULT_LIMITS) -> SealResult:
    """Authenticate exactly these immutable bytes, never reopening a mutable path."""
    try:
        validate_path(path, limits)
        if seal.status != Status.VALID:
            return replace(seal, data=None)
        index = bisect_left(seal.resources, path, key=lambda resource: resource.name)
        resource = seal.resources[index] if index < len(seal.resources) and seal.resources[index].name == path else None
        if resource is None:
            return replace(seal, status=Status.UNSIGNED, reason="path_not_covered", data=None)
        if data is None:
            return replace(seal, status=Status.TAMPERED, reason="file_missing", data=None)
        if type(data) is not bytes or len(data) > limits.file_bytes:
            raise ValueError("Expected bounded immutable file bytes")
        if hashlib.sha256(data).hexdigest() != resource.digest:
            return replace(seal, status=Status.TAMPERED, reason="digest_mismatch", data=None)
        return replace(seal, reason="content_valid", data=data)
    except (ValueError, UnicodeError):
        return replace(seal, status=Status.INVALID, reason="file_input_invalid", data=None)


def build_seal(files: Mapping[str, bytes], private_key, *, kind: str,
               root_name: str, origin: str, signer: str, signed_at: str,
               workspace_key: str | None = None, limits: Limits = DEFAULT_LIMITS) -> bytes:
    """Build an OMS seal over supplied bytes; obtaining signing authority is external."""
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ValueError("Only ECDSA P-256 private keys are supported")
    key_id = fingerprint(private_key.public_key())
    names = sorted(validate_paths(list(files), limits))
    if not names:
        raise ValueError("Seals must cover at least one resource")
    extension = dict(profile=PROFILE, seal=kind, origin=origin, signer=signer, signed_at=signed_at)
    if workspace_key is not None:
        extension["workspace_key"] = workspace_key
    _profile(extension, kind, len(names))
    text(root_name, maximum=limits.path_bytes)
    resources = []
    total = 0
    for name in names:
        data = files[name]
        if type(data) is not bytes or len(data) > limits.file_bytes:
            raise ValueError("Expected bounded immutable file bytes")
        total += len(data)
        if total > limits.total_bytes:
            raise ValueError("Total content exceeds limit")
        resources.append(dict(name=name, algorithm="sha256", digest=hashlib.sha256(data).hexdigest()))
    resources[0]["agent_instructions"] = extension
    root_digest = hashlib.sha256(b"".join(bytes.fromhex(r["digest"]) for r in resources)).hexdigest()
    statement = {"_type": STATEMENT_TYPE, "subject": [{"name": root_name, "digest": {"sha256": root_digest}}],
                 "predicateType": PREDICATE_TYPE, "predicate": {
                     "serialization": {"method": "files", "hash_type": "sha256", "allow_symlinks": False, "ignore_paths": []},
                     "resources": resources}}
    payload = json.dumps(statement, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > limits.payload_bytes:
        raise ValueError("Statement exceeds byte limit")
    signature = private_key.sign(pae(payload), ec.ECDSA(hashes.SHA256()))
    bundle = {"mediaType": BUNDLE_TYPE, "verificationMaterial": {"publicKey": {"hint": key_id}, "tlogEntries": []},
              "dsseEnvelope": {"payloadType": PAYLOAD_TYPE, "payload": base64.b64encode(payload).decode(),
                               "signatures": [{"sig": base64.b64encode(signature).decode(), "keyid": ""}]}}
    encoded = json.dumps(bundle, separators=(",", ":")).encode()
    if len(encoded) > limits.seal_bytes:
        raise ValueError("Seal exceeds byte limit")
    return encoded
