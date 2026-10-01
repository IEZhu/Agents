"""Loopback peer ownership for the flow editor's automatic sign-in (src/daemon/peer.py).

Standard library and pytest only: the Windows CI job runs this file without the
daemon's dependencies, and the real-connection test runs on every platform.
"""
import ipaddress
import os
import socket
import struct
import subprocess
import sys

import pytest

from src.daemon import peer

CLIENT, SERVER = ("127.0.0.1", 54321), ("127.0.0.1", 8765)


# --- Linux: /proc/net/tcp and tcp6 ---------------------------------------------------

def proc_hex(ip: str, port: int) -> str:
    raw = ipaddress.ip_address(ip).packed
    if sys.byteorder == "little":
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return f"{raw.hex().upper()}:{port:04X}"


def proc_line(local, remote, state, uid, ip="127.0.0.1"):
    return (f"   0: {proc_hex(ip, local)} {proc_hex(ip if ip == '127.0.0.1' else '::ffff:127.0.0.1', remote)} "
            f"{state} 00000000:00000000 00:00000000 00000000  {uid}        0 12345 1 0000000000000000 20 4 30 10 -1")


HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def test_proc_address_decodes_ipv4_and_mapped_ipv6():
    assert peer._proc_address(proc_hex("127.0.0.1", 8765)) == ("127.0.0.1", 8765)
    assert peer._proc_address(proc_hex("::ffff:127.0.0.1", 443)) == ("127.0.0.1", 443)
    assert peer._proc_address(proc_hex("::1", 22)) == ("::1", 22)


def test_proc_owners_reads_only_the_established_client_side(tmp_path):
    (tmp_path / "tcp").write_text(HEADER + "\n".join([
        proc_line(54321, 8765, "01", 501),     # the client side: counted
        proc_line(8765, 54321, "01", 999),     # the daemon's side: different direction
        proc_line(54322, 8765, "01", 777),     # another connection
        proc_line(54321, 8765, "06", 888),     # TIME_WAIT
    ]) + "\n")
    assert peer.proc_owners(54321, 8765, net=tmp_path) == {501}
    (tmp_path / "tcp6").write_text(HEADER + proc_line(54321, 8765, "01", 502, ip="::ffff:127.0.0.1") + "\n")
    assert peer.proc_owners(54321, 8765, net=tmp_path) == {501, 502}


def test_proc_owners_without_tables_finds_nobody(tmp_path):
    assert peer.proc_owners(54321, 8765, net=tmp_path) == set()


