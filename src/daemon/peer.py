"""Whether the other end of a loopback TCP connection runs as the daemon's OS user.

The flow editor signs in a browser without a code only when this holds. Each
platform answers from the operating system's own connection table:

* Linux: ``/proc/net/tcp`` and ``tcp6`` record the owning user ID of every socket.
* Windows: ``GetExtendedTcpTable`` gives the owning process and the bind time; the
  process token gives the user SID, and a process newer than the bind holds a reused PID.
* macOS and other systems: ``lsof``, which without root lists only processes this
  user may inspect, so a connection from another account is never found.

Every error, timeout or unexpected value is a refusal. Standard library only, so
the check runs (and is tested) where the rest of the daemon is not installed.
"""
import ipaddress
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

LOOPBACK = "127.0.0.1"
LSOF_TIMEOUT = 5


def loopback_peer_is_owner(client, server) -> bool:
    """``client`` and ``server`` are the ASGI ``(host, port)`` pairs of one connection."""
    if not (_endpoint(client) and _endpoint(server)):
        return False
    client_port, server_port = client[1], server[1]
    try:
        if sys.platform.startswith("linux"):
            return proc_owners(client_port, server_port) == {os.getuid()}
        if sys.platform == "win32":
            return _windows_peer_is_owner(client_port, server_port)
        return _lsof_peer_is_owner(client_port, server_port)
    except (OSError, ValueError, struct.error, subprocess.SubprocessError):
        return False


def _endpoint(value) -> bool:
    return (isinstance(value, (tuple, list)) and len(value) == 2 and value[0] == LOOPBACK
            and type(value[1]) is int and 0 < value[1] < 65536)


# --- Linux ---------------------------------------------------------------------------

def _proc_address(text: str):
    """``0100007F:1F49`` -> ``("127.0.0.1", 8053)``; IPv4-mapped IPv6 becomes IPv4."""
    address, port = text.split(":")
    raw = bytes.fromhex(address)
    if sys.byteorder == "little":  # the kernel prints each 32-bit word in host byte order
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    ip = ipaddress.ip_address(raw)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip), int(port, 16)


def proc_owners(client_port: int, server_port: int, net=Path("/proc/net")) -> set[int]:
    """User IDs owning the established client side of ``client_port`` -> ``server_port``."""
    owners = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = (net / name).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 8 or fields[3] != "01":  # 01 = ESTABLISHED
                continue
            if (_proc_address(fields[1]) == (LOOPBACK, client_port)
                    and _proc_address(fields[2]) == (LOOPBACK, server_port)):
                owners.add(int(fields[7]))
    return owners


# --- macOS and other systems with lsof ------------------------------------------------

def lsof_owners(listing: str, client_port: int, server_port: int) -> set[int]:
    """User IDs owning the client side ``client_port -> server_port`` in ``lsof -Fpun`` output."""
    wanted = f"{LOOPBACK}:{client_port}->{LOOPBACK}:{server_port}"
    owners, uid = set(), None
    for line in listing.splitlines():
        if line.startswith("p"):
            uid = None
        elif line.startswith("u"):
            uid = int(line[1:]) if line[1:].isdigit() else -1  # unreadable: never the daemon's user
        elif line == "n" + wanted:
            owners.add(-1 if uid is None else uid)
    return owners


def _lsof_peer_is_owner(client_port: int, server_port: int) -> bool:
    lsof = shutil.which("lsof", path=os.environ.get("PATH", "") + os.pathsep + "/usr/sbin")
    if not lsof:
        return False
    listed = subprocess.run(
        [lsof, "-nP", f"-iTCP@{LOOPBACK}:{client_port}", "-sTCP:ESTABLISHED", "-Fpun"],
        capture_output=True, text=True, timeout=LSOF_TIMEOUT, check=True)  # lsof exits 1 when nothing matches
    return lsof_owners(listed.stdout, client_port, server_port) == {os.getuid()}


# --- Windows ---------------------------------------------------------------------------

