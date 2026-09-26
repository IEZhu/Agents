Нужно два изменения: жёсткий дедлайн на запрос и перехват ошибок внутри инструмента. Ошибку отдаём через `ToolError`, тогда клиент получит короткое сообщение, а не трейсбек.

Сначала поправка к исходной посылке. Насколько я помню, у httpx уже есть таймаут по умолчанию, 5 секунд (recalled, not verified: проверить можно командой `python -c "import httpx; print(httpx.AsyncClient().timeout)"`). Но это таймаут **на каждую операцию** (connect, read одного чанка и т.д.), а не на весь запрос. Если Jira медленно отдаёт тело по кусочкам, запрос может идти и 40 секунд, ни разу не превысив read-timeout. Поэтому `httpx.Timeout(10)` сам по себе 10 секунд на запрос не гарантирует. Нужен общий дедлайн через `asyncio.wait_for`.

```python
import asyncio
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

mcp = FastMCP("jira-lite")

JIRA_URL = os.environ["JIRA_URL"].rstrip("/")
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
DEADLINE = float(os.getenv("JIRA_TIMEOUT", "10"))

HEADERS = {
    "Authorization": f"Bearer {JIRA_TOKEN}",
    "Accept": "application/json",
}


async def _get(path: str, params: dict | None = None) -> dict:
    """GET to Jira with a hard total deadline; raises ToolError with a readable message."""

    async def _do() -> dict:
        # per-operation timeouts as a second line of defense
        async with httpx.AsyncClient(timeout=httpx.Timeout(DEADLINE)) as client:
            r = await client.get(f"{JIRA_URL}{path}", params=params, headers=HEADERS)
            r.raise_for_status()
            return r.json()

    try:
        return await asyncio.wait_for(_do(), timeout=DEADLINE)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise ToolError(
            f"Jira did not respond within {DEADLINE:.0f}s. "
            "Try again later or narrow the JQL query."
        )
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        hint = {
            400: "invalid JQL or parameters",
            401: "invalid or expired token",
            403: "no permission",
            404: "issue not found",
        }.get(code, "unexpected response")
        raise ToolError(f"Jira returned HTTP {code}: {hint}. {e.response.text[:300]}")
    except httpx.RequestError as e:
        raise ToolError(f"Cannot reach Jira ({type(e).__name__}): {e}")


@mcp.tool()
async def get_issue(key: str) -> dict:
    """Return summary, status and assignee of a Jira issue by key (e.g. PROJ-123)."""
    data = await _get(f"/rest/api/2/issue/{key}")
    f = data["fields"]
    return {
        "key": data["key"],
        "summary": f["summary"],
        "status": f["status"]["name"],
        "assignee": (f.get("assignee") or {}).get("displayName"),
    }


@mcp.tool()
async def search_issues(jql: str, limit: int = 20) -> list[dict]:
    """Search Jira issues with JQL. Returns up to `limit` issues. May time out on heavy JQL."""
    data = await _get("/rest/api/2/search", {"jql": jql, "maxResults": limit})
    return [
        {"key": i["key"], "summary": i["fields"]["summary"]}
        for i in data["issues"]
    ]


if __name__ == "__main__":
    mcp.run()
```

Что изменилось:

- **`asyncio.wait_for(..., 10)`** ограничивает весь запрос целиком. По истечении срока корутина отменяется, а httpx закрывает соединение.
- **`httpx.Timeout(10)`** — запасная страховка на уровне операций.
- **`ToolError`**: FastMCP превращает его в результат инструмента с `isError: true` и текстом сообщения, так что модель видит «Jira did not respond within 10s…» и может сама предложить сузить JQL. Путь импорта `mcp.server.fastmcp.exceptions.ToolError` и такое поведение я пишу по памяти, не проверял (recalled, not verified: проверить можно командой `python -c "from mcp.server.fastmcp.exceptions import ToolError"`). Если импорт не пройдёт, подойдёт обычный `raise RuntimeError("...")`: FastMCP, насколько я знаю, тоже оборачивает его в `isError`, только текст получится менее аккуратным.
- Почему не `return {"error": ...}`: у `search_issues` в сигнатуре указан `list[dict]`. Новые версии FastMCP строят по аннотации схему structured output, и dict вместо списка может не пройти валидацию. Исключение от этого не зависит.
- Таймаут вынесен в env `JIRA_TIMEOUT`, по умолчанию 10.

Если хотите, чтобы на тяжёлом JQL всё-таки приходил хоть какой-то результат, можно уменьшить `maxResults` и передавать `fields=summary`: Jira тогда обычно отвечает заметно быстрее.
