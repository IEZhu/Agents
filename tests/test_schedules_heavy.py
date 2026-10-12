"""The heavy-process lease behind the runner socket and the ``heavy`` client, on real sockets (#340)."""
import os
import signal
import subprocess
import sys
import time

import pytest

from src.schedules import heavy, lease
from src.schedules.socket_service import Registry, serve
from tests.test_schedules_socket import Client, run, run_dir, short  # noqa: F401 (fixtures)

PYTHON = sys.executable


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def endpoint(run_dir, service_state_dir):
    """The socket with only the heavy lease; no foreign heavy process unless a test brings its own probe."""
    registry = Registry()
    heavy.register(registry, service_state_dir, processes=lambda requester: [], wait=0.1)
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
    assert heavy.heavy_processes(str(tmp_path / "no-ps")) == []
