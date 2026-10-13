"""The runner socket (design 5.6, 10.1): fixed request types for sandboxed sessions, never a command passthrough.

A run opens the socket with `serve` around its stage sessions; a session may only connect. The
socket is ``{run_dir}/control.sock`` when that absolute path fits the platform's ``AF_UNIX`` limit
(`socket_limit`), otherwise ``control.sock`` in a private directory that ``tempfile.mkdtemp``
creates under the runner's own temporary directory. Either directory must belong to this user, be
no symlink, be writable by no group or other user, and lie neither inside nor around a path the
session can write, compared by path and by file identity (`check_directory`); anything else raises
before the socket is bound. An existing file at the socket path is refused, never removed.

The protocol is newline-delimited JSON. A request is ``{"type": str, "args": object}`` of at most
``MAX_REQUEST`` bytes; the answer is ``{"ok": true, "result": ...}`` or ``{"ok": false, "error": str}``.
A whole request must arrive within ``READ_TIMEOUT`` seconds, counted from the first byte; a
connection that holds nothing must also start it within that time.
Handlers register under fixed types ``gh.<operation>``, ``worker.<operation>`` and
``heavy.<operation>`` with an argument specification (`Field`) and a deadline; an unknown type, a
malformed request, an unknown argument or a wrong type is refused before a handler runs. The
registry has no generic type, refuses argument fields named like a command (``command``, ``argv``,
``shell``…), and a string argument must match a narrow pattern unless its field declares free
text, which a handler treats as data only; no string may carry a control character or a lone
surrogate. A handler gets a `RequestContext` with its deadline, the peer's PID and a closing
check. A type registered ``session_bound`` answers only a process of the current stage session
(the runner names its root PID), checked on every request. The heavy-process lease
(`src.schedules.heavy`) registers here; the GitHub broker (#342) and the worker launcher (#345) will.

`serve` also writes the thin clients ``gh``, ``worker`` and ``heavy`` (`src.schedules.client_stub`)
into a ``clients`` directory beside the socket. When the run ends, normally, by an exception, or
by SIGTERM or SIGHUP (turned into `RunStopped` while it serves), it stops serving, closes open
connections (which releases what they hold, and no handler can keep anything afterwards) and
removes the socket, the clients and the private directory. A SIGKILL leaves them behind:
`remove_leftovers` removes them from outside once the run's processes are gone. POSIX only: the
socket is an ``AF_UNIX`` socket.
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
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping

from . import client_stub  # imported, not only read, so the runner's drift check covers it

SOCKET_NAME = "control.sock"
CLIENTS_DIR = "clients"
CLIENTS = ("gh", "worker", "heavy")  # also the services: a request type is "<service>.<operation>"
STUB = Path(client_stub.__file__)
PRIVATE_PREFIX = "autodev-socket-"
MAX_REQUEST = 64 * 1024  # bytes of one request line, the newline excluded
READ_TIMEOUT = 10.0  # seconds for a whole request; an idle wait is unbounded while the connection holds a resource
MAX_CONNECTIONS = 16
CLOSE_WAIT = 5.0  # seconds `close` waits for all connection threads together
DEFAULT_DEADLINE = 5.0  # seconds a handler has unless its type registers another deadline
MAX_DEADLINE = 120.0
TAKE_RETRY = 0.05  # seconds between attempts of `Connection.take`
AF_UNIX_LIMITS = {"darwin": 103, "linux": 107}  # sizeof(sun_path) minus the terminating NUL
DEFAULT_AF_UNIX_LIMIT = 103
_SOL_LOCAL, _LOCAL_PEERPID = 0, 0x002  # <sys/un.h> on macOS; the socket module names neither
PS = "/bin/ps"
PS_TIMEOUT = 5.0  # seconds
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)  # SIGINT already raises KeyboardInterrupt

TYPE = re.compile(r"(?:gh|worker|heavy)\.[a-z][a-z0-9_-]{0,63}")
FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
FORBIDDEN_OPERATIONS = frozenset({"command", "eval", "exec", "passthrough", "raw", "shell", "spawn", "system"})
FORBIDDEN_FIELDS = frozenset({"args", "argv", "cmd", "command", "exec", "executable", "program", "script", "shell"})
# No space, slash, quote or shell syntax, and none of ':', '@', '=', '+' or '..', which git reads
# as a refspec, a reflog, an option value or a range.
TOKEN = re.compile(r"(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
# C0 controls but tab and newline, DEL, C1 controls, and lone surrogates: refused in every string.
FORBIDDEN_CHARACTERS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
TEXT_MAX_LENGTH = 32 * 1024  # characters of a free-text argument unless its field sets max_length
INT_MINIMUM, INT_MAXIMUM = 1, 1_000_000  # an integer argument's range unless its field sets one
FIELD_KINDS = (str, int, bool)


class UnsafeSocketDirectory(RuntimeError):
    """The socket's directory, or the socket path, fails a placement check."""


