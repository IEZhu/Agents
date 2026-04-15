# Подсистема памяти Agents-Core: `describe` + `history.md`

> ТЗ и пошаговая реализация механизма репо-памяти для MCP-сервера Agents-Core.
> Статус: реализовано 2026-04-15. См. Приложение C — отличия фактической имплементации от плана.

> **Update 2026-04-15 (post-implementation):** MCP-инструмент `record_history` слит с
> `log_interaction`. Теперь `log_interaction(...)` всегда дозаписывает в `history.md` (с
> опциональными `intent`/`action`/`outcome`/`files`/`tags` для курирования) и, если
> подключен Langfuse, дополнительно отправляет generation-трейс. Класс `HistoryWriter`
> и формат `history.md` не изменились — описанное ниже про дедупликацию, ротацию,
> формат и `read_history` остаётся в силе. Отдельный MCP-tool `record_history` удалён,
> чтобы не дублировать инструкции для модели.

---

## 1. Контекст и мотивация

У MCP-сервера Agents-Core (`/home/wondermr/repos/Agents`) сейчас нет постоянной per-repo памяти: каждая новая сессия Claude Code заново изучает кодовую базу и забывает смысл прошлых действий. Это сжигает токены, теряет архитектурные решения и делает работу невоспроизводимой.

Внедряем два дополняющих механизма — файловые (markdown), привязанные к репозиторию, переиспользуемые между LLM:

1. **Режим `describe`** — однократный bootstrap, который дистиллирует репозиторий в качественный краткий обзор, сохраняемый в управляемую секцию `CLAUDE.md`. Будущие сессии читают его вместо повторного исследования.
2. **Режим `history.md`** — append-only журнал *intent + action + outcome* для каждого значимого turn'а; даёт воспроизводимую историю действий, которую можно подгружать в контекст по требованию.

Подсистема **переиспользует существующие примитивы** Agents-Core (FastMCP-сервер, `NumpyVectorStore`, маркер-редактор `CLAUDE.md`, эмбеддер, hash-инвалидация). Никакой новой инфраструктуры не вводится.

### Проектные решения (зафиксированы)

| Решение | Выбор | Обоснование |
|---|---|---|
| Способ генерации describe | **Prompt + MCP sampling** | Сервер строит prompt и контекст-bundle, через `ctx.session.create_message(...)` просит вызывающий LLM сгенерировать summary, затем сам пишет результат в `CLAUDE.md`. Уже используется в `route_and_load` (`src/server.py:196`). |
| Расположение `history.md` | **Корень репо, коммитится в git** | Видим в PR, ревьюабельно, шарится между разработчиками. `.gitignore` opt-out — закомментированный. |
| Триггер записи в history | **Явный `record_history()`** | Без хуков (в репо пока нет `.claude/settings.json`). Инструкция в `CLAUDE.md` обязывает Claude вызывать tool в конце каждого значимого turn'а. |
| Семантический поиск | **Lazy** | `record_history` остаётся быстрым (только append). `NumpyVectorStore` строится при первом `read_history(query=...)` и инкрементально обновляется по mtime. |

### Сверка с аналогами

| Источник | Чем вдохновляемся |
|---|---|
| **claude-recall** (TS, SQLite, hooks-driven) | Outcome-aware memory; content-hash dedup; JIT-инъекция активных правил. **Не берём:** SQLite (для нашего use-case markdown проще и git-friendly), хуки (Phase 2). |
| **mcp-memory-keeper** (TS, SQLite, explicit tools) | Идея явных tool'ов вместо хуков; checkpoint-семантика; channel-based scoping. **Адаптируем:** explicit tool surface, без channels (пока). |
| **mcp-memory-service** (Python, REST+MCP) | Идея autonomous consolidation (decay + compress) — кладём в Phase 5. **Не берём:** knowledge graph с типизированными рёбрами. |
| **Mintlify guidance** | Принцип: native CLAUDE.md = постоянный контекст; MCP = real-time запросы. У нас describe пишет в CLAUDE.md (читается на старте), history запрашивается по требованию через MCP. |

---

## 2. Цели и не-цели

