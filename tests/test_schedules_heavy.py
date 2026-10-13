"""The heavy-process lease behind the runner socket and the ``heavy`` client, on real sockets (#340)."""
import functools
import os
import signal
import subprocess
import sys
import threading

import pytest

from src.schedules import heavy, lease, socket_service
from src.schedules.socket_service import Registry, serve
from tests.test_schedules_socket import Client, run, run_dir, short, wait_for  # noqa: F401 (fixtures)

PYTHON = sys.executable


def no_heavy_process(**probe):
    return []


@pytest.fixture
def endpoint(run_dir, service_state_dir):
    """The socket with only the heavy lease; no foreign heavy process unless a test brings its own probe."""
    registry = Registry()
    heavy.register(registry, service_state_dir, processes=no_heavy_process, wait=0.1)
    with serve(run_dir, registry) as found:
        yield found


def test_held_creates_nothing_when_no_one_took_the_lease(service_state_dir):
    assert heavy.held(service_state_dir) is False
    assert not heavy.lease_path(service_state_dir).exists()
    assert heavy.lease_path(service_state_dir) == service_state_dir / "schedules" / "heavy.lease"
    assert heavy.lease_path(service_state_dir).parent == lease.lease_path(service_state_dir).parent


def test_a_second_lease_is_refused_while_one_is_held(endpoint, service_state_dir):
    with Client(endpoint.socket) as first, Client(endpoint.socket) as second:
        assert first.ask("heavy.acquire") == {"ok": True, "result": {"lease": "granted"}}
        assert heavy.held(service_state_dir)
        assert first.ask("heavy.acquire")["error"] == "this connection already holds the heavy-process lease"
        refused = second.ask("heavy.acquire")
        assert refused == {"ok": False, "error": "the heavy-process lease is held by another check"}
        assert first.ask("heavy.release") == {"ok": True, "result": {"released": True}}
        assert not heavy.held(service_state_dir)
        assert first.ask("heavy.release") == {"ok": True, "result": {"released": False}}
        assert second.ask("heavy.acquire")["ok"] is True


def test_heavy_requests_take_no_arguments(endpoint, service_state_dir):
    with Client(endpoint.socket) as client:
        response = client.ask("heavy.acquire", {"command": "pytest -q"})
        assert response == {"ok": False, "error": "unknown argument: 'command'"}
        assert not heavy.held(service_state_dir)


def test_a_lease_is_refused_while_a_foreign_pytest_runs(run_dir, service_state_dir):
    """The real probe: this test process (a pytest) is the requester and is never counted itself."""
    registry = Registry()
    heavy.register(registry, service_state_dir, wait=0.1)
    helper = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(60)", "pytest-helper"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with serve(run_dir, registry) as endpoint, Client(endpoint.socket) as client:
            response = client.ask("heavy.acquire")
            assert response["ok"] is False and response["error"].startswith("heavy processes are running: ")
            listed = response["error"].removeprefix("heavy processes are running: ")
            assert f"{helper.pid} " in listed  # command lines are cut at 80 characters
            assert not listed.startswith(f"{os.getpid()} ") and f", {os.getpid()} " not in listed
            assert not heavy.held(service_state_dir)
    finally:
        helper.kill()
        helper.wait()


def test_the_real_client_and_probe_grant_the_lease_unless_a_foreign_pytest_runs(run_dir, service_state_dir):
    """End to end: the kernel's peer PID reaches the default probe, so the client's own command
    line, which names pytest, never counts; a separate pytest does."""
    if heavy.heavy_processes(requester=os.getpid()):
        pytest.skip("another heavy process runs on this machine")
    registry = Registry()
    heavy.register(registry, service_state_dir, wait=0.1)
    with serve(run_dir, registry) as endpoint:
        command = [endpoint.clients["heavy"], "--", PYTHON, "-c", "pass", "pytest-marker"]
        done = run(command)
        assert done.returncode == 0, done.stderr
        helper = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(60)", "pytest-helper"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            done = run(command)
            assert done.returncode == 75 and "heavy processes are running: " in done.stderr
            assert f"{helper.pid} " in done.stderr
        finally:
            helper.kill()
            helper.wait()
        assert not heavy.held(service_state_dir)


