"""Stable sidecar locks; replacing/rotating a data file never replaces its lock."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import os


@contextmanager
def file_lock(path, *, blocking=True, shared=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(fd, operation | (0 if blocking else fcntl.LOCK_NB))
        yield fd
    finally:
        os.close(fd)
