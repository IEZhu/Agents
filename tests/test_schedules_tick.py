"""The scheduler tick on a fake clock: slots, catch-up, missed slots, continuations and blockers (#338)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.schedules.config import DEFAULTS
from src.schedules.tick import decide, next_slot, within
from src.schedules.config import hour_ranges

ZONE = timezone(timedelta(hours=3))


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE)


def config(**changes):
    return {**DEFAULTS, "enabled": True, **changes}


ENABLED = {"enabled_at": at(12, 0).isoformat()}
LONG_ENABLED = {"enabled_at": at(10, 12).isoformat()}


def test_disabled_schedule_does_nothing():
    decision = decide(at(12, 2, 20), {**DEFAULTS, "enabled": False}, ENABLED)
    assert decision.action == "idle" and decision.reason == "the schedule is disabled"
    assert decision.state["last_tick"] == at(12, 2, 20).isoformat()


def test_slot_runs_once_within_the_catch_up_window():
    first = decide(at(12, 2, 20), config(), ENABLED)
    assert first.action == "run" and first.trigger == {"kind": "slot", "at": at(12, 2, 17).isoformat()}
    assert first.state["last_slot"] == at(12, 2, 17).isoformat()
    # Five minutes later the same slot is handled: nothing is due.
    second = decide(at(12, 2, 25), config(), first.state)
    assert second.action == "idle" and second.trigger is None


def test_slot_before_its_time_is_not_due():
    assert decide(at(12, 2, 16), config(), ENABLED).action == "idle"


@pytest.mark.parametrize("minutes_late, action", [(0, "run"), (119, "run"), (120, "run"), (121, "idle")])
def test_catch_up_window_edge(minutes_late, action):
    now = at(12, 2, 17) + timedelta(minutes=minutes_late)
    decision = decide(now, config(), ENABLED)
    assert decision.action == action
    if action == "idle":
        assert decision.missed == [at(12, 2, 17).isoformat()]


def test_slots_missed_while_asleep_are_recorded_as_missed():
    state = {**LONG_ENABLED, "last_slot": at(11, 2, 17).isoformat()}
    decision = decide(at(12, 9, 0), config(), state)  # asleep through 11 Oct 13:47 and 12 Oct 02:17
    assert decision.action == "idle"
    assert decision.missed == [at(11, 13, 47).isoformat(), at(12, 2, 17).isoformat()]
    assert decision.state["last_slot"] == at(12, 2, 17).isoformat()
    assert decision.state["missed"] == decision.missed
    # The next tick does not record them again.
    assert decide(at(12, 9, 5), config(), decision.state).missed == []


def test_missed_and_due_slots_in_one_tick():
    state = {**LONG_ENABLED, "last_slot": at(11, 13, 47).isoformat()}
    decision = decide(at(12, 13, 50), config(), state)
    assert decision.action == "run" and decision.trigger["at"] == at(12, 13, 47).isoformat()
    assert decision.missed == [at(12, 2, 17).isoformat()]


def test_slots_before_enabling_never_count():
    state = {"enabled_at": at(12, 2, 30).isoformat()}
    decision = decide(at(12, 2, 35), config(), state)
    assert decision.action == "idle" and decision.missed == []


def test_missed_list_is_bounded():
    state = {**LONG_ENABLED, "last_slot": at(11, 2, 17).isoformat(), "missed": ["x"] * 30}
    decision = decide(at(12, 9, 0), config(), state)
    assert len(decision.state["missed"]) == 20 and decision.state["missed"][-1] == at(12, 2, 17).isoformat()


def test_a_busy_lease_keeps_the_slot_for_the_next_tick_until_it_is_missed():
    busy = decide(at(12, 2, 20), config(), ENABLED, lease_free=lambda: False)
    assert busy.action == "skip" and "run lease" in busy.reason
    assert "last_slot" not in busy.state
    retried = decide(at(12, 2, 25), config(), busy.state)
    assert retried.action == "run" and retried.trigger["at"] == at(12, 2, 17).isoformat()
    late = decide(at(12, 4, 30), config(), busy.state, lease_free=lambda: False)
    assert late.action == "idle" and late.missed == [at(12, 2, 17).isoformat()]


@pytest.mark.parametrize("probe, reason", [
    ({"update_pending": lambda: True}, "daemon update"),
    ({"heavy_processes": lambda: ["4242 pytest"]}, "heavy processes are running: 4242 pytest"),
    ({"on_ac_power": lambda: False}, "battery"),
])
def test_blockers_skip_a_due_slot(probe, reason):
    decision = decide(at(12, 2, 20), config(), ENABLED, **probe)
    assert decision.action == "skip" and reason in decision.reason


def test_ac_power_is_optional_and_unknown_power_does_not_block():
    assert decide(at(12, 2, 20), config(require_ac_power=False), ENABLED,
                  on_ac_power=lambda: False).action == "run"
    assert decide(at(12, 2, 20), config(), ENABLED, on_ac_power=lambda: None).action == "run"


def test_probes_run_only_when_something_is_due():
    def fail():
        raise AssertionError("a probe ran on an empty tick")
    assert decide(at(12, 9, 0), config(), ENABLED, lease_free=fail, update_pending=fail,
                  heavy_processes=fail, on_ac_power=fail).action == "idle"


def continuation(when):
    return {**LONG_ENABLED, "last_slot": at(12, 2, 17).isoformat(), "next_attempt": {"at": when.isoformat(), "pr": 400}}


def test_continuation_runs_at_next_attempt_within_allowed_hours():
    state = continuation(at(12, 10, 0))
    assert decide(at(12, 9, 55), config(), state).action == "idle"
    decision = decide(at(12, 10, 0), config(), state)
    assert decision.action == "run"
    assert decision.trigger == {"kind": "continuation", "at": at(12, 10, 0).isoformat(), "pr": 400}
    assert decide(at(12, 10, 5), config(), decision.state).action == "idle"  # handled once


@pytest.mark.parametrize("hour, minute, action", [
    (23, 30, "idle"),  # between the ranges 08:00-23:00 and 00:00-06:00
    (0, 0, "run"),
    (5, 59, "run"),
    (6, 0, "idle"),
    (7, 59, "idle"),
    (8, 0, "run"),
])
def test_continuation_respects_the_default_allowed_hours(hour, minute, action):
    now = at(12, hour, minute) if hour >= 8 else at(13, hour, minute)
    state = continuation(at(11, 22, 0))
    state["last_slot"] = now.isoformat()  # no slot in the way
    decision = decide(now, config(), state)
    assert decision.action == action
    if action == "idle":
        assert decision.reason == "a continuation is due outside the allowed hours"
        assert "last_continuation" not in decision.state


@pytest.mark.parametrize("hour, inside", [(21, False), (22, True), (23, True), (0, True), (1, True), (2, False)])
def test_allowed_hours_crossing_midnight(hour, inside):
    assert within(hour_ranges("22:00-02:00"), at(12, hour, 30)) is inside


def test_a_due_slot_wins_over_a_continuation_which_waits_for_the_next_tick():
    state = continuation(at(12, 13, 0))
    state["last_slot"] = at(12, 2, 17).isoformat()
    first = decide(at(12, 13, 50), config(), state)
    assert first.trigger["kind"] == "slot"
    second = decide(at(12, 13, 55), config(), first.state)
    assert second.trigger["kind"] == "continuation"


def test_next_slot_and_naive_time_refused():
    assert next_slot(DEFAULTS["times"], at(12, 2, 17)) == at(12, 13, 47)
    assert next_slot(DEFAULTS["times"], at(12, 23, 0)) == at(13, 2, 17)
    with pytest.raises(ValueError):
        decide(datetime(2026, 10, 12, 2, 20), config(), ENABLED)
