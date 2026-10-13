"""The runner socket on real sockets: placement, lifecycle, protocol, registry and thin clients (#340).

``AF_UNIX`` paths are limited to 103 bytes on macOS and pytest's ``tmp_path`` is longer, so the
sockets of these tests live under ``tmp_path`` but are reached through a short symlink to it in a
fresh ``mkdtemp`` directory (`short`).
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

import pytest

from src.schedules import socket_service
from src.schedules.socket_service import Connection, Field, Registry, RequestRefused, UnsafeSocketDirectory, serve

PYTHON = sys.executable
INSTALLATION = Path(socket_service.__file__).resolve().parents[2]


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


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def long_run_dir(tmp_path):
    """A run directory whose socket path is too long for AF_UNIX: the socket goes private."""
    path = tmp_path / ("long-run-directory-" * 4)
    path.mkdir(mode=0o700)
    return path


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
        assert answer == {"ok": False, "error": "no complete request within 0.2 seconds"}
        assert client.closed_by_server()


def trickle(client, line, interval):
    """Send ``line`` a byte every ``interval`` seconds until the server answers; seconds taken."""
    started = time.monotonic()
    for byte in line:
        try:
            client.sock.sendall(bytes([byte]))
        except OSError:
            break
        if select.select([client.sock], [], [], interval)[0]:
            break
    return time.monotonic() - started


def test_a_trickled_request_is_cut_off_after_the_read_timeout(run_dir):
    line = json.dumps({"type": "gh.echo", "args": {"name": "trickled-request"}}).encode() + b"\n"
    with serve(run_dir, make_registry(), read_timeout=0.5) as endpoint, Client(endpoint.socket) as client:
        elapsed = trickle(client, line, 0.2)
        answer = json.loads(client.reader.readline())
        assert answer == {"ok": False, "error": "no complete request within 0.5 seconds"}
        assert 0.4 <= elapsed < 2.0
        assert client.closed_by_server()


def test_a_connection_that_holds_a_resource_may_idle_but_not_trickle(run_dir):
    registry = make_registry()
    registry.register("gh.hold", lambda args, request: request.connection.keep("thing", ExitStack()))
    with serve(run_dir, registry, read_timeout=0.3) as endpoint, Client(endpoint.socket) as client:
        assert client.ask("gh.hold") == {"ok": True, "result": None}
        time.sleep(0.6)  # idle past the timeout: allowed while the connection holds something
        assert client.ask("gh.echo", {"name": "after-idle"})["ok"] is True
        elapsed = trickle(client, json.dumps({"type": "gh.echo", "args": {"name": "x" * 30}}).encode() + b"\n", 0.15)
        assert json.loads(client.reader.readline())["error"] == "no complete request within 0.3 seconds"
        assert elapsed < 1.5


def test_a_failing_handler_answers_without_details_and_the_server_keeps_serving(run_dir):
    with serve(run_dir, make_registry()) as endpoint, Client(endpoint.socket) as client:
        assert client.ask("gh.fail") == {"ok": False, "error": "internal error: ZeroDivisionError"}
        assert client.ask("gh.echo", {"name": "a"})["ok"] is True


def test_the_peer_pid_comes_from_the_kernel(run_dir):
    with serve(run_dir, make_registry()) as endpoint, Client(endpoint.socket) as client:
        assert client.ask("gh.whoami") == {"ok": True, "result": {"pid": os.getpid()}}


@pytest.mark.parametrize("args, error", [
    ({"name": "x:main"}, "argument name contains characters it may not contain"),  # a refspec that pushes elsewhere
    ({"name": "main..HEAD"}, "argument name contains characters it may not contain"),  # a range
    ({"name": "user@host"}, "argument name contains characters it may not contain"),
    ({"name": "key=value"}, "argument name contains characters it may not contain"),
    ({"name": "a+b"}, "argument name contains characters it may not contain"),
    ({"name": "a", "count": 0}, "argument count must be between 1 and 1000000"),
    ({"name": "a", "count": 1_000_001}, "argument count must be between 1 and 1000000"),
    ({"name": "a", "count": -10 ** 3999}, "argument count must be between 1 and 1000000"),
    ({"name": "a", "body": "nul\u0000"}, "argument body contains a control character or a lone surrogate"),
    ({"name": "a", "body": "\ud800;$(id)"}, "argument body contains a control character or a lone surrogate"),
    ({"name": "a", "body": "escape\x1b[31m"}, "argument body contains a control character or a lone surrogate"),
    ({"name": "a", "body": "carriage\rreturn"}, "argument body contains a control character or a lone surrogate"),
    ({"name": "a", "body": "c1\x85control"}, "argument body contains a control character or a lone surrogate"),
    ({"name": "a", "body": "delete\x7f"}, "argument body contains a control character or a lone surrogate"),
])
def test_narrow_defaults_refuse_refspecs_unbounded_integers_and_control_characters(run_dir, args, error):
    with serve(run_dir, make_registry()) as endpoint, Client(endpoint.socket) as client:
        assert client.ask("gh.echo", args) == {"ok": False, "error": error}


def test_free_text_keeps_newlines_and_tabs_and_has_a_default_length_limit(run_dir):
    registry = Registry()
    registry.register("gh.note", lambda args, request: len(args["body"]), {"body": Field(str, text=True)})
    with serve(run_dir, registry) as endpoint, Client(endpoint.socket) as client:
        assert client.ask("gh.note", {"body": "line one\n\tline two"}) == {"ok": True, "result": 18}
        limit = socket_service.TEXT_MAX_LENGTH
        assert client.ask("gh.note", {"body": "x" * limit}) == {"ok": True, "result": limit}
        assert client.ask("gh.note", {"body": "x" * (limit + 1)}) == \
            {"ok": False, "error": f"argument body is longer than {limit} characters"}


def test_a_handler_gets_its_deadline_the_peer_pid_and_the_closing_check(run_dir):
    registry = Registry()
    registry.register("gh.context", lambda args, request: {
        "peer": request.peer_pid, "remaining": request.remaining(), "closing": request.closing(),
        "same": request.connection.peer_pid == request.peer_pid}, deadline=2.0)
    with serve(run_dir, registry) as endpoint, Client(endpoint.socket) as client:
        result = client.ask("gh.context")["result"]
        assert result["peer"] == os.getpid() and result["same"] is True and result["closing"] is False
        assert 1.0 < result["remaining"] <= 2.0


def test_close_waits_once_for_all_handlers_and_a_late_handler_writes_nothing(run_dir, tmp_path):
    entered, finished = [], []

    def slow(args, request):
        entered.append(args["name"])
        time.sleep(1.5)  # past the server's close
        if not request.closing():  # the check every handler makes right before an external write
            (tmp_path / f"written-{args['name']}").touch()
        finished.append(args["name"])
        return {}
    registry = Registry()
    registry.register("gh.slow", slow, {"name": Field(str)}, deadline=2.0)
    clients = []
    try:
        with serve(run_dir, registry, close_wait=0.3) as endpoint:
            for name in ("a", "b", "c"):
                clients.append(Client(endpoint.socket))
                clients[-1].sock.sendall(json.dumps({"type": "gh.slow", "args": {"name": name}}).encode() + b"\n")
            assert wait_for(lambda: len(entered) == 3)
            closing = time.monotonic()
        elapsed = time.monotonic() - closing
        assert elapsed < 0.8  # one overall wait of 0.3 seconds, not 0.3 per connection, nor the handlers' 1.5
        assert wait_for(lambda: len(finished) == 3)
        assert list(tmp_path.glob("written-*")) == []
    finally:
        for client in clients:
            client.close()


def test_keep_refuses_and_releases_once_the_connection_is_closed():
    connection = Connection(None)
    released = []
    first = ExitStack()
    first.callback(released.append, "first")
    connection.keep("thing", first)
    duplicate = ExitStack()
    duplicate.callback(released.append, "duplicate")
    with pytest.raises(RequestRefused, match="already holds thing"):
        connection.keep("thing", duplicate)
    assert released == ["duplicate"] and connection.holds("thing")
    connection.close()
    assert released == ["duplicate", "first"] and not connection.holding and connection.closed
    late = ExitStack()
    late.callback(released.append, "late")
    with pytest.raises(RequestRefused, match="the runner socket is closing"):
        connection.keep("thing", late)
    assert released == ["duplicate", "first", "late"] and not connection.holding


def test_take_opens_nothing_once_the_connection_is_closed():
    connection = Connection(None)
    released, opened, busy = [], [], {"tries": 2}

    def opener():
        opened.append(len(opened))
        if busy["tries"]:
            busy["tries"] -= 1
            return None
        stack = ExitStack()
        stack.callback(released.append, "taken")
        return stack
    assert connection.take("thing", opener, wait=1.0) is True and len(opened) == 3  # retried while busy
    with pytest.raises(RequestRefused, match="already holds thing"):
        connection.take("thing", opener)
    assert len(opened) == 3 and connection.holds("thing")
    busy["tries"] = 1_000
    assert connection.take("other", opener, wait=0.1) is False and not connection.holds("other")
    connection.close()
    assert released == ["taken"]
    tries = len(opened)
    with pytest.raises(RequestRefused, match="the runner socket is closing"):
        connection.take("thing", opener)
    assert len(opened) == tries and not connection.holding  # the opener was not even called


def test_a_session_bound_type_answers_only_the_current_session(run_dir):
    root = {"pid": os.getpid()}
    registry = Registry()
    registry.register("gh.bound", lambda args, request: {"peer": request.peer_pid}, session_bound=True)
    registry.register("gh.free", lambda args, request: {"peer": request.peer_pid})
    helper = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(60)"])
    try:
        with serve(run_dir, registry, session_root=lambda: root["pid"]) as endpoint, \
                Client(endpoint.socket) as client:
            assert client.ask("gh.bound") == {"ok": True, "result": {"peer": os.getpid()}}
            done = run([endpoint.clients["gh"], "bound"])  # a descendant of the session root
            assert done.returncode == 0, done.stderr
            assert json.loads(done.stdout)["peer"] not in (None, os.getpid())
            root["pid"] = helper.pid  # the next stage's session: checked again on the same connection
            assert client.ask("gh.bound") == {
                "ok": False, "error": "refused: the caller is not a process of the current stage session"}
            assert client.ask("gh.free") == {"ok": True, "result": {"peer": os.getpid()}}
            done = run([endpoint.clients["gh"], "bound"])
            assert done.returncode == 1 and "not a process of the current stage session" in done.stderr
            root["pid"] = None
            assert client.ask("gh.bound") == {"ok": False, "error": "refused: no stage session is running"}
    finally:
        helper.kill()
        helper.wait()


def test_a_session_bound_type_fails_closed_without_a_lineage_or_a_peer():
    registry = Registry()
    registry.register("gh.bound", lambda args, request: "answered", session_bound=True)
    line = b'{"type": "gh.bound", "args": {}}'
    assert registry.handle(line, Connection(4242), session_root=lambda: 1, lineage=lambda pid: None) == \
        {"ok": False, "error": "refused: the process table cannot be read"}
    assert registry.handle(line, Connection(None), session_root=lambda: 1, lineage=lambda pid: {1}) == \
        {"ok": False, "error": "refused: the caller's process is unknown"}
    assert registry.handle(line, Connection(4242), lineage=lambda pid: {4242, 1}) == \
        {"ok": False, "error": "refused: no stage session is running"}
    assert registry.handle(line, Connection(4242), session_root=lambda: 1, lineage=lambda pid: {4242, 1}) == \
        {"ok": True, "result": "answered"}


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
    with pytest.raises(ValueError, match="only an integer"):
        Field(str, minimum=1)
    with pytest.raises(ValueError, match="minimum not above the maximum"):
        Field(int, minimum=5, maximum=1)
    with pytest.raises(ValueError, match="max_length"):
        Field(str, text=True, max_length=0)
    assert Field(int).bounds == (1, 1_000_000) and Field(int, minimum=0, maximum=9).bounds == (0, 9)
    registry = Registry()
    registry.register("gh.op", lambda args, connection: None)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("gh.op", lambda args, connection: None)
    with pytest.raises(TypeError):
        registry.register("gh.other", lambda args, connection: None, {"name": str})
    for deadline in (0, -1, socket_service.MAX_DEADLINE + 1, True, float("nan")):
        with pytest.raises(ValueError, match="deadline"):
            registry.register("gh.other", lambda args, connection: None, deadline=deadline)
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


@pytest.fixture
def aliased_run_dir(short, tmp_path):
    """A run directory ``<tmp_path>/Sess/run`` for alias spellings of ``<tmp_path>/Sess``."""
    (tmp_path / "Sess").mkdir()
    path = short / "Sess" / "run"
    path.mkdir(mode=0o700)
    return path


def test_a_case_variant_alias_of_a_session_writable_directory_is_refused(aliased_run_dir, tmp_path):
    if not (tmp_path / "SESS").exists():
        pytest.skip("a case-sensitive file system: SESS is another directory")
    for writable in (tmp_path / "SESS", tmp_path / "SESS" / "RUN", tmp_path / "SESS" / "RUN" / "exchange"):
        with pytest.raises(UnsafeSocketDirectory, match="session-writable"):
            socket_service.place_socket(aliased_run_dir, [writable])
    assert not (aliased_run_dir / "control.sock").exists()


def test_a_firmlink_alias_of_a_session_writable_directory_is_refused(aliased_run_dir, tmp_path):
    alias = Path("/System/Volumes/Data") / os.path.realpath(tmp_path).lstrip("/") / "Sess"
    if sys.platform != "darwin" or not alias.is_dir():
        pytest.skip("no /System/Volumes/Data firmlink here")
    with pytest.raises(UnsafeSocketDirectory, match="lies inside the session-writable"):
        socket_service.place_socket(aliased_run_dir, [alias])


def test_unrelated_and_not_yet_existing_session_paths_are_accepted(aliased_run_dir, tmp_path):
    (tmp_path / "repo").mkdir()
    writable = [tmp_path / "repo", tmp_path / "missing" / "tmp", tmp_path / "Sessions"]
    path, private = socket_service.place_socket(aliased_run_dir, writable)
    assert path == aliased_run_dir / "control.sock" and private is False


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


SERVING_CHILD = """
import json, sys, time
from src.schedules.socket_service import Registry, serve
with serve(sys.argv[1], Registry(), temp_root=sys.argv[2]) as endpoint:
    print(json.dumps(endpoint.to_record()), flush=True)
    time.sleep(60)