def test_the_lease_is_refused_when_the_process_table_cannot_be_read(run_dir, service_state_dir, tmp_path):
    registry = Registry()
    heavy.register(registry, service_state_dir, wait=0.1,
                   processes=functools.partial(heavy.foreign_heavy, str(tmp_path / "no-ps")))
    with serve(run_dir, registry) as endpoint, Client(endpoint.socket) as client:
        assert client.ask("heavy.acquire") == {
            "ok": False, "error": "the process table cannot be read, so the lease is not granted"}
        assert not heavy.held(service_state_dir)


def test_the_probe_gets_the_peer_pid_the_session_root_and_a_bounded_timeout(run_dir, service_state_dir):
    probes = []

    def probe(**given):
        probes.append(given)
        return []
    registry = Registry()
    heavy.register(registry, service_state_dir, processes=probe, wait=0.1)
    with serve(run_dir, registry, session_root=lambda: 4242) as endpoint, Client(endpoint.socket) as client:
        assert client.ask("heavy.acquire")["ok"] is True
    assert probes[0]["requester"] == os.getpid() and probes[0]["session_root"] == 4242
    assert 0 < probes[0]["timeout"] <= heavy.ACQUIRE_DEADLINE


def test_no_lease_is_held_after_serve_returns_while_an_acquire_was_in_flight(run_dir, service_state_dir):
    """The probe of an in-flight ``heavy.acquire`` outlives the server's close: the lock it took is
    released by that close, not afterwards, and the handler cannot keep it later."""
    entered, go, outcomes = threading.Event(), threading.Event(), []

    def slow_probe(**given):
        entered.set()
        go.wait(10)  # until serve has returned
        return []
    service = heavy.HeavyLease(service_state_dir, processes=slow_probe, wait=0.1)

    def acquire(args, request):
        try:
            outcomes.append(service.acquire(args, request))
        finally:
            outcomes.append(("after the handler", heavy.held(service_state_dir)))
        return outcomes[0]
    registry = Registry()
    registry.register("heavy.acquire", acquire, deadline=heavy.ACQUIRE_DEADLINE)
    with serve(run_dir, registry, close_wait=0.2) as endpoint:
        client = Client(endpoint.socket)
        client.sock.sendall(b'{"type": "heavy.acquire", "args": {}}\n')
        assert entered.wait(10) and heavy.held(service_state_dir)  # the lock is taken before the probe
    try:
        assert not heavy.held(service_state_dir)  # released by the close, though the probe still runs
        go.set()
        assert wait_for(lambda: len(outcomes) == 2)
        assert outcomes[1] == ("after the handler", False) and not heavy.held(service_state_dir)
    finally:
        go.set()
        client.close()


def test_the_lease_is_released_when_the_client_connection_drops(endpoint, service_state_dir):
    first = Client(endpoint.socket)
    assert first.ask("heavy.acquire")["ok"] is True
    first.close()
    assert wait_for(lambda: not heavy.held(service_state_dir))
    with Client(endpoint.socket) as second:
        assert second.ask("heavy.acquire")["ok"] is True


def test_the_lease_is_released_when_the_client_process_dies(endpoint, service_state_dir):
    client = subprocess.Popen([str(endpoint.clients["heavy"]), "--", PYTHON, "-c", "import time; time.sleep(60)"],
                              start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: heavy.held(service_state_dir))
    finally:
        os.killpg(client.pid, signal.SIGKILL)  # the client and the command it started
        client.wait()
    assert wait_for(lambda: not heavy.held(service_state_dir))


