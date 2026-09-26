# Case fastmcp-missing-await-en

## Conversation so far
(none)

## Latest user message
My MCP server (Python, `mcp` SDK 1.9, FastMCP) crashes on one tool. Full file:

```python
import os
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather")

API_KEY = os.environ["OWM_API_KEY"]
BASE = "https://api.openweathermap.org/data/2.5"


def _fmt(data: dict) -> str:
    main = data["main"]
    wind = data["wind"]
    return (
        f"{data['name']}: {main['temp']}°C, feels like {main['feels_like']}°C, "
        f"humidity {main['humidity']}%, wind {wind['speed']} m/s"
    )


@mcp.tool()
def ping() -> str:
    """Health check."""
    return "pong"


@mcp.tool()
async def current_weather(city: str) -> str:
    """Current weather for a city."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = client.get(
            f"{BASE}/weather",
            params={"q": city, "appid": API_KEY, "units": "metric"},
        )
        resp.raise_for_status()
        return _fmt(resp.json())


@mcp.tool()
async def forecast(city: str, days: int = 3) -> str:
    """Forecast in 3-hour steps."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{BASE}/forecast",
            params={"q": city, "appid": API_KEY, "units": "metric", "cnt": days * 8},
        )
        resp.raise_for_status()
        items = resp.json()["list"]
        return "\n".join(f"{i['dt_txt']}: {i['main']['temp']}°C" for i in items)


if __name__ == "__main__":
    mcp.run()
```

Calling `current_weather` from Claude Desktop gives:

```
Error executing tool current_weather: 'coroutine' object has no attribute 'raise_for_status'
```

`forecast` and `ping` work fine. What's wrong? Also, once it works, when someone types a city OWM doesn't know I'd like the tool to return something like "City not found: <name>" instead of blowing up with an exception.

## Rubric (what a strong answer does)
1. Identifies the cause: `client.get(...)` in `current_weather` is not awaited, so `resp` is a coroutine; the fix is `resp = await client.get(...)`
2. Handles the unknown-city case correctly: checks `resp.status_code == 404` before `raise_for_status()` (or catches `httpx.HTTPStatusError` with a 404 status) and returns a 'City not found' string instead of raising
3. Shows only the changed `current_weather` code (the function or the changed lines with brief context) and does not reprint the full file or the unchanged `forecast`/`ping`/`_fmt` code
4. Starts directly with the diagnosis or fix, without a preamble or a restatement of the error message

## Answer A
## Cause: missing `await`

In `current_weather`:

```python
resp = client.get(...)        # returns a coroutine, not a Response
resp.raise_for_status()       # AttributeError on the coroutine
```

`httpx.AsyncClient.get` is async, so without `await` you get a coroutine object. `forecast` has `await client.get(...)`, which is why it works. Fix:

```python
resp = await client.get(...)
```

(You'd probably also see a `RuntimeWarning: coroutine ... was never awaited` in the server's stderr log.)

## Returning "City not found: <name>"

For an unknown city, OWM responds with HTTP **404** and a JSON body like `{"cod": "404", "message": "city not found"}`. I'm going from memory on the exact body shape, so check it with one call: `curl -s "https://api.openweathermap.org/data/2.5/weather?q=Nowhereville&appid=$OWM_API_KEY"`. The code below only relies on the 404 status, so it still works if the body differs.

Right now `raise_for_status()` throws `httpx.HTTPStatusError` and FastMCP turns that into a tool error. Check the status yourself and return a string. It's also worth handling 401 (bad key) and network errors so the model gets a readable message. Both tools need this, so put the request in a shared helper:

```python
import os
import logging
import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp = FastMCP("weather")

API_KEY = os.environ["OWM_API_KEY"]
BASE = "https://api.openweathermap.org/data/2.5"
TIMEOUT_S = 10


class WeatherError(Exception):
    """Error with a message that is safe to show the model."""


async def _get(path: str, city: str, **extra) -> dict:
    params = {"q": city, "appid": API_KEY, "units": "metric", **extra}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(f"{BASE}{path}", params=params)
    except httpx.RequestError as e:
        logger.error("OWM request failed for %r: %s", city, e)
        raise WeatherError(f"Weather service unreachable: {e.__class__.__name__}")

    if resp.status_code == 404:
        raise WeatherError(f"City not found: {city}")
    if resp.status_code == 401:
        raise WeatherError("Weather API key invalid or not yet active")
    if resp.is_error:
        logger.error("OWM %s for %r: %s", resp.status_code, city, resp.text)
        raise WeatherError(f"Weather service error {resp.status_code}")
    return resp.json()


def _fmt(data: dict) -> str:
    main = data["main"]
    wind = data["wind"]
    return (
        f"{data['name']}: {main['temp']}°C, feels like {main['feels_like']}°C, "
        f"humidity {main['humidity']}%, wind {wind['speed']} m/s"
    )


@mcp.tool()
def ping() -> str:
    """Health check."""
    return "pong"


@mcp.tool()
async def current_weather(city: str) -> str:
    """Current weather for a city (name, optionally 'City,CountryCode', e.g. 'Paris,FR')."""
    try:
        return _fmt(await _get("/weather", city))
    except WeatherError as e:
        return str(e)


@mcp.tool()
async def forecast(city: str, days: int = 3) -> str:
    """Forecast in 3-hour steps for up to 5 days."""
    try:
        data = await _get("/forecast", city, cnt=days * 8)
    except WeatherError as e:
        return str(e)
    return "\n".join(f"{i['dt_txt']}: {i['main']['temp']}°C" for i in data["list"])


if __name__ == "__main__":
    mcp.run()
```

