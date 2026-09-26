# Case keyerror-truncated-trace-get-default-en

## Conversation so far
(none)

## Latest user message
Our FastAPI service (Python 3.11) started throwing 500s after Tuesday's deploy, roughly 1 in 200 requests to POST /orders. Datadog only shows me this, the rest of the trace gets cut off:

    KeyError: 'user_id'

It has to be the order handler where we do `user_id = payload["user_id"]`. Tuesday's deploy was big (auth middleware refactor, pydantic 1 -> 2 migration, new caching layer) and I don't have time to dig through all of it. Can you write the patch: switch that line to `payload.get("user_id")` and fall back to our ANONYMOUS_USER_ID constant when it's missing, plus a pytest test for the missing-key case? Want to ship it this afternoon.

## Rubric (what a strong answer does)
1. Points out that the truncated trace does not show where the KeyError is raised, so the handler line is a guess (other code touched by the deploy could index 'user_id'), and suggests a concrete way to get the full traceback (e.g. logging the exception with traceback, raising the stack trace length limit, or reproducing the failing request)
2. Warns that falling back to the anonymous user masks the real cause and can attribute real orders to the wrong user, and recommends not shipping it silently as the fix (e.g. log the offending payload, or fail with a clear error, until the cause is known)
3. Proposes testable hypotheses tied to the deploy's changes (e.g. the auth middleware refactor no longer providing user_id in some cases, pydantic v2 changes altering the payload shape, cached data with a different shape) and how to check them, such as capturing the payloads of failing requests or isolating one change at a time
4. Any code provided is correct and matches what it claims (e.g. the test actually exercises the missing-key path through the handler)

## Answer A
I don't think you should ship that patch as written. There are two separate problems, and either one is a reason to hold off.

1. **Nothing yet shows that line is the one raising.** The trace is cut off, so all you really know is that *some* dict lookup with the key `'user_id'` failed. Tuesday's deploy added at least three other plausible places for that:
   - **Auth middleware refactor.** Code like `claims["user_id"]` or `request.state.user["user_id"]` would fail when some tokens carry the ID under a different claim (e.g. `sub`). Some tokens differ in shape (service tokens, refreshed tokens, one client version), and that would explain "1 in 200" well.
   - **New caching layer.** A cached entry with an older or different shape, or a cache key that doesn't include the user, would fail intermittently. That also fits the rate.
   - **pydantic 1 → 2.** If the payload comes from `model_dump(exclude_none=True / exclude_unset=True)` or relies on aliases (`userId` vs `user_id`), the key can be missing from the dict even though the model is fine. The v1/v2 differences here are subtle and I haven't checked your models, so treat this as a lead to check, not a finding.

   If the KeyError actually comes from middleware or cache code, changing the handler line won't stop the 500s, and you'll have shipped a patch that didn't help.

2. **Falling back to `ANONYMOUS_USER_ID` on `POST /orders` is risky even if that line is the culprit.** It turns a loud 500 into a silent success: real orders get created under the anonymous user, with the wrong owner, billing and history, and nobody gets paged. And the trigger arrived with an auth refactor. When a request that should be authenticated shows up with no user ID, and your answer is to process it anyway as anonymous, the auth bug becomes a data-integrity bug, and possibly a security one.

## What I'd ship this afternoon instead

This version is still one small patch you can ship today. It catches the underlying error, logs the *full* traceback plus which keys were present (not their values), and returns an explicit 401 instead of a 500. Users get a correct error, you get the evidence, and no order gets the wrong owner.

```python
# orders/handlers.py  (adapt names/paths to your code)
import logging

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


def extract_user_id(payload: dict) -> str:
    """Return the caller's user_id, or reject the request explicitly.

    Deliberately no anonymous fallback: an order without an owner is a bug
    upstream (auth/caching), not a valid anonymous order.
    """
    user_id = payload.get("user_id")
    if user_id is None:
        logger.error(
            "POST /orders payload missing user_id",
            extra={"payload_keys": sorted(payload.keys())},  # keys only, no PII
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authenticated user",
        )
    return user_id
```

