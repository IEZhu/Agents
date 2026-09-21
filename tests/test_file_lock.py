from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

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
