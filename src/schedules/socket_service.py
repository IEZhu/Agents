"""The runner socket (design 5.6, 10.1): fixed request types for sandboxed sessions, never a command passthrough.

A run opens the socket with `serve` around its stage sessions; a session may only connect. The
socket is ``{run_dir}/control.sock`` when that absolute path fits the platform's ``AF_UNIX`` limit
(`socket_limit`), otherwise ``control.sock`` in a private directory that ``tempfile.mkdtemp``
creates under the runner's own temporary directory. Either directory must belong to this user, be
no symlink, be writable by no group or other user, and lie neither inside nor around a path the
session can write (`check_directory`); anything else raises before the socket is bound. An
existing file at the socket path is refused, never removed.

The protocol is newline-delimited JSON. A request is ``{"type": str, "args": object}`` of at most
``MAX_REQUEST`` bytes; the answer is ``{"ok": true, "result": ...}`` or ``{"ok": false, "error": str}``.
A connection that holds nothing must send each request within ``READ_TIMEOUT`` seconds.
Handlers register under fixed types ``gh.<operation>``, ``worker.<operation>`` and
``heavy.<operation>`` with an argument specification (`Field`); an unknown type, a malformed
request, an unknown argument or a wrong type is refused before a handler runs. The registry has
no generic type, refuses argument fields named like a command (``command``, ``argv``, ``shell``…),
and a string argument must match a narrow pattern unless its field declares free text, which a
handler treats as data only. The heavy-process lease (`src.schedules.heavy`) registers here; the
GitHub broker (#342) and the worker launcher (#345) will.

`serve` also writes the thin clients ``gh``, ``worker`` and ``heavy`` (`src.schedules.client_stub`)
into a ``clients`` directory beside the socket. When the run ends, normally or by an exception, it
stops serving, closes open connections (which releases what they hold) and removes the socket, the
clients and the private directory. POSIX only: the socket is an ``AF_UNIX`` socket.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import socket
import stat
import struct
import sys
import tempfile
import threading
from typing import Any, Callable, Iterable, Iterator, Mapping

from . import client_stub  # imported, not only read, so the runner's drift check covers it

SOCKET_NAME = "control.sock"
CLIENTS_DIR = "clients"
CLIENTS = ("gh", "worker", "heavy")  # also the services: a request type is "<service>.<operation>"
STUB = Path(client_stub.__file__)
PRIVATE_PREFIX = "autodev-socket-"
MAX_REQUEST = 64 * 1024  # bytes of one request line, the newline excluded
READ_TIMEOUT = 10.0  # seconds; lifted while the connection holds a resource such as the heavy lease
MAX_CONNECTIONS = 16
CLOSE_WAIT = 5.0  # seconds `close` waits for each connection's thread
AF_UNIX_LIMITS = {"darwin": 103, "linux": 107}  # sizeof(sun_path) minus the terminating NUL
DEFAULT_AF_UNIX_LIMIT = 103
_SOL_LOCAL, _LOCAL_PEERPID = 0, 0x002  # <sys/un.h> on macOS; the socket module names neither

TYPE = re.compile(r"(?:gh|worker|heavy)\.[a-z][a-z0-9_-]{0,63}")
FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
FORBIDDEN_OPERATIONS = frozenset({"command", "eval", "exec", "passthrough", "raw", "shell", "spawn", "system"})
FORBIDDEN_FIELDS = frozenset({"args", "argv", "cmd", "command", "exec", "executable", "program", "script", "shell"})
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+=-]{0,255}")  # no space, slash, quote or shell syntax
FIELD_KINDS = (str, int, bool)


class UnsafeSocketDirectory(RuntimeError):
    """The socket's directory, or the socket path, fails a placement check."""


class RequestRefused(Exception):
    """A refused request; the message goes to the client as ``error``."""


