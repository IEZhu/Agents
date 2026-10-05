"""The stdio trigger: one cycle a fixed delay after the first change of a burst, never in the writer's way.

Most tests use timers that fire only when the test fires them and a clock the test
sets; the last one runs real timers with a delay of 0.05 s.
"""
import logging
import threading
import time

import pytest

from src import user_library
from src.user_sync import trigger as sync_trigger


class Timers:
    """A ``threading.Timer`` factory whose timers never fire by themselves."""

    def __init__(self):
        self.made = []

    def __call__(self, wait, function):
        timer = FakeTimer(wait, function)
        self.made.append(timer)
        return timer


class FakeTimer:
    def __init__(self, wait, function):
        self.wait, self.function = wait, function
        self.started = self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        assert self.started
        if not self.cancelled:
            self.function()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "flows" / ".user"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def start(library):
    """`trigger.start` with fake timers and clock; every trigger is stopped after the test."""
    started = []

    def make(run_cycle, **options):
        timers, clock = Timers(), Clock()
        trigger = sync_trigger.start(run_cycle, **{"library_root": library, "is_ready": lambda: True,
                                                   "timer": timers, "clock": clock, **options})
        started.append(trigger)
        return trigger, timers, clock

    yield make
    for trigger in started:
        trigger.stop()


def changed(root, *paths):
    """What a writer does after a write: `user_library.notify` calls every listener."""
    user_library.notify(root, paths or ("common/review.md",))


def test_a_change_runs_one_cycle_delay_seconds_later(start, library):
    runs = []
    trigger, timers, clock = start(lambda: runs.append(1))
    changed(library)
    timer, = timers.made
    assert timer.started and timer.wait == sync_trigger.DELAY == 10.0 and runs == []
    timer.fire()
    assert runs == [1]


def test_changes_during_the_wait_neither_extend_it_nor_add_runs(start, library):
    runs = []
    trigger, timers, clock = start(lambda: runs.append(1))
    changed(library)
    clock.now += 9
    changed(library, "common/a.md")
    changed(library, "personas/user/b.json")
    timer, = timers.made  # still the timer of the first change, with its wait
    assert timer.wait == 10.0 and not timer.cancelled
    timer.fire()
    assert runs == [1]
    changed(library)  # a new burst gets a run of its own
    assert len(timers.made) == 2


def test_only_changes_of_this_library_count_however_its_path_is_spelled(start, library, tmp_path):
    other = tmp_path / "other library"
    other.mkdir()
    (library / "common").mkdir()
    trigger, timers, clock = start(lambda: None, library_root=library / "common" / "..")
    changed(other)
    assert timers.made == []
    changed(library)  # `notify` passes the resolved root
    assert len(timers.made) == 1


@pytest.mark.parametrize("cycle_ends_at, wait", [(104.0, 7.0), (130.0, 0.0)])
def test_a_change_during_a_cycle_runs_exactly_one_more_after_it(start, library, cycle_ends_at, wait):
    entered, release, runs = threading.Event(), threading.Event(), []

    def run_cycle():
        runs.append(clock.now)
        entered.set()
        assert release.wait(5)

    trigger, timers, clock = start(run_cycle)
    changed(library)
    cycle = threading.Thread(target=timers.made[0].fire)
    cycle.start()
    assert entered.wait(5)
    clock.now = 101.0
    for path in ("common/a.md", "common/b.md", "common/c.md"):
        changed(library, path)  # returns at once while the cycle runs
    assert len(timers.made) == 1
    clock.now = cycle_ends_at
    release.set()
    cycle.join(5)
    follow_up, = timers.made[1:]
    # Due 10 s after the first change during the cycle, or at once after a longer cycle.
    assert follow_up.started and follow_up.wait == pytest.approx(wait)
    follow_up.fire()
    assert len(runs) == 2 and len(timers.made) == 2


def test_readiness_is_asked_right_before_the_run(start, library):
    ready, runs = [True], []
    trigger, timers, clock = start(lambda: runs.append(1), is_ready=lambda: ready[0])
    changed(library)
    ready[0] = False  # another runner took the lock, or sync was switched off, during the wait
    timers.made[0].fire()
    retry, = timers.made[1:]
    assert runs == [] and retry.wait == sync_trigger.RETRY_DELAY == 3.0
    ready[0] = True
    retry.fire()
    assert runs == [1] and len(timers.made) == 2


def test_a_run_that_finds_sync_busy_is_tried_again_a_bounded_number_of_times(start, library):
    ready, runs = [False], []
    trigger, timers, clock = start(lambda: runs.append(1), is_ready=lambda: ready[0])
    changed(library)
    fired = 0
    while fired < len(timers.made):  # each busy run arms the next try until the tries run out
        timers.made[fired].fire()
        fired += 1
    assert fired == 1 + sync_trigger.RETRIES == 6 and runs == []
    assert all(timer.wait == sync_trigger.RETRY_DELAY for timer in timers.made[1:])
    changed(library)  # a new change gets all its tries again
    ready[0] = True
    timers.made[-1].fire()
    assert runs == [1] and fired + 1 == len(timers.made)


@pytest.mark.parametrize("status", ["lock_held", "pending"])
def test_a_cycle_that_reports_sync_busy_is_tried_again(start, library, status):
    results = [{"status": status}, {"status": "ok"}]
    trigger, timers, clock = start(lambda: results.pop(0))
    changed(library)
    timers.made[0].fire()
    retry, = timers.made[1:]
    assert retry.wait == sync_trigger.RETRY_DELAY
    retry.fire()
    assert results == [] and len(timers.made) == 2  # done: no further try