AF_INET, AF_INET6 = 2, 23
MIB_TCP_STATE_ESTAB = 5
TCP_TABLE_OWNER_MODULE_CONNECTIONS = 7
TABLE_ROWS_AT = 8  # rows hold 64-bit fields, so they start after 4 bytes of padding
# MIB_TCPROW_OWNER_MODULE: state, local addr, local port, remote addr, remote port, pid,
# liCreateTimestamp (FILETIME of the bind), OwningModuleInfo[16].
ROW_V4 = struct.Struct("<I4s4s4s4sIq128s")
# MIB_TCP6ROW_OWNER_MODULE: local addr, scope, port, remote addr, scope, port, state, pid, timestamp, module info.
ROW_V6 = struct.Struct("<16sI4s16sI4sIIq128s")


def windows_rows(table: bytes, family: int):
    """``(local, remote, pid, created)`` of each established row of a ``*_TABLE_OWNER_MODULE`` buffer."""
    row, count = (ROW_V4 if family == AF_INET else ROW_V6), struct.unpack_from("<I", table)[0]
    if TABLE_ROWS_AT + count * row.size > len(table):
        raise ValueError("truncated TCP table")
    for index in range(count):
        values = row.unpack_from(table, TABLE_ROWS_AT + index * row.size)
        if family == AF_INET:
            state, local, local_port, remote, remote_port, pid, created, _ = values
        else:
            local, _, local_port, remote, _, remote_port, state, pid, created, _ = values
        if state != MIB_TCP_STATE_ESTAB:
            continue
        yield _windows_address(local, local_port), _windows_address(remote, remote_port), pid, created


def _windows_address(raw: bytes, port: bytes):
    ip = ipaddress.ip_address(raw)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip), int.from_bytes(port[:2], "big")  # the port is in network byte order


def windows_owner_matches(sockets, own_sid, inspect) -> bool:
    """Whether every ``(pid, created)`` socket belongs to a live process of ``own_sid``.

    ``inspect(pid)`` returns ``(sid, process_created)`` or None. The table keeps the PID
    of the process that bound the socket even after it exits, so a process started
    after the bind holds a reused PID and proves nothing.
    """
    if not sockets or own_sid is None:
        return False
    for pid, created in sockets:
        found = inspect(pid)
        if found is None or created <= 0:
            return False
        sid, started = found
        if sid != own_sid or started > created:
            return False
    return True


def _windows_peer_is_owner(client_port: int, server_port: int) -> bool:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    iphlpapi = ctypes.WinDLL("iphlpapi")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    iphlpapi.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                             wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD

    def table(family):
        size = wintypes.DWORD(0)
        for _ in range(4):  # the table may grow between the size query and the read
            buffer = ctypes.create_string_buffer(max(size.value, TABLE_ROWS_AT))
            result = iphlpapi.GetExtendedTcpTable(buffer, ctypes.byref(size), False, family,
                                                  TCP_TABLE_OWNER_MODULE_CONNECTIONS, 0)
            if result == 0:
                return buffer.raw
            if result != 122:  # ERROR_INSUFFICIENT_BUFFER
                break
        raise OSError(f"GetExtendedTcpTable failed: {result}")

    def user_sid(process):
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        try:
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # 1 = TokenUser
            buffer = ctypes.create_string_buffer(size.value)
            if not size.value or not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
            return ctypes.string_at(sid, advapi32.GetLengthSid(sid))
        finally:
            kernel32.CloseHandle(token)

    def inspect(pid):
        process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(process, *(ctypes.byref(t) for t in times)):
                return None
            sid = user_sid(process)
            started = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
            return None if sid is None else (sid, started)
        finally:
            kernel32.CloseHandle(process)

    tables = [(AF_INET, table(AF_INET))]
    try:
        tables.append((AF_INET6, table(AF_INET6)))
    except OSError:
        pass  # no IPv6 stack: an IPv4-mapped client cannot exist either
    sockets = {(pid, created) for family, raw in tables for local, remote, pid, created in windows_rows(raw, family)
               if local == (LOOPBACK, client_port) and remote == (LOOPBACK, server_port)}
    return windows_owner_matches(sockets, user_sid(kernel32.GetCurrentProcess()), inspect)