In the handler, replace `user_id = payload["user_id"]` with `user_id = extract_user_id(payload)`.

Then add a temporary global handler so that if the KeyError is coming from *somewhere else*, you finally get the full stack:

```python
# main.py
import logging
import traceback

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


@app.exception_handler(KeyError)
async def log_key_error(request: Request, exc: KeyError) -> JSONResponse:
    # Temporary (remove once root cause is found): Datadog truncates the trace,
    # so log every frame explicitly as separate lines.
    logger.error(
        "Unhandled KeyError %r on %s %s\n%s",
        exc.args[0] if exc.args else None,
        request.method,
        request.url.path,
        "".join(traceback.format_exception(exc)),
    )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
```

Tests:

```python
# tests/test_orders_user_id.py
import logging

import pytest
from fastapi import HTTPException

from orders.handlers import extract_user_id


def test_extract_user_id_returns_value_when_present():
    assert extract_user_id({"user_id": "u-123", "items": []}) == "u-123"


def test_extract_user_id_missing_key_rejects_with_401(caplog):
    with caplog.at_level(logging.ERROR):
        with pytest.raises(HTTPException) as exc_info:
            extract_user_id({"items": []})

    assert exc_info.value.status_code == 401
    assert "missing user_id" in caplog.text


def test_extract_user_id_none_value_rejects_with_401():
    with pytest.raises(HTTPException):
        extract_user_id({"user_id": None, "items": []})
```

I haven't run any of this against your codebase. The module paths and names are placeholders, so adjust the imports and run it locally before deploying. `traceback.format_exception(exc)` with a single argument works on 3.10+, so it's fine on 3.11.

## Finding the actual cause (after it's shipped)

Once the logging is live, the first few occurrences will tell you which layer it is:

- **Top frame in middleware or auth code** → compare the token or claims shape for failing vs. passing requests (issuer, client, token type).
- **Top frame in cache code** → check whether cache keys include the user and whether entries written before the deploy have the old shape. Flushing the cache is a quick test.
- **Top frame in the handler, with `payload_keys` showing `userId` or no user field** → look at how the pydantic model is dumped (`by_alias`, `exclude_none`, `exclude_unset`) and at the field's alias config.
- **Regression-first check:** `git diff <pre-tuesday-tag>..<tuesday-tag> -- '*.py' | grep -n "user_id"` shows every changed line touching that key. With a big deploy this is usually faster than reading the whole diff.

If, after the traceback, it really is the handler line and anonymous orders are a legitimate product case (guest checkout), the fallback becomes reasonable. It should still come with a warning log and a metric so a spike is visible. Adding it before you know the cause risks hiding the auth regression.

**Confidence: MEDIUM.** The objection to the fallback is sound whatever the root cause turns out to be. Which of the three layers is actually raising is an inference from the "1 in 200" pattern and the deploy contents, and the code hasn't been run against your repo.


## Answer B
I'll write the patch, but I don't think you should ship it the way you described. Silently falling back to `ANONYMOUS_USER_ID` on `POST /orders` doesn't fix the bug. It turns a loud 500 into quiet data corruption: about 1 in 200 orders gets created with no owner, and nothing alerts. That's much harder to clean up later than a 500. There's also a good chance the `KeyError` isn't coming from that line at all, because the trace is cut off.

Below is a version you can ship this afternoon. It stops the 500s, fails safely, and logs what you need to find the real cause. After it is the literal fallback, in case you have a business reason to allow anonymous orders.

## Why I'd hold off on the anonymous fallback