def test_a_busy_cycle_with_a_change_during_it_tries_again_at_the_sooner_time(start, library):
    entered, release = threading.Event(), threading.Event()

    def run_cycle():
        entered.set()
        assert release.wait(5)
        return {"status": "lock_held"}

    trigger, timers, clock = start(run_cycle)
    changed(library)
    cycle = threading.Thread(target=timers.made[0].fire)
    cycle.start()
    assert entered.wait(5)
    changed(library)  # due 10 s later
    clock.now += 1
    release.set()
    cycle.join(5)
    follow_up, = timers.made[1:]
    assert follow_up.wait == pytest.approx(sync_trigger.RETRY_DELAY)  # sooner than the 9 s left of the delay


@pytest.mark.parametrize("failing", ["run_cycle", "is_ready"])
def test_a_failure_is_logged_and_never_reaches_the_writer(start, library, caplog, failing):
    def broken():
        raise RuntimeError("remote unreachable")

    options = {"is_ready": broken} if failing == "is_ready" else {}
    trigger, timers, clock = start(broken if failing == "run_cycle" else (lambda: None), **options)
    changed(library)  # the writer's call returns normally
    with caplog.at_level(logging.ERROR, logger="src.user_sync.trigger"):
        timers.made[0].fire()
    assert "remote unreachable" in caplog.text
    changed(library)  # and the next change still gets its run
    assert len(timers.made) == 2 and timers.made[1].started


def test_stop_unsubscribes_and_cancels_a_pending_run(start, library):
    runs = []
    trigger, timers, clock = start(lambda: runs.append(1))
    changed(library)
    pending, = timers.made
    trigger.stop()
    assert pending.cancelled
    pending.function()  # a timer thread that already woke up still runs nothing
    changed(library)
    assert runs == [] and timers.made == [pending]
    trigger.stop()  # twice is fine


def test_stop_during_a_cycle_lets_it_finish_without_a_run_after_it(start, library):
    entered, release, runs = threading.Event(), threading.Event(), []

    def run_cycle():
        runs.append(1)
        entered.set()
        assert release.wait(5)

    trigger, timers, clock = start(run_cycle)
    changed(library)
    cycle = threading.Thread(target=timers.made[0].fire)
    cycle.start()
    assert entered.wait(5)
    changed(library)
    trigger.stop()
    release.set()
    cycle.join(5)
    assert runs == [1] and len(timers.made) == 1


def test_concurrent_changes_arm_one_run(start, library):
    trigger, timers, clock = start(lambda: None)
    barrier = threading.Barrier(8)

    def burst():
        barrier.wait(5)
        for _ in range(200):
            changed(library)

    writers = [threading.Thread(target=burst) for _ in range(8)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(10)
    assert len(timers.made) == 1


def test_a_timer_that_cannot_start_does_not_block_later_runs(start, library, caplog):
    class Refusing(Timers):
        def __call__(self, wait, function):
            timer = super().__call__(wait, function)
            if len(self.made) == 1:
                def no_thread():
                    raise RuntimeError("can't start new thread")
                timer.start = no_thread
            return timer

    runs, refusing = [], Refusing()
    start(lambda: runs.append(1), timer=refusing)
    with caplog.at_level(logging.ERROR, logger="src.user_sync.trigger"):
        changed(library)  # the writer's call still returns normally
    assert "can't start new thread" in caplog.text
    changed(library)
    refusing.made[1].fire()
    assert runs == [1]


def test_a_negative_delay_is_refused(library):
    with pytest.raises(ValueError, match="negative"):
        sync_trigger.start(lambda: None, library_root=library, is_ready=lambda: True, delay=-1)


def test_real_timers_run_cycles_in_daemon_threads_and_never_block_the_writer(library):
    running, release, finished = threading.Event(), threading.Event(), threading.Event()
    threads = []

    def run_cycle():
        threads.append(threading.current_thread())
        running.set()
        assert release.wait(5)
        finished.set()

    trigger = sync_trigger.start(run_cycle, library_root=library, is_ready=lambda: True, delay=0.05)
    try:
        for _ in range(3):
            changed(library)
        assert running.wait(5)
        began = time.monotonic()
        changed(library)  # during a cycle that is still blocked
        assert time.monotonic() - began < 1.0
        release.set()
        assert finished.wait(5)
        deadline = time.monotonic() + 5
        while len(threads) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)  # room for a run that should not come
        assert len(threads) == 2  # the burst, then the one change during its cycle
        assert all(thread.daemon and thread is not threading.current_thread() for thread in threads)
    finally:
        trigger.stop()


def test_the_stdio_trigger_follows_the_sync_settings(tmp_path, monkeypatch):
    from src.user_sync import engine
    from src.user_sync.trigger import start_for_stdio

    state = tmp_path / "state"
    monkeypatch.setattr(engine, "default_state_dir", lambda: state)
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    trigger = start_for_stdio()
    try:
        assert trigger.root == (tmp_path / "library").resolve()
        assert trigger._is_ready() is False  # not set up
        state.mkdir()
        settings = engine.Settings(remote="git@github.com:me/lib.git", name="Owner",
                                   email="owner@example.com", label="a")
        settings.save(state / engine.SETTINGS_FILE)
        assert trigger._is_ready() is False  # set up, not started
        settings.started = "2026-10-05T12:00:00+00:00"
        settings.save(state / engine.SETTINGS_FILE)
        assert trigger._is_ready() is True
        settings.paused = True
        settings.save(state / engine.SETTINGS_FILE)
        assert trigger._is_ready() is False
    finally:
        trigger.stop()