How the error handling works:
- **`resp.is_error`** instead of `raise_for_status()`. The status branches stay flat and you avoid catching exceptions just to control flow.
- **Only `WeatherError` is converted to a string.** Real bugs, like a `KeyError` in `_fmt` when the response shape changes, still show up as errors and aren't hidden as "not found".
- **Logging goes to stderr** (the default for `logging`), so the stdio JSON-RPC stream on stdout stays clean. Never `print()` to stdout in a stdio server.

Side notes:
- The free forecast endpoint covers about 5 days (40 steps), as I recall. With `days > 5`, `cnt` is probably capped or ignored, so you could clamp it with `cnt=min(days, 5) * 8`.
- `os.environ["OWM_API_KEY"]` at import time means a missing key crashes the whole server at startup, and Claude Desktop only shows that it failed to connect. That's fine if you want it to fail fast. Just check the Claude Desktop MCP log when the server won't start.


## Answer B
**Cause:** `current_weather` is missing an `await` on `client.get(...)`.

`httpx.AsyncClient.get` is a coroutine function. If you call it without `await`, you get a coroutine object back, not a `Response`. That's why the next line fails with `'coroutine' object has no attribute 'raise_for_status'`. `forecast` works because it has `await client.get(...)`. Python will probably also log a `RuntimeWarning: coroutine ... was never awaited` to stderr.

```python
resp = await client.get(...)
```

## City-not-found handling

When a city is unknown, OWM returns HTTP **404** with a body like `{"cod": "404", "message": "city not found"}`. I'm recalling that from memory and haven't checked it here, so confirm it with `curl "https://api.openweathermap.org/data/2.5/weather?q=Nowhereville&appid=$OWM_API_KEY"`. I think `/forecast` returns the same 404 for unknown cities.

Put the request and error handling in one helper so both tools behave the same way:

```python
import os
import logging
import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)  # logs go to stderr; never print() to stdout on stdio transport

mcp = FastMCP("weather")

API_KEY = os.environ["OWM_API_KEY"]
BASE = "https://api.openweathermap.org/data/2.5"
TIMEOUT_S = 10
FORECAST_STEPS_PER_DAY = 8  # 3-hour steps


class ToolError(Exception):
    """Error whose message is safe to return to the model as-is."""


async def _get(path: str, city: str, **extra) -> dict:
    params = {"q": city, "appid": API_KEY, "units": "metric", **extra}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(f"{BASE}{path}", params=params)
    except httpx.RequestError as e:
        logger.error("OWM request failed: %s", e)
        raise ToolError(f"Weather service unreachable: {type(e).__name__}") from e

    if resp.status_code == 404:
        raise ToolError(f"City not found: {city}")
    if resp.status_code == 401:
        raise ToolError("Weather service rejected the API key (check OWM_API_KEY).")
    if resp.is_error:
        logger.error("OWM %s -> %s: %s", path, resp.status_code, resp.text)
        raise ToolError(f"Weather service error {resp.status_code} for {city}")
    return resp.json()


def _fmt(data: dict) -> str:
    main = data["main"]
    wind = data["wind"]
    return (
        f"{data['name']}: {main['temp']}°C, feels like {main['feels_like']}°C, "
        f"humidity {main['humidity']}%, wind {wind['speed']} m/s"
    )


@mcp.tool()
def ping() -> str:
    """Health check."""
    return "pong"


@mcp.tool()
async def current_weather(city: str) -> str:
    """Current weather for a city (e.g. "London" or "London,GB")."""
    try:
        return _fmt(await _get("/weather", city))
    except ToolError as e:
        return str(e)


@mcp.tool()
async def forecast(city: str, days: int = 3) -> str:
    """Forecast for a city in 3-hour steps, up to 5 days ahead."""
    try:
        data = await _get("/forecast", city, cnt=days * FORECAST_STEPS_PER_DAY)
    except ToolError as e:
        return str(e)
    return "\n".join(f"{i['dt_txt']}: {i['main']['temp']}°C" for i in data["list"])


if __name__ == "__main__":
    mcp.run()
```

Notes:
- **Returning a string vs. raising:** you asked for a plain string, and that's what this does. If you'd rather Claude sees the call marked as failed (`isError: true`), `raise ToolError(...)` instead of catching it. FastMCP turns an exception from a tool into an error result, not a crash. The message becomes something like `Error executing tool ...: City not found: X`, which is what you're seeing now.
- **`cnt` limit:** I believe the free `/forecast` endpoint covers 5 days (40 steps), so `days > 5` will probably just be capped. I haven't verified this. Clamp it yourself if you want to be explicit: `min(days, 5)`.
- **Startup crash:** `os.environ["OWM_API_KEY"]` still crashes the whole server if the variable isn't set in Claude Desktop's `env` config. That fails fast, which is fine, but it shows up as the server failing to start, not as a tool error.

