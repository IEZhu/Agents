# Case youtrack-tool-review-sync-requests-ru

## Conversation so far
(none)

## Latest user message
Привет. Пишу на Python свой MCP-сервер, чтобы Claude мог заводить задачи в наш YouTrack (self-hosted, REST API). Вот что есть сейчас, работает, но криво:

```python
import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("youtrack")
TOKEN = "perm:YWxleGV5.NjEtMTI=.x8Jd93kLqPz"
BASE = "https://yt.company.local/api"

@mcp.tool()
def create_issue(data: dict) -> dict:
    """Create issue"""
    r = requests.post(f"{BASE}/issues", json=data,
                      headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    r.raise_for_status()
    return r.json()

if __name__ == "__main__":
    mcp.run()
```

Проблемы: 1) модель постоянно шлёт в data что попало — то summary нет, то project передаёт строкой "BACKEND" вместо {"id": "0-3"}, и YouTrack отвечает 400; 2) при 400 в чате просто что-то невнятное про ошибку, модель не понимает, что исправить. Код в репозиторий хочу закоммитить на этой неделе. Перепиши нормально и скажи, что поменял и почему.

## Rubric (what a strong answer does)
1. Replaces the untyped `data: dict` with an explicit typed input (named typed parameters or a pydantic BaseModel with field descriptions) and expands the docstring to describe what the tool does and what each argument means, including the project identifier format, explaining that the model sees this schema and docstring when choosing arguments
2. Makes the tool async and uses an async HTTP client (e.g. `async with httpx.AsyncClient(...)`) instead of the blocking `requests` call, noting that a synchronous HTTP call inside the tool blocks the server while it waits
3. Wraps the API call in error handling that returns or reports a clear, actionable message including the YouTrack status code and response body (e.g. which field was invalid) instead of letting a bare raise_for_status exception surface, and logs the error (to stderr/logger, not stdout, since stdio carries the protocol)
4. Removes the hardcoded token, reads it (and ideally the base URL) from environment variables via os.getenv or similar, and warns that the already-exposed token should be revoked/rotated before committing

## Answer A
Привет. Сначала о срочном, потом код.

## 0. Токен: до коммита

`TOKEN = "perm:..."` лежит прямо в коде. Если закоммитить, токен попадёт в историю git навсегда: удаление в следующем коммите его оттуда не уберёт. Кроме того, ты уже вставил его в этот чат.

- **Отзови этот токен и выпусти новый** (YouTrack → Profile → Account Security → Tokens). Старый считай скомпрометированным.
- Новый токен храни только в env (`YOUTRACK_TOKEN`), а `.env` добавь в `.gitignore`.
- Проверь, что токен ещё не попал в историю: `git log -p -S 'perm:' --all`.

## 1. Переписанный сервер