"""


def start_serving_child(run_dir, temp_root):
    """A process serving a run's socket until it is signalled; returns it and its endpoint record."""
    child = subprocess.Popen([PYTHON, "-c", SERVING_CHILD, str(run_dir), str(temp_root)], cwd=INSTALLATION,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = child.stdout.readline()
    if not line:
        child.kill()
        child.wait()
        pytest.fail(f"the serving child did not start: {child.stderr.read()}")
    return child, json.loads(line)


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_a_stop_signal_inside_serve_leaves_nothing_behind(tmp_path, short, number):
    run_dir = long_run_dir(tmp_path)
    child, record = start_serving_child(run_dir, short)
    try:
        socket_path = Path(record["socket"])
        assert record["private_directory"] is True and stat.S_ISSOCK(os.lstat(socket_path).st_mode)
        child.send_signal(number)
        _, stderr = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert child.returncode == 128 + number, stderr
    assert not socket_path.parent.exists()
    assert list(tmp_path.glob(socket_service.PRIVATE_PREFIX + "*")) == [] and list(run_dir.iterdir()) == []


def test_serve_restores_the_previous_signal_handlers(run_dir):
    before = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
    with serve(run_dir, make_registry()):
        assert all(signal.getsignal(number) is not handler for number, handler in before.items())
    assert {number: signal.getsignal(number) for number in before} == before


def test_remove_leftovers_cleans_up_after_a_killed_run(tmp_path, short):
    child, record = start_serving_child(long_run_dir(tmp_path), short)
    child.kill()  # SIGKILL: no cleanup in the process
    child.communicate(timeout=30)
    private = Path(record["socket"]).parent
    assert stat.S_ISSOCK(os.lstat(record["socket"]).st_mode) and (private / "clients" / "heavy").exists()
    outcome = socket_service.remove_leftovers(record)
    assert outcome["kept"] == [] and not private.exists()
    assert {record["socket"], str(private), *record["clients"].values()} <= set(outcome["removed"])
    assert socket_service.remove_leftovers(record) == {"removed": [], "kept": []}


def leave_endpoint(directory, private):
    """The socket file and clients a killed `serve` leaves in ``directory``; its record."""
    path = directory / "control.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.close()
    clients = socket_service.write_clients(directory / "clients", path)
    return socket_service.Endpoint(path, directory, private, clients).to_record()


def test_remove_leftovers_in_the_run_directory_keeps_the_directory(run_dir):
    (run_dir / "record.json").write_text("{}")
    record = leave_endpoint(run_dir, private=False)
    outcome = socket_service.remove_leftovers(record)
    assert outcome["kept"] == [] and sorted(path.name for path in run_dir.iterdir()) == ["record.json"]


def test_remove_leftovers_in_the_run_directory_needs_the_runners_own_run_directory(run_dir, short):
    """Review round 2: a record whose socket path runs through a symlinked ancestor is not followed."""
    other = short / "other"
    other.mkdir(mode=0o700)
    record = leave_endpoint(other, private=False)
    alias = run_dir / "alias"
    alias.symlink_to(other, target_is_directory=True)
    through_alias = {**record, "socket": str(alias / "control.sock"),
                     "clients": {name: str(alias / "clients" / name) for name in record["clients"]}}
    outcome = socket_service.remove_leftovers(through_alias, run_directory=run_dir)
    assert outcome["removed"] == [] and "not this run's directory" in outcome["kept"][0]
    assert (other / "control.sock").exists() and (other / "clients" / "gh").exists()
    assert socket_service.remove_leftovers(record, run_directory=other)["kept"] == []


def test_remove_leftovers_keeps_what_serve_did_not_create(short):
    foreign = Path(tempfile.mkdtemp(prefix=socket_service.PRIVATE_PREFIX, dir=short))
    (foreign / "control.sock").write_text("a regular file")
    outcome = socket_service.remove_leftovers({"socket": str(foreign / "control.sock"), "private_directory": True})
    assert outcome["removed"] == [] and "not the kind of entry" in outcome["kept"][0]
    assert (foreign / "control.sock").read_text() == "a regular file" and foreign.exists()

    unnamed = short / "unnamed"
    unnamed.mkdir(mode=0o700)
    record = leave_endpoint(unnamed, private=True)
    outcome = socket_service.remove_leftovers(record)
    assert outcome["removed"] == [] and "not a private directory serve creates" in outcome["kept"][0]
    assert (unnamed / "control.sock").exists()

    crowded = Path(tempfile.mkdtemp(prefix=socket_service.PRIVATE_PREFIX, dir=short))
    record = leave_endpoint(crowded, private=True)
    (crowded / "notes.txt").write_text("kept")
    outcome = socket_service.remove_leftovers(record)
    assert outcome["removed"] == [] and "other entries" in outcome["kept"][0]

    elsewhere = {**record, "clients": {"gh": str(short / "gh")}}
    assert "do not lie beside its socket" in socket_service.remove_leftovers(elsewhere)["kept"][0]
    assert socket_service.remove_leftovers({}) == {"removed": [], "kept": ["the record names no endpoint"]}
    assert "not a socket path" in socket_service.remove_leftovers(
        {"socket": str(short / ".." / "control.sock"), "private_directory": False})["kept"][0]
    assert (crowded / "control.sock").exists() and (crowded / "clients" / "gh").exists()


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
            assert text.splitlines()[0] == f"#!{socket_service.client_interpreter()} -I"
            assert f"SOCKET_PATH = {str(endpoint.socket)!r}" in text and f"CLIENT = {name!r}" in text
            assert not re.search(r"^\s*(from|import)\s+src\b", text, re.M)


def test_clients_run_on_the_resolved_base_interpreter(run_dir, monkeypatch):
    base = os.path.realpath(getattr(sys, "_base_executable", "") or sys.executable)
    assert socket_service.client_interpreter() == base and os.path.realpath(base) == base
    with serve(run_dir, make_registry()) as endpoint:
        assert endpoint.clients["gh"].read_text().splitlines()[0] == f"#!{base} -I"
        done = run([endpoint.clients["gh"], "echo", '{"name": "base"}'])
        assert done.returncode == 0 and json.loads(done.stdout) == {"echo": {"name": "base"}}
    monkeypatch.setattr(sys, "_base_executable", "", raising=False)
    assert socket_service.client_interpreter() == sys.executable


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
