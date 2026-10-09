"""Process-level regression tests for the installation lifetime lease."""

import os
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src import startup, self_update

ROOT = Path(__file__).resolve().parents[1]


posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX children inherit flock leases")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows job objects")


@pytest.fixture
def leased_install(tmp_path):
    if not startup.LEASES:
        pytest.skip("no installation leases on this platform")
    source = tmp_path / "src"
    source.mkdir()
    (source / "__init__.py").touch()
    shutil.copy(ROOT / "src/startup.py", source / "startup.py")
    # Execute the actual production entry guard, with a tiny server body that
    # avoids loading embeddings. No .env/network work in the activation stub.
    server_source = (ROOT / "src/server.py").read_text(encoding="utf-8")
    assert "import atexit" in server_source, "server.py no longer contains the stub split marker"
    prefix = server_source.split("import atexit", 1)[0]
    server = source / "server.py"
    server.write_text(prefix + 'print("OLD_SERVER", flush=True)\n')
    (source / "self_update.py").write_text('def run_activation_safely(session_fd): pass\n')
    (tmp_path / "data").mkdir()
    return tmp_path, server, prefix


def _child(code, *args, cwd=None):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)], cwd=cwd,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)


def _stop(process):
    if process.poll() is None:
        process.terminate()
    process.communicate(timeout=5)


def test_stdio_indexes_survive_restart_and_concurrent_servers_are_isolated(leased_install):
    root, server, prefix = leased_install
    server.write_text(prefix + '''
import json, os
from pathlib import Path
derived = Path(os.environ["AGENTS_DERIVED_DIR"])
router = Path(os.environ["AGENTS_ROUTER_DATA_DIR"])
router.mkdir(parents=True, exist_ok=True)
marker = router / "cached-entry"
print(json.dumps({"derived": str(derived), "router": str(router), "cached": marker.exists()}), flush=True)
marker.write_text("cached routing decision")
input()
''')
    code = 'import runpy, sys; runpy.run_path(sys.argv[1], run_name="__main__")'
    first = _child(code, server, cwd=root)
    second = None
    restarted = None
    try:
        first_state = json.loads(first.stdout.readline())
        second = _child(code, server, cwd=root)
        second_state = json.loads(second.stdout.readline())
        assert first_state["derived"] != second_state["derived"]
        assert first_state["router"] != second_state["router"]
        assert not first_state["cached"] and not second_state["cached"]
        first.communicate(input="exit\n", timeout=5)
        assert first.returncode == 0
        restarted = _child(code, server, cwd=root)
        restarted_state = json.loads(restarted.stdout.readline())
        assert restarted_state["derived"] == first_state["derived"]
        assert restarted_state["router"] == first_state["router"]
        assert restarted_state["cached"]
    finally:
        for process in (first, second, restarted):
            if process is not None:
                _stop(process)


@pytest.mark.parametrize("launch", ["script", "module"])
def test_contending_start_rereads_server_after_activation(leased_install, launch):
    root, server, prefix = leased_install
    bootstrap = root / "src/startup.py"
    bootstrap_source = bootstrap.read_text(encoding="utf-8")
    bootstrap.write_text(bootstrap_source + '\nOBSOLETE_API = True\n', encoding="utf-8")
    # Another process owns the writer lease while changing the server file.
    with open(root / "data/.sessions.lock", "a+b") as lease:
        assert startup._lock(lease.fileno(), exclusive=True, blocking=True)
        code = '''
import runpy, sys
print("STARTING", flush=True)
if sys.argv[1] == "module":
    runpy.run_module("src.server", run_name="__main__", alter_sys=True)
else:
    runpy.run_path(sys.argv[2], run_name="__main__")
'''
        process = _child(code, launch, server, cwd=root if launch == "module" else root.parent)
        try:
            assert process.stdout.readline().strip() == "STARTING"
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.3)
            # A new server can depend on APIs added to startup.py by the writer.
            bootstrap.write_text(bootstrap_source + '\nNEW_API = "NEW_SERVER"\n', encoding="utf-8")
            server.write_text(prefix + 'from src import startup\nassert not hasattr(startup, "OBSOLETE_API")\nprint(startup.NEW_API, flush=True)\n')
            startup._unlock(lease.fileno())
            output, errors = process.communicate(timeout=5)
            assert process.returncode == 0, errors
            assert output.strip() == "NEW_SERVER"
        finally:
            _stop(process)


