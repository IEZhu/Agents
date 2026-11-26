"""Private append-only JSON-lines files inside a workspace.

The validated-claims store and the work map keep their records the same way: a file under
``<workspace>/data/<name>/`` (directory 0700, file 0600), opened without following links,
appended under an exclusive lock, a torn last line never glued to the next record, and a
size cap above which writes are refused. This module holds that mechanism; each store keeps
its own record format.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Callable

from src.file_lock import file_lock

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

# Opening flags that refuse a symbolic link and never block on a FIFO, where the platform has them.
NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
# POSIX opens every component from its parent's descriptor, so neither a link nor a directory
# swapped in between a check and an open can redirect the store. Windows has no dir_fd and checks
# each component with lstat instead, which leaves that swap window open.
ANCHORED = (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
            and os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd)
# What opening a link with O_NOFOLLOW, or a non-directory with O_DIRECTORY, answers.
REFUSED_KINDS = {errno.ELOOP, errno.ENOTDIR, errno.EISDIR, getattr(errno, "EMLINK", errno.ELOOP),
                 getattr(errno, "EFTYPE", errno.ELOOP)}
# Attempts at a creating open. macOS answers a creating open with ENOENT now and then while
# another thread creates the same name in the same directory (measured: hundreds of times in
# 200 rounds of 8 threads, with plain flags too), so a creating open is retried.
CREATE_ATTEMPTS = 20
# What a creating open writes into the store's own directory, so a repository that does not
# ignore data/ never stages the records (a work map keeps whole worker results).
IGNORE_FILE = ".gitignore"
IGNORE_TEXT = b"*\n"
# Windows' lstat reports a directory junction as a directory; only this attribute tells it apart.
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class StoreError(ValueError):
    """A request a store refuses, with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class UnsafeStore(StoreError):
    def __init__(self, message: str):
        super().__init__("unsafe_store", message)


def owned_by_caller(info: os.stat_result) -> bool:
    """POSIX: the file belongs to this process's user. Windows reports no owner here."""
    return os.name == "nt" or info.st_uid == os.geteuid()


def plain_directory(info) -> bool:
    """A directory that is neither a symbolic link nor, on Windows, a junction or other reparse point."""
    return stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & REPARSE_POINT


