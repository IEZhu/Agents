"""The warmup duration estimate: learning across starts, the hint it phrases, and failing quietly."""
import asyncio
import json
import math

import pytest

import src.server as server
from src.engine import readiness, warmup_estimate
from src.estimation.kalman import SECONDS_PER_DAY

DAY = SECONDS_PER_DAY


@pytest.fixture
def estimate_file(tmp_path, monkeypatch):
    path = tmp_path / "readiness" / "warmup-estimate.json"
    monkeypatch.setattr(warmup_estimate, "state_path", lambda: path)
    return path


def test_no_hint_before_the_first_recorded_start(estimate_file):
    assert warmup_estimate.typical() is None
    assert warmup_estimate.retry_hint(3.0) is None


def test_repeated_starts_converge_on_their_duration(estimate_file):
    for index in range(30):
        warmup_estimate.record(4.0, now=index * DAY)
    usual, upper = warmup_estimate.typical(now=30 * DAY)
    assert usual == pytest.approx(4.0, rel=0.05)
    assert upper > usual


def test_one_slow_start_moves_the_estimate_only_part_of_the_way(estimate_file):
    for index in range(20):
        warmup_estimate.record(4.0, now=index * DAY)
    warmup_estimate.record(400.0, now=20 * DAY)
    usual, _ = warmup_estimate.typical(now=20 * DAY)
    assert 4.0 < usual < 40.0


def test_a_lasting_change_is_followed(estimate_file):
    for index in range(20):
        warmup_estimate.record(4.0, now=index * DAY)
    for index in range(20, 40):
        warmup_estimate.record(30.0, now=index * DAY)
    usual, _ = warmup_estimate.typical(now=40 * DAY)
    assert usual == pytest.approx(30.0, rel=0.15)


def test_the_hint_counts_down_from_the_elapsed_time(estimate_file):
    for index in range(30):
        warmup_estimate.record(12.0, now=index * DAY)
    hint = warmup_estimate.retry_hint(5.0, now=30 * DAY)
    assert "about 12 s" in hint and "retry in about 7 s" in hint
    late = warmup_estimate.retry_hint(500.0, now=30 * DAY)
    assert "longer than usual" in late


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf, "3"])
def test_impossible_durations_are_ignored(estimate_file, bad):
    warmup_estimate.record(bad)
    assert not estimate_file.exists()


def test_an_unreadable_state_file_means_no_hint(estimate_file):
    estimate_file.parent.mkdir(parents=True)
    estimate_file.write_text("{not json", encoding="utf-8")
    assert warmup_estimate.typical() is None
    warmup_estimate.record(5.0)  # and the next start starts over instead of failing
    assert json.loads(estimate_file.read_text(encoding="utf-8"))["starts"] == 1


@pytest.mark.parametrize("content", ["[]", '"x"', '{"v": 1, "estimate": {"mean": 1, "variance": 1, "at": 0}, '
                                     '"starts": Infinity}', '{"v": 1, "estimate": [], "starts": 1}'])
def test_any_unreadable_state_is_replaced_by_the_next_start(estimate_file, content):
    estimate_file.parent.mkdir(parents=True)
    estimate_file.write_text(content, encoding="utf-8")
    assert warmup_estimate.retry_hint(1.0) is None
    warmup_estimate.record(5.0)
    assert json.loads(estimate_file.read_text(encoding="utf-8"))["starts"] == 1


def test_a_held_lock_skips_the_reading_instead_of_waiting(estimate_file):
    from src.file_lock import file_lock

    estimate_file.parent.mkdir(parents=True)
    with file_lock(estimate_file.with_name(estimate_file.name + ".lock")):
        warmup_estimate.record(5.0)
    assert not estimate_file.exists()


def test_a_start_with_a_tolerated_failure_is_not_recorded(estimate_file):
    readiness.reset_for_tests()
    try:
        failing = readiness._tolerant(lambda: (_ for _ in ()).throw(RuntimeError("no model")), "Embedding model")
        readiness.start([("embedding_model", failing)])
        assert readiness.join(10) and readiness.state() == "ready"
        assert not estimate_file.exists()
    finally:
        readiness.reset_for_tests()


def test_readiness_records_each_successful_start(estimate_file):
    readiness.reset_for_tests()
    try:
        readiness.start([("noop", lambda: None)])
        assert readiness.join(10)
        assert json.loads(estimate_file.read_text(encoding="utf-8"))["starts"] == 1
        assert readiness.elapsed() is None  # finished
    finally:
        readiness.reset_for_tests()


def test_a_failed_start_records_nothing(estimate_file):
    readiness.reset_for_tests()
    try:
        readiness.start([("boom", lambda: (_ for _ in ()).throw(RuntimeError("boom")))])
        assert readiness.join(10)
        assert not estimate_file.exists()
    finally:
        readiness.reset_for_tests()


def test_the_server_hint_adds_the_estimate_and_falls_back_without_it(estimate_file, monkeypatch):
    assert server._warming_up_hint() == server.WARMING_UP_HINT
    for index in range(5):
        warmup_estimate.record(8.0, now=index * DAY)
    assert server._warming_up_hint().startswith(server.WARMING_UP_HINT + " Starts here usually take")
    monkeypatch.setattr(warmup_estimate, "retry_hint", lambda *_: (_ for _ in ()).throw(RuntimeError("x")))
    assert server._warming_up_hint() == server.WARMING_UP_HINT
