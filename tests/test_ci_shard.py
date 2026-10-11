"""Shards cover the full collection exactly once and never silently misconfigure."""
from types import SimpleNamespace

import pytest

from tests import ci_shard


def collection(environment, items, monkeypatch):
    monkeypatch.setattr(ci_shard.os, "environ", environment)
    discarded = []
    config = SimpleNamespace(stash={}, hook=SimpleNamespace(
        pytest_deselected=lambda *, items: discarded.extend(items)))
    ci_shard.pytest_configure(config)
    selected = list(items)
    ci_shard.pytest_collection_modifyitems(config, selected)
    return selected, discarded


def test_all_shards_cover_every_item_once_independent_of_collection_order(monkeypatch):
    items = [SimpleNamespace(nodeid=f"tests/test_sync.py::test_cycle[{number}]") for number in range(110)]
    seen = []
    for index in range(4):
        environment = {"AGENTS_TEST_SHARD_COUNT": "4", "AGENTS_TEST_SHARD_INDEX": str(index)}
        selected, discarded = collection(environment, items, monkeypatch)
        reversed_selected, _ = collection(environment, reversed(items), monkeypatch)
        assert {item.nodeid for item in selected} == {item.nodeid for item in reversed_selected}
        assert selected and discarded
        assert len(selected) + len(discarded) == len(items)
        assert not {item.nodeid for item in selected} & {item.nodeid for item in discarded}
        seen.extend(item.nodeid for item in selected)
    assert sorted(seen) == sorted(item.nodeid for item in items)


@pytest.mark.parametrize("environment", [{}, {"AGENTS_TEST_SHARD_COUNT": "1", "AGENTS_TEST_SHARD_INDEX": "0"}])
def test_default_and_single_shard_preserve_the_full_suite(environment, monkeypatch):
    items = [SimpleNamespace(nodeid="tests/test_sync.py::test_one")]
    selected, discarded = collection(environment, items, monkeypatch)
    assert selected == items and discarded == []


@pytest.mark.parametrize("count,index", [(None, "0"), ("4", None), ("", "0"), ("x", "0"),
    ("4", "x"), ("0", "0"), ("-1", "0"), ("4", "-1"), ("4", "4")])
def test_invalid_configuration_fails_before_collection(count, index, monkeypatch):
    environment = {name: value for name, value in (
        ("AGENTS_TEST_SHARD_COUNT", count), ("AGENTS_TEST_SHARD_INDEX", index)) if value is not None}
    with pytest.raises(pytest.UsageError):
        collection(environment, [], monkeypatch)
