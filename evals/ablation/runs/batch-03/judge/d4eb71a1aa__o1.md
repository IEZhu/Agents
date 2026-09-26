# Case flask-dynamic-order-by-highlight-ru

## Conversation so far
(none)

## Latest user message
Flask 3.0 + psycopg2, Postgres 15. Есть эндпоинт поиска товаров:

```python
@app.get("/products")
def products():
    q = request.args.get("q", "")
    sort = request.args.get("sort", "created_at")
    direction = request.args.get("dir", "desc")
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, price FROM products WHERE name ILIKE %s ORDER BY %s %s",
        (f"%{q}%", sort, direction),
    )
    rows = cur.fetchall()
    highlighted = [(r[0], r[1].replace(q, f"<mark>{q}</mark>"), r[2]) for r in rows] if q else rows
    return render_template("products.html", rows=highlighted, q=q)
```

В шаблоне:
```html
<h2>Результаты по запросу: {{ q|safe }}</h2>
{% for id, name, price in rows %}<li>{{ name|safe }} — {{ price }}</li>{% endfor %}
```

Проблема: сортировка вообще не работает, падает с `psycopg2.errors.SyntaxError: syntax error at or near "'desc'"`, а если убрать направление — порядок не меняется. Как сделать, чтобы sort и dir из query-параметров нормально подставлялись?

## Rubric (what a strong answer does)
1. Explains correctly that %s placeholders bind values, not identifiers/keywords, so column name and ASC/DESC cannot be passed as query parameters (this is why it fails / sorts by a constant).
2. Does not interpolate raw request values into SQL: validates sort and dir against an explicit allow-list (or builds identifiers with psycopg2.sql.Identifier plus a fixed ASC/DESC choice).
3. Points out that `{{ q|safe }}` / `{{ name|safe }}` with unescaped user input allows XSS, and proposes a fix that keeps highlighting while escaping (e.g. escape q and name first with markupsafe.escape, then wrap in <mark>, or drop |safe on the heading).
4. Keeps the ILIKE filter parameterized and gives working corrected code for the endpoint.

## Answer A
### Коротко

Через `%s` передаются только **значения**. Имена колонок и ключевые слова (`ASC`/`DESC`) так передать нельзя. psycopg2 превращает их в строковые литералы, поэтому запрос выходит таким:

```sql
... ORDER BY 'created_at' 'desc'
```

- `'desc'` здесь просто строка, отсюда `syntax error at or near "'desc'"`.
- `ORDER BY 'created_at'` сортирует по константе, у всех строк она одинаковая. Поэтому порядок и не меняется.

Решение: проверять `sort` и `dir` по whitelist, а потом собирать SQL через модуль `psycopg2.sql` (`sql.Identifier` для колонки, `sql.SQL` для направления из фиксированного набора). Склеивать через f-string нельзя: это прямая SQL-инъекция.

### Исправленный эндпоинт

```python
import re

from flask import abort, render_template, request
from markupsafe import Markup, escape
from psycopg2 import sql

ALLOWED_SORT_COLUMNS = {"created_at", "name", "price"}
ALLOWED_DIRECTIONS = {"asc": sql.SQL("ASC"), "desc": sql.SQL("DESC")}


def escape_like(value: str) -> str:
    """Экранирует спецсимволы LIKE, чтобы '%' и '_' в запросе искались буквально."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.get("/products")
def products():
    q = request.args.get("q", "")
    sort = request.args.get("sort", "created_at")
    direction = request.args.get("dir", "desc").lower()

    if sort not in ALLOWED_SORT_COLUMNS:
        abort(400, description=f"Unsupported sort column: {sort!r}")
    if direction not in ALLOWED_DIRECTIONS:
        abort(400, description=f"Unsupported sort direction: {direction!r}")

    query = sql.SQL(
        "SELECT id, name, price FROM products "
        "WHERE name ILIKE %s "
        "ORDER BY {column} {direction}, id {direction}"
    ).format(
        column=sql.Identifier(sort),
        direction=ALLOWED_DIRECTIONS[direction],
    )

    with conn.cursor() as cur:
        cur.execute(query, (f"%{escape_like(q)}%",))
        rows = cur.fetchall()

    highlighted = [(pid, highlight(name, q), price) for pid, name, price in rows]
    return render_template("products.html", rows=highlighted, q=q)
```

Что здесь изменилось:
- **Whitelist обязателен**, даже с `sql.Identifier`. Identifier защищает от инъекции, но не мешает отсортировать по любой колонке таблицы или получить 500 на несуществующую. Если значение не проходит проверку, отдаём 400 с понятной ошибкой. Если нужно молча откатываться к дефолту, замените `abort` на присваивание значения по умолчанию.
- **`id` как tiebreaker**: при одинаковых `price`/`created_at` без него порядок недетерминирован, и пагинация «плавает».
- **`escape_like`**: сейчас запрос `q=%` находит всё, а `_` работает как wildcard. В Postgres обратный слэш по умолчанию служит escape-символом для `LIKE`/`ILIKE`, поэтому этого достаточно.

