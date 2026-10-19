"""Owner-enrolled key/root authority and authenticated local policy revisions.

This backend must run in an already trusted runtime. It neither authenticates
its own Python imports nor proves human intent from a Python caller. Only local
owner controls may call mutations; there are deliberately no MCP endpoints.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import UnsupportedAlgorithm

from .common import DEFAULT_LIMITS, HEX_DIGEST, SealResult, Status, load_json, text, validate_paths
from .inventory import INVENTORY_PATH, _parse_inventory
from .oms import fingerprint, public_key_pem, verify_bytes, verify_seal
from . import storage
from .storage import AuthorityError

_SCHEMA = "agents-core/instruction-authority/1"
_MAX_REVISIONS = 4096
_MAX_HISTORY_BYTES = 64 * 1024 * 1024


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _root(path) -> str:
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_dir():
        raise AuthorityError("An enrolled root must be an existing directory")
    return os.path.normcase(str(resolved))


def _public(pem: bytes):
    if type(pem) is not bytes or len(pem) > 16384:
        raise AuthorityError("Expected a bounded P-256 public key")
    key = serialization.load_pem_public_key(pem)
    return key, public_key_pem(key)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TrustEntry:
    fingerprint: str
    public_key: bytes
    kind: str
    roots: tuple[str, ...]
    label: str
    added_at: str
    source: str


@dataclass(frozen=True)
class AuthoritySnapshot:
    """No trust on error. The token also binds pending decisions/cache entries."""

    valid: bool
    diagnostic: str | None = None
    revision: int | None = None
    digest: str | None = None
    entries: tuple[TrustEntry, ...] = ()
    policy_bytes: bytes | None = None
    authority_id: str | None = None
    installation_root: str | None = None
    library_root: str | None = None
    policy_digest: str | None = None

    @property
    def token(self) -> tuple[int, str] | None:
        return (self.revision, self.digest) if self.valid else None


class AuthorityStore:
    """One installation's authority, shared by daemon and stdio callers.

    ``state_directory`` is the daemon state directory, not the signing directory.
    A separate random authentication key protects local records: a machine's
    library signing key is never a policy-signing authority. The accepted head
    and retained hash/MAC chain detect record edits, missing records and head
    rollback. Replacing the entire boundary as the OS user is outside the model.
    """

    def __init__(self, state_directory, installation):
        self.directory = Path(os.path.abspath(state_directory)) / "signing"
        self.installation = _root(installation)
        if self.directory.resolve().is_relative_to(self.installation):
            raise AuthorityError("Authority state must be outside the candidate installation")

    @classmethod
    def for_installation(cls, installation=None):
        from src.daemon.state import state_dir
        from src.runtime_paths import installation_root
        root = installation_root() if installation is None else installation
        return cls(state_dir(root), root)

    def initialize(self, *, label: str, library_root, expected_library_root=None) -> AuthoritySnapshot:
        """Trusted first-start enrollment; never repair/re-enroll existing state."""
        text(label, maximum=256)
        library = _root(library_root)
        if expected_library_root is not None and library != os.path.normcase(os.fspath(expected_library_root)):
            raise AuthorityError("Library root changed after confirmation; repeat enrollment")
        if (library == self.installation or Path(self.installation).is_relative_to(library) or
                self.directory.resolve().is_relative_to(library)):
            raise AuthorityError("Library scope overlaps the trust base or installation root")
        with storage.locked(self.directory, create=True):
            if any(p.name != ".lock" for p in self.directory.iterdir()):
                raise AuthorityError("Authority already exists or enrollment is incomplete; refusing reset")
            key = ec.generate_private_key(ec.SECP256R1())
            public = public_key_pem(key.public_key())
            storage.write(self.directory / "machine.key", key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            storage.write(self.directory / "machine.pub", public)
            secret = secrets.token_bytes(32)
            storage.write(self.directory / "authority.key", secret)
            storage.directory(self.directory / "revisions", create=True)
            state = {"schema": _SCHEMA, "installation": self.installation,
                     "library": library, "machine": fingerprint(key.public_key()),
                     "entries": [self._entry(public, "library", (library,), label, "machine")],
                     "policy": None}
            self._commit(state, secret, previous=None)
            return self._snapshot(self._load()[0])

    @staticmethod
    def _entry(pem, kind, roots, label, source):
        key, canonical = _public(pem)
        text(label, maximum=256)
        text(source, maximum=256)
        if kind not in {"release", "library"}:
            raise AuthorityError("Only release and library authorities exist")
        return {"fingerprint": fingerprint(key), "public_key": canonical.decode("ascii"),
                "kind": kind, "roots": list(roots), "label": label,
                "added_at": _stamp(), "source": source}

    def _validate(self, state):
        if (type(state) is not dict or set(state) != {"schema", "installation", "library", "machine", "entries", "policy"}
                or state["schema"] != _SCHEMA or state["installation"] != self.installation):
            raise AuthorityError("Authority schema or installation binding is invalid")
        library = state["library"]
        if not isinstance(library, str) or not os.path.isabs(library) or library == self.installation:
            raise AuthorityError("Library binding is invalid")
        if not isinstance(state["machine"], str) or not HEX_DIGEST.fullmatch(state["machine"]):
            raise AuthorityError("Machine identity is invalid")
        if type(state["entries"]) is not list or len(state["entries"]) > 256:
            raise AuthorityError("Trust entry limit or type is invalid")
        seen = set()
        for entry in state["entries"]:
            if type(entry) is not dict or set(entry) != {"fingerprint", "public_key", "kind", "roots", "label", "added_at", "source"}:
                raise AuthorityError("Trust entry fields are invalid")
            key, pem = _public(entry["public_key"].encode("ascii"))
            fp = fingerprint(key)
            if fp != entry["fingerprint"] or fp in seen or pem.decode("ascii") != entry["public_key"]:
                raise AuthorityError("Trust fingerprint, key encoding or uniqueness is invalid")
            seen.add(fp)
            if entry["kind"] not in {"release", "library"}:
                raise AuthorityError("Trust kind is invalid")
            expected = self.installation if entry["kind"] == "release" else library
            if entry["roots"] != [expected] or (fp == state["machine"] and entry["kind"] != "library"):
                raise AuthorityError("Trust authority does not match its allowed root scope")
            for field in ("label", "source", "added_at"):
                text(entry[field], maximum=256)
        policy = state["policy"]
        if policy is not None:
            if (type(policy) is not dict or set(policy) != {"mode", "additional_paths", "inventory", "release_fingerprint"}
                    or policy["mode"] not in {"enforce", "warn"}
                    or type(policy["additional_paths"]) is not list):
                raise AuthorityError("Protected policy fields are invalid")
            validate_paths(policy["additional_paths"], DEFAULT_LIMITS)
            if policy["additional_paths"] != sorted(policy["additional_paths"]):
                raise AuthorityError("Additional coverage must be sorted")
            _parse_inventory(_json(policy["inventory"]), DEFAULT_LIMITS)
            if not HEX_DIGEST.fullmatch(policy["release_fingerprint"]):
                raise AuthorityError("Policy release identity is invalid")

    def _load(self):
        secret = storage.read(self.directory / "authority.key", 32)
        if len(secret) != 32:
            raise AuthorityError("Authority authentication key is invalid")
        storage.directory(self.directory / "revisions")
        names = sorted(p.name for p in (self.directory / "revisions").iterdir())
        if not names or len(names) > _MAX_REVISIONS or names != [f"{n:08d}.json" for n in range(len(names))]:
            raise AuthorityError("Authority history has missing, unexpected or excessive revisions")
        previous = None
        total = 0
        record = None
        for revision, name in enumerate(names):
            raw = storage.read(self.directory / "revisions" / name)
            total += len(raw)
            if total > _MAX_HISTORY_BYTES:
                raise AuthorityError("Authority history exceeds its byte bound")
            record = load_json(raw, 2 * 1024 * 1024)
            if (type(record) is not dict or set(record) != {"revision", "previous", "state", "policy_digest", "mac"}
                    or type(record["revision"]) is not int or record["revision"] != revision
                    or record["previous"] != previous
                    or record["policy_digest"] != _digest(_json(record["state"]["policy"]))):
                raise AuthorityError("Authority revision chain is invalid")
            body = {k: record[k] for k in ("revision", "previous", "state", "policy_digest")}
            mac = hmac.digest(secret, b"agents-core/authority/1\0" + _json(body), "sha256").hex()
            if not isinstance(record["mac"], str) or not hmac.compare_digest(mac, record["mac"]):
                raise AuthorityError("Authority record authentication failed")
            self._validate(record["state"])
            previous = _digest(raw)
        head = load_json(storage.read(self.directory / "accepted.json", 4096), 4096)
        if (type(head) is not dict or type(head.get("revision")) is not int or
                head != {"revision": len(names) - 1, "digest": previous}):
            raise AuthorityError("Accepted authority head is missing, rolled back or incomplete")
        record["digest"] = previous
        record["history_bytes"] = total
        return record, secret

    def _commit(self, state, secret, previous):
        self._validate(state)
        revision = 0 if previous is None else previous["revision"] + 1
        if revision >= _MAX_REVISIONS:
            raise AuthorityError("Authority revision capacity reached; trusted maintenance is required")
        record = {"revision": revision, "previous": previous["digest"] if previous else None, "state": state,
                  "policy_digest": _digest(_json(state["policy"]))}
        record["mac"] = hmac.digest(secret, b"agents-core/authority/1\0" + _json(record), "sha256").hex()
        raw = _json(record)
        if len(raw) > 2 * 1024 * 1024 or len(raw) + (previous["history_bytes"] if previous else 0) > _MAX_HISTORY_BYTES:
            raise AuthorityError("Authority history capacity exceeded; no transition committed")
        # Publish the durable record first. A crash before the head replacement
        # blocks use; it never silently authorizes either an older or new state.
        storage.write(self.directory / "revisions" / f"{revision:08d}.json", raw)
        storage.write(self.directory / "accepted.json", _json({"revision": revision, "digest": _digest(raw)}))

    @staticmethod
    def _snapshot(record):
        state = record["state"]
        entries = tuple(TrustEntry(e["fingerprint"], e["public_key"].encode("ascii"), e["kind"],
                                  tuple(e["roots"]), e["label"], e["added_at"], e["source"])
                        for e in state["entries"])
        return AuthoritySnapshot(True, revision=record["revision"], digest=record["digest"],
                                 entries=entries, policy_bytes=_json(state["policy"]) if state["policy"] else None,
                                 authority_id=_digest(_json([state["installation"], state["machine"]])),
                                 installation_root=state["installation"], library_root=state["library"],
                                 policy_digest=record["policy_digest"])

    def snapshot(self) -> AuthoritySnapshot:
        """Read-only and nonthrowing for expected storage/input corruption."""
        try:
            with storage.locked(self.directory, shared=True):
                return self._snapshot(self._load()[0])
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, AttributeError, RecursionError, UnsupportedAlgorithm) as error:
            return AuthoritySnapshot(False, diagnostic=f"Authority unavailable: {error}")

    @staticmethod
    def _expect(record, expected_token):
        if expected_token != (record["revision"], record["digest"]):
            raise AuthorityError("Authority changed; repeat the local owner decision")

    def add_key(self, pem: bytes, *, kind: str, label: str, expected_token,
                source="cli") -> AuthoritySnapshot:
        with storage.locked(self.directory):
            record, secret = self._load()
            self._expect(record, expected_token)
            state = deepcopy(record["state"])
            root = state["installation"] if kind == "release" else state["library"]
            entry = self._entry(pem, kind, (root,), label, source)
            if entry["fingerprint"] == state["machine"] and kind != "library":
                raise AuthorityError("The machine key cannot acquire release authority")
            if any(e["fingerprint"] == entry["fingerprint"] for e in state["entries"]):
                raise AuthorityError("Key is already enrolled; remove it before changing its authority")
            state["entries"].append(entry)
            self._commit(state, secret, record)
            return self._snapshot(self._load()[0])

    def remove_key(self, key_fingerprint: str, *, expected_token) -> AuthoritySnapshot:
        with storage.locked(self.directory):
            record, secret = self._load()
            self._expect(record, expected_token)
            state = deepcopy(record["state"])
            entries = [e for e in state["entries"] if e["fingerprint"] != key_fingerprint]
            if len(entries) == len(state["entries"]):
                raise AuthorityError("Key is not enrolled")
            state["entries"] = entries
            self._commit(state, secret, record)
            return self._snapshot(self._load()[0])

    def verify(self, seal: bytes | None, *, kind: str, root, expected_path=None,
               expected_token=None) -> SealResult:
        """Always re-read current authority; mode never grants signing rights."""
        snapshot = self.snapshot()
        if not snapshot.valid:
            return SealResult(Status.UNTRUSTED, "authority_unavailable")
        if expected_token is not None and expected_token != snapshot.token:
            return SealResult(Status.UNTRUSTED, "authority_revision_changed")
        try:
            root_id = _root(root)
            if kind not in {"release", "library"}:
                raise AuthorityError("Unsupported seal authority")
        except (OSError, ValueError):
            return SealResult(Status.UNTRUSTED, "root_authority_invalid")
        keys = {e.fingerprint: e.public_key for e in snapshot.entries if e.kind == kind and root_id in e.roots}
        result = verify_seal(seal, keys, expected_kind=kind, expected_path=expected_path)
        return replace(result, authorization_checked=result.status == Status.VALID)

    def machine_key(self, *, expected_token):
        """Trusted signing handoff, with a fresh revision and revocation check."""
        with storage.locked(self.directory):
            record, _ = self._load()
            self._expect(record, expected_token)
            state = record["state"]
            key = serialization.load_pem_private_key(storage.read(self.directory / "machine.key", 16384), password=None)
            fp = fingerprint(key.public_key())
            if fp != state["machine"] or not any(e["fingerprint"] == fp and e["kind"] == "library" for e in state["entries"]):
                raise AuthorityError("Machine key is changed or revoked")
            if storage.read(self.directory / "machine.pub", 16384) != public_key_pem(key.public_key()):
                raise AuthorityError("Machine public and private keys disagree")
            return key

    @staticmethod
    def _policy_value(state, inventory_bytes, release_seal, additional_paths, mode):
        keys = {e["fingerprint"]: e["public_key"].encode("ascii") for e in state["entries"] if e["kind"] == "release"}
        release = verify_seal(release_seal, keys, expected_kind="release")
        check = verify_bytes(release, INVENTORY_PATH, inventory_bytes)
        if check.status != Status.VALID:
            raise AuthorityError("Policy minimum requires a pinned release seal over the inventory")
        inventory = _parse_inventory(check.data, DEFAULT_LIMITS)
        resources = {r.name for r in release.resources}
        if any(f["path"] not in resources for f in inventory["files"]):
            raise AuthorityError("Release inventory contains unsealed mandatory paths")
        if type(additional_paths) is not tuple:
            raise AuthorityError("Additional coverage must be an explicit immutable path tuple")
        validate_paths(additional_paths, DEFAULT_LIMITS)
        if mode not in {"enforce", "warn"}:
            raise AuthorityError("Only enforce and the explicit warn rollout policy are supported")
        return {"mode": mode, "additional_paths": sorted(additional_paths),
                "inventory": inventory, "release_fingerprint": check.fingerprint}

    def preview_policy(self, *, inventory_bytes: bytes, release_seal: bytes,
                       additional_paths: tuple[str, ...], mode: str, expected_token) -> dict:
        """Validate exact inputs before an owner prompt, without granting use."""
        with storage.locked(self.directory):
            record, _ = self._load()
            self._expect(record, expected_token)
            policy = self._policy_value(record["state"], inventory_bytes, release_seal, additional_paths, mode)
            return {"previous_policy": deepcopy(record["state"]["policy"]), "policy": policy,
                    "previous_digest": record["policy_digest"], "digest": _digest(_json(policy))}

    def set_policy(self, *, inventory_bytes: bytes, release_seal: bytes,
                   additional_paths: tuple[str, ...], mode: str, expected_token) -> AuthoritySnapshot:
        """Explicit owner transition; candidate config is never a policy source.

        The release minimum is derived from an authenticated inventory, not a
        caller-supplied path subset. Even an optional-scope removal or mode change
        needs the current revision and release authority again.
        """
        with storage.locked(self.directory):
            record, secret = self._load()
            self._expect(record, expected_token)
            state = deepcopy(record["state"])
            state["policy"] = self._policy_value(state, inventory_bytes, release_seal, additional_paths, mode)
            self._commit(state, secret, record)
            return self._snapshot(self._load()[0])

    def policy(self, *, expected_token=None) -> dict:
        """Fresh effective policy or refusal; no empty/minimum/warn fallback."""
        snapshot = self.snapshot()
        if not snapshot.valid or (expected_token is not None and snapshot.token != expected_token):
            raise AuthorityError("Current authenticated policy is unavailable or changed")
        if snapshot.policy_bytes is None:
            raise AuthorityError("No owner policy has been accepted")
        policy = load_json(snapshot.policy_bytes, 2 * 1024 * 1024)
        if not any(e.fingerprint == policy["release_fingerprint"] and e.kind == "release" for e in snapshot.entries):
            raise AuthorityError("Accepted policy release authority has been revoked")
        return policy