class RequestRefused(Exception):
    """A refused request; the message goes to the client as ``error``."""


class RunStopped(SystemExit):
    """The process serving the socket received SIGTERM or SIGHUP; raised inside `serve`, so the
    socket, the clients and the private directory are removed before the process ends."""

    def __init__(self, number: int):
        super().__init__(128 + number)
        self.signal = number


# --- requests and the registry ---------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Field:
    """One argument of a request type: its JSON type and what it may contain.

    A string must match ``pattern`` in full (default `TOKEN`); ``text=True`` declares free text,
    such as a comment body, that the handler only stores or sends as data, at most ``max_length``
    characters (default `TEXT_MAX_LENGTH`). No string may contain `FORBIDDEN_CHARACTERS`. An
    integer lies between ``minimum`` and ``maximum`` (default `INT_MINIMUM` to `INT_MAXIMUM`).
    """
    kind: type
    required: bool = True
    pattern: re.Pattern | None = None
    text: bool = False
    max_length: int | None = None
    minimum: int | None = None
    maximum: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in FIELD_KINDS:
            raise ValueError(f"an argument is one of {[kind.__name__ for kind in FIELD_KINDS]}")
        if self.kind is not str and (self.pattern or self.text or self.max_length is not None):
            raise ValueError("only a string argument takes a pattern, text or max_length")
        if self.kind is not int and (self.minimum is not None or self.maximum is not None):
            raise ValueError("only an integer argument takes a minimum or maximum")
        if self.text and self.pattern:
            raise ValueError("free text takes no pattern")
        if self.max_length is not None and (not _is_int(self.max_length) or self.max_length < 1):
            raise ValueError("max_length must be a positive integer")
        if self.kind is int:
            low, high = self.bounds
            if not (_is_int(low) and _is_int(high)) or low > high:
                raise ValueError("minimum and maximum must be integers, the minimum not above the maximum")

    @property
    def bounds(self) -> tuple[int, int]:
        return (INT_MINIMUM if self.minimum is None else self.minimum,
                INT_MAXIMUM if self.maximum is None else self.maximum)

    @property
    def length_limit(self) -> int | None:
        return self.max_length if self.max_length is not None else TEXT_MAX_LENGTH if self.text else None

    def problem(self, name: str, value: Any) -> str | None:
        """Why ``value`` is not acceptable, or None."""
        accepted = _is_int(value) if self.kind is int else isinstance(value, self.kind)
        if not accepted:
            return f"argument {name} must be {self.kind.__name__}"
        if self.kind is int:
            low, high = self.bounds
            if not low <= value <= high:
                return f"argument {name} must be between {low} and {high}"
        if self.kind is str:
            limit = self.length_limit
            if limit is not None and len(value) > limit:
                return f"argument {name} is longer than {limit} characters"
            if FORBIDDEN_CHARACTERS.search(value):
                return f"argument {name} contains a control character or a lone surrogate"
            if not self.text and not (self.pattern or TOKEN).fullmatch(value):
                return f"argument {name} contains characters it may not contain"
        return None


def _never() -> bool:
    return False


@dataclass(frozen=True)
class RequestContext:
    """What a handler gets besides its checked arguments.

    ``deadline`` is the `time.monotonic` time by which the handler should have answered: it bounds
    its own subprocess and network timeouts by `remaining`. ``closing()`` turns true once the
    server closes; a handler checks it right before any external write and aborts without writing
    then, because nothing waits for it any more. ``peer_pid`` is the client's PID from the kernel
    (None when unknown), for logging; ``session_root`` is the root PID of the current stage
    session as the runner names it (None when none runs or the runner names none).
    """
    connection: Connection
    peer_pid: int | None
    deadline: float
    session_root: int | None = None
    closing: Callable[[], bool] = _never

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