### Вторая проблема: XSS (важнее сортировки)

В шаблоне сейчас дыра, и её нужно закрыть в этом же PR:

- `{{ q|safe }}` даёт reflected XSS: ссылка `/products?q=<script>...</script>` выполнит скрипт у любого, кто по ней перейдёт.
- `{{ name|safe }}` даёт stored XSS, если названия товаров может редактировать кто-то кроме вас (админка, импорт, продавцы).
- Кроме того, `r[1].replace(q, ...)` регистрозависим, а `ILIKE` нет. Поиск `phone` найдёт `iPhone`, но подсветки не будет.

Правильный подход: экранировать всё, а `Markup` ставить только на свои теги `<mark>`:

```python
def highlight(text: str, q: str) -> Markup:
    """Экранирует text и оборачивает совпадения с q (без учёта регистра) в <mark>."""
    if not q:
        return escape(text)
    pattern = re.compile(re.escape(q), re.IGNORECASE)
    parts: list[Markup] = []
    last_end = 0
    for match in pattern.finditer(text):
        parts.append(escape(text[last_end:match.start()]))
        parts.append(Markup("<mark>{}</mark>").format(match.group()))  # format() экранирует аргумент
        last_end = match.end()
    parts.append(escape(text[last_end:]))
    return Markup("").join(parts)
```

Шаблон без `|safe`:

```html
<h2>Результаты по запросу: {{ q }}</h2>
{% for id, name, price in rows %}<li>{{ name }} — {{ price }}</li>{% endfor %}
```

`name` уже приходит как `Markup`, поэтому Jinja не экранирует его повторно и `<mark>` отрисуется. А `q` автоматически экранируется.

### Ещё один момент: глобальный `conn`

После вашей `SyntaxError` транзакция на этом соединении переходит в состояние aborted. Пока не будет сделан `rollback()`, все следующие запросы падают с `InFailedSqlTransaction`, в том числе у других пользователей, если соединение общее. Кроме того, одно соединение на всё приложение плохо живёт под многопоточным сервером. Лучше брать соединение из пула (`psycopg2.pool.ThreadedConnectionPool`) или открывать его на запрос через `g` с закрытием в `teardown_appcontext`. Это отдельная задача, но если сортировка «иногда ломает весь поиск», причина почти наверняка здесь.

### Проверка

Код я не запускал. Что стоит покрыть тестами (Flask `test_client`):
- `?sort=price&dir=asc` и `?sort=price&dir=desc` дают противоположный порядок;
- `?sort=password` и `?dir=desc;drop` возвращают 400;
- `?q=%` не возвращает все товары;
- `?q=<script>` в HTML-ответе приходит как `&lt;script&gt;`;
- `?q=PHONE` подсвечивает `iPhone` с сохранением исходного регистра.

Сложность: `highlight` работает за O(len(name)) на строку, запрос выполняется за O(n log n) по отфильтрованным строкам. Если таблица большая, индекс под `ILIKE '%...%'` можно сделать через `pg_trgm` (GIN), но это стоит делать только после `EXPLAIN ANALYZE`.

### Confidence

MEDIUM. `psycopg2.sql.SQL`/`Identifier` и `markupsafe.Markup.format`/`escape` работают так, как описано выше, но я не проверял это на вашем окружении. Прогоните тесты из списка.


## Answer B
**Коротко:** параметры `%s` в psycopg2 подставляют только **значения** (literals), а не идентификаторы и ключевые слова. `ORDER BY %s %s` превращается в `ORDER BY 'created_at' 'desc'`: `'desc'` как строковый литерал даёт синтаксическую ошибку, а `ORDER BY 'price'` сортирует по константе, поэтому порядок и не меняется. Имя колонки и направление нужно брать из whitelist и собирать через модуль `psycopg2.sql`.

Но сначала о более серьёзной проблеме в этом же коде: эндпоинт уязвим к XSS (об этом ниже), и это стоит исправить одновременно с сортировкой.

## 1. Сортировка: whitelist + `psycopg2.sql`

```python
from flask import Flask, abort, render_template, request
from psycopg2 import sql

# Публичное имя параметра -> реальная колонка. Клиент никогда не передаёт имя колонки напрямую.
SORT_COLUMNS = {
    "created_at": "created_at",
    "name": "name",
    "price": "price",
}
SORT_DIRECTIONS = {
    "asc": sql.SQL("ASC"),
    "desc": sql.SQL("DESC"),
}
DEFAULT_SORT = "created_at"
DEFAULT_DIRECTION = "desc"


def escape_like(value: str) -> str:
    """Экранирует спецсимволы LIKE, чтобы '%' и '_' в запросе искались буквально."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.get("/products")
def products():
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", DEFAULT_SORT)
    direction = request.args.get("dir", DEFAULT_DIRECTION).lower()

    if sort not in SORT_COLUMNS or direction not in SORT_DIRECTIONS:
        abort(400, description="Invalid sort or dir parameter")

    query = sql.SQL(
        "SELECT id, name, price FROM products "
        "WHERE name ILIKE %s "
        "ORDER BY {column} {direction}, id {direction}"
    ).format(
        column=sql.Identifier(SORT_COLUMNS[sort]),
        direction=SORT_DIRECTIONS[direction],
    )

    with conn.cursor() as cur:
        cur.execute(query, (f"%{escape_like(q)}%",))
        rows = cur.fetchall()

    items = [(pid, highlight(name, q), price) for pid, name, price in rows]
    return render_template("products.html", rows=items, q=q)
```