@pytest.mark.parametrize("owners, expected", [({501}, True), (set(), False), ({502}, False), ({501, 0}, False)])
def test_linux_compares_with_the_daemon_user(monkeypatch, owners, expected):
    monkeypatch.setattr(peer.sys, "platform", "linux")
    monkeypatch.setattr(peer.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(peer, "proc_owners", lambda client, server: owners)
    assert peer.loopback_peer_is_owner(CLIENT, SERVER) is expected


def test_linux_read_error_is_a_refusal(monkeypatch):
    monkeypatch.setattr(peer.sys, "platform", "linux")

    def broken(client, server):
        raise ValueError("odd /proc line")

    monkeypatch.setattr(peer, "proc_owners", broken)
    assert peer.loopback_peer_is_owner(CLIENT, SERVER) is False


# --- macOS and others: lsof -------------------------------------------------------------

CLIENT_SIDE = "n127.0.0.1:54321->127.0.0.1:8765"
DAEMON_SIDE = "n127.0.0.1:8765->127.0.0.1:54321"


@pytest.mark.parametrize("listing, expected", [
    (f"p100\nu501\nf20\n{DAEMON_SIDE}\np200\nu501\nf3\n{CLIENT_SIDE}\n", True),  # daemon and browser
    (f"p200\nu501\nf3\n{CLIENT_SIDE}\n", True),
    (f"p100\nu501\nf20\n{DAEMON_SIDE}\n", False),           # only the daemon's side
    ("", False),                                                  # nothing visible: another account
    (f"p100\nu501\nf20\n{DAEMON_SIDE}\np200\nu502\nf3\n{CLIENT_SIDE}\n", False),  # another user
    (f"p200\nu501\nf3\n{CLIENT_SIDE}\np300\nu0\nf4\n{CLIENT_SIDE}\n", False),     # mixed owners
    (f"p200\nuroot\nf3\n{CLIENT_SIDE}\n", False),             # unreadable owner
    (f"p200\nf3\n{CLIENT_SIDE}\n", False),                     # owner missing
    # A same-user socket that only touches the client port is not this connection.
    (f"p300\nu501\nf4\nn127.0.0.1:9999->127.0.0.1:54321\n", False),
    (f"p300\nu501\nf4\nn127.0.0.1:54321->127.0.0.1:9999\n", False),
])
def test_lsof_listing(monkeypatch, listing, expected):
    seen = []
    monkeypatch.setattr(peer.sys, "platform", "darwin")
    monkeypatch.setattr(peer.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(peer.shutil, "which", lambda name, path=None: "/usr/sbin/lsof")
    monkeypatch.setattr(peer.subprocess, "run", lambda args, **kwargs: seen.append(args) or
                        subprocess.CompletedProcess(args, 0, stdout=listing, stderr=""))
    assert peer.loopback_peer_is_owner(CLIENT, SERVER) is expected
    assert seen == [["/usr/sbin/lsof", "-nP", "-iTCP@127.0.0.1:54321", "-sTCP:ESTABLISHED", "-Fpun"]]


@pytest.mark.parametrize("failure", ["missing", "timeout", "oserror"])
def test_lsof_failures_are_refusals(monkeypatch, failure):
    monkeypatch.setattr(peer.sys, "platform", "darwin")
    monkeypatch.setattr(peer.shutil, "which",
                        lambda name, path=None: None if failure == "missing" else "/usr/sbin/lsof")

    def run(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        raise OSError("lsof failed")

    monkeypatch.setattr(peer.subprocess, "run", run)
    assert peer.loopback_peer_is_owner(CLIENT, SERVER) is False


# --- Windows: GetExtendedTcpTable rows --------------------------------------------------

def port_bytes(port):
    return port.to_bytes(2, "big") + b"\0\0"


def table_of(rows):
    return struct.pack("<I", len(rows)) + b"\0" * (peer.TABLE_ROWS_AT - 4) + b"".join(rows)


def v4_table(*rows):
    return table_of([peer.ROW_V4.pack(state, ipaddress.ip_address(local[0]).packed, port_bytes(local[1]),
                                      ipaddress.ip_address(remote[0]).packed, port_bytes(remote[1]), pid,
                                      created, b"module")
                     for state, local, remote, pid, created in rows])


def v6_table(*rows):
    return table_of([peer.ROW_V6.pack(ipaddress.ip_address(local[0]).packed, 0, port_bytes(local[1]),
                                      ipaddress.ip_address(remote[0]).packed, 0, port_bytes(remote[1]), state, pid,
                                      created, b"module")
                     for state, local, remote, pid, created in rows])


def test_windows_row_layouts_match_the_sdk():
    # MIB_TCPROW_OWNER_MODULE: 6 DWORDs, LARGE_INTEGER, ULONGLONG[16]; the IPv6 row adds two 16-byte addresses.
    assert (peer.ROW_V4.size, peer.ROW_V6.size, peer.TABLE_ROWS_AT) == (160, 192, 8)


def test_windows_rows_decode_established_connections():
    table = v4_table((5, CLIENT, SERVER, 4242, 1000), (2, CLIENT, SERVER, 1, 1000), (5, SERVER, CLIENT, 7, 900))
    assert list(peer.windows_rows(table, peer.AF_INET)) == [(CLIENT, SERVER, 4242, 1000), (SERVER, CLIENT, 7, 900)]
    mapped = v6_table((5, ("::ffff:127.0.0.1", 54321), ("::ffff:127.0.0.1", 8765), 4343, 1200),
                      (5, ("::1", 1), ("::1", 2), 9, 0))
    assert list(peer.windows_rows(mapped, peer.AF_INET6)) == [(CLIENT, SERVER, 4343, 1200),
                                                              (("::1", 1), ("::1", 2), 9, 0)]


def test_windows_truncated_table_is_an_error():
    table = v4_table((5, CLIENT, SERVER, 4242, 1000))
    with pytest.raises(ValueError):
        list(peer.windows_rows(table[:-1], peer.AF_INET))
    with pytest.raises(ValueError):
        list(peer.windows_rows(struct.pack("<I", 3) + b"\0" * 4, peer.AF_INET6))


OWN, OTHER = b"sid-of-daemon-user", b"sid-of-someone-else"


@pytest.mark.parametrize("sockets, processes, own, expected", [
    ({(10, 2000)}, {10: (OWN, 1000)}, OWN, True),                       # started before the bind
    ({(10, 2000), (11, 2000)}, {10: (OWN, 1000), 11: (OWN, 2000)}, OWN, True),
    ({(10, 2000)}, {10: (OTHER, 1000)}, OWN, False),                    # another user
    ({(10, 2000)}, {}, OWN, False),                                     # OpenProcess or token refused
    ({(10, 2000)}, {10: (OWN, 3000)}, OWN, False),                      # PID reused after the bind
    ({(10, 0)}, {10: (OWN, 1000)}, OWN, False),                         # no bind time
    ({(10, 2000), (11, 2000)}, {10: (OWN, 1000), 11: (OTHER, 1000)}, OWN, False),
    (set(), {}, OWN, False),                                            # no matching row
    ({(10, 2000)}, {10: (OWN, 1000)}, None, False),                     # own SID unknown
])
def test_windows_owner_decision(sockets, processes, own, expected):
    assert peer.windows_owner_matches(sockets, own, processes.get) is expected


@pytest.mark.parametrize("error", [OSError("access denied"), ValueError("bad table"), struct.error("short")])
def test_windows_errors_are_refusals(monkeypatch, error):
    monkeypatch.setattr(peer.sys, "platform", "win32")

    def broken(client, server):
        raise error

    monkeypatch.setattr(peer, "_windows_peer_is_owner", broken)
    assert peer.loopback_peer_is_owner(CLIENT, SERVER) is False


# --- Input validation -----------------------------------------------------------------------

@pytest.mark.parametrize("client, server", [
    (None, SERVER), (CLIENT, None), (("::1", 54321), SERVER), (("10.0.0.5", 54321), SERVER),
    (("127.0.0.1", 0), SERVER), (("127.0.0.1", 70000), SERVER), (("127.0.0.1", "54321"), SERVER),
    (("127.0.0.1", True), SERVER), (("127.0.0.1",), SERVER), (CLIENT, ("0.0.0.0", 8765)),
])
@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_non_loopback_endpoints_never_reach_the_os(monkeypatch, client, server, platform):
    monkeypatch.setattr(peer.sys, "platform", platform)
    for name in ("proc_owners", "_lsof_peer_is_owner", "_windows_peer_is_owner"):
        monkeypatch.setattr(peer, name, lambda *args: pytest.fail("the OS must not be queried"))
    assert peer.loopback_peer_is_owner(client, server) is False


# --- A real connection from a child process ------------------------------------------------

def real_check_available():
    if sys.platform.startswith("linux"):
        return os.path.exists("/proc/net/tcp")
    if sys.platform == "win32":
        return True
    return bool(peer.shutil.which("lsof", path=os.environ.get("PATH", "") + os.pathsep + "/usr/sbin"))


def connect_from_child(server_socket, **popen):
    port = server_socket.getsockname()[1]
    code = f"import socket, sys\ns = socket.create_connection(('127.0.0.1', {port}))\nsys.stdin.read()\n"
    return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, **popen)


@pytest.mark.skipif(not real_check_available(), reason="no connection table on this platform")
def test_a_child_process_of_the_same_user_is_the_owner():
    with socket.create_server(("127.0.0.1", 0)) as server_socket:
        child = connect_from_child(server_socket)
        try:
            connection, client = server_socket.accept()
            with connection:
                assert peer.loopback_peer_is_owner(client, server_socket.getsockname()) is True
                # The client port alone is not enough: the pair must be this connection.
                assert peer.loopback_peer_is_owner(client, ("127.0.0.1", 1)) is False
        finally:
            child.stdin.close()
            child.wait(timeout=10)


@pytest.mark.skipif(not (sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs root on Linux to start a process as another user")
def test_a_process_of_another_user_is_refused():
    with socket.create_server(("127.0.0.1", 0)) as server_socket:
        child = connect_from_child(server_socket, user=65534, group=65534)  # nobody
        try:
            connection, client = server_socket.accept()
            with connection:
                assert peer.loopback_peer_is_owner(client, server_socket.getsockname()) is False
        finally:
            child.stdin.close()
            child.wait(timeout=10)
