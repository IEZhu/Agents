"""Bounded work; cancellation of a waiter does not release running work."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import threading

# Only task accounting travels implicitly. Workspace paths are explicit arguments.
request_jobs = ContextVar("agents_request_jobs", default=None)


class TrackedExecutor(ThreadPoolExecutor):
    def __init__(self, workers=8, capacity=96):
        super().__init__(max_workers=workers, thread_name_prefix="agents-io")
        self.slots = threading.BoundedSemaphore(capacity)
        self.pending = set()
        self.guard = threading.Lock()

    def submit(self, function, /, *args, **kwargs):
        if not self.slots.acquire(blocking=False):
            raise RuntimeError("busy: I/O queue full")
        try:
            future = super().submit(function, *args, **kwargs)
        except BaseException:
            self.slots.release()
            raise
        with self.guard:
            self.pending.add(future)
        jobs = request_jobs.get()
        if jobs is not None:
            jobs.append(future)
        def finished(done):
            with self.guard:
                self.pending.discard(done)
            self.slots.release()
        future.add_done_callback(finished)
        return future

    @property
    def inflight(self):
        with self.guard:
            return len(self.pending)


class InferenceExecutor:
    def __init__(self, queue_size=64):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agents-inference")
        self.slots = threading.BoundedSemaphore(queue_size + 1)
        self.pending = 0
        self.guard = threading.Lock()

    def run(self, function, *args):
        if not self.slots.acquire(blocking=False):
            raise RuntimeError("busy: inference queue full")
        with self.guard:
            self.pending += 1
        try:
            return self.executor.submit(function, *args).result()
        finally:
            with self.guard:
                self.pending -= 1
            self.slots.release()


async def finish_jobs(jobs):
    # concurrent futures retain the real execution status even when an asyncio
    # wrapper was cancelled (e.g. telemetry timeout or connection loss).
    while jobs:
        batch, jobs[:] = jobs[:], []
        await asyncio.gather(*(asyncio.wrap_future(f) for f in batch), return_exceptions=True)
