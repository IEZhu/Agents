"""Private local authority files. No library, sync or MCP configuration imports."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import tempfile

from src.file_lock import file_lock


class AuthorityError(ValueError):
    """A diagnosable refusal; callers must not replace failed state with defaults."""


def _reparse(info):
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _check(path: Path, *, directory=False):
    info = path.lstat()
    if (_reparse(info) or not (stat.S_ISDIR(info.st_mode) if directory else
                              stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
        raise AuthorityError("Authority paths must be regular, unlinked files/directories")
    if os.name == "nt":
        from src.daemon import acl
        if not acl.owned_by_user_or_admins(path) or not acl.is_private(path, inherited_below=directory):
            raise AuthorityError("Authority storage requires a user-only Windows ACL")
    elif info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise AuthorityError("Authority storage requires owner-only permissions")
    return info


def directory(path: Path, *, create=False):
    """Ancestors may be shared, but no component may redirect the authority root."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise
            current.mkdir(mode=0o700, exist_ok=True)
            if os.name == "nt":
                from src.daemon import acl
                acl.restrict(current)
            _sync(current.parent)
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or _reparse(info):
            raise AuthorityError("Authority directory contains a link or reparse point")
    _check(path, directory=True)


def read(path: Path, limit=2 * 1024 * 1024) -> bytes:
    before = _check(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(before, opened):
            raise AuthorityError("Authority file changed while opening")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(data) > limit or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) !=
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or
            not os.path.samestat(after, _check(path))):
        raise AuthorityError("Authority file is oversized or changed while reading")
    return data


def _sync(path: Path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write(path: Path, data: bytes):
    """Atomic replacement within an already validated private directory."""
    directory(path.parent)
    if os.path.lexists(path):
        _check(path)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            if os.name == "nt":
                from src.daemon import acl
                acl.restrict(temporary)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _check(temporary)
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(path: Path, *, create=False):
    directory(path, create=create)
    lock = path / ".lock"
    if os.path.lexists(lock):
        _check(lock)
    with file_lock(lock, blocking=False) as fd:
        if fd is None:
            raise AuthorityError("Authority storage requires an OS file lock")
        _check(lock)
        yield