# --- requests and the registry ---------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    """One argument of a request type: its JSON type and, for a string, what it may contain.

    A string must match ``pattern`` in full (default `TOKEN`); ``text=True`` declares free text,
    such as a comment body, that the handler only stores or sends as data.
    """
    kind: type
    required: bool = True
    pattern: re.Pattern | None = None
    text: bool = False
    max_length: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in FIELD_KINDS:
            raise ValueError(f"an argument is one of {[kind.__name__ for kind in FIELD_KINDS]}")
        if self.kind is not str and (self.pattern or self.text or self.max_length is not None):
            raise ValueError("only a string argument takes a pattern, text or max_length")
        if self.text and self.pattern:
            raise ValueError("free text takes no pattern")

    def problem(self, name: str, value: Any) -> str | None:
        """Why ``value`` is not acceptable, or None."""
        accepted = isinstance(value, self.kind) and not (self.kind is int and isinstance(value, bool))
        if not accepted:
            return f"argument {name} must be {self.kind.__name__}"
        if self.kind is str:
            if self.max_length is not None and len(value) > self.max_length:
                return f"argument {name} is longer than {self.max_length} characters"
            if not self.text and not (self.pattern or TOKEN).fullmatch(value):
                return f"argument {name} contains characters it may not contain"
        return None


Handler = Callable[[dict, "Connection"], Any]


@dataclass(frozen=True)
class _Entry:
    function: Handler
    spec: Mapping[str, Field]


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict:
    found = {}
    for key, value in pairs:
        if key in found:
            raise RequestRefused(f"malformed request: duplicate key {key[:64]!r}")
        found[key] = value
    return found


def _no_constant(name: str) -> None:
    raise RequestRefused(f"malformed request: {name} is not JSON")


def parse_request(line: bytes) -> tuple[str, dict]:
    """``(type, args)`` of one request line; ``RequestRefused`` when it is not a request."""
    try:
        value = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RequestRefused("malformed request: not a JSON object") from None
    if not isinstance(value, dict) or set(value) != {"type", "args"}:
        raise RequestRefused('malformed request: expected {"type": str, "args": object}')
    if not isinstance(value["type"], str) or not isinstance(value["args"], dict):
        raise RequestRefused('malformed request: expected {"type": str, "args": object}')
    return value["type"], value["args"]


class Registry:
    """Request types and their handlers; a handler gets the checked ``args`` and the `Connection`."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def register(self, request_type: str, function: Handler, spec: Mapping[str, Field] | None = None) -> None:
        if not TYPE.fullmatch(request_type) or request_type.partition(".")[2] in FORBIDDEN_OPERATIONS:
            raise ValueError(f"not an allowed request type: {request_type!r}")
        if request_type in self._entries:
            raise ValueError(f"request type already registered: {request_type}")
        spec = dict(spec or {})
        for name, field in spec.items():
            if not FIELD_NAME.fullmatch(name) or name in FORBIDDEN_FIELDS:
                raise ValueError(f"not an allowed argument name: {name!r}")
            if not isinstance(field, Field):
                raise TypeError(f"argument {name} needs a Field")
        self._entries[request_type] = _Entry(function, spec)

    def types(self) -> list[str]:
        return sorted(self._entries)

    def handle(self, line: bytes, connection: Connection) -> dict:
        """The response to one request line."""
        try:
            request_type, args = parse_request(line)
            entry = self._entries.get(request_type)
            if entry is None:
                raise RequestRefused(f"unknown request type: {request_type[:80]!r}")
            self._check(entry.spec, args)
            return {"ok": True, "result": entry.function(args, connection)}
        except RequestRefused as refused:
            return {"ok": False, "error": str(refused)}
        except Exception as error:  # a handler's failure answers this request; the server keeps serving
            return {"ok": False, "error": f"internal error: {type(error).__name__}"}

    @staticmethod
    def _check(spec: Mapping[str, Field], args: dict) -> None:
        for name in args:
            if name not in spec:
                raise RequestRefused(f"unknown argument: {name[:64]!r}")
        for name, field in spec.items():
            if name not in args:
                if field.required:
                    raise RequestRefused(f"missing argument: {name}")
                continue
            if problem := field.problem(name, args[name]):
                raise RequestRefused(problem)


