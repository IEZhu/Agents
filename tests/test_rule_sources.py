"""Network-boundary tests for rule fetching; no external network is used."""

from __future__ import annotations

import socket
import ssl
import threading
import traceback
from types import SimpleNamespace

import pytest

from src import rule_sources as sources

PUBLIC_IP = "93.184.216.34"
URL = "https://rules.example/team/rule.mdc"


def address(ip=PUBLIC_IP):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))


def response(body=b"# Rule\nBe precise.\n", headers=(), status="200 OK", version="1.1"):
    return "\r\n".join([f"HTTP/{version} {status}", *headers, "", ""]).encode() + body


class FakeSocket:
    def __init__(self, wire):
        self.wire = bytearray(wire)
        self.sent = bytearray()
        self.destinations = []
        self.timeouts = []
        self.closed = False
        self.read_size = 4096
        self.on_read = lambda: None
        self.on_connect = lambda: None

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def connect(self, destination):
        self.destinations.append(destination)
        self.on_connect()

    def sendall(self, data):
        self.sent.extend(data)

    def recv_into(self, buffer):
        assert not self.closed, "HTTP body must survive connection-close handoff"
        self.on_read()
        size = min(len(buffer), len(self.wire), self.read_size)
        buffer[:size] = self.wire[:size]
        del self.wire[:size]
        return size

    def close(self):
        self.closed = True


@pytest.fixture
def network(monkeypatch):
    monkeypatch.delenv("AGENTS_RULE_PRIVATE_HOSTS", raising=False)
    monkeypatch.setenv("AGENTS_RULE_AUTH_HOSTS", '{"RULE_SOURCE_TEST_TOKEN": "rules.example"}')
    state = SimpleNamespace(
        sock=FakeSocket(response()), dns_calls=[], tls_hosts=[], addresses=[address()],
    )

    def resolve(host, port, *, type):
        state.dns_calls.append((host, port, type))
        return state.addresses

    def wrap(sock, *, server_hostname):
        state.tls_hosts.append(server_hostname)
        return sock

    monkeypatch.setattr(sources.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(sources.socket, "socket", lambda *args: state.sock)
    monkeypatch.setattr(sources.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=wrap))
    return state


def test_raw_utf8_with_pinned_ip_tls_hostname_and_host_header(network, monkeypatch):
    network.sock.wire = bytearray(response("# Règle\nТекст\n".encode(), ("Content-Type: text/plain; charset=utf-8",)))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    assert sources.fetch_rule(URL) == "# Règle\nТекст\n"
    assert network.dns_calls == [("rules.example", 443, socket.SOCK_STREAM)]
    assert network.sock.destinations == [(PUBLIC_IP, 443)]
    assert network.tls_hosts == ["rules.example"]
    assert b"GET /team/rule.mdc HTTP/1.1" in network.sock.sent
    assert b"Host: rules.example" in network.sock.sent
    assert b"Accept-Encoding: identity" in network.sock.sent
    assert network.sock.closed
    assert all(0 < value <= sources.REQUEST_TIMEOUT for value in network.sock.timeouts)


def test_default_tls_verifies_certificates_and_hostname():
    connection = sources._PinnedHTTPSConnection("rules.example", 443, address())
    assert connection._context.verify_mode == ssl.CERT_REQUIRED
    assert connection._context.check_hostname is True
    connection.close()