def test_running_reader_defers_activation_until_last_session_exits(leased_install):
    root, _, _ = leased_install
    prompt = root / "prompt.mdc"
    prompt.write_text("old prompt")
    code = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src.startup import server_session
root = Path(sys.argv[2])
with server_session(root, lambda fd: None):
    print((root / "prompt.mdc").read_text(), flush=True)
    sys.stdin.readline()
    print((root / "prompt.mdc").read_text(), flush=True)
'''
    process = _child(code, ROOT, root, cwd=root.parent)
    try:
        assert process.stdout.readline().strip() == "old prompt"
        with startup.server_session(root, lambda fd: prompt.write_text("new prompt")):
            assert prompt.read_text() == "old prompt"
        output, errors = process.communicate(input="next request\n", timeout=5)
        assert process.returncode == 0, errors
        assert output.strip() == "old prompt"
        with startup.server_session(root, lambda fd: prompt.write_text("new prompt")):
            assert prompt.read_text() == "new prompt"
    finally:
        _stop(process)


def test_background_prepare_lock_does_not_block_startup(leased_install, monkeypatch):
    root, _, _ = leased_install
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(root / "data/marker.json"))
    monkeypatch.setattr(self_update, "STAGING_ROOT", str(root / "data/.prepared"))
    monkeypatch.setattr(self_update, "LOCK_FILE", str(root / "data/.update.lock"))
    Path(self_update.STAGING_ROOT).mkdir()  # in-flight prepare, no published marker
    monkeypatch.setattr(self_update, "activate_prepared_update", lambda: pytest.fail("prepare owns lock"))
    with self_update._process_lock(self_update.LOCK_FILE) as acquired:
        assert acquired
        with startup.server_session(root, self_update.run_activation_safely):
            pass  # no wait for network/embedding, no activation


def test_unsupported_locking_disables_updates_but_serves(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "LEASES", False)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    with startup.server_session(tmp_path, lambda fd: pytest.fail("unsafe activation")):
        assert self_update.start_background_update() is None


@pytest.mark.parametrize("journal_content", ["", "{broken", '{"old_sha": "abc"}'])
def test_unfinished_update_blocks_before_application_imports(leased_install, journal_content):
    root, server, _ = leased_install
    (root / "data" / startup.UPDATE_JOURNAL).write_text(journal_content)
    process = _child('import runpy, sys; runpy.run_path(sys.argv[1], run_name="__main__")', server, cwd=root.parent)
    try:
        output, errors = process.communicate(timeout=5)
        assert process.returncode != 0
        assert "Unfinished auto-update" in errors
        assert "OLD_SERVER" not in output
    finally:
        _stop(process)


@posix_only
@pytest.mark.parametrize("staged", [True, False])
def test_reindex_child_retains_leases_after_parent_exits(tmp_path, staged):
    root = tmp_path / "install"
    source = root / "src"
    source.mkdir(parents=True)
    (source / "__init__.py").touch()
    (source / "reindex.py").write_text('''
import os, time
from pathlib import Path
Path("worker.pid").write_text(str(os.getpid()))
deadline = time.monotonic() + 15
while not Path("finish").exists() and time.monotonic() < deadline:
    time.sleep(0.02)
Path("finished").touch()
''', encoding="utf-8")
    code = '''
import sys
sys.path.insert(0, sys.argv[1])
from src import self_update as su
from src.startup import server_session
root, staged = sys.argv[2], sys.argv[3] == "True"
def run_worker(session_fd):
    # Legacy writes the live install and retains the exclusive session lease.
    # Background staged preparation only needs the updater lease.
    with su._inherit_lock(None if staged else session_fd):
        with su._process_lock(root + "/data/.update.lock") as acquired:
            assert acquired
            worker = su._run_reindex_at if staged else su._run_reindex
            worker(root, 15)
with server_session(root, run_worker):
    pass
