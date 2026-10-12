"""Run supervision: a timed-out run leaves no descendant, caffeinate holds the Mac awake, the lease (#338)."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from src.file_lock import file_lock
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


def test_a_probe_of_the_lease_does_not_make_a_hold_fail(tmp_path):
    """`held` takes the lock for an instant; a run or an update that collides with it waits it out."""
    path = lease.lease_path(tmp_path)
    path.parent.mkdir(parents=True)
    taken, done = threading.Event(), threading.Event()

    def slow_probe():
        with file_lock(path, blocking=False):
            taken.set()
            time.sleep(0.3)
        done.set()
    thread = threading.Thread(target=slow_probe)
    thread.start()
    try:
        assert taken.wait(5)
        with lease.hold(tmp_path):
            assert done.is_set()
    finally:
        thread.join(5)


def test_concurrent_probes_never_fail_a_hold(tmp_path):
    stop = threading.Event()

    def probe():
        while not stop.is_set():
            lease.held(tmp_path)
    with lease.hold(tmp_path):
        pass
    probes = [threading.Thread(target=probe) for _ in range(3)]
    for thread in probes:
        thread.start()
    try:
        for _ in range(50):
            with lease.hold(tmp_path):
                pass
    finally:
        stop.set()
        for thread in probes:
            thread.join(5)


def test_a_lease_kept_by_another_holder_is_busy_after_the_wait(tmp_path):
    with lease.hold(tmp_path):
        started = time.monotonic()
        with pytest.raises(lease.LeaseBusy):
            with lease.hold(tmp_path, wait=0.3):
                pass
        assert 0.25 < time.monotonic() - started < 5


# --- review F7 to F10: every way a run ends sweeps its tree, and no reused PID is signalled -------------

REPO = Path(__file__).resolve().parents[1]


def wait_for(path, count=1, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and len(path.read_text().split()) >= count:
            return [int(pid) for pid in path.read_text().split()]
        time.sleep(0.05)
    raise AssertionError(f"{path} did not list {count} PIDs")


def gone(pids, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and any(alive(pid) for pid in pids):
        time.sleep(0.1)
    return [pid for pid in pids if alive(pid)] == []


def supervised(tmp_path, argv, **kwargs):
    return run_supervised(argv, cwd=tmp_path, env=dict(os.environ), stdout_path=tmp_path / "out",
                          stderr_path=tmp_path / "err", caffeinate=False, **{"timeout": 30, "grace": 1, **kwargs})


def test_a_process_left_behind_by_a_normal_exit_is_swept(tmp_path):
    pids = tmp_path / "pids"
    result = supervised(tmp_path, ["/bin/sh", "-c", f"sleep 61 & echo $! > {pids}; exit 0"])
    leftover = wait_for(pids)
    assert result.returncode == 0 and not result.timed_out
    assert gone(leftover) and result.signalled == leftover


# The root starts a child that calls setsid, starts a grandchild and exits at once: the grandchild
# is in another session and process group, and its parent is launchd.
DOUBLE_FORK = textwrap.dedent("""
    import os, subprocess, sys, time
    pids, root_exits = sys.argv[1], sys.argv[2] == "exit"
    grandchild = "import os, sys, time; open(sys.argv[1], 'a').write(f'{os.getpid()}\\\\n'); time.sleep(600)"
    child = ("import os, subprocess, sys; os.setsid(); "
             f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, sys.argv[1]]); os._exit(0)")
    subprocess.run([sys.executable, "-c", child, pids], check=True)
    while not (os.path.exists(pids) and open(pids).read().strip()):
        time.sleep(0.05)
    if not root_exits:
        time.sleep(600)
""")


@pytest.mark.skipif(sys.platform != "darwin", reason="the marker is read with macOS ps -E")
@pytest.mark.parametrize("ending", ["timeout", "exit"])
def test_a_double_forked_setsid_descendant_is_swept(tmp_path, ending):
    pids, script = tmp_path / "pids", tmp_path / "double_fork.py"
    script.write_text(DOUBLE_FORK)
    result = supervised(tmp_path, [sys.executable, str(script), str(pids), ending],
                        timeout=8 if ending == "timeout" else 30)
    grandchild = wait_for(pids)[0]
    assert result.timed_out is (ending == "timeout")
    assert gone([grandchild]) and grandchild in result.signalled


SUPERVISOR = textwrap.dedent("""
    import os, sys
    from src.schedules.supervise import run_supervised
    child = "import os, sys, time; open(sys.argv[1], 'a').write(f'{os.getpid()}\\\\n'); time.sleep(600)"
    tmp = sys.argv[1]
    run_supervised([sys.executable, "-c", child, os.path.join(tmp, "pids")], timeout=600, cwd=tmp,
                   env=dict(os.environ), stdout_path=os.path.join(tmp, "out"), stderr_path=os.path.join(tmp, "err"),
                   caffeinate=False, grace=1)
