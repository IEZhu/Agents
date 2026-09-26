# Case tdd-new-feature-plan-en

## Conversation so far
### User
I'm adding a `search_tickets(query: str, status: str | None, limit: int = 20)` tool to our MCP server (Python, FastMCP). It calls our internal Jira-like REST API at /api/v2/tickets?q=...&status=...&page_size=... and returns a compact list of {id, title, status}. The API caps page_size at 50 and returns 400 on unknown status values.

### Assistant
Here's an implementation:

```python
@mcp.tool()
async def search_tickets(query: str, status: str | None = None, limit: int = 20) -> list[dict]:
    params = {"q": query, "page_size": limit}
    if status:
        params["status"] = status
    async with httpx.AsyncClient(base_url=API_URL) as c:
        r = await c.get("/api/v2/tickets", params=params)
        r.raise_for_status()
    return [{"id": t["id"], "title": t["title"], "status": t["status"]} for t in r.json()["items"]]
```

Then add a quick test that calls the tool against staging and checks it returns a non-empty list.

## Latest user message
Hmm, the staging test is going to be annoying, staging data changes every day and the API is down during deploys. How should I actually test this thing? Show me the tests.

## Rubric (what a strong answer does)
1. Replaces the live staging call with a mock/fake at the HTTP boundary (e.g. respx, pytest-httpx, or httpx.MockTransport) so tests are deterministic and do not depend on staging data or uptime
2. Includes separate tests for distinct behaviours: request params built correctly (status omitted when None), response mapped to {id, title, status}, and the 400-on-unknown-status path
3. Notices that limit above 50 is not handled by the current implementation and either tests for/proposes clamping or validation, or flags it as a bug the tests should catch
4. Test code is valid for async Python (e.g. pytest-asyncio or anyio marker) and each test is independently runnable with clear arrange/act/assert steps

## Answer A
You're right, and the staging test I suggested was a poor choice as the main test. It fails for reasons that have nothing to do with your code (deploys, data churn), and it can't reach the two behaviours you care about: the 50 cap and the 400 on an unknown status. My earlier implementation also mishandles both of those. It passes `limit` straight through as `page_size`, and a 400 surfaces as a raw `HTTPStatusError`.

Here's how I'd set it up:

1. **Unit tests against a fake HTTP layer.** Use `httpx.MockTransport`, which is built into httpx, so there's no new dependency. These tests are fast, deterministic and cover every edge case. This is where almost all the value is.
2. **An optional contract test against staging.** It's skipped by default and only checks the response *shape*, never the contents. Run it on demand or nightly, not in the PR gate.

To make step 1 easy, split the logic from the tool so the tests can inject a client.

## Refactored server code

```python
# server.py
import os

import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.getenv("TICKETS_API_URL", "")
MAX_PAGE_SIZE = 50

mcp = FastMCP("tickets")


class TicketAPIError(Exception):
    """The ticket API was unreachable or returned an unexpected error."""


async def fetch_tickets(
    client: httpx.AsyncClient, query: str, status: str | None, limit: int
) -> list[dict]:
    if limit < 1:
        raise ValueError("limit must be >= 1")

    params: dict[str, str | int] = {"q": query, "page_size": min(limit, MAX_PAGE_SIZE)}
    if status:  # treat None and "" the same: no filter
        params["status"] = status

    try:
        r = await client.get("/api/v2/tickets", params=params)
    except httpx.TransportError as e:  # connect errors, timeouts, etc.
        raise TicketAPIError(f"Ticket API unreachable: {e}") from e

    if r.status_code == 400 and status:
        raise ValueError(f"Unknown status {status!r}. API response: {r.text[:200]}")
    if r.is_error:
        raise TicketAPIError(f"Ticket API returned {r.status_code}: {r.text[:200]}")

    return [
        {"id": t["id"], "title": t["title"], "status": t["status"]}
        for t in r.json()["items"]
    ]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API_URL, timeout=10.0)


@mcp.tool()
async def search_tickets(query: str, status: str | None = None, limit: int = 20) -> list[dict]:
    """Search tickets by text. Optional status filter. Returns at most 50 results as {id, title, status}."""
    async with _client() as client:
        return await fetch_tickets(client, query, status, limit)
```

On `limit > 50`, this clamps rather than paginates. If callers really need more than 50 results, you'd have to add a pagination loop, and I don't know your API's paging parameter (page number or cursor), so I left that out. The docstring tells the model about the cap, which matters because the docstring is what the LLM sees.

## Unit tests