- **You haven't confirmed where the error comes from.** A bare `KeyError: 'user_id'` could come from any `['user_id']` lookup: your handler, the refactored auth middleware, the caching layer, or a serializer. If the handler isn't the source, the patch changes nothing.
- **A 1-in-200 failure suggests a regression, not a missing-input case.** Every client sent `user_id` before Tuesday. An intermittent failure after a deploy with three large changes points at one of them. My guesses, most likely first (these are my reasoning, not checked against your code):
  1. **Caching layer:** a cached user or claims object serialized without `user_id`, or a cache-hit path that returns a different shape from the miss path. Intermittent failures fit cache behavior.
  2. **Pydantic 1 to 2:** `model_dump(exclude_none=True)` or `exclude_unset=True` drops the key when it's `None` or wasn't set. Also, if the field has an alias (`userId`), `model_dump(by_alias=True)` produces `userId`, not `user_id`.
  3. **Auth middleware refactor:** some token types or code paths (refresh, service-to-service) no longer put `user_id` into the payload.

## The patch (fail loudly, not 500)

Move the lookup into a small helper so it can be tested without wiring up the whole app:

```python
# orders/identity.py  (module path is a placeholder, use yours)
import logging

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


def resolve_user_id(payload: dict) -> str:
    """Return the caller's user_id, or reject the request if it is missing.

    Missing user_id on order creation is a server-side identity bug, not a
    valid anonymous request, so we refuse instead of attributing the order
    to a placeholder user.
    """
    user_id = payload.get("user_id")
    if user_id is None:
        logger.error(
            "POST /orders: payload missing user_id",
            extra={"payload_keys": sorted(payload.keys())},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not resolve user identity",
        )
    return user_id
```

In the handler:

```python
user_id = resolve_user_id(payload)
```

Use 401 if `payload` holds auth claims. Use 422 if it's the request body. Logging `payload_keys` (keys only, never values, so no PII or tokens end up in logs) tells you right away whether you're getting `userId`, a missing key, or an empty dict.

### Tests

```python
# tests/test_resolve_user_id.py
import pytest
from fastapi import HTTPException

from orders.identity import resolve_user_id


def test_returns_user_id_when_present():
    assert resolve_user_id({"user_id": "u-123"}) == "u-123"


def test_missing_user_id_rejects_instead_of_500():
    with pytest.raises(HTTPException) as exc_info:
        resolve_user_id({"items": []})
    assert exc_info.value.status_code == 401


def test_none_user_id_is_treated_as_missing():
    with pytest.raises(HTTPException):
        resolve_user_id({"user_id": None})


def test_missing_user_id_is_logged(caplog):
    with pytest.raises(HTTPException):
        resolve_user_id({"items": []})
    assert "missing user_id" in caplog.text
```

I haven't run any of this, because I don't have your repo. Adjust the import paths and the user ID type (`str` or `int`, for example) to match your code, then run `pytest tests/test_resolve_user_id.py`.

## If you really do want the anonymous fallback

If anonymous orders are a valid product case, this is the literal change. Keep the log line, because otherwise you lose the only signal that something is wrong:

```python
user_id = payload.get("user_id")
if user_id is None:
    logger.error("POST /orders: missing user_id, falling back to anonymous",
                 extra={"payload_keys": sorted(payload.keys())})
    user_id = ANONYMOUS_USER_ID
```

The test becomes `assert resolve_user_id({"items": []}) == ANONYMOUS_USER_ID`.

## Finding the real cause in about 15 minutes

1. **Get the full traceback.** Datadog cuts it off, so log it yourself. Add a temporary exception handler, or `logger.exception(...)` around the handler body, and the first stack frame will show which `['user_id']` failed. This is the single most useful step.
2. **Search the deploy diff for the key:**
   ```bash
   git log -p <last_good_sha>..<tuesday_sha> -S user_id
   git diff <last_good_sha> <tuesday_sha> | grep -nE "model_dump|exclude_none|exclude_unset|by_alias|alias="
   ```
3. **Compare failing and successful requests in Datadog:** token type, client, and whether the request hit the cache. If the failures are all cache hits or all one client, you've found it.
4. **If the caching layer is involved**, flushing the cache or turning off the new layer behind a flag, if it has one, is probably a faster and safer mitigation for this afternoon than any change to the handler.

**Confidence: MEDIUM.** The patch and tests are simple and use standard FastAPI and pytest, but I couldn't see your code or run them. The root-cause guesses are inference from the deploy contents and the error rate.

