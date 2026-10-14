"""The runner's CONNECT proxy for a Codex process (#344; docs/autodev-codex.md).

Seatbelt network rules accept only ``*`` or ``localhost`` as a host (the research: an IP or a
name fails to compile with "host must be * or localhost"), so the outer profile lets a Codex
process reach only ``localhost:<port>``, and this proxy, outside the sandbox, decides where that
port leads. It listens on 127.0.0.1 on an ephemeral port, started before the profile is written
(the port is a profile parameter and the ``HTTPS_PROXY`` value). It accepts only ``CONNECT
<host>:443`` for ``api.openai.com`` (`codex_sandbox.allowed_hosts`) and relays the bytes both
ways without reading them (it never terminates TLS); every other target, and every request that
is not ``CONNECT``, gets ``403``. It records each requested target (host and port only, a few
hundred at most) and counts accepted connections, which the probes read.

Any local process can reach the listener, and a tunnel to an allowed host is a channel the proxy
cannot read, so it is bounded: at most `MAX_CLIENTS` connections and `MAX_TUNNELS` tunnels at a
time (``503`` beyond either), the whole request head must arrive within `HEADER_TIMEOUT`
seconds, and a tunnel ends after `IDLE_TIMEOUT` seconds without a byte in
either direction, after `TUNNEL_SECONDS` seconds or after `TUNNEL_BYTES` bytes (``ended`` counts
why). `ConnectProxy.close` ends every tunnel; the run closes it when it ends (a context manager),
and `codex_probes.start` also closes it when the codex process exits (`ConnectProxy.close_when`).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import re
import selectors
import socket
import threading
import time
from typing import Any

from .codex_sandbox import PROXY_PORT_HTTPS

HEADER_LIMIT = 8192
HEADER_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0
RELAY_CHUNK = 65536
MAX_RECORDED = 500
# Bounds on what the listener serves (any local process can reach it). codex keeps a few
# connections to the API open at a time; the limits are well above that and far below what would
# exhaust the runner's threads or descriptors.
MAX_CLIENTS = 16
MAX_TUNNELS = 8
IDLE_TIMEOUT = 300.0
TUNNEL_SECONDS = 3600.0
TUNNEL_BYTES = 256 * 1024 * 1024
POLL_INTERVAL = 0.25
FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BUSY = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
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
    for each request (plus ``"reason"`` when a limit refused it), ``accepted`` counts connections,
    ``tunnels`` the tunnels opened, ``busy`` the connections and tunnels refused by a limit and
    ``ended`` the tunnels a limit ended (``idle``, ``time``, ``bytes``). The limits default to the
    module's constants."""

    def __init__(self, allowed: Iterable[str], *, port: int = PROXY_PORT_HTTPS,
                 connect: Callable[[tuple[str, int], float], socket.socket] | None = None,
                 max_clients: int = MAX_CLIENTS, max_tunnels: int = MAX_TUNNELS, idle_timeout: float = IDLE_TIMEOUT,
                 tunnel_seconds: float = TUNNEL_SECONDS, tunnel_bytes: int = TUNNEL_BYTES,
                 header_timeout: float = HEADER_TIMEOUT):
        self.allowed = frozenset(host.lower() for host in allowed)
        for host in self.allowed:
            if not _HOST.fullmatch(host):
                raise ValueError(f"not a host name: {host!r}")
        for name, value in (("max_clients", max_clients), ("max_tunnels", max_tunnels), ("idle_timeout", idle_timeout),
                            ("tunnel_seconds", tunnel_seconds), ("tunnel_bytes", tunnel_bytes),
                            ("header_timeout", header_timeout)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        self.port_allowed = port
        self._connect = connect or (lambda address, timeout: socket.create_connection(address, timeout=timeout))
        self.max_clients, self.max_tunnels = int(max_clients), int(max_tunnels)
        self.idle_timeout, self.tunnel_seconds, self.tunnel_bytes = float(idle_timeout), float(tunnel_seconds), int(tunnel_bytes)
        self.header_timeout = float(header_timeout)
        self.requests: list[dict] = []
        self.accepted = 0
        self.tunnels = 0
        self.dropped = 0
        self.busy = 0
        self.ended = {"idle": 0, "time": 0, "bytes": 0}
        self._clients = 0
        self._open_tunnels = 0
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._sockets: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self._server: socket.socket | None = None
        self._port = 0

    # --- lifecycle ---

    def start(self) -> ConnectProxy:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(32)
        server.settimeout(0.2)
        self._server = server
        self._port = server.getsockname()[1]
        thread = threading.Thread(target=self._accept_loop, name="codex-proxy", daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the proxy is not started")
        return self._port

    def close(self) -> None:
        self._closing.set()
        if self._server is not None:
            self._server.close()
        with self._lock:
            open_sockets = list(self._sockets)
        for sock in open_sockets:
            _close(sock)
        current = threading.current_thread()
        for thread in list(self._threads):
            if thread is not current:
                thread.join(timeout=2)

    @property
    def closed(self) -> bool:
        return self._closing.is_set()

    def close_when(self, process: Any) -> threading.Thread:
        """Close the proxy once ``process`` (a `subprocess.Popen`) has exited; polls without waiting
        on it, so the caller may wait on the process itself."""
        def watch() -> None:
            while not self._closing.is_set() and process.poll() is None:
                time.sleep(POLL_INTERVAL)
            self.close()

        thread = threading.Thread(target=watch, name="codex-proxy-watch", daemon=True)
        thread.start()
        return thread

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

    def _record(self, method: str, target: str, allowed: bool, *, reason: str | None = None) -> None:
        with self._lock:
            if len(self.requests) < MAX_RECORDED:
                entry = {"method": method, "target": target, "allowed": allowed}
                if reason is not None:
                    entry["reason"] = reason
                self.requests.append(entry)
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
                full = self._clients >= self.max_clients
                if full:
                    self.busy += 1
                else:
                    self._clients += 1
            if full:
                client.settimeout(1.0)
                _send(client, BUSY)
                _close(client)
                continue
            self._track(client)
            thread = threading.Thread(target=self._serve, args=(client,), daemon=True)
            thread.start()
            with self._lock:
                self._threads = [item for item in self._threads if item.is_alive()] + [thread]

    def _serve(self, client: socket.socket) -> None:
        try:
            self._handle(client)
        finally:
            with self._lock:
                self._clients -= 1

    def _handle(self, client: socket.socket) -> None:
        try:
            header, rest = _read_header(client, self.header_timeout)
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
        if not allowed or self._closing.is_set():
            self._record(method, f"{_shown(host)}:{port}", False)
            _send(client, FORBIDDEN)
            self._untrack(client)
            return
        with self._lock:
            full = self._open_tunnels >= self.max_tunnels
            if full:
                self.busy += 1
            else:
                self._open_tunnels += 1
        if full:
            self._record(method, f"{_shown(host)}:{port}", False, reason="tunnel limit")
            _send(client, BUSY)
            self._untrack(client)
            return
        self._record(method, f"{_shown(host)}:{port}", True)
        try:
            self._tunnel(client, (host, port), rest)
        finally:
            with self._lock:
                self._open_tunnels -= 1

    def _tunnel(self, client: socket.socket, address: tuple[str, int], rest: bytes) -> None:
        try:
            upstream = self._connect(address, CONNECT_TIMEOUT)
        except OSError:
            _send(client, BAD_GATEWAY)
            self._untrack(client)
            return
        self._track(upstream)
        with self._lock:
            self.tunnels += 1
        moved = len(rest)
        try:
            client.sendall(ESTABLISHED)
            if rest:
                upstream.sendall(rest)
        except OSError:
            self._untrack(upstream)
            self._untrack(client)
            return
        ended = self._relay(client, upstream, moved)
        if ended:
            with self._lock:
                self.ended[ended] += 1
        self._untrack(upstream)
        self._untrack(client)

    def _relay(self, client: socket.socket, upstream: socket.socket, moved: int) -> str | None:
        """Copy bytes both ways (never inspected) until both sides close, the proxy closes or a
        limit ends the tunnel; returns the limit (``idle``, ``time``, ``bytes``) or None."""
        started = last = time.monotonic()
        if moved > self.tunnel_bytes:
            return "bytes"
        peers = {client: upstream, upstream: client}
        for sock in peers:
            sock.settimeout(self.idle_timeout)  # bounds a blocked send as well
        selector = selectors.DefaultSelector()
        try:
            for sock in peers:
                selector.register(sock, selectors.EVENT_READ)
            while not self._closing.is_set() and selector.get_map():
                now = time.monotonic()
                if now - started >= self.tunnel_seconds:
                    return "time"
                if now - last >= self.idle_timeout:
                    return "idle"
                wait = min(POLL_INTERVAL, self.tunnel_seconds - (now - started), self.idle_timeout - (now - last))
                for key, _ in selector.select(timeout=max(wait, 0.0)):
                    source = key.fileobj
                    target = peers[source]  # type: ignore[index]
                    chunk = source.recv(RELAY_CHUNK)  # type: ignore[union-attr]
                    if not chunk:
                        selector.unregister(source)
                        _shutdown_write(target)
                        continue
                    moved += len(chunk)
                    if moved > self.tunnel_bytes:
                        return "bytes"
                    target.sendall(chunk)
                    last = time.monotonic()
        except (OSError, ValueError):
            return None
        finally:
            selector.close()
        return None


def _read_header(client: socket.socket, timeout: float = HEADER_TIMEOUT) -> tuple[bytes | None, bytes]:
    """The request head (up to the blank line) and any bytes after it; None for a client that
    closed, sent more than `HEADER_LIMIT` bytes without one or did not send it all within
    ``timeout`` seconds: one deadline for the whole head, so a client that trickles a byte at a
    time cannot hold a client slot."""
    deadline = time.monotonic() + timeout
    data = b""
    while b"\r\n\r\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, b""
        client.settimeout(remaining)
        chunk = client.recv(4096)
        if not chunk:
            return None, b""
        data += chunk
        if len(data) > HEADER_LIMIT:
            _send(client, FORBIDDEN)
            return None, b""
    header, _, rest = data.partition(b"\r\n\r\n")
    return header, rest


def _shutdown_write(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_WR)
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
