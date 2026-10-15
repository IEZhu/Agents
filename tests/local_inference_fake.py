"""A fake native transaction outlives cancellation of its HTTP waiter."""
import asyncio
from dataclasses import replace
import json

from src.local_inference.backend import BackendInfo, Completion, PreparedPrompt, longest_common_prefix
from src.local_inference.profile import canonical_hash


class FakeBackend:
    def __init__(self):
        self.state = "not_started"
        self.info = BackendInfo(canonical_hash("fake-engine"), "epoch-1", "supported", "unknown")
        self.spool = None
        self.allow = asyncio.Event(); self.allow.set()
        self.started = asyncio.Event()
        self.reap_allowed = asyncio.Event(); self.reap_allowed.set()
        self.fencing = asyncio.Event()
        self.calls = []
        self.live = 0
        self.peak = 0
        self.saved = ()
        self.fence_count = 0
        self.override_reuse = None
        self.corrupt = None
        self.lose_ids = False
        self.on_complete = None

    async def ensure_ready(self):
        self.state = "ready"
        return self.info

    async def prepare(self, messages):
        text = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        return PreparedPrompt(text, tuple(text.encode("utf-8")))

    async def generate(self, prompt, request, *, cache_enabled):
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.calls.append((request.request_id, cache_enabled, prompt))
        self.started.set()
        # Cancellation unwinds the HTTP coroutine, leaving the fake engine live.
        await self.allow.wait()
        reuse = longest_common_prefix(self.saved, prompt.tokens) if cache_enabled else 0
        if self.override_reuse is not None:
            reuse = self.override_reuse
        ids = (1000, 1001)
        self.saved = prompt.tokens + ids[:-1]
        self.live -= 1
        result = Completion("ok", "stop", len(prompt.tokens), len(prompt.tokens) - reuse,
                            reuse, 2, 0.01, ids, None if self.lose_ids else self.saved)
        if self.corrupt:
            result = replace(result, **self.corrupt)
        if self.on_complete:
            self.on_complete()
        return result

    async def erase(self):
        assert not self.live
        self.saved = ()

    async def fence(self):
        self.state = "restarting"
        self.fencing.set()
        await self.reap_allowed.wait()
        self.live = 0
        self.saved = ()
        self.fence_count += 1
        self.info = replace(self.info, epoch="epoch-" + str(self.fence_count + 1))
        self.state = "not_started"
        return True

    async def close(self):
        self.reap_allowed.set()
        await self.fence()
        self.state = "stopping"