class Connection:
    """One client connection. A handler keeps a resource on it (`keep`) until the client releases
    it or disconnects, or the server closes; while it holds one, the read timeout is lifted."""

    def __init__(self, peer_pid: int | None) -> None:
        self.peer_pid = peer_pid  # from the kernel, not from the client; None when unknown
        self._resources: dict[str, ExitStack] = {}
        self._lock = threading.Lock()

    @property
    def holding(self) -> bool:
        with self._lock:
            return bool(self._resources)

    def holds(self, name: str) -> bool:
        with self._lock:
            return name in self._resources

    def keep(self, name: str, stack: ExitStack) -> None:
        with self._lock:
            if name in self._resources:
                raise RequestRefused(f"this connection already holds {name}")
            self._resources[name] = stack

    def release(self, name: str) -> bool:
        with self._lock:
            stack = self._resources.pop(name, None)
        if stack is None:
            return False
        stack.close()
        return True

    def release_all(self) -> None:
        with self._lock:
            stacks, self._resources = list(self._resources.values()), {}
        for stack in stacks:
            stack.close()


def peer_pid(sock: socket.socket) -> int | None:
    """The PID of the process at the other end, from the kernel; None when the platform cannot tell."""
    try:
        if sys.platform == "darwin":
            return struct.unpack("i", sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4))[0]
        if hasattr(socket, "SO_PEERCRED"):
            credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", credentials)[0]
    except OSError:
        return None
    return None


# --- the server ------------------------------------------------------------------------------------

def _answer(sock: socket.socket, response: dict) -> bool:
    try:
        data = json.dumps(response).encode() + b"\n"
    except (TypeError, ValueError):
        data = json.dumps({"ok": False, "error": "internal error: the result is not JSON"}).encode() + b"\n"
    try:
        sock.sendall(data)
    except OSError:
        return False
    return True


class Server:
    """A thread per connection behind one accept thread; `close` ends them all."""

    def __init__(self, path: Path, registry: Registry, *, read_timeout: float = READ_TIMEOUT,
                 max_connections: int = MAX_CONNECTIONS) -> None:
        self.path = Path(path)
        self.registry = registry
        self.read_timeout = read_timeout
        self.max_connections = max_connections
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._active: dict[socket.socket, tuple[Connection, threading.Thread]] = {}
        self._listener: socket.socket | None = None
        self._inode: int | None = None
        self._wake_read, self._wake_write = socket.socketpair()
        self._accepting: threading.Thread | None = None

    def start(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))  # fails when anything exists at the path
            self._inode = os.lstat(self.path).st_ino
            os.chmod(self.path, 0o600)
            listener.listen(self.max_connections)
            listener.setblocking(False)  # a connection gone between select and accept never blocks
        except BaseException:
            listener.close()
            self._remove_socket()
            self._wake_read.close()
            self._wake_write.close()
            raise
        self._listener = listener
        self._accepting = threading.Thread(target=self._accept_loop, name="autodev-socket-accept", daemon=True)
        self._accepting.start()

    def _accept_loop(self) -> None:
        while not self._closing.is_set():
            try:
                readable, _, _ = select.select([self._listener, self._wake_read], [], [])
            except (OSError, ValueError):
                return
            if self._wake_read in readable or self._closing.is_set():
                return
            try:
                sock, _ = self._listener.accept()  # `_serve` sets its timeout
            except OSError:
                continue
            with self._lock:
                if self._closing.is_set():
                    sock.close()
                    return
                if len(self._active) >= self.max_connections:
                    _answer(sock, {"ok": False, "error": "too many connections"})
                    sock.close()
                    continue
                connection = Connection(peer_pid(sock))
                thread = threading.Thread(target=self._serve, args=(sock, connection),
                                          name="autodev-socket-connection", daemon=True)
                self._active[sock] = (connection, thread)
                thread.start()  # under the lock: `close` never sees a thread it cannot join

    def _serve(self, sock: socket.socket, connection: Connection) -> None:
        buffer = b""
        try:
            while not self._closing.is_set():
                sock.settimeout(None if connection.holding else self.read_timeout)
                line, buffer = self._read_line(sock, buffer)
                if line is None or self._closing.is_set():
                    return
                if not _answer(sock, self.registry.handle(line, connection)):
                    return
        finally:
            connection.release_all()
            with self._lock:
                self._active.pop(sock, None)
            sock.close()

    def _read_line(self, sock: socket.socket, buffer: bytes) -> tuple[bytes | None, bytes]:
        """The next request line, or None at the end of the connection (EOF, timeout, too long)."""
        while b"\n" not in buffer:
            if len(buffer) > MAX_REQUEST:
                break
            try:
                chunk = sock.recv(MAX_REQUEST + 1)
            except socket.timeout:
                _answer(sock, {"ok": False, "error": f"no request within {self.read_timeout:g} seconds"})
                return None, b""
            except OSError:
                return None, b""
            if not chunk:
                return None, b""
            buffer += chunk
        line, _, rest = buffer.partition(b"\n")
        if len(line) > MAX_REQUEST:
            _answer(sock, {"ok": False, "error": f"request too long: the limit is {MAX_REQUEST} bytes"})
            return None, b""
        return line, rest

    def close(self) -> None:
        """Stop accepting, end every connection (which releases what it holds) and remove the socket."""
        self._closing.set()
        try:
            self._wake_write.send(b"x")
        except OSError:
            pass
        if self._accepting is not None:
            self._accepting.join(CLOSE_WAIT)
        if self._listener is not None:
            self._listener.close()
        self._wake_read.close()
        self._wake_write.close()
        self._remove_socket()
        with self._lock:
            active = list(self._active.items())
        for sock, _ in active:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for _, (connection, thread) in active:
            thread.join(CLOSE_WAIT)
            connection.release_all()  # a handler still running past the wait loses its resources here

    def _remove_socket(self) -> None:
        """Remove the socket file this server bound, and nothing that replaced it."""
        try:
            found = os.lstat(self.path)
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(found.st_mode) and found.st_ino == self._inode:
            os.unlink(self.path)


