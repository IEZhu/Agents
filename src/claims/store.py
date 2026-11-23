"""Append-only per-workspace storage of claim verdicts, and the views the tools return.

One JSON line per verdict in ``<workspace>/data/claims/claims.jsonl`` (directory
0700, file 0600), written under a sidecar lock. Nothing is rewritten or deleted: a
new verdict supersedes the earlier ones by being filtered after them, and the
earlier ones stay readable, which is what makes a contradiction over time visible.

A claim is identified by a key derived from its normalised subject and text, so the
same statement recorded twice lands on the same claim. The filtered state is not
stored; it is recomputed from the verdicts on every read with the current
parameters of ``src.claims.model``, so changing a parameter re-reads the whole
history consistently instead of mixing two calibrations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable, Iterable
import unicodedata
import uuid

from src.claims import model
from src.file_lock import file_lock

FORMAT_VERSION = 1
SUBJECT_MAX = 200
CLAIM_MAX = 1_000
EVIDENCE_MAX_ITEMS = 10
EVIDENCE_ITEM_MAX = 500
ID_MAX = 32
# Above this size the store refuses new verdicts rather than growing without bound;
# at a few hundred bytes a verdict that is tens of thousands of them.
MAX_STORE_BYTES = 20 * 1024 * 1024
KEY = re.compile(r"[0-9a-f]{16}")
HEX64 = re.compile(r"[0-9a-f]{64}")
# Opening flags that refuse a symbolic link and never block on a FIFO, where the platform has them.
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
STATES = ("supported", "stale", "contested", "uncertain", "refuted")


class ClaimError(ValueError):
    """A request the store refuses, with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize(text: str) -> str:
    """Unicode NFC, case-folded, whitespace collapsed, one trailing full stop dropped."""
    folded = " ".join(unicodedata.normalize("NFC", text).casefold().split())
    return folded[:-1].rstrip() if folded.endswith(".") else folded


def claim_key(subject: str, claim: str) -> str:
    digest = hashlib.sha256(f"{normalize(subject)}\n{normalize(claim)}".encode("utf-8")).hexdigest()
    return digest[:16]


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _seconds(value: str) -> float:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text).timestamp()


