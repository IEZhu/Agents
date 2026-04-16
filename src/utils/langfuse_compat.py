"""
Langfuse compatibility layer.

Provides no-op fallbacks when Langfuse is not configured (missing keys or library).
This allows the MCP server to run without Langfuse for observability.
"""

import logging
import os

logger = logging.getLogger(__name__)

_langfuse_available = False
_langfuse_instance = None


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


try:
    from langfuse import Langfuse as _RealLangfuse
    from langfuse import observe as _real_observe

    # Check if keys are actually configured
    has_keys = bool(os.getenv("LANGFUSE_PUBLIC_KEY")) and bool(os.getenv("LANGFUSE_SECRET_KEY"))

    if has_keys:
        _langfuse_available = True
        _real_observe_ref = _real_observe
        logger.info("Langfuse enabled (keys found)")
    else:
        _real_observe_ref = None
        logger.info("Langfuse disabled (keys not configured)")
except ImportError:
    _real_observe_ref = None
    logger.info("Langfuse disabled (library not installed)")


def observe(*args, **kwargs):
    """Dynamic observe — falls back to no-op if Langfuse init failed at runtime."""
    if _langfuse_available and _real_observe_ref is not None:
        return _real_observe_ref(*args, **kwargs)
    return _noop_decorator(*args, **kwargs)


def is_langfuse_configured() -> bool:
    """True when Langfuse keys are present, the library is importable,
    and client initialization has not failed."""
    return _langfuse_available


def get_langfuse():
    """Returns a real Langfuse client if configured, otherwise a no-op stub."""
    global _langfuse_instance, _langfuse_available
    if _langfuse_instance is not None:
        return _langfuse_instance

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
