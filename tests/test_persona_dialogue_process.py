"""Evaluation process cleanup and unsupported-platform preflight."""

import json
import os
import signal
import sys

import pytest

from evals.runners import run_persona_dialogues as runner


@pytest.fixture
def windows_without_launch(monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "win32")

    def unexpected_launch(*args, **kwargs):
        pytest.fail("Windows must be rejected before interpreter, client, or worker startup")

    monkeypatch.setattr(runner, "project_interpreter", unexpected_launch)
    monkeypatch.setattr(runner.subprocess, "Popen", unexpected_launch)
    monkeypatch.setattr(runner.subprocess, "run", unexpected_launch)
    monkeypatch.setattr(runner, "ThreadPoolExecutor", unexpected_launch)


def test_windows_main_rejects_before_setup(windows_without_launch, monkeypatch, tmp_path, capsys):
    output = tmp_path / "output"
    monkeypatch.setattr(runner.sys, "argv", [
        "run_persona_dialogues", "--client", "claude", "--out", str(output),
        "--seed-data", str(tmp_path / "seed"),
    ])
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2
    assert "do not support Windows process-tree cleanup" in capsys.readouterr().err
    assert not output.exists()


def test_windows_direct_case_rejects_before_workspace_creation(windows_without_launch, tmp_path):
    workspace = tmp_path / "case"
    with pytest.raises(NotImplementedError, match="Windows process-tree cleanup"):
        runner.run_case("claude", {"turns": []}, workspace, "protocol", 1)
    assert not workspace.exists()


def test_windows_direct_process_rejects_before_launch(windows_without_launch, tmp_path):
    with pytest.raises(NotImplementedError, match="Windows process-tree cleanup"):
        runner.run_process(["claude"], "request", tmp_path, 1)


@pytest.mark.skipif(os.name != "posix", reason="Exercises real POSIX process groups")
def test_timeout_terminates_parent_and_inheriting_child(tmp_path):
    import fcntl

    child_ids = tmp_path / "process_ids.json"
    script = """
import json, os, pathlib, subprocess, sys, time
child_code = '''
import fcntl, pathlib, time
with open('child.lock', 'w') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    pathlib.Path('child.ready').touch()
    time.sleep(60)
'''
child = subprocess.Popen([sys.executable, '-c', child_code])
while not pathlib.Path('child.ready').exists():
    time.sleep(0.01)
ids = {'parent': os.getpid(), 'child': child.pid,
       'group': os.getpgrp(), 'child_group': os.getpgid(child.pid)}
pathlib.Path('process_ids.json').write_text(json.dumps(ids), encoding='utf-8')
print(json.dumps(ids), flush=True)
time.sleep(60)
"""
    try:
        code, stdout, stderr, timed_out = runner.run_process(
            [sys.executable, "-c", script], "", tmp_path, 2,
        )
        ids = json.loads(stdout)
        assert timed_out
        assert code == -signal.SIGKILL
        assert stderr == ""
        assert ids["group"] == ids["parent"] == ids["child_group"]
        # A live child holds this lock. Checking its release also works when
        # the killed orphan remains a zombie until init reaps it.
        with (tmp_path / "child.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        if child_ids.exists():
            ids = json.loads(child_ids.read_text(encoding="utf-8"))
            try:
                os.killpg(ids["group"], signal.SIGKILL)
            except ProcessLookupError:
                pass
