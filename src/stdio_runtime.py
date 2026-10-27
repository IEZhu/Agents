"""Run a transport with explicitly supplied startup and shutdown dependencies."""


def run_stdio(transport, readiness, *, startup, drain_logs, shutdown_wait):
    """Keep warmup asynchronous and join pending work before draining logs.

    The caller acquires installation leases and configures process paths before
    importing the application. This function neither acquires another lease nor
    chooses Personal sync, an updater, or a different application profile.
    """
    readiness.start()
    try:
        startup(readiness)
        transport.run()
    finally:
        readiness.join(shutdown_wait)
        drain_logs()
