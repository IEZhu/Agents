"""Run supervision: a timed-out run leaves no descendant, caffeinate holds the Mac awake, the lease (#338)."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from src.schedules import lease, supervise
from src.schedules.supervise import caffeinate_argv, descendants, run_supervised

pytestmark = pytest.mark.skipif(os.name != "posix", reason="run supervision uses POSIX process groups")

# A run whose tree outlives a plain kill of its leader: a child that ignores SIGTERM, a grandchild,
# and a child that leaves the process group with setsid. Each writes its PID to the pids file.
TREE = textwrap.dedent("""
    import os, signal, subprocess, sys, time
    pids = sys.argv[1]
    def spawn(code):
        return subprocess.Popen([sys.executable, "-c", code, pids])
    record = "import os, sys; open(sys.argv[1], 'a').write(f'{os.getpid()}\\\\n'); "
    spawn(record + "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)")
    spawn(record + "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', "
          "\\"import os, sys, time; open(sys.argv[1], 'a').write(f'{os.getpid()}\\\\\\\\n'); time.sleep(600)\\", sys.argv[1]]); time.sleep(600)")
    spawn(record + "import os, time; os.setsid(); time.sleep(600)")
    open(pids, "a").write(f"{os.getpid()}\\n")
    time.sleep(600)
""")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers until its new parent reaps it.
    state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_timed_out_run_leaves_no_descendant(tmp_path):
    pids = tmp_path / "pids"
    script = tmp_path / "tree.py"
    script.write_text(TREE)
    started = time.monotonic()

    def wait_for_tree():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if pids.exists() and len(pids.read_text().split()) >= 5:
                return
            time.sleep(0.1)
    # The timeout must allow the tree to start: 5 Python processes.
    result = run_supervised([sys.executable, str(script), str(pids)], timeout=6, cwd=tmp_path,
                            env=dict(os.environ), stdout_path=tmp_path / "out", stderr_path=tmp_path / "err",
                            caffeinate=False, grace=1)
    wait_for_tree()
    spawned = [int(pid) for pid in pids.read_text().split()]
    assert len(spawned) == 5, pids.read_text()
    assert result.timed_out and result.returncode is not None
    assert set(spawned) <= set(result.signalled)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(alive(pid) for pid in spawned):
        time.sleep(0.1)
    assert [pid for pid in spawned if alive(pid)] == []
    assert time.monotonic() - started < 30


def test_finished_run_reports_its_exit_code(tmp_path):
    result = run_supervised([sys.executable, "-c", "raise SystemExit(3)"], timeout=30, cwd=tmp_path,
                            env=dict(os.environ), stdout_path=tmp_path / "out", stderr_path=tmp_path / "err",
                            caffeinate=False)
    assert result.returncode == 3 and not result.timed_out and result.signalled == []


def test_run_starts_in_its_own_process_group(tmp_path):
    out = tmp_path / "out"
    run_supervised([sys.executable, "-c", "import os; print(os.getpgid(0) == os.getpid())"], timeout=30,
                   cwd=tmp_path, env=dict(os.environ), stdout_path=out, stderr_path=tmp_path / "err", caffeinate=False)
    assert out.read_text().strip() == "True"


def test_caffeinate_keeps_the_mac_awake_for_the_run(tmp_path, monkeypatch):
    started = []
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        if argv[0] == supervise.CAFFEINATE:
            started.append(argv)
            return real_popen([sys.executable, "-c", "pass"])
        return real_popen(argv, **kwargs)
    monkeypatch.setattr(supervise.subprocess, "Popen", popen)
    monkeypatch.setattr(supervise.sys, "platform", "darwin")
    monkeypatch.setattr(supervise.os.path, "exists", lambda path: path == supervise.CAFFEINATE or os.access(path, os.F_OK))
    result = run_supervised([sys.executable, "-c", "pass"], timeout=30, cwd=tmp_path, env=dict(os.environ),
                            stdout_path=tmp_path / "out", stderr_path=tmp_path / "err")
    assert result.caffeinated and len(started) == 1
    assert started[0][:3] == ["/usr/bin/caffeinate", "-i", "-w"] and started[0][3].isdigit()


def test_caffeinate_command():
    assert caffeinate_argv(4242) == ["/usr/bin/caffeinate", "-i", "-w", "4242"]


def test_descendants_follow_parents_and_the_group():
    table = [(10, 1, 10), (11, 10, 10), (12, 11, 10), (13, 10, 13), (14, 13, 13), (20, 1, 20), (21, 1, 10)]
    assert descendants(10, table) == {11, 12, 13, 14, 21}


def test_lease_excludes_a_second_run_and_ends_with_its_holder(tmp_path):
    assert not lease.held(tmp_path)
    assert not (tmp_path / "schedules").exists()  # checking creates nothing
    with lease.hold(tmp_path) as path:
        assert path == tmp_path / "schedules" / "run.lease"
        assert lease.held(tmp_path)
        with pytest.raises(lease.LeaseBusy):
            with lease.hold(tmp_path):
                pass
    assert not lease.held(tmp_path)


def test_lease_held_by_a_killed_process_is_released(tmp_path):
    code = ("import sys, time; from src.schedules import lease\n"
            "with lease.hold(sys.argv[1]):\n    print('held', flush=True); time.sleep(600)\n")
    holder = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True,
                              cwd=Path(__file__).resolve().parents[1])
    try:
        assert holder.stdout.readline().strip() == "held"
        assert lease.held(tmp_path)
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        holder.stdout.close()
    assert not lease.held(tmp_path)
