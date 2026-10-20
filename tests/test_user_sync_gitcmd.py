"""Bounded regressions for Git's binary input and batch-output backpressure."""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys

import pytest

from src.user_sync import gitcmd


@pytest.fixture
def python_git(tmp_path):
    return gitcmd.Git(tmp_path, config=tmp_path / "config", ssh=None, executable=sys.executable)


@pytest.mark.parametrize("data", [b"", b"\x00\xff\r\n", bytes(range(256)) * 8192],
                         ids=["empty", "binary", "large"])
def test_binary_input_after_large_output(python_git, data):
    # Output precedes input consumption, as it can in cat-file --batch.
    code = ("import sys; sys.stdout.buffer.write(b'p' * 2097152); sys.stdout.buffer.flush(); "
            "sys.stdout.buffer.write(sys.stdin.buffer.read()); "
            "sys.stderr.buffer.write(b'\\x00\\xff\\r\\n')")
    result = python_git.run("-c", code, input=data, repository=False)
    assert result.stdout == b"p" * 2097152 + data
    assert result.stderr == b"\x00\xff\r\n"


def test_no_input_is_eof_and_early_exit_keeps_error_semantics(python_git):
    result = python_git.run("-c", "import sys; assert sys.stdin.buffer.read() == b''", repository=False)
    assert result.returncode == 0
    code = "import sys; sys.stderr.buffer.write(b'bad\\xff\\r\\n'); sys.exit(7)"
    result = python_git.run("-c", code, input=b"x" * 2097152, repository=False, check=False)
    assert (result.returncode, result.stderr) == (7, b"bad\xff\r\n")
    with pytest.raises(gitcmd.GitError) as raised:
        python_git.run("-c", code, input=b"x" * 2097152, repository=False)
    assert (raised.value.kind, raised.value.returncode, raised.value.stderr) == ("local", 7, "bad\ufffd\r\n")


@pytest.mark.parametrize("network,kind", [(False, "local"), (True, "timeout")])
def test_timeout_reaps_child(python_git, monkeypatch, network, kind):
    children = []
    original = subprocess.Popen

    class RecordingPopen(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            children.append(self)

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    monkeypatch.setattr(gitcmd, "LOCAL_TIMEOUT", 0.2)
    monkeypatch.setattr(gitcmd, "NETWORK_TIMEOUT", 0.2)
    with pytest.raises(gitcmd.GitError) as raised:
        python_git.run("-c", "import time; time.sleep(60)", input=b"x", network=network, repository=False)
    assert raised.value.kind == kind
    assert raised.value.stderr == "timed out after 0.2 s"
    assert len(children) == 1 and children[0].returncode is not None
    if os.name == "posix":
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)


def _history_backpressure(root):
    """Run only in a supervised interpreter: the old transport blocks inside os.write."""
    from src import library_history
    from src.file_lock import file_lock

    history = library_history.LibraryHistory(root)
    with file_lock(root / ".lock"):
        history.ensure()
        history.apply({"common/test.md": b"# Test\n"}, flow_path="common/test.md")
    library_history.clear_cache()
    children = []
    original_popen = subprocess.Popen
    original_selector = subprocess._PopenSelector

    class BatchPopen(original_popen):
        def __init__(self, args, **kwargs):
            batch = "cat-file" in args and "--batch" in args
            if batch:
                # Never consume stdin; emit more than a pipe holds, then stay alive.
                args = [sys.executable, "-c", "import sys,time; "
                        "sys.stdout.buffer.write(b'x' * 2097152); sys.stdout.buffer.flush(); time.sleep(60)"]
            super().__init__(args, **kwargs)
            if batch:
                children.append(self)

    class StaleWritableSelector(original_selector):
        def select(self, timeout=None):
            batch_fds = {child.stdin.fileno() for child in children
                         if child.stdin is not None and not child.stdin.closed}
            writers = [key for key in self.get_map().values()
                       if key.events & selectors.EVENT_WRITE and key.fd in batch_fds]
            if not writers:
                return super().select(timeout)
            key = writers[0]
            # Deterministically model a stale writable notification for a full,
            # blocking pipe. A deadline cannot interrupt the following os.write.
            os.set_blocking(key.fd, False)
            try:
                while True:
                    os.write(key.fd, b"x" * 65536)
            except BlockingIOError:
                pass
            finally:
                os.set_blocking(key.fd, True)
            (root / "stale-ready").touch()
            return [(key, selectors.EVENT_WRITE)]

    subprocess.Popen = BatchPopen
    subprocess._PopenSelector = StaleWritableSelector
    original_timeout = gitcmd.LOCAL_TIMEOUT
    gitcmd.LOCAL_TIMEOUT = 0.3
    try:
        with pytest.raises(library_history.HistoryError, match="timed out after 0.3 s"):
            with file_lock(root / ".lock"):
                history.events()
        assert children and all(child.returncode is not None for child in children)
        for child in children:
            with pytest.raises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
        with file_lock(root / ".lock", blocking=False):
            pass
        index = library_history._library_index(root)
        assert index.lock.acquire(blocking=False)
        index.lock.release()
    finally:
        subprocess.Popen = original_popen
        subprocess._PopenSelector = original_selector
        gitcmd.LOCAL_TIMEOUT = original_timeout
    # A timeout must not poison the history cache or prevent later reads.
    assert history.events()


@pytest.mark.skipif(os.name != "posix" or not hasattr(subprocess, "_PopenSelector"),
                    reason="CPython POSIX selector fault injection")
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_batch_backpressure_timeout_releases_library_locks(tmp_path):
    code = ("from pathlib import Path; from tests.test_user_sync_gitcmd import _history_backpressure; "
            "import sys; _history_backpressure(Path(sys.argv[1]))")
    worker = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                              cwd=Path(__file__).resolve().parents[1], start_new_session=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        try:
            stdout, stderr = worker.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("Git batch deadline did not complete; stale writable pipe injected="
                        + str((tmp_path / "stale-ready").exists()))
        assert worker.returncode == 0, (stdout + stderr).decode("utf-8", "replace")
    finally:
        # Kill descendants too when a regression wedges the inner communicate.
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        worker.communicate(timeout=5)