# --- placement, clients and the endpoint -----------------------------------------------------------

def socket_limit(platform: str = sys.platform) -> int:
    """The longest ``AF_UNIX`` path in bytes: 103 on macOS, 107 on Linux."""
    return AF_UNIX_LIMITS.get("linux" if platform.startswith("linux") else platform, DEFAULT_AF_UNIX_LIMIT)


def _fits(path: Path) -> bool:
    return len(os.fsencode(str(path))) <= socket_limit()


def _absolute(paths: Iterable[str | Path]) -> list[Path]:
    found = [Path(path) for path in paths]
    if relative := [str(path) for path in found if not path.is_absolute()]:
        raise ValueError(f"session-writable paths must be absolute: {relative}")
    return found


def check_directory(directory: Path, session_writable: Iterable[str | Path] = ()) -> None:
    """Raise `UnsafeSocketDirectory` unless ``directory`` suits the socket: this user's own, no
    symlink, writable by no group or other user, and neither inside nor around a session-writable path."""
    try:
        found = os.lstat(directory)
    except OSError as error:
        raise UnsafeSocketDirectory(f"the socket directory {directory} cannot be read: {error}") from None
    if stat.S_ISLNK(found.st_mode) or not stat.S_ISDIR(found.st_mode):
        raise UnsafeSocketDirectory(f"the socket directory {directory} is a symlink or not a directory")
    if found.st_uid != os.getuid():
        raise UnsafeSocketDirectory(f"the socket directory {directory} belongs to another user")
    if found.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafeSocketDirectory(f"the socket directory {directory} is writable by its group or others")
    real = Path(os.path.realpath(directory))
    for path in _absolute(session_writable):
        writable = Path(os.path.realpath(path))
        if real == writable or writable in real.parents:
            raise UnsafeSocketDirectory(f"the socket directory {directory} lies inside the session-writable {path}")
        if real in writable.parents:
            raise UnsafeSocketDirectory(f"the socket directory {directory} contains the session-writable {path}")