**Цели**
- Три новых MCP-инструмента: `describe_repo`, `record_history`, `read_history`.
- Идемпотентное, неразрушающее редактирование `CLAUDE.md` через новую пару маркеров (отдельную от существующей секции routing-protocol).
- Append-only `history.md` в корне репо с content-hash dedup, ежемесячной ротацией, опциональным семантическим recall.
- Тесты в стиле существующих (`pytest`, `tmp_path`, mock-эмбеддер).
- Промпт describe-режима — production-grade (BLUF, MECE, структура Mega-Prompting).

**Не-цели**
- Hooks для авто-захвата (Phase 2; в репо ещё нет `.claude/settings.json` с хуками).
- Knowledge graph с типизированными рёбрами.
- Consolidation/decay памяти (Phase 5).
- Cross-repo федерация памяти.
- Изменение существующей секции routing-protocol в `CLAUDE.md`.

---

## 3. Архитектура

### 3.1 Поток данных

```
describe_repo(repo_path?, force_refresh=False)
  ├─ RepoDescriber.compute_repo_hash()       # MD5 от pyproject/package.json/top-level dirs/начала README
  ├─ если хэш не изменился и не force → return {status:"up-to-date"}
  ├─ обход дерева (depth ≤ 3, исключаем vendor) + чтение ключевых файлов → CONTEXT_BUNDLE
  ├─ render DESCRIBE_PROMPT (template) с CONTEXT_BUNDLE
  ├─ ctx.session.create_message(prompt)      # MCP sampling — Claude генерирует summary
  ├─ managed_section.upsert(CLAUDE.md, DESCRIBE_MARKER_BEGIN/END, summary)
  ├─ сохраняем хэш → DESCRIBE_HASH_FILE
  └─ return {status, path, hash, word_count, summary_preview}

record_history(intent, action, outcome, files?, tags?, metadata?)
  ├─ HistoryWriter.compute_entry_hash()      # SHA256(intent+action+outcome)[:12]
  ├─ скан последних 50 записей на дубль → return {status:"duplicate"} при совпадении
  ├─ форматируем markdown-запись
  ├─ fcntl.flock + атомарный append к history.md
  ├─ maybe_rotate() если файл > 512 KB → архив в history/YYYY-MM.md
  └─ return {status:"recorded", entry_id, path}

read_history(limit=20, since?, query?)
  ├─ если query:
  │    ├─ HistoryStore.ensure_index()        # lazy: rebuild если mtime файла > mtime store
  │    └─ семантический поиск через NumpyVectorStore + эмбеддер
  └─ иначе:
       └─ HistoryReader.read_recent()        # парсинг снизу вверх, фильтр по since
```

### 3.2 Раскладка модулей

| Новый файл | Роль |
|---|---|
| `src/memory/__init__.py` | Маркер пакета |
| `src/memory/config.py` | Константы: пути, маркеры, пороги; импортирует `REPO_ROOT`/`DATA_DIR` из `src/engine/config.py:7-8` |
| `src/memory/managed_section.py` | Чистый Python-порт маркер-редактора из `scripts/init_repo.sh:636-672`. Функции: `upsert_section`, `read_section`, `remove_section`. Атомарная запись через `tempfile` + `os.replace`. |
| `src/memory/describer.py` | `RepoDescriber`: hash → bundle → prompt → sampling → upsert |
| `src/memory/history.py` | `HistoryWriter` (append, dedup, rotate) + `HistoryReader` (recent + lazy semantic) + `HistoryStore` (обёртка над NumpyVectorStore) |
| `tests/test_managed_section.py` | Тесты маркер-редактора (стиль `tests/test_vector_store.py`) |
| `tests/test_describer.py` | Хэш, refresh-логика, мок sampling, проверка upsert |
| `tests/test_history.py` | Append, dedup, ротация, recent read, семантический поиск (mock embedder) |

| Изменяемый файл | Что меняем |
|---|---|
| `src/server.py` | Добавить `@mcp.tool()` для `describe_repo`, `record_history`, `read_history` |
| `CLAUDE.md` (корень проекта) | Добавить короткую инструкцию в routing protocol: после шагов протокола агент должен вызывать `record_history()` в конце значимого turn'а; на первой сессии в незнакомом репо — сначала `describe_repo()` |
| `.gitignore` | Закомментированные строки opt-out для `history.md` |

### 3.3 Карта переиспользования (НЕ переписывать заново)

