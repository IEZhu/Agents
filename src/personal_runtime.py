"""Startup composition for the existing Personal local runtime.

Both transports use these hooks. This is an explicit home for Personal side
effects, not an edition selector: the daemon still owns its transport lifecycle,
and UserSync still owns its scheduler, leases, queue and drain behavior.
"""
import logging

logger = logging.getLogger("mcp-server")


def install_history_sync() -> None:
    """Install cross-machine history and queue its startup catch-up.

    Called at startup, never on import, so importing tools does not reach the
    machine's sync settings or library. A failure leaves local tools usable.
    """
    try:
        from src.user_sync import history as history_sync
        history_sync.install()
    except Exception:
        logger.warning("History sync between machines is unavailable", exc_info=True)


def daemon_components(service, *, hand_off_schedule=False):
    """Create the existing Personal editor and sync task without starting them.

    Keep the service attributes used by the UI, health and admin routes. Only
    the daemon's real entry point opts into handing off the OS sync scheduler.
    """
    from src.daemon.flows_ui import FlowsUI
    from src.daemon.runtime import DaemonComponents
    from src.daemon.sync_loop import UserSync

    return DaemonComponents(FlowsUI(service), UserSync(service, hand_off_schedule=hand_off_schedule))


def _start_library_sync() -> None:
    try:
        from src.user_sync.trigger import start_for_stdio
        start_for_stdio()
    except Exception:
        logger.exception("Library sync trigger did not start")


def start_stdio(readiness) -> None:
    """Install Personal hooks after stdio has started its readiness worker.

    Background update preparation and the library trigger run after warmup
    finishes (including a failed warmup), as readiness.when_done specifies.
    The daemon has its own updater and UserSync loop and does not call this.
    """
    install_history_sync()
    from src.self_update import log_last_update, start_background_update
    log_last_update()
    readiness.when_done(start_background_update)
    readiness.when_done(_start_library_sync)