'''
    parent = _child(code, ROOT, root, staged, cwd=tmp_path)
    worker_pid = None

    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        pytest.fail("worker/lease transition timed out")

    try:
        pid_file = root / "worker.pid"
        wait_for(lambda: pid_file.exists() and bool(pid_file.read_text()))
        worker_pid = int(pid_file.read_text())
        parent.kill()  # abrupt stdio server exit while its daemon owns a child
        parent.communicate(timeout=5)
        with self_update._process_lock(str(root / "data/.update.lock")) as acquired:
            assert not acquired  # a new startup cannot prune the active worktree
        if not staged:
            with self_update._process_lock(str(root / "data/.sessions.lock")) as acquired:
                assert not acquired  # legacy worker still writes the live stores
        (root / "finish").touch()
        wait_for(lambda: (root / "finished").exists())

        def released():
            with self_update._process_lock(str(root / "data/.update.lock")) as acquired:
                return acquired

        wait_for(released)
        if not staged:
            with self_update._process_lock(str(root / "data/.sessions.lock")) as acquired:
                assert acquired
        worker_pid = None
    finally:
        _stop(parent)
        if worker_pid is not None:
            try:
                os.kill(worker_pid, 15)
            except ProcessLookupError:
                pass


@posix_only
@pytest.mark.parametrize("staged", [True, False])
@pytest.mark.parametrize("outcome", ["success", "timeout", "detached_timeout"])
def test_reindex_descendant_finishes_before_leases_are_released(tmp_path, staged, outcome):
    root = tmp_path / "install"
    source = root / "src"
    source.mkdir(parents=True)
    (source / "__init__.py").touch()
    descendant = '''
import os, time
from pathlib import Path
Path("descendant.pid").write_text(str(os.getpid()))
deadline = time.monotonic() + 15
while not Path("finish").exists() and time.monotonic() < deadline:
    time.sleep(0.02)
Path("late_write").touch()
'''
    (source / "reindex.py").write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}], close_fds=False, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        f"start_new_session={outcome == 'detached_timeout'!r})\n"
        + ("time.sleep(15)\n" if outcome != "success" else ""),
        encoding="utf-8",
    )
    code = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src import self_update as su
from src.startup import server_session
root, staged = Path(sys.argv[2]), sys.argv[3] == "True"
def run_worker(session_fd):
    with su._inherit_lock(None if staged else session_fd):
        with su._process_lock(str(root / "data/.update.lock")) as acquired:
            assert acquired
            worker = su._run_reindex_at if staged else su._run_reindex
            result = worker(str(root), 0.5)
            (root / "returned").write_text(str(result))
with server_session(root, run_worker):
    print("READY", flush=True)
'''
    parent = _child(code, ROOT, root, staged, cwd=tmp_path)
    descendant_pid = None

    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        pytest.fail("descendant/runner transition timed out")

    try:
        pid_file = root / "descendant.pid"
        wait_for(lambda: pid_file.exists() and bool(pid_file.read_text()))
        descendant_pid = int(pid_file.read_text())
        if outcome == "timeout":
            # Killing the process group must stop the grandchild before return.
            wait_for(lambda: (root / "returned").exists())
            (root / "finish").touch()
            time.sleep(0.2)
            assert not (root / "late_write").exists()
        else:
            # A successful leader, or an escaped timeout descendant, may leave
            # inherited leases alive: do not roll back or unlock underneath it.
            with pytest.raises(subprocess.TimeoutExpired):
                parent.communicate(timeout=0.8)
            assert not (root / "returned").exists()
            with self_update._process_lock(str(root / "data/.update.lock")) as acquired:
                assert not acquired
            if not staged:
                with self_update._process_lock(str(root / "data/.sessions.lock")) as acquired:
                    assert not acquired
            (root / "finish").touch()
        output, errors = parent.communicate(timeout=5)
        assert parent.returncode == 0, errors
        assert output.strip() == "READY"
        assert (root / "returned").read_text() == str(outcome == "success")
        if outcome != "timeout":
            assert (root / "late_write").exists()
            assert errors.count("waiting for descendants of PID") == 1
        with self_update._process_lock(str(root / "data/.update.lock")) as acquired:
            assert acquired
    finally:
        (root / "finish").touch()
        _stop(parent)
        if descendant_pid is not None:
            try:
                os.kill(descendant_pid, 15)
            except ProcessLookupError:
                pass