| Существующее | Расположение | Где переиспользуем |
|---|---|---|
| FastMCP `@mcp.tool()` декоратор + JSON-string возвраты | `src/server.py:70-608` | Все три новых tool'а — тот же паттерн регистрации |
| MCP sampling через `ctx.session.create_message(...)` | `src/server.py:196` | `describe_repo` использует тот же sampling-вызов |
| Маркер-редактор CLAUDE.md (inline Python) | `scripts/init_repo.sh:636-672` | Портируем буквально в `src/memory/managed_section.py` — bash-инсталлер и MCP-tool используют одну реализацию (bash подцепит через subprocess) |
| `SkillRetriever._compute_dir_hash` + `_needs_reindex` | `src/engine/skills.py:32-51` | `RepoDescriber._compute_repo_hash` + `_needs_refresh` |
| `NumpyVectorStore` (атомарные .npz+.json, thread-safe) | `src/engine/vector_store.py:39` | `HistoryStore` для семантического recall |
| `embed_texts` / `embed_query` (FastEmbed) | `src/engine/embedder.py:83,89` | Векторизация записей и запросов |
| `LanguageDetector` | `src/engine/language.py:39` | Тегирование записей по языку |
| `debug_log` | `src/utils/debug_logger.py:18` | Инструментация всех трёх tool'ов |
| `@observe` из `langfuse_compat` | `src/utils/langfuse_compat.py` | Опциональная observability |
| `REPO_ROOT`, `DATA_DIR` | `src/engine/config.py:7-8` | Базовые пути |
| pytest-фикстуры (`tmp_path`, `populated_store`) | `tests/test_vector_store.py:12-31` | Зеркалим для новых тестов |

---

## 4. Контракты форматов

### 4.1 `CLAUDE.md` (управляемая секция)

Сосуществуют две пары маркеров. Новая пара ставится **ниже** существующей секции routing-protocol, чтобы повторные запуски `init_repo.sh` и повторные `describe_repo` никогда не пересекались.

```
# >>> Agents-Core Routing Protocol (managed by init_repo) >>>
… существующее …
# <<< Agents-Core Routing Protocol (managed by init_repo) <<<

# >>> Agents-Core Repository Memory (managed by describe_repo) >>>

# Repository: agents-core
> Auto-generated by `describe_repo` on 2026-04-15T10:00:00Z. Hash: a1b2c3.
> Re-run with `describe_repo(force_refresh=True)` to update.

## Project Identity
…
## Tech Stack
…
## Entry Points
…
## Module Map
…
## Conventions
…
## Key Workflows
…
## Architecture Patterns
…
## Test Strategy
…
## Gotchas
…
## Glossary
…

# <<< Agents-Core Repository Memory (managed by describe_repo) <<<
```

**Бюджет слов:** 800–1500 (проверяется тестами). Жёсткий cap, чтобы `CLAUDE.md` не раздул контекстное окно.

### 4.2 `history.md` (append-only, корень репо)

Шапка файла (пишется один раз при первом append):
```yaml
---
repo: agents-core
created: 2026-04-15T09:30:00Z
format_version: 1
---
```

Шаблон записи:
```markdown
## 2026-04-15T14:32:00Z | a1b2c3d4e5f6
**Intent:** Включить семантический recall прошлых действий для подгрузки в контекст.
**Action:** Добавил `HistoryStore` поверх `NumpyVectorStore` в `src/memory/history.py`; записи эмбеддятся лениво.
**Outcome:** `pytest tests/test_history.py::test_semantic_search_returns_relevant` проходит; индекс пересобирается, когда mtime history.md растёт.
**Files:** src/memory/history.py, tests/test_history.py
**Tags:** #feature #memory
```

**Правила:**
- `entry_id` (12-hex суффикс в заголовке) = `sha256(intent+action+outcome)[:12]`. Стабильный, дедупится.
- Dedup: скан последних 50 записей по id перед append. Дубли коротко-замыкаются.
- Только append. Прошлые записи никогда не редактируются и не удаляются.
- `fcntl.flock(LOCK_EX)` на запись (защита от параллельных сессий).
- Ротация: при `os.path.getsize > 512 KB` — переносим файл в `history/YYYY-MM.md` (месяц по timestamp последней записи), открываем свежий `history.md` со ссылкой-шапкой на архив.
- UTF-8, `\n` line endings.
- `tags` — свободные `#хэштеги`; `metadata` — плоский JSON, если задан, сериализуется inline как `**Meta:** {...}`.