Handler = Callable[[dict, RequestContext], Any]
Lineage = Callable[[int], "set[int] | None"]


@dataclass(frozen=True)
class _Entry:
    function: Handler
    spec: Mapping[str, Field]
    deadline: float
    session_bound: bool


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
    """Request types and their handlers; a handler gets the checked ``args`` and a `RequestContext`."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def register(self, request_type: str, function: Handler, spec: Mapping[str, Field] | None = None, *,
                 deadline: float = DEFAULT_DEADLINE, session_bound: bool = False) -> None:
        """Register ``function`` for ``request_type``. ``deadline`` is the seconds its handler has
        (it reads them from its context); ``session_bound`` answers only the current stage session."""
        if not TYPE.fullmatch(request_type) or request_type.partition(".")[2] in FORBIDDEN_OPERATIONS:
            raise ValueError(f"not an allowed request type: {request_type!r}")
        if request_type in self._entries:
            raise ValueError(f"request type already registered: {request_type}")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not 0 < deadline <= MAX_DEADLINE:
            raise ValueError(f"a deadline is more than 0 and at most {MAX_DEADLINE:g} seconds: {deadline!r}")
        spec = dict(spec or {})
        for name, field in spec.items():
            if not FIELD_NAME.fullmatch(name) or name in FORBIDDEN_FIELDS:
                raise ValueError(f"not an allowed argument name: {name!r}")
            if not isinstance(field, Field):
                raise TypeError(f"argument {name} needs a Field")
        self._entries[request_type] = _Entry(function, spec, float(deadline), bool(session_bound))

    def types(self) -> list[str]:
        return sorted(self._entries)

    def handle(self, line: bytes, connection: Connection, *, closing: Callable[[], bool] = _never,
               session_root: Callable[[], int | None] | None = None, lineage: Lineage | None = None) -> dict:
        """The response to one request line. ``session_root`` names the current session's root
        PID; ``lineage(pid)`` gives a PID with its ancestors (default `peer_lineage`)."""
        try:
            request_type, args = parse_request(line)
            entry = self._entries.get(request_type)
            if entry is None:
                raise RequestRefused(f"unknown request type: {request_type[:80]!r}")
            self._check(entry.spec, args)
            root = session_root() if session_root is not None else None
            if entry.session_bound:
                _check_session(connection.peer_pid, root, lineage or peer_lineage)
            context = RequestContext(connection, connection.peer_pid, time.monotonic() + entry.deadline, root, closing)
            return {"ok": True, "result": entry.function(args, context)}
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


def _check_session(peer: int | None, root: int | None, lineage: Lineage) -> None:
    """Refuse unless process ``peer`` descends from (or is) the session root ``root``."""
    if root is None:
        raise RequestRefused("refused: no stage session is running")
    if peer is None:
        raise RequestRefused("refused: the caller's process is unknown")
    chain = lineage(peer)
    if chain is None:
        raise RequestRefused("refused: the process table cannot be read")
    if root not in chain:
        raise RequestRefused("refused: the caller is not a process of the current stage session")


class Connection:
    """One client connection. A handler keeps a resource on it (`take` or `keep`) until the client
    releases it or disconnects, or the server closes; while it holds one, an idle wait has no
    timeout. Once the connection is closed, `take` opens nothing and `keep` refuses and releases
    what it was given."""

    def __init__(self, peer_pid: int | None) -> None:
        self.peer_pid = peer_pid  # from the kernel, not from the client; None when unknown
        self._resources: dict[str, ExitStack] = {}
        self._lock = threading.Lock()
        self._closed = False

    @property
    def holding(self) -> bool:
        with self._lock:
            return bool(self._resources)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def holds(self, name: str) -> bool:
        with self._lock:
            return name in self._resources

    def take(self, name: str, opener: Callable[[], ExitStack | None], *, wait: float = 0.0) -> bool:
        """Open a resource with ``opener`` and keep it as ``name``, atomically with `close`: once
        `close` has run, nothing is opened, and what was opened before is released by it.
        ``opener`` must not block; it returns the resource's stack, or None while the resource is
        busy, and is retried for ``wait`` seconds. False when it stayed busy; `RequestRefused` when
        the connection is closed or already holds ``name``."""
        deadline = time.monotonic() + wait
        while True:
            with self._lock:
                if self._closed:
                    raise RequestRefused("the runner socket is closing")
                if name in self._resources:
                    raise RequestRefused(f"this connection already holds {name}")
                stack = opener()
                if stack is not None:
                    self._resources[name] = stack
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(TAKE_RETRY)

    def keep(self, name: str, stack: ExitStack) -> None:
        """Keep the already opened ``stack`` as ``name``; refused, and ``stack`` closed, once the
        connection is closed or when it already holds ``name``."""
        with self._lock:
            closed, duplicate = self._closed, name in self._resources
            if not (closed or duplicate):
                self._resources[name] = stack
                return
        stack.close()
        raise RequestRefused("the runner socket is closing" if closed else f"this connection already holds {name}")

    def release(self, name: str) -> bool:
        with self._lock:
            stack = self._resources.pop(name, None)
        if stack is None:
            return False
        stack.close()
        return True

    def close(self) -> None:
        """Release everything held; from now on `keep` refuses."""
        with self._lock:
            self._closed = True
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


# --- process lineage -------------------------------------------------------------------------------

def process_table(ps: str = PS, *, timeout: float = PS_TIMEOUT) -> dict[int, tuple[int, str]] | None:
    """``{pid: (parent pid, command line)}`` of every process; None when ``ps`` fails, times out
    or lists nothing (it lists at least itself)."""
    try:
        done = subprocess.run([ps, "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True,
                              errors="replace", stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    table = {}
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) == 3 else "")
    return table or None


def lineage(table: Mapping[int, tuple[int, str]], pid: int, *, stop: int | None = None) -> set[int]:
    """``pid`` and its ancestors in ``table``. With ``stop``, only up to and including that
    ancestor, and only ``pid`` itself when ``stop`` is not among its ancestors."""
    found: set[int] = set()
    current = pid
    while current > 0 and current not in found:
        found.add(current)
        if current == stop:
            return found
        if current not in table:
            break
        current = table[current][0]
    if stop is not None:
        return {pid} if pid > 0 else set()
    return found


def peer_lineage(pid: int, ps: str = PS) -> set[int] | None:
    """``pid`` and its ancestors from a fresh process table; None when ``ps`` fails."""
    table = process_table(ps)
    return None if table is None else lineage(table, pid)


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
    """A thread per connection behind one accept thread; `close` ends them all.

    ``session_root()`` names the root PID of the current stage session for session-bound types;
    ``lineage(pid)`` gives a PID with its ancestors (default `peer_lineage`)."""

    def __init__(self, path: Path, registry: Registry, *, read_timeout: float = READ_TIMEOUT,
                 max_connections: int = MAX_CONNECTIONS, close_wait: float = CLOSE_WAIT,
                 session_root: Callable[[], int | None] | None = None, lineage: Lineage | None = None) -> None:
        self.path = Path(path)
        self.registry = registry
        self.read_timeout = read_timeout
        self.max_connections = max_connections
        self.close_wait = close_wait
        self.session_root = session_root
        self.lineage = lineage or peer_lineage
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._active: dict[socket.socket, tuple[Connection, threading.Thread]] = {}
        self._listener: socket.socket | None = None
        self._inode: int | None = None
        self._wake_read, self._wake_write = socket.socketpair()
        self._accepting: threading.Thread | None = None

    @property
    def closing(self) -> bool:
        return self._closing.is_set()

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
                sock, _ = self._listener.accept()  # `_read_line` sets its timeout
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
                line, buffer = self._read_line(sock, buffer, connection.holding)
                if line is None or self._closing.is_set():
                    return
                response = self.registry.handle(line, connection, closing=self._closing.is_set,
                                                session_root=self.session_root, lineage=self.lineage)
                sock.settimeout(self.read_timeout)  # a client that reads no answer cannot hold the thread
                if not _answer(sock, response):
                    return
        finally:
            connection.close()
            with self._lock:
                self._active.pop(sock, None)
            sock.close()

    def _read_line(self, sock: socket.socket, buffer: bytes, holding: bool) -> tuple[bytes | None, bytes]:
        """The next request line, or None at the end of the connection (EOF, timeout, too long).

        The whole request must arrive within ``read_timeout`` seconds: counted from now, or, while
        the connection holds a resource and has nothing buffered, from the request's first byte."""
        deadline = None if holding and not buffer else time.monotonic() + self.read_timeout
        while b"\n" not in buffer:
            if len(buffer) > MAX_REQUEST:
                break
            timeout = None if deadline is None else deadline - time.monotonic()
            if timeout is not None and timeout <= 0:
                return self._timed_out(sock)
            try:
                sock.settimeout(timeout)
                chunk = sock.recv(MAX_REQUEST + 1)
            except socket.timeout:
                return self._timed_out(sock)
            except OSError:
                return None, b""
            if not chunk:
                return None, b""
            buffer += chunk
            if deadline is None:
                deadline = time.monotonic() + self.read_timeout
        line, _, rest = buffer.partition(b"\n")
        if len(line) > MAX_REQUEST:
            _answer(sock, {"ok": False, "error": f"request too long: the limit is {MAX_REQUEST} bytes"})
            return None, b""
        return line, rest

    def _timed_out(self, sock: socket.socket) -> tuple[None, bytes]:
        sock.settimeout(self.read_timeout)
        _answer(sock, {"ok": False, "error": f"no complete request within {self.read_timeout:g} seconds"})
        return None, b""

    def close(self) -> None:
        """Stop accepting, end every connection (which releases what it holds and refuses any later
        `Connection.keep`) and remove the socket. Waits ``close_wait`` seconds in all for the
        connection threads; a handler still running afterwards sees `RequestContext.closing`."""
        deadline = time.monotonic() + self.close_wait
        self._closing.set()
        try:
            self._wake_write.send(b"x")
        except OSError:
            pass
        if self._accepting is not None:
            self._accepting.join(max(0.0, deadline - time.monotonic()))
        if self._listener is not None:
            self._listener.close()
        self._wake_read.close()
        self._wake_write.close()
        self._remove_socket()
        with self._lock:
            active = list(self._active.items())
        for sock, (connection, _) in active:
            connection.close()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for _, (_, thread) in active:
            thread.join(max(0.0, deadline - time.monotonic()))

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


