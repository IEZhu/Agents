"""
Langfuse compatibility layer.

Provides no-op fallbacks when Langfuse is not configured (missing keys or library).
This allows the MCP server to run without Langfuse for observability.
"""

import asyncio
import functools
import inspect
import logging
import os
import threading

logger = logging.getLogger(__name__)

_langfuse_available = False
_langfuse_instance = None

# env.example shipped these placeholders until 2026-10, and setup copied them
# into .env unchanged. python-dotenv before 1.2.4 also reads "KEY=  # text" as
# "# text". Neither is a real key, so neither enables tracing.
_PLACEHOLDER_KEYS = frozenset({"pk-lf-...", "sk-lf-..."})


def keys_configured(public_key: str | None, secret_key: str | None) -> bool:
    """True when both Langfuse keys are set to something other than a placeholder."""
    def real(value: str | None) -> bool:
        value = (value or "").strip()
        return bool(value) and value not in _PLACEHOLDER_KEYS and not value.startswith("#")
    return real(public_key) and real(secret_key)


def _noop_decorator(*args, **kwargs):
    """No-op decorator that returns the function unchanged."""
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda fn: fn


class _NoopLangfuse:
    """Stub that silently ignores all Langfuse calls."""

    def flush(self):
        pass

    def create_trace_id(self, seed=None):
        return seed or "noop"

    def start_as_current_observation(self, **kwargs):
        return _NoopContext()


class _NoopContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def update(self, **kwargs):
        pass


_init_lock = threading.Lock()
_initialized = False
_RealLangfuse = None
_real_observe_ref = None


def _has_keys() -> bool:
    return keys_configured(os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY"))


def _init() -> None:
    """Import the langfuse library on first real use, never at module import.

    The import is slow, and the MCP handshake must not wait for it. Without
    keys the library is not imported at all.
    """
    global _initialized, _langfuse_available, _RealLangfuse, _real_observe_ref
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        if not _has_keys():
            logger.info("Langfuse disabled (keys not configured)")
        else:
            try:
                from langfuse import Langfuse as real_langfuse
                from langfuse import observe as real_observe
                _RealLangfuse, _real_observe_ref = real_langfuse, real_observe
                _langfuse_available = True
                logger.info("Langfuse enabled (keys found)")
            except ImportError:
                logger.info("Langfuse disabled (library not installed)")
        _initialized = True


def observe(*args, **kwargs):
    """Lazy ``observe``: a no-op without keys, otherwise it resolves the real
    decorator on the first call so decorating at import never loads langfuse."""
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return observe()(args[0])
    if not _has_keys():
        return _noop_decorator(*args, **kwargs)

    def decorate(fn):
        resolved = []

        def resolve():
            if not resolved:
                _init()
                resolved.append(_real_observe_ref(*args, **kwargs)(fn) if _real_observe_ref else fn)
            return resolved[0]

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **k):
                if not _initialized:
                    from src.engine import readiness
                    if readiness.is_warming() or readiness.state() == "failed":  # untraced: the slow import must not delay the warming_up or init-failure answer
                        return await fn(*a, **k)
                    await asyncio.to_thread(_init)  # keep the import off the event loop
                return await resolve()(*a, **k)
        else:
            @functools.wraps(fn)
            def wrapper(*a, **k):
                return resolve()(*a, **k)
        return wrapper

    return decorate


def is_langfuse_configured() -> bool:
    """True when Langfuse keys are present, the library is importable,
    and client initialization has not failed. May import the library."""
    _init()
    return _langfuse_available


def get_langfuse():
    """Returns a real Langfuse client if configured, otherwise a no-op stub."""
    global _langfuse_instance, _langfuse_available
    if _langfuse_instance is not None:
        return _langfuse_instance

    _init()
    if _langfuse_available:
        try:
            _langfuse_instance = _RealLangfuse()
            logger.info("Langfuse client initialized")
        except Exception as e:
            _langfuse_available = False
            logger.warning(f"Langfuse init failed, using no-op: {e}")
            _langfuse_instance = _NoopLangfuse()
    else:
        _langfuse_instance = _NoopLangfuse()

    return _langfuse_instance


def flush_if_initialized() -> None:
    """Flush a real client when one exists; never imports langfuse just to exit."""
    if _langfuse_instance is not None:
        try:
            _langfuse_instance.flush()
        except Exception as e:
            logger.warning("Langfuse flush failed: %s", e)
