"""Append-only intent/action/outcome history with optional semantic recall.

Three classes:

* ``HistoryWriter`` — stdlib-only. Writes deduplicated, content-hashed entries
  to ``history.md`` under an exclusive file lock. Rotates to
  ``history/YYYY-MM.md`` when the file grows past
  ``HISTORY_ROTATION_THRESHOLD_KB``.

* ``HistoryReader`` — stdlib-only. Parses entries from disk and serves
  recency / ``since`` queries.

* ``HistoryStore`` — lazy ``NumpyVectorStore`` wrapper. Built only on the
  first ``read_history(query=...)`` call; rebuilds when the content of the
  markdown file or the embedding fingerprint changes, re-embedding only new
  or edited entries while the fingerprint is unchanged. The embedder is
  imported lazily so ``HistoryWriter``/``HistoryReader`` users never pay the
  numpy cost.

With the user library sync installed (``set_sync``; the MCP servers do it at
startup), each append tells the sync's worker that the checkout changed (it
never waits for it), and reads merge the checkout's journal (``history.md`` and
``history/*.md``) with the entries the user's machines shared for the same
repository (``src/user_sync/history.py``), newest month first. This module
never imports the sync itself.
"""

from __future__ import annotations

from src.file_lock import file_lock

import contextlib
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from src.memory import config as _memory_config
from src.memory.config import (
    HISTORY_DEDUP_TAIL_SIZE,
    HISTORY_FORMAT_VERSION,
    HISTORY_ROTATION_THRESHOLD_KB,
    HISTORY_VECTOR_STORE_NAME,
)

logger = logging.getLogger(__name__)

# (path, errno) of rotation failures already logged: a persistent failure warns once.
_ROTATION_WARNED: set = set()


# A lock on history.md itself — fcntl on POSIX, a no-op shim on Windows. Appends
# also take the sidecar lock (src.file_lock), which excludes other processes on both.
try:  # pragma: no cover - exercised via integration only
    import fcntl as _fcntl

    def _lock_exclusive(fh) -> None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)

    def _unlock(fh) -> None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)

except ImportError:  # pragma: no cover - Windows
    def _lock_exclusive(fh) -> None:
        return None

    def _unlock(fh) -> None:
        return None

# Process-local mutex — fcntl.flock is advisory and per-process on Linux,
# so concurrent threads (e.g. multiple run_in_executor calls) can bypass it.
_WRITE_LOCK = threading.Lock()


# An entry's id: the first 12 hex digits of its content hash (HistoryWriter._compute_entry_hash).
ENTRY_ID = re.compile(r"[0-9a-f]{12}")
_HEADER_RE = re.compile(
    rf"^##\s+(?P<ts>\S+)\s+\|\s+(?P<id>{ENTRY_ID.pattern})\s*$",
    re.MULTILINE,
)
_FIELD_RE = re.compile(r"^\*\*(?P<name>[A-Za-z]+):\*\*\s*(?P<value>.*)$")
# Monthly archives that rotation writes into history/.
_ARCHIVE_NAME = re.compile(r"[0-9]{4}-[0-9]{2}\.md")


@dataclass
class HistoryEntry:
    """In-memory representation of a single history entry.

    ``machine`` tells where a merged read found it: None for this checkout's
    own journal, this machine's label for an entry it wrote in another
    checkout of the repository, another machine's label for that machine's
    entries. A block parsed alone takes it from its ``**Machine:**`` line.
    """
    id: str
    timestamp: str
    intent: str
    action: str
    outcome: str
    files: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None
    machine: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _sidecar(history_path: str) -> str:
    """The stable lock file of a history file (``.history.md.lock`` beside it)."""
    return os.path.join(os.path.dirname(history_path), "." + os.path.basename(history_path) + ".lock")


@contextlib.contextmanager
def _reading(history_path: str):
    """The history lock, shared. Readers take it so a rotation is never seen half way and,
    on Windows, never fails on a file a reader holds open; a reader that may not open the
    lock file (a read-only checkout) reads without it, as before."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(_sidecar(history_path), shared=True))
        except OSError:
            pass
        yield


def _archive_names(archive_dir: str, *, strict=False) -> List[str]:
    try:
        info = os.lstat(archive_dir)
        if _is_link(info):
            raise PermissionError("journal archive directory must not be a link")
        if not stat.S_ISDIR(info.st_mode):
            if strict:
                raise NotADirectoryError("journal archive path is not a directory")
            return []
        return sorted(name for name in os.listdir(archive_dir) if _ARCHIVE_NAME.fullmatch(name))
    except FileNotFoundError:
        return []


def _is_link(info):
    """Classify an lstat result, including Windows name-surrogate reparse tags."""
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    tag = getattr(info, "st_reparse_tag", 0)
    # CPython lstat traverses non-name-surrogate reparse points and may return
    # their attributes with tag 0. Symlinks and junctions retain their link tag.
    return stat.S_ISLNK(info.st_mode) or bool(reparse and tag & 0x20000000)


def _regular_kind(info, *, directory=False):
    """Reject links, Windows name-surrogate reparse points and special files."""
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if _is_link(info) or not expected(info.st_mode):
        raise PermissionError("journal artifact must be a regular directory" if directory
                              else "journal artifact must be a regular file")


def _windows_final_path(fd):
    """Resolve the opened handle, never the pathname that may have been swapped."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = final_path(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise OSError("cannot validate journal file handle")
    return _normalized_windows_path(buffer.value)