Что здесь важно:

- **`sql.Identifier`** корректно квотирует имя колонки (`"price"`), а **`sql.SQL`** вставляет фрагмент как есть. Поэтому направление берётся только из заранее созданных объектов `sql.SQL("ASC")` / `sql.SQL("DESC")` и никогда не строится из пользовательской строки. Иначе получилась бы SQL-инъекция.
- **Whitelist обязателен даже с `Identifier`.** Без него клиент может отсортировать по любой колонке таблицы (например, по внутренней служебной), а ошибка «нет такой колонки» превратится в 500.
- **`abort(400)` вместо молчаливого fallback.** Так клиент узнаёт об опечатке в `sort`. Если по продуктовым причинам нужен именно fallback на дефолт, замените `abort` на подстановку `DEFAULT_*`.
- **`id` как tie-breaker** даёт стабильный порядок при одинаковых `price`/`name`. Без него пагинация (если она появится) будет «прыгать».
- **`escape_like`**: сейчас `q=%` или `q=_` фактически отключает фильтр. В Postgres обратный слеш по умолчанию служит escape-символом для `LIKE`/`ILIKE`, поэтому отдельный `ESCAPE` указывать не нужно.

## 2. XSS: уберите `|safe`

Сейчас `{{ q|safe }}` отдаёт пользовательский ввод в HTML без экранирования. Ссылка вида `/products?q=<script>...</script>` выполнит скрипт у любого, кто по ней перейдёт (reflected XSS). То же с `{{ name|safe }}`: `replace` вставляет сырой `q` внутрь `<mark>`, а само `name` из БД тоже не экранируется (stored XSS, если названия товаров можно редактировать).

Правильная схема: экранировать всё и помечать как безопасный только собственный `<mark>`:

```python
import re
from markupsafe import Markup, escape


def highlight(text: str, needle: str) -> Markup:
    """Экранирует текст и оборачивает в <mark> совпадения с needle (без учёта регистра, как ILIKE)."""
    if not needle:
        return escape(text)
    parts = re.split(f"({re.escape(needle)})", text, flags=re.IGNORECASE)
    # Нечётные индексы после re.split с группой: это совпадения.
    return Markup("").join(
        Markup("<mark>{}</mark>").format(part) if i % 2 else escape(part)
        for i, part in enumerate(parts)
    )
```

`Markup.format` экранирует аргументы, поэтому `part` внутри `<mark>` тоже безопасен. Заодно исправляется ещё одна ошибка: старый `str.replace` чувствителен к регистру, а `ILIKE` нет, поэтому `q=phone` не подсвечивал `iPhone`/`Phone`.

Шаблон:

```html
<h2>Результаты по запросу: {{ q }}</h2>
{% for id, name, price in rows %}<li>{{ name }} — {{ price }}</li>{% endfor %}
```

`name` уже объект `Markup`, и Jinja не будет экранировать его повторно. `|safe` больше нигде не нужен.

## 3. Глобальный `conn`

Это напрямую связано с вашей ошибкой. После `SyntaxError` транзакция на этом соединении переходит в состояние aborted, и все последующие запросы через тот же `conn` падают с `current transaction is aborted, commands ignored until end of transaction block`, пока не будет вызван `rollback()`. Если после первой ошибки у вас «сломалось вообще всё», причина в этом. Кроме того, одно соединение на весь процесс плохо работает с многопоточным сервером. Минимальный вариант: `psycopg2.pool.ThreadedConnectionPool`, соединение берётся на запрос (через `g` + `teardown_appcontext`) и оборачивается в `with conn:`, чтобы при исключении автоматически выполнялся rollback.

## Проверка

Тесты, которые стоит добавить (pytest + `app.test_client()`):

- `?sort=price&dir=asc` и `?sort=price&dir=desc` возвращают противоположный порядок цен.
- `?sort=password` и `?dir=desc;DROP TABLE products` дают 400, а не 500.
- `?q=<script>alert(1)</script>`: в ответе есть `&lt;script&gt;`, а сырого `<script>` нет.
- `?q=%`: возвращаются только товары с `%` в названии, а не весь каталог.
- `?q=phone` подсвечивает `iPhone` как `i<mark>Phone</mark>`.

**Уверенность: MEDIUM.** Корневая причина (параметры подставляются только как литералы) и API `psycopg2.sql` / `markupsafe` стандартные, но этот код я не запускал на вашей схеме. Прогоните тесты выше.

