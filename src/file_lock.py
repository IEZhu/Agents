"""Stable sidecar locks; replacing/rotating a data file never replaces its lock.

POSIX uses ``flock``. Windows uses ``LockFileEx`` on the first byte of the lock
file. Either way the lock excludes other processes, and other opens of the same
file within one process. Without both, or on a Windows file system without
byte-range locks (some network shares), a process-local lock serializes the
threads of this process only.
"""
from contextlib import contextmanager
import errno
from pathlib import Path
import os
import threading
import weakref

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

if fcntl is None and msvcrt is not None:
    import ctypes
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        """OVERLAPPED with only the fields LockFileEx reads: the lock starts at offset 0."""
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                            wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(_Overlapped)]
    _UnlockFileEx.restype = wintypes.BOOL
    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _ERROR_INVALID_FUNCTION = 1
    _ERROR_NOT_SUPPORTED = 50
    _ERROR_LOCK_VIOLATION = 33

    def _windows_lock(fd, *, blocking, shared):
        """True once locked; False on a file system without byte-range locks."""
        flags = (0 if shared else _LOCKFILE_EXCLUSIVE_LOCK) | (0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY)
        if _LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(_Overlapped())):
            return True
        code = ctypes.get_last_error()
        if code == _ERROR_LOCK_VIOLATION:
            raise BlockingIOError(errno.EWOULDBLOCK, "The lock file is held by another holder")
        if code in (_ERROR_INVALID_FUNCTION, _ERROR_NOT_SUPPORTED):
            return False
        raise ctypes.WinError(code)

    def _windows_unlock(fd):
        _UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(_Overlapped()))

_fallback_locks = weakref.WeakValueDictionary()
_fallback_guard = threading.Lock()


@contextmanager
def file_lock(path, *, blocking=True, shared=False):
    """Yield the lease fd of an OS lock, or None for the process-local fallback.

    ``blocking=False`` raises ``BlockingIOError`` instead of waiting. The fallback
    does not provide cross-process exclusion; shared daemon control remains macOS-only.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(fd, operation | (0 if blocking else fcntl.LOCK_NB))
            yield fd
        finally:
            os.close(fd)
        return
    if msvcrt is not None:
        if path.is_symlink():  # Windows has no O_NOFOLLOW
            raise OSError(errno.ELOOP, "A lock file must not be a symlink", str(path))
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_BINARY | os.O_NOINHERIT, 0o600)
        try:
            locked = _windows_lock(fd, blocking=blocking, shared=shared)
        except BaseException:
            os.close(fd)
            raise
        if locked:
            try:
                yield fd
            finally:
                try:
                    _windows_unlock(fd)
                finally:
                    os.close(fd)
            return
        os.close(fd)  # no byte-range locks here: fall through to the process-local lock
    key = os.path.normcase(str(path.resolve()))
    with _fallback_guard:
        lock = _fallback_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _fallback_locks[key] = lock
    if not lock.acquire(blocking=blocking):
        raise BlockingIOError("Process-local lock is already held")
    try:
        yield None
    finally:
        lock.release()
