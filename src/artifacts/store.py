"""Immutable text artifacts, persisted independently of ephemeral state backends.

An artifact's content and original descriptor are published by one directory
rename. A stable store lock serializes publication and quota accounting across
processes. Manifests pin descriptors and hashes; release only adds a tombstone.
There is deliberately no garbage collection in this first storage format.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable
import unicodedata
import uuid

from src.file_lock import file_lock


_SCHEMA = "agents-core/artifact-manifest/1"
_IS_WINDOWS = os.name == "nt"
_MEDIA_TYPES = {"text/plain", "text/markdown", "application/json"}
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_ARTIFACT_ID = re.compile(r"art_([a-f0-9]{64})\Z")
_MANIFEST_ID = re.compile(r"manifest_[a-f0-9]{32}\Z")
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DESCRIPTOR_KEYS = {"artifact_id", "sha256", "size_bytes", "media_type", "name"}
_MANIFEST_KEYS = {"schema", "manifest_id", "workspace_sha256", "task_id", "created_at",
                  "expires_at", "entries", "sha256"}
_MAX_METADATA_BYTES = 4096
_MAX_MANIFEST_BYTES = 262144
_MAX_ENTRIES = 32
_MAX_STAGING_ENTRIES = 32
# Eight-character suffixes were used by tempfile in the first store revision.
# Current artifact directories and atomic JSON files use UUID hex suffixes.
_ARTIFACT_STAGING = re.compile(r"\.tmp-(?:[a-z0-9_]{8}|[a-f0-9]{32})\Z")
_JSON_STAGING = re.compile(r"\.tmp-[a-f0-9]{32}\Z")
_MESSAGES = {
    "invalid_input": "The artifact request is invalid.",
    "artifact_too_large": "The artifact exceeds the byte limit.",
    "quota_exceeded": "The artifact store quota has been reached.",
    "artifact_unavailable": "The artifact or manifest is unavailable.",
    "integrity_error": "Stored artifact integrity verification failed.",
    "permission_denied": "The source or storage path is not permitted.",
    "source_changed": "The source changed during capture.",
    "store_unavailable": "The artifact store is unavailable.",
    "store_busy": "The artifact store is busy; retry the request.",
    "unsupported": "Safe filesystem access is unavailable on this platform.",
}


class ArtifactError(Exception):
    """A public, non-sensitive storage failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _error(code: str) -> ArtifactError:
    return ArtifactError(code, _MESSAGES[code])


def _windows_acl():
    # The daemon's ACL helper has no service, inference or configuration side effects.
    from src.daemon import acl
    return acl


def _reparse(info) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _reject_constant(value: str):
    raise ValueError("Non-finite JSON number")


def _json(data: bytes):
    return json.loads(data.decode("utf-8"), parse_float=_finite_float,
                      parse_constant=_reject_constant)


def _digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _text(value: object, maximum: int) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _name(value: object) -> bool:
    return (_text(value, 120) and "/" not in value and "\\" not in value
            and not any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in value))


