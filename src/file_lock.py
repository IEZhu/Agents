"""Stable sidecar locks; replacing/rotating a data file never replaces its lock."""
from contextlib import contextmanager
from pathlib import Path
import os
import threading
import weakref

try:
    import fcntl
except ImportError:
    fcntl = None

_fallback_locks = weakref.WeakValueDictionary()
_fallback_guard = threading.Lock()


@contextmanager
def file_lock(path, *, blocking=True, shared=False):
    """Yield a POSIX lease fd, or None for a process-local fallback.

    Without flock, serialize all access within this process. This does not
    provide cross-process exclusion; shared daemon control remains macOS-only.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
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
        return
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(fd, operation | (0 if blocking else fcntl.LOCK_NB))
        yield fd
    finally:
        os.close(fd)
