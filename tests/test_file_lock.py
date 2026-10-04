from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_stdio_history_import_and_write_without_fcntl(tmp_path):
    result = subprocess.run([sys.executable, "-c", '''
import sys
sys.modules["fcntl"] = None
from src.memory.history import HistoryWriter
writer = HistoryWriter(history_path=sys.argv[1])
writer.append_entry("portable history", "append", "written")
''', str(tmp_path / "history.md")], cwd=ROOT, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert "portable history" in (tmp_path / "history.md").read_text()


def test_fallback_serializes_threads_without_posix_flags(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    spec = importlib.util.spec_from_file_location("portable_file_lock", ROOT / "src/file_lock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "history.lock"

    def acquire(path):
        with module.file_lock(path, blocking=False):
            return True

    with ThreadPoolExecutor(max_workers=1) as executor:
        with module.file_lock(target):
            with pytest.raises(BlockingIOError):
                executor.submit(acquire, target).result(timeout=5)
            assert executor.submit(acquire, tmp_path / "other.lock").result(timeout=5)
        assert executor.submit(acquire, target).result(timeout=5)


# A process that holds a lock until it reads a line. On POSIX this exercises flock,
# on Windows LockFileEx, the cross-process exclusion the user library relies on.
HOLDER = '''
import sys
from src.file_lock import file_lock
with file_lock(sys.argv[1], shared=sys.argv[2] == "shared"):
    print("held", flush=True)
    sys.stdin.readline()
'''


def _hold(path, mode):
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(path), mode], cwd=ROOT, text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert holder.stdout.readline().strip() == "held", holder.stderr.read()
    return holder


def _release(holder):
    holder.stdin.write("\n")
    holder.stdin.flush()
    assert holder.wait(timeout=10) == 0, holder.stderr.read()


@pytest.mark.parametrize("held, wanted, conflict", [
    ("exclusive", "exclusive", True), ("exclusive", "shared", True),
    ("shared", "exclusive", True), ("shared", "shared", False)])
def test_lock_excludes_another_process(tmp_path, held, wanted, conflict):
    from src.file_lock import file_lock
    target = tmp_path / "library.lock"
    holder = _hold(target, held)
    try:
        if conflict:
            with pytest.raises(BlockingIOError):
                with file_lock(target, blocking=False, shared=wanted == "shared"):
                    pass
        else:
            with file_lock(target, blocking=False, shared=True) as lease:
                assert lease is not None
    finally:
        _release(holder)
    with file_lock(target, blocking=False):  # free once the holder has released it
        pass


def test_blocking_lock_waits_for_another_process(tmp_path):
    from src.file_lock import file_lock
    target = tmp_path / "library.lock"
    holder = _hold(target, "exclusive")
    acquired = threading.Event()

    def wait_for_lock():
        with file_lock(target):
            acquired.set()

    waiting = threading.Thread(target=wait_for_lock, daemon=True)  # a lost wake-up must not hang the run
    waiting.start()
    try:
        assert not acquired.wait(0.5)  # still held by the other process
    finally:
        _release(holder)
    waiting.join(timeout=10)
    assert acquired.is_set()


@pytest.mark.skipif(sys.platform != "win32", reason="LockFileEx exists only on Windows")
def test_windows_without_byte_range_locks_falls_back_to_the_process_lock(tmp_path, monkeypatch):
    from src import file_lock as module
    monkeypatch.setattr(module, "_windows_lock", lambda fd, **kwargs: False)  # e.g. some network shares
    target = tmp_path / "share.lock"
    with module.file_lock(target) as lease:
        assert lease is None
        with pytest.raises(BlockingIOError):
            with module.file_lock(target, blocking=False):
                pass
    with module.file_lock(target, blocking=False):
        pass
