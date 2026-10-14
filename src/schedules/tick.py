"""The tick decision (design 10.1): a pure function of the clock, the configuration and the tick state.

The scheduler starts ``python -m src.schedules tick`` every 300 seconds. A tick starts a run when
the schedule is enabled and either

* a slot of ``times`` (local time) has come and is no older than ``catch_up_minutes``, or
* a continuation's ``next_attempt`` has come and the current time is within ``allowed_hours``
  (a range may cross midnight, such as ``22:00-02:00``),

and then only when the run lease is free, no daemon update is in progress, no session holds the
heavy-process lease (`src.schedules.heavy`), no other heavy process runs (that probe fails
open: when ``ps`` fails it sees none, unlike a lease grant) and, with
``require_ac_power``, the machine is not on battery. A slot that is older
than the catch-up window when a tick first sees it is recorded as ``missed``; a due slot that a
busy machine keeps from running is retried by every tick until it becomes ``missed``.

The tick state (``<state_dir>/schedules/tick.json``) holds ``enabled_at`` (slots before it never
count), ``last_slot`` (the newest slot run or missed), ``last_continuation`` (the newest
``next_attempt`` handled), ``next_attempt`` (``{"at": ISO time, "pr": N}``, written by the run
sequence after a submit stage that waits for bots, `src.schedules.sequence`) and a bounded
``missed`` list.

Slots are wall-clock times in the local time zone (`local_zone`, a ``zoneinfo`` zone), and all
arithmetic is on instants. A wall-clock time that a daylight-saving fall-back repeats counts once,
at its first occurrence; one that a spring-forward skips runs at the instant it maps to (an hour
later on the clock). A mark later than ``now`` (``last_slot``, ``enabled_at``,
``last_continuation``) can only come from a clock that was ahead; it is replaced by
``now - catch_up_minutes`` and the reset is recorded as ``clock_reset``, so the schedule is not
silenced until the clock reaches the bad mark.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo
import os
from typing import Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import hour_ranges, minutes

MISSED_KEPT = 20
MAX_LOOKBACK = timedelta(days=7)
MARKS = ("last_slot", "enabled_at", "last_continuation")
LOCALTIME = "/etc/localtime"


@dataclass(frozen=True)
class TickDecision:
    action: str  # "run", "skip" (something was due but blocked) or "idle" (nothing due)
    reason: str
    state: dict
    trigger: dict | None = None
    missed: list[str] = field(default_factory=list)
    reset: dict | None = None  # marks later than now that this tick replaced

    def to_dict(self) -> dict:
        found = {"action": self.action, "reason": self.reason, "trigger": self.trigger, "missed": self.missed}
        if self.reset:
            found["reset"] = self.reset
        return found


def local_zone() -> tzinfo:
    """The local time zone as a ``zoneinfo`` zone (``TZ``, else ``/etc/localtime``), so that local
    times know their daylight-saving fold; a fixed offset only when no zone name can be found."""
    names = []
    configured = os.environ.get("TZ", "").lstrip(":")
    if configured:
        names.append(configured)
    try:
        target = os.path.realpath(LOCALTIME)
    except OSError:
        target = ""
    if "/zoneinfo/" in target:
        names.append(target.split("/zoneinfo/", 1)[1])
    for name in names:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return datetime.now().astimezone().tzinfo


def local_now() -> datetime:
    return datetime.now(local_zone())


def fixed(moment: datetime) -> datetime:
    """The same instant in a fixed offset, so comparisons and differences are between instants
    (Python compares and subtracts two times of one ``ZoneInfo`` by wall clock)."""
    return moment.astimezone(timezone(moment.utcoffset()))


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


def slots_between(times: list[str], since: datetime, now: datetime, zone: tzinfo | None = None) -> list[datetime]:
    """Slot instants in ``(since, now]``, oldest first, each in its own fixed offset.

    A slot is a wall-clock time of ``zone`` (default: ``now``'s); a repeated wall-clock time counts
    once, at its first occurrence (``fold=0``).
    """
    zone = zone or now.tzinfo
    since, now = fixed(since), fixed(now)
    found = set()
    day = max(since, now - MAX_LOOKBACK).astimezone(zone).date() - timedelta(days=1)
    last = now.astimezone(zone).date()
    while day <= last:
        for value in times:
            minute = minutes(value)
            local = datetime.combine(day, time(minute // 60, minute % 60), tzinfo=zone)
            slot = fixed(local.astimezone(timezone.utc).astimezone(zone))  # a skipped time as it is shown
            if since < slot <= now:
                found.add(slot)
        day += timedelta(days=1)
    return sorted(found)


def next_slot(times: list[str], now: datetime) -> datetime:
    """The first slot after ``now``."""
    start = fixed(now)
    return slots_between(times, start, start + timedelta(days=1, hours=2), now.tzinfo)[0]


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
           heavy_lease_held: Callable[[], bool] = _never,
           heavy_processes: Callable[[], list] = _nothing,
           on_ac_power: Callable[[], bool | None] = _unknown) -> TickDecision:
    """One tick. ``now`` is an aware local time; the probes run only when something is due."""
    if now.tzinfo is None:
        raise ValueError("now must be an aware datetime")
    zone, now = now.tzinfo, fixed(now)
    state = dict(state or {})
    new_state = {**state, "last_tick": iso(now)}
    if not config.get("enabled"):
        return TickDecision("idle", "the schedule is disabled", new_state)

    catch_up = timedelta(minutes=config["catch_up_minutes"])
    reset = {name: state[name] for name in MARKS if (mark := parse_time(state.get(name))) is not None and mark > now}
    if reset:  # the clock was ahead when these were stored
        for name in reset:
            new_state[name] = state[name] = iso(now - catch_up)
        new_state["clock_reset"] = {"at": iso(now), "marks": reset}
        reset = new_state["clock_reset"]
    marks = [parse_time(state.get("last_slot")), parse_time(state.get("enabled_at"))]
    marks = [mark for mark in marks if mark is not None]
    since = max(marks) if marks else now - catch_up
    slots = slots_between(config["times"], since, now, zone)
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
        return TickDecision("idle", idle_reason, new_state, None, missed_iso, reset or None)

    blockers = []
    if update_pending():
        blockers.append("a daemon update or maintenance is in progress")
    if not lease_free():
        blockers.append("an autodev run or a daemon update holds the run lease")
    if heavy_lease_held():
        blockers.append("the heavy-process lease is held")
    heavy = heavy_processes()
    if heavy:
        blockers.append(f"heavy processes are running: {', '.join(map(str, heavy))}")
    if config.get("require_ac_power") and on_ac_power() is False:
        blockers.append("the machine is on battery power")
    if blockers:
        return TickDecision("skip", "; ".join(blockers), new_state, trigger, missed_iso, reset or None)

    if trigger["kind"] == "slot":
        new_state["last_slot"] = trigger["at"]
    else:
        new_state["last_continuation"] = trigger["at"]
    return TickDecision("run", f"{trigger['kind']} due at {trigger['at']}", new_state, trigger, missed_iso,
                        reset or None)