""")


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_a_stop_signal_to_the_supervisor_ends_the_run(tmp_path, number):
    supervisor = subprocess.Popen([sys.executable, "-c", SUPERVISOR, str(tmp_path)], cwd=REPO,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        run = wait_for(tmp_path / "pids")[0]
        supervisor.send_signal(number)
        _, errors = supervisor.communicate(timeout=30)
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.communicate()
    assert supervisor.returncode == 128 + number, errors
    assert gone([run])


def test_the_previous_signal_handlers_are_restored(tmp_path):
    before = {number: signal.getsignal(number) for number in supervise.STOP_SIGNALS}
    supervised(tmp_path, [sys.executable, "-c", "pass"])
    assert {number: signal.getsignal(number) for number in supervise.STOP_SIGNALS} == before


def root_state(pid):
    return subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def test_the_root_stays_unreaped_until_the_sweep_ends(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    while not supervise.exited(process.pid):
        time.sleep(0.05)
    states = []

    def table():
        states.append(root_state(process.pid))
        return []
    assert supervise.terminate_tree(process, grace=0.3, table=table) == []
    assert states and all(state.startswith("Z") for state in states), states  # PID and group reserved
    assert process.returncode == 0 and root_state(process.pid) == ""


def test_a_remembered_pid_that_now_names_another_process_is_not_signalled(tmp_path, monkeypatch):
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    while not supervise.exited(process.pid):
        time.sleep(0.05)
    stranger = supervise.Proc(4242, 1, 4242, "Mon Oct 12 20:00:01 2026")  # PID 4242 reused since
    sent = []
    monkeypatch.setattr(supervise.os, "kill", lambda pid, number: sent.append(pid))
    monkeypatch.setattr(supervise.os, "killpg", lambda group, number: sent.append(("group", group)))
    signalled = supervise.terminate_tree(process, grace=0.3, table=lambda: [stranger],
                                         seen={4242: "Mon Oct 12 19:00:00 2026"})
    assert signalled == [] and 4242 not in sent
    same = supervise.Proc(4242, 1, 4242, "Mon Oct 12 19:00:00 2026")  # still the process seen in the run
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    while not supervise.exited(process.pid):
        time.sleep(0.05)
    assert supervise.terminate_tree(process, grace=0.3, table=lambda: [same],
                                    seen={4242: "Mon Oct 12 19:00:00 2026"}) == [4242]


def test_the_marker_finds_a_process_by_its_environment():
    table = [supervise.Proc(30, 1, 30, "t", False, frozenset({"abc"})), supervise.Proc(31, 1, 31, "t"),
             supervise.Proc(32, 1, 10, "t", True)]
    assert descendants(10, table, "abc") == {30}  # 32 is a zombie of the group


@pytest.mark.skipif(sys.platform != "darwin", reason="the marker is read with macOS ps -E")
def test_the_process_table_reads_the_marker_of_a_python_process(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             env={**os.environ, supervise.MARKER: "f00d"})
    try:
        time.sleep(0.3)
        rows = {row.pid: row for row in supervise.process_table()}
        assert rows[child.pid].markers == frozenset({"f00d"}) and rows[child.pid].started
    finally:
        child.kill()
        child.wait()


# --- review round 2: a blind process scan is counted, a stop signal while the run starts sweeps it --------

def test_a_failed_process_scan_is_counted_not_taken_for_an_empty_table(tmp_path):
    assert supervise.process_table(str(tmp_path / "no-ps")) is None
    result = supervised(tmp_path, [sys.executable, "-c", "pass"], table=lambda: None)
    assert result.returncode == 0 and result.failed_scans >= 2  # the sweep's first and final scans
    assert supervised(tmp_path, [sys.executable, "-c", "pass"]).failed_scans == 0


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(),
                    reason="the supervisor installs its stop handlers only in the main thread")
def test_a_stop_signal_while_the_run_starts_ends_the_run(tmp_path, monkeypatch):
    real, started = subprocess.Popen, []

    def popen_then_stop(*args, **kwargs):  # the signal comes after the fork, before Popen returns
        process = real(*args, **kwargs)
        if not started:
            started.append(process.pid)
            os.kill(os.getpid(), signal.SIGTERM)
        return process
    monkeypatch.setattr(supervise.subprocess, "Popen", popen_then_stop)
    try:
        with pytest.raises(supervise.SupervisorStopped) as stopped:
            supervised(tmp_path, [sys.executable, "-c", "import time; time.sleep(600)"])
        assert stopped.value.signal == signal.SIGTERM
        assert started and gone(started)
    finally:
        for pid in started:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