def _identity(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _timestamp(value: float) -> str:
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        raise _error("invalid_input") from None


def _parse_timestamp(value: object) -> datetime:
    if not _text(value, 40) or not (value.endswith("Z") or value.endswith("+00:00")):
        raise ValueError("Invalid UTC timestamp")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("Invalid UTC timestamp")
    return parsed


class ArtifactStore:
    def __init__(self, directory, *, clock: Callable[[], float] = time.time,
                 max_artifact_bytes: int = 4194304, max_workspace_bytes: int = 67108864,
                 max_artifacts: int = 256, max_manifests: int = 256):
        limits = (max_artifact_bytes, max_workspace_bytes, max_artifacts, max_manifests)
        if any(type(value) is not int or value < 1 for value in limits):
            raise _error("invalid_input")
        self.directory = Path(os.path.abspath(os.fspath(directory)))
        if self.directory == Path(self.directory.anchor):
            raise _error("invalid_input")
        self.clock = clock
        self.max_artifact_bytes = max_artifact_bytes
        self.max_workspace_bytes = max_workspace_bytes
        self.max_artifacts = max_artifacts
        self.max_manifests = max_manifests

    def _now(self) -> float:
        try:
            now = self.clock()
            if type(now) not in (int, float) or not -62135596800 <= now <= 253402300799:
                raise _error("invalid_input")
            _timestamp(now)
            return now
        except Exception:
            raise _error("invalid_input") from None

    @staticmethod
    @contextmanager
    def _lease(path: Path):
        """Keep request threads bounded when another process holds the lock."""
        deadline = time.monotonic() + 2.0
        with ExitStack() as stack:
            while True:
                try:
                    lock_fd = stack.enter_context(file_lock(path, blocking=False))
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _error("store_busy") from None
                    time.sleep(min(0.01, remaining))
            yield lock_fd

    @staticmethod
    def _workspace(workspace: str) -> str:
        if (not isinstance(workspace, str) or not workspace or "\x00" in workspace
                or not os.path.isabs(workspace) or os.path.normpath(workspace) != workspace):
            raise _error("invalid_input")
        try:
            return hashlib.sha256(workspace.encode("utf-8")).hexdigest()
        except UnicodeEncodeError:
            raise _error("invalid_input") from None

    @staticmethod
    def _private(info, *, directory: bool, path: Path | None = None):
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not valid_type or _reparse(info) or (not directory and info.st_nlink != 1):
            raise _error("permission_denied")
        if _IS_WINDOWS:
            acl = _windows_acl()
            if (path is None or not acl.owned_by_user_or_admins(path)
                    or not acl.is_private(path, inherited_below=directory)):
                raise _error("permission_denied")
        elif info.st_mode & 0o077 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise _error("permission_denied")

    def _directory(self, path: Path, *, create: bool = True):
        """Reject symlinks in every component, including ancestors of the store."""
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current = current / component
            created = False
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    current.mkdir(mode=0o700)
                    created = True
                    self._sync(current.parent)
                except FileExistsError:
                    pass
                info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or _reparse(info):
                raise _error("permission_denied")
            if current == self.directory or self.directory in current.parents:
                if _IS_WINDOWS:
                    acl = _windows_acl()
                    if not acl.owned_by_user_or_admins(current):
                        raise _error("permission_denied")
                    if created and not acl.is_private(current, inherited_below=True):
                        acl.restrict(current)
                    elif current == self.directory and not created and not acl.is_private(current, inherited_below=True):
                        # Another first writer can observe the new directory in
                        # the brief interval before its creator sets the DACL.
                        # Wait for that owner; never rewrite an existing DACL.
                        deadline = time.monotonic() + 2.0
                        while not acl.is_private(current, inherited_below=True):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise _error("permission_denied")
                            time.sleep(min(0.01, remaining))
                self._private(info, directory=True, path=current)

    @contextmanager
    def _locked(self, workspace: str):
        namespace = self._workspace(workspace)
        try:
            self._directory(self.directory)
            lock_path = self.directory / ".lock"
            try:
                self._private(lock_path.lstat(), directory=False, path=lock_path)
            except FileNotFoundError:
                pass
            with self._lease(lock_path) as lock_fd:
                # The process-local fallback cannot protect durable quota accounting.
                if lock_fd is None:
                    raise _error("unsupported")
                self._private(os.fstat(lock_fd), directory=False, path=lock_path)
                self._directory(self.directory, create=False)
                root = self.directory / namespace
                for path in (root, root / "artifacts", root / "manifests", root / "revoked"):
                    self._directory(path)
                self._reconcile(root)
                yield root
        except ArtifactError:
            raise
        except OSError:
            raise _error("store_unavailable") from None

    @staticmethod
    def _read_file(path: Path, limit: int, *, missing: str = "integrity_error") -> bytes:
        fd = None
        try:
            flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_BINARY", 0))
            ArtifactStore._private(path.lstat(), directory=False, path=path)
            if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
                raise _error("permission_denied")
            fd = os.open(path, flags)
            before = os.fstat(fd)
            ArtifactStore._private(before, directory=False, path=path)
            if before.st_size > limit:
                raise _error("integrity_error")
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > limit or len(data) != before.st_size or _identity(before) != _identity(os.fstat(fd)):
                raise _error("integrity_error")
            return bytes(data)
        except FileNotFoundError:
            raise _error(missing) from None
        except OSError:
            raise _error("integrity_error") from None
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _write_file(path: Path, content: bytes):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(path, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _sync(path: Path):
        if not hasattr(os, "O_DIRECTORY"):
            return
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _atomic_file(self, path: Path, content: bytes):
        temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
        try:
            self._write_file(temporary, content)
            os.replace(temporary, path)
            self._sync(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _entries(path: Path, maximum: int, *, directories: bool) -> list[Path]:
        entries = []
        with os.scandir(path) as iterator:
            for entry in iterator:
                if len(entries) >= maximum:
                    raise _error("quota_exceeded")
                info = entry.stat(follow_symlinks=False)
                ArtifactStore._private(info, directory=directories, path=Path(entry.path))
                entries.append(Path(entry.path))
        return entries

    def _reconcile(self, root: Path):
        """Discard only bounded, structurally verified abandoned publications.

        This runs while holding the exclusive OS lock, so no live writer can own
        a staging entry. Partial writes need not parse as JSON or UTF-8, but their
        names, ownership, permissions, types, link counts and byte bounds must all
        match this store's staging format. Validate every category before deleting
        anything, and never treat a published artifact or manifest as disposable.
        """
        roots = self._entries(root, 3, directories=True)
        if {path.name for path in roots} != {"artifacts", "manifests", "revoked"}:
            raise _error("integrity_error")
        pending = []
        for category, maximum in (("artifacts", self.max_artifacts),
                                  ("manifests", self.max_manifests),
                                  ("revoked", self.max_manifests)):
            directories = category == "artifacts"
            published = 0
            staged = 0
            paths = self._entries(root / category, maximum + _MAX_STAGING_ENTRIES,
                                  directories=directories)
            for path in paths:
                info = path.lstat()
                if (_ARTIFACT_ID.fullmatch(path.name) if directories else
                        path.name.endswith(".json") and _MANIFEST_ID.fullmatch(path.name[:-5])):
                    published += 1
                    if published > maximum:
                        raise _error("quota_exceeded")
                    if not directories and info.st_size > (_MAX_MANIFEST_BYTES if category == "manifests" else _MAX_METADATA_BYTES):
                        raise _error("integrity_error")
                    continue
                pattern = _ARTIFACT_STAGING if directories else _JSON_STAGING
                if not pattern.fullmatch(path.name):
                    raise _error("integrity_error")
                staged += 1
                if staged > _MAX_STAGING_ENTRIES:
                    raise _error("quota_exceeded")
                children = []
                if directories:
                    for child in self._entries(path, 2, directories=False):
                        if child.name not in {"content", "metadata.json"}:
                            raise _error("integrity_error")
                        child_info = child.lstat()
                        limit = self.max_artifact_bytes if child.name == "content" else _MAX_METADATA_BYTES
                        if child_info.st_size > limit:
                            raise _error("integrity_error")
                        children.append((child, child_info))
                elif info.st_size > (_MAX_MANIFEST_BYTES if category == "manifests" else _MAX_METADATA_BYTES):
                    raise _error("integrity_error")
                pending.append((path, info, children, directories))
        # A concurrent non-cooperating filesystem change must fail before cleanup.
        for path, info, children, _ in pending:
            self._same_staging(path, info)
            for child, child_info in children:
                self._same_staging(child, child_info)
        for path, info, children, directories in pending:
            if directories:
                self._discard_staging_directory(path, info, children)
            else:
                self._same_staging(path, info)
                path.unlink()
            self._sync(path.parent)

    @staticmethod
    def _same_staging(path: Path, expected):
        current = path.lstat()
        if _identity(current) != _identity(expected) or current.st_mode != expected.st_mode:
            raise _error("integrity_error")
        ArtifactStore._private(current, directory=stat.S_ISDIR(expected.st_mode), path=path)

    def _discard_staging_directory(self, path: Path, info, children):
        self._same_staging(path, info)
        if os.unlink in os.supports_dir_fd:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                if _identity(os.fstat(fd)) != _identity(info):
                    raise _error("integrity_error")
                for child, child_info in children:
                    current = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                    if _identity(current) != _identity(child_info):
                        raise _error("integrity_error")
                    self._private(current, directory=False, path=child)
                    os.unlink(child.name, dir_fd=fd)
            finally:
                os.close(fd)
        else:
            # Owner-private parents and the exclusive lock protect this fallback;
            # recheck the directory before each child and never recurse into it.
            for child, child_info in children:
                self._same_staging(child, child_info)
                directory = path.lstat()
                self._private(directory, directory=True, path=path)
                if (directory.st_dev, directory.st_ino) != (info.st_dev, info.st_ino):
                    raise _error("integrity_error")
                child.unlink()
        current = path.lstat()
        self._private(current, directory=True, path=path)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise _error("integrity_error")
        path.rmdir()

    def _content(self, content: bytes, media_type: str):
        if not isinstance(content, bytes) or media_type not in _MEDIA_TYPES:
            raise _error("invalid_input")
        if len(content) > self.max_artifact_bytes:
            raise _error("artifact_too_large")
        try:
            content.decode("utf-8")
            if media_type == "application/json":
                _json(content)
        except (UnicodeError, ValueError, RecursionError):
            raise _error("invalid_input") from None

    def _descriptor(self, value: object) -> dict:
        if (not isinstance(value, dict) or set(value) != _DESCRIPTOR_KEYS
                or not _digest(value.get("sha256"))
                or value.get("artifact_id") != "art_" + value["sha256"]
                or type(value.get("size_bytes")) is not int
                or not 0 <= value["size_bytes"] <= self.max_artifact_bytes
                or not isinstance(value.get("media_type"), str)
                or value["media_type"] not in _MEDIA_TYPES or not _name(value.get("name"))):
            raise _error("integrity_error")
        return value

    def _artifact(self, root: Path, artifact_id: str, *, expected: dict | None = None) -> tuple[dict, bytes]:
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise _error("invalid_input")
        directory = root / "artifacts" / artifact_id
        try:
            self._directory(directory, create=False)
        except FileNotFoundError:
            raise _error("artifact_unavailable") from None
        files = self._entries(directory, 2, directories=False)
        if {file.name for file in files} != {"content", "metadata.json"}:
            raise _error("integrity_error")
        try:
            descriptor = self._descriptor(_json(self._read_file(directory / "metadata.json", _MAX_METADATA_BYTES)))
        except (UnicodeError, ValueError, RecursionError):
            raise _error("integrity_error") from None
        if descriptor["artifact_id"] != artifact_id or (expected is not None and descriptor != expected):
            raise _error("integrity_error")
        content = self._read_file(directory / "content", self.max_artifact_bytes)
        if len(content) != descriptor["size_bytes"] or hashlib.sha256(content).hexdigest() != descriptor["sha256"]:
            raise _error("integrity_error")
        try:
            self._content(content, descriptor["media_type"])
        except ArtifactError:
            raise _error("integrity_error") from None
        return descriptor, content

    def publish(self, workspace: str, content: bytes, *, name: str, media_type: str) -> dict:
        if not _name(name) or not isinstance(media_type, str):
            raise _error("invalid_input")
        self._content(content, media_type)
        sha256 = hashlib.sha256(content).hexdigest()
        descriptor = {"artifact_id": "art_" + sha256, "sha256": sha256,
                      "size_bytes": len(content), "media_type": media_type, "name": name}
        with self._locked(workspace) as root:
            artifacts = root / "artifacts"
            destination = artifacts / descriptor["artifact_id"]
            if destination.exists() or destination.is_symlink():
                return self._artifact(root, descriptor["artifact_id"])[0]
            entries = self._entries(artifacts, self.max_artifacts, directories=True)
            if len(entries) >= self.max_artifacts:
                raise _error("quota_exceeded")
            total = 0
            for entry in entries:
                stored, _ = self._artifact(root, entry.name)
                total += stored["size_bytes"]
            if total + len(content) > self.max_workspace_bytes:
                raise _error("quota_exceeded")
            staging = artifacts / (".tmp-" + uuid.uuid4().hex)
            staging.mkdir(mode=0o700)
            try:
                self._write_file(staging / "content", content)
                self._write_file(staging / "metadata.json", _canonical(descriptor))
                self._sync(staging)
                os.replace(staging, destination)
                self._sync(artifacts)
            finally:
                if staging.exists():
                    (staging / "content").unlink(missing_ok=True)
                    (staging / "metadata.json").unlink(missing_ok=True)
                    staging.rmdir()
        return descriptor

    def capture(self, workspace: str, source_root: Path, source_path: str, *, name: str, media_type: str) -> dict:
        """Capture a regular file beneath a trusted root without following links."""
        self._workspace(workspace)
        if not _name(name) or not isinstance(media_type, str) or media_type not in _MEDIA_TYPES:
            raise _error("invalid_input")
        required_flags = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
        if (any(not hasattr(os, flag) for flag in required_flags)
                or os.open not in os.supports_dir_fd or os.name != "posix"):
            raise _error("unsupported")
        if (not isinstance(source_path, str) or not source_path or source_path.startswith("/")
                or "\x00" in source_path or "\\" in source_path
                or any(part in ("", ".", "..") for part in source_path.split("/"))):
            raise _error("permission_denied")
        source_root = Path(source_root)
        if not source_root.is_absolute() or ".." in source_root.parts:
            raise _error("permission_denied")
        directory = None
        source_fd = None
        try:
            directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            for part in (*source_root.parts[1:], *source_path.split("/")[:-1]):
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = next_fd
            source_fd = os.open(source_path.split("/")[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=directory)
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise _error("permission_denied")
            if before.st_size > self.max_artifact_bytes:
                raise _error("artifact_too_large")
            content = bytearray()
            while len(content) <= self.max_artifact_bytes:
                chunk = os.read(source_fd, min(65536, self.max_artifact_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > self.max_artifact_bytes:
                raise _error("artifact_too_large")
            if _identity(before) != _identity(os.fstat(source_fd)) or len(content) != before.st_size:
                raise _error("source_changed")
        except OSError:
            raise _error("permission_denied") from None
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if directory is not None:
                os.close(directory)
        return self.publish(workspace, bytes(content), name=name, media_type=media_type)

    @staticmethod
    def _manifest_id(manifest_id: str):
        if not isinstance(manifest_id, str) or not _MANIFEST_ID.fullmatch(manifest_id):
            raise _error("invalid_input")

    @staticmethod
    def _expected(expected_sha256: str):
        if not _digest(expected_sha256):
            raise _error("invalid_input")

    def _validate_manifest(self, value: object, namespace: str, manifest_id: str) -> dict:
        if isinstance(value, dict) and value.get("workspace_sha256") != namespace:
            raise _error("artifact_unavailable")
        if (not isinstance(value, dict) or set(value) != _MANIFEST_KEYS
                or value.get("schema") != _SCHEMA or value.get("manifest_id") != manifest_id
                or value.get("workspace_sha256") != namespace or not _digest(value.get("sha256"))
                or not isinstance(value.get("task_id"), str) or not _TASK_ID.fullmatch(value["task_id"])
                or not isinstance(value.get("entries"), list) or not 1 <= len(value["entries"]) <= _MAX_ENTRIES):
            raise _error("integrity_error")
        seen = set()
        for entry in value["entries"]:
            if (not isinstance(entry, dict) or set(entry) != {"artifact", "required", "purpose"}
                    or type(entry.get("required")) is not bool or not _text(entry.get("purpose"), 1000)):
                raise _error("integrity_error")
            descriptor = self._descriptor(entry["artifact"])
            if descriptor["artifact_id"] in seen:
                raise _error("integrity_error")
            seen.add(descriptor["artifact_id"])
        try:
            timestamps = []
            for key in ("created_at", "expires_at"):
                timestamps.append(_parse_timestamp(value[key]))
            if not 30 <= (timestamps[1] - timestamps[0]).total_seconds() <= 604800:
                raise ValueError()
            unsigned = {key: item for key, item in value.items() if key != "sha256"}
            if hashlib.sha256(_canonical(unsigned)).hexdigest() != value["sha256"]:
                raise ValueError()
        except (ValueError, TypeError, OverflowError, OSError, RecursionError):
            raise _error("integrity_error") from None
        return value

    def _manifest(self, root: Path, manifest_id: str, expected_sha256: str | None,
                  *, allow_released: bool = False) -> dict:
        self._manifest_id(manifest_id)
        if expected_sha256 is not None:
            self._expected(expected_sha256)
        try:
            value = _json(self._read_file(root / "manifests" / (manifest_id + ".json"),
                                          _MAX_MANIFEST_BYTES, missing="artifact_unavailable"))
            manifest = self._validate_manifest(value, root.name, manifest_id)
        except (UnicodeError, ValueError, RecursionError):
            raise _error("integrity_error") from None
        if expected_sha256 is not None and manifest["sha256"] != expected_sha256:
            raise _error("integrity_error")
        if not allow_released:
            revoked = root / "revoked" / (manifest_id + ".json")
            if revoked.exists() or revoked.is_symlink():
                raise _error("artifact_unavailable")
            expires = _parse_timestamp(manifest["expires_at"]).timestamp()
            if self._now() >= expires:
                raise _error("artifact_unavailable")
        return manifest

    def create_manifest(self, workspace: str, task_id: str, entries: list[dict], *, ttl_seconds=86400) -> dict:
        if (not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id)
                or not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_ENTRIES
                or type(ttl_seconds) not in (int, float) or not 30 <= ttl_seconds <= 604800):
            raise _error("invalid_input")
        seen = set()
        for entry in entries:
            if (not isinstance(entry, dict) or set(entry) != {"artifact_id", "sha256", "required", "purpose"}
                    or not isinstance(entry.get("artifact_id"), str)
                    or not _ARTIFACT_ID.fullmatch(entry["artifact_id"])
                    or not _digest(entry.get("sha256")) or type(entry.get("required")) is not bool
                    or not _text(entry.get("purpose"), 1000) or entry["artifact_id"] in seen):
                raise _error("invalid_input")
            seen.add(entry["artifact_id"])
        with self._locked(workspace) as root:
            manifests = self._entries(root / "manifests", self.max_manifests, directories=False)
            # Tombstones never outnumber the immutable manifests they revoke.
            self._entries(root / "revoked", self.max_manifests, directories=False)
            if len(manifests) >= self.max_manifests:
                raise _error("quota_exceeded")
            saved_entries = []
            for entry in entries:
                descriptor, _ = self._artifact(root, entry["artifact_id"])
                if descriptor["sha256"] != entry["sha256"]:
                    raise _error("integrity_error")
                saved_entries.append({"artifact": descriptor, "required": entry["required"], "purpose": entry["purpose"]})
            now = self._now()
            manifest = {"schema": _SCHEMA, "manifest_id": "manifest_" + uuid.uuid4().hex,
                        "workspace_sha256": root.name, "task_id": task_id,
                        "created_at": _timestamp(now), "expires_at": _timestamp(now + ttl_seconds),
                        "entries": saved_entries}
            manifest["sha256"] = hashlib.sha256(_canonical(manifest)).hexdigest()
            serialized = _canonical(manifest)
            if len(serialized) > _MAX_MANIFEST_BYTES:
                raise _error("invalid_input")
            self._atomic_file(root / "manifests" / (manifest["manifest_id"] + ".json"), serialized)
        return manifest

    def get_manifest(self, workspace: str, manifest_id: str, expected_sha256: str | None = None) -> dict:
        with self._locked(workspace) as root:
            return self._manifest(root, manifest_id, expected_sha256)

    def verify(self, workspace: str, manifest_id: str, expected_sha256: str) -> dict:
        self._expected(expected_sha256)
        with self._locked(workspace) as root:
            manifest = self._manifest(root, manifest_id, expected_sha256)
            artifacts = [self._artifact(root, entry["artifact"]["artifact_id"], expected=entry["artifact"])[0]
                         for entry in manifest["entries"]]
            return {"verified": True, "manifest_id": manifest_id, "sha256": manifest["sha256"],
                    "artifacts": artifacts}

    def read(self, workspace: str, manifest_id: str, expected_sha256: str, artifact_id: str,
             *, offset: int = 0, max_bytes: int = 16384) -> dict:
        self._expected(expected_sha256)
        if type(offset) is not int or offset < 0 or type(max_bytes) is not int or not 4 <= max_bytes <= 65536:
            raise _error("invalid_input")
        with self._locked(workspace) as root:
            manifest = self._manifest(root, manifest_id, expected_sha256)
            descriptor = next((entry["artifact"] for entry in manifest["entries"]
                               if entry["artifact"]["artifact_id"] == artifact_id), None)
            if descriptor is None:
                raise _error("artifact_unavailable")
            descriptor, content = self._artifact(root, artifact_id, expected=descriptor)
            if offset > len(content) or (offset < len(content) and content[offset] & 0xC0 == 0x80):
                raise _error("invalid_input")
            end = min(len(content), offset + max_bytes)
            while end < len(content) and content[end] & 0xC0 == 0x80:
                end -= 1
            return {"artifact": descriptor, "manifest_id": manifest_id, "manifest_sha256": manifest["sha256"],
                    "offset": offset, "next_offset": end, "eof": end == len(content),
                    "text": content[offset:end].decode("utf-8")}

    def release_manifest(self, workspace: str, manifest_id: str, expected_sha256: str) -> dict:
        self._expected(expected_sha256)
        with self._locked(workspace) as root:
            manifest = self._manifest(root, manifest_id, expected_sha256, allow_released=True)
            marker = root / "revoked" / (manifest_id + ".json")
            content = _canonical({"manifest_id": manifest_id, "sha256": manifest["sha256"]})
            if marker.exists() or marker.is_symlink():
                if self._read_file(marker, _MAX_METADATA_BYTES) != content:
                    raise _error("integrity_error")
            else:
                entries = self._entries(root / "revoked", self.max_manifests, directories=False)
                if len(entries) >= self.max_manifests:
                    raise _error("quota_exceeded")
                self._atomic_file(marker, content)
            return {"manifest_id": manifest_id, "released": True}
