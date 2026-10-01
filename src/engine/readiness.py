"""Background readiness for retrieval (stores, embedding model, rules).

The MCP handshake (``initialize``, ``tools/list``) must not wait for the store
load, the embedding model or Langfuse, so Claude Code's connect budget is not
at risk. ``start()`` runs the heavy setup once in a daemon thread and resolves a
``concurrent.futures.Future``. Tools that need retrieval await ``wait()``, which
is capped by ``WARMUP_WAIT_SECONDS`` and never hangs: past the cap it reports
``warming_up``, and a failed initialization is stored and reported as an error.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
import time
from typing import Callable, Optional

from src.engine.config import WARMUP_WAIT_SECONDS

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_future: "concurrent.futures.Future[dict]" = concurrent.futures.Future()
_started = False
_thread: Optional[threading.Thread] = None


def _build_retrievers() -> None:
    from src.engine.enrichment import get_implant_retriever, get_skill_retriever
    get_skill_retriever()
    get_implant_retriever()


def _warmup_embedding_model() -> None:
    from src.engine.embedder import embed_query, embed_texts
    embed_texts(["warmup"])
    embed_query("warmup")


def _warmup_rules(strict: bool) -> None:
    from src.engine.rules import get_rules
    logger.info("Rules layer warmed up: %d rule(s) loaded", len(get_rules(strict=strict)))


def _resolve_version() -> None:
    from src.version import agents_core_version
    agents_core_version()  # one git call, cached, before the first bundle footer needs it


def _tolerant(step: Callable[[], None], label: str) -> Callable[[], None]:
    """stdio starts even when this warmup fails; the first real call reports it."""
    def run() -> None:
        try:
            step()
        except Exception as error:
            logger.warning("%s warmup failed: %s", label, error, exc_info=True)
    return run


def _default_phases(strict: bool = False) -> list[tuple[str, Callable[[], None]]]:
    """The daemon is strict: a failed warmup prevents ready. stdio is lenient."""
    embed = _warmup_embedding_model
    rules = lambda: _warmup_rules(strict)
    if not strict:
        embed, rules = _tolerant(embed, "Embedding model"), _tolerant(rules, "Rules layer")
    return [
        ("version", _tolerant(_resolve_version, "Version")),
        ("retrievers", _build_retrievers), ("embedding_model", embed), ("rules", rules),
    ]


def _run(phases: list[tuple[str, Callable[[], None]]], future: "concurrent.futures.Future[dict]") -> None:
    started = time.monotonic()
    durations: dict[str, float] = {}
    try:
        for name, step in phases:
            phase_started = time.monotonic()
            step()
            durations[name] = round(time.monotonic() - phase_started, 3)
    except BaseException as error:
        logger.error("Readiness failed after %.2fs: %s", time.monotonic() - started, error, exc_info=True)
        future.set_exception(error if isinstance(error, Exception) else RuntimeError(str(error)))
        return
    durations["total"] = round(time.monotonic() - started, 3)
    from src.version import agents_core_version
    logger.info(
        "Readiness complete pid=%d cwd=%s revision=%s durations=%s",
        os.getpid(), os.getcwd(), agents_core_version(), durations,
    )
    future.set_result(durations)


def start(phases: Optional[list[tuple[str, Callable[[], None]]]] = None) -> bool:
    """Start the initializer once; later calls return False and change nothing."""
    global _started, _thread
    with _lock:
        if _started:
            return False
        _started = True
        _thread = threading.Thread(
            target=_run, args=(phases if phases is not None else _default_phases(), _future),
            name="readiness", daemon=True,
        )
        _thread.start()
        return True


def run_blocking() -> dict:
    """Run the phases in the calling thread (HTTP daemon loader); start-once."""
    global _started
    with _lock:
        first = not _started
        _started = True
    if first:
        _run(_default_phases(strict=True), _future)
    return _future.result()


def when_done(callback: Callable[[], None]) -> None:
    """Run *callback* once the initializer finished (either way), in its thread."""
    _future.add_done_callback(lambda _: callback())


def is_ready() -> bool:
    return _future.done() and _future.exception() is None


def is_warming() -> bool:
    """True while a started initializer has not finished."""
    return _started and not _future.done()


def state() -> str:
    """``ready``, ``failed`` or ``warming_up`` (also before ``start``)."""
    if not _future.done():
        return "warming_up"
    return "failed" if _future.exception() is not None else "ready"


async def wait(timeout: Optional[float] = None) -> Optional[str]:
    """Return None when ready, else ``"warming_up"`` or an error message.

    Waits at most *timeout* seconds (default ``WARMUP_WAIT_SECONDS``). When no
    initializer was started (library use, tests) there is nothing to wait for.
    """
    if not _started:
        return None
    cap = WARMUP_WAIT_SECONDS if timeout is None else timeout
    try:
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(_future)), cap)
    except asyncio.TimeoutError as error:
        # On 3.11+ a TimeoutError raised by the initializer itself lands here too.
        if _future.done() and _future.exception() is not None:
            return f"Retrieval initialization failed: {_future.exception()}"
        return "warming_up"
    except Exception as error:
        return f"Retrieval initialization failed: {error}"
    return None


def reset_for_tests() -> None:
    global _future, _started, _thread
    with _lock:
        _future = concurrent.futures.Future()
        _started = False
        _thread = None
