"""Owner probe evidence, authenticated and bound to an exact native profile."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .process import private_directory
from .models import LocalInferenceError
from .profile import canonical_json

PROBE_PROTOCOL = "agents-core/local-inference-probe/1"
MAPPING_VERSION = "llama-d81235049/conventional-evaluated-output/1"
MAX_RECORD_BYTES = 65536
_HEX = re.compile(r"^[a-f0-9]{64}$")
_FIELDS = {"schema", "profile_hash", "mapping_version", "resident", "snapshot", "mapping_proven",
           "native_write_limit", "finish_cases", "model_calls", "created_at", "rendered_token_hashes",
           "native_reuse_counts"}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence key")
        result[key] = value
    return result


class CapabilityRecords:
    """Called lazily after startup. Creating this object performs no filesystem I/O."""
    def __init__(self, directory: Path | str, *, clock=time.time):
        self.directory = Path(directory) / "capabilities"
        self.clock = clock

    def _key(self) -> bytes:
        private_directory(self.directory)
        path = self.directory / "signing.key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError("unsafe signing key")
            key = os.read(fd, 33)
            if len(key) != 32:
                raise ValueError("invalid signing key")
            return key
        finally:
            os.close(fd)

    def _valid(self, record: dict, profile_hash: str) -> bool:
        if (type(record) is not dict or set(record) != _FIELDS or record["schema"] != PROBE_PROTOCOL
                or record["profile_hash"] != profile_hash or not _HEX.fullmatch(profile_hash)
                or record["mapping_version"] != MAPPING_VERSION):
            return False
        if (record["resident"] not in ("supported", "unsupported", "unknown") or
                record["snapshot"] not in ("supported", "unsupported", "unknown")):
            return False
        if any(type(record[field]) is not bool for field in ("mapping_proven", "native_write_limit")):
            return False
        if type(record["model_calls"]) is not int or not 2 <= record["model_calls"] <= 100:
            return False
        created = record["created_at"]
        if type(created) not in (float, int) or not math.isfinite(created) or not 0 < created <= self.clock():
            return False
        cases, hashes, counts = (record[field] for field in ("finish_cases", "rendered_token_hashes", "native_reuse_counts"))
        return (type(cases) is list and len(cases) <= 4
                and all(type(case) is str and case in ("eos", "word", "limit", "unicode") for case in cases)
                and len(set(cases)) == len(cases)
                and type(hashes) is list and len(hashes) == record["model_calls"]
                and all(type(item) is str and _HEX.fullmatch(item) for item in hashes)
                and type(counts) is list and len(counts) == record["model_calls"]
                and all(type(count) is int and 0 <= count <= 0xffffffff for count in counts)
                and (record["resident"] != "supported" or any(counts)))

    def write(self, evidence: dict) -> Path:
        """Owner diagnostic publication; this method does not itself run a probe."""
        profile_hash = evidence.get("profile_hash", "")
        if not self._valid(evidence, profile_hash):
            raise ValueError("invalid probe evidence")
        payload = canonical_json(evidence)
        record = {**evidence, "hmac": hmac.new(self._key(), payload, hashlib.sha256).hexdigest()}
        encoded = canonical_json(record)
        if len(encoded) > MAX_RECORD_BYTES:
            raise ValueError("probe evidence too large")
        path = self.directory / f"{profile_hash}.json"
        temporary = self.directory / f"probe-{secrets.token_hex(16)}.partial"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def load(self, profile_hash: str, *, native_write_limit: bool) -> tuple[str, str]:
        """Missing/invalid evidence never upgrades unknown capability."""
        if type(profile_hash) is not str or not _HEX.fullmatch(profile_hash):
            return "unknown", "unknown"
        try:
            fd = os.open(self.directory / f"{profile_hash}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_RECORD_BYTES):
                    return "unknown", "unknown"
                raw = os.read(fd, MAX_RECORD_BYTES + 1)
            finally:
                os.close(fd)
            record = json.loads(raw, object_pairs_hook=_json_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(record) is not dict:
                return "unknown", "unknown"
            signature = record.pop("hmac")
            if type(signature) is not str or not _HEX.fullmatch(signature) or not self._valid(record, profile_hash):
                return "unknown", "unknown"
            expected = hmac.new(self._key(), canonical_json(record), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return "unknown", "unknown"
            resident = record["resident"]
            snapshot = record["snapshot"]
            if snapshot == "supported" and not (resident == "supported" and record["mapping_proven"]
                    and record["native_write_limit"] and native_write_limit
                    and set(record["finish_cases"]) == {"eos", "word", "limit", "unicode"}
                    and record["model_calls"] >= 6):
                snapshot = "unknown"
            return resident, snapshot
        except (OSError, ValueError, TypeError, KeyError, LocalInferenceError):
            return "unknown", "unknown"