```python
"""YouTrack MCP server: create issues via YouTrack REST API."""
import os
from typing import Annotated

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

YOUTRACK_URL = os.environ.get("YOUTRACK_URL", "https://yt.company.local").rstrip("/")
YOUTRACK_TOKEN = os.environ["YOUTRACK_TOKEN"]  # fail fast: no token -> no server
# Path to internal CA bundle for self-hosted instance; "true" = system CAs
YOUTRACK_CA_BUNDLE = os.environ.get("YOUTRACK_CA_BUNDLE", "true")

API_URL = f"{YOUTRACK_URL}/api"
TIMEOUT_SECONDS = 30
ISSUE_FIELDS = "id,idReadable,summary"
PROJECT_FIELDS = "id,shortName,name"
MAX_SUMMARY_LENGTH = 255

mcp = FastMCP("youtrack")

_client: httpx.AsyncClient | None = None
_project_ids: dict[str, str] = {}  # shortName (upper) -> internal id, e.g. "BACKEND" -> "0-3"


class Project(BaseModel):
    short_name: str
    name: str


class CreatedIssue(BaseModel):
    id_readable: str
    summary: str
    url: str


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        verify: bool | str = YOUTRACK_CA_BUNDLE if YOUTRACK_CA_BUNDLE != "true" else True
        _client = httpx.AsyncClient(
            base_url=API_URL,
            headers={
                "Authorization": f"Bearer {YOUTRACK_TOKEN}",
                "Accept": "application/json",
            },
            timeout=TIMEOUT_SECONDS,
            verify=verify,
        )
    return _client


def _describe_http_error(response: httpx.Response) -> str:
    """Turn a YouTrack error response into a message the model can act on."""
    try:
        body = response.json()
        detail = body.get("error_description") or body.get("error") or response.text
    except ValueError:
        detail = response.text
    hints = {
        400: "Request rejected by YouTrack. Fix the arguments described above and retry.",
        401: "Token invalid or expired. Do not retry; ask the user to check YOUTRACK_TOKEN.",
        403: "Token lacks permission for this project. Do not retry; ask the user.",
        404: "Resource not found. Check the project short name via list_projects.",
    }
    hint = hints.get(response.status_code, "Unexpected YouTrack error.")
    return f"YouTrack HTTP {response.status_code}: {detail}. {hint}"


async def _request(method: str, path: str, **kwargs) -> dict | list:
    try:
        response = await _get_client().request(method, path, **kwargs)
    except httpx.RequestError as exc:
        raise ToolError(f"Cannot reach YouTrack at {API_URL}: {exc!r}") from exc
    if response.is_error:
        raise ToolError(_describe_http_error(response))
    return response.json()


async def _load_projects() -> list[dict]:
    projects = await _request("GET", "/admin/projects", params={"fields": PROJECT_FIELDS})
    _project_ids.clear()
    _project_ids.update({p["shortName"].upper(): p["id"] for p in projects})
    return projects


async def _resolve_project_id(short_name: str) -> str:
    key = short_name.strip().upper()
    if key not in _project_ids:
        await _load_projects()  # cache miss: project may have been created recently
    if key not in _project_ids:
        available = ", ".join(sorted(_project_ids)) or "none visible to this token"
        raise ToolError(
            f"Unknown project '{short_name}'. Use one of the short names: {available}."
        )
    return _project_ids[key]


@mcp.tool()
async def list_projects() -> list[Project]:
    """List YouTrack projects available for issue creation.

    Call this when unsure which project short name to pass to create_issue.
    """
    projects = await _load_projects()
    return [Project(short_name=p["shortName"], name=p["name"]) for p in projects]


@mcp.tool()
async def create_issue(
    project: Annotated[
        str,
        Field(description="Project short name, e.g. 'BACKEND'. Not the internal id."),
    ],
    summary: Annotated[
        str,
        Field(min_length=1, max_length=MAX_SUMMARY_LENGTH, description="One-line issue title."),
    ],
    description: Annotated[
        str | None,
        Field(description="Issue body in Markdown. Optional."),
    ] = None,
) -> CreatedIssue:
    """Create an issue in YouTrack and return its readable id (e.g. BACKEND-123) and URL."""
    payload: dict = {
        "project": {"id": await _resolve_project_id(project)},
        "summary": summary.strip(),
    }
    if description:
        payload["description"] = description

    issue = await _request(
        "POST", "/issues", params={"fields": ISSUE_FIELDS}, json=payload
    )
    return CreatedIssue(
        id_readable=issue["idReadable"],
        summary=issue["summary"],
        url=f"{YOUTRACK_URL}/issue/{issue['idReadable']}",
    )


if __name__ == "__main__":
    mcp.run()
```

`.env.example`:

```bash
YOUTRACK_URL=https://yt.company.local
YOUTRACK_TOKEN=perm:...
# Optional: path to internal CA bundle if the instance uses a corporate certificate
# YOUTRACK_CA_BUNDLE=/etc/ssl/certs/company-ca.pem
```

## 2. Что поменял и почему

