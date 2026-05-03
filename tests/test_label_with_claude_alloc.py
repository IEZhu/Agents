"""Edge cases for ``evals.scripts.label_with_claude.resolve_alloc`` and
``cmd_preview`` allocation resolution.

Regression coverage for the falsy-zero bug reported on PR #48: ``--limit 0``
and ``--preview 0`` were silently ignored because the code used truthiness
(``if args.limit:``) instead of explicit ``is not None`` checks. The full
default allocation was returned, so the runner fetched 110 samples instead
of zero.
"""

from __future__ import annotations

import argparse

import pytest

datasets_mod = pytest.importorskip("datasets")  # evals optional dep

from evals.scripts.label_with_claude import (  # noqa: E402
    DEFAULT_ALLOC,
    resolve_alloc,
)


def _ns(**kwargs) -> argparse.Namespace:
    base = {"source": None, "limit": None, "preview": None, "seed": 42}
    base.update(kwargs)
    return argparse.Namespace(**base)


class TestResolveAllocLimit:
    def test_limit_none_returns_default(self):
        alloc = resolve_alloc(_ns(limit=None))
        assert alloc == DEFAULT_ALLOC

    def test_limit_zero_returns_zero_allocation(self):
        alloc = resolve_alloc(_ns(limit=0))
        assert sum(alloc.values()) == 0
        assert set(alloc) == set(DEFAULT_ALLOC)

    def test_limit_negative_returns_zero_allocation(self):
        alloc = resolve_alloc(_ns(limit=-5))
        assert sum(alloc.values()) == 0

    def test_limit_positive_scales_proportionally(self):
        alloc = resolve_alloc(_ns(limit=10))
        assert sum(alloc.values()) == 10

    def test_limit_with_source_restricts_to_one_bucket(self):
        alloc = resolve_alloc(_ns(source="wildbench", limit=3))
        assert list(alloc) == ["wildbench"]
        assert sum(alloc.values()) == 3
