"""Bounded asynchronous waits; durable mailbox cursors remain authoritative."""
import asyncio
from contextlib import contextmanager
import threading

from .store import CooperationError


class MailboxNotifier:
    """Thread-safe hints, registered before reading to avoid lost wakeups."""

    def __init__(self):
        self._lock = threading.Lock()
        self._listeners = {}

    @contextmanager
    def listen(self, key):
        listener = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            if sum(map(len, self._listeners.values())) >= 128:
                raise CooperationError("busy", "Too many mailbox waits; retry later")
            self._listeners.setdefault(key, set()).add(listener)
        try:
            yield listener[1]
        finally:
            with self._lock:
                self._listeners[key].discard(listener)
                if not self._listeners[key]:
                    del self._listeners[key]

    def notify(self, keys=None):
        with self._lock:
            listeners = [item for key, group in self._listeners.items()
                         if keys is None or key in keys for item in group]
        for loop, event in listeners:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # Its cancelled request/closed loop owns listener cleanup.


async def wait_for_notifications(service, request, identity):
    from .mailbox import Mailbox

    mailbox = Mailbox(service)
    key = (request.repository_id, request.subject_id or request.participant_id)
    deadline = asyncio.get_running_loop().time() + request.timeout_seconds
    epoch = service.epoch
    try:
        with service.mailbox_notifier.listen(key) as changed:
            while True:
                if service.epoch != epoch:
                    raise CooperationError("store_unavailable", "Coordinator restarted; reconnect with the durable cursor")
                changed.clear()
                response, next_expiry = await asyncio.to_thread(mailbox.notifications, request, identity)
                if not service.started or service.epoch != epoch:
                    raise CooperationError("store_unavailable", "Coordinator lifecycle changed; reconnect with the durable cursor")
                if response["status"] != "ok" or response["events"]:
                    return response
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return {**response, "timed_out": True}
                if next_expiry is not None:
                    remaining = min(remaining, max(0.001, next_expiry - service.clock()))
                try:
                    await asyncio.wait_for(changed.wait(), remaining)
                except TimeoutError:
                    pass  # Recheck durable state and deadlines before returning.
    except CooperationError as error:
        return mailbox._error(error)
