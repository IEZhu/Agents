"""The runner's CONNECT proxy for a Codex process (#344; docs/autodev-codex.md).

Seatbelt network rules accept only ``*`` or ``localhost`` as a host (the research: an IP or a
name fails to compile with "host must be * or localhost"), so the outer profile lets a Codex
process reach only ``localhost:<port>``, and this proxy, outside the sandbox, decides where that
port leads. It listens on 127.0.0.1 on an ephemeral port, started before the profile is written
(the port is a profile parameter and the ``HTTPS_PROXY`` value). It accepts only ``CONNECT
<host>:443`` for the hosts of the login mode (`codex_sandbox.allowed_hosts`) and relays the bytes
both ways without reading them (it never terminates TLS); every other target, and every request
that is not ``CONNECT``, gets ``403``. It records each requested target (host and port only, a
few hundred at most) and counts accepted connections, which the probes read. `ConnectProxy.close`
ends every tunnel; the run closes it when it ends (a context manager).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import re
import socket
import threading

from .codex_sandbox import PROXY_PORT_HTTPS

HEADER_LIMIT = 8192
HEADER_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0
RELAY_CHUNK = 65536
MAX_RECORDED = 500
FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
_TARGET = re.compile(r"(?P<host>[A-Za-z0-9.-]{1,253}):(?P<port>[0-9]{1,5})")
_METHOD = re.compile(r"[A-Z]{1,16}")


def _shown(text: str) -> str:
    """A requested target as a record may keep it: printable, at most 80 characters."""
    return re.sub(r"[^A-Za-z0-9.:_-]", "?", text)[:80]


class ConnectProxy:
    """A CONNECT-only proxy on 127.0.0.1 that tunnels to ``allowed`` hosts on port 443.

    ``connect`` opens the upstream connection (default `socket.create_connection`); the tests
    pass one that leads to a local listener. ``requests`` lists ``{"method", "target", "allowed"}``
    for each request, ``accepted`` counts connections and ``tunnels`` the tunnels opened."""

    def __init__(self, allowed: Iterable[str], *, port: int = PROXY_PORT_HTTPS,
                 connect: Callable[[tuple[str, int], float], socket.socket] | None = None):
        self.allowed = frozenset(host.lower() for host in allowed)
        for host in self.allowed:
            if not _HOST.fullmatch(host):
                raise ValueError(f"not a host name: {host!r}")
        self.port_allowed = port
        self._connect = connect or (lambda address, timeout: socket.create_connection(address, timeout=timeout))
        self.requests: list[dict] = []
        self.accepted = 0
        self.tunnels = 0
        self.dropped = 0
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._sockets: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self._server: socket.socket | None = None

    # --- lifecycle ---

    def start(self) -> ConnectProxy:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(32)
        server.settimeout(0.2)
        self._server = server
        thread = threading.Thread(target=self._accept_loop, name="codex-proxy", daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the proxy is not started")
        return self._server.getsockname()[1]

    def close(self) -> None:
        self._closing.set()
        if self._server is not None:
            self._server.close()
        with self._lock:
            open_sockets = list(self._sockets)
        for sock in open_sockets:
            _close(sock)
        for thread in list(self._threads):
            thread.join(timeout=2)

    def __enter__(self) -> ConnectProxy:
        return self.start() if self._server is None else self

    def __exit__(self, *exc) -> None:
        self.close()

    def targets(self, *, allowed: bool | None = None) -> list[str]:
        with self._lock:
            return [entry["target"] for entry in self.requests if allowed is None or entry["allowed"] is allowed]

    # --- serving ---

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.add(sock)

    def _untrack(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(sock)
        _close(sock)

    def _record(self, method: str, target: str, allowed: bool) -> None:
        with self._lock:
            if len(self.requests) < MAX_RECORDED:
                self.requests.append({"method": method, "target": target, "allowed": allowed})
            else:
                self.dropped += 1

    def _accept_loop(self) -> None:
        while not self._closing.is_set():
            try:
                client, _ = self._server.accept()  # type: ignore[union-attr]
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            with self._lock:
                self.accepted += 1
            self._track(client)
            thread = threading.Thread(target=self._serve, args=(client,), daemon=True)
            thread.start()
            with self._lock:
                self._threads = [item for item in self._threads if item.is_alive()] + [thread]

    def _serve(self, client: socket.socket) -> None:
        try:
            header, rest = _read_header(client)
        except OSError:
            self._untrack(client)
            return
        if header is None:
            self._untrack(client)
            return
        line = header.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split(" ")
        method = parts[0] if parts and _METHOD.fullmatch(parts[0]) else "?"
        target = parts[1] if len(parts) == 3 else ""
        match = _TARGET.fullmatch(target)
        if method != "CONNECT" or match is None:
            self._record(method, _shown(target), False)
            _send(client, FORBIDDEN)
            self._untrack(client)
            return
        host, port = match["host"].lower().rstrip("."), int(match["port"])
        allowed = host in self.allowed and port == self.port_allowed
        self._record(method, f"{_shown(host)}:{port}", allowed)
        if not allowed or self._closing.is_set():
            _send(client, FORBIDDEN)
            self._untrack(client)
            return
        try:
            upstream = self._connect((host, port), CONNECT_TIMEOUT)
        except OSError:
            _send(client, BAD_GATEWAY)
            self._untrack(client)
            return
        self._track(upstream)
        with self._lock:
            self.tunnels += 1
        try:
            client.sendall(ESTABLISHED)
            if rest:
                upstream.sendall(rest)
        except OSError:
            self._untrack(upstream)
            self._untrack(client)
            return
        client.settimeout(None)
        upstream.settimeout(None)
        back = threading.Thread(target=_relay, args=(upstream, client), daemon=True)
        back.start()
        _relay(client, upstream)
        back.join(timeout=5)
        self._untrack(upstream)
        self._untrack(client)


def _read_header(client: socket.socket) -> tuple[bytes | None, bytes]:
    """The request head (up to the blank line) and any bytes after it; None for a client that
    closed or sent more than `HEADER_LIMIT` bytes without one."""
    client.settimeout(HEADER_TIMEOUT)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(4096)
        if not chunk:
            return None, b""
        data += chunk
        if len(data) > HEADER_LIMIT:
            _send(client, FORBIDDEN)
            return None, b""
    header, _, rest = data.partition(b"\r\n\r\n")
    return header, rest


def _relay(source: socket.socket, target: socket.socket) -> None:
    """Copy bytes from ``source`` to ``target`` until either side closes (never inspected)."""
    try:
        while True:
            chunk = source.recv(RELAY_CHUNK)
            if not chunk:
                break
            target.sendall(chunk)
    except OSError:
        pass
    try:
        target.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _send(sock: socket.socket, data: bytes) -> None:
    try:
        sock.sendall(data)
    except OSError:
        pass


def _close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