def _identity(path: Path) -> tuple[int, int]:
    found = os.stat(path)
    return found.st_dev, found.st_ino


def _chain(path: Path) -> set[tuple[int, int]]:
    """The file identities of the existing ``path`` and all its ancestors."""
    real = Path(os.path.realpath(path))
    return {_identity(entry) for entry in (real, *real.parents)}


def _nearest_existing(path: Path) -> tuple[Path, bool]:
    """``path`` or its nearest existing ancestor, and whether that is ``path`` itself."""
    current = Path(os.path.realpath(path))
    exact = True
    while True:
        try:
            os.stat(current)
            return current, exact
        except (FileNotFoundError, NotADirectoryError):
            if current.parent == current:
                raise
            current, exact = current.parent, False


def check_directory(directory: Path, session_writable: Iterable[str | Path] = ()) -> None:
    """Raise `UnsafeSocketDirectory` unless ``directory`` suits the socket: this user's own, no
    symlink, writable by no group or other user, and neither inside nor around a session-writable path.

    Inside and around are decided twice: by the resolved path strings and by file identity
    (``st_dev`` and ``st_ino``), which also catches a case-variant spelling on a case-insensitive
    volume and a firmlink alias such as ``/System/Volumes/Data``. A session-writable path that
    does not exist yet counts by its nearest existing ancestor for the "around" case."""
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
    try:
        own, around = _identity(real), _chain(real)
    except OSError as error:
        raise UnsafeSocketDirectory(f"the socket directory {directory} cannot be checked: {error}") from None
    for path in _absolute(session_writable):
        writable = Path(os.path.realpath(path))
        if real == writable or writable in real.parents:
            raise UnsafeSocketDirectory(f"the socket directory {directory} lies inside the session-writable {path}")
        if real in writable.parents:
            raise UnsafeSocketDirectory(f"the socket directory {directory} contains the session-writable {path}")
        try:
            existing, exact = _nearest_existing(writable)
            if exact and _identity(existing) in around:
                raise UnsafeSocketDirectory(
                    f"the socket directory {directory} lies inside the session-writable {path} (same file)")
            if own in _chain(existing):
                raise UnsafeSocketDirectory(
                    f"the socket directory {directory} contains the session-writable {path} (same file)")
        except OSError as error:
            raise UnsafeSocketDirectory(f"the session-writable {path} cannot be checked: {error}") from None


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