```python
# tests/test_search_tickets.py
import httpx
import pytest

import server
from server import MAX_PAGE_SIZE, TicketAPIError, fetch_tickets

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://tickets.test")


FULL_TICKET = {
    "id": "T-1",
    "title": "Login broken",
    "status": "open",
    "assignee": "bob",
    "description": "very long text that should not leak into the tool output",
}


def ok(items):
    return lambda request: httpx.Response(200, json={"items": items})


class Recorder:
    """Handler that records the request and returns a canned response."""

    def __init__(self, response=None):
        self.request: httpx.Request | None = None
        self.response = response or httpx.Response(200, json={"items": []})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return self.response


# --- response mapping -------------------------------------------------------

async def test_maps_items_to_compact_shape():
    async with make_client(ok([FULL_TICKET])) as c:
        result = await fetch_tickets(c, "login", None, 20)
    assert result == [{"id": "T-1", "title": "Login broken", "status": "open"}]


async def test_empty_result_is_empty_list():
    async with make_client(ok([])) as c:
        assert await fetch_tickets(c, "nothing", None, 20) == []


# --- request construction ---------------------------------------------------

async def test_sends_path_and_params():
    rec = Recorder()
    async with make_client(rec) as c:
        await fetch_tickets(c, "login bug", "open", 20)
    assert rec.request.url.path == "/api/v2/tickets"
    assert dict(rec.request.url.params) == {"q": "login bug", "status": "open", "page_size": "20"}


@pytest.mark.parametrize("status", [None, ""])
async def test_omits_status_when_not_given(status):
    rec = Recorder()
    async with make_client(rec) as c:
        await fetch_tickets(c, "x", status, 20)
    assert "status" not in rec.request.url.params


@pytest.mark.parametrize("limit, expected", [(1, "1"), (20, "20"), (50, "50"), (51, "50"), (1000, "50")])
async def test_page_size_is_clamped(limit, expected):
    rec = Recorder()
    async with make_client(rec) as c:
        await fetch_tickets(c, "x", None, limit)
    assert rec.request.url.params["page_size"] == expected


@pytest.mark.parametrize("limit", [0, -5])
async def test_rejects_non_positive_limit(limit):
    rec = Recorder()
    async with make_client(rec) as c:
        with pytest.raises(ValueError, match="limit"):
            await fetch_tickets(c, "x", None, limit)
    assert rec.request is None  # never hit the API


# --- error handling ---------------------------------------------------------

async def test_unknown_status_gives_readable_error():
    rec = Recorder(httpx.Response(400, json={"error": "invalid status 'opne'"}))
    async with make_client(rec) as c:
        with pytest.raises(ValueError, match="Unknown status 'opne'"):
            await fetch_tickets(c, "x", "opne", 20)


@pytest.mark.parametrize("code", [401, 404, 500, 503])
async def test_http_errors_become_ticket_api_error(code):
    async with make_client(lambda req: httpx.Response(code, text="nope")) as c:
        with pytest.raises(TicketAPIError, match=str(code)):
            await fetch_tickets(c, "x", None, 20)


async def test_network_failure_becomes_ticket_api_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    async with make_client(handler) as c:
        with pytest.raises(TicketAPIError, match="unreachable"):
            await fetch_tickets(c, "x", None, 20)


# --- tool wiring (thin) -----------------------------------------------------

async def test_tool_uses_fetch_tickets(monkeypatch):
    monkeypatch.setattr(server, "_client", lambda: make_client(ok([FULL_TICKET])))
    result = await server.search_tickets("login")
    assert result == [{"id": "T-1", "title": "Login broken", "status": "open"}]
```

Some of this rests on things I remember but haven't checked against your installed versions:

- **`pytest.mark.anyio` plus the `anyio_backend` fixture.** This uses the pytest plugin that ships with `anyio`, which `mcp` should already pull in. If you already use `pytest-asyncio`, swap in `@pytest.mark.asyncio` and drop the fixture.
- **The last test.** It assumes `@mcp.tool()` returns the original function unchanged, so `server.search_tickets` can be called directly. I believe that's how FastMCP's decorator behaves, but check your version. If it doesn't, drop that test. The `fetch_tickets` tests cover the logic either way.

## Optional contract test against staging

This one catches the thing mocks can't: the real API changing its response shape, for example renaming `items` or `title`.

