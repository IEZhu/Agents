"""Bounded diagnostics; filesystem work runs outside the event loop."""
from pathlib import Path
import time


def prune_debug(directory, *, days=7, max_bytes=100 * 1024**2):
    directory = Path(directory)
    cutoff = time.time() - days * 86400
    retained = []
    for path in directory.glob("*/*.json"):
        try:
            stat = path.stat()
            if stat.st_mtime < cutoff:
                path.unlink()
            else:
                retained.append((stat.st_mtime, stat.st_size, path))
        except FileNotFoundError:
            continue
    size = sum(item[1] for item in retained)
    for _, amount, path in sorted(retained):
        if size <= max_bytes: break
        path.unlink(missing_ok=True)
        size -= amount


class LogStream:
    """Capture import-time stdout/stderr in the rotating service logger."""
    def __init__(self, logger, level):
        self.logger, self.level = logger, level

    def write(self, text):
        for line in text.splitlines():
            if line.strip(): self.logger.log(self.level, "%s", line[:16384])
        return len(text)

    def flush(self): pass
    def isatty(self): return False