def client_interpreter() -> str:
    """The thin clients' interpreter: the runner's base interpreter with symlinks resolved, so a
    session needs to read no virtual environment; `sys.executable` when there is no base one."""
    base = getattr(sys, "_base_executable", None)
    if base:
        real = os.path.realpath(base)
        if os.path.isfile(real) and os.access(real, os.X_OK):
            return real
    return sys.executable


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


def write_clients(directory: Path, socket_path: Path, python: str | None = None) -> dict[str, Path]:
    """Create ``directory`` (mode 0700) with the executables ``gh``, ``worker`` and ``heavy`` (0555);
    ``python`` is their interpreter (default `client_interpreter`)."""
    python = python or client_interpreter()
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
        """For the run record (key ``endpoint``), so `remove_leftovers` can clean up after a SIGKILL."""
        return {"socket": str(self.socket), "private_directory": self.private,
                "clients": {client: str(path) for client, path in self.clients.items()}}


# --- leftovers of a killed run ---------------------------------------------------------------------

def _own_entry(path: Path, kind: Callable[[int], bool]) -> os.stat_result | None:
    """``path``'s lstat when it is this user's own entry of ``kind``; None when it is gone.
    ``ValueError`` names what is wrong otherwise."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return None
    if not kind(found.st_mode):
        raise ValueError("not the kind of entry serve creates")
    if found.st_uid != os.getuid():
        raise ValueError("it belongs to another user")
    return found


def _remove_clients(clients_dir: Path, removed: list[str]) -> None:
    if _own_entry(clients_dir, stat.S_ISDIR) is None:
        return
    entries = os.listdir(clients_dir)
    if unexpected := sorted(set(entries) - set(CLIENTS)):
        raise ValueError(f"it holds other entries: {unexpected[:5]}")
    for name in entries:
        _own_entry(clients_dir / name, stat.S_ISREG)
    for name in entries:
        os.unlink(clients_dir / name)
        removed.append(str(clients_dir / name))
    os.rmdir(clients_dir)
    removed.append(str(clients_dir))


def remove_leftovers(record: Mapping, *, run_directory: str | Path | None = None) -> dict:
    """Remove what `serve` left behind when its process was killed (SIGKILL), as the run record's
    ``endpoint`` (`Endpoint.to_record`) names it. For the runner, once the run's processes are gone.

    Removed are only this user's own entries of the kinds `serve` creates: a socket at the socket
    path, a ``clients`` directory beside it holding nothing but the clients, and, for a private
    directory, that directory when it is named ``autodev-socket-*``, is no symlink, has mode 0700
    and holds nothing but those two. For a socket in the run directory, ``run_directory`` (the
    runner's own idea of it, not the record's) must resolve to the same directory, so a record
    cannot point the cleanup elsewhere through a symlinked ancestor. Anything else is kept. Returns ``{"removed": [paths],
    "kept": ["<path>: <reason>"]}``; both are empty when nothing was left.
    """
    removed: list[str] = []
    kept: list[str] = []
    socket_text, private = (record.get("socket"), record.get("private_directory")) if isinstance(record, Mapping) \
        else (None, None)
    if not isinstance(socket_text, str) or not isinstance(private, bool):
        return {"removed": removed, "kept": ["the record names no endpoint"]}
    socket_path = Path(socket_text)
    if not socket_path.is_absolute() or socket_path.name != SOCKET_NAME or ".." in socket_path.parts:
        return {"removed": removed, "kept": [f"{socket_text}: not a socket path serve uses"]}
    directory = socket_path.parent
    if not private and run_directory is not None and os.path.realpath(directory) != os.path.realpath(run_directory):
        return {"removed": removed, "kept": [f"{directory}: not this run's directory"]}
    clients_dir = directory / CLIENTS_DIR
    clients = record.get("clients") or {}
    if not isinstance(clients, Mapping) or any(
            name not in CLIENTS or path != str(clients_dir / name) for name, path in clients.items()):
        return {"removed": removed, "kept": [f"{directory}: the record's clients do not lie beside its socket"]}
    if private:
        try:
            found = _own_entry(directory, stat.S_ISDIR)
            if found is None:
                return {"removed": removed, "kept": kept}
            if not directory.name.startswith(PRIVATE_PREFIX) or stat.S_IMODE(found.st_mode) & 0o077:
                raise ValueError("not a private directory serve creates")
            if unexpected := sorted(set(os.listdir(directory)) - {SOCKET_NAME, CLIENTS_DIR}):
                raise ValueError(f"it holds other entries: {unexpected[:5]}")
        except (OSError, ValueError) as problem:
            return {"removed": removed, "kept": [f"{directory}: {problem}"]}
    for path, remove in ((socket_path, _remove_socket_file), (clients_dir, _remove_clients)):
        try:
            remove(path, removed)
        except (OSError, ValueError) as problem:
            kept.append(f"{path}: {problem}")
    if private and not kept:
        try:
            os.rmdir(directory)
            removed.append(str(directory))
        except FileNotFoundError:
            pass
        except OSError as problem:
            kept.append(f"{directory}: {problem}")
    return {"removed": removed, "kept": kept}


def _remove_socket_file(path: Path, removed: list[str]) -> None:
    if _own_entry(path, stat.S_ISSOCK) is not None:
        os.unlink(path)
        removed.append(str(path))


# --- serving a run ---------------------------------------------------------------------------------

def _install_stop_handlers(state: dict) -> dict | None:
    """While ``state["armed"]``, the first SIGTERM or SIGHUP raises `RunStopped`; otherwise, and
    for later signals, it is only recorded in ``state["stopped"]``. None outside the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return None

    def stop(number, frame):
        if state["stopped"] is None:
            state["stopped"] = number
            if state["armed"]:
                state["raised"] = True
                raise RunStopped(number)
    return {number: signal.signal(number, stop) for number in STOP_SIGNALS}


