"""POSIX exec boundary: enforce the native snapshot file limit before the engine."""
from __future__ import annotations

import os
import stat
import sys

LIMIT_ACK = "agents-core/native-fsize/1:"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        import resource

        limit = int(args[0])
        engine_args = args[1:]
        if engine_args[0] == "--lease-fd":
            lease_fd = int(engine_args[1])
            engine_args = engine_args[2:]
            if lease_fd < 3:
                raise ValueError()
            lease = os.fstat(lease_fd)
            if (not stat.S_ISREG(lease.st_mode) or lease.st_uid != os.geteuid()
                    or stat.S_IMODE(lease.st_mode) & 0o077 or lease.st_nlink != 1):
                raise ValueError()
            # pass_fds retains the supervisor's flock open file description in
            # this wrapper. Retain it through the second, native exec as well.
            os.set_inheritable(lease_fd, True)
        binary = engine_args[0]
        if limit < 1 or not os.path.isabs(binary):
            raise ValueError()
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        if resource.getrlimit(resource.RLIMIT_FSIZE) != (limit, limit):
            raise ValueError()
        # The supervisor must receive this exact acknowledgement before declaring
        # snapshots possible. Nothing from the engine's later output is retained.
        os.write(1, f"{LIMIT_ACK}{limit}\n".encode("ascii"))
        os.execv(binary, engine_args)
    except (ImportError, IndexError, OSError, ValueError):
        return 125
    return 125


if __name__ == "__main__":
    raise SystemExit(main())