### 4.3 Сигнатуры MCP-инструментов (добавляются в `src/server.py`)

```python
@mcp.tool()
async def describe_repo(
    ctx: Context,
    repo_path: str | None = None,
    force_refresh: bool = False,
) -> str:
    """One-shot repo bootstrap. Generates a structured summary via MCP sampling
    and writes it into the managed Repository Memory section of CLAUDE.md.

    Returns JSON: {status, path, hash, word_count, summary_preview}.
    status ∈ {"refreshed", "up-to-date", "error"}.
    """

@mcp.tool()
async def record_history(
    intent: str,
    action: str,
    outcome: str,
    files: list[str] | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> str:
    """Append a single intent-centric entry to history.md (append-only, deduped by content hash).

    Returns JSON: {status, entry_id, path}.
    status ∈ {"recorded", "duplicate", "error"}.
    """

@mcp.tool()
async def read_history(
    limit: int = 20,
    since: str | None = None,
    query: str | None = None,
) -> str:
    """Read recent entries (limit/since) or run a lazy semantic search (query). Returns JSON.

    Returns JSON: {entries: [{id, timestamp, intent, action, outcome, files, tags, distance?}], total, mode}.
    mode ∈ {"recency", "semantic"}.
    """
```

Все три возвращают JSON-строки (как существующий паттерн в `src/server.py`).

---

## 5. Промпт describe-режима (центральный артефакт)

`RepoDescriber` строит этот промпт и отправляет через `ctx.session.create_message(...)`. Плейсхолдер `{{CONTEXT_BUNDLE}}` заполняется детерминированно: дерево файлов (depth ≤ 3), `pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod`, README.md (первые 200 строк), заголовки entry-point файлов, sample frontmatter `.mdc`, список тестов, скрипты.

> Текст промпта оставлен на английском намеренно — будущие сессии Claude в любом языковом контексте смогут его выполнить, а структура output совпадает с английскими секциями `CLAUDE.md`.

```
You are a **Repository Analyst** performing a one-time deep study of a codebase.
Your output will be saved into CLAUDE.md as the project memory and read by every
future Claude session, so future sessions can work effectively without re-exploring
the codebase.

## Task
Produce a compressed, LLM-consumable repository overview for `{{REPO_NAME}}`.

## Input (CONTEXT_BUNDLE)
{{CONTEXT_BUNDLE}}

## Output Format
Produce exactly these sections in this order. Use compressed markdown
(BLUF, atomic paragraphs, no filler).

### Project Identity
One paragraph: name, purpose, primary language, framework, package manager.

### Tech Stack
Bulleted list of key dependencies with version and purpose. Max 15 items.

### Entry Points
Table: path | purpose. Include: main, tests, config, CI/CD, scripts.

### Module Map
Depth-2 directory listing. One line per directory: `path/ — purpose`.
Skip generated/vendor dirs.

### Conventions
Bulleted list: naming, imports, error handling, logging, frontmatter format,
code style.

### Key Workflows
For each script / Make target / npm script: `name — what it does`. Max 10.

### Architecture Patterns
2–3 paragraphs: data flow, key abstractions, dependency injection style,
async patterns.

### Test Strategy
Runner, fixture patterns, mock strategy, coverage config. One paragraph.

### Gotchas
Bulleted list of non-obvious things: env vars needed, startup order,
known limitations, common mistakes.

### Glossary
Table: term | definition. Domain-specific terms only. Max 15.

## Rules
- BLUF: lead every section with the most important fact.
- Compress: no filler ("this project is", "in order to", "it is worth noting").
- Atomic: one paragraph = one idea.
- Concrete: cite file paths and line numbers, not vague references.
- Skip empty sections rather than writing "N/A".
- Total output: 800–1500 words. Exceeding wastes context; under-shooting loses info.
- Output ONLY the markdown sections above. No preamble, no closing remarks.
```

---

## 6. Пошаговая реализация

### Phase 1 — Foundation (1 PR)