@pytest.mark.parametrize("url", [
    "http://rules.example/rule", "file:///etc/passwd", "https:///rule", "",
    "https://user:secret@rules.example/rule", "https://@rules.example/rule",
    "https://rules.example/rule?access_token=secret", "https://rules.example/rule?raw=1",
    "https://rules.example/rule?", "https://rules.example/rule#secret", "https://rules.example/rule#",
    " https://rules.example/rule", "https://rules.example/\r\nHost:evil", "https://rules.example\\@evil/rule",
    "https://rules.example:0/rule", "https://rules.example:65536/rule", "https://rules.example:/rule",
    "https://[fe80::1%25en0]/rule", "https://%31%32%37.0.0.1/rule", "https://-bad.example/rule",
    "https://rules.example/" + "a" * 4096,
])
def test_malicious_urls_fail_before_dns(url, network):
    with pytest.raises(sources.RuleSourceError) as error:
        sources.fetch_rule(url)
    assert "secret" not in str(error.value)
    assert network.dns_calls == []
    assert network.sock.destinations == []


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.1", "172.16.5.2", "192.168.1.3", "169.254.169.254",
    "100.100.100.200", "0.0.0.0", "224.0.0.1", "192.0.2.1", "168.63.129.16",
    "::1", "::", "fe80::1", "fc00::1", "ff02::1", "::ffff:127.0.0.1",
    "::ffff:93.184.216.34",
])
def test_non_global_addresses_rejected(network, ip):
    network.addresses = [address(ip)]
    with pytest.raises(sources.RuleSourceError, match="disallowed"):
        sources.fetch_rule(URL)
    assert network.sock.destinations == []


def test_mixed_dns_answer_fails_closed(network):
    network.addresses = [address(), address("127.0.0.1")]
    with pytest.raises(sources.RuleSourceError, match="disallowed"):
        sources.fetch_rule(URL)
    assert network.sock.destinations == []


def test_dns_rebinding_cannot_change_connect_target(network, monkeypatch):
    def resolve(*args, **kwargs):
        network.dns_calls.append(args)
        return [address(PUBLIC_IP if len(network.dns_calls) == 1 else "127.0.0.1")]

    monkeypatch.setattr(sources.socket, "getaddrinfo", resolve)
    assert sources.fetch_rule(URL).startswith("# Rule")
    assert len(network.dns_calls) == 1
    assert network.sock.destinations == [(PUBLIC_IP, 443)]


@pytest.mark.parametrize("ip", ["10.0.0.5", "172.16.0.6", "192.168.0.7"])
def test_exact_private_host_allowlist(network, monkeypatch, ip):
    network.addresses = [address(ip)]
    monkeypatch.setenv("AGENTS_RULE_PRIVATE_HOSTS", "another.example, RULES.EXAMPLE")
    assert sources.fetch_rule(URL).startswith("# Rule")
    assert network.sock.destinations == [(ip, 443)]


@pytest.mark.parametrize("allowlist", ["example", "*.example", "other.rules.example", "https://rules.example"])
def test_private_allowlist_has_no_wildcards_or_suffix_matches(network, monkeypatch, allowlist):
    network.addresses = [address("10.0.0.5")]
    monkeypatch.setenv("AGENTS_RULE_PRIVATE_HOSTS", allowlist)
    with pytest.raises(sources.RuleSourceError, match="disallowed"):
        sources.fetch_rule(URL)


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "168.63.129.16", "100.100.100.200", "::1", "fc00::1"])
def test_allowlist_never_permits_local_or_metadata(network, monkeypatch, ip):
    network.addresses = [address(ip)]
    monkeypatch.setenv("AGENTS_RULE_PRIVATE_HOSTS", "rules.example")
    with pytest.raises(sources.RuleSourceError, match="disallowed"):
        sources.fetch_rule(URL)


def test_bearer_secret_only_in_authorization_header(network, monkeypatch):
    monkeypatch.setenv("RULE_SOURCE_TEST_TOKEN", "private.secret-token")
    assert sources.fetch_rule(URL, auth_env="RULE_SOURCE_TEST_TOKEN").startswith("# Rule")
    assert b"Authorization: Bearer private.secret-token\r\n" in network.sock.sent