| Было | Стало | Почему |
|---|---|---|
| `data: dict` | Типизированные `project`, `summary`, `description` | Из `dict` FastMCP строит пустую JSON Schema, и модель не видит, какие поля нужны. Из типизированных аргументов строится схема с `required` и описаниями полей. Если `summary` не передан, валидация отклонит вызов ещё до запроса в YouTrack. Это закрывает проблему 1. |
| Модель сама собирает `{"project": {"id": "0-3"}}` | Модель передаёт `"BACKEND"`, сервер находит внутренний id | Внутренний id модель знать не может, а short name знает. Сервер берёт маппинг из `/admin/projects` и кэширует его. |
| — | Tool `list_projects` | Модели нужен способ узнать допустимые значения, а не угадывать их. |
| `raise_for_status()` | `ToolError` с текстом `error_description` от YouTrack и подсказкой, что делать дальше | Раньше модель получала голый stack trace `HTTPError`, теперь получает, например, «Unknown project 'BAKEND'. Use one of: BACKEND, FRONTEND». Для 401/403 в тексте прямо сказано не ретраить. Это закрывает проблему 2. |
| Неизвестный проект → 400 от YouTrack | Проверка до POST со списком допустимых значений | Самую частую ошибку ловим заранее, и сообщение получается точным. |
| `requests` (sync) | `httpx.AsyncClient`, один на процесс | Синхронный вызов внутри async-сервера блокирует event loop. Общий клиент переиспользует соединения. |
| Сетевые ошибки не обрабатывались | `httpx.RequestError` → понятный `ToolError` | Модель различает «YouTrack недоступен» и «неверные аргументы». |
| Токен и URL в коде | env, `os.environ[...]` падает сразу при старте | Секрет не попадает в git. Отсутствие токена видно при запуске, а не на первом вызове. |
| Ответ целиком | `fields=id,idReadable,summary` и URL задачи | Без `fields` YouTrack отдаёт почти пустой объект. Модели нужен `BACKEND-123` и ссылка для пользователя. |

## 3. Что я не проверял, проверь сам

Написано по памяти, на твоём инстансе не запускалось:

1. **`/api/admin/projects` отдаёт `shortName` и `id`, а токену хватает прав их читать**:
   `curl -s -H "Authorization: Bearer $YOUTRACK_TOKEN" "$YOUTRACK_URL/api/admin/projects?fields=id,shortName,name"`
   Если придёт 403, у пользователя токена нет доступа на чтение проектов. Тогда маппинг short name → id придётся задать статически в env.
2. **Ошибки YouTrack приходят в полях `error` / `error_description`**: `curl -s -X POST -H "Authorization: Bearer $YOUTRACK_TOKEN" -H "Content-Type: application/json" -d '{}' "$YOUTRACK_URL/api/issues"`. Если поля называются иначе, поправь `_describe_http_error`.
3. **`mcp.server.fastmcp.exceptions.ToolError` существует в твоей версии `mcp`, а FastMCP отдаёт текст исключения модели как `isError`-результат**: `python -c "from mcp.server.fastmcp.exceptions import ToolError"`. Если импорт упадёт, подойдёт обычный `ValueError` с тем же текстом: FastMCP, насколько я помню, тоже передаёт его сообщение.

Если понадобятся обязательные custom fields (Priority, Type и т.п.), YouTrack вернёт 400 с их названием. Такие поля лучше добавить отдельными типизированными аргументами с `Literal[...]`, а не возвращать `dict`.


## Answer B
Привет. Сначала о главном: **в коде захардкожен permanent token**, и ты только что вставил его в чат. Если токен настоящий, отзови его в YouTrack (Profile → Account Security → Tokens) и выпусти новый. Коммитить его нельзя. Если он уже попал хотя бы в локальный коммит, одним удалением строки не обойтись: он останется в истории, её придётся чистить (`git filter-repo` или BFG) либо просто считать токен скомпрометированным.

Теперь по сути. Обе проблемы растут из одного места: `data: dict`. Для модели это «любой JSON», схемы нет, поэтому она гадает. Нужны явные типизированные параметры, преобразование `BACKEND` → `{"id": "0-3"}` внутри сервера и ошибки с текстом, по которому видно, что исправить.

## Код

