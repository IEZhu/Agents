"""Authenticate a complete, already acquired protected-file snapshot.

The finite ``agents-core/protected-files/1`` JSON shape is::

    {"schema": "agents-core/protected-files/1",
     "protected_roots": ["src"],
     "files": [{"path": "src/runtime.py", "class": "runtime",
                "type": "regular", "executable": false}, ...],
     "exclusions": [{"path": "src/cache", "type": "directory",
                     "reason": "Generated cache",
                     "load_isolation": "outside-load-path"}],
     "dependencies": {
         "environment": {"boundary": "external", "identity": "<sha256>"},
         "artifacts": [{"path": "packages/runtime.whl", "sha256": "<sha256>"}]}}

All shown keys are required; no additional keys are accepted. Roots, files,
exclusions and artifacts are sorted, unique portable paths. Roots and directory
exclusions denote exact subtrees, never globs. Files include this inventory at
``integrity/protected-files.json`` (normally class ``configuration``), and every
listed file must be sealed. Every nonexcluded release resource inside protected
roots must be listed. Unrelated sealed documentation outside those roots need
not be inventoried or observed. File classes are instruction,
runtime, configuration and dependency. Exclusion types are regular/directory;
the only isolation claim is outside-load-path. No exclusion may cover a listed protected
file or an entire protected root. Dependency environment boundaries are external
or protected-set. The latter requires each artifact to be a signed dependency
file with the identical SHA-256; external identities are signed provenance only.

On POSIX, observed executable is the boolean presence of *any* executable bit.
On Windows it must be None: POSIX bits have no invented Windows meaning. Both
platforms permit only regular files, forbid any symlink/reparse component, and
check every supplied file under the same inventory/load isolation policy.

This module does no acquisition or load-path enforcement. enumeration_complete
is an explicit caller attestation about the protected roots; required paths and
roots come from independent caller policy, never from candidate inventory alone.
A valid result authenticates supplied bytes/metadata and signed identities. It
does not establish key/root authorization, actual filesystem acquisition,
external environment enrollment or the truth of an exclusion's isolation claim.
Those belong to the trusted caller and subsequent policy/runtime layers.
"""
from __future__ import annotations

import hashlib
from bisect import bisect_left
from dataclasses import dataclass

from .common import (DEFAULT_LIMITS, HEX_DIGEST, Limits, SealResult, Status,
                     load_json, text, validate_path, validate_paths)
from .oms import PROFILE, verify_bytes

INVENTORY_PATH = "integrity/protected-files.json"
INVENTORY_SCHEMA = "agents-core/protected-files/1"
_CLASSES = {"instruction", "runtime", "configuration", "dependency"}


@dataclass(frozen=True)
class ObservedFile:
    """Immutable acquisition record; None bytes explicitly represent absence.

    Link-component records contain root-relative ancestor/self paths. Empty
    tuples attest no such components; acquisition must establish that fact.
    Types and bounds are checked by verify_protected_set, without coercion.
    """

    path: str
    data: bytes | None
    file_type: str
    executable: bool | None
    symlink_components: tuple[str, ...] = ()
    reparse_components: tuple[str, ...] = ()

    def __post_init__(self):
        if (type(self.path) is not str or type(self.file_type) is not str or
            (self.data is not None and type(self.data) is not bytes) or
            (self.executable is not None and type(self.executable) is not bool) or
            type(self.symlink_components) is not tuple or
            type(self.reparse_components) is not tuple or
            any(type(p) is not str for p in self.symlink_components + self.reparse_components)):
            raise TypeError("ObservedFile requires immutable, explicitly typed fields")


@dataclass(frozen=True)
class CoverageIssue:
    status: Status
    reason: str
    path: str | None = None


@dataclass(frozen=True)
class CoverageResult:
    status: Status
    reason: str
    issues: tuple[CoverageIssue, ...]
    seal: SealResult
    files: tuple[ObservedFile, ...]
    enumeration_complete: bool
    inventory_digest: str | None = None

    @property
    def authorization_checked(self) -> bool:
        """Preserve prior authorization state; identities never grant authority."""
        return self.seal.authorization_checked


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("Unexpected or missing inventory fields")
    return value


def _list(value, limits, *, nonempty=False):
    if type(value) is not list or len(value) > limits.resources or (nonempty and not value):
        raise ValueError("Expected a bounded inventory list")
    return value


def _paths(values, limits, *, sorted_paths=True):
    names = validate_paths(values, limits)
    if sorted_paths and list(names) != sorted(names):
        raise ValueError("Inventory paths must be sorted")
    return names


def _inside(path, root):
    return path == root or path.startswith(root + "/")