def _normalized_windows_path(value):
    """Compare DOS and extended-length DOS/UNC names in the same namespace."""
    import ntpath

    if value[:8].upper() == "\\\\?\\UNC\\":
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def _windows_directory_path(path):
    """The real path of a directory, read from a handle that refuses a reparse point.

    ``os.path.realpath`` resolves the name a second time, so a junction put in
    place after the caller checked the name would be resolved to its target and
    every later comparison would match the substituted directory instead.
    ``FILE_FLAG_OPEN_REPARSE_POINT`` is the Windows counterpart of ``O_NOFOLLOW``:
    a junction or symlink opens as itself and is refused here, so the path
    returned always belongs to a directory that was not substituted.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    # No access requested, every share mode, OPEN_EXISTING, and
    # BACKUP_SEMANTICS | OPEN_REPARSE_POINT so a directory opens and a link does
    # not resolve.
    handle = create(path, 0, 0x7, None, 3, 0x02000000 | 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = kernel.GetFileAttributesW(path)
        if attributes != 0xFFFFFFFF and attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise PermissionError("journal artifact must be a regular directory")
        final_path = kernel.GetFinalPathNameByHandleW
        final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_path(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError("cannot validate journal directory handle")
        # Raw, like GetFinalPathNameByHandleW gives it: the caller joins the
        # relative part and normalizes the whole once, as before.
        return buffer.value
    finally:
        kernel.CloseHandle(handle)


@contextlib.contextmanager
def _open_regular(path, mode="r", *, root=None, **kwargs):
    """Open a journal artifact without following a substituted file or directory.

    The caller selects a trusted root; its ancestors are trusted. Descendants
    and the file must not be links. POSIX traverses from a directory descriptor.
    Windows additionally checks the final handle path before exposing any bytes,
    since it has no O_NOFOLLOW.
    """
    path = os.path.abspath(path)
    parent, name = os.path.split(path)
    root = os.path.abspath(root or parent)
    relative = os.path.relpath(path, root)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise PermissionError("journal file is outside its authorized root")
    root_info = os.lstat(root)
    _regular_kind(root_info, directory=True)
    expected_path = None
    directory_fd = None
    anchored = hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
    try:
        if anchored:
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if not os.path.samestat(root_info, os.fstat(directory_fd)):
                raise PermissionError("journal directory changed while opening")
            for component in relative.split(os.sep)[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
        else:
            # The expected root comes from a handle that refuses a reparse
            # point, not from resolving the name again: a junction put over the
            # root between the lstat above and this call is refused here
            # instead of becoming the path the final-path check below accepts.
            expected_path = _normalized_windows_path(
                os.path.join(_windows_directory_path(root), relative))
            directory = root
            for component in relative.split(os.sep)[:-1]:
                directory = os.path.join(directory, component)
                _regular_kind(os.lstat(directory), directory=True)

        def opener(filename, flags):
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False) if anchored else os.lstat(path)
            _regular_kind(before)
            # No truncation or data access until the actual descriptor is checked.
            if flags & (os.O_TRUNC | os.O_CREAT):
                raise ValueError("journal guarded open requires an existing file")
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(name, flags, dir_fd=directory_fd) if anchored else os.open(filename, flags)
            try:
                opened = os.fstat(fd)
                # Windows fstat omits the reparse tag even for ordinary cloud
                # files. Link classification belongs to lstat above; identity
                # and final handle path still verify the object actually opened.
                if not stat.S_ISREG(opened.st_mode):
                    raise PermissionError("journal artifact must be a regular file")
                if not os.path.samestat(before, opened):
                    raise PermissionError("journal file changed while opening")
                if not anchored and _windows_final_path(fd) != expected_path:
                    raise PermissionError("journal file escaped its directory")
                return fd
            except BaseException:
                os.close(fd)
                raise

        with open(path, mode, opener=opener, **kwargs) as stream:
            yield stream
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _month_after(month: str) -> str:
    """``YYYY-MM-01T00:00:00`` of the month after ``month``: no entry of a ``month`` file is that late."""
    year, number = int(month[:4]), int(month[5:7])
    year, number = (year + 1, 1) if number == 12 else (year, number + 1)
    return f"{year:04d}-{number:02d}-01T00:00:00"


# --- Parsed files ------------------------------------------------------------

class _ParsedFiles:
    """Entries of history files by path, kept while a file's size and mtime stay the same.

    Archives and other machines' segments rarely change, so repeated reads and
    index checks parse only what changed. Bounded by file count and bytes.
    """

    def __init__(self, max_files: int = 256, max_bytes: int = 64 * 1024 * 1024):
        self._items: "OrderedDict[str, tuple]" = OrderedDict()
        self._bytes = 0
        self._max_files, self._max_bytes = max_files, max_bytes
        self._lock = threading.Lock()

    def load(self, path: str, *, root=None) -> Optional[Tuple[str, Tuple[HistoryEntry, ...]]]:
        """``(sha256 of the bytes, entries)``, or None when ``path`` is missing.

        Other read errors propagate; entries are shared, so callers copy before changing them.
        """
        try:
            with _open_regular(path, "rb", root=root) as stream:
                info = os.fstat(stream.fileno())
                key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                with self._lock:
                    cached = self._items.get(path)
                    if cached is not None and cached[0] == key:
                        self._items.move_to_end(path)
                        return cached[1], cached[2]
                data = stream.read()
                info = os.fstat(stream.fileno())
        except FileNotFoundError:
            return None
        digest = hashlib.sha256(data).hexdigest()
        entries = tuple(HistoryReader._parse(data.decode("utf-8", "replace")))
        with self._lock:
            previous = self._items.pop(path, None)
            if previous is not None:
                self._bytes -= previous[3]
            if len(data) == info.st_size and key == (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                self._items[path] = (key, digest, entries, len(data))
                self._bytes += len(data)
                while self._items and (len(self._items) > self._max_files or self._bytes > self._max_bytes):
                    _, dropped = self._items.popitem(last=False)
                    self._bytes -= dropped[3]
        return digest, entries


_PARSED = _ParsedFiles()


def parsed_file(path: str, *, root=None) -> Optional[Tuple[str, Tuple[HistoryEntry, ...]]]:
    """``(sha256, entries)`` of a history file through the shared parse cache; see ``_ParsedFiles``."""
    return _PARSED.load(path, root=root)


def _archive_root(history_path, archive_dir):
    """Keep local archives below the workspace; explicit external roots stay supported."""
    root, archive = os.path.dirname(os.path.abspath(history_path)), os.path.abspath(archive_dir)
    try:
        if os.path.normcase(os.path.commonpath((root, archive))) == os.path.normcase(root):
            return root
    except ValueError:  # An explicitly selected archive on another Windows drive.
        pass
    return archive


# --- Sync with the user's other machines (optional) -------------------------

@dataclass(frozen=True)
class MachineHistory:
    """The synced history of one repository, as the user library sync finds it.

    ``label`` names this machine. ``files`` holds ``(label, path)`` of history
    segments of the same repository: other machines', and this machine's own
    from its other checkouts of the repository (their label is ``label``).
    ``deleted`` holds ``(id, time)`` of entries deleted from a journal here: a
    segment's copy of one is left out, also before sync takes it out.
    """
    label: str
    files: Tuple[Tuple[str, str], ...] = ()
    deleted: FrozenSet[Tuple[str, str]] = frozenset()


_sync = None


def set_sync(integration):
    """Install the user library sync integration, or remove it with None; returns the previous one.

    ``integration.appended(history_path, entry_id)`` is called after each append, once
    every history lock is released, and must return at once;
    ``integration.machines(history_path)`` returns a ``MachineHistory``, or None
    while the repository's history does not take part in sync; an optional
    ``integration.deleted(history_path, entry_id, timestamp)`` hears of an entry
    ``delete_entry`` removed, also once the locks are released. Without an
    integration the history is this machine's journal only.
    ``src.user_sync.history.install`` sets it.
    """
    global _sync
    previous, _sync = _sync, integration
    return previous


def _machine_history(history_path: str) -> Optional[MachineHistory]:
    integration = _sync
    if integration is None:
        return None
    try:
        return integration.machines(history_path)
    except Exception:
        logger.warning("could not list other machines' history for %s", history_path, exc_info=True)
        return None


def _appended(history_path: str, entry_id: Optional[str]) -> None:
    """Tell the sync that an entry was appended; its failure never fails the append."""
    integration = _sync
    if integration is None:
        return
    try:
        integration.appended(history_path, entry_id)
    except Exception:
        logger.warning("could not schedule sharing the history of %s", history_path, exc_info=True)


def _deleted(history_path: str, entry_id: str, timestamp: str) -> None:
    """Tell the sync that an entry was deleted; called once every history lock is released."""
    integration = _sync
    hook = getattr(integration, "deleted", None)
    if hook is None:
        return
    try:
        hook(history_path, entry_id, timestamp)
    except Exception:
        logger.warning("could not schedule removing a deleted entry of %s from sync", history_path, exc_info=True)


class EntryChanged(Exception):
    """The entry to delete is not there as it was shown."""


class MemoryWriteRejected(ValueError):
    """A write was rejected before mutation, rather than failing in storage."""


_MARKER = re.compile(r"\s*(?:<!--|>)")  # a rotation's merge comment or the archive pointer
_RECEIPT_RE = re.compile(r"^<!-- agents-write-receipt (.+) -->\r?$", re.MULTILINE)


def without_entry(content: str, entry_id: str, timestamp: str) -> str:
    """``content`` without the entry headed ``## <timestamp> | <entry_id>``: everything up to the next
    heading, as the parser reads an entry, except a rotation's markers and the blank lines at its end.
    ``EntryChanged`` when no such heading is there."""
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        header = _HEADER_RE.match(line)
        if not header or header.group("id") != entry_id or header.group("ts") != timestamp:
            continue
        end = index + 1
        while end < len(lines) and not _HEADER_RE.match(lines[end]):
            end += 1
        last = end - 1  # the entry's last line: markers and blank lines after it are no part of it
        while last > index and (not lines[last].strip() or _MARKER.match(lines[last])):
            last -= 1
        tail = lines[last + 1:end]
        kept = tail if any(_MARKER.match(line) for line in tail) else []
        return "".join(lines[:index] + kept + lines[end:])
    raise EntryChanged("the entry is no longer there as shown")


_ANCHORED_DIR = (hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd
                 and os.rename in os.supports_dir_fd)


def _checked_directory(directory):
    """The identity of ``directory``, refused as ``_open_regular`` refuses it: a link or a non-directory."""
    info = os.lstat(directory)
    _regular_kind(info, directory=True)
    return info


def _journal_directory(path):
    """The checked identity of the directory holding ``path``, taken before it is read.

    Every replacement, move and sync compares the directory it reaches with
    this, so the trust a guarded read earned reaches the write instead of
    ending at the read.
    """
    return _checked_directory(os.path.dirname(path) or ".")


def _refuse_swapped_dir(directory, info, message):
    """Re-identify ``directory`` by name where no descriptor can hold it.

    Windows accepts no directory descriptor, so a create or a move there reaches
    the directory by name. Each one is preceded by this comparison with the
    caller's check, and a swap is refused rather than followed. The window left
    is this comparison to the call that follows it.
    """
    current = os.lstat(directory)
    swapped = _is_link(current) or not stat.S_ISDIR(current.st_mode)
    if info.st_ino and current.st_ino:
        # A file system that numbers no entry cannot tell two apart.
        swapped = swapped or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
    if swapped:
        raise PermissionError(message)


@contextlib.contextmanager
def _anchored_dir(path, info, message="journal directory changed before it was written"):
    """A descriptor for ``path`` that the steps below use instead of its name.

    The trust a pathname carries has to be earned again at every call, because
    the kernel resolves the name afresh each time. A descriptor holds the
    directory object itself, so a name swapped afterwards cannot redirect a
    create, a move or a sync made through it. Windows accepts no directory
    descriptor anywhere (``os.supports_dir_fd`` is empty there), so it yields
    ``None`` and the caller re-identifies the directory by name with
    ``_refuse_swapped_dir`` before each step instead.

    ``O_NOFOLLOW`` is deliberately not used: a trusted root may legitimately be
    reached through a link, which is what ``_open_regular`` means by trusting a
    selected root's ancestors, and refusing one here would stop a working
    installation rather than an attack. Which directory the descriptor holds is
    established instead by ``info``, the caller's own check of it taken before
    its read: a name substituted between that check and this open has another
    identity and is refused with ``message``.
    """
    if not _ANCHORED_DIR:
        yield None
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise PermissionError("journal artifact must be a regular directory")
        if not os.path.samestat(info, opened):
            raise PermissionError(message)
        yield fd
    finally:
        os.close(fd)


def _temp_beside(directory, name, dir_fd):
    """``(descriptor, name)`` of a fresh temporary file for a replacement of ``name``."""
    prefix, suffix = "." + name + ".", ".tmp"
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(100):
        candidate = prefix + os.urandom(8).hex() + suffix
        try:
            if dir_fd is None:
                return os.open(os.path.join(directory, candidate), flags, 0o600), candidate
            return os.open(candidate, flags, 0o600, dir_fd=dir_fd), candidate
        except FileExistsError:
            continue
    raise FileExistsError("no free temporary name in the journal directory")


def _move_onto(directory, temporary, name, dir_fd):
    """Put ``temporary`` at ``name`` in one step, through ``dir_fd`` where there is one.

    ``os.replace`` takes no directory descriptor on any platform, but
    ``os.rename`` does and overwrites atomically on POSIX, which is exactly what
    a replacement needs.
    """
    if dir_fd is None:
        os.replace(os.path.join(directory, temporary), os.path.join(directory, name))
    else:
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _discard_temp(directory, temporary, dir_fd):
    with contextlib.suppress(OSError):
        if dir_fd is None:
            os.unlink(os.path.join(directory, temporary))
        else:
            os.unlink(temporary, dir_fd=dir_fd)


def _sync_moved(directory, dir_fd) -> None:
    """Persist the entry a move just made, through the descriptor the move used.

    Opening the directory by name again here would sync whatever the name holds
    now: after a swap, a decoy, while the directory that received the entry
    stayed unsynced and the call still reported it durable.
    """
    if dir_fd is None:
        _sync_directory(directory)
    else:
        os.fsync(dir_fd)


def _sync_recorded(path: str, info, *, root=None) -> None:
    """Persist a file that is already written, and the entry that names it, in the directory ``info`` checked.

    A replay answers with a receipt as durable, so it syncs what holds the
    receipt again, and both syncs have to reach the directory its read checked:
    opened by name, a swapped directory would be synced while the one holding
    the receipt stayed as it was. With a descriptor the file is opened relative
    to it, without following a link; without one (Windows) the directory is
    re-identified first and the file goes through ``_sync_file``.
    """
    directory, name = os.path.dirname(path) or ".", os.path.basename(path)
    message = "journal directory changed before its receipt was synced"
    with _anchored_dir(directory, info, message) as dir_fd:
        if dir_fd is None:
            _refuse_swapped_dir(directory, info, message)
            _sync_file(path, root=root)
            _sync_directory(directory)
            return
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(name, flags, dir_fd=dir_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PermissionError("journal artifact must be a regular file")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(dir_fd)


def _replace(path: str, text: str, *, info) -> None:
    """Write ``text`` beside ``path`` and replace it in one step, synced first, keeping its mode.

    ``surrogateescape`` writes back exactly the bytes a read with it decoded, invalid ones included.
    ``info`` is ``_journal_directory(path)``, taken before the caller read the
    file it replaces. The temporary file, the move and the directory sync all go
    through one descriptor for that directory, so none reaches it by a name that
    may have been substituted since; without a descriptor, the directory is
    re-identified before the temporary file is created and again before the move.
    """
    directory, name = os.path.dirname(path) or ".", os.path.basename(path)
    message = "journal directory changed before it was written"
    with _anchored_dir(directory, info, message) as dir_fd:
        if dir_fd is None:
            _refuse_swapped_dir(directory, info, message)
        fd, temp = _temp_beside(directory, name, dir_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
                with contextlib.suppress(OSError):
                    # os.fchmod is POSIX only, and AttributeError is not an
                    # OSError, so reaching for it on Windows would not be
                    # suppressed: it would fail the second write to a journal,
                    # the first one having raised FileNotFoundError here first.
                    if dir_fd is not None:
                        os.fchmod(stream.fileno(), stat.S_IMODE(os.stat(name, dir_fd=dir_fd).st_mode))
                    else:
                        os.chmod(os.path.join(directory, temp), stat.S_IMODE(os.stat(path).st_mode))
            if dir_fd is None:
                _refuse_swapped_dir(directory, info, message)
            _move_onto(directory, temp, name, dir_fd)
            _sync_moved(directory, dir_fd)
        except BaseException:
            _discard_temp(directory, temp, dir_fd)
            raise


def _sync_file(path: str, *, root=None) -> None:
    # Windows _commit/FlushFileBuffers requires a writable handle, even when
    # replay only confirms an existing record and does not modify its bytes.
    with _open_regular(path, "r+b" if os.name == "nt" else "rb", root=root) as stream:
        os.fsync(stream.fileno())


def _sync_directory(path: str) -> None:
    """Persist a directory entry on POSIX; Windows only promises process-crash recovery."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def delete_entry(history_path: str, month: Optional[str], entry_id: str, timestamp: str,
                 archive_dir: Optional[str] = None) -> None:
    """Delete one entry from ``history.md`` (``month`` None) or the archive ``history/<month>.md``.

    The file is rewritten under the locks ``HistoryWriter.append_entry`` takes, so an append or a
    rotation waits for it and then sees the rest unchanged; it is replaced in one step, as rotation
    replaces archives. ``EntryChanged`` when the entry is not there as shown, or while a rotation
    is pending. The sync then removes the entry from what this machine shared (``_deleted``).
    """
    archive_dir = archive_dir or os.path.join(os.path.dirname(history_path), "history")
    path = history_path if month is None else os.path.join(archive_dir, f"{month}.md")
    with _WRITE_LOCK, file_lock(_sidecar(history_path)):
        pending = history_path + ".rotating"
        try:
            with _open_regular(pending, "rb") as stream:
                if os.fstat(stream.fileno()).st_size > 0:  # an emptied leftover holds nothing
                    raise EntryChanged("a rotation of history.md is pending; try again in a moment")
        except FileNotFoundError:
            pass
        # Taken before the guarded read, so the replacement writes into the
        # directory that read checked and refuses one swapped in since.
        try:
            directory_info = _journal_directory(path)
        except FileNotFoundError:
            raise EntryChanged("the file is gone") from None
        try:
            with _open_regular(path, "r", root=_archive_root(history_path, archive_dir) if month else None,
                               encoding="utf-8", errors="surrogateescape", newline="") as stream:
                content = stream.read()
        except FileNotFoundError:
            raise EntryChanged("the file is gone") from None
        updated = without_entry(content, entry_id, timestamp)
        for entry in HistoryReader._parse(content):
            if entry.id == entry_id and entry.timestamp == timestamp:
                receipt = (entry.metadata or {}).get("_agents_write")
                if receipt:
                    # Keep only the acceptance receipt, not the deleted content.
                    # A delayed retry must not resurrect a deliberately removed entry.
                    updated += "\n<!-- agents-write-receipt " + json.dumps(receipt, sort_keys=True) + " -->\n"
        _replace(path, updated, info=directory_info)
    _deleted(history_path, entry_id, timestamp)