@pytest.mark.parametrize("token", [None, "", "secret\r\nInjected: value", "secret value", "sécret"])
def test_invalid_auth_never_leaks(network, monkeypatch, token):
    monkeypatch.delenv("RULE_SOURCE_TEST_TOKEN", raising=False)
    if token is not None:
        monkeypatch.setenv("RULE_SOURCE_TEST_TOKEN", token)
    with pytest.raises(sources.RuleSourceError) as error:
        sources.fetch_rule(URL, auth_env="RULE_SOURCE_TEST_TOKEN")
    if token:
        assert token not in str(error.value)
    assert network.dns_calls == []


def test_auth_environment_name_is_validated(network):
    with pytest.raises(sources.RuleSourceError):
        sources.fetch_rule(URL, auth_env="private secret token")
    assert network.dns_calls == []


def test_transport_exception_details_and_token_are_suppressed(network, monkeypatch):
    monkeypatch.setenv("RULE_SOURCE_TEST_TOKEN", "private-secret")

    def fail():
        raise OSError("private-secret in an upstream diagnostic")

    network.sock.on_connect = fail
    with pytest.raises(sources.RuleSourceError) as error:
        sources.fetch_rule(URL, auth_env="RULE_SOURCE_TEST_TOKEN")
    assert "private-secret" not in "".join(traceback.format_exception(error.value))
    assert network.sock.closed


@pytest.mark.parametrize("headers,body,status,match", [
    (("Location: https://other.example/?token=secret",), b"secret", "302 Found", "redirect"),
    ((), b"secret", "401 Unauthorized", "HTTP 200"),
    (("Content-Encoding: gzip",), b"compressed", "200 OK", "Compressed"),
    (("Content-Type: text/html",), b"login", "200 OK", "raw text"),
    (("Content-Type: application/xhtml+xml",), b"login", "200 OK", "raw text"),
    (("Content-Type: text/plain",), b" <!DOCTYPE html><html>login", "200 OK", "HTML"),
    ((), b"\xef\xbb\xbf\n<!-- comment -->\n<html>login", "200 OK", "HTML"),
    ((), b"\xff\xfe", "200 OK", "UTF-8"),
    (("Content-Length: 65537",), b"short", "200 OK", "64 KiB"),
    ((), b"a" * 65537, "200 OK", "64 KiB"),
    (("Content-Length: -1",), b"", "200 OK", "content length"),
    (("Content-Length: 10",), b"short", "200 OK", "incomplete"),
    (("Transfer-Encoding: gzip",), b"body", "200 OK", "framing"),
    (("Transfer-Encoding: chunked", "Content-Length: 0"), b"0\r\n\r\n", "200 OK", "framing"),
])
def test_response_rejections(network, headers, body, status, match):
    network.sock.wire = bytearray(response(body, headers, status))
    with pytest.raises(sources.RuleSourceError, match=match) as error:
        sources.fetch_rule(URL)
    assert "secret" not in str(error.value)
    assert len(network.sock.destinations) == 1
    assert network.sock.closed


