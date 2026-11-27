"""Builds a known-answer control set from one pinned commit of a repository.

docs/verifier-accuracy-measurement.md, "The control set": one item per source file, half
true and half false, a false item being one perturbation of a true statement that is not
itself in the set (one item per file guarantees that), clusters by source directory two
levels deep, and at least half of each class decidable by running something. Each label is
fixed by construction and confirmed by a deterministic check against the snapshot; a
statement the check cannot decide goes to the owner's list and stays out of the set.

Files are drawn round-robin across clusters, so no directory dominates the set, and a
statement whose text would raise the worker's model tier (`model_tiers.RISKS`) is skipped,
so that every run of one arm shares one configuration.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import subprocess
import tarfile
from typing import Callable

from evals.verifier_accuracy import control, strata
from src.engine.model_tiers import RISKS

EXCLUDED_PREFIXES = ("data/", "evals/datasets/", "evals/fixtures/", "evals/reports/",
                     "evals/verifier_accuracy/", ".git/")
HISTORY_COMMITS = 30


class BuildError(RuntimeError):
    """The set could not be built as the design requires."""


@dataclass(frozen=True)
class Item:
    claim: strata.Claim
    truth: bool
    kind: str  # "true" or the perturbation
    source: str
    cluster: str


def _git(repo: Path, *arguments: str, binary: bool = False):
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, check=False)
    if result.returncode != 0:
        raise BuildError(f"git {' '.join(arguments)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")


def resolve_commit(repo: Path, commit: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()


def _make_read_only(root: Path) -> None:
    for directory, subdirectories, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o444)
        Path(directory).chmod(0o555)


def _make_writable(root: Path) -> None:
    for directory, _, files in os.walk(root):
        Path(directory).chmod(0o755)
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o644)


def export_snapshot(repo: Path, commit: str, snapshots: Path) -> Path:
    """A read-only export of ``commit`` (no ``.git``), reused once complete."""
    snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = snapshots / commit
    marker = snapshots / f"{commit}.complete"
    if marker.exists() and target.is_dir():
        return target
    if target.exists():
        _make_writable(target)
        shutil.rmtree(target)
    target.mkdir(mode=0o700)
    archive = _git(repo, "archive", "--format=tar", commit, binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        # No links: the worker may read only what the commit holds, and a link could point at
        # any file of the machine, the service-state directory included.
        members = [member for member in tar.getmembers() if not (member.issym() or member.islnk())]
        if hasattr(tarfile, "data_filter"):  # Python 3.11.4 and later
            tar.extractall(target, members=members, filter="data")
        else:  # the archive is git's own export of a local commit
            tar.extractall(target, members=members)
    _make_read_only(target)
    marker.write_text(commit + "\n", encoding="utf-8")
    return target


def snapshot_files(snapshot: Path) -> tuple[list[str], set[str]]:
    files, directories = [], set()
    for directory, subdirectories, names in os.walk(snapshot):
        relative = Path(directory).relative_to(snapshot).as_posix()
        if relative != ".":
            directories.add(relative)
        for name in names:
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                files.append(path.relative_to(snapshot).as_posix())
    return sorted(files), directories


def history(repo: Path, commit: str):
    """Texts of a file at earlier commits that changed it, newest first."""
    def earlier(path: str):
        hashes = _git(repo, "log", "--format=%H", "-n", str(HISTORY_COMMITS), commit, "--", path).split()
        for older in hashes[1:]:  # the first commit is the one the snapshot's content comes from
            try:
                yield _git(repo, "show", f"{older}:{path}")
            except BuildError:
                return  # renamed or removed before this commit
    return earlier


def recorded_paths(store: Path | None, files: set[str]) -> set[str]:
    """Snapshot files that recorded claims mention, so their subjects resemble production."""
    if store is None or not store.exists():
        return set()
    mentioned = set()
    token = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+")
    for row in control.read_jsonl(store):
        text = " ".join(str(row.get(key, "")) for key in ("subject", "claim"))
        mentioned.update(candidate for candidate in token.findall(text) if candidate in files)
    return mentioned


def _eligible(path: str, snapshot: Path) -> str | None:
    if path.startswith(EXCLUDED_PREFIXES):
        return None
    limit = strata.MAX_LOCK_BYTES if path.endswith(".lock") else strata.MAX_FILE_BYTES
    file = snapshot / path
    if file.stat().st_size > limit:
        return None
    try:
        return file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _risky(claim: strata.Claim) -> bool:
    """A statement that would raise the worker's tier, or name the control directory a verifier
    must never be sent towards (its runs would be discarded)."""
    folded = claim.statement.casefold()
    return control.DIRECTORY_NAME in folded or any(pattern.search(folded) for pattern in RISKS.values())


def _ordered(files: list[str], first: set[str], rng: random.Random) -> list[str]:
    """Files that recorded claims mention first, then round-robin across shuffled clusters."""
    clusters: dict[str, list[str]] = {}
    for path in files:
        clusters.setdefault(strata.cluster_of(path), []).append(path)
    for members in clusters.values():
        rng.shuffle(members)
        members.sort(key=lambda path: path not in first)  # stable: mentioned files lead their cluster
    names = sorted(clusters)
    rng.shuffle(names)
    order = []
    while any(clusters[name] for name in names):
        for name in names:
            if clusters[name]:
                order.append(clusters[name].pop(0))
    order.sort(key=lambda path: path not in first)
    return order


class _Selector:
    def __init__(self, items: int, rng: random.Random, snapshot: Path,
                 same_bundle: Callable[[str], bool] | None = None):
        self.target = items
        self.rng = rng
        self.snapshot = snapshot
        self.same_bundle = same_bundle
        self.need = {True: items // 2, False: items - items // 2}
        self.quota = {stratum: math.ceil(items / len(strata.STRATA)) for stratum in strata.STRATA}
        self.taken = Counter()
        self.kinds = Counter()
        self.items: list[Item] = []
        self.skipped = Counter()
        self.owner: list[dict] = []
        self._undecided: set[str] = set()

    def full(self) -> bool:
        return len(self.items) >= self.target

    def _decide(self, claim: strata.Claim, intended: bool, source: str) -> bool:
        decided = strata.check(claim, self.snapshot)
        if decided is None:
            if claim.statement not in self._undecided:
                self._undecided.add(claim.statement)
                self.skipped["undecided"] += 1
                self.owner.append({"source": source, "stratum": claim.stratum, "statement": claim.statement,
                                   "intended": intended, "reason": "the deterministic check cannot decide it"})
            return False
        if decided != intended:
            self.skipped["mismatch"] += 1
            return False
        return True

    def _variant(self, fact: strata.Fact) -> strata.Variant | None:
        variants = list(fact.variants)
        if fact.history is not None:
            variants += list(fact.history())
        variants = [variant for variant in variants if not _risky(variant.claim)]
        if not variants:
            return None
        least = min(self.kinds[variant.kind] for variant in variants)
        return self.rng.choice([variant for variant in variants if self.kinds[variant.kind] == least])

    def offer(self, path: str, available: dict[str, list[strata.Fact]]) -> None:
        classes = [truth for truth in (True, False) if self.need[truth] > 0]
        classes.sort(key=lambda truth: (-self.need[truth], self.rng.random()))
        open_strata = [stratum for stratum in available if self.quota[stratum] - self.taken[stratum] > 0]
        open_strata.sort(key=lambda stratum: (-(self.quota[stratum] - self.taken[stratum]), self.rng.random()))
        for stratum in open_strata:
            facts = [fact for fact in available[stratum] if not _risky(fact.claim)]
            self.skipped["risk_floor"] += len(available[stratum]) - len(facts)
            self.rng.shuffle(facts)
            for truth in classes:
                for fact in facts[:5]:  # a few tries per file; one file gives at most one item
                    if not self._decide(fact.claim, True, path):
                        continue
                    claim, kind = fact.claim, "true"
                    if not truth:
                        variant = self._variant(fact)
                        if variant is None or not self._decide(variant.claim, False, path):
                            continue
                        claim, kind = variant.claim, variant.kind
                    if self.same_bundle is not None and not self.same_bundle(claim.statement):
                        self.skipped["bundle_change"] += 1  # its package would be another configuration
                        continue
                    self.items.append(Item(claim, truth, kind, path, strata.cluster_of(path)))
                    self.need[truth] -= 1
                    self.taken[stratum] += 1
                    self.kinds[kind] += 1
                    return

    def unlimited(self) -> None:
        self.quota = {stratum: self.target for stratum in strata.STRATA}


def build(*, repo: Path, commit: str, control_dir: Path, snapshots: Path, name: str, items: int,
          seed: int, v2_per_class: int | None, installation_roots: list[Path],
          claims_store: Path | None = None,
          same_bundle: Callable[[Path], Callable[[str], bool]] | None = None) -> dict:
    """Write a new set into ``control_dir`` and return its manifest (aggregates only).

    ``same_bundle(snapshot)`` returns the check that a statement's packages keep the reference
    bundle; without it every statement is accepted.
    """
    if items < 2:
        raise BuildError("A set needs at least two items.")
    repo = repo.resolve()
    commit = resolve_commit(repo, commit)
    snapshot = (snapshots / commit).resolve()
    control.ensure_separate(control_dir, installation_roots=installation_roots, checkouts=[repo, snapshot])
    control.open_private(control_dir)
    if (control_dir / control.STATEMENTS).exists():
        raise BuildError(f"{control_dir} already holds a set; discard it first.")
    snapshot = export_snapshot(repo, commit, snapshots)
    files, directories = snapshot_files(snapshot)
    file_set = set(files)
    rng = random.Random(seed)
    earlier = history(repo, commit)
    selector = _Selector(items, rng, snapshot, same_bundle(snapshot) if same_bundle else None)
    order = _ordered(files, recorded_paths(claims_store, file_set), rng)
    texts: dict[str, str | None] = {}
    for unlimited in (False, True):  # a second pass lifts the stratum quotas when rare strata run out
        if unlimited:
            selector.unlimited()
        for path in order:
            if selector.full():
                break
            if path in {item.source for item in selector.items}:
                continue
            if path not in texts:
                texts[path] = _eligible(path, snapshot)
            if texts[path] is None:
                continue
            available = strata.extract(path, texts[path], directories=directories, files=file_set, earlier=earlier)
            selector.offer(path, available)
        if selector.full():
            break
    if not selector.full():
        raise BuildError(f"Only {len(selector.items)} of {items} items could be built from {commit}.")
    chosen = list(selector.items)
    rng.shuffle(chosen)  # item IDs say nothing about class, stratum or cluster
    checkable = {truth: [item for item in chosen if item.truth == truth
                         and item.claim.stratum in strata.COMMAND_CHECKABLE] for truth in (True, False)}
    for truth in (True, False):
        size = sum(item.truth == truth for item in chosen)
        if 2 * len(checkable[truth]) < size:
            raise BuildError(f"Fewer than half of the {'true' if truth else 'false'} items are command-checkable.")
    v2 = {id(item) for truth in (True, False)
          for item in (checkable[truth] if v2_per_class is None else checkable[truth][:v2_per_class])}
    statements, labels = [], []
    for index, item in enumerate(chosen, start=1):
        identifier = f"i{index:03d}"
        statements.append({"item": identifier, "statement": item.claim.statement, "stratum": item.claim.stratum,
                           "cluster": item.cluster, "command_checkable": item.claim.stratum in strata.COMMAND_CHECKABLE,
                           "v2": id(item) in v2})
        labels.append({"item": identifier, "truth": item.truth, "kind": item.kind, "source": item.source,
                       "stratum": item.claim.stratum, "subject": item.claim.subject_dict(), "check": "agreed"})
    truth_of = {row["item"]: row["truth"] for row in labels}

    def by_class(key: str) -> dict:
        return {name: sum(1 for row in statements if row[key] and truth_of[row["item"]] is truth)
                for name, truth in (("true", True), ("false", False))}

    with control.exclusive(control_dir, "writing a set"):
        if (control_dir / control.STATEMENTS).exists():  # another build finished in the meantime
            raise BuildError(f"{control_dir} already holds a set; discard it first.")
        control.write_jsonl(control_dir / control.STATEMENTS, statements)
        control.write_jsonl(control_dir / control.LABELS, labels)
        control.write_jsonl(control_dir / control.OWNER_REVIEW, selector.owner)
        control.write_private(control_dir / control.CANARY, f"canary {secrets.token_hex(16)}\n")
        version = control.set_version(control_dir)
        manifest = {
            "name": name, "version": version, "set": f"{name}@{version}", "commit": commit,
            "snapshot": str(snapshot), "repository": str(repo), "created_at": control.now(), "seed": seed,
            "items": len(chosen), "classes": {"true": items // 2, "false": items - items // 2},
            "strata": dict(Counter(row["stratum"] for row in statements)),
            "clusters": dict(Counter(row["cluster"] for row in statements)),
            "perturbations": dict(Counter(item.kind for item in chosen if not item.truth)),
            "command_checkable": by_class("command_checkable"), "v2": by_class("v2"),
            "skipped": dict(selector.skipped), "owner_review": len(selector.owner),
            "labels_sha256": control.file_digest(control_dir / control.LABELS),
        }
        control.write_private(control_dir / control.MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def discard(control_dir: Path) -> Path:
    """Move the current set aside, inside the control directory, so workers stay denied it.

    The target is new every time: the set version from the statement file (or ``incomplete``
    when a build stopped before writing it) and the time of the discard, so a rebuilt set
    with the same version never overwrites the records of an earlier one. A run of the set that
    is going holds its run lock, and the discard is refused, so no run splits across two sets.
    """
    with control.exclusive(control_dir, "discarding the set"):
        version = (control.set_version(control_dir) if (control_dir / control.STATEMENTS).exists()
                   else "incomplete")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = control_dir / control.DISCARDED / f"{version}-{stamp}"
        if target.exists():
            raise BuildError(f"{target} already exists")
        control.open_private(control_dir / control.DISCARDED)
        control.open_private(target)
        for name in (control.STATEMENTS, control.LABELS, control.ATTEMPTS, control.PROBES, control.OWNER_REVIEW,
                     control.CANARY, control.MANIFEST, control.SIMULATION):
            source = control_dir / name
            if source.exists():
                os.replace(source, target / name)
    return target