```python
"""YouTrack MCP server: create issues in a self-hosted YouTrack via REST API."""

import logging
import os
import sys
from typing import Annotated, Any

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

# stdio transport: stdout is the MCP channel, so logs must go to stderr only.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("youtrack_mcp")


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"youtrack_mcp: environment variable {name} is not set")
    return value


BASE_URL = _require_env("YOUTRACK_URL").rstrip("/")  # e.g. https://yt.company.local
TOKEN = _require_env("YOUTRACK_TOKEN")
# Self-hosted with an internal CA: path to the CA bundle. Unset = system trust store.
CA_BUNDLE = os.getenv("YOUTRACK_CA_BUNDLE")

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_ERROR_BODY_CHARS = 1000
ISSUE_FIELDS = "id,idReadable,summary"
PROJECT_FIELDS = "id,shortName,name,archived"

mcp = FastMCP("youtrack")

# shortName (upper-case) -> internal project id, e.g. "BACKEND" -> "0-3"
_project_ids: dict[str, str] = {}


class YouTrackError(Exception):
    """Raised with a message the model can act on."""


class ProjectInfo(BaseModel):
    short_name: str
    name: str
    id: str


class CreatedIssue(BaseModel):
    id_readable: str
    summary: str
    url: str


def _explain_http_error(response: httpx.Response) -> str:
    """Turn a YouTrack error response into an actionable message."""
    try:
        payload = response.json()
        detail = payload.get("error_description") or payload.get("error") or str(payload)
    except ValueError:
        detail = response.text
    detail = detail[:MAX_ERROR_BODY_CHARS]

    hints = {
        400: "The request body was rejected. Check field names and values; do not retry unchanged.",
        401: "Token is invalid or expired. This is a server configuration problem, not a model input problem.",
        403: "Token lacks permission for this project or operation.",
        404: "Resource not found. Check the project short name via list_projects.",
    }
    hint = hints.get(response.status_code, "Unexpected YouTrack error.")
    return f"YouTrack HTTP {response.status_code}: {detail}. {hint}"


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
    }
    verify: str | bool = CA_BUNDLE or True
    try:
        async with httpx.AsyncClient(
            base_url=f"{BASE_URL}/api",
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            verify=verify,
        ) as client:
            response = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        logger.error("YouTrack %s %s failed: %r", method, path, exc)
        raise YouTrackError(f"Cannot reach YouTrack at {BASE_URL}: {exc!r}") from exc

    if response.is_error:
        message = _explain_http_error(response)
        logger.error("YouTrack %s %s -> %s", method, path, message)
        raise YouTrackError(message)
    return response.json()


async def _load_projects() -> list[dict[str, Any]]:
    projects = await _request(
        "GET", "/admin/projects", params={"fields": PROJECT_FIELDS, "$top": 500}
    )
    active = [p for p in projects if not p.get("archived")]
    _project_ids.clear()
    _project_ids.update({p["shortName"].upper(): p["id"] for p in active})
    return active


async def _resolve_project_id(short_name: str) -> str:
    key = short_name.strip().upper()
    if key not in _project_ids:
        await _load_projects()  # cache miss: project may have been created recently
    if key not in _project_ids:
        known = ", ".join(sorted(_project_ids)) or "none visible to this token"
        raise YouTrackError(
            f"Unknown project '{short_name}'. Known project short names: {known}."
        )
    return _project_ids[key]


@mcp.tool()
async def list_projects() -> list[ProjectInfo]:
    """List active YouTrack projects. Use short_name as the `project` argument of create_issue."""
    projects = await _load_projects()
    return [
        ProjectInfo(short_name=p["shortName"], name=p.get("name", ""), id=p["id"])
        for p in projects
    ]


@mcp.tool()
async def create_issue(
    project: Annotated[
        str,
        Field(description="Project short name, e.g. 'BACKEND'. Call list_projects if unsure."),
    ],
    summary: Annotated[
        str,
        Field(min_length=1, max_length=255, description="One-line issue title."),
    ],
    description: Annotated[
        str | None,
        Field(description="Issue body in Markdown. Optional."),
    ] = None,
) -> CreatedIssue:
    """Create a new issue in YouTrack and return its readable ID (e.g. BACKEND-123) and URL.

    Pass the project by its short name; the server resolves it to the internal ID.
    """
    if not summary.strip():
        raise YouTrackError("summary must not be blank.")

    body: dict[str, Any] = {
        "project": {"id": await _resolve_project_id(project)},
        "summary": summary.strip(),
    }
    if description:
        body["description"] = description

    issue = await _request("POST", "/issues", params={"fields": ISSUE_FIELDS}, json=body)
    return CreatedIssue(
        id_readable=issue["idReadable"],
        summary=issue["summary"],
        url=f"{BASE_URL}/issue/{issue['idReadable']}",
    )


if __name__ == "__main__":
    mcp.run()
```