def test_exact_size_limit_and_multibyte_utf8(network):
    body = ("é" * (sources.MAX_RULE_BYTES // 2)).encode()
    network.sock.wire = bytearray(response(body, (f"Content-Length: {len(body)}",)))
    assert sources.fetch_rule(URL).encode() == body


def test_chunked_body(network):
    network.sock.wire = bytearray(response(b"4\r\nrule\r\n0\r\n\r\n", ("Transfer-Encoding: chunked",)))
    assert sources.fetch_rule(URL) == "rule"


def test_chunked_oversize_body(network):
    body = b"a" * (sources.MAX_RULE_BYTES + 1)
    chunks = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    network.sock.wire = bytearray(response(chunks, ("Transfer-Encoding: chunked",)))
    with pytest.raises(sources.RuleSourceError, match="64 KiB"):
        sources.fetch_rule(URL)


@pytest.mark.parametrize("version,headers", [("1.0", ()), ("1.1", ("Connection: close",))])
def test_connection_close_preserves_body_until_read(network, version, headers):
    network.sock.wire = bytearray(response(b"rule" * 3000, headers, version=version))
    assert sources.fetch_rule(URL) == "rule" * 3000
    assert network.sock.closed


@pytest.mark.parametrize("wire", [
    response(b"rule" * 1000),
    b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 1000 + b"\r\n\r\nbody",
    response(b"1000;extension=" + b"a" * 1000 + b"\r\n" + b"a" * 4096 + b"\r\n0\r\n\r\n", ("Transfer-Encoding: chunked",)),
])
def test_deadline_stops_trickling_headers_body_and_chunk_framing(network, monkeypatch, wire):
    now = [0.0]
    monkeypatch.setattr(sources.time, "monotonic", lambda: now[0])
    network.sock.wire = bytearray(wire)
    network.sock.read_size = 1
    network.sock.on_read = lambda: now.__setitem__(0, now[0] + 0.25)
    with pytest.raises(sources.RuleSourceError, match="timed out"):
        sources.fetch_rule(URL)
    assert now[0] <= sources.BODY_DEADLINE
    assert network.sock.closed


def test_socket_read_timeout_is_safe(network):
    def timeout():
        raise TimeoutError("sensitive diagnostic")

    network.sock.on_read = timeout
    with pytest.raises(sources.RuleSourceError, match="timed out") as error:
        sources.fetch_rule(URL)
    assert "sensitive" not in str(error.value)
    assert network.sock.closed


def test_dns_error_diagnostics_are_safe(network, monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror("sensitive diagnostic")

    monkeypatch.setattr(sources.socket, "getaddrinfo", fail)
    with pytest.raises(sources.RuleSourceError, match="DNS resolution failed") as error:
        sources.fetch_rule(URL)
    assert "sensitive" not in str(error.value)


def test_dns_wait_is_bounded(network, monkeypatch):
    release = threading.Event()
    finished = threading.Event()

    def resolve(*args, **kwargs):
        release.wait(1)
        finished.set()
        return [address()]

    monkeypatch.setattr(sources, "REQUEST_TIMEOUT", 0.01)
    monkeypatch.setattr(sources.socket, "getaddrinfo", resolve)
    try:
        with pytest.raises(sources.RuleSourceError, match="DNS resolution timed out"):
            sources.fetch_rule(URL)
        assert network.sock.destinations == []
    finally:
        release.set()
        assert finished.wait(1)


def test_dns_concurrency_exhaustion_fails_without_launching_more(network, monkeypatch):
    monkeypatch.setattr(sources, "_DNS_SLOTS", threading.BoundedSemaphore(0))
    with pytest.raises(sources.RuleSourceError, match="resolver is busy"):
        sources.fetch_rule(URL)
    assert network.dns_calls == []


def test_ipv6_literal_remains_pinned_and_bracketed_in_host_header(network):
    ip = "2606:4700:4700::1111"
    sockaddr = (ip, 443, 0, 0)
    network.addresses = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]
    assert sources.fetch_rule(f"https://[{ip}]/rule").startswith("# Rule")
    assert network.sock.destinations == [sockaddr]
    assert network.tls_hosts == [ip]
    assert f"Host: [{ip}]\r\n".encode() in network.sock.sent


def test_inherited_http_debug_setting_cannot_log_credentials(network, monkeypatch, capsys):
    monkeypatch.setattr(sources.http.client.HTTPConnection, "debuglevel", 1)
    monkeypatch.setenv("RULE_SOURCE_TEST_TOKEN", "private-secret")
    assert sources.fetch_rule(URL, auth_env="RULE_SOURCE_TEST_TOKEN").startswith("# Rule")
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_tls_failure_closes_socket_and_suppresses_diagnostic(network, monkeypatch):
    def reject(*args, **kwargs):
        raise ssl.SSLCertVerificationError("sensitive server details")

    monkeypatch.setattr(sources.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=reject))
    with pytest.raises(sources.RuleSourceError) as error:
        sources.fetch_rule(URL)
    assert "sensitive" not in str(error.value)
    assert network.sock.closed


@pytest.mark.parametrize("bindings", [None, "{}", '{"OTHER_TOKEN":"rules.example"}',
    '{"RULE_SOURCE_TEST_TOKEN":"other.example"}',
    '{"RULE_SOURCE_TEST_TOKEN":"example"}',
    '{"RULE_SOURCE_TEST_TOKEN":"*.example"}',
    '{"RULE_SOURCE_TEST_TOKEN":"https://rules.example"}',
])
def test_auth_requires_owner_approved_exact_host(network, monkeypatch, bindings):
    monkeypatch.setenv("RULE_SOURCE_TEST_TOKEN", "private-secret")
    monkeypatch.delenv("AGENTS_RULE_AUTH_HOSTS", raising=False)
    if bindings is not None:
        monkeypatch.setenv("AGENTS_RULE_AUTH_HOSTS", bindings)
    with pytest.raises(sources.RuleSourceError, match="host binding") as error:
        sources.fetch_rule(URL, auth_env="RULE_SOURCE_TEST_TOKEN")
    assert "private-secret" not in str(error.value)
    assert network.dns_calls == []
    assert network.sock.sent == b""


@pytest.mark.parametrize("bindings", ["not json", "[]", "null",
    '{"RULE_SOURCE_TEST_TOKEN":["rules.example"]}',
    '{"RULE_SOURCE_TEST_TOKEN":null}', '{"invalid name":"rules.example"}',
])
def test_malformed_auth_host_configuration_fails_safely(network, monkeypatch, bindings):
    monkeypatch.setenv("AGENTS_RULE_AUTH_HOSTS", bindings)
    monkeypatch.setenv("RULE_SOURCE_TEST_TOKEN", "private-secret")
    with pytest.raises(sources.RuleSourceError, match="JSON map") as error:
        sources.fetch_rule(URL, auth_env="RULE_SOURCE_TEST_TOKEN")
    assert "private-secret" not in str(error.value)
    assert network.dns_calls == []


@pytest.mark.parametrize("query,expected", [
    ("ref=main", "ref=main"),
    ("ref=feature/new-rule", "ref=feature%2Fnew-rule"),
    ("ref=release%2F1.2_3-test", "ref=release%2F1.2_3-test"),
])
def test_gitlab_api_raw_ref_is_validated_and_encoded(network, monkeypatch, query, expected):
    monkeypatch.setenv("RULE_SOURCE_TEST_TOKEN", "private-secret")
    path = "/api/v4/projects/123/repository/files/rules%2Frule-test.mdc/raw"
    assert sources.fetch_rule(f"https://rules.example{path}?{query}", auth_env="RULE_SOURCE_TEST_TOKEN").startswith("# Rule")
    assert f"GET {path}?{expected} HTTP/1.1\r\n".encode() in network.sock.sent
    assert b"Authorization: Bearer private-secret\r\n" in network.sock.sent


@pytest.mark.parametrize("query", [
    "ref=", "ref=-main", "ref=main&ref=dev", "ref=main&token=secret", "Ref=main",
    "ref=a+b", "ref=a%0d", "ref=a%26token%3Dsecret", "ref=main&", "ref",
    "ref=" + "x" * 257, "access_token=secret",
])
def test_unsafe_query_parameters_fail_before_network(network, query):
    with pytest.raises(sources.RuleSourceError, match="ref query") as error:
        sources.fetch_rule(URL + "?" + query)
    assert "secret" not in str(error.value)
    assert network.dns_calls == []


@pytest.mark.parametrize("ip", ["2002:7f00:1::", "2001::ffff:ffff", "64:ff9b::7f00:1"])
def test_ipv6_transition_addresses_cannot_tunnel_to_private_destinations(network, ip):
    network.addresses = [address(ip)]
    with pytest.raises(sources.RuleSourceError, match="disallowed"):
        sources.fetch_rule(URL)
    assert network.sock.destinations == []
