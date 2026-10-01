import pytest

from src.utils.langfuse_compat import keys_configured


@pytest.mark.parametrize("public,secret,expected", [
    ("pk-lf-real", "sk-lf-real", True),
    ("", "", False),
    (None, "sk-lf-real", False),
    ("pk-lf-...", "sk-lf-...", False),  # placeholders from the old env.example
    ("# Optional: observability", "sk-lf-real", False),  # comment parsed as a value
    ("  pk-lf-real  ", "sk-lf-real", True),
])
def test_keys_configured_ignores_empty_and_placeholder_keys(public, secret, expected):
    assert keys_configured(public, secret) is expected


# --- lazy import ---------------------------------------------------------------------------

import asyncio
import sys
import types

from src.utils import langfuse_compat


@pytest.fixture
def clean_compat(monkeypatch):
    monkeypatch.setattr(langfuse_compat, "_initialized", False)
    monkeypatch.setattr(langfuse_compat, "_langfuse_available", False)
    monkeypatch.setattr(langfuse_compat, "_langfuse_instance", None)
    monkeypatch.setattr(langfuse_compat, "_real_observe_ref", None)
    monkeypatch.setattr(langfuse_compat, "_RealLangfuse", None)
    monkeypatch.delitem(sys.modules, "langfuse", raising=False)


def test_without_keys_observe_is_a_noop_and_never_imports_langfuse(clean_compat, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    def fn():
        return 1

    assert langfuse_compat.observe(name="x")(fn) is fn
    assert langfuse_compat.observe(fn) is fn
    assert "langfuse" not in sys.modules
    assert langfuse_compat.is_langfuse_configured() is False
    assert "langfuse" not in sys.modules  # still no import: nothing to enable


def test_with_keys_decorating_defers_the_import_to_the_first_call(clean_compat, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-real")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-real")
    imported = []
    fake = types.ModuleType("langfuse")
    fake.Langfuse = object

    def real_observe(*args, **kwargs):
        def decorate(fn):
            def traced(*a, **k):
                imported.append("traced")
                return fn(*a, **k)
            return traced
        return decorate

    fake.observe = real_observe


    @langfuse_compat.observe(name="sync")
    def sync_fn(x):
        return x + 1

    @langfuse_compat.observe(name="async")
    async def async_fn(x):
        return x * 2

    assert "langfuse" not in sys.modules and not langfuse_compat._initialized
    assert asyncio.iscoroutinefunction(async_fn) and not asyncio.iscoroutinefunction(sync_fn)
    monkeypatch.setitem(sys.modules, "langfuse", fake)
    assert sync_fn(1) == 2 and asyncio.run(async_fn(2)) == 4
    assert imported == ["traced", "traced"]
    assert langfuse_compat._initialized and langfuse_compat.is_langfuse_configured()


def test_flush_if_initialized_never_creates_a_client(clean_compat):
    langfuse_compat.flush_if_initialized()
    assert langfuse_compat._langfuse_instance is None