1. Создать `src/memory/__init__.py` (пустой).
2. Создать `src/memory/config.py` с константами: `MEMORY_DATA_DIR`, `HISTORY_FILE`, `DESCRIBE_HASH_FILE`, `DESCRIBE_MARKER_BEGIN/END`, `HISTORY_VECTOR_STORE_NAME`, `HISTORY_ROTATION_THRESHOLD_KB = 512`. Все пути привязаны к `REPO_ROOT`/`DATA_DIR` из `src/engine/config.py`.
3. Создать `src/memory/managed_section.py` с `upsert_section`, `read_section`, `remove_section`. Портировать inline Python из `scripts/init_repo.sh:636-672` строка-в-строку, добавить атомарную запись через `tempfile.NamedTemporaryFile` + `os.replace`. Валидировать уникальность маркеров; кидать ошибку при частичных маркерах.
4. Написать `tests/test_managed_section.py`: create, replace, append, partial-marker rejection, сохранение контента вне маркеров, атомарная запись при сбое.

### Phase 2 — Describe (1 PR)

5. Создать `src/memory/describer.py`:
   - `RepoDescriber.__init__(repo_path)`.
   - `_compute_repo_hash()` — MD5 от: отсортированных top-level имён файлов, содержимого `pyproject.toml`, содержимого `package.json`, имён директорий depth-1 + depth-2, первых 200 строк README.md.
   - `_needs_refresh(force) → (bool, hash)` — паттерн из `src/engine/skills.py:43-51`.
   - `_build_context_bundle() → str` — рендерит блок `{{CONTEXT_BUNDLE}}`: дерево (`Path.rglob` с фильтрами, depth ≤ 3, исключая `node_modules`, `.venv`, `__pycache__`, `.git`, `data/`), содержимое ключевых файлов, sample frontmatter `.mdc`.
   - `_render_prompt(bundle, repo_name) → str` — подставляет плейсхолдеры в шаблон describe-промпта (хранится как multiline-константа в модуле).
   - `async describe(ctx, force=False) → dict` — оркестратор: если refresh не нужен — вернуть кэш summary, прочитанный через `managed_section.read_section`; иначе построить prompt, вызвать `ctx.session.create_message(...)` (sampling), `managed_section.upsert_section(CLAUDE.md, …, generated)`, сохранить хэш, вернуть status.
6. Добавить tool `describe_repo` в `src/server.py`. Обернуть в `@observe`, если Langfuse подгружен. Вернуть JSON.
7. Написать `tests/test_describer.py`: детерминированный хэш, refresh-on-change, refresh-skipped-when-unchanged, моканый `ctx.session.create_message` с заранее заготовленным summary, проверка upsert, ассерт word-count (800–1500).

### Phase 3 — History (1 PR)

8. Создать `src/memory/history.py`:
   - `HistoryWriter`: `__init__(history_path)`, `_compute_entry_hash`, `_is_duplicate` (tail-scan последних 50), `append_entry` (format → `flock` → атомарный append → `maybe_rotate`), `maybe_rotate`.
   - `HistoryReader`: `read_recent(limit, since)` — парсинг снизу вверх через regex `## {ts} | {id}`.
   - `HistoryStore`: lazy-обёртка над `NumpyVectorStore`. `ensure_index()` сравнивает mtime store и файла; если устарел — переэмбеддит все записи (или инкрементально: только записи, чьего id нет в store). `search(query, limit)` эмбеддит запрос, возвращает топ-N с distance.
9. Добавить `record_history` и `read_history` в `src/server.py`. Оба возвращают JSON.
10. Написать `tests/test_history.py`: append создаёт файл с шапкой, format roundtrip, dedup, порядок recent read, фильтр `since`, ротация триггерит + создаёт архив, lazy-семантический индекс пересобирается при изменении файла (mock embedder).

### Phase 4 — Wiring & Documentation (1 PR)

11. Обновить `CLAUDE.md` (корень проекта) — короткая post-flight инструкция внутри секции routing protocol: *"After completing a meaningful turn, call `record_history(intent, action, outcome, files)`. On first session in an unfamiliar repo, call `describe_repo()` first."* Распространится в `~/.claude/CLAUDE.md` при следующем запуске `init_repo.sh`.
12. Обновить `.gitignore` закомментированным opt-out:
    ```
    # Uncomment if you prefer to keep action history private:
    # history.md
    # history/
    ```
