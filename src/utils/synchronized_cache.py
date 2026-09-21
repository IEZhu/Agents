"""A small synchronized TTL cache for process-wide derived context."""
from collections.abc import MutableMapping
from threading import RLock
from cachetools import TTLCache


class SynchronizedTTLCache(MutableMapping):
    def __init__(self, maxsize, ttl):
        self.cache = TTLCache(maxsize=maxsize, ttl=ttl)
        self.lock = RLock()

    def __getitem__(self, key):
        with self.lock: return self.cache[key]

    def __setitem__(self, key, value):
        with self.lock: self.cache[key] = value

    def __delitem__(self, key):
        with self.lock: del self.cache[key]

    def __len__(self):
        with self.lock: return len(self.cache)

    def __iter__(self):
        with self.lock: return iter(list(self.cache))

    def clear(self):
        with self.lock: self.cache.clear()