def test_the_heavy_client_runs_its_command_and_returns_its_status(endpoint, service_state_dir):
    client = endpoint.clients["heavy"]
    done = run([client, "--", PYTHON, "-c", "import sys; print('checked'); sys.exit(7)"])
    assert done.returncode == 7 and done.stdout == "checked\n"
    assert not heavy.held(service_state_dir)

    # The lease is held while the command runs: a nested request is refused with the distinct status.
    done = run([client, "--", client, "--", "/usr/bin/true"])
    assert done.returncode == 75 and "lease refused: the heavy-process lease is held by another check" in done.stderr
    assert run([client, "--", "/usr/bin/true"]).returncode == 0

    assert run([client, "--", "/nonexistent/command"]).returncode == 127
    assert run([client, PYTHON, "-c", "pass"]).returncode == 2  # without "--"
    assert not heavy.held(service_state_dir)


def test_the_heavy_client_runs_no_shell(endpoint, tmp_path):
    marker = tmp_path / "marker"
    done = run([endpoint.clients["heavy"], "--", "/bin/echo", f"x; touch {marker}"])
    assert done.returncode == 0 and done.stdout == f"x; touch {marker}\n" and not marker.exists()


def test_heavy_processes_skip_this_process_and_the_requester_with_its_ancestors(tmp_path):
    ps = tmp_path / "ps"
    ps.write_text("#!/bin/sh\ncat <<'EOF'\n"
                  "  1     0 /sbin/launchd\n"
                  "100     1 /bin/zsh -c python -m pytest -q\n"
                  "200   100 /venv/bin/python -I /run/clients/heavy -- python -m pytest -q\n"
                  "300     1 /venv/bin/python -m pytest tests/\n"
                  "400     1 /venv/bin/python -m src.reindex\n"
                  "500     1 /usr/bin/vim notes.txt\n"
                  f"{os.getpid()}     1 /venv/bin/python -m pytest this-process\n"
                  "EOF\n")
    ps.chmod(0o755)
    assert heavy.heavy_processes(str(ps), requester=200) == [
        "300 /venv/bin/python -m pytest tests/", "400 /venv/bin/python -m src.reindex"]
    assert [entry.split()[0] for entry in heavy.heavy_processes(str(ps))] == ["100", "200", "300", "400"]
    assert heavy.heavy_processes(str(tmp_path / "no-ps")) == []  # the tick's probe fails open
    assert heavy.foreign_heavy(str(tmp_path / "no-ps")) is None  # the lease grant does not
    failing = tmp_path / "failing-ps"
    failing.write_text("#!/bin/sh\necho '1 0 /sbin/launchd'\nexit 1\n")
    failing.chmod(0o755)
    assert heavy.foreign_heavy(str(failing)) is None and heavy.heavy_processes(str(failing)) == []


def test_the_requester_lineage_is_excluded_only_up_to_the_session_root(tmp_path):
    ps = tmp_path / "ps"
    ps.write_text("#!/bin/sh\ncat <<'EOF'\n"
                  "  1     0 /sbin/launchd\n"
                  " 50     1 /venv/bin/python -m pytest tests/ (a pytest that started the session)\n"
                  " 60    50 /usr/local/bin/claude --print (the session root)\n"
                  "100    60 /bin/zsh -c python -m pytest -q\n"
                  "200   100 /venv/bin/python -I /run/clients/heavy -- python -m pytest -q\n"
                  "300     1 /venv/bin/python -m pytest other/\n"
                  "EOF\n")
    ps.chmod(0o755)

    def pids(**probe):
        return [entry.split()[0] for entry in heavy.foreign_heavy(str(ps), **probe)]
    assert pids(requester=200, session_root=60) == ["50", "300"]
    assert pids(requester=200) == ["300"]  # no session root named: every ancestor is excluded
    assert pids(requester=300, session_root=60) == ["50", "100", "200"]  # outside the session: only itself
    assert socket_service.lineage({200: (100, ""), 100: (60, ""), 60: (50, "")}, 200, stop=60) == {200, 100, 60}
