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
# Above this size the store refuses new verdicts rather than growing without bound;
# at a few hundred bytes a verdict that is tens of thousands of them.
MAX_STORE_BYTES = 20 * 1024 * 1024
KEY = re.compile(r"^[0-9a-f]{16}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
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


def _text(name: str, value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClaimError("invalid_request", f"{name} is required text")
    value = " ".join(value.split())
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
        item = " ".join(item.split())
        if len(item) > EVIDENCE_ITEM_MAX:
            raise ClaimError("invalid_request", f"an evidence item is at most {EVIDENCE_ITEM_MAX} characters")
        cleaned.append(item)
    return cleaned


def _attestation(contract_revision: object, result_sha256: object) -> dict | None:
    if contract_revision is None and result_sha256 is None:
        return None
    for name, value in (("contract_revision", contract_revision), ("result_sha256", result_sha256)):
        if value is not None and (not isinstance(value, str) or not HEX64.match(value)):
            raise ClaimError("invalid_request", f"{name} is 64 lowercase hexadecimal characters")
    return {"contract_revision": contract_revision, "result_sha256": result_sha256}


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
        "confidence": round(assessment.confidence, 3),
        "band": [round(assessment.lower, 3), round(assessment.upper, 3)],
        "level": assessment.level,
        "importance": assessment.importance,
        "usable_for": list(assessment.usable_for),
        "revalidate_at": None if assessment.revalidate_at is None else _iso(assessment.revalidate_at),
        "checks": assessment.checks,
        "confirmed": assessment.confirmed,
        "refuted": assessment.refuted,
        "last_verdict": assessment.last_verdict,
        "last_checked": _iso(assessment.last_at),
        "log_odds": {"mean": round(assessment.estimate.mean, 4),
                     "variance": round(assessment.estimate.variance, 4)},
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
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        events = []
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("v") == FORMAT_VERSION and KEY.match(str(event.get("key"))):
                events.append(event)
        return events

    def claims(self) -> dict[str, Claim]:
        grouped: dict[str, list[dict]] = {}
        for event in self._events():
            grouped.setdefault(event["key"], []).append(event)
        return {key: Claim(key, tuple(events)) for key, events in grouped.items()}

    def get(self, key: str) -> Claim | None:
        if not KEY.match(key):
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
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
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
            line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            size = self.path.stat().st_size if self.path.exists() else 0
            if size + len(line.encode("utf-8")) > MAX_STORE_BYTES:
                raise ClaimError("store_full", f"{self.path} would exceed {MAX_STORE_BYTES} bytes; "
                                               "archive it by hand to start a new store")
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line)
            updated = Claim(key, (existing.events if existing else ()) + (event,))
        after = updated.assess(_seconds(event["at"]))
        return {
            "status": "recorded",
            "event_id": event["id"],
            "contradiction": after.contested,
            "nis": round(after.last_nis, 3),
            "previous": None if before is None else {"state": before.state,
                                                     "confidence": round(before.confidence, 3)},
            "claim": view(updated, after),
        }
