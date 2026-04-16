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
        observe = _real_observe
        logger.info("Langfuse enabled (keys found)")
    else:
        observe = _noop_decorator
        logger.info("Langfuse disabled (keys not configured)")
except ImportError:
    observe = _noop_decorator
    logger.info("Langfuse disabled (library not installed)")


def is_langfuse_configured() -> bool:
    """True when Langfuse keys are present and the library is importable."""
    return _langfuse_available


def get_langfuse():
    """Returns a real Langfuse client if configured, otherwise a no-op stub."""
    global _langfuse_instance
    if _langfuse_instance is not None:
        return _langfuse_instance

    if _langfuse_available:
        try:
            _langfuse_instance = _RealLangfuse()
            logger.info("Langfuse client initialized")
        except Exception as e:
            global _langfuse_available
            _langfuse_available = False
            logger.warning(f"Langfuse init failed, using no-op: {e}")
            _langfuse_instance = _NoopLangfuse()
    else:
        _langfuse_instance = _NoopLangfuse()

    return _langfuse_instance
