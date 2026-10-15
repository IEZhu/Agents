"""Verified physical identities, separate from memory's main-checkout root.

Git is queried read-only after the caller's physical access grant is checked.
Repository membership alone never grants access to another linked checkout.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import unicodedata

from src.daemon.workspaces import ClientContext, WorkspaceError


class IdentityError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CooperationIdentity:
    repository_id: str
    workspace_id: str
    checkout_id: str
    checkout_path: str
    base_commit: str | None
    branch: str | None
    common_dir: str = ""


def _canonical(path: Path, *, strict=True) -> Path:
    """Resolve symlinks and the real spelling of existing case aliases.

    Path.resolve does not restore case on every case-insensitive filesystem.
    Checking actual entries avoids lowercasing distinct case-sensitive files.
    """
    path = path.resolve(strict=strict)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        candidate = current / part
        if candidate.exists():
            for entry in current.iterdir():
                if (unicodedata.normalize("NFC", entry.name).casefold() ==
                        unicodedata.normalize("NFC", part).casefold() and entry.samefile(candidate)):
                    part = entry.name
                    break
        current /= part
    return current


def _git(checkout: Path, *arguments: str, optional=False) -> str | None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(checkout), *arguments],
            capture_output=True, text=True, timeout=3, check=False, env=environment,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise IdentityError("workspace_invalid", "Checkout identity could not be verified.") from None
    if result.returncode:
        if optional:
            return None
        raise IdentityError("workspace_invalid", "Checkout identity could not be verified.")
    value = result.stdout.strip()
    if not value or len(value.encode("utf-8")) > 8192 or "\n" in value:
        raise IdentityError("workspace_invalid", "Checkout identity could not be verified.")
    return value


def _handle(kind: str, path: Path, authority: Path) -> str:
    info = path.stat()
    # Device/inode identity resolves physical aliases on both case-sensitive and
    # case-insensitive filesystems. Separate clones have separate common dirs.
    source = [kind, str(authority), info.st_dev, info.st_ino]
    return kind + "_" + hashlib.sha256(json.dumps(source).encode()).hexdigest()


def _case_sensitive(directory: Path) -> bool:
    """Read directory case behavior without creating a filesystem probe."""
    try:
        if sys.platform == "darwin":
            # Darwin sys/unistd.h: _PC_CASE_SENSITIVE (not named by Python).
            value = os.pathconf(directory, 11)
            if value in (0, 1):
                return bool(value)
        elif sys.platform.startswith("linux"):
            import array
            import fcntl
            import struct
            # Linux uapi/linux/fs.h: FS_IOC_GETFLAGS = _IOR('f', 1, long),
            # FS_CASEFOLD_FL = 0x40000000. No set-flags operation is used.
            operation = (2 << 30) | (struct.calcsize("l") << 16) | (ord("f") << 8) | 1
            # The ioctl number encodes long, but its returned flags are an int.
            flags = array.array("I", [0])
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.ioctl(fd, operation, flags, True)
            finally:
                os.close(fd)
            return not bool(flags[0] & 0x40000000)
        elif os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                          ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                            ctypes.c_void_p, wintypes.DWORD]
            kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.CreateFileW(str(directory), 0x80, 7, None, 3, 0x02000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise OSError()
            try:
                flags = wintypes.DWORD()
                # FileCaseSensitiveInfo is class 23; its DWORD flags uses bit 1.
                if not kernel.GetFileInformationByHandleEx(handle, 23, ctypes.byref(flags), ctypes.sizeof(flags)):
                    raise OSError()
                return bool(flags.value & 1)
            finally:
                kernel.CloseHandle(handle)
    except (OSError, ValueError):
        pass
    # Other local filesystems may answer through an existing directory entry.
    # If there is no evidence, refuse the claim rather than guess and split it.
    for entry in directory.iterdir():
        alternate = entry.name.swapcase()
        if alternate != entry.name and entry.exists():
            alias = directory / alternate
            return not (alias.exists() and alias.samefile(entry))
    raise IdentityError("scope_denied", "Filesystem case behavior could not be verified.")


def _physical_key(checkout: Path, target: Path) -> str:
    current = checkout
    parts = []
    sensitive = _case_sensitive(checkout)
    for part in target.relative_to(checkout).parts:
        if current.is_dir():
            sensitive = _case_sensitive(current)
        parts.append(part if sensitive else unicodedata.normalize("NFC", part).casefold())
        current /= part
    return "/".join(parts) or "."


def resolve_identity(context: ClientContext, checkout_path: str | None = None, *,
                     authority_path: str | Path, repository_id: str | None = None) -> CooperationIdentity:
    """Resolve one explicitly authorized checkout, optionally checking its handle."""
    try:
        context.workspace_root()
        roots = tuple(_canonical(Path(root)) for root in context.cooperation_roots)
        if not roots:
            raise IdentityError("scope_denied", "No physical checkout access was registered.")
        authority = _canonical(Path(authority_path))
        if checkout_path is not None:
            if not isinstance(checkout_path, str) or len(checkout_path) > 4000:
                raise IdentityError("workspace_invalid", "An absolute checkout path is required.")
            requested = Path(checkout_path)
            if not requested.is_absolute() or ".." in requested.parts:
                raise IdentityError("workspace_invalid", "An absolute checkout path without traversal is required.")
            candidates = [_canonical(requested)]
        else:
            candidates = list(roots)
        for candidate in candidates:
            if not candidate.is_dir() or not any(candidate.is_relative_to(root) for root in roots):
                raise IdentityError("scope_denied", "Checkout is outside registered physical access.")
            checkout = _canonical(Path(_git(candidate, "rev-parse", "--show-toplevel")))
            if not any(checkout.is_relative_to(root) for root in roots):
                raise IdentityError("scope_denied", "Checkout is outside registered physical access.")
            common = Path(_git(checkout, "rev-parse", "--git-common-dir"))
            common = _canonical(common if common.is_absolute() else checkout / common)
            derived = _handle("repository", common, authority)
            if repository_id is not None and repository_id != derived:
                continue
            return CooperationIdentity(
                repository_id=derived,
                workspace_id=context.workspace_id or _handle("workspace", roots[0], authority),
                checkout_id=_handle("checkout", checkout, authority), checkout_path=str(checkout),
                common_dir=str(common), base_commit=_git(checkout, "rev-parse", "--verify", "HEAD", optional=True),
                branch=_git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True),
            )
        raise IdentityError("unauthorized_actor", "Repository is outside the verified access context.")
    except WorkspaceError as error:
        code = "workspace_required" if "workspace_required" in str(error) else "workspace_invalid"
        raise IdentityError(code, "A verified workspace is required.") from None
    except (OSError, RuntimeError, TypeError) as error:
        raise IdentityError("workspace_invalid", "Checkout identity could not be verified.") from None


def normalize_resource(resource, identity: CooperationIdentity):
    """Return a ResourceSpec with a server-derived domain and canonical key."""
    from .models import ResourceSpec
    value = resource.model_dump() if hasattr(resource, "model_dump") else dict(resource)
    kind, key = value["resource_type"], value["resource_key"]
    if kind in {"physical_file", "physical_subtree"}:
        prefix = identity.checkout_id + ":"
        if key.startswith(prefix):
            key = key[len(prefix):]
        elif key.startswith("checkout_") and ":" in key:
            raise IdentityError("scope_denied", "Resource names another checkout.")
        relative = PurePosixPath(key)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in key or
                not key or "\x00" in key or ":" in key):
            raise IdentityError("scope_denied", "Resource must be a relative checkout path without traversal.")
        try:
            checkout = _canonical(Path(identity.checkout_path))
            target = _canonical(checkout / key, strict=False)
            if not target.is_relative_to(checkout):
                raise IdentityError("scope_denied", "Resource escapes the physical checkout.")
            # A nested checkout/submodule is a separate repository domain.
            for parent in (target, *target.parents):
                if parent == checkout:
                    break
                if parent.is_dir() and (parent / ".git").exists():
                    raise IdentityError("scope_denied", "Resource belongs to a nested repository.")
            value["resource_key"] = prefix + _physical_key(checkout, target)
        except (OSError, RuntimeError, ValueError) as error:
            if isinstance(error, IdentityError):
                raise
            raise IdentityError("scope_denied", "Resource path could not be verified.") from None
    elif kind in {"logical_intent", "integration_target", "shared_git"}:
        prefix = identity.repository_id + ":"
        if key.startswith("repository_") and ":" in key and not key.startswith(prefix):
            raise IdentityError("scope_denied", "Resource names another repository.")
        value["resource_key"] = key if key.startswith(prefix) else prefix + key
    elif kind != "heavy_execution":
        raise IdentityError("scope_denied", "Unknown resource type.")
    try:
        return ResourceSpec.model_validate(value)
    except ValueError:
        raise IdentityError("scope_denied", "Canonical resource exceeds the allowed bounds.") from None
