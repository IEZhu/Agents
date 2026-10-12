"""The tick decision (design 10.1): a pure function of the clock, the configuration and the tick state.

The scheduler starts ``python -m src.schedules tick`` every 300 seconds. A tick starts a run when
the schedule is enabled and either

* a slot of ``times`` (local time) has come and is no older than ``catch_up_minutes``, or
* a continuation's ``next_attempt`` has come and the current time is within ``allowed_hours``
  (a range may cross midnight, such as ``22:00-02:00``),

and then only when the run lease is free, no daemon update is in progress, no other heavy
process runs and, with ``require_ac_power``, the machine is not on battery. A slot that is older
than the catch-up window when a tick first sees it is recorded as ``missed``; a due slot that a
busy machine keeps from running is retried by every tick until it becomes ``missed``.

The tick state (``<state_dir>/schedules/tick.json``) holds ``enabled_at`` (slots before it never
count), ``last_slot`` (the newest slot run or missed), ``last_continuation`` (the newest
``next_attempt`` handled), ``next_attempt`` (``{"at": ISO time, "pr": N}``, written by the run
sequence of #347) and a bounded ``missed`` list. Slots are local wall-clock times in the offset of
``now``; around a daylight-saving change a slot may shift by the change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable, Mapping

from .config import hour_ranges, minutes

MISSED_KEPT = 20
MAX_LOOKBACK = timedelta(days=7)


@dataclass(frozen=True)
class TickDecision:
    action: str  # "run", "skip" (something was due but blocked) or "idle" (nothing due)
    reason: str
    state: dict
    trigger: dict | None = None
    missed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"action": self.action, "reason": self.reason, "trigger": self.trigger, "missed": self.missed}


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def within(ranges: list[tuple[int, int]], moment: datetime) -> bool:
    """Whether ``moment``'s local time of day lies in one of ``ranges`` (end exclusive)."""
    now = moment.hour * 60 + moment.minute
    for start, end in ranges:
        if start < end and start <= now < end:
            return True
        if start > end and (now >= start or now < end):  # crosses midnight
            return True
        if start == end:  # the whole day
            return True
    return False


def slots_between(times: list[str], since: datetime, now: datetime) -> list[datetime]:
    """Slot instances in ``(since, now]``, oldest first, in ``now``'s offset."""
    found = []
    day = max(since, now - MAX_LOOKBACK).date()
    while day <= now.date():
        for value in times:
            minute = minutes(value)
            slot = datetime.combine(day, time(minute // 60, minute % 60), tzinfo=now.tzinfo)
            if since < slot <= now:
                found.append(slot)
        day += timedelta(days=1)
    return sorted(found)


def next_slot(times: list[str], now: datetime) -> datetime:
    """The first slot after ``now``."""
    return slots_between(times, now, now + timedelta(days=1, minutes=1))[0]


def _always() -> bool:
    return True


def _never() -> bool:
    return False


def _nothing() -> list:
    return []


def _unknown() -> None:
    return None


def decide(now: datetime, config: Mapping, state: Mapping | None, *,
           lease_free: Callable[[], bool] = _always,
           update_pending: Callable[[], bool] = _never,
           heavy_processes: Callable[[], list] = _nothing,
           on_ac_power: Callable[[], bool | None] = _unknown) -> TickDecision:
    """One tick. ``now`` is an aware local time; the probes run only when something is due."""
    if now.tzinfo is None:
        raise ValueError("now must be an aware datetime")
    state = dict(state or {})
    new_state = {**state, "last_tick": iso(now)}
    if not config.get("enabled"):
        return TickDecision("idle", "the schedule is disabled", new_state)

    catch_up = timedelta(minutes=config["catch_up_minutes"])
    marks = [parse_time(state.get("last_slot")), parse_time(state.get("enabled_at"))]
    marks = [mark for mark in marks if mark is not None]
    since = max(marks) if marks else now - catch_up
    slots = slots_between(config["times"], since, now)
    missed = [slot for slot in slots if now - slot > catch_up]
    due = [slot for slot in slots if now - slot <= catch_up]
    if len(due) > 1:  # only the newest due slot runs; the ones it supersedes count as missed
        missed, due = missed + due[:-1], due[-1:]
    if missed:
        new_state["last_slot"] = iso(max(missed))
        new_state["missed"] = (list(state.get("missed") or []) + [iso(slot) for slot in missed])[-MISSED_KEPT:]
    missed_iso = [iso(slot) for slot in missed]

    trigger, idle_reason = None, "no slot or continuation is due"
    if due:
        trigger = {"kind": "slot", "at": iso(due[0])}
    else:
        attempt = state.get("next_attempt") if isinstance(state.get("next_attempt"), dict) else {}
        at, handled = parse_time(attempt.get("at")), parse_time(state.get("last_continuation"))
        if at is not None and at <= now and (handled is None or at > handled):
            if within(hour_ranges(config["allowed_hours"]), now):
                trigger = {"kind": "continuation", "at": iso(at), "pr": attempt.get("pr")}
            else:
                idle_reason = "a continuation is due outside the allowed hours"
    if trigger is None:
        return TickDecision("idle", idle_reason, new_state, None, missed_iso)

    blockers = []
    if update_pending():
        blockers.append("a daemon update or maintenance is in progress")
    if not lease_free():
        blockers.append("another run holds the run lease")
    heavy = heavy_processes()
    if heavy:
        blockers.append(f"heavy processes are running: {', '.join(map(str, heavy))}")
    if config.get("require_ac_power") and on_ac_power() is False:
        blockers.append("the machine is on battery power")
    if blockers:
        return TickDecision("skip", "; ".join(blockers), new_state, trigger, missed_iso)

    if trigger["kind"] == "slot":
        new_state["last_slot"] = trigger["at"]
    else:
        new_state["last_continuation"] = trigger["at"]
    return TickDecision("run", f"{trigger['kind']} due at {trigger['at']}", new_state, trigger, missed_iso)