def entry_blocks(content: str) -> List[Tuple[HistoryEntry, str]]:
    """Each entry of ``content`` with its block: the heading and its field lines.

    Text between entries that is not a field (an archive's separator or
    header) is left out, so a block holds exactly what the writer rendered.
    """
    out: List[Tuple[HistoryEntry, str]] = []
    positions = [m.start() for m in _HEADER_RE.finditer(content)]
    positions.append(len(content))
    for start, end in zip(positions, positions[1:]):
        lines = content[start:end].splitlines()
        entry = HistoryReader._parse_block(content[start:end])
        if entry is None:
            continue
        kept = [lines[0].rstrip()] + [line.rstrip() for line in lines[1:] if _FIELD_RE.match(line)]
        out.append((entry, "\n".join(kept)))
    return out


def _journal_texts(history_path: str, archive_dir: Optional[str], since_month: Optional[str]) -> List[str]:
    """This machine's journal, oldest first: archives from ``since_month`` on, a pending rotation, history.md.

    ``history.md`` and the pending file are read under the shared history lock;
    the archives after it, without the lock: rotation replaces or creates them
    whole, so an entry that moved meanwhile is read twice (and deduplicated),
    never missed, and a reader never keeps an archive open while rotation runs.
    """
    archive_dir = archive_dir or os.path.join(os.path.dirname(history_path), "history")
    pending = history_path + ".rotating"
    live: List[str] = []
    if os.path.lexists(history_path) or os.path.lexists(pending):
        with _reading(history_path):
            for path in (pending, history_path):
                try:
                    with _open_regular(path, "r", encoding="utf-8", errors="replace") as fh:
                        live.append(fh.read())
                except FileNotFoundError:
                    continue
    older = []
    for name in _archive_names(archive_dir):
        if since_month and name[:7] < since_month:
            continue
        try:
            with _open_regular(os.path.join(archive_dir, name), "r", root=_archive_root(history_path, archive_dir),
                               encoding="utf-8", errors="replace") as fh:
                older.append(fh.read())
        except FileNotFoundError:
            continue
    return older + live