```python
# tests/test_search_tickets_contract.py
import os

import httpx
import pytest

from server import fetch_tickets

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TICKETS_API_URL"), reason="TICKETS_API_URL not set"),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_response_shape_matches_contract():
    async with httpx.AsyncClient(base_url=os.environ["TICKETS_API_URL"], timeout=10.0) as c:
        result = await fetch_tickets(c, "a", None, 5)
    # Don't assert on contents or even non-emptiness: staging data changes daily.
    assert len(result) <= 5
    for t in result:
        assert set(t) == {"id", "title", "status"}
```

Register the marker in `pyproject.toml`, then keep it out of the default run:

```toml
[tool.pytest.ini_options]
markers = ["integration: hits a live ticket API"]
addopts = "-m 'not integration'"
```

Run it on demand with `TICKETS_API_URL=https://staging... pytest -m integration`.

For extra confidence in the contract test, also check that a known-bad status returns 400. Use a value that can't be a real status, such as `"__nope__"`, and assert `pytest.raises(ValueError)`. That pins down the one API behaviour your error handling depends on.


## Answer B
You're right, and I'd take back that suggestion. A test that hits staging checks staging's data and uptime more than it checks your code. It will fail randomly during deploys, and "non-empty list" breaks the day someone cleans up test tickets. Use this split instead:

1. **Unit tests (most of them):** mock the HTTP boundary with `httpx.MockTransport`. They're fast, deterministic and run offline in CI.
2. **One opt-in contract test:** runs against the real API only when you ask for it. It checks the *shape* of the response and your assumptions about the API (the `items` key, the 400 on bad status, the page_size cap), never the contents.

Writing the tests also turned up two bugs in the version I gave you. `limit` is passed through as-is, so asking for 100 either gets a 400 or gets capped without any sign, depending on how the API enforces its limit. And a bad `status` surfaces as a bare `HTTPStatusError` that tells the model calling the tool nothing useful. The refactor below fixes both and makes the code testable by moving the logic into a function that receives the client.

## Refactored tool

```python
# server.py
import os
import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.getenv("TICKETS_API_URL", "http://localhost:8000")
MAX_PAGE_SIZE = 50

mcp = FastMCP("tickets")


class TicketAPIError(Exception):
    """The ticket API was unreachable or returned an unexpected error."""


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API_URL, timeout=10.0)


async def fetch_tickets(
    client: httpx.AsyncClient, query: str, status: str | None, limit: int
) -> list[dict]:
    params: dict[str, str | int] = {
        "q": query,
        "page_size": max(1, min(limit, MAX_PAGE_SIZE)),
    }
    if status is not None:
        params["status"] = status

    try:
        r = await client.get("/api/v2/tickets", params=params)
    except httpx.RequestError as e:  # connect errors, timeouts, etc.
        raise TicketAPIError(f"Ticket API unreachable: {e}") from e

    if r.status_code == 400 and status is not None:
        raise ValueError(f"Unknown status {status!r}; the API rejected it: {r.text[:200]}")
    if r.is_error:
        raise TicketAPIError(f"Ticket API returned {r.status_code}: {r.text[:200]}")

    return [
        {"id": t["id"], "title": t["title"], "status": t["status"]}
        for t in r.json().get("items", [])
    ]


@mcp.tool()
async def search_tickets(query: str, status: str | None = None, limit: int = 20) -> list[dict]:
    """Search tickets by text. Optional status filter. Returns at most 50 results (id, title, status)."""
    async with make_client() as client:
        return await fetch_tickets(client, query, status, limit)
```

The tool is now a thin wrapper. All the behavior lives in `fetch_tickets`, and you can hand that function a client with a fake transport.

## Unit tests (no network)