`.env.example`:

```bash
YOUTRACK_URL=https://yt.company.local
YOUTRACK_TOKEN=perm:...
# Optional: internal CA bundle for a self-signed certificate
YOUTRACK_CA_BUNDLE=
```

## Что поменял и зачем

| Было | Стало | Почему |
|---|---|---|
| `data: dict` | `project`, `summary`, `description` как отдельные типизированные параметры | FastMCP строит JSON Schema по сигнатуре. Модель видит обязательный `summary` и описание каждого поля. Если `summary` не передан, запрос отсекает pydantic ещё до YouTrack, и модель получает внятную ошибку валидации. |
| Модель должна знать `{"id": "0-3"}` | Модель передаёт `"BACKEND"`, сервер сам находит id через `/admin/projects` (с кэшем) | Внутренний id модели знать неоткуда, а short name она видит в интерфейсе и в тексте. Этот маппинг должен делать сервер, а не LLM. |
| Нет способа узнать проекты | Tool `list_projects` | Если модель ошиблась с проектом, в ошибке будет список допустимых short name, и есть tool, чтобы получить их заранее. |
| `raise_for_status()` | `_explain_http_error`: статус, текст ошибки YouTrack и подсказка, что делать | `raise_for_status` выдаёт общее `400 Bad Request for url ...`, а тело ответа с реальной причиной выбрасывает. Теперь модель видит, какое поле не понравилось, и понимает, стоит ли вообще ретраить (401/403 от её ввода не зависят). |
| `requests` (sync) | `httpx.AsyncClient` | Синхронный вызов на 30 секунд блокирует event loop MCP-сервера целиком. |
| Токен и URL в коде | `YOUTRACK_TOKEN` / `YOUTRACK_URL` из env, при отсутствии сервер сразу падает с понятным сообщением | Секреты не место в репозитории. Лучше честно упасть на старте, чем получить невнятный 401 в чате. |
| Ответ без `fields` | `?fields=id,idReadable,summary`, наружу возвращается `CreatedIssue` с URL | Без `fields` YouTrack отдаёт почти пустой объект. Модели нужен `BACKEND-123` и ссылка, чтобы показать их пользователю. |
| — | `YOUTRACK_CA_BUNDLE` | Для `.local` у вас почти наверняка внутренний CA. Правильнее передать `verify=`, чем отключать проверку TLS. |
| — | Логи в stderr | При stdio-транспорте любой `print` в stdout ломает MCP-протокол. |

## Что проверить перед коммитом

Всё ниже я написал по памяти и на твоём YouTrack не запускал:

1. **Формат ошибки и `/admin/projects`.** Насколько я помню, YouTrack отдаёт ошибки как `{"error": ..., "error_description": ...}`, а `/api/admin/projects` доступен обычному токену для проектов, которые он может читать. Это стоит подтвердить:
   ```bash
   curl -s -H "Authorization: Bearer $YOUTRACK_TOKEN" \
     "$YOUTRACK_URL/api/admin/projects?fields=id,shortName,name"
   curl -s -X POST -H "Authorization: Bearer $YOUTRACK_TOKEN" -H "Content-Type: application/json" \
     -d '{"project":"BACKEND"}' "$YOUTRACK_URL/api/issues"
   ```
   Второй запрос специально некорректный: он покажет реальное тело ответа на 400. Если формат другой, поправь ключи в `_explain_http_error`.
2. **Как исключение попадает к модели.** В FastMCP (по памяти) исключение из tool превращается в результат с `isError: true` и текстом исключения, так что модель видит сообщение `YouTrackError`. Возврат pydantic-моделей как structured output есть в свежих версиях `mcp` SDK. Проверь по своей версии: `pip show mcp`.
3. **Custom fields** (Type, Priority, Assignee) я не добавлял. В YouTrack они передаются через `customFields` с `$type` вроде `SingleEnumIssueCustomField`, и у каждого инстанса своя схема. Если понадобятся, я бы добавил их так же, как проект: отдельными параметрами с `Literal[...]` или с резолвом по имени на стороне сервера, а не свободным dict.