def _restore_handlers(previous: dict | None) -> None:
    for number, handler in (previous or {}).items():
        signal.signal(number, handler if handler is not None else signal.SIG_DFL)


@contextmanager
def serve(run_dir: str | Path, registry: Registry, *, session_writable: Iterable[str | Path] = (),
          python: str | None = None, temp_root: str | Path | None = None,
          read_timeout: float = READ_TIMEOUT, close_wait: float = CLOSE_WAIT,
          session_root: Callable[[], int | None] | None = None) -> Iterator[Endpoint]:
    """Serve ``registry`` on the run's socket for the body, with the thin clients beside it.

    ``session_writable`` names every path a session can write: the one-off repository, the
    exchange directory and the session's ``TMPDIR``; the run sequence passes the sandbox's real
    writable set. ``session_root()`` names the root PID of the current stage session, or None.
    ``python`` is the clients' interpreter (default `client_interpreter`). On exit, normal or by an
    exception, the server stops, its connections close, and the socket, the clients and a
    private directory go. In the main thread, SIGTERM and SIGHUP raise `RunStopped` inside the
    body, so they end the run the same way; one that arrives during that cleanup waits for it and
    is raised afterwards. The previous handlers are restored on exit.
    """
    stop = {"armed": True, "stopped": None, "raised": False}
    opened: dict = {}  # what `_close_endpoint` ends, filled as it is created
    previous = _install_stop_handlers(stop)
    try:
        try:
            socket_path, private = place_socket(run_dir, session_writable, temp_root=temp_root)
            directory = socket_path.parent
            if private:
                opened["private"] = directory
            clients_dir = directory / CLIENTS_DIR
            if os.path.lexists(clients_dir):
                raise UnsafeSocketDirectory(f"refusing the existing clients directory {clients_dir}")
            opened["clients"] = clients_dir
            clients = write_clients(clients_dir, socket_path, python)
            opened["server"] = server = Server(socket_path, registry, read_timeout=read_timeout,
                                               close_wait=close_wait, session_root=session_root)
            server.start()
            yield Endpoint(socket_path, directory, private, clients)
        finally:
            stop["armed"] = False
            _close_endpoint(opened)
    finally:
        # A stop signal raised before the line that disarms it above skips that cleanup; it can be
        # raised only once, so this second call completes it. Otherwise there is nothing left.
        stop["armed"] = False
        try:
            _close_endpoint(opened)
        finally:
            _restore_handlers(previous)
        if stop["stopped"] is not None and not stop["raised"]:
            raise RunStopped(stop["stopped"])  # it came during the cleanup, which it waited for


def _close_endpoint(opened: dict) -> None:
    """Close the server, then remove the private directory or the clients; each only once."""
    try:
        if (server := opened.pop("server", None)) is not None:
            server.close()
    finally:
        for key in ("private", "clients"):
            if (path := opened.pop(key, None)) is not None:
                shutil.rmtree(path, ignore_errors=True)
