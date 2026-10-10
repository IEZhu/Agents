"""Opt-in deterministic pytest sharding for slow native Git integration checks."""
from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping

import pytest

_SHARD = pytest.StashKey[tuple[int, int] | None]()


def shard_settings(environment: Mapping[str, str]) -> tuple[int, int] | None:
    count = environment.get("AGENTS_TEST_SHARD_COUNT")
    index = environment.get("AGENTS_TEST_SHARD_INDEX")
    if count is None and index is None:
        return None
    try:
        total, part = int(count), int(index)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError("Set both AGENTS_TEST_SHARD_COUNT and AGENTS_TEST_SHARD_INDEX to integers") from exc
    if total < 1 or not 0 <= part < total:
        raise pytest.UsageError("AGENTS_TEST_SHARD_COUNT must be positive and 0 <= AGENTS_TEST_SHARD_INDEX < count")
    return total, part


def shard_for(nodeid: str, count: int) -> int:
    return int.from_bytes(hashlib.sha256(nodeid.encode("utf-8")).digest(), "big") % count


def pytest_configure(config):
    config.stash[_SHARD] = shard_settings(os.environ)


def pytest_collection_modifyitems(config, items):
    settings = config.stash[_SHARD]
    if settings is None:
        return
    count, index = settings
    selected, deselected = [], []
    for item in items:
        (selected if shard_for(item.nodeid, count) == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