def real_directory(path: Path, *, private: bool = False) -> None:
    """``path`` exists as a directory and is not a symbolic link or a Windows junction.

    With ``private`` it must also belong to this user, and group and other permissions
    are removed: ``mkdir``'s mode does nothing for a directory that already exists.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(mode=0o700, exist_ok=True)  # another process may create it first; checked below
        info = os.lstat(path)
    if not plain_directory(info):
        raise UnsafeStore(f"{path} is not a plain directory; the store does not follow links")
    if not private or os.name == "nt":
        return
    if not owned_by_caller(info):
        raise UnsafeStore(f"{path} belongs to another user; the store keeps only its own files")
    if info.st_mode & 0o077:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | NO_FOLLOW)
        try:
            if not os.path.samestat(info, os.fstat(descriptor)):
                raise UnsafeStore(f"{path} changed while it was opened")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)


def open_store(path: Path, flags: int, *, private: bool = False) -> int:
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
        raise UnsafeStore(f"{path} is not a regular file; the store does not follow links")
    descriptor = os.open(path, flags | NO_FOLLOW, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (before is not None and not os.path.samestat(before, opened)):
            raise UnsafeStore(f"{path} changed while it was opened")
        if private and os.name != "nt":
            if not owned_by_caller(opened):
                raise UnsafeStore(f"{path} belongs to another user; the store keeps only its own files")
            if opened.st_mode & 0o077:
                os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def not_plain(path: Path) -> UnsafeStore:
    return UnsafeStore(f"{path} is not a plain directory or file; the store does not follow links")


def private_descriptor(descriptor: int, path: Path, mode: int) -> None:
    """The open object belongs to this user and loses group and other permissions."""
    info = os.fstat(descriptor)
    if not owned_by_caller(info):
        raise UnsafeStore(f"{path} belongs to another user; the store keeps only its own files")
    if info.st_mode & 0o077:
        os.fchmod(descriptor, mode)


def one_name(name: str) -> None:
    """Refuse a name that is not one entry of its directory, before anything is opened."""
    if name in ("", ".", "..") or os.sep in name or (os.altsep is not None and os.altsep in name):
        raise ValueError(f"{name!r} is not a single file or directory name")


@contextlib.contextmanager
def store_directory(root: Path, parts: tuple[str, ...], *, create: bool, private: bool):
    """Yield ``root/<parts…>``: a descriptor on POSIX, the path on Windows, None when missing.

    On POSIX each component is opened from its parent's descriptor with ``O_NOFOLLOW``; the
    workspace root itself is trusted. With ``create`` missing components are made (0700)
    and the last one gets a ``.gitignore`` holding ``*`` unless it has one; with ``private``
    the last component must be this user's and loses group and other permissions, which
    ``mkdir``'s mode does not do for a directory that already exists.
    A part that is empty, ``.``, ``..`` or holds a path separator raises ValueError.
    """
    for name in parts:
        one_name(name)
    last = len(parts) - 1
    if not ANCHORED:
        path = root
        for index, name in enumerate(parts):
            path = path / name
            if create:
                real_directory(path, private=private and index == last)
            else:
                try:
                    if not plain_directory(os.lstat(path)):
                        raise not_plain(path)
                except FileNotFoundError:
                    yield None
                    return
        if create:
            ignore_in_git(path)
        yield path
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    opened: list[int] = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        opened.append(current)
        path = root
        for index, name in enumerate(parts):
            path = path / name
            try:
                current = os.open(name, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                with contextlib.suppress(FileExistsError):  # another process may create it first
                    os.mkdir(name, 0o700, dir_fd=current)
                try:
                    current = os.open(name, flags, dir_fd=current)
                except OSError as error:
                    if error.errno in REFUSED_KINDS:
                        raise not_plain(path) from None
                    raise
            except OSError as error:
                if error.errno in REFUSED_KINDS:
                    raise not_plain(path) from None
                raise
            opened.append(current)
            if private and index == last:
                private_descriptor(current, path, 0o700)
        if create:
            ignore_in_git(current)
        yield current
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def open_relative(directory: int | Path, name: str, flags: int) -> int:
    """Open ``name`` inside ``directory`` (a descriptor, or a path on Windows) without following a link."""
    for attempt in range(CREATE_ATTEMPTS):
        try:
            if isinstance(directory, Path):
                return os.open(directory / name, flags | NO_FOLLOW, 0o600)
            return os.open(name, flags | NO_FOLLOW, 0o600, dir_fd=directory)
        except FileNotFoundError:
            if not flags & os.O_CREAT or attempt == CREATE_ATTEMPTS - 1:
                raise
            time.sleep(0.001 * (attempt + 1))
    raise AssertionError("unreachable")


def ignore_in_git(directory: int | Path) -> None:
    """Write ``IGNORE_TEXT`` to ``IGNORE_FILE`` in ``directory`` unless a file or link of that name exists.

    ``O_EXCL`` never opens what is there, a link included, so an existing file stays the user's.
    """
    try:
        descriptor = open_relative(directory, IGNORE_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return
    try:
        os.write(descriptor, IGNORE_TEXT)
    finally:
        os.close(descriptor)


def not_created(name) -> UnsafeStore:
    return UnsafeStore(f"{name} could not be created; its directory was removed or replaced meanwhile")


@contextlib.contextmanager
def store_file(directory, name: str, flags: int, mode: str, *, private: bool):
    """Yield the file ``name`` inside ``directory`` (from ``store_directory``), or None when missing.

    Only a reader gets None: a creating open (``os.O_CREAT``) that still finds nothing has
    lost its directory, and that is refused instead of handing a writer no file. A ``name``
    that is empty, ``.``, ``..`` or holds a path separator raises ValueError.
    """
    one_name(name)
    if isinstance(directory, Path):
        try:
            descriptor = open_store(directory / name, flags, private=private)
        except FileNotFoundError:
            if flags & os.O_CREAT:
                raise not_created(directory / name) from None
            yield None
            return
    else:
        try:
            descriptor = open_relative(directory, name, flags)
        except FileNotFoundError:
            if flags & os.O_CREAT:
                raise not_created(name) from None
            yield None
            return
        except OSError as error:
            if error.errno in REFUSED_KINDS:
                raise UnsafeStore(f"{name} is not a regular file; the store does not follow links") from None
            raise
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UnsafeStore(f"{name} is not a regular file; the store does not follow links")
            if private:
                private_descriptor(descriptor, Path(name), 0o600)
        except BaseException:
            os.close(descriptor)
            raise
    with os.fdopen(descriptor, mode) as handle:
        yield handle


@contextlib.contextmanager
def exclusive(handle, path: Path):
    """Serialize writers. POSIX locks the open file itself, so no path is resolved again;
    Windows, which opened the file by name anyway, locks a sidecar file beside it."""
    if fcntl is not None and ANCHORED:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    with file_lock(path.with_name(path.name + ".lock")):
        yield


def read_records(raw: bytes, max_bytes: int, path: Path, *,
                 keep: Callable[[Any], bool] | None = None) -> list[Any]:
    """Every line of ``raw`` that parses as JSON, in order; a torn or foreign line is skipped.

    Lines end at ``\\n`` only: ``json.dumps(..., ensure_ascii=False)`` leaves U+2028, U+2029
    and U+0085 raw inside a record, and ``str.splitlines`` would cut the record there.
    With ``keep``, a record it refuses is dropped as soon as it is parsed, so a file of
    lines the store cannot use is never held in memory whole.
    """
    if len(raw) > max_bytes:
        raise UnsafeStore(f"{path} is larger than {max_bytes} bytes; archive it by hand")
    records = []
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        if not line:
            continue  # the end of the last record, or a blank line
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if keep is None or keep(record):
            records.append(record)
    return records


def _separator(handle) -> bytes:
    """A newline when the file does not end with one: an earlier write was torn, and a record
    must never be glued onto it."""
    size = os.fstat(handle.fileno()).st_size
    if size:
        handle.seek(size - 1)
        if handle.read(1) != b"\n":
            return b"\n"
    return b""


def require_room(handle, length: int, max_bytes: int, path: Path) -> None:
    """Refuse with ``store_full`` when appending ``length`` bytes would take the file past
    ``max_bytes``; a store writing two files checks the second before it writes the first."""
    if os.fstat(handle.fileno()).st_size + len(_separator(handle)) + length > max_bytes:
        raise StoreError("store_full", f"{path} would exceed {max_bytes} bytes; "
                                       "archive it by hand to start a new store")


def append_line(handle, line: bytes, max_bytes: int, path: Path) -> None:
    """Append ``line`` (one or more newline-terminated records) to a file opened ``r+b`` with
    ``O_APPEND`` while ``exclusive`` holds its lock."""
    require_room(handle, len(line), max_bytes, path)
    handle.write(_separator(handle) + line)
    handle.flush()
