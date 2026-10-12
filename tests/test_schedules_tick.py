"""The scheduler tick on a fake clock: slots, catch-up, missed slots, continuations and blockers (#338)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import pytest

from src.schedules import runner
from src.schedules.config import DEFAULTS
from src.schedules.tick import decide, local_zone, next_slot, within
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
    assert busy.action == "skip" and busy.reason == "an autodev run or a daemon update holds the run lease"
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


# --- a clock that was ahead (review F1) ---------------------------------------------------------------

@pytest.mark.parametrize("mark", ["last_slot", "enabled_at"])
def test_a_mark_stored_by_a_clock_that_was_ahead_does_not_silence_the_schedule(mark):
    ahead = datetime(2027, 1, 1, 13, 47, tzinfo=ZONE).isoformat()
    state = {**LONG_ENABLED, "last_slot": at(12, 2, 17).isoformat(), mark: ahead}
    decision = decide(at(12, 13, 50), config(), state)
    assert decision.action == "run" and decision.trigger["at"] == at(12, 13, 47).isoformat()
    assert decision.reset == {"at": at(12, 13, 50).isoformat(), "marks": {mark: ahead}}
    assert decision.to_dict()["reset"] == decision.reset and decision.state["clock_reset"] == decision.reset
    assert decision.state["last_slot"] == at(12, 13, 47).isoformat()
    # Stored again with a time that is not in the future: the next tick has nothing to reset.
    later = decide(at(12, 13, 55), config(), decision.state)
    assert later.action == "idle" and later.reset is None and "reset" not in later.to_dict()


def test_a_continuation_mark_from_a_clock_that_was_ahead_is_reset():
    state = continuation(at(12, 10, 0))
    state["last_continuation"] = datetime(2027, 1, 1, tzinfo=ZONE).isoformat()
    decision = decide(at(12, 10, 0), config(), state)
    assert decision.action == "run" and decision.trigger["kind"] == "continuation"
    assert list(decision.reset["marks"]) == ["last_continuation"]


# --- daylight saving in a real zone (review F2) ------------------------------------------------------

BERLIN = ZoneInfo("Europe/Berlin")


def ticks(start_utc, hours, state, *, fixed_offset=False):
    """Every five minutes from ``start_utc``, as the scheduler ticks; the runs and the last state."""
    runs = []
    for step in range(hours * 12):
        now = (start_utc + timedelta(minutes=5 * step)).astimezone(BERLIN)
        decision = decide(now, config(), state)
        state = decision.state
        if decision.action == "run":
            runs.append((now.isoformat(), decision.trigger["at"]))
    return runs, state


def test_a_slot_in_the_repeated_hour_of_a_fall_back_night_runs_once():
    # 2026-10-25 in Berlin: 03:00 CEST becomes 02:00 CET, so 02:17 happens twice.
    runs, _ = ticks(datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc), 8,
                    {"enabled_at": "2026-10-24T12:00:00+02:00"})
    assert runs == [("2026-10-25T02:20:00+02:00", "2026-10-25T02:17:00+02:00")]


def test_a_slot_skipped_by_spring_forward_runs_once_an_hour_later():
    # 2026-03-29 in Berlin: 02:00 CET becomes 03:00 CEST, so 02:17 does not exist.
    runs, _ = ticks(datetime(2026, 3, 28, 22, 0, tzinfo=timezone.utc), 8,
                    {"enabled_at": "2026-03-28T12:00:00+01:00"})
    assert runs == [("2026-03-29T03:20:00+02:00", "2026-03-29T03:17:00+02:00")]


def test_the_catch_up_window_counts_real_time_across_the_fall_back_hour():
    # 02:17 CEST was 2 h 59 min before 04:16 CET, although the clock shows 1 h 59 min.
    now = datetime(2026, 10, 25, 3, 16, tzinfo=timezone.utc).astimezone(BERLIN)
    assert now.isoformat() == "2026-10-25T04:16:00+01:00"
    decision = decide(now, config(), {"enabled_at": "2026-10-24T14:00:00+02:00"})
    assert decision.action == "idle" and decision.missed == ["2026-10-25T02:17:00+02:00"]


def test_the_clock_of_the_runner_carries_the_local_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        now = runner.Context(installation=Path("/nonexistent"), directory=Path("/nonexistent")).clock()
        assert getattr(now.tzinfo, "key", None) == "Europe/Berlin"
        assert local_zone() == BERLIN
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_next_slot_across_a_25_hour_day():
    now = datetime(2026, 10, 24, 2, 18, tzinfo=BERLIN)
    assert next_slot(["02:17"], now).isoformat() == "2026-10-25T02:17:00+02:00"