@posix_only
def test_process_lock_close_does_not_unlock_inherited_descriptor(tmp_path):
    lock = str(tmp_path / "update.lock")
    process = None
    try:
        with self_update._process_lock(lock) as acquired:
            assert acquired
            process = subprocess.Popen(
                [sys.executable, "-c", "import sys; print('READY', flush=True); sys.stdin.read()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, pass_fds=self_update._subprocess_lock_fds(),
            )
            assert process.stdout.readline().strip() == "READY"
        with self_update._process_lock(lock) as acquired:
            assert not acquired
        process.communicate(timeout=5)
        with self_update._process_lock(lock) as acquired:
            assert acquired
    finally:
        if process is not None:
            _stop(process)


def _ended(pid, timeout=5):
    """Windows: whether process ``pid`` ended within ``timeout`` seconds (``os.kill`` would end it)."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True  # no such process
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout * 1000)) == 0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _wait_for(predicate, what):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail(what + " timed out")


def _free(path):
    with self_update._process_lock(str(path)) as acquired:
        return acquired


@windows_only
@pytest.mark.parametrize("staged", [True, False])
def test_windows_reindex_child_ends_with_the_lease_holder(tmp_path, staged):
    """A LockFileEx lease ends with its process: the updater's child must not write without it."""
    root = tmp_path / "install"
    source = root / "src"
    source.mkdir(parents=True)
    (source / "__init__.py").touch()
    (source / "reindex.py").write_text('''
import os, time
from pathlib import Path
Path("worker.pid").write_text(str(os.getpid()))
deadline = time.monotonic() + 15
while not Path("finish").exists() and time.monotonic() < deadline:
    time.sleep(0.02)
Path("finished").touch()
''', encoding="utf-8")
    code = '''
import sys
sys.path.insert(0, sys.argv[1])
from src import self_update as su
from src.startup import server_session
root, staged = sys.argv[2], sys.argv[3] == "True"
def run_worker(session_fd):
    with su._inherit_lock(None if staged else session_fd):
        with su._process_lock(root + "/data/.update.lock") as acquired:
            assert acquired
            worker = su._run_reindex_at if staged else su._run_reindex
            worker(root, 15)
with server_session(root, run_worker):
    pass
'''
    parent = _child(code, ROOT, root, staged, cwd=tmp_path)
    try:
        pid_file = root / "worker.pid"
        _wait_for(lambda: pid_file.exists() and bool(pid_file.read_text()), "worker start")
        worker_pid = int(pid_file.read_text())
        assert not _free(root / "data/.update.lock")
        parent.kill()  # abrupt stdio server exit while its updater thread waits on a child
        parent.communicate(timeout=5)
        assert _ended(worker_pid)
        (root / "finish").touch()
        time.sleep(0.2)
        assert not (root / "finished").exists()
        assert _free(root / "data/.update.lock") and _free(root / "data/.sessions.lock")
    finally:
        _stop(parent)


@windows_only
@pytest.mark.parametrize("outcome", ["success", "timeout", "detached"])
def test_windows_descendants_end_before_the_command_returns(tmp_path, outcome, caplog):
    """A grandchild of the command, even a detached one, never writes after ``_run_command`` returns."""
    descendant = '''
import os, time
from pathlib import Path
Path("descendant.pid").write_text(str(os.getpid()))
deadline = time.monotonic() + 15
while not Path("finish").exists() and time.monotonic() < deadline:
    time.sleep(0.02)
Path("late_write").touch()
'''
    flags = "subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP" if outcome == "detached" else "0"
    leader = (f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {descendant!r}], "
              f"creationflags={flags}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
              "stderr=subprocess.DEVNULL)\n"
              "from pathlib import Path\n"
              "while not Path('descendant.pid').exists(): time.sleep(0.02)\n"
              + ("time.sleep(15)\n" if outcome == "timeout" else ""))
    started = time.monotonic()
    if outcome == "timeout":
        with pytest.raises(subprocess.TimeoutExpired):
            self_update._run_command([sys.executable, "-c", leader], cwd=str(tmp_path), timeout=2)
    else:
        result = self_update._run_command([sys.executable, "-c", leader], cwd=str(tmp_path), timeout=2)
        assert result.returncode == 0
        assert "outlived it" in caplog.text
    assert time.monotonic() - started < 10
    # Ended or ending: TerminateJobObject stops every thread before the process object is signaled.
    assert _ended(int((tmp_path / "descendant.pid").read_text()))
    (tmp_path / "finish").touch()
    time.sleep(0.2)
    assert not (tmp_path / "late_write").exists()