def place_socket(run_dir: str | Path, session_writable: Iterable[str | Path] = (), *,
                 temp_root: str | Path | None = None) -> tuple[Path, bool]:
    """The socket path and whether its directory is a private one created here (then the caller
    removes it). ``temp_root`` replaces the runner's temporary directory for the private one."""
    run_dir = Path(run_dir)
    if not run_dir.is_absolute():
        raise ValueError(f"the run directory must be absolute: {run_dir}")
    session = _absolute(session_writable)
    if _fits(run_dir / SOCKET_NAME):
        check_directory(run_dir, session)
        directory, private = run_dir, False
    else:
        directory = Path(tempfile.mkdtemp(prefix=PRIVATE_PREFIX, dir=temp_root))  # mode 0700
        private = True
        try:
            check_directory(directory, session)
            if not _fits(directory / SOCKET_NAME):
                raise UnsafeSocketDirectory(f"the private socket directory {directory} is too long for AF_UNIX")
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    path = directory / SOCKET_NAME
    if os.path.lexists(path):
        raise UnsafeSocketDirectory(f"refusing the existing file at the socket path {path}")
    return path, private


def client_source(socket_path: Path, client: str, python: str) -> str:
    """The text of one thin client: the stub with its interpreter line, socket path and name."""
    if not os.path.isabs(python) or any(character.isspace() for character in python):
        raise ValueError(f"the client interpreter must be an absolute path without spaces: {python!r}")
    if client not in CLIENTS:
        raise ValueError(f"not a client: {client!r}")
    source = STUB.read_text(encoding="utf-8")
    for placeholder, value in (('SOCKET_PATH = ""', str(socket_path)), ('CLIENT = ""', client)):
        if source.count(placeholder) != 1:
            raise RuntimeError(f"client_stub.py must define {placeholder} exactly once")
        source = source.replace(placeholder, f"{placeholder.split(' =')[0]} = {value!r}")
    return f"#!{python} -I\n{source}"


def write_clients(directory: Path, socket_path: Path, python: str = sys.executable) -> dict[str, Path]:
    """Create ``directory`` (mode 0700) with the executables ``gh``, ``worker`` and ``heavy`` (0555)."""
    sources = {client: client_source(socket_path, client, python) for client in CLIENTS}
    directory.mkdir(mode=0o700)  # FileExistsError: an existing directory is never reused
    paths = {}
    for client, source in sources.items():
        path = directory / client
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(source)
        os.chmod(path, 0o555)
        paths[client] = path
    return paths


@dataclass(frozen=True)
class Endpoint:
    """Where sessions reach the runner during a run."""
    socket: Path
    directory: Path  # the socket's directory: the run directory or a private temporary one
    private: bool
    clients: Mapping[str, Path]

    def commands(self) -> dict[str, str]:
        """``gh_cmd``, ``worker_cmd`` and ``heavy_cmd`` for the session prompt: absolute paths, shell-quoted."""
        return {f"{client}_cmd": shlex.quote(str(path)) for client, path in self.clients.items()}

    def to_record(self) -> dict:
        return {"socket": str(self.socket), "private_directory": self.private,
                "clients": {client: str(path) for client, path in self.clients.items()}}


@contextmanager
def serve(run_dir: str | Path, registry: Registry, *, session_writable: Iterable[str | Path] = (),
          python: str = sys.executable, temp_root: str | Path | None = None,
          read_timeout: float = READ_TIMEOUT) -> Iterator[Endpoint]:
    """Serve ``registry`` on the run's socket for the body, with the thin clients beside it.

    ``session_writable`` names every path a session can write: the one-off repository, the
    exchange directory and the session's ``TMPDIR``. On exit, normal or by an exception, the
    server stops, its connections close, and the socket, the clients and a private directory go.
    """
    socket_path, private = place_socket(run_dir, session_writable, temp_root=temp_root)
    directory = socket_path.parent
    clients_dir = directory / CLIENTS_DIR
    existed = os.path.lexists(clients_dir)
    server = None
    try:
        if existed:
            raise UnsafeSocketDirectory(f"refusing the existing clients directory {clients_dir}")
        clients = write_clients(clients_dir, socket_path, python)
        server = Server(socket_path, registry, read_timeout=read_timeout)
        server.start()
        yield Endpoint(socket_path, directory, private, clients)
    finally:
        if server is not None:
            server.close()
        if private:
            shutil.rmtree(directory, ignore_errors=True)
        elif not existed:
            shutil.rmtree(clients_dir, ignore_errors=True)