13. Обновить корневой `README.md` — короткий раздел "Repository memory: `describe_repo` / `record_history`" со ссылкой на эту спецификацию.

### Phase 5 — Опциональные follow-ups (отдельные PR, не в текущем scope)

14. **Hooks:** добавить шаблон `.claude/settings.json` с `PostToolUse`-хуком, который напоминает Claude вызвать `record_history`. Документировать в спеке, по умолчанию не включать.
15. **Memory consolidation:** ночная задача, которая сжимает записи старше N дней в месячные summary.
16. **Интеграция с `init_repo.sh`:** опциональный флаг `--describe`, который запускает `describe_repo` при первой установке.

---

## 7. План тестирования

### 7.1 Юнит-тесты (pytest)

| Тест | Что проверяет |
|---|---|
| `test_managed_section_create` | Создание файла с маркерами и контентом |
| `test_managed_section_replace` | Замена существующей секции, контент вне маркеров не тронут |
| `test_managed_section_append` | Append при отсутствии маркеров |
| `test_managed_section_partial_markers` | Ошибка при частичных маркерах (только begin или только end) |
| `test_describer_hash_deterministic` | Один и тот же репо → один и тот же хэш |
| `test_describer_hash_changes_on_file_add` | Новый top-level файл → хэш меняется |
| `test_describer_skips_when_unchanged` | Второй вызов без force → `status:"up-to-date"` |
| `test_describer_force_refresh_overwrites` | `force_refresh=True` → перегенерация даже при том же хэше |
| `test_describer_word_count_in_range` | Сгенерированный summary укладывается в 800–1500 слов |
| `test_history_append_creates_file` | Первый вызов создаёт `history.md` с frontmatter |
| `test_history_format_roundtrip` | Append → read → все поля совпадают |
| `test_history_dedup_by_hash` | Повторный вызов с тем же intent/action/outcome → `status:"duplicate"` |
| `test_history_read_recent_order` | Возвращает записи в обратном хронологическом порядке |
| `test_history_read_since_filter` | Фильтрация по timestamp работает |
| `test_history_rotation_triggers` | Файл > 512 KB → перенос в `history/YYYY-MM.md` |
| `test_history_semantic_lazy_index` | Векторный store не существует до первого `read_history(query=...)` |
| `test_history_semantic_returns_relevant` | Mock-эмбеддер: запрос возвращает наиболее релевантную запись |

### 7.2 Live MCP-проверки (вручную)

1. Поднять сервер: `bash scripts/run_tests.sh && python -m src.server`.
2. В Claude Code сессии:
   - `describe_repo()` → подтвердить, что `CLAUDE.md` обновился; routing-секция не тронута.
   - Повторно `describe_repo()` → `status:"up-to-date"`.
   - `describe_repo(force_refresh=True)` → перегенерация.
   - `record_history(intent="Verify integration", action="Manual test", outcome="passed")` → запись в `history.md`.
   - Повторно тот же `record_history` → `status:"duplicate"`.
   - `read_history(limit=5)` → запись видна.
   - `read_history(query="verify integration")` → семантический матч.
3. Регрессии: `pytest tests/test_routing.py` — `route_and_load` корректно роутит существующие тестовые запросы.

### 7.3 Метрики качества

| Метрика | Цель |
|---|---|
| Word count describe-summary | 800–1500 |
| Размер `CLAUDE.md` после describe | < 15 KB |
| Латентность `record_history` | < 20 ms (без векторизации) |
| Латентность `read_history(query=...)` (cold) | < 500 ms (включая первичную индексацию ≤ 100 записей) |
| Точность семантического поиска | top-1 совпадает с ожидаемым в ≥ 90% тестов |

---

## 8. Открытые решения (не блокируют v1)

- **Категории в `record_history`?** Принимать ли enum `category` (decision/refactor/fix/feature) для фильтрованного чтения? **Рекомендация:** пропустить в v1; tags покрывают это с нулевой жёсткостью схемы.
- **Git-метаданные в describe?** Включать ли текущую ветку и последний коммит? **Рекомендация:** да — append блока `## Git State` в конце управляемой секции, перегенеряется при каждом refresh.
- **Phase 2 hooks:** opt-in PostToolUse для авто-подсказки `record_history`. **Не в scope v1;** контракт хука зафиксировать в этой спеке (раздел Phase 5).