_RESPAWNING_SERVER = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src import self_update
from src.startup import server_session
root = Path(sys.argv[2])
first = not (root / "parent.pid").exists()
activated = []
def activate(fd):
    activated.append(fd)
    if first:
        (root / "parent.pid").write_text(str(os.getpid()))
        self_update._reexec_updated_server()
        raise AssertionError("re-exec returned")
with server_session(root, activate):
    print("SERVING", os.getpid(), "activated" if activated else "shared", flush=True)
    line = sys.stdin.readline()
    if line.strip() == "hang":
        sys.stdin.readline()
    print("ECHO " + line.strip(), flush=True)
sys.exit(7)
'''


def _respawning_server(tmp_path):
    root = tmp_path / "install"
    (root / "data").mkdir(parents=True)
    script = tmp_path / "server.py"
    script.write_text(_RESPAWNING_SERVER, encoding="utf-8")
    return root, subprocess.Popen([sys.executable, str(script), str(ROOT), str(root)], cwd=tmp_path,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@windows_only
def test_windows_reexec_serves_the_updated_code_on_the_same_stdio(tmp_path):
    """Windows has no exec: the child takes over the client's pipes, the leases and the exit code."""
    root, parent = _respawning_server(tmp_path)
    try:
        serving = parent.stdout.readline().split()
        assert serving[0] == "SERVING", parent.stderr.read() if parent.poll() is not None else serving
        # The child got the session lease exclusively: the parent released it before starting it.
        assert serving[2] == "activated"
        # A venv's python.exe starts the interpreter as its child: compare the interpreters' PIDs.
        assert int(serving[1]) != int((root / "parent.pid").read_text())
        output, errors = parent.communicate(input="request\n", timeout=10)
        assert output.strip() == "ECHO request", errors
        assert parent.returncode == 7
        assert _free(root / "data/.sessions.lock")
    finally:
        _stop(parent)


@windows_only
def test_windows_reexec_child_ends_when_the_client_ends_its_server(tmp_path):
    root, parent = _respawning_server(tmp_path)
    try:
        serving = parent.stdout.readline().split()
        assert serving[0] == "SERVING"
        parent.stdin.write("hang\n")
        parent.stdin.flush()
        assert not _free(root / "data/.sessions.lock")
        parent.kill()  # what an MCP client does to the process it started
        parent.communicate(timeout=5)
        assert _ended(int(serving[1]))
        assert _free(root / "data/.sessions.lock")
    finally:
        _stop(parent)


@windows_only
def test_windows_session_lease_downgrades_without_a_gap(tmp_path):
    """The activating start keeps the lease from writers between activation and serving."""
    data = tmp_path / "data"
    data.mkdir()
    probe = ('import sys; sys.path.insert(0, sys.argv[1]); from src import startup\n'
             'lease = open(sys.argv[2], "a+b")\n'
             'try:\n    startup._lock(lease.fileno(), exclusive=sys.argv[3] == "x", blocking=False); print("got")\n'
             'except BlockingIOError:\n    print("busy")\n')

    def other(kind):
        result = subprocess.run([sys.executable, "-c", probe, str(ROOT), str(data / ".sessions.lock"), kind],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    seen = {}
    with startup.server_session(tmp_path, lambda fd: seen.update(during=(other("s"), other("x")))):
        seen["serving"] = (other("s"), other("x"))
    assert seen == {"during": ("busy", "busy"), "serving": ("got", "busy")}
    assert other("x") == "got"
