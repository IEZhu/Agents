"""A thin client of the autodev runner socket for sandboxed sessions (design 5.6).

The runner writes this file into each run as the executables ``gh``, ``worker`` and ``heavy``
(`src.schedules.socket_service.write_clients`): an absolute ``#!<python> -I`` line on top and the
socket path and the client's name embedded below. It imports only the standard library and opens
nothing but the socket, so a session needs read access to the clients directory and the
interpreter only.

    gh <operation> [JSON object]       one request of type gh.<operation>
    worker <operation> [JSON object]   one request of type worker.<operation>
    heavy -- <command> [args]          the command, run under the heavy-process lease

``gh`` and ``worker`` print the result as JSON and exit with 0, or print the refusal on standard
error and exit with 1. ``heavy`` takes the lease, runs the command itself without a shell, with
the inherited standard streams, keeps the connection open while it runs, releases the lease and
exits with the command's status. When the lease is refused it prints the reason on standard error
and exits with 75. Other exit codes: 2 for wrong arguments, 69 when the runner socket cannot be
reached, 126 or 127 when the command cannot be started.
"""
import json
import signal
import socket
import subprocess
import sys

SOCKET_PATH = ""
CLIENT = ""

EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 69  # EX_UNAVAILABLE
EXIT_LEASE_REFUSED = 75  # EX_TEMPFAIL
EXIT_CANNOT_EXECUTE = 126
EXIT_NOT_FOUND = 127
FORWARDED = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


class Unavailable(Exception):
    """The runner socket cannot be reached or gave no answer."""


def connect():
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(SOCKET_PATH)
    except OSError as error:
        client.close()
        raise Unavailable(f"cannot reach the runner socket {SOCKET_PATH}: {error.strerror or error}") from None
    return client


def ask(client, request_type, args):
    """Send one request and return the response object."""
    received = bytearray()
    try:
        client.sendall(json.dumps({"type": request_type, "args": args}).encode() + b"\n")
        while b"\n" not in received:
            chunk = client.recv(65536)
            if not chunk:
                raise Unavailable("the runner socket closed the connection without an answer")
            received += chunk
    except OSError as error:
        raise Unavailable(f"the runner socket failed: {error.strerror or error}") from None
    try:
        response = json.loads(bytes(received).partition(b"\n")[0])
    except ValueError:
        raise Unavailable("the runner socket did not answer with JSON") from None
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        raise Unavailable("the runner socket gave an answer without ok")
    return response


def usage(message=None):
    if message:
        print(f"{CLIENT}: {message}", file=sys.stderr)
    shape = "-- <command> [args]" if CLIENT == "heavy" else "<operation> [JSON object]"
    print(f"usage: {CLIENT} {shape}", file=sys.stderr)
    return EXIT_USAGE


def request(argv):
    """``gh`` and ``worker``: one request; the result printed as JSON."""
    if not argv or len(argv) > 2 or argv[0].startswith("-"):
        return usage()
    args = {}
    if len(argv) == 2:
        try:
            args = json.loads(argv[1])
        except ValueError:
            args = None
        if not isinstance(args, dict):
            return usage("the arguments must be one JSON object")
    client = connect()
    try:
        response = ask(client, f"{CLIENT}.{argv[0]}", args)
    finally:
        client.close()
    if response["ok"]:
        print(json.dumps(response.get("result"), ensure_ascii=False, indent=2))
        return 0
    print(f"{CLIENT}: refused: {response.get('error')}", file=sys.stderr)
    return EXIT_REFUSED


def run(command):
    """Run ``command`` without a shell, passing stop signals on; its exit status."""
    try:
        process = subprocess.Popen(command)
    except FileNotFoundError:
        print(f"heavy: command not found: {command[0]}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except OSError as error:
        print(f"heavy: cannot run {command[0]}: {error.strerror or error}", file=sys.stderr)
        return EXIT_CANNOT_EXECUTE

    def forward(signum, frame):
        process.send_signal(signum)

    previous = {number: signal.signal(number, forward) for number in FORWARDED}
    try:
        status = process.wait()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 128 - status if status < 0 else status


def heavy(argv):
    """``heavy -- <command> [args]``: the command under the heavy-process lease."""
    if len(argv) < 2 or argv[0] != "--":
        return usage()
    client = connect()
    try:
        response = ask(client, "heavy.acquire", {})
        if not response["ok"]:
            print(f"heavy: lease refused: {response.get('error')}", file=sys.stderr)
            return EXIT_LEASE_REFUSED
        status = run(argv[1:])
        try:
            ask(client, "heavy.release", {})
        except Unavailable:
            pass  # the closed connection releases the lease as well
        return status
    finally:
        client.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not SOCKET_PATH or CLIENT not in ("gh", "worker", "heavy"):
        print("this client was not written by the autodev runner", file=sys.stderr)
        return EXIT_USAGE
    try:
        return heavy(argv) if CLIENT == "heavy" else request(argv)
    except Unavailable as error:
        print(f"{CLIENT}: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(main())