def _ancestors(path):
    parts = path.split("/")
    return ("/".join(parts[:count]) for count in range(1, len(parts) + 1))


def _covered(path, directories):
    return any(part in directories for part in _ancestors(path))


def _has_descendant(path, sorted_paths):
    prefix = path + "/"
    index = bisect_left(sorted_paths, prefix)
    return index < len(sorted_paths) and sorted_paths[index].startswith(prefix)


def _component_spellings(paths):
    """Catch directory aliases across otherwise distinct root/file namespaces."""
    spellings = {}
    for path in paths:
        parts = path.split("/")
        for count in range(1, len(parts) + 1):
            prefix = "/".join(parts[:count])
            key = prefix.casefold()
            if key in spellings and spellings[key] != prefix:
                raise ValueError("Platform-aliasing path components")
            spellings[key] = prefix


def _parse_inventory(data, limits):
    value = _object(load_json(data, min(limits.file_bytes, limits.payload_bytes)),
                    {"schema", "protected_roots", "files", "exclusions", "dependencies"})
    if value["schema"] != INVENTORY_SCHEMA:
        raise ValueError("Unsupported protected inventory schema")
    roots = _paths(_list(value["protected_roots"], limits, nonempty=True), limits)
    entries = _list(value["files"], limits, nonempty=True)
    for entry in entries:
        _object(entry, {"path", "class", "type", "executable"})
        if (type(entry["class"]) is not str or entry["class"] not in _CLASSES or
            entry["type"] != "regular" or type(entry["executable"]) is not bool):
            raise ValueError("Unsupported protected-file metadata")
    files = _paths([entry["path"] for entry in entries], limits)
    if INVENTORY_PATH not in files:
        raise ValueError("Inventory must protect its own path and metadata")
    if any(not _has_descendant(root, files) for root in roots):
        raise ValueError("Protected roots must enumerate at least one file")
    if any(root in files for root in roots):
        raise ValueError("A protected root must be a directory")
    exclusions = _list(value["exclusions"], limits)
    for exclusion in exclusions:
        _object(exclusion, {"path", "type", "reason", "load_isolation"})
        if (exclusion["type"] not in {"regular", "directory"} or
            exclusion["load_isolation"] != "outside-load-path"):
            raise ValueError("Unsupported exclusion or load-isolation rule")
        text(exclusion["reason"], maximum=4096)
    excluded_paths = _paths([entry["path"] for entry in exclusions], limits)
    file_set, root_set = set(files), set(roots)
    for exclusion in exclusions:
        path = exclusion["path"]
        if (_covered(path, file_set) or
            (exclusion["type"] == "directory" and _has_descendant(path, files))):
            raise ValueError("Exclusion intersects a signed file")
        if path in root_set or (exclusion["type"] == "directory" and _has_descendant(path, roots)):
            raise ValueError("Exclusion covers a protected root")
    dependencies = _object(value["dependencies"], {"environment", "artifacts"})
    environment = _object(dependencies["environment"], {"boundary", "identity"})
    if (environment["boundary"] not in {"external", "protected-set"} or
        type(environment["identity"]) is not str or not HEX_DIGEST.fullmatch(environment["identity"])):
        raise ValueError("Expected explicit dependency environment boundary and content identity")
    artifacts = _list(dependencies["artifacts"], limits, nonempty=True)
    for artifact in artifacts:
        _object(artifact, {"path", "sha256"})
        if type(artifact["sha256"]) is not str or not HEX_DIGEST.fullmatch(artifact["sha256"]):
            raise ValueError("Expected dependency artifact SHA-256")
    artifact_paths = _paths([artifact["path"] for artifact in artifacts], limits)
    _component_spellings((*roots, *files, *excluded_paths))
    # External artifact names belong to a different environment namespace.
    _component_spellings(artifact_paths)
    return value


def _finish(issues, seal, files, complete, digest):
    priority = {Status.VALID: 0, Status.UNSIGNED: 1, Status.TAMPERED: 2,
                Status.UNTRUSTED: 3, Status.INVALID: 4}
    status = max((issue.status for issue in issues), default=Status.VALID,
                 key=priority.__getitem__)
    reason = next((issue.reason for issue in issues if issue.status == status), "protected_set_valid")
    return CoverageResult(status, reason, tuple(issues), seal, files, complete, digest)


