"""The run state file of an autodev run and its read-only copy for the sessions (#347).

flows/autodev-implement.md step 0 has a session re-read ``{state_copy}/state.json`` after any
context loss: the run sequence's own summary of the run. `write_state` writes it at every stage
transition twice: ``<run_dir>/state.json`` in the runner's records (private) and
``<state_copy>/state.json``, read-only (0400), in the run's state copy, the directory every stage
session of the run can read but not write (``worker-log.json`` of `src.schedules.workers` sits
beside it). One state copy serves all stages of a run, so the worker service's copy stays visible
to the submit session too.

The file (`RunState.to_dict`) holds ``{"version": 1, "run_id", "mode", "repository", "pinned",
"runner_commit", "stage", "stages_done", "issue", "branch", "pr", "base", "head", "slice",
"criteria_refs", "flows", "persona_source", "limits", "updated"}``: ``base`` is ``pinned``, or
the merge base of a resume after main moved; ``flows`` the blob SHA at ``pinned`` of each flow the
run reads (`FLOW_PATHS`, `repos.TrustedRepository.blob_ids`); ``persona_source`` how a session
reads its persona, skill and rule files. It holds no secret and no security detail; every value
is checked, and strings pass `records.mask` with the run's known secrets.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from src.daemon.state import atomic_private, private_dir, write_json

from . import records, repos
from .config import LIMITS

STATE_NAME = "state.json"
VERSION = 1
MODES = ("dry-run", "run", "resume")
STAGE_KINDS = ("select", "implement", "submit")
STAGES = (*STAGE_KINDS, "done")
FLOW_PATHS = ("flows/autodev.md", "flows/autodev-select.md", "flows/autodev-implement.md", "flows/pr-review.md",
              "flows/code-review-pass.md", "flows/change-premortem.md")
_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _positive(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _sha_or_none(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(_SHA.fullmatch(value)))


@dataclass
class RunState:
    """What the run sequence knows about one run; see the module docstring."""

    run_id: str
    mode: str
    repository: str
    pinned: str
    runner_commit: str | None = None
    stage: str = "select"
    stages_done: list[str] = field(default_factory=list)
    issue: int | None = None
    branch: str | None = None
    pr: int | None = None
    base: str | None = None
    head: str | None = None
    slice: dict | None = None
    criteria_refs: list[str] = field(default_factory=list)
    flows: dict[str, str | None] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)

    def problems(self) -> list[str]:
        """Every field that does not hold what the file promises."""
        found = []
        if not records.valid_run_id(self.run_id):
            found.append("run_id")
        if self.mode not in MODES:
            found.append("mode")
        if not isinstance(self.repository, str) or not _REPOSITORY.fullmatch(self.repository):
            found.append("repository")
        if not isinstance(self.pinned, str) or not _SHA.fullmatch(self.pinned):
            found.append("pinned")
        for name in ("runner_commit", "base", "head"):
            if not _sha_or_none(getattr(self, name)):
                found.append(name)
        if self.stage not in STAGES:
            found.append("stage")
        if not isinstance(self.stages_done, list) or not set(self.stages_done) <= set(STAGE_KINDS):
            found.append("stages_done")
        for name in ("issue", "pr"):
            if getattr(self, name) is not None and not _positive(getattr(self, name)):
                found.append(name)
        if self.branch is not None and not (isinstance(self.branch, str) and repos._BRANCH.fullmatch(self.branch)):
            found.append("branch")
        if self.slice is not None and not isinstance(self.slice, dict):
            found.append("slice")
        if not isinstance(self.criteria_refs, list) or not all(isinstance(ref, str) for ref in self.criteria_refs):
            found.append("criteria_refs")
        if not isinstance(self.flows, dict) or not all(isinstance(path, str) and _sha_or_none(sha)
                                                       for path, sha in self.flows.items()):
            found.append("flows")
        if not isinstance(self.limits, dict) or not set(self.limits) <= set(LIMITS) or \
                not all(_positive(value) for value in self.limits.values()):
            found.append("limits")
        return found

    def to_dict(self, now: datetime | None = None) -> dict:
        """The file's content; `ValueError` naming every field that does not hold."""
        if problems := self.problems():
            raise ValueError("the run state is invalid: " + ", ".join(problems))
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return {"version": VERSION, "run_id": self.run_id, "mode": self.mode, "repository": self.repository,
                "pinned": self.pinned, "runner_commit": self.runner_commit, "stage": self.stage,
                "stages_done": list(self.stages_done), "issue": self.issue, "branch": self.branch, "pr": self.pr,
                "base": self.base, "head": self.head, "slice": self.slice, "criteria_refs": list(self.criteria_refs),
                "flows": dict(self.flows), "persona_source": f"git show {self.pinned}:<path>",
                "limits": dict(self.limits), "updated": moment.isoformat(timespec="seconds")}


def write_state(run_dir: str | os.PathLike[str], state: RunState, copy_dir: str | os.PathLike[str], *,
                now: datetime | None = None, known: Iterable[str] = ()) -> dict:
    """Write ``<run_dir>/state.json`` and the read-only ``<copy_dir>/state.json``, both atomically
    and masked with ``known``; returns what was written."""
    data = records.mask(state.to_dict(now), list(known))
    write_json(Path(run_dir) / STATE_NAME, data)
    copy = private_dir(Path(copy_dir)) / STATE_NAME
    if copy.exists() and not copy.is_symlink():
        os.chmod(copy, 0o600)  # replaced atomically below; the old copy was read-only
    atomic_private(copy, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.chmod(copy, 0o400)
    return data


def read_state(run_dir: str | os.PathLike[str]) -> dict | None:
    """``<run_dir>/state.json``, or None when it is missing or unreadable."""
    try:
        data = json.loads((Path(run_dir) / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None