def journal_blocks(
    history_path: str,
    archive_dir: Optional[str] = None,
    since_month: Optional[str] = None,
) -> List[Tuple[HistoryEntry, str]]:
    """Entries of this machine's journal with their blocks (see ``entry_blocks``), oldest file first.

    ``since_month`` (``YYYY-MM``) skips archives of earlier months, which hold
    no entry from that month on.
    """
    out: List[Tuple[HistoryEntry, str]] = []
    for text in _journal_texts(history_path, archive_dir, since_month):
        out.extend(entry_blocks(text))
    return out


def _older_sources(archive_dir: str, machines: MachineHistory):
    """``(bound, side, machine, path)`` of the archives and segments, newest month first.

    ``bound`` is a time no entry of the file reaches: an archive or segment of
    a month holds nothing later than that month (None when the name tells
    nothing, read first). This machine's archives (side 0) come before
    segments (side 1) of the same month.
    """
    out = []
    for name in _archive_names(archive_dir):
        out.append((_month_after(name[:7]), 0, None, os.path.join(archive_dir, name)))
    for label, path in machines.files:
        month = os.path.basename(path)[:7]
        bound = _month_after(month) if re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", month) else None
        out.append((bound, 1, label, path))
    out.sort(key=lambda source: source[1])
    out.sort(key=lambda source: source[0] or "~", reverse=True)  # stable: archives stay first
    return out


def _merge_into(chosen: Dict[str, HistoryEntry], machine: Optional[str], entries) -> None:
    """Add entries to a union by id: this machine's journal (machine None) wins, then the later copy."""
    for entry in entries:
        current = chosen.get(entry.id)
        if current is not None:
            if (current.machine is None) != (machine is None):
                if machine is not None:
                    continue
            elif entry.timestamp <= current.timestamp:
                continue
        chosen[entry.id] = replace(entry, machine=machine)


def merge_entries(files) -> List[HistoryEntry]:
    """A union by entry id of ``(machine, entries)`` pairs, oldest first.

    This machine's journal (machine None) wins over a segment; between copies
    from the same side, the later one wins.
    """
    chosen: Dict[str, HistoryEntry] = {}
    for machine, entries in files:
        _merge_into(chosen, machine, entries)
    return sorted(chosen.values(), key=lambda e: (e.timestamp, e.id))


def _machine_filter(machine: Optional[str], machines: Optional[MachineHistory]):
    """A predicate for ``read_history(machine=...)``, or None for every entry.

    ``local`` keeps this workspace's journal; this machine's label also keeps
    what this machine wrote in its other checkouts; another label, that machine's.
    """
    machine = (machine or "").strip()
    if not machine:
        return None
    if machine == "local":
        return lambda entry: entry.machine is None
    if machines is not None and machine == machines.label:
        return lambda entry: entry.machine in (None, machine)
    return lambda entry: entry.machine == machine


# --- Writer ------------------------------------------------------------------

