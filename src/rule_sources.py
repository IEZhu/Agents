"""Fetch bounded UTF-8 rule sources over HTTPS without trusting DNS twice.

Sources must be direct raw-file URLs. Only a single validated ``ref`` query
parameter is accepted (for GitLab raw-file APIs); fragments and redirects are
rejected. Authentication belongs in a named environment variable, never a URL. The owner
must bind that variable to the exact host in AGENTS_RULE_AUTH_HOSTS (a JSON map).
Only globally routable addresses are allowed unless the exact hostname occurs in
AGENTS_RULE_PRIVATE_HOSTS (comma/whitespace separated); that exception permits
RFC1918 IPv4 addresses only, never loopback, link-local or metadata addresses.
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

MAX_RULE_BYTES = 64 * 1024
REQUEST_TIMEOUT = 10.0
BODY_DEADLINE = 20.0
_MAX_URL_CHARS = 4096
_DNS_SLOTS = threading.BoundedSemaphore(4)
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
_METADATA_ADDRESSES = {ipaddress.ip_address("168.63.129.16")}
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_HTML_START = re.compile(
    r"<(?:!doctype\s+html|html|head|body|title|meta|script|div|form)(?:\s|/?>)", re.I,
)


class RuleSourceError(ValueError):
    """A safe, credential-free diagnostic for a rejected or unavailable source."""


def _parse_url(url: str) -> tuple[str, int, str]:
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_CHARS:
        raise RuleSourceError("Rule source must be a bounded HTTPS URL.")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise RuleSourceError("Rule source URL contains invalid characters.")
    if "#" in url:
        raise RuleSourceError("Use a direct raw-file URL without a fragment.")
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() != "https" or not parts.hostname:
            raise ValueError
        if parts.username is not None or parts.password is not None:
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = 443 if parts.port is None else parts.port
        if not 1 <= port <= 65535 or "%" in host or parts.netloc.endswith(":"):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or not all(_HOST_LABEL.fullmatch(part) for part in host.split(".")):
                raise ValueError from None
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    except (ValueError, UnicodeError):
        raise RuleSourceError("Rule source must be an HTTPS URL without embedded credentials.") from None
    if "?" in url:
        try:
            pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
        except ValueError:
            raise RuleSourceError("Only one valid ref query parameter is allowed.") from None
        if (
            len(pairs) != 1 or pairs[0][0] != "ref"
            or not re.fullmatch(r"[A-Za-z0-9_./-]{1,256}", pairs[0][1])
            or pairs[0][1].startswith("-")
        ):
            raise RuleSourceError("Only one valid ref query parameter is allowed.")
        path += "?" + urlencode(pairs)
    return host, port, path


def _authorization(auth_env: str | None, host: str) -> dict[str, str]:
    if auth_env is None:
        return {}
    if not isinstance(auth_env, str) or not _ENV_NAME.fullmatch(auth_env):
        raise RuleSourceError("Authentication requires a valid environment variable name.")
    try:
        bindings = json.loads(os.environ.get("AGENTS_RULE_AUTH_HOSTS", "{}"))
    except ValueError:
        raise RuleSourceError("AGENTS_RULE_AUTH_HOSTS must be a JSON map of environment names to exact hostnames.") from None
    if not isinstance(bindings, dict) or not all(
        isinstance(key, str) and _ENV_NAME.fullmatch(key) and isinstance(value, str)
        for key, value in bindings.items()
    ):
        raise RuleSourceError("AGENTS_RULE_AUTH_HOSTS must be a JSON map of environment names to exact hostnames.")
    configured_host = bindings.get(auth_env, "").lower().rstrip(".")
    if not configured_host or configured_host != host:
        raise RuleSourceError("Authentication requires a matching exact host binding in AGENTS_RULE_AUTH_HOSTS.")
    token = os.environ.get(auth_env)
    if not token:
        raise RuleSourceError("The authentication environment variable is unset or empty.")
    if len(token) > 8192 or not _BEARER_TOKEN.fullmatch(token):
        raise RuleSourceError("The authentication environment variable is not a valid Bearer token.")
    return {"Authorization": "Bearer " + token}


def _resolve(host: str, port: int) -> list[tuple]:
    """Bound resolver waits and outstanding lookups without blocking shutdown."""
    slots = _DNS_SLOTS
    if not slots.acquire(blocking=False):
        raise RuleSourceError("Rule source DNS resolver is busy; try again later.")
    results: queue.Queue = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            results.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception:
            # Resolver diagnostics may include supplied input. Never expose them.
            results.put(None)
        finally:
            slots.release()

    try:
        threading.Thread(target=resolve, daemon=True, name="rule-source-dns").start()
    except RuntimeError:
        slots.release()
        raise RuleSourceError("Rule source DNS resolution could not start.") from None
    try:
        addresses = results.get(timeout=REQUEST_TIMEOUT)
    except queue.Empty:
        raise RuleSourceError("Rule source DNS resolution timed out.") from None
    if not addresses or len(addresses) > 64:
        raise RuleSourceError("Rule source DNS resolution failed.")
    return addresses


def _validated_address(host: str, port: int) -> tuple:
    private_hosts = {
        entry.lower().rstrip(".")
        for entry in re.split(r"[\s,]+", os.environ.get("AGENTS_RULE_PRIVATE_HOSTS", ""))
        if entry
    }
    addresses = _resolve(host, port)
    for family, kind, protocol, _name, address in addresses:
        try:
            ip = ipaddress.ip_address(address[0])
        except ValueError:
            raise RuleSourceError("Rule source DNS returned an invalid address.") from None
        private_allowed = host in private_hosts and ip.version == 4 and any(
            ip in network for network in _PRIVATE_NETWORKS
        )
        if (
            family not in (socket.AF_INET, socket.AF_INET6)
            or kind != socket.SOCK_STREAM
            or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_unspecified or ip.is_reserved or ip in _METADATA_ADDRESSES
            or getattr(ip, "ipv4_mapped", None) is not None
            or getattr(ip, "sixtofour", None) is not None
            or getattr(ip, "teredo", None) is not None
            or (not ip.is_global and not private_allowed)
        ):
            raise RuleSourceError("Rule source resolves to a disallowed network address.")
    # Prefer IPv4 when both are offered; connect directly to this numeric address.
    return next((entry for entry in addresses if entry[0] == socket.AF_INET), addresses[0])


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return min(REQUEST_TIMEOUT, remaining)


class _DeadlineReader(io.RawIOBase):
    def __init__(self, owner: _DeadlineSocket):
        self._owner = owner

    def close(self) -> None:
        if not self.closed:
            super().close()
            self._owner._release_reader()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        self._owner._sock.settimeout(_remaining(self._owner._deadline))
        count = self._owner._sock.recv_into(buffer)
        _remaining(self._owner._deadline)
        return count


class _DeadlineSocket:
    """Enforce a single deadline across headers, chunk framing and body reads."""

    def __init__(self, sock: ssl.SSLSocket, deadline: float):
        self._sock = sock
        self._deadline = deadline
        self._readers = 0
        self._close_requested = False

    def makefile(self, mode: str):
        if mode != "rb":
            raise ValueError("Only response reads are supported")
        self._readers += 1
        return io.BufferedReader(_DeadlineReader(self))

    def sendall(self, data) -> None:
        self._sock.settimeout(_remaining(self._deadline))
        self._sock.sendall(data)
        _remaining(self._deadline)

    def _release_reader(self) -> None:
        self._readers -= 1
        if self._close_requested and self._readers == 0:
            self._sock.close()

    def close(self) -> None:
        # HTTPConnection can close itself before its response body is consumed.
        # Preserve the same file-reference lifetime as SSLSocket.makefile().
        self._close_requested = True
        if self._readers == 0:
            self._sock.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: tuple):
        super().__init__(host, port, timeout=REQUEST_TIMEOUT, context=ssl.create_default_context())
        self.debuglevel = 0  # Never inherit HTTPConnection's header debug logging.
        self._address = address
        self._deadline = time.monotonic() + BODY_DEADLINE

    def connect(self) -> None:
        family, kind, protocol, _name, address = self._address
        raw = socket.socket(family, kind, protocol)
        try:
            raw.settimeout(_remaining(self._deadline))
            raw.connect(address)
            raw.settimeout(_remaining(self._deadline))
            wrapped = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock = _DeadlineSocket(wrapped, self._deadline)
        except BaseException:
            raw.close()
            raise


def _read_response(response: http.client.HTTPResponse, deadline: float) -> str:
    if 300 <= response.status < 400:
        raise RuleSourceError("Rule source redirects are not allowed; use the final raw-file URL.")
    if response.status != 200:
        raise RuleSourceError("Rule source did not return HTTP 200.")
    encoding = response.getheader("Content-Encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        raise RuleSourceError("Compressed rule sources are not supported.")
    content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type not in ("", "text/plain", "text/markdown", "text/x-markdown", "application/octet-stream"):
        raise RuleSourceError("Rule source must return raw text, not HTML or another document format.")
    transfer = response.getheader("Transfer-Encoding", "").strip().lower()
    length = response.getheader("Content-Length")
    if transfer not in ("", "chunked") or (transfer and length is not None):
        raise RuleSourceError("Rule source returned unsupported response framing.")
    expected = None
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,10}", length.strip()):
            raise RuleSourceError("Rule source returned an invalid content length.")
        expected = int(length)
        if expected > MAX_RULE_BYTES:
            raise RuleSourceError("Rule source exceeds the 64 KiB size limit.")
    body = bytearray()
    while True:
        _remaining(deadline)
        chunk = response.read1(min(8192, MAX_RULE_BYTES + 1 - len(body)))
        _remaining(deadline)
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > MAX_RULE_BYTES:
            raise RuleSourceError("Rule source exceeds the 64 KiB size limit.")
    if expected is not None and len(body) != expected:
        raise RuleSourceError("Rule source response was incomplete.")
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeError:
        raise RuleSourceError("Rule source must be valid UTF-8 text.") from None
    prefix = re.sub(r"\A(?:\s|\ufeff|<!--[\s\S]*?-->)*", "", text)
    if _HTML_START.match(prefix):
        raise RuleSourceError("Rule source returned HTML; use a raw-file URL.")
    return text


def fetch_rule(url: str, *, auth_env: str | None = None) -> str:
    """Fetch one direct HTTPS raw-text source, or raise a safe RuleSourceError.

    DNS waits and each socket operation are limited to REQUEST_TIMEOUT seconds;
    connection, TLS, headers and body also share BODY_DEADLINE seconds. Redirects
    are rejected even on the same host. Proxy settings are not consumed by this
    transport. TLS uses Python's default certificate-verifying trust context.
    """
    host, port, path = _parse_url(url)
    headers = {"Accept": "text/plain, text/markdown", "Accept-Encoding": "identity", "Connection": "close"}
    headers.update(_authorization(auth_env, host))
    address = _validated_address(host, port)
    connection = None
    try:
        connection = _PinnedHTTPSConnection(host, port, address)
        connection.request("GET", path, headers=headers)
        with connection.getresponse() as response:
            return _read_response(response, connection._deadline)
    except RuleSourceError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise RuleSourceError("Rule source fetch failed or timed out.") from None
    finally:
        if connection is not None:
            connection.close()
