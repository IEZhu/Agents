"""Process-level regression tests for the installation lifetime lease."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src import startup, self_update

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def leased_install(tmp_path):
    pytest.importorskip("fcntl")
    source = tmp_path / "src"
    source.mkdir()
    (source / "__init__.py").touch()
    shutil.copy(ROOT / "src/startup.py", source / "startup.py")
    # Execute the actual production entry guard, with a tiny server body that
    # avoids loading embeddings. No .env/network work in the activation stub.
    prefix = (ROOT / "src/server.py").read_text(encoding="utf-8").split("import atexit", 1)[0]
    server = source / "server.py"
    server.write_text(prefix + 'print("OLD_SERVER", flush=True)\n')
    (source / "self_update.py").write_text('def run_activation_safely(): pass\n')
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


@pytest.mark.parametrize("launch", ["script", "module"])
def test_contending_start_rereads_server_after_activation(leased_install, launch):
    root, server, prefix = leased_install
    # Another process owns the writer lease while changing the server file.
    with open(root / "data/.sessions.lock", "a+b") as lease:
        startup.fcntl.flock(lease, startup.fcntl.LOCK_EX)
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
            server.write_text(prefix + 'print("NEW_SERVER", flush=True)\n')
            startup.fcntl.flock(lease, startup.fcntl.LOCK_UN)
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
with server_session(root, lambda: None):
    print((root / "prompt.mdc").read_text(), flush=True)
    sys.stdin.readline()
    print((root / "prompt.mdc").read_text(), flush=True)
'''
    process = _child(code, ROOT, root, cwd=root.parent)
    try:
        assert process.stdout.readline().strip() == "old prompt"
        with startup.server_session(root, lambda: prompt.write_text("new prompt")):
            assert prompt.read_text() == "old prompt"
        output, errors = process.communicate(input="next request\n", timeout=5)
        assert process.returncode == 0, errors
        assert output.strip() == "old prompt"
        with startup.server_session(root, lambda: prompt.write_text("new prompt")):
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
    monkeypatch.setattr(startup, "fcntl", None)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    with startup.server_session(tmp_path, lambda: pytest.fail("unsafe activation")):
        assert self_update.start_background_update() is None