---

## Приложение A. Затрагиваемые файлы (быстрая навигация)

**Создаём:**
- `src/memory/__init__.py`
- `src/memory/config.py` — константы
- `src/memory/managed_section.py` — маркер-редактор (порт из `scripts/init_repo.sh:636-672`)
- `src/memory/describer.py` — `RepoDescriber` + шаблон describe-промпта
- `src/memory/history.py` — `HistoryWriter`, `HistoryReader`, `HistoryStore`
- `tests/test_managed_section.py`
- `tests/test_describer.py`
- `tests/test_history.py`

**Изменяем:**
- `src/server.py` — три новых `@mcp.tool()`
- `CLAUDE.md` — добавить post-flight инструкцию (один абзац) внутрь routing protocol
- `.gitignore` — закомментированный opt-out для `history.md`
- `README.md` — один абзац про подсистему памяти со ссылкой на эту спеку

## Приложение C. Implementation Notes (что реально вошло в код)

Реализация на 2026-04-15 совпадает со спекой. Уточнения, появившиеся при кодинге:

1. **Модуль `src/memory/config.py`** добавляет константы, не упомянутые в плане явно: `CLAUDE_MD_FILE`, `HISTORY_ARCHIVE_DIR`, `DESCRIBE_TREE_MAX_DEPTH`, `DESCRIBE_README_HEAD_LINES`, `DESCRIBE_EXCLUDED_DIRS`, `DESCRIBE_WORD_MIN/MAX`. Все — производные от значений, упомянутых в спеке (512 KB, 800–1500 слов, depth ≤ 3).

2. **Hash CLAUDE.md/history.md исключается из repo-hash** (`_HASH_EXCLUDED_FILES` в `describer.py`). Без этого `describe_repo` инвалидировал бы собственный кэш на каждом запуске — побочный эффект записи в CLAUDE.md меняет top-level filenames.

3. **`RepoDescriber` разбит на `plan() / build_prompt() / write_summary()`**, а вызов `ctx.session.create_message(...)` остался в `src/server.py`. Это позволяет писать unit-тесты без MCP-контекста — тесты не используют sampling.

4. **`HistoryWriter`/`HistoryReader` — pure stdlib**, без numpy. `HistoryStore` (семантический recall) импортирует `NumpyVectorStore` и эмбеддер только при первом `search()`. Это критично для NixOS-окружения: writer/reader работают даже когда numpy не может загрузиться (тесты semantic-store при этом помечаются `skipif`).

5. **`HistoryStore.search` принимает `embed_query` / `embed_texts` как аргументы** (DI). По умолчанию подгружаются `src.engine.embedder.*`; в тестах подсовывается `FakeEmbedder` с детерминированными векторами, не требующий загрузки реальной модели.

6. **fcntl-обёртка** `_lock_exclusive` / `_unlock` в `history.py` — no-op на Windows, чтобы модуль импортировался кросс-платформенно.

7. **Tag-нормализация:** хэштеги без префикса `#` дополняются им автоматически (`"feature"` → `"#feature"`).

8. **Rotation merge:** если `history/YYYY-MM.md` уже существует на момент ротации (несколько ротаций в один месяц), новый снапшот аппендится с разделителем `<!-- merged on rotation -->`, а не перезаписывает старый архив.

9. **CLI-инструкция в `CLAUDE.md`** добавлена как пункт `4. Repository memory (first session per repo)` — отдельный раздел внутри Routing Flow, чтобы не путаться с post-flight шагом 3.

10. **README.md** содержит раздел `🧠 Repository Memory` со ссылкой на эту спеку и упоминанием `.gitignore` opt-out.

---

## Приложение B. Источники (research)

- `https://github.com/raoulbia-ai/claude-recall` — outcome-aware memory, hooks, JIT-инъекция
- `https://github.com/mkreyman/mcp-memory-keeper` — explicit tool surface, checkpoints, channels
- `https://github.com/doobidoo/mcp-memory-service` — autonomous consolidation, knowledge graph
- `https://www.mintlify.com/blog/how-claudes-memory-and-mcp-work` — split native CLAUDE.md vs MCP
