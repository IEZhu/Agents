"""The runner socket on real sockets: placement, lifecycle, protocol, registry and thin clients (#340).

``AF_UNIX`` paths are limited to 103 bytes on macOS and pytest's ``tmp_path`` is longer, so the
sockets of these tests live under ``tmp_path`` but are reached through a short symlink to it in a
fresh ``mkdtemp`` directory (`short`).
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile

import pytest

from src.schedules import socket_service
from src.schedules.socket_service import Field, Registry, UnsafeSocketDirectory, serve


@pytest.fixture
def short(tmp_path):
    """A short alias of ``tmp_path``."""
    holder = Path(tempfile.mkdtemp(prefix="sock-"))
    alias = holder / "t"
    alias.symlink_to(tmp_path, target_is_directory=True)
    yield alias
    shutil.rmtree(holder)  # removes the symlink only


@pytest.fixture
def run_dir(short):
    path = short / "run"
    path.mkdir(mode=0o700)
    return path


class Client:
    """A raw protocol client in this process."""

    def __init__(self, path, timeout=10.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.reader = self.sock.makefile("rb")

    def raw(self, line: bytes):
        self.sock.sendall(line + b"\n")
        answer = self.reader.readline()
        return json.loads(answer) if answer else None

    def ask(self, request_type, args=None):
        return self.raw(json.dumps({"type": request_type, "args": {} if args is None else args}).encode())

    def closed_by_server(self) -> bool:
        try:
            return self.sock.recv(1) == b""
        except ConnectionResetError:
            return True

    def close(self):
        self.reader.close()
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def make_registry():
    registry = Registry()
    registry.register("gh.echo", lambda args, connection: {"echo": args},
                      {"name": Field(str), "count": Field(int, required=False),
                       "body": Field(str, text=True, required=False, max_length=1000)})
    registry.register("gh.whoami", lambda args, connection: {"pid": connection.peer_pid})
    registry.register("gh.fail", lambda args, connection: 1 / 0)
    return registry


def run(argv, **kwargs):
    return subprocess.run([str(part) for part in argv], capture_output=True, text=True, timeout=60, **kwargs)


# --- protocol and registry ---------------------------------------------------------------------------

@pytest.mark.parametrize("request_type", ["gh.nothing", "worker.decide", "heavy.acquire", "shell", "exec", ""])
def test_unknown_request_type_is_refused(run_dir, request_type):
    with serve(run_dir, make_registry()) as endpoint, Client(endpoint.socket) as client:
        response = client.ask(request_type, {})
        assert response["ok"] is False and response["error"].startswith("unknown request type")
        assert client.ask("gh.echo", {"name": "still-serving"})["ok"] is True


@pytest.mark.parametrize("sent, error", [
    ({"type": "gh.echo", "args": {"name": "x", "command": "rm -rf ~"}}, "unknown argument: 'command'"),
    ({"type": "gh.echo", "args": {"name": "x", "argv": ["sh", "-c", "id"]}}, "unknown argument: 'argv'"),
    ({"type": "gh.echo", "args": {"name": "x", "shell": True}}, "unknown argument: 'shell'"),
    ({"type": "gh.echo", "args": {"name": "x; rm -rf ~"}}, "contains characters"),
    ({"type": "gh.echo", "args": {"name": "$(id)"}}, "contains characters"),
    ({"type": "gh.echo", "args": {"name": "`id`"}}, "contains characters"),
    ({"type": "gh.echo", "args": {"name": "/bin/sh"}}, "contains characters"),
    ({"type": "shell", "args": {"command": "id"}}, "unknown request type"),
    ({"type": "gh.exec", "args": {"command": "id"}}, "unknown request type"),
])
def test_a_request_carrying_a_shell_command_is_refused(run_dir, sent, error):
    with serve(run_dir, make_registry()) as endpoint, Client(endpoint.socket) as client:
        response = client.raw(json.dumps(sent).encode())
        assert response["ok"] is False and error in response["error"]


@pytest.mark.parametrize("line, error", [
    (b'{"type": "gh.echo", "args": {"name": 1}}', "argument name must be str"),
    (b'{"type": "gh.echo", "args": {"name": "a", "count": true}}', "argument count must be int"),
    (b'{"type": "gh.echo", "args": {"name": "a", "count": "1"}}', "argument count must be int"),
    (b'{"type": "gh.echo", "args": {}}', "missing argument: name"),
    (b'not json', "malformed request"),
    (b'["gh.echo", {}]', "malformed request"),
    (b'{"type": "gh.echo"}', "malformed request"),
    (b'{"type": "gh.echo", "args": {"name": "a"}, "extra": 1}', "malformed request"),
    (b'{"type": "gh.echo", "args": ["name"]}', "malformed request"),
    (b'{"type": 7, "args": {}}', "malformed request"),
    (b'{"type": "gh.echo", "type": "gh.whoami", "args": {}}', "duplicate key"),
    (b'{"type": "gh.echo", "args": {"name": "a", "count": NaN}}', "NaN is not JSON"),
    (b'\xff\xfe', "malformed request"),
])
def test_malformed_requests_and_wrong_types_are_refused(run_dir, line, error):
    with serve(run_dir, make_registry()) as endpoint, Client(endpoint.socket) as client:
        response = client.raw(line)
        assert response["ok"] is False and error in response["error"]


def test_free_text_and_valid_arguments_reach_the_handler_as_data(run_dir):
    text = "a comment with `backticks`, $(substitutions) and; separators"
    with serve(run_dir, make_registry()) as endpoint, Client(endpoint.socket) as client:
        response = client.ask("gh.echo", {"name": "issue-340", "count": 2, "body": text})
        assert response == {"ok": True, "result": {"echo": {"name": "issue-340", "count": 2, "body": text}}}
        assert client.ask("gh.echo", {"name": "a", "body": "x" * 1001})["error"] == \
            "argument body is longer than 1000 characters"


def test_over_long_request_is_refused_and_the_connection_closed(run_dir):
    with serve(run_dir, make_registry()) as endpoint, Client(endpoint.socket) as client:
        body = "x" * (socket_service.MAX_REQUEST + 10)
        response = client.raw(json.dumps({"type": "gh.echo", "args": {"name": "a", "body": body}}).encode())
        assert response["ok"] is False and response["error"].startswith("request too long")
        assert client.closed_by_server()


def test_an_idle_connection_times_out(run_dir):
    with serve(run_dir, make_registry(), read_timeout=0.2) as endpoint, Client(endpoint.socket) as client:
        answer = json.loads(client.reader.readline())
        assert answer["ok"] is False and "no request within 0.2 seconds" in answer["error"]
        assert client.closed_by_server()


def test_a_failing_handler_answers_without_details_and_the_server_keeps_serving(run_dir):
    with serve(run_dir, make_registry()) as endpoint, Client(endpoint.socket) as client:
        assert client.ask("gh.fail") == {"ok": False, "error": "internal error: ZeroDivisionError"}
        assert client.ask("gh.echo", {"name": "a"})["ok"] is True


def test_the_peer_pid_comes_from_the_kernel(run_dir):
    with serve(run_dir, make_registry()) as endpoint, Client(endpoint.socket) as client:
        assert client.ask("gh.whoami") == {"ok": True, "result": {"pid": os.getpid()}}


@pytest.mark.parametrize("request_type", ["shell", "exec", "gh.exec", "worker.shell", "heavy.command",
                                          "gh.passthrough", "other.list", "gh.", "gh.List", "gh.a b"])
def test_the_registry_refuses_generic_and_unknown_service_types(request_type):
    with pytest.raises(ValueError, match="not an allowed request type"):
        Registry().register(request_type, lambda args, connection: None)


@pytest.mark.parametrize("name", ["command", "argv", "shell", "cmd", "args", "exec", "script", "Name", "a-b"])
def test_the_registry_refuses_command_like_argument_fields(name):
    with pytest.raises(ValueError, match="not an allowed argument name"):
        Registry().register("gh.op", lambda args, connection: None, {name: Field(str)})


def test_fields_and_registrations_are_checked():
    with pytest.raises(ValueError):
        Field(list)
    with pytest.raises(ValueError):
        Field(int, text=True)
    with pytest.raises(ValueError):
        Field(str, text=True, pattern=re.compile("x"))
    registry = Registry()
    registry.register("gh.op", lambda args, connection: None)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("gh.op", lambda args, connection: None)
    with pytest.raises(TypeError):
        registry.register("gh.other", lambda args, connection: None, {"name": str})
    assert registry.types() == ["gh.op"]


# --- placement ---------------------------------------------------------------------------------------

def test_the_socket_lives_in_the_run_directory_when_its_path_fits(run_dir):
    with serve(run_dir, make_registry()) as endpoint:
        assert endpoint.socket == run_dir / "control.sock" and endpoint.private is False
        assert stat.S_ISSOCK(os.lstat(endpoint.socket).st_mode)
        assert stat.S_IMODE(os.lstat(endpoint.socket).st_mode) == 0o600
        assert endpoint.to_record()["socket"] == str(run_dir / "control.sock")


def test_an_over_long_socket_path_falls_back_to_a_private_directory(tmp_path, short):
    run_dir = tmp_path / ("long-run-directory-" * 4)
    run_dir.mkdir(mode=0o700)
    assert len(str(run_dir / "control.sock")) > socket_service.socket_limit()
    with serve(run_dir, make_registry(), temp_root=short) as endpoint:
        assert endpoint.private is True and endpoint.directory.parent == short
        assert endpoint.directory.name.startswith(socket_service.PRIVATE_PREFIX)
        assert stat.S_IMODE(endpoint.directory.stat().st_mode) == 0o700
        assert len(os.fsencode(str(endpoint.socket))) <= socket_service.socket_limit()
        with Client(endpoint.socket) as client:
            assert client.ask("gh.echo", {"name": "a"})["ok"] is True
        private = endpoint.directory
    assert not private.exists() and not (run_dir / "control.sock").exists()
    assert list(tmp_path.glob(socket_service.PRIVATE_PREFIX + "*")) == []


def test_the_platform_limits():
    assert socket_service.socket_limit("darwin") == 103
    assert socket_service.socket_limit("linux") == 107


@pytest.mark.parametrize("writable", [
    lambda run_dir, tmp_path: [run_dir],  # the run directory itself
    lambda run_dir, tmp_path: [tmp_path / "run"],  # the same directory by its real path
    lambda run_dir, tmp_path: [tmp_path],  # a directory around it
    lambda run_dir, tmp_path: [tmp_path / "repo", run_dir / "exchange"],  # one inside it
])
def test_a_session_writable_directory_is_never_chosen(run_dir, tmp_path, writable):
    with pytest.raises(UnsafeSocketDirectory, match="session-writable"):
        with serve(run_dir, make_registry(), session_writable=writable(run_dir, tmp_path)):
            pytest.fail("the socket opened")
    assert not (run_dir / "control.sock").exists() and not (run_dir / "clients").exists()


def test_a_private_directory_inside_the_session_tmpdir_is_refused(tmp_path, short):
    run_dir = tmp_path / ("long-run-directory-" * 4)
    run_dir.mkdir(mode=0o700)
    session_tmp = short / "session-tmp"
    session_tmp.mkdir()
    with pytest.raises(UnsafeSocketDirectory, match="lies inside the session-writable"):
        with serve(run_dir, make_registry(), temp_root=session_tmp, session_writable=[session_tmp]):
            pytest.fail("the socket opened")
    assert list(session_tmp.iterdir()) == []  # the private directory it made is gone


def test_unsafe_directories_are_refused(run_dir, short):
    run_dir.chmod(0o770)
    with pytest.raises(UnsafeSocketDirectory, match="writable by its group"):
        socket_service.place_socket(run_dir)
    run_dir.chmod(0o700)
    link = short / "link"
    link.symlink_to(run_dir, target_is_directory=True)
    with pytest.raises(UnsafeSocketDirectory, match="symlink"):
        socket_service.place_socket(link)
    with pytest.raises(UnsafeSocketDirectory, match="cannot be read"):
        socket_service.place_socket(short / "missing")
    with pytest.raises(ValueError, match="absolute"):
        socket_service.place_socket(run_dir, ["relative/path"])


def test_an_existing_file_at_the_socket_path_is_refused_and_kept(run_dir):
    existing = run_dir / "control.sock"
    existing.write_text("not ours")
    with pytest.raises(UnsafeSocketDirectory, match="existing file"):
        with serve(run_dir, make_registry()):
            pytest.fail("the socket opened")
    assert existing.read_text() == "not ours"


# --- lifecycle ---------------------------------------------------------------------------------------

def test_the_socket_clients_and_connections_are_gone_after_the_run(run_dir):
    with serve(run_dir, make_registry()) as endpoint:
        client = Client(endpoint.socket)
        assert client.ask("gh.echo", {"name": "a"})["ok"] is True
        assert endpoint.socket.exists() and all(path.exists() for path in endpoint.clients.values())
    try:
        assert client.closed_by_server()
    finally:
        client.close()
    assert not endpoint.socket.exists() and not (run_dir / "clients").exists()
    assert list(run_dir.iterdir()) == []
    with pytest.raises(OSError):
        Client(endpoint.socket)


def test_the_socket_is_removed_when_the_run_raises(tmp_path, short):
    run_dir = tmp_path / ("long-run-directory-" * 4)
    run_dir.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="the run failed"):
        with serve(run_dir, make_registry(), temp_root=short) as endpoint:
            client = Client(endpoint.socket)
            raise RuntimeError("the run failed")
    try:
        assert client.closed_by_server()
    finally:
        client.close()
    assert not endpoint.socket.exists() and not endpoint.directory.exists()


# --- thin clients ------------------------------------------------------------------------------------

def test_clients_are_isolated_stdlib_scripts_with_the_socket_embedded(run_dir):
    with serve(run_dir, make_registry()) as endpoint:
        assert set(endpoint.clients) == {"gh", "worker", "heavy"}
        assert endpoint.commands() == {f"{name}_cmd": shlex.quote(str(path)) for name, path in endpoint.clients.items()}
        assert endpoint.to_record()["clients"]["heavy"] == str(run_dir / "clients" / "heavy")
        for name, path in endpoint.clients.items():
            assert path.parent == run_dir / "clients" and path.is_absolute()
            assert stat.S_IMODE(path.stat().st_mode) == 0o555
            text = path.read_text()
            assert text.splitlines()[0] == f"#!{sys.executable} -I"
            assert f"SOCKET_PATH = {str(endpoint.socket)!r}" in text and f"CLIENT = {name!r}" in text
            assert not re.search(r"^\s*(from|import)\s+src\b", text, re.M)


def test_gh_and_worker_send_their_request_types_and_print_the_result(run_dir):
    with serve(run_dir, make_registry()) as endpoint:
        done = run([endpoint.clients["gh"], "echo", '{"name": "issue-340"}'], cwd=run_dir)
        assert done.returncode == 0 and json.loads(done.stdout) == {"echo": {"name": "issue-340"}}

        done = run([endpoint.clients["worker"], "launch", "{}"])
        assert done.returncode == 1 and "refused: unknown request type: 'worker.launch'" in done.stderr

        done = run([endpoint.clients["gh"], "echo", '{"name": "x", "command": "id"}'])
        assert done.returncode == 1 and "unknown argument: 'command'" in done.stderr

        assert run([endpoint.clients["gh"], "echo", "[1]"]).returncode == 2
        assert run([endpoint.clients["gh"]]).returncode == 2


def test_a_client_reports_an_unreachable_socket(tmp_path):
    path = tmp_path / "gh"
    path.write_text(socket_service.client_source(tmp_path / "missing.sock", "gh", sys.executable))
    path.chmod(0o555)
    done = run([path, "echo", "{}"])
    assert done.returncode == 69 and "cannot reach the runner socket" in done.stderr


def test_the_client_interpreter_must_be_absolute_without_spaces(tmp_path):
    for python in ("python3", "/opt/my python/bin/python"):
        with pytest.raises(ValueError, match="interpreter"):
            socket_service.client_source(tmp_path / "s", "gh", python)


def test_the_client_stub_is_an_imported_module_so_the_drift_check_covers_it():
    from src.schedules import drift
    installation = Path(socket_service.__file__).resolve().parents[2]
    assert "src/schedules/client_stub.py" in drift.imported_files(installation)
