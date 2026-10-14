"""Process-local sharing of one resident llama.cpp slot by trusted local actors.

The host owns this adapter and uses it for *all* access to the pinned slot. Actor
names and pre-rendered text/token IDs are trusted host inputs, not credentials or
proof of text/token equivalence. Include source digests in the profile and verify
live sources before requesting completion. One instance serializes entire client
requests; it offers neither parallel inference nor disk snapshots or memoization.
"""

from collections import OrderedDict
from dataclasses import dataclass, replace
import json
import os
import threading
import time

from experiments.accord.inference.client import encode, fingerprint


class CacheValidationError(ValueError):
    """A handle, trusted input, or engine response violated the pinned contract."""


class _Handle:
    __slots__ = ()

    def __repr__(self):
        return "<SharedKV handle>"


@dataclass(frozen=True)
class _Entry:
    profile_fingerprint: str
    namespace: str
    owner: str
    prefix_ids: tuple
    actors: frozenset


def _name(value, label):
    if type(value) is not str or not value:
        raise CacheValidationError(label + " must be nonempty text")
    return value


def _positive_int(value, label):
    if type(value) is not int or value < 1:
        raise CacheValidationError(label + " must be a positive integer")
    return value


def _ids(value, limit):
    if type(value) not in (list, tuple) or not value or len(value) > limit:
        raise CacheValidationError("invalid token sequence length or type")
    if any(type(token) is not int or token < 0 for token in value):
        raise CacheValidationError("token IDs must be nonnegative integers")
    return tuple(value)


def _profile_hash(profile):
    if type(profile) is not dict:
        raise CacheValidationError("profile must be a JSON object")
    try:
        return fingerprint(profile)
    except (TypeError, ValueError) as error:
        raise CacheValidationError("profile must be finite JSON") from error