def _encodable(name: str, value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ClaimError("invalid_request", f"{name} holds a character UTF-8 cannot encode") from None
    return value


def _text(name: str, value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClaimError("invalid_request", f"{name} is required text")
    value = _encodable(name, " ".join(value.split()))
    if len(value) > limit:
        raise ClaimError("invalid_request", f"{name} is {len(value)} characters; at most {limit}")
    return value


def _choice(name: str, value: object, allowed: Iterable[str]) -> str:
    allowed = tuple(allowed)
    if value not in allowed:
        raise ClaimError("invalid_request", f"{name} must be one of {', '.join(allowed)}; got {value!r}")
    return value


def _evidence(items: object) -> list[str]:
    if items is None:
        return []
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list) or len(items) > EVIDENCE_MAX_ITEMS:
        raise ClaimError("invalid_request", f"evidence is a list of at most {EVIDENCE_MAX_ITEMS} strings")
    cleaned = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ClaimError("invalid_request", "every evidence item is nonempty text")
        item = _encodable("evidence", " ".join(item.split()))
        if len(item) > EVIDENCE_ITEM_MAX:
            raise ClaimError("invalid_request", f"an evidence item is at most {EVIDENCE_ITEM_MAX} characters")
        cleaned.append(item)
    return cleaned


def _attestation(contract_revision: object, result_sha256: object) -> dict | None:
    if contract_revision is None and result_sha256 is None:
        return None
    for name, value in (("contract_revision", contract_revision), ("result_sha256", result_sha256)):
        if value is not None and (not isinstance(value, str) or not HEX64.fullmatch(value)):
            raise ClaimError("invalid_request", f"{name} is 64 lowercase hexadecimal characters")
    return {"contract_revision": contract_revision, "result_sha256": result_sha256}


def _well_formed(event: object) -> bool:
    """A line the filter and the views can use; anything else (a hand edit, a torn write) is skipped."""
    if not isinstance(event, dict) or event.get("v") != FORMAT_VERSION or not isinstance(event.get("at"), str):
        return False
    try:
        _seconds(event["at"])
    except (ValueError, OverflowError, OSError):
        return False
    subject, claim, evidence = event.get("subject"), event.get("claim"), event.get("evidence")
    if not (isinstance(subject, str) and 0 < len(subject) <= SUBJECT_MAX
            and isinstance(claim, str) and 0 < len(claim) <= CLAIM_MAX
            and isinstance(event.get("key"), str) and isinstance(event.get("id"), str)
            and 0 < len(event["id"]) <= ID_MAX):
        return False
    try:
        # The key is derived, never trusted: a line whose key does not match its own text would
        # attach a verdict to another claim.
        if event["key"] != claim_key(subject, claim):
            return False
    except UnicodeEncodeError:
        return False
    return (event.get("verdict") in model.VERDICTS and event.get("level") in model.LEVEL_ORDER
            and event.get("importance") in model.IMPORTANCE_ORDER
            and event.get("volatility") in model.VOLATILITY_ORDER
            and isinstance(evidence, list) and len(evidence) <= EVIDENCE_MAX_ITEMS
            and all(isinstance(item, str) and len(item) <= EVIDENCE_ITEM_MAX for item in evidence)
            and "attestation" in event and _well_formed_attestation(event["attestation"]))


def _well_formed_attestation(attestation: object) -> bool:
    if attestation is None:
        return True
    return (isinstance(attestation, dict) and set(attestation) == {"contract_revision", "result_sha256"}
            and all(value is None or (isinstance(value, str) and bool(HEX64.fullmatch(value)))
                    for value in attestation.values()))


class _UnsafeStore(ClaimError):
    def __init__(self, message: str):
        super().__init__("unsafe_store", message)


def _owned_by_caller(info: os.stat_result) -> bool:
    """POSIX: the file belongs to this process's user. Windows reports no owner here."""
    return os.name == "nt" or info.st_uid == os.geteuid()


def _real_directory(path: Path, *, private: bool = False) -> None:
    """``path`` exists as a directory and is not a symbolic link.

    With ``private`` it must also belong to this user, and group and other permissions
    are removed: ``mkdir``'s mode does nothing for a directory that already exists.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(mode=0o700, exist_ok=True)  # another process may create it first; checked below
        info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise _UnsafeStore(f"{path} is not a plain directory; the claims store does not follow links")
    if not private or os.name == "nt":
        return
    if not _owned_by_caller(info):
        raise _UnsafeStore(f"{path} belongs to another user; the claims store keeps only its own files")
    if info.st_mode & 0o077:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NO_FOLLOW)
        try:
            if not os.path.samestat(info, os.fstat(descriptor)):
                raise _UnsafeStore(f"{path} changed while it was opened")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)


def _open_store(path: Path, flags: int, *, private: bool = False) -> int:
    """Open the store file itself, never through a link, and only as a regular file.

    With ``private`` (the writer) the file must belong to this user and loses group and
    other permissions; the mode passed to ``open`` applies only to a file it creates.
    """
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
        if not flags & os.O_CREAT:
            raise
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise _UnsafeStore(f"{path} is not a regular file; the claims store does not follow links")
    descriptor = os.open(path, flags | _NO_FOLLOW, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (before is not None and not os.path.samestat(before, opened)):
            raise _UnsafeStore(f"{path} changed while it was opened")
        if private and os.name != "nt":
            if not _owned_by_caller(opened):
                raise _UnsafeStore(f"{path} belongs to another user; the claims store keeps only its own files")
            if opened.st_mode & 0o077:
                os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@dataclass(frozen=True)
class Claim:
    """Every verdict recorded for one key, oldest first."""

    key: str
    events: tuple[dict, ...]

    @property
    def subject(self) -> str:
        return self.events[-1]["subject"]

    @property
    def text(self) -> str:
        return self.events[-1]["claim"]

    def verdicts(self) -> list[model.Verdict]:
        return [model.Verdict(at=_seconds(event["at"]), verdict=event["verdict"], level=event["level"],
                              volatility=event["volatility"], importance=event["importance"])
                for event in self.events]

    def assess(self, now: float) -> model.Assessment:
        return model.assess(self.verdicts(), now)


def view(claim: Claim, assessment: model.Assessment) -> dict:
    """The compact form of a claim's state, as both tools return it."""
    return {
        "key": claim.key,
        "subject": claim.subject,
        "claim": claim.text,
        "state": assessment.state,
        "confidence": round(assessment.confidence, 4),
        "confidence_at_last_check": round(assessment.settled, 4),
        "level": assessment.level,
        "families": list(assessment.families),
        "importance": assessment.importance,
        "usable_for": list(assessment.usable_for),
        "revalidate_at": None if assessment.revalidate_at is None else _iso(assessment.revalidate_at),
        "checks": assessment.checks,
        "confirmed": assessment.confirmed,
        "refuted": assessment.refuted,
        "last_verdict": assessment.last_verdict,
        "last_checked": _iso(assessment.last_at),
        "last_surprise": round(assessment.surprise, 4),
    }


def history_view(claim: Claim, limit: int) -> list[dict]:
    """The newest ``limit`` verdicts of a claim, newest first."""
    return [{key: event[key] for key in ("id", "at", "verdict", "level", "importance", "volatility",
                                         "evidence", "attestation")}
            for event in reversed(claim.events[-limit:])]


class ClaimStore:
    """The verdicts of one workspace."""

    def __init__(self, root: Path | str, *, clock: Callable[[], float] = time.time):
        self.directory = Path(root) / "data" / "claims"
        self.path = self.directory / "claims.jsonl"
        self.clock = clock

    # --- reading -------------------------------------------------------------------

    def _events(self) -> list[dict]:
        for directory in (self.directory.parent, self.directory):
            try:
                if not stat.S_ISDIR(os.lstat(directory).st_mode):
                    raise _UnsafeStore(f"{directory} is not a plain directory; the claims store does not follow links")
            except FileNotFoundError:
                return []
        try:
            descriptor = _open_store(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return []
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(MAX_STORE_BYTES + 1)
        if len(raw) > MAX_STORE_BYTES:
            raise _UnsafeStore(f"{self.path} is larger than {MAX_STORE_BYTES} bytes; archive it by hand")
        events = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if _well_formed(event):
                events.append(event)
        return events

    def claims(self) -> dict[str, Claim]:
        grouped: dict[str, list[dict]] = {}
        for event in self._events():
            grouped.setdefault(event["key"], []).append(event)
        return {key: Claim(key, tuple(events)) for key, events in grouped.items()}

    def get(self, key: str) -> Claim | None:
        if not KEY.fullmatch(key):
            raise ClaimError("invalid_request", "key is 16 lowercase hexadecimal characters")
        return self.claims().get(key)

    def search(self, *, query: str | None = None, state: str | None = None,
               now: float | None = None) -> list[tuple[Claim, model.Assessment]]:
        """Claims whose subject or text contains ``query`` and whose state is ``state``.

        Newest verdict first; with ``state`` set, the earliest revalidation first, so
        the list reads as a work queue.
        """
        if state is not None:
            _choice("state", state, STATES)
        now = self.clock() if now is None else now
        needle = normalize(query) if query else None
        matches = []
        for claim in self.claims().values():
            if needle and needle not in normalize(claim.subject) and needle not in normalize(claim.text):
                continue
            assessment = claim.assess(now)
            if state is not None and assessment.state != state:
                continue
            matches.append((claim, assessment))
        if state is None:
            matches.sort(key=lambda pair: pair[1].last_at, reverse=True)
        else:
            matches.sort(key=lambda pair: (pair[1].revalidate_at is None, pair[1].revalidate_at or 0.0))
        return matches

    # --- writing -------------------------------------------------------------------

    def record(self, *, subject: object, claim: object, verdict: object, level: object,
               importance: object = "load_bearing", volatility: object = "normal",
               evidence: object = None, contract_revision: object = None,
               result_sha256: object = None) -> dict:
        """Append one verdict and return the claim's state before and after it."""
        subject = _text("subject", subject, SUBJECT_MAX)
        claim = _text("claim", claim, CLAIM_MAX)
        verdict = _choice("verdict", verdict, model.VERDICTS)
        if level == "V0":
            raise ClaimError("invalid_request", "V0 is the model's own words and validates nothing; "
                                                "record a verdict once a source, a command or a reviewer backs it")
        level = _choice("level", level, model.LEVEL_ORDER)
        importance = _choice("importance", importance, model.IMPORTANCE_ORDER)
        volatility = _choice("volatility", volatility, model.VOLATILITY_ORDER)
        evidence = _evidence(evidence)
        attestation = _attestation(contract_revision, result_sha256)
        key = claim_key(subject, claim)
        _real_directory(self.directory.parent)
        _real_directory(self.directory, private=True)
        with file_lock(self.path.with_name(self.path.name + ".lock")):
            existing = self.claims().get(key)
            # Never earlier than the newest verdict, so the filter's time only moves forward.
            now = self.clock()
            if existing is not None:
                now = max(now, _seconds(existing.events[-1]["at"]))
            before = existing.assess(now) if existing is not None else None
            event = {"v": FORMAT_VERSION, "id": uuid.uuid4().hex[:12], "key": key, "at": _iso(now),
                     "subject": subject, "claim": claim, "verdict": verdict, "level": level,
                     "importance": importance, "volatility": volatility, "evidence": evidence,
                     "attestation": attestation}
            line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            descriptor = _open_store(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, private=True)
            with os.fdopen(descriptor, "r+b") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size and (handle.seek(size - 1), handle.read(1))[1] != b"\n":
                    line = b"\n" + line  # an earlier write was torn: never glue a verdict onto it
                if size + len(line) > MAX_STORE_BYTES:
                    raise ClaimError("store_full", f"{self.path} would exceed {MAX_STORE_BYTES} bytes; "
                                                   "archive it by hand to start a new store")
                handle.write(line)
            updated = Claim(key, (existing.events if existing else ()) + (event,))
        after = updated.assess(_seconds(event["at"]))
        return {
            "status": "recorded",
            "event_id": event["id"],
            "contradiction": after.contested,
            "predictive": round(after.surprise, 4),
            "previous": None if before is None else {"state": before.state,
                                                     "confidence": round(before.confidence, 3)},
            "claim": view(updated, after),
        }
