# Case mcp-httpx-timeout-followup-ru

## Conversation so far
### User
Набросай минимальный MCP-сервер на Python (FastMCP) для Jira: получить задачу по ключу и поиск по JQL. Токен из env.

### Assistant
```python
import os
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("jira-lite")

JIRA_URL = os.environ["JIRA_URL"]
JIRA_TOKEN = os.environ["JIRA_TOKEN"]

HEADERS = {
    "Authorization": f"Bearer {JIRA_TOKEN}",
    "Accept": "application/json",
}


@mcp.tool()
async def get_issue(key: str) -> dict:
    """Return summary, status and assignee of a Jira issue by key (e.g. PROJ-123)."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{JIRA_URL}/rest/api/2/issue/{key}", headers=HEADERS)
        r.raise_for_status()
        data = r.json()
    f = data["fields"]
    return {
        "key": data["key"],
        "summary": f["summary"],
        "status": f["status"]["name"],
        "assignee": (f.get("assignee") or {}).get("displayName"),
    }


@mcp.tool()
async def search_issues(jql: str, limit: int = 20) -> list[dict]:
    """Search Jira issues with JQL. Returns up to `limit` issues."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{JIRA_URL}/rest/api/2/search",
            params={"jql": jql, "maxResults": limit},
            headers=HEADERS,
        )
        r.raise_for_status()
        data = r.json()
    return [
        {"key": i["key"], "summary": i["fields"]["summary"]}
        for i in data["issues"]
    ]


if __name__ == "__main__":
    mcp.run()
```

Запуск: `JIRA_URL=https://jira.example.com JIRA_TOKEN=... python server.py`.

## Latest user message
Работает, спасибо. Но наш Jira иногда тупит по 40+ секунд на JQL, и Claude Desktop висит. Хочу таймаут 10 секунд на все запросы, и чтобы при таймауте инструмент возвращал понятную ошибку, а не падал с трейсбеком. Что поменять?

## Rubric (what a strong answer does)
1. Задаёт таймаут 10 секунд для httpx (например, httpx.AsyncClient(timeout=10) или timeout=10.0 в запросе) так, чтобы он действовал на оба инструмента.
2. Перехватывает httpx.TimeoutException (или его подкласс/родителя) и возвращает понятное сообщение об ошибке вместо необработанного исключения.
3. Показывает только изменённые фрагменты (или вынос общего клиента/хелпера), а не перепечатывает весь файл сервера из предыдущего ответа.
4. Нет вводных фраз и пересказа задачи; ответ начинается сразу с правки или её сути.

## Answer A
Нужно поменять две вещи. Первая: все запросы идут через один helper с таймаутом. Вторая: этот helper ловит ошибки `httpx` и превращает их в понятную ошибку инструмента.

Сначала оговорка. `httpx.Timeout(10)` не ограничивает запрос целиком: это лимит на каждую фазу отдельно (connect, read, write, pool). `read` считается между порциями данных, а не на весь ответ. Если Jira отдаёт тело медленно и по кускам, запрос может идти дольше 10 секунд. Поэтому для жёсткого потолка я добавил ещё `asyncio.wait_for`.

Кстати, у `httpx` уже есть таймаут по умолчанию, около 5 секунд на фазу (по памяти, не проверял). Раз запрос висит 40+ секунд, скорее всего Jira понемногу отдаёт данные, либо зависание вообще не в HTTP. `wait_for` сработает в обоих случаях.

```python
import asyncio
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

mcp = FastMCP("jira-lite")

JIRA_URL = os.environ["JIRA_URL"].rstrip("/")
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
TIMEOUT_S = float(os.getenv("JIRA_TIMEOUT", "10"))

HEADERS = {
    "Authorization": f"Bearer {JIRA_TOKEN}",
    "Accept": "application/json",
}


async def _jira_get(path: str, params: dict | None = None) -> dict:
    """GET to Jira with a hard total deadline; errors become readable ToolErrors."""
    try:
        async with httpx.AsyncClient(
            base_url=JIRA_URL, headers=HEADERS, timeout=TIMEOUT_S
        ) as client:
            r = await asyncio.wait_for(client.get(path, params=params), TIMEOUT_S)
            r.raise_for_status()
            return r.json()
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise ToolError(
            f"Jira did not respond within {TIMEOUT_S:.0f}s ({path}). "
            "Try a narrower JQL (project/date filter) or retry later."
        ) from None
    except httpx.HTTPStatusError as e:
        raise ToolError(
            f"Jira returned HTTP {e.response.status_code}: {e.response.text[:300]}"
        ) from None
    except httpx.RequestError as e:
        raise ToolError(f"Network error talking to Jira: {type(e).__name__}: {e}") from None
    except ValueError:
        raise ToolError("Jira returned a non-JSON response.") from None


@mcp.tool()
async def get_issue(key: str) -> dict:
    """Return summary, status and assignee of a Jira issue by key (e.g. PROJ-123)."""
    data = await _jira_get(f"/rest/api/2/issue/{key}")
    f = data["fields"]
    return {
        "key": data["key"],
        "summary": f["summary"],
        "status": f["status"]["name"],
        "assignee": (f.get("assignee") or {}).get("displayName"),
    }


@mcp.tool()
async def search_issues(jql: str, limit: int = 20) -> list[dict]:
    """Search Jira issues with JQL. Returns up to `limit` issues.
    Slow queries time out after ~10s: prefer JQL restricted by project and date."""
    data = await _jira_get(
        "/rest/api/2/search",
        params={"jql": jql, "maxResults": limit, "fields": "summary"},
    )
    return [
        {"key": i["key"], "summary": i["fields"]["summary"]}
        for i in data.get("issues", [])
    ]


if __name__ == "__main__":
    mcp.run()
```

Что изменилось:

- **Одна точка входа `_jira_get`.** Таймаут и обработка ошибок теперь в одном месте, а не в каждом инструменте.
- **`timeout=TIMEOUT_S` + `asyncio.wait_for`.** Первый задаёт лимиты на каждую фазу, второй ограничивает всё время запроса. Значение можно переопределить через `JIRA_TIMEOUT`.
- **`ToolError` с текстом.** FastMCP вернёт клиенту результат с `isError=True` и вашим сообщением. Claude увидит «Jira did not respond within 10s…» и сможет, например, предложить сузить JQL. Сервер при этом продолжит работать. `from None` убирает цепочку исключений из сообщения. Путь импорта `mcp.server.fastmcp.exceptions.ToolError` я пишу по памяти, не проверял. Если импорт не пройдёт, используйте обычный `RuntimeError`: FastMCP, насколько я помню, превращает любое исключение в инструменте в error-результат, только сообщение будет с префиксом «Error executing tool …».
- **`fields=summary` в поиске.** Jira вернёт меньше данных, так что ответ может стать быстрее. Но если тормозит сам JQL на стороне Jira, это не поможет.

Учтите, что таймаут на клиенте не отменяет запрос в Jira: сервер Jira всё равно доработает свои 40 секунд. Если зависания частые, дело скорее в самих JQL-запросах, например без фильтра по `project`, или с `text ~` по всей базе, или с `ORDER BY` по неиндексированному полю.

Как проверить без медленной Jira: запустите с `JIRA_TIMEOUT=0.001` и вызовите `search_issues`. Должна вернуться ошибка с текстом, а не трейсбек.


## Answer B
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