class SharedKV:
    """Share a single resident engine slot through explicit, revocable grants.

    Handles have identity only within this instance and process. ``invalidate``
    and LRU eviction permanently retire them. A residency miss is different: a
    valid nonresident handle can be used, but first dispatches with cache=False.
    The profile and settings are immutable snapshots taken at construction.
    """

    def __init__(self, client, profile, settings, enabled=True, max_entries=8):
        self._profile_fingerprint = _profile_hash(profile)
        if type(settings) is not dict:
            raise CacheValidationError("settings must be a JSON object")
        try:
            self._settings_json = encode(settings)
        except (TypeError, ValueError) as error:
            raise CacheValidationError("settings must be finite JSON") from error
        self._context_size = _positive_int(profile.get("context_size"), "context_size")
        self._output_limit = _positive_int(settings.get("n_predict"), "n_predict")
        if self._output_limit >= self._context_size:
            raise CacheValidationError("output allowance leaves no input context")
        if type(settings.get("id_slot")) is not int or settings["id_slot"] != 0:
            raise CacheValidationError("this adapter requires the dedicated slot 0")
        if "settings" in profile and fingerprint(profile["settings"]) != fingerprint(settings):
            raise CacheValidationError("settings disagree with pinned profile")
        if type(enabled) is not bool:
            raise CacheValidationError("enabled must be boolean")
        self._max_entries = _positive_int(max_entries, "max_entries")
        self._client = client
        self._enabled = enabled
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._resident = None

    def _check_process(self):
        # Check before acquiring a lock inherited in a fork, which may be locked.
        if os.getpid() != self._pid:
            raise CacheValidationError("SharedKV cannot be used after process fork")

    def _entry(self, handle):
        if type(handle) is not _Handle or handle not in self._entries:
            raise CacheValidationError("unknown, invalidated, or evicted handle")
        return self._entries[handle]

    def _owned(self, handle, owner):
        entry = self._entry(handle)
        if _name(owner, "owner") != entry.owner:
            raise PermissionError("only the handle owner can change grants or invalidate")
        return entry

    def register(self, namespace, owner, prefix_ids):
        self._check_process()
        namespace, owner = _name(namespace, "namespace"), _name(owner, "owner")
        prefix = _ids(prefix_ids, self._context_size - self._output_limit)
        with self._lock:
            handle = _Handle()
            self._entries[handle] = _Entry(self._profile_fingerprint, namespace, owner,
                                           prefix, frozenset((owner,)))
            while len(self._entries) > self._max_entries:
                evicted, _ = self._entries.popitem(last=False)
                if self._resident is evicted:
                    self._resident = None
            return handle

    def grant(self, handle, owner, child):
        self._check_process()
        child = _name(child, "child")
        with self._lock:
            entry = self._owned(handle, owner)
            self._entries[handle] = replace(entry, actors=entry.actors | {child})

    def revoke(self, handle, owner, actor):
        self._check_process()
        actor = _name(actor, "actor")
        with self._lock:
            entry = self._owned(handle, owner)
            if actor == entry.owner:
                raise CacheValidationError("invalidate the handle to remove its owner")
            self._entries[handle] = replace(entry, actors=entry.actors - {actor})

    def invalidate(self, handle, owner):
        self._check_process()
        with self._lock:
            self._owned(handle, owner)
            del self._entries[handle]
            if self._resident is handle:
                self._resident = None

    def complete(self, handle, actor, namespace, profile, prompt_text, prompt_ids):
        """Return the existing Client response plus a ``shared_kv`` metadata map.

        Cache hits are observed through engine counters, never inferred solely
        from requested_reuse. A successful cache miss becomes the next resident
        request; any dispatch/response exception clears residency belief. Input
        rejection does not dispatch and leaves the untouched slot belief intact.
        """
        self._check_process()
        started = time.perf_counter()
        with self._lock:
            acquired = time.perf_counter()
            entry = self._entry(handle)
            if _name(actor, "actor") not in entry.actors:
                raise PermissionError("actor has no grant for this handle")
            if _name(namespace, "namespace") != entry.namespace:
                raise CacheValidationError("namespace mismatch")
            if _profile_hash(profile) != entry.profile_fingerprint:
                raise CacheValidationError("profile or source fingerprint mismatch")
            if type(prompt_text) is not str or not prompt_text:
                raise CacheValidationError("prompt must be nonempty rendered text")
            ids = _ids(prompt_ids, self._context_size - self._output_limit)
            if ids[:len(entry.prefix_ids)] != entry.prefix_ids:
                raise CacheValidationError("prompt differs from exact registered prefix")
            requested_reuse = self._enabled and self._resident is handle
            prior_state = ("disabled" if not self._enabled else
                           "empty" if self._resident is None else
                           "resident" if requested_reuse else "residency_miss")
            dispatched = time.perf_counter()
            try:
                # Both experiment arms send the exact same rendered text form.
                response = self._client.completion(prompt_text, json.loads(self._settings_json),
                                                   cache=requested_reuse)
                returned = time.perf_counter()
                reused, evaluated = self._check_response(response, prompt_text, len(ids), requested_reuse)
            except BaseException:
                self._resident = None
                raise
            self._entries.move_to_end(handle)
            self._resident = handle if self._enabled else None
            ended = time.perf_counter()
            state = ("hit" if reused >= len(entry.prefix_ids) else "partial_hit" if reused else
                     "miss" if requested_reuse else "cold")
            metadata = {
                "requested_reuse": requested_reuse,
                "actual_reused_tokens": reused,
                "actual_evaluated_tokens": evaluated,
                "registered_prefix_tokens": len(entry.prefix_ids),
                "input_tokens": len(ids),
                "cache_state": state,
                "prior_cache_state": prior_state,
                "lock_wait_ms": (acquired - started) * 1000,
                "validation_ms": (dispatched - acquired) * 1000,
                "dispatch_ms": (returned - dispatched) * 1000,
                "lock_hold_ms": (ended - acquired) * 1000,
                "service_total_ms": (ended - started) * 1000,
                "service_ttft_ms": (dispatched - started) * 1000 + response["client_ttft_ms"],
            }
            return {**response, "shared_kv": metadata}

    def _check_response(self, response, prompt_text, input_tokens, requested_reuse):
        if type(response) is not dict or type(response.get("final")) is not dict:
            raise CacheValidationError("missing final engine response")
        final = response["final"]
        timing = final.get("timings")
        if type(timing) is not dict:
            raise CacheValidationError("missing engine token counters")
        reused, evaluated = timing.get("cache_n"), timing.get("prompt_n")
        if (type(reused) is not int or type(evaluated) is not int or
                reused < 0 or evaluated < 0 or reused + evaluated != input_tokens):
            raise CacheValidationError("engine token counters disagree with trusted input")
        if not requested_reuse and reused != 0:
            raise CacheValidationError("engine reused tokens on a cold request")
        if (type(final.get("tokens_evaluated")) is not int or
                final["tokens_evaluated"] != input_tokens):
            raise CacheValidationError("engine input count disagrees with trusted IDs")
        if final.get("prompt") != prompt_text or final.get("truncated") is not False:
            raise CacheValidationError("engine prompt mismatch or context truncation")
        tokens = response.get("tokens")
        if (type(tokens) is not list or len(tokens) > self._output_limit or
                any(type(token) is not int or token < 0 for token in tokens)):
            raise CacheValidationError("engine output exceeds token contract")
        ttft = response.get("client_ttft_ms")
        if type(ttft) not in (float, int) or not 0 <= ttft < float("inf"):
            raise CacheValidationError("invalid client first-token timing")
        return reused, evaluated