def verify_protected_set(release: SealResult, inventory_bytes: bytes | None,
                         observed_files: tuple[ObservedFile, ...], *,
                         enumeration_complete: bool,
                         minimum_paths: tuple[str, ...],
                         minimum_roots: tuple[str, ...],
                         platform: str = "posix",
                         limits: Limits = DEFAULT_LIMITS) -> CoverageResult:
    """Verify the entire protected scope and supplied additions, never a subset.

    Input release must be a signature-verified release-profile result from
    verify_seal. Authenticate inventory_bytes via verify_bytes before parsing.
    Observations must include the inventory with those identical bytes and every
    listed file, even listed files outside enumerated roots. All nonexcluded
    release resources inside protected roots must be inventoried and observed;
    unrelated release documentation outside those roots need not be. Required minima are bounded
    nonempty tuples supplied independently by the caller; required roots may be
    covered by a signed ancestor root. Complete enumeration is mandatory, and
    asserted completeness is retained without claiming acquisition verification.

    All independent observed/path/content failures survive in issues. Overall
    precedence is invalid, untrusted, tampered, unsigned, valid. Unsupported file
    types and unsafe names/components are invalid; absent/changed bytes or POSIX
    executable mismatch are tampered; absent signed coverage is unsigned.
    Aggregate byte/component bounds stop comparison before hashing observation
    payloads. The separately bounded inventory is authenticated first; metadata
    diagnostics collected within the bounds survive a resource-limit refusal.
    """
    issues = []
    authenticated = verify_bytes(release, INVENTORY_PATH, inventory_bytes, limits=limits)
    if authenticated.status != Status.VALID:
        issues.append(CoverageIssue(authenticated.status, authenticated.reason, INVENTORY_PATH))
    elif (authenticated.profile != PROFILE or authenticated.seal_kind != "release" or
          authenticated.signature_verified is not True):
        issues.append(CoverageIssue(Status.INVALID, "release_profile_required", INVENTORY_PATH))
    if type(enumeration_complete) is not bool or not enumeration_complete:
        issues.append(CoverageIssue(Status.INVALID, "enumeration_incomplete"))
    complete = enumeration_complete is True
    if type(platform) is not str or platform not in {"posix", "windows"}:
        issues.append(CoverageIssue(Status.INVALID, "unsupported_platform"))
    minima_valid = True
    try:
        for names in (minimum_paths, minimum_roots):
            if type(names) is not tuple or not names:
                raise ValueError("Caller minimum paths and roots must be explicit nonempty tuples")
            _paths(names, limits, sorted_paths=False)
        _component_spellings((*minimum_paths, *minimum_roots))
    except (ValueError, TypeError, UnicodeError):
        minima_valid = False
        issues.append(CoverageIssue(Status.INVALID, "minimum_scope_invalid"))
    if type(observed_files) is not tuple or len(observed_files) > limits.resources:
        issues.append(CoverageIssue(Status.INVALID, "observed_set_invalid"))
        return _finish(issues, authenticated, (), complete, None)
    observed = {}
    total = 0
    component_total = 0
    for entry in observed_files:
        if type(entry) is not ObservedFile:
            issues.append(CoverageIssue(Status.INVALID, "observation_type_invalid"))
            continue
        try:
            validate_path(entry.path, limits)
        except (ValueError, UnicodeError):
            issues.append(CoverageIssue(Status.INVALID, "observed_path_invalid", entry.path))
            continue
        if entry.path in observed:
            issues.append(CoverageIssue(Status.INVALID, "observation_duplicate", entry.path))
        else:
            observed[entry.path] = entry
        if entry.data is not None:
            total += len(entry.data)
            if len(entry.data) > limits.file_bytes:
                issues.append(CoverageIssue(Status.INVALID, "file_size_limit", entry.path))
        if entry.file_type != "regular":
            issues.append(CoverageIssue(Status.INVALID, "unsupported_file_type", entry.path))
        if ((platform == "posix" and type(entry.executable) is not bool) or
            (platform == "windows" and entry.executable is not None)):
            issues.append(CoverageIssue(Status.INVALID, "executable_metadata_invalid", entry.path))
        for kind, components in (("symlink", entry.symlink_components),
                                 ("reparse", entry.reparse_components)):
            component_total += len(components)
            if component_total > limits.resources:
                issues.append(CoverageIssue(Status.INVALID, "component_count_limit", entry.path))
                continue
            try:
                for component in components:
                    validate_path(component, limits)
                    if not _inside(entry.path, component):
                        raise ValueError("Component does not belong to observed path")
                if len(set(components)) != len(components) or list(components) != sorted(components):
                    raise ValueError("Component records must be sorted and unique")
            except (ValueError, UnicodeError):
                issues.append(CoverageIssue(Status.INVALID, "component_metadata_invalid", entry.path))
            if components:
                issues.append(CoverageIssue(Status.INVALID, kind + "_component", entry.path))
    if total > limits.total_bytes:
        issues.append(CoverageIssue(Status.INVALID, "total_size_limit"))
    try:
        _paths(list(observed), limits, sorted_paths=False)
        _component_spellings(observed)
    except (ValueError, TypeError, UnicodeError):
        issues.append(CoverageIssue(Status.INVALID, "observed_path_alias"))
    if total > limits.total_bytes or component_total > limits.resources:
        return _finish(issues, authenticated, observed_files, complete, None)
    if authenticated.status != Status.VALID or any(i.reason == "release_profile_required" for i in issues):
        return _finish(issues, authenticated, observed_files, complete, None)
    try:
        inventory = _parse_inventory(authenticated.data, limits)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        issues.append(CoverageIssue(Status.INVALID, "inventory_invalid", INVENTORY_PATH))
        return _finish(issues, authenticated, observed_files, complete, None)
    digest = hashlib.sha256(authenticated.data).hexdigest()
    entries = {entry["path"]: entry for entry in inventory["files"]}
    resources = {resource.name: resource for resource in authenticated.resources}
    # SealResult is the trusted parser's output, not a second unsigned manifest.
    try:
        names = [resource.name for resource in authenticated.resources]
        _paths(names, limits)
        if any(type(r.digest) is not str or not HEX_DIGEST.fullmatch(r.digest)
               for r in authenticated.resources):
            raise ValueError("Malformed release resource digest")
    except (ValueError, TypeError, UnicodeError):
        issues.append(CoverageIssue(Status.INVALID, "release_resources_invalid"))
    roots = set(inventory["protected_roots"])
    exclusions = inventory["exclusions"]
    excluded_files = {e["path"] for e in exclusions if e["type"] == "regular"}
    excluded_roots = {e["path"] for e in exclusions if e["type"] == "directory"}

    def excluded(path):
        return path in excluded_files or _covered(path, excluded_roots)

    protected_resources = {path for path in resources if _covered(path, roots) and not excluded(path)}
    for path in sorted(entries.keys() - resources.keys()):
        issues.append(CoverageIssue(Status.UNSIGNED, "inventory_path_not_signed", path))
    for path in sorted(protected_resources - entries.keys()):
        issues.append(CoverageIssue(Status.INVALID, "signed_path_not_in_inventory", path))
    if minima_valid:
        for path in minimum_paths:
            if path not in entries:
                issues.append(CoverageIssue(Status.UNSIGNED, "minimum_path_not_covered", path))
        for root in minimum_roots:
            if not _covered(root, roots):
                issues.append(CoverageIssue(Status.UNSIGNED, "minimum_root_not_covered", root))
        try:
            _paths(sorted(set(entries) | set(observed)), limits)
            _component_spellings((*roots, *entries, *resources, *observed, *minimum_paths, *minimum_roots,
                                  *(e["path"] for e in inventory["exclusions"])))
        except ValueError:
            issues.append(CoverageIssue(Status.INVALID, "scope_path_alias"))
    for path in sorted(set(entries) | protected_resources | (resources.keys() & observed.keys())):
        observation = observed.get(path)
        if observation is None:
            issues.append(CoverageIssue(Status.TAMPERED, "file_missing", path))
            continue
        check = verify_bytes(authenticated, path, observation.data, limits=limits)
        if check.status != Status.VALID:
            issues.append(CoverageIssue(check.status, check.reason, path))
        if (path in entries and platform == "posix" and type(observation.executable) is bool and
            observation.executable != entries[path]["executable"]):
            issues.append(CoverageIssue(Status.TAMPERED, "executable_mismatch", path))
        if path == INVENTORY_PATH and observation.data != authenticated.data:
            issues.append(CoverageIssue(Status.TAMPERED, "inventory_observation_mismatch", path))
    for path in sorted(observed.keys() - entries.keys() - resources.keys()):
        if not excluded(path):
            issues.append(CoverageIssue(Status.UNSIGNED, "observed_path_not_covered", path))
    dependencies = inventory["dependencies"]
    if dependencies["environment"]["boundary"] == "protected-set":
        for artifact in dependencies["artifacts"]:
            path = artifact["path"]
            if (path not in entries or entries[path]["class"] != "dependency" or path not in resources):
                issues.append(CoverageIssue(Status.UNSIGNED, "dependency_not_covered", path))
            elif artifact["sha256"] != resources[path].digest:
                issues.append(CoverageIssue(Status.INVALID, "dependency_identity_mismatch", path))
        try:
            _component_spellings((*entries, *(a["path"] for a in dependencies["artifacts"])))
        except ValueError:
            issues.append(CoverageIssue(Status.INVALID, "dependency_path_alias"))
    return _finish(issues, authenticated, observed_files, complete, digest)