```python
# tests/test_search_tickets.py
import httpx
import pytest

import server
from server import MAX_PAGE_SIZE, TicketAPIError, fetch_tickets

pytestmark = pytest.mark.anyio  # or pytest.mark.asyncio if you use pytest-asyncio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeAPI:
    """Records requests and returns a canned response (or raises)."""

    def __init__(self, response: httpx.Response | None = None, exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return self.response

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(self))

    @property
    def params(self) -> httpx.QueryParams:
        return self.requests[-1].url.params


ITEMS = {
    "items": [
        {"id": "T-1", "title": "Login broken", "status": "open", "assignee": "ann", "body": "..."},
        {"id": "T-2", "title": "Slow search", "status": "closed", "assignee": None, "body": "..."},
    ]
}


async def test_sends_query_and_page_size_without_status():
    api = FakeAPI(httpx.Response(200, json=ITEMS))
    async with api.client() as c:
        await fetch_tickets(c, "login", None, 20)

    assert api.requests[-1].url.path == "/api/v2/tickets"
    assert api.params["q"] == "login"
    assert api.params["page_size"] == "20"
    assert "status" not in api.params


async def test_passes_status_when_given():
    api = FakeAPI(httpx.Response(200, json=ITEMS))
    async with api.client() as c:
        await fetch_tickets(c, "login", "open", 20)

    assert api.params["status"] == "open"


@pytest.mark.parametrize(
    "limit, expected",
    [(20, 20), (50, 50), (51, MAX_PAGE_SIZE), (1000, MAX_PAGE_SIZE), (0, 1), (-5, 1)],
)
async def test_limit_is_clamped_to_api_bounds(limit, expected):
    api = FakeAPI(httpx.Response(200, json=ITEMS))
    async with api.client() as c:
        await fetch_tickets(c, "x", None, limit)

    assert api.params["page_size"] == str(expected)


async def test_returns_compact_records_only():
    api = FakeAPI(httpx.Response(200, json=ITEMS))
    async with api.client() as c:
        result = await fetch_tickets(c, "x", None, 20)

    assert result == [
        {"id": "T-1", "title": "Login broken", "status": "open"},
        {"id": "T-2", "title": "Slow search", "status": "closed"},
    ]


async def test_empty_result():
    api = FakeAPI(httpx.Response(200, json={"items": []}))
    async with api.client() as c:
        assert await fetch_tickets(c, "nothing", None, 20) == []


async def test_unknown_status_gives_clear_error():
    api = FakeAPI(httpx.Response(400, text="invalid status"))
    async with api.client() as c:
        with pytest.raises(ValueError, match="Unknown status 'bogus'"):
            await fetch_tickets(c, "x", "bogus", 20)


async def test_server_error_raises_api_error():
    api = FakeAPI(httpx.Response(503, text="deploying"))
    async with api.client() as c:
        with pytest.raises(TicketAPIError, match="503"):
            await fetch_tickets(c, "x", None, 20)


async def test_network_failure_raises_api_error():
    api = FakeAPI(exc=httpx.ConnectError("connection refused"))
    async with api.client() as c:
        with pytest.raises(TicketAPIError, match="unreachable"):
            await fetch_tickets(c, "x", None, 20)


async def test_tool_wrapper_uses_client(monkeypatch):
    api = FakeAPI(httpx.Response(200, json=ITEMS))
    monkeypatch.setattr(server, "make_client", api.client)

    result = await server.search_tickets("login", status="open", limit=5)

    assert [t["id"] for t in result] == ["T-1", "T-2"]
    assert api.params["page_size"] == "5"
```

The "API is down during deploys" case is now a test (`503` → `TicketAPIError`) rather than a source of random failures.

## Opt-in contract test (real API, shape only)

```python
# tests/test_tickets_contract.py
import os
import httpx
import pytest

from server import fetch_tickets

API = os.getenv("TICKETS_CONTRACT_URL")
pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not API, reason="set TICKETS_CONTRACT_URL to run contract tests"),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_response_shape_matches_our_assumptions():
    async with httpx.AsyncClient(base_url=API, timeout=10) as c:
        result = await fetch_tickets(c, "a", None, 50)  # 50 = cap, must be accepted

    assert isinstance(result, list)
    for t in result:  # may be empty on a given day; that's fine
        assert set(t) == {"id", "title", "status"}


async def test_unknown_status_is_rejected():
    async with httpx.AsyncClient(base_url=API, timeout=10) as c:
        with pytest.raises(ValueError):
            await fetch_tickets(c, "a", "definitely-not-a-status", 10)
```

Run it on demand or on a nightly job, not on every PR. Its only job is to catch the API changing under you, for example if `items` gets renamed or bad statuses stop returning 400. Those are exactly the assumptions the mocks hard-code.

## Setup notes (from memory, not checked against your repo)

- `pytest.mark.anyio` uses the pytest plugin that ships with `anyio`, and `httpx` already depends on `anyio`. If you already use `pytest-asyncio`, swap in `@pytest.mark.asyncio` and drop the `anyio_backend` fixtures.
- `test_tool_wrapper_uses_client` calls `server.search_tickets` directly. That works if `@mcp.tool()` returns the original function, which I believe FastMCP's decorator does. Check with `python -c "import server; print(server.search_tickets)"`: you should see a plain `<function ...>`. If you see something else, drop that test. The `fetch_tickets` tests cover the logic either way.
- I assumed the payload key is `items`, as in your original snippet. The contract test is where that assumption gets checked for real.