class HistoryWriter:
    """Append-only writer for ``history.md``."""

    def __init__(
        self,
        history_path: Optional[str] = None,
        archive_dir: Optional[str] = None,
        rotation_kb: int = HISTORY_ROTATION_THRESHOLD_KB,
        dedup_tail: int = HISTORY_DEDUP_TAIL_SIZE,
    ):
        # Resolve defaults lazily via module attribute access so PEP 562 picks
        # up the current client repo root each time (see src/memory/config.py).
        self.history_path = history_path or _memory_config.HISTORY_FILE
        self.archive_dir = archive_dir or (
            os.path.join(os.path.dirname(history_path), "history") if history_path else _memory_config.HISTORY_ARCHIVE_DIR
        )
        self.rotation_kb = rotation_kb
        self.dedup_tail = dedup_tail

    # ------------------------------------------------------------------ public
    def append_entry(
        self,
        intent: str,
        action: str,
        outcome: str,
        files: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        dedupe_action: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
        payload_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a single entry; returns a JSON-friendly status dict.

        ``dedupe_action`` replaces ``action`` in the content hash, so a retry that
        differs only in an appended attribution line is still a duplicate.
        """
        if idempotency_key is not None and (not isinstance(idempotency_key, str)
                                             or not 1 <= len(idempotency_key) <= 256
                                             or not idempotency_key.strip()):
            raise MemoryWriteRejected("invalid_idempotency_key")
        intent = (intent or "").strip()
        action = (action or "").strip()
        outcome = (outcome or "").strip()
        if not (intent and action and outcome):
            return {
                "status": "error",
                "state": "rejected",
                "error": "intent, action, and outcome are all required",
            }

        entry_id = self._compute_entry_hash(intent, (dedupe_action or action).strip(), outcome)

        os.makedirs(os.path.dirname(self.history_path) or ".", exist_ok=True)

        # Commit the entry and its retry receipt in the SAME atomic replacement.
        # The journal is also the recoverable source for indexing and Git export.
        rotated_to: Optional[str] = None
        with _WRITE_LOCK, file_lock(_sidecar(self.history_path)):
            if os.path.islink(self.history_path):
                raise MemoryWriteRejected("journal_symlink_unsupported")
            # The journal's directory as the read in _append_locked finds it.
            # The replacement that commits the entry and the rotation after it
            # both reach this directory or refuse.
            directory_info = _journal_directory(self.history_path)
            result, fresh = self._append_locked(
                entry_id, intent, action, outcome, files, tags, metadata,
                idempotency_key, payload_fingerprint, directory_info,
            )

            # Rotate after the append handle is closed (Windows cannot move an
            # open file) but still inside both locks, so the move/merge cannot
            # race a second writer. The entry is already written: a rotation
            # failure is logged and never turns it into an error.
            try:
                rotated_to = self._maybe_rotate_locked(directory_info)
            except Exception as err:
                key = (self.history_path, getattr(err, "errno", None))
                if key not in _ROTATION_WARNED:
                    _ROTATION_WARNED.add(key)
                    logger.warning("history rotation failed for %s: %s", self.history_path, err)

        # Outside every history lock, and it only schedules the work: sharing the
        # entry waits for the library lock in the sync's own worker, never here.
        if fresh:
            _appended(self.history_path, result["entry_id"])
        elif idempotency_key is not None:
            # Reconcile after a lost commit notification, but do not classify an
            # old/replayed (possibly deleted) entry as newly approved for sharing.
            _appended(self.history_path, None)
        if rotated_to and idempotency_key is None:
            result["rotated_to"] = rotated_to
        return result

    def _append_locked(self, entry_id, intent, action, outcome, files, tags, metadata,
                       idempotency_key, payload_fingerprint, directory_info):
        try:
            with _open_regular(self.history_path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
                content = fh.read()
        except FileNotFoundError:
            content = self._render_header()
        if not content:
            content = self._render_header()
        metadata = dict(metadata or {})
        key_digest = None
        if idempotency_key is not None:
            scope = os.path.normcase(os.path.realpath(self.history_path))
            key_digest = hashlib.sha256(json.dumps(
                ["log_interaction/v1", scope, idempotency_key], ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            payload_digest = payload_fingerprint or hashlib.sha256(json.dumps(
                [intent, action, outcome, files or [], tags or [], metadata],
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            # The local journal includes archives and interrupted rotations.
            # Synced segments are another writer's data, not this acceptance scope.
            texts = [(self.history_path, content)]
            archives = _archive_names(self.archive_dir, strict=True)
            # Checked before the archives are read, so a receipt replayed from
            # one is synced in the directory that read checked.
            archive_info = _checked_directory(self.archive_dir) if archives else None
            paths = [self.history_path + ".rotating"] + [os.path.join(self.archive_dir, name) for name in archives]
            for path in paths:
                try:
                    with _open_regular(path, root=(None if path == self.history_path + ".rotating"
                                                   else _archive_root(self.history_path, self.archive_dir)),
                                       encoding="utf-8", errors="surrogateescape", newline="") as fh:
                        texts.append((path, fh.read()))
                except FileNotFoundError:
                    continue
            entry_id = key_digest[:12]  # Preserve the v1 Personal/Git id format.
            collision = False
            for path, text in texts:
                entries = HistoryReader._parse(text)
                receipts = [(entry.metadata or {}).get("_agents_write", {}) for entry in entries]
                receipts.extend(json.loads(match) for match in _RECEIPT_RE.findall(text))
                collision |= any(entry.id == entry_id for entry in entries)
                for receipt in receipts:
                    if not isinstance(receipt, dict) or receipt.get("key_digest") != key_digest:
                        continue
                    if receipt.get("payload_digest") != payload_digest:
                        raise MemoryWriteRejected("idempotency_conflict")
                    # A prior call could have died between replace and directory
                    # sync. Establish durability again before replaying its receipt.
                    live = path in (self.history_path, self.history_path + ".rotating")
                    _sync_recorded(path, directory_info if live else archive_info,
                                   root=None if live else _archive_root(self.history_path, self.archive_dir))
                    return {**receipt["result"], "path": self.history_path}, False
            if collision:
                raise MemoryWriteRejected("idempotency_collision")
        else:
            entries = HistoryReader._parse(content)
            complete_duplicate = False
            for entry in entries[-self.dedup_tail:]:
                if entry.id != entry_id:
                    continue
                if (entry.action and entry.intent == " ".join(intent.split())
                        and entry.outcome == " ".join(outcome.split())):
                    complete_duplicate = True
                elif (not entry.intent or not entry.action or not entry.outcome
                      or (entry.intent == " ".join(intent.split())
                          and " ".join(outcome.split()).startswith(entry.outcome))):
                    # Old append-in-place versions could leave a valid heading
                    # with missing/truncated fields. Do not acknowledge that as
                    # the complete write the caller is retrying.
                    content = without_entry(content, entry.id, entry.timestamp)
                else:
                    # A complete different entry is not evidence of a torn append.
                    raise MemoryWriteRejected("history_entry_conflict")
            if complete_duplicate:
                _sync_recorded(self.history_path, directory_info)
                return {"status": "duplicate", "entry_id": entry_id, "path": self.history_path,
                        "state": "durably_accepted"}, False

        timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        result = {"status": "recorded", "entry_id": entry_id, "path": self.history_path,
                  "timestamp": timestamp, "state": "durably_accepted"}
        if "answer_timestamp" in metadata:
            result["answer_timestamp"] = metadata["answer_timestamp"]
        if key_digest is not None:
            metadata["_agents_write"] = {"version": 1, "key_digest": key_digest,
                                        "payload_digest": payload_digest,
                                        "result": {key: value for key, value in result.items() if key != "path"}}
        block = self._render_entry(entry_id, timestamp, intent, action, outcome, files, tags, metadata)
        _replace(self.history_path, content + block, info=directory_info)
        return result, True

    def append_existing(self, entry: "HistoryEntry") -> Dict[str, Any]:
        """Append an entry kept elsewhere as it is: its id, time, fields and metadata (#292).

        Moving the fallback journal's entries into a project uses it. A journal that already
        holds the id, in ``history.md`` or an archive, keeps its own copy: ``duplicate``.
        """
        if entry.id in {known.id for known in HistoryReader(self.history_path, self.archive_dir).journal_entries()}:
            return {"status": "duplicate", "entry_id": entry.id, "path": self.history_path}
        os.makedirs(os.path.dirname(self.history_path) or ".", exist_ok=True)
        with _WRITE_LOCK, file_lock(_sidecar(self.history_path)):
            directory_info = _journal_directory(self.history_path)
            if not os.path.lexists(self.history_path):
                _replace(self.history_path, self._render_header(), info=directory_info)
            with _open_regular(self.history_path, "r+", encoding="utf-8", newline="") as fh:
                try:
                    _lock_exclusive(fh)
                    fh.seek(0, os.SEEK_END)
                    if fh.tell() == 0:
                        fh.write(self._render_header())
                        fh.flush()
                    if self._is_duplicate_from_handle(fh, entry.id):
                        return {"status": "duplicate", "entry_id": entry.id, "path": self.history_path}
                    fh.write(self._render_entry(entry.id, entry.timestamp, entry.intent, entry.action,
                                                entry.outcome, entry.files, entry.tags, entry.metadata))
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    _unlock(fh)
            try:
                self._maybe_rotate_locked(directory_info)
            except Exception as err:
                logger.warning("history rotation failed for %s: %s", self.history_path, err)
        _appended(self.history_path, entry.id)
        return {"status": "recorded", "entry_id": entry.id, "path": self.history_path, "timestamp": entry.timestamp}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _compute_entry_hash(intent: str, action: str, outcome: str) -> str:
        h = hashlib.sha256()
        h.update(intent.encode("utf-8"))
        h.update(b"\x1f")
        h.update(action.encode("utf-8"))
        h.update(b"\x1f")
        h.update(outcome.encode("utf-8"))
        return h.hexdigest()[:12]

    def _render_header(self) -> str:
        repo_name = os.path.basename(os.path.dirname(os.path.abspath(self.history_path))) or "repo"
        timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        return (
            "---\n"
            f"repo: {repo_name}\n"
            f"created: {timestamp}\n"
            f"format_version: {HISTORY_FORMAT_VERSION}\n"
            "---\n"
        )

    @staticmethod
    def _render_entry(
        entry_id: str,
        timestamp: str,
        intent: str,
        action: str,
        outcome: str,
        files: Optional[List[str]],
        tags: Optional[List[str]],
        metadata: Optional[Dict[str, Any]],
    ) -> str:
        # Collapse newlines to spaces so multi-line raw query/response
        # doesn't break the single-line **Field:** format that the parser expects.
        def _oneline(s: str) -> str:
            return " ".join(s.split())

        lines = [
            "",  # blank separator before the heading
            f"## {timestamp} | {entry_id}",
            f"**Intent:** {_oneline(intent)}",
            f"**Action:** {_oneline(action)}",
            f"**Outcome:** {_oneline(outcome)}",
        ]
        if files:
            lines.append(f"**Files:** {', '.join(_oneline(f) for f in files)}")
        if tags:
            normalized = [_oneline(t) for t in tags]
            normalized = [t if t.startswith("#") else f"#{t}" for t in normalized]
            lines.append(f"**Tags:** {' '.join(normalized)}")
        if metadata:
            lines.append(f"**Meta:** {json.dumps(metadata, ensure_ascii=False, sort_keys=True)}")
        lines.append("")  # trailing newline
        return "\n".join(lines) + "\n"

    def _is_duplicate_from_handle(self, fh, entry_id: str) -> bool:
        """Tail-scan the last ``self.dedup_tail`` entry IDs using the
        already-open file handle (avoids opening a second fd under the lock)."""
        pos = fh.tell()
        fh.seek(0)
        content = fh.read()
        fh.seek(pos)
        ids = _HEADER_RE.findall(content)
        recent = [match[1] for match in ids[-self.dedup_tail:]]
        return entry_id in recent

    def _maybe_rotate_locked(self, directory_info) -> Optional[str]:
        """Move ``history.md`` to ``history/YYYY-MM.md`` if it is too large.

        MUST be called while the caller still holds the exclusive lock on
        ``history.md`` — otherwise concurrent writers could race with the
        move/merge step. ``directory_info`` is the identity the caller took of
        the journal's directory before its own read and write; the move to
        ``.rotating`` and the fresh journal both reach that directory or refuse.

        Returns the archive path if rotation happened, else ``None``.
        """
        # A pending file left by an interrupted rotation is merged first, before
        # any early return, so its entries never stay hidden or get overwritten.
        pending = self.history_path + ".rotating"
        if os.path.lexists(pending):
            self._merge_pending_locked(pending)
        if not os.path.lexists(self.history_path):
            return None
        size_kb = os.path.getsize(self.history_path) / 1024
        if size_kb < self.rotation_kb:
            return None

        # Keep a durable recovery source until even a first archive is synced.
        # A failed merge may already have replaced the archive. Do not restore
        # pending to live and append to it: that would defeat suffix recovery.
        # Readers include pending, and the next rotation recovers it first.
        # The move goes through the directory the append checked, not its name:
        # by name, a directory swapped in after the append's commit would have
        # its own journal moved aside and merged instead.
        directory = os.path.dirname(self.history_path) or "."
        message = "journal directory changed before it was rotated"
        with _anchored_dir(directory, directory_info, message) as dir_fd:
            if dir_fd is None:
                _refuse_swapped_dir(directory, directory_info, message)
            _move_onto(directory, os.path.basename(self.history_path), os.path.basename(pending), dir_fd)
            _sync_moved(directory, dir_fd)
        archive_path = self._merge_pending_locked(pending)

        # Recreate fresh history.md with header pointing at the archive.
        _replace(self.history_path, self._render_header() + (
                f"\n> Previous entries archived to "
                f"`{os.path.relpath(archive_path, os.path.dirname(self.history_path))}` "
                f"on {_dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')}.\n"
            ), info=directory_info)

        return archive_path

    @staticmethod
    def _discard_pending(pending: str) -> None:
        """Remove or safely empty an already-archived pending file, never raising.

        The archive holds the payload at this point, so a cleanup failure must
        not look like a failed merge: a leftover payload is recognized by
        `_archive_ends_with` and skipped on the next recovery.
        """
        try:
            os.unlink(pending)
            _sync_directory(os.path.dirname(pending) or ".")
            return
        except OSError:
            pass
        try:
            with _open_regular(pending, "r+b") as stream:
                stream.truncate(0)
                stream.flush()
                os.fsync(stream.fileno())
        except FileNotFoundError:
            pass  # The unlink succeeded but syncing the directory failed.
        except OSError as err:
            key = (pending, getattr(err, "errno", None))
            if key not in _ROTATION_WARNED:
                _ROTATION_WARNED.add(key)
                logger.warning("could not clear archived pending file %s: %s", pending, err)

    @staticmethod
    def _archive_ends_with(archive_path: str, payload: str, *, root=None) -> bool:
        data = payload.encode("utf-8", errors="surrogateescape")
        try:
            with _open_regular(archive_path, "rb", root=root) as fh:
                size = fh.seek(0, os.SEEK_END)
                if size < len(data):
                    return False
                fh.seek(size - len(data))
                return fh.read() == data
        except OSError:
            return False

    def _merge_pending_locked(self, pending: str) -> str:
        """Append the pending file to its month's archive, then empty it.

        The archive is rebuilt in a temporary file and replaced atomically, so a
        failure or crash cannot leave partial content. Returns the archive path.
        """
        # Suffix recovery compares bytes; universal newline conversion would
        # turn an already archived CRLF payload into a second, different copy.
        with _open_regular(pending, "r", encoding="utf-8", errors="surrogateescape", newline="") as src:
            payload = src.read()
        if not payload:
            # Emptied after an archived merge: nothing to add to the archive.
            try:
                os.unlink(pending)
            except OSError:
                pass
            return ""
        matches = _HEADER_RE.findall(payload)
        month = (max(match[0] for match in matches) if matches else _dt.datetime.now(_dt.timezone.utc).isoformat())[:7]
        os.makedirs(self.archive_dir, exist_ok=True)
        archive_dir_info = os.lstat(self.archive_dir)
        _regular_kind(archive_dir_info, directory=True)
        _sync_directory(os.path.dirname(self.archive_dir) or ".")
        archive_path = os.path.join(self.archive_dir, f"{month}.md")
        # os.path.exists follows the link, so a dangling archive symlink reads as
        # absent: the replace below would overwrite the link itself while this
        # append reports the entry as recorded. Decide from the entry instead,
        # and let a link or a special file raise rather than be replaced.
        try:
            _regular_kind(os.lstat(archive_path))
        except FileNotFoundError:
            existed = False
        else:
            existed = True
        archive_root = _archive_root(self.history_path, self.archive_dir)
        if existed and self._archive_ends_with(archive_path, payload, root=archive_root):
            # A crash after the append but before the unlink: already archived.
            # Synced in the directory checked above, as a replayed receipt is.
            _sync_recorded(archive_path, archive_dir_info, root=archive_root)
            self._discard_pending(pending)
            return archive_path
        # Build the merged archive beside the real one and replace it in one
        # step: a crash or error at any point leaves the archive as it was, and
        # the pending file is only emptied after the replace.
        archive_name = os.path.basename(archive_path)
        moved = "journal archive directory changed while rotating"
        with _anchored_dir(self.archive_dir, archive_dir_info, moved) as dir_fd:
            if dir_fd is None:
                # Windows takes no directory descriptor, so the temporary file is
                # created by name: re-identify the directory first, or the whole
                # merged archive would be written into a substituted target
                # before the check at the move refused it.
                _refuse_swapped_dir(self.archive_dir, archive_dir_info, moved)
            fd, temp = _temp_beside(self.archive_dir, archive_name, dir_fd)
            try:
                stream = os.fdopen(fd, "wb")
                fd = None  # stream owns the descriptor, including on a later write failure.
                with stream as dst:
                    if existed:
                        with _open_regular(archive_path, "rb", root=archive_root) as old:
                            shutil.copyfileobj(old, dst)
                        dst.write(b"\n\n<!-- merged on rotation -->\n\n")
                    dst.write(payload.encode("utf-8", errors="surrogateescape"))
                    dst.flush()
                    os.fsync(dst.fileno())
                if dir_fd is None:
                    # The move reaches the directory by name too: re-identify it
                    # immediately before, and refuse a swap rather than send the
                    # archive to the link's target. Elsewhere the descriptor above
                    # holds the directory itself and there is no window.
                    _refuse_swapped_dir(self.archive_dir, archive_dir_info, moved)
                _move_onto(self.archive_dir, temp, archive_name, dir_fd)
                _sync_moved(self.archive_dir, dir_fd)
            except Exception:
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                _discard_temp(self.archive_dir, temp, dir_fd)
                raise
        self._discard_pending(pending)
        return archive_path

    def _read_newest_timestamp(self) -> Optional[str]:
        try:
            with _open_regular(self.history_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except FileNotFoundError:
            return None
        matches = _HEADER_RE.findall(content)
        if not matches:
            return None
        return max(match[0] for match in matches)  # the newest, wherever it is in the file


# --- Reader ------------------------------------------------------------------

class HistoryReader:
    """Parses ``history.md`` and serves recency/since/tag queries."""

    def __init__(self, history_path: Optional[str] = None, archive_dir: Optional[str] = None):
        self.history_path = history_path or _memory_config.HISTORY_FILE
        self.archive_dir = archive_dir or os.path.join(os.path.dirname(self.history_path), "history")

    def read_all(self) -> List[HistoryEntry]:
        """The entries of ``history.md`` alone, in file order.

        Reads under the shared history lock (``_reading``).
        """
        if not os.path.lexists(self.history_path):
            return []
        with _reading(self.history_path):
            return self._read_unlocked()

    def _read_unlocked(self) -> List[HistoryEntry]:
        """``read_all`` for a caller that already holds the history lock."""
        try:
            with _open_regular(self.history_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except FileNotFoundError:
            return []
        return self._parse(content)

    def _live_files(self) -> List[Tuple[str, str, Tuple[HistoryEntry, ...]]]:
        """``(path, sha256, entries)`` of a pending rotation and ``history.md``, under the shared lock."""
        pending = self.history_path + ".rotating"
        if not (os.path.lexists(self.history_path) or os.path.lexists(pending)):
            return []
        out = []
        with _reading(self.history_path):
            for path in (pending, self.history_path):
                parsed = parsed_file(path)
                if parsed is not None:
                    out.append((path, parsed[0], parsed[1]))
        return out

    def _older_file(self, machine: Optional[str], path: str, deleted: FrozenSet[Tuple[str, str]] = frozenset()):
        """``(sha256, entries)`` of an archive or segment; an unreadable segment is skipped, an archive is not.

        A segment's last block without its ``**Machine:**`` line was cut short by a crash or a full
        disk on the machine that writes it; it is left out until that machine writes it again whole.
        A segment's copy of a ``deleted`` entry is left out as well.
        """
        if machine is None:
            return parsed_file(path, root=_archive_root(self.history_path, self.archive_dir))
        try:
            parsed = parsed_file(path)
        except OSError:
            return None
        if parsed is not None and parsed[1] and parsed[1][-1].machine is None:
            parsed = parsed[0], parsed[1][:-1]
        if parsed is not None and deleted:
            parsed = parsed[0], tuple(e for e in parsed[1] if (e.id, e.timestamp) not in deleted)
        return parsed

    def merged_files(self, machines: MachineHistory) -> List[Tuple[Optional[str], str, str, Tuple[HistoryEntry, ...]]]:
        """``(machine, path, sha256, entries)`` of every file of the merged history.

        ``history.md`` and a pending rotation are read under the shared history
        lock, the archives after it (rotation replaces or creates them whole, so
        an entry that moved meanwhile is read twice and deduplicated, never
        missed) and the segments without any history lock.
        """
        files = [(None, path, digest, entries) for path, digest, entries in self._live_files()]
        for _bound, _side, machine, path in _older_sources(self.archive_dir, machines):
            parsed = self._older_file(machine, path, machines.deleted)
            if parsed is not None:
                files.append((machine, path, parsed[0], parsed[1]))
        return files

    def journal_entries(self) -> List[HistoryEntry]:
        """This checkout's whole journal (``history.md``, a pending rotation, ``history/*.md``), parsed once per change."""
        entries = [entry for _, _, items in self._live_files() for entry in items]
        for name in _archive_names(self.archive_dir):
            parsed = parsed_file(os.path.join(self.archive_dir, name), root=_archive_root(self.history_path, self.archive_dir))
            if parsed is not None:
                entries.extend(parsed[1])
        return entries

    def read_merged(self, machines: MachineHistory) -> List[HistoryEntry]:
        """Every entry of the merged history, oldest first; see ``merge_entries``."""
        return merge_entries((machine, entries) for machine, _, _, entries in self.merged_files(machines))

    def read_recent(
        self,
        limit: int = 20,
        since: Optional[str] = None,
        machine: Optional[str] = None,
    ) -> List[HistoryEntry]:
        """Newest-first list, optionally filtered by ``since`` (ISO timestamp prefix).

        With the user library sync set up for this repository, the list is the
        merged history (``history.md``, ``history/*.md`` and the synced
        segments), read newest month first until ``limit`` is filled; otherwise
        ``history.md`` and any pending rotation. ``machine`` filters as ``_machine_filter`` says.
        """
        limit = max(1, limit) if limit > 0 else 20
        machines = _machine_history(self.history_path)
        keep = _machine_filter(machine, machines)
        if machines is None:
            entries = [entry for _, _, items in self._live_files() for entry in items]
        else:
            entries = self._recent_merged(machines, limit, since, keep)
        if keep is not None:
            entries = [e for e in entries if keep(e)]
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        if since:
            entries = [e for e in entries if e.timestamp >= since]
        return entries[:limit]

    def find(self, entry_id: str) -> Optional[HistoryEntry]:
        """The entry with ``entry_id`` as a merged read shows it (see ``merge_entries``), or None.

        Looks through this checkout's whole journal (``history.md``, a pending rotation and
        ``history/*.md``), so an entry that rotation archived is still found, and with the user
        library sync set up for this repository through the synced segments too. Files are read
        newest month first, and the search stops at this checkout's own copy, which wins over a
        segment's. A segment that cannot be read is skipped; an archive of this checkout that
        cannot be read raises, so a refused read is never reported as "no such entry".
        """
        chosen: Dict[str, HistoryEntry] = {}
        for _path, _digest, entries in self._live_files():
            _merge_into(chosen, None, [e for e in entries if e.id == entry_id])
        machines = _machine_history(self.history_path) or MachineHistory("")
        for bound, side, machine, path in _older_sources(self.archive_dir, machines):
            found = chosen.get(entry_id)
            if found is not None and bound is not None and bound <= found.timestamp:
                # Neither this file nor an older one holds a later copy: this checkout's copy is
                # final, and a segment's can still lose only to this checkout's (an archive's).
                if found.machine is None:
                    break
                if side == 1:
                    continue
            # _older_file already skips an unreadable synced segment and
            # returns None, so the only error that reaches here belongs to this
            # checkout's own archive. Swallowing it would answer a lookup with
            # an empty success, or with a segment's older copy, exactly when a
            # guarded read refused a substituted archive. It is raised instead.
            parsed = self._older_file(machine, path, machines.deleted)
            if parsed is not None:
                _merge_into(chosen, machine, [e for e in parsed[1] if e.id == entry_id])
        return chosen.get(entry_id)

    def _recent_merged(self, machines: MachineHistory, limit: int, since: Optional[str], keep) -> List[HistoryEntry]:
        """The merged entries that can be among the ``limit`` newest kept ones.

        Months are read newest first; once ``limit`` kept entries are newer than
        everything an older file can hold, the older files are not read. (An
        entry in both this machine's journal and a newer segment may then show
        the segment's copy: the journal copy is older than every entry shown.)
        """
        chosen: Dict[str, HistoryEntry] = {}
        for _path, _digest, entries in self._live_files():
            _merge_into(chosen, None, entries)

        def enough(bound: str) -> bool:
            if since and bound <= since:
                return True
            times = sorted((e.timestamp for e in chosen.values() if (keep is None or keep(e))
                            and (not since or e.timestamp >= since)), reverse=True)
            return len(times) >= limit and bound <= times[limit - 1]

        sources = _older_sources(self.archive_dir, machines)
        index = 0
        while index < len(sources):
            bound = sources[index][0]
            if bound is not None and enough(bound):
                break
            while index < len(sources) and sources[index][0] == bound:
                _bound, _side, source_machine, path = sources[index]
                parsed = self._older_file(source_machine, path, machines.deleted)
                if parsed is not None:
                    _merge_into(chosen, source_machine, parsed[1])
                index += 1
        return list(chosen.values())

    # ------------------------------------------------------------------ parser
    @staticmethod
    def _parse(content: str) -> List[HistoryEntry]:
        # Split content on heading boundaries while keeping the headings.
        positions = [m.start() for m in _HEADER_RE.finditer(content)]
        if not positions:
            return []
        positions.append(len(content))
        out: List[HistoryEntry] = []
        for i in range(len(positions) - 1):
            block = content[positions[i] : positions[i + 1]]
            entry = HistoryReader._parse_block(block)
            if entry:
                out.append(entry)
        return out

    @staticmethod
    def _parse_block(block: str) -> Optional[HistoryEntry]:
        lines = block.splitlines()
        if not lines:
            return None
        header = _HEADER_RE.match(lines[0])
        if not header:
            return None
        ts = header.group("ts")
        eid = header.group("id")
        fields: Dict[str, str] = {}
        for line in lines[1:]:
            m = _FIELD_RE.match(line)
            if m:
                fields[m.group("name").lower()] = m.group("value").strip()
        intent = fields.get("intent", "")
        action = fields.get("action", "")
        outcome = fields.get("outcome", "")
        files_raw = fields.get("files", "")
        files = [f.strip() for f in files_raw.split(",") if f.strip()] if files_raw else []
        tags_raw = fields.get("tags", "")
        tags = [t.strip() for t in tags_raw.split() if t.strip()] if tags_raw else []
        meta_raw = fields.get("meta", "")
        metadata = None
        if meta_raw:
            try:
                metadata = json.loads(meta_raw)
            except json.JSONDecodeError:
                metadata = {"_raw": meta_raw}
        return HistoryEntry(
            id=eid,
            timestamp=ts,
            intent=intent,
            action=action,
            outcome=outcome,
            files=files,
            tags=tags,
            metadata=metadata,
            # Only a synced segment carries it; a merged read sets it from the file instead.
            machine=fields.get("machine") or None,
        )


# --- Lazy semantic store -----------------------------------------------------

class HistoryStore:
    """Lazy semantic recall over history entries.

    Wrapping ``NumpyVectorStore`` so callers don't pay numpy/embedder import
    costs unless they actually run a semantic query. With the user library
    sync set up for the repository, the index covers the merged history of
    every machine (see ``HistoryReader.read_recent``).
    """

    def __init__(
        self,
        history_path: Optional[str] = None,
        data_dir: Optional[str] = None,
        store_name: str = HISTORY_VECTOR_STORE_NAME,
        archive_dir: Optional[str] = None,
    ):
        self.history_path = history_path or _memory_config.HISTORY_FILE
        self.data_dir = data_dir or _memory_config.MEMORY_DATA_DIR
        self.store_name = store_name
        self.archive_dir = archive_dir or os.path.join(os.path.dirname(self.history_path), "history")
        self._store = None  # lazy
        self._index_lock = threading.RLock()

    # ------------------------------------------------------------------ public
    def search(
        self,
        query: str,
        limit: int = 5,
        embed_query=None,
        embed_texts=None,
        machine: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return top-``limit`` entries with semantic distance.

        ``embed_query`` / ``embed_texts`` are injectable so tests can swap in
        deterministic fakes; production callers leave them None and the
        ONNX-backed defaults are loaded lazily. ``machine`` filters as in
        ``HistoryReader.read_recent``.
        """
        from src.engine.embedder import model_fingerprint
        with self._index_lock:
            machines = _machine_history(self.history_path)
            checked = model_fingerprint()
            store = self._ensure(machines, embed_texts)
            if store.count() == 0:
                return []
            if embed_query is None:
                from src.engine.embedder import embed_query as _eq
                embed_query = _eq
            vec = embed_query(query)
            if model_fingerprint() != checked:
                # Loading the model changed the fingerprint the index was
                # checked against: check it again for the loaded snapshot.
                store = self._ensure(machines, embed_texts)
                if store.count() == 0:
                    return []
            keep = _machine_filter(machine, machines)
            result = store.query(vec, n_results=store.count() if keep else limit)
            out: List[Dict[str, Any]] = []
            for i, eid in enumerate(result.ids):
                meta = result.metadatas[i] or {}
                if keep is not None and not keep(HistoryEntry(eid, "", "", "", "", machine=meta.get("machine"))):
                    continue
                out.append({
                    "id": eid,
                    "distance": float(result.distances[i]),
                    "timestamp": meta.get("timestamp", ""),
                    "intent": meta.get("intent", ""),
                    **self._document_fields(result.documents[i]),
                    "tags": meta.get("tags", []),
                    "machine": meta.get("machine"),
                })
                if len(out) >= limit:
                    break
            return out

    def ensure_index(self, embed_texts=None):
        """Build / refresh the vector index when its content fingerprint changes.

        The fingerprint is sha256(history.md) plus the embedding fingerprint of
        the loaded model (``src.engine.embedder.model_fingerprint``: model,
        revision, index schema, preprocessing, embedding contract and ONNX Runtime version), compared with
        ``.history_fingerprint`` in ``data_dir``; a missing index file also
        triggers a rebuild. For a merged history, the first part hashes every
        file of it instead (``HistoryReader.merged_files``).

        Thread-safe: serialized via ``_index_lock`` so concurrent
        ``read_history(query=...)`` calls don't race on rebuild.

        Returns the underlying ``NumpyVectorStore``.
        """
        with self._index_lock:
            return self._ensure(_machine_history(self.history_path), embed_texts)

    # ------------------------------------------------------------------ helpers
    def _ensure(self, machines: Optional[MachineHistory], embed_texts=None):
        with self._index_lock:
            from src.engine.vector_store import NumpyVectorStore  # heavy import — defer
            if self._store is None:
                self._store = NumpyVectorStore(name=self.store_name, data_dir=self.data_dir)
            if machines is not None:
                return self._ensure_merged(machines, embed_texts)

            with _reading(self.history_path):
                # Content-based invalidation catches edits that preserve mtimes.
                try:
                    with _open_regular(self.history_path, "rb") as source:
                        data = source.read()
                except FileNotFoundError:
                    # Missing history clears stale vectors without loading an
                    # embedding model. Unsafe artifacts remain storage errors.
                    if self._store.count() > 0:
                        self._store.clear()
                        self._store.save()
                    return self._store
            from src.engine.embedder import model_fingerprint
            digest = hashlib.sha256(data).hexdigest() + ":" + model_fingerprint()
            # Embedding runs under the index's own lock, never the history lock:
            # appends and readers do not wait for the model.
            with file_lock(os.path.join(self.data_dir, f".{self.store_name}.lock")):
                self._refresh(digest, embed_texts, load=lambda: HistoryReader._parse(data.decode("utf-8")))
            return self._store

    def _ensure_merged(self, machines: MachineHistory, embed_texts=None):
        """The index of the merged history; embeds only entries it has not embedded yet.

        The files are read once (``HistoryReader.merged_files``; unchanged ones
        come from the parse cache) and the shared history lock is released
        before any embedding, so appends and readers never wait for the model;
        the digest and the entries come from the same reading.
        """
        files = HistoryReader(self.history_path, self.archive_dir).merged_files(machines)
        if not files:
            if self._store.count() > 0:
                self._store.clear()
                self._store.save()
            # Content that comes back must be indexed again, not matched with an empty store.
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(self.data_dir, ".history_fingerprint"))
            return self._store
        from src.engine.embedder import model_fingerprint
        digest = hashlib.sha256(b"merged history\0")
        for machine, path, sha, _entries in files:
            digest.update(f"{machine or ''}\0{os.path.basename(path)}\0{sha}\0".encode("utf-8"))
        for entry_id, timestamp in sorted(machines.deleted):  # a deletion changes the merged entries too
            digest.update(f"deleted\0{entry_id}\0{timestamp}\0".encode("utf-8"))
        # Rebuilds of one index from several processes take turns.
        with file_lock(os.path.join(self.data_dir, f".{self.store_name}.lock")):
            self._refresh(digest.hexdigest() + ":" + model_fingerprint(), embed_texts,
                          load=lambda: merge_entries((machine, entries) for machine, _, _, entries in files))
        return self._store

    def _refresh(self, digest: str, embed_texts=None, load=None) -> None:
        """Rebuild the index unless ``.history_fingerprint`` already vouches for ``digest``.

        ``load`` returns the entries to index (default: those of ``history.md``);
        it runs only when a rebuild is needed.
        """
        from src.engine.vector_store import NumpyVectorStore
        from src.daemon.state import atomic_private
        marker = os.path.join(self.data_dir, ".history_fingerprint")
        try:
            with open(marker) as stream: saved = stream.read()
        except FileNotFoundError:
            saved = None
        if saved != digest or not os.path.exists(os.path.join(self.data_dir, f"{self.store_name}.npz")):
            # Stored vectors stay valid only for the embedding
            # configuration that produced them.
            reuse = saved is not None and saved.partition(":")[2] == digest.partition(":")[2]
            if reuse:
                # Reuse the vectors this marker describes: another
                # process may have rewritten the store since it loaded.
                self._store = NumpyVectorStore(name=self.store_name, data_dir=self.data_dir)
            if saved is not None:
                # A rebuild that saves the store but dies before the
                # new marker must not leave the old marker vouching
                # for vectors from another configuration.
                with contextlib.suppress(FileNotFoundError):
                    os.remove(marker)
            self._rebuild(embed_texts=embed_texts, reuse_vectors=reuse, entries=load() if load else None)
            atomic_private(marker, digest)

    def _rebuild(self, embed_texts=None, reuse_vectors: bool = False,
                 entries: Optional[List[HistoryEntry]] = None) -> None:
        """Replace the index with every current entry.

        ``entries`` defaults to those of ``history.md``; the callers pass the
        entries they hashed. With *reuse_vectors*, an entry
        whose id and formatted document are already stored keeps its vector,
        so only new or edited entries are embedded; the caller passes it only
        when the stored vectors come from the current embedding fingerprint.
        """
        if entries is None:
            entries = HistoryReader(self.history_path).read_all()
        if not entries:
            self._store.clear()
            self._store.save()
            return

        if embed_texts is None:
            from src.engine.embedder import embed_texts as _et
            embed_texts = _et

        # Deduplicate by id — entries outside the dedup_tail window can share
        # the same content hash. Keep the latest (last) entry for each id.
        seen: dict[str, int] = {}
        for i, e in enumerate(entries):
            seen[e.id] = i
        unique_indices = sorted(seen.values())
        entries = [entries[i] for i in unique_indices]

        import numpy as np  # heavy import — defer

        ids = [e.id for e in entries]
        documents = [self._format_for_embedding(e) for e in entries]
        vectors = self._reusable_vectors(dict(zip(ids, documents))) if reuse_vectors else {}
        missing = [i for i, id_ in enumerate(ids) if id_ not in vectors]
        if missing:
            fresh = list(embed_texts([documents[i] for i in missing]))
            if len(fresh) != len(missing):
                raise ValueError(f"Embedder returned {len(fresh)} vectors for {len(missing)} history entries")
            vectors.update((ids[i], vector) for i, vector in zip(missing, fresh))
        embeddings = np.stack([np.asarray(vectors[id_], dtype=np.float32) for id_ in ids])

        metadatas = [
            {
                "timestamp": e.timestamp,
                "intent": e.intent,
                "tags": e.tags,
                "files": e.files,
                "machine": e.machine,
            }
            for e in entries
        ]
        self._store.replace(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
        self._store.save()

    def _reusable_vectors(self, documents: dict[str, str]) -> dict:
        """Map each id whose stored document equals ``documents[id]`` to its stored vector."""
        stored = self._store.get(list(documents))
        unchanged = [id_ for id_, document in zip(stored.ids, stored.documents)
                     if document == documents[id_]]
        return self._store.get_embeddings(unchanged)

    @staticmethod
    def _format_for_embedding(entry: HistoryEntry) -> str:
        parts = [
            f"Intent: {entry.intent}",
            f"Action: {entry.action}",
            f"Outcome: {entry.outcome}",
        ]
        if entry.tags:
            parts.append("Tags: " + " ".join(entry.tags))
        return "\n".join(parts)

    @staticmethod
    def _document_fields(document: str) -> Dict[str, str]:
        """``action`` and ``outcome`` back from a ``_format_for_embedding`` document.

        Each of its fields is one line: the writer collapses an entry's newlines.
        """
        fields = {"action": "", "outcome": ""}
        for line in document.split("\n"):
            name, _, value = line.partition(": ")
            if name in ("Action", "Outcome"):
                fields[name.lower()] = value
        return fields


__all__ = [
    "ENTRY_ID",
    "EntryChanged",
    "HistoryEntry",
    "HistoryReader",
    "HistoryStore",
    "HistoryWriter",
    "MachineHistory",
    "delete_entry",
    "entry_blocks",
    "journal_blocks",
    "merge_entries",
    "parsed_file",
    "set_sync",
    "without_entry",
]
