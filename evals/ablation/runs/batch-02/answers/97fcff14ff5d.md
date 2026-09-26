### What's wrong

The loop deletes keys from `sessions` while it is still iterating over `sessions.items()`. `items()` returns a live view of the dict. When the dict's size changes, CPython sees it on the next step of the iterator and raises `RuntimeError: dictionary changed size during iteration`. This is intentional: Python refuses to carry on because it can't guarantee the iteration would still be correct.

That is also why the job only started crashing now. The loop body never removed anything until you added the expiry logic. The error appears on any night when at least one session has expired.

### Fix

Decide which keys to remove first, then delete them in a separate pass:

```python
def purge_expired(sessions: dict[str, Session], now: float) -> None:
    expired_ids = [sid for sid, s in sessions.items() if s.expires_at < now]
    for sid in expired_ids:
        del sessions[sid]
```

- The list is a snapshot, so the deletes never touch the object being iterated.
- This is O(n) time, with O(k) extra memory for the k expired IDs.
- It still mutates the dict **in place**, which matters if other code holds a reference to `sessions`. Rebinding with `sessions = {sid: s for ... if s.expires_at >= now}` would only change the local name, and the caller's dict would keep the expired entries. If you want a comprehension, return the new dict and update the call site instead.
- `for sid in list(sessions):` followed by a check and `del` also works. The version above is more explicit about which entries get removed.

Edge case: `<` keeps a session whose `expires_at == now`. That matches your original code. If you meant "expired at or before now", change it to `<=`.

### Regression test

```python
def test_purge_expired_removes_only_expired_without_error():
    sessions = {
        "old": Session(expires_at=100.0),
        "fresh": Session(expires_at=300.0),
        "also_old": Session(expires_at=150.0),
    }
    purge_expired(sessions, now=200.0)
    assert set(sessions) == {"fresh"}
```

The old code fails this test with the same `RuntimeError`. I'm assuming `Session` can be constructed with just `expires_at`. If it needs more fields, use your existing fixture or factory.

### Commit message

```
Fix crash in purge_expired when sessions expire

The nightly cleanup deleted entries from the sessions dict while
iterating over sessions.items(), which raises "RuntimeError:
dictionary changed size during iteration" as soon as any session
has expired. The bug surfaced once the expiry logic was added.

Collect the expired session IDs first, then delete them in a
separate pass. The dict is still mutated in place, so callers
holding a reference see the purge.

Add a regression test covering a mix of expired and live sessions.
```

### Confidence
HIGH on the root cause and fix, since this is standard CPython dict behavior. I haven't run the fix or the test against your code, so run the suite before merging.
