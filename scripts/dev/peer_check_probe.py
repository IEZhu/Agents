"""Run the flow editor's loopback owner lookup on a real connection and report it.

Used by the Windows CI job as a standard (non-administrator) user, the account
type a normal installation runs as. Usage: python peer_check_probe.py REPO_ROOT
Exits 0 when a child process of this user is recognized as the owner.
"""
import getpass
import socket
import subprocess
import sys

sys.path.insert(0, sys.argv[1])
from src.daemon import peer  # noqa: E402

if sys.platform == "win32":
    import ctypes
    name, size = ctypes.create_unicode_buffer(257), ctypes.c_ulong(257)
    ctypes.windll.advapi32.GetUserNameW(name, ctypes.byref(size))  # the token's user, not %USERNAME%
    print("user:", name.value)
    print("administrator:", bool(ctypes.windll.shell32.IsUserAnAdmin()))
else:
    print("user:", getpass.getuser())
with socket.create_server(("127.0.0.1", 0)) as server:
    port = server.getsockname()[1]
    child = subprocess.Popen([sys.executable, "-c", "import socket, sys\n"
                              f"s = socket.create_connection(('127.0.0.1', {port}))\nsys.stdin.read()\n"],
                             stdin=subprocess.PIPE)
    connection, client = server.accept()
    try:
        owner = peer.loopback_peer_is_owner(client, server.getsockname())
        if sys.platform == "win32" and not owner:
            try:  # the public check hides errors; show the reason
                print("direct:", peer._windows_peer_is_owner(client[1], port))
            except Exception as error:
                print("direct error:", repr(error))
    finally:
        connection.close()
        child.stdin.close()
        child.wait(timeout=10)
print("owner:", owner)
sys.exit(0 if owner else 1)
