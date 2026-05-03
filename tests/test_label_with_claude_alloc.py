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

from evals.scripts.label_with_claude import (
    DEFAULT_ALLOC,
    cmd_preview,
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


class TestCmdPreview:
    """Smoke coverage for ``cmd_preview`` allocation handling.

    ``preview=0`` exercises the short-circuit branch (``alloc = {}`` →
    ``iter_samples`` yields nothing → no HF fetch). This works even without
    the optional ``datasets`` extra, which is the whole point of the
    short-circuit: zero rows means zero loads.
    """

    def test_preview_zero_short_circuits_with_no_samples(self, capsys):
        rc = cmd_preview(_ns(preview=0))
        assert rc == 0
        captured = capsys.readouterr()
        # Zero allocation → "samples: 0" in the human-readable summary.
        assert "samples: 0" in captured.out

    def test_preview_negative_short_circuits_with_no_samples(self, capsys):
        rc = cmd_preview(_ns(preview=-3))
        assert rc == 0
        captured = capsys.readouterr()
        assert "samples: 0" in captured.out

    def test_preview_with_source_not_in_default_alloc_does_not_crash(self, capsys, monkeypatch):
        """Regression: ``--source X --preview N`` for a source registered in
        ``DATASETS`` but missing from ``DEFAULT_ALLOC`` previously crashed with
        ``StopIteration`` because ``resolve_alloc`` filtered out the zero-valued
        entry and ``next(iter(alloc))`` was called on the empty dict.

        We simulate the condition by adding a fake source to ``DATASETS`` (via
        monkeypatch) without touching ``DEFAULT_ALLOC``. Sampling itself fails
        since the fake source isn't a real HF dataset, but the failure must
        come from the fetch path (``RuntimeError`` from a real HF lookup or
        a clean per-sample fetch_error) — never from ``next(iter({}))``.
        """
        from evals.scripts import label_with_claude as mod

        # The crash we're regression-testing happens before any fetch — when
        # `cmd_preview` builds `alloc`. Patch `iter_samples` so we never reach
        # the network/HF path and can assert the alloc contains the source.
        captured_alloc: dict[str, int] = {}

        def _fake_iter_samples(alloc, seed):
            captured_alloc.update(alloc)
            return []

        monkeypatch.setattr(mod, "iter_samples", _fake_iter_samples)
        # Pretend `fakeset` is a registered DatasetSpec (argparse choices come
        # from `sorted(DATASETS)`, so we'd normally need to pass a real key,
        # but `cmd_preview` only inspects `args.source` as a string).
        rc = cmd_preview(_ns(source="fakeset", preview=2))
        assert rc == 0
        assert captured_alloc == {"fakeset": 2}
