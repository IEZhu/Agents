# Ревью плана «Один общий процесс Agents-Core для MCP-клиентов»

Дата: 2026-09-20. Рецензируемый документ: `docs/shared-mcp-daemon-plan.md` (версия от 2026-09-20). Статус: критика и предложения; runtime и конфигурации клиентов не изменены.

## TL;DR

Диагностика кода в плане точна: все девять строк таблицы «Что подтверждено в коде» сверены с исходниками и совпадают. Цель верна и теперь подтверждена измерениями (раздел 1).

Главная проблема плана: он проектирует полноценный stateful multi-session сервер (TTL и лимиты сессий, привязка контекста к сессии, roots и их смена, operation token против гонок, отмена server→client запросов), хотя ни один из целевых клиентов не документирует поддержку sampling, а workspace можно передать детерминированно HTTP-заголовком. Вторая проблема: шесть последовательных этапов не дают пользователю ни одного освобождённого гигабайта до самого конца.

Три главных предложения:

1. **Stateless HTTP плюс workspace в заголовке** вместо stateful сессий и roots. Убирает большую часть этапа 3 и заметную часть этапа 2.
2. **Переупорядочить поставку вертикальными срезами**: v1 = daemon для routing/persona плюс memory tools по явному workspace, миграция Codex и Claude Code (12 из 15 процессов). Bridge, токен, автообновление и Claude Desktop во v2.
3. **Заменить оценку памяти измерением**: baseline снят ниже. Метрика `ps rss` непригодна, нужен physical footprint.

## 1. Измеренный baseline (в плане отсутствует)

Снято на этой машине 2026-09-20 при подготовке ревью: `ps -axo pid,ppid,rss,etime,command`, `vmmap --summary <pid>`, `sysctl vm.swapusage`.

| Клиент | Процессов `src/server.py` | Physical footprint на процесс |
|---|---|---|
| Codex (ChatGPT.app, один родительский процесс, по серверу на тред) | 5 | 1.5–1.6 GB |
| Claude Code desktop app (`claude-code/2.1.266`) | 4 | 1.5 GB |
| Claude Code CLI (`claude`, 2.1.278) | 3 | 1.5–1.6 GB |
| Claude Desktop (через обёртку `disclaimer --pgroup`) | 2 | 1.5 GB |
| Cursor (`Cursor Helper: mcp-process`) | 1 | 1.5 GB |
| **Итого** | **15** | **≈ 22.9 GB** |

Контекст хоста: 36 GB RAM; swap занят на 99 % (25.7 из 26.0 GiB). Возраст процессов от 2 минут до 4.5 суток. Это не «1, 3 и 5 клиентов» из критериев приёмки, а 15 одновременных долгоживущих сессий, часть из которых давно не используется, но не завершена клиентом.

Что это меняет для плана:

- `ps rss` показывает для спящих процессов 9–83 MB, потому что macOS сжал их страницы. Physical footprint тех же процессов 1.5–1.6 GB. Этап 1 должен измерять именно footprint (`vmmap --summary`, `footprint`), иначе выигрыш будет недооценён примерно в 20 раз.
- Формула `N × (runtime + model + indexes)` верна, и N сегодня равно 15. Ожидаемая экономия около 21 GB, а не абстрактное «снижение суммарной памяти».
- Память процесса определяется моделью: `.env` задаёт `EMBEDDING_MODEL=intfloat/multilingual-e5-large`, кеш fastembed занимает 2.3 GB на диске. ONNX Runtime держит веса в приватной памяти процесса, страницы между процессами не разделяются.
- Критерий приёмки №1 нужно проверять на 15–20 сессиях, а не на 5.

Побочные симптомы текущей архитектуры, которые план не приводит как мотивацию, хотя они её усиливают:

- `data/.last_update.json` сегодня в 20:28 зафиксировал `REINDEX_FAILED` (переход `7edeb3c` → `04dcf9c`). Staging reindex поднимает второй процесс с моделью при почти полном swap. Связь вероятна, но причина сбоя по логам не проверялась.
- 15 процессов держат `data/.sessions.lock` в режиме `LOCK_SH` до выхода. Активация обновления требует `LOCK_EX` без читателей и при таком числе долгоживущих клиентов практически недостижима.
- `data/router_cache.npz` пишут все 15 процессов независимо (`SemanticRouter._mutate_and_save`: trim, save, marker). Атомарная замена файла защищает целостность, но записи других процессов теряются по принципу «последний победил». Daemon как единственный писатель закрывает и это.

## 2. Что в плане сделано хорошо

- Ссылки на код точны: `get_client_repo_root` действительно `lru_cache(maxsize=1)`; `src/memory/config.py` резолвит `HISTORY_FILE` и `CLAUDE_MD_FILE` через него лениво в `__getattr__`; `_history_store` глобален (`src/server.py:53`); warmup вызывается в `__main__`; `.sessions.lock` намеренно shared; `os.execv` в updater (`src/self_update.py:1379`); reindex через subprocess (`_run_reindex_at`).
- Верное решение оставить состояние персоны v2 на клиенте и не вводить глобальный `active_agent`.
- Отдельный singleton lock службы вместо переиспользования `.sessions.lock`.
- Maintenance-протокол для установщика с учётом того, что LaunchAgent перезапустит процесс посреди изменения.
- Явный запрет на массовый kill Python-процессов и на автоматический fallback в тяжёлый stdio при недоступности daemon.
- Предупреждение о разнице документации SDK v1/v2 подтверждено: установлен `mcp 1.27.1`, в `uv.lock` зафиксирован 1.27.0.
- Замечание об именах debug-логов подтверждено: формат `HH-MM-SS.fff_{tool}_{dir}.json`, при параллельных запросах коллизии реальны.

## 3. Критика по убыванию влияния

### К1. Stateful сессии и roots создают сложность, которую можно не иметь

План опирается на stateful Streamable HTTP: TTL и лимиты сессий, привязка `ClientContext` к сессии, пересоздание контекста при `roots/list_changed`, operation token против гонки между `describe_repo` и `write_repo_summary`, таймауты и отмена `sampling/createMessage` и `roots/list`.

Проверенные факты:

- Claude Code (документация): sampling не упоминается; roots поддерживаются с v2.1.203 вместе с `notifications/roots/list_changed`. Установлены 2.1.278 (CLI) и 2.1.266 (desktop).
- Cursor (документация): roots поддерживаются, sampling не упоминается.
- Codex (документация): ни roots, ни sampling не упоминаются. При этом Codex даёт 5 из 15 процессов.
- Установленный SDK: `FastMCP(stateless_http=True, json_response=True)` доступны; `RequestContext.request` содержит Starlette `Request`, поэтому HTTP-заголовки читаются внутри tool.
- `FastMCP.streamable_http_app()` создаёт `StreamableHTTPSessionManager` без параметра `session_idle_timeout`, хотя сам менеджер его принимает. Публичного способа задать TTL сессий через FastMCP в 1.27.1 нет: понадобится приватный `_session_manager` или низкоуровневый `Server`. Это ровно то место, где план говорит «выбрать публично поддерживаемый SDK API», и такого API нет.

Предложение: v1 в режиме `stateless_http=True`, `json_response=True`. Workspace передаётся заголовком `X-Agents-Workspace` (canonical path либо ID из локального реестра). Каждый запрос самодостаточен.

Что исчезает:

- TTL, лимиты и cleanup сессий, session-bound контекст, вопрос «сессия не тождественна диалогу».
- Гонка при смене roots между `describe_repo` и `write_repo_summary`: оба вызова несут заголовок; смена заголовка между вызовами это явный выбор клиента, а не гонка. Существующая проверка `repo_hash` в `RepoDescriber.write_summary` (ветвь `rejected`, `src/memory/describer.py:368`) уже отклоняет устаревшее продолжение; проверить, что она покрывает несовпадение хеша, дешевле, чем вводить operation token.
- Отмена server→client запросов и связанное с ней request state.
- Сессионная семантика `clear_session_cache`: кеши `SESSION_CACHE` и `CONTEXT_HASH_CACHE` ключуются хешами содержимого, а не сессией, поэтому безопасны как общие immutable кеши; очистка становится административной операцией без «области сессии».

Что теряется: sampling (`SUCCESS_SAMPLED`) и server→client уведомления по HTTP. Ни один из трёх целевых клиентов их не документирует. Claude Desktop, который sampling поддерживает, по HTTP к loopback всё равно не подключится (К3). Stateful режим остаётся опцией v3, если появится клиент, которому он нужен.

Как каждый клиент передаёт заголовок (по документации):

| Клиент | Механизм | Где хранить |
|---|---|---|
| Claude Code | `headers` в JSON с `${VAR}` expansion; `headersHelper` для динамических значений | local scope (per-project, вне репозитория) или user scope в `~/.claude.json` |
| Cursor | `headers` с интерполяцией `${workspaceFolder}`: путь проекта без roots | `.cursor/mcp.json` в проекте или `~/.cursor/mcp.json` |
| Codex | `http_headers` (static map) или `http_headers_helper` | `.codex/config.toml` (trusted project) или `~/.codex/config.toml` |

Открытый вопрос: схема ID. Хеш пути машинно-специфичен, идентичность репозитория (remote URL) не различает два клона на одной машине. Реестр «ID → путь» решает, но политику коллизий нужно описать явно (ошибка конфигурации, а не выбор первого).

### К2. Порядок этапов не даёт ранней ценности

«Контекст проектов → HTTP/runtime → служба → обновления → миграция клиентов» означает, что пользователь получает первый освобождённый гигабайт после завершения всех шести этапов. Самый тяжёлый и рискованный этап (2, рефакторинг контекста) стоит первым и целиком объявлен предпосылкой.

Предложение (детали в разделе 5): вертикальный срез. Daemon сначала обслуживает `route_and_load`, `get_agent_context`, `list_agents`, `load_implants`, `refresh_persona_context`: они зависят только от общих индексов и модели. Memory tools (`log_interaction`, `read_history`, `describe_repo`, `write_repo_summary`) требуют заголовок workspace и без него возвращают ошибку конфигурации. Инструкция сервера уже содержит «Skip unavailable logging», то есть клиент деградирует штатно. Мигрировать сначала Codex и Claude Code: 12 из 15 процессов.

### К3. Матрица клиентов неполна

План: «Codex, Claude Code, Cursor; перечень не подтверждён». Измерено: плюс Claude Desktop (2 процесса) и два разных входа Claude Code (desktop app и CLI; общая конфигурация, разные особенности).

Claude Desktop: `claude_desktop_config.json` принимает только stdio (`command`/`args`), поле `url` не поддерживается. Custom connectors настраиваются через аккаунт; по справке Anthropic и сторонним источникам соединение исходит из инфраструктуры Anthropic, а не с машины пользователя, поэтому `127.0.0.1` недостижим. Request headers в connectors находятся в бете для ограниченного набора организаций, а заголовок `Authorization` зарезервирован под OAuth. Вывод: для Claude Desktop единственный путь к общему runtime это stdio→HTTP bridge. План относит bridge к «только при подтверждённой необходимости», а необходимость уже измерена: 3 GB footprint. Варианты: готовый `mcp-remote` (npm; поддержка `--header` по памяти, не проверена), собственный тонкий bridge без импорта движка, либо явный non-goal «Claude Desktop остаётся на stdio» в критериях приёмки.

Claude Code desktop app: по документации Code tab при одинаковом имени stdio-сервера в `~/.claude.json` и в `.mcp.json` использует определение из `~/.claude.json`. Миграция обязана обновить оба места, иначе desktop app продолжит запускать stdio-копию. Там же: JSON-запись с `url` без `"type": "http"` пропускается с ошибкой. Сейчас Agents-Core зарегистрирован и в user scope `~/.claude.json`, и в project `.mcp.json`.

### К4. Токен: доставка в GUI-клиенты не спроектирована

План: bearer token из пользовательского файла с ограниченными правами, не в URL, логах и git. Не сказано, как каждый клиент его получит. Ловушка: Cursor, ChatGPT.app (Codex), Claude Desktop и Claude Code desktop app запускаются из Dock и не видят переменные окружения из `.zshrc`; `${AGENTS_CORE_TOKEN}` в их конфигах развернётся в пустую строку или останется как есть.

Модель угроз для честности. Сервер слушает loopback. SDK при `host="127.0.0.1"` автоматически включает DNS-rebinding protection (`allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]` и соответствующие `allowed_origins`), то есть браузерный вектор закрыт из коробки, и план не должен реализовывать проверку Host/Origin повторно. Токен защищает от локальных процессов того же пользователя, которые и так могут читать `history.md`, `~/.claude.json` и сам файл токена. Ценность токена умеренная, цена в миграции ощутимая.

| Вариант | Плюсы | Минусы |
|---|---|---|
| Без токена в v1: loopback плюс встроенный Host/Origin | Ноль отказов миграции из-за auth | Любой локальный процесс пользователя может вызвать tools с записью в файлы репозиториев |
| Литеральный токен в user-scope конфигах клиентов (0600), установщик ротирует | Работает для Dock-приложений; тот же уровень защиты, что файл токена | Токен лежит в 3–4 файлах; нельзя класть в project `.mcp.json`, он в git |
| Env var (`bearer_token_env_var`, `${env:...}`) | Единственный источник | Не работает для GUI без `launchctl setenv`; у Claude Code часть имён (ANTHROPIC_*, NPM_TOKEN и др.) читается как пустые |
| Helper-команды (`headersHelper`, `http_headers_helper`) читают файл токена | Единственный источник истины | Нет у Cursor; таймаут 10 с у Claude Code; ещё один процесс на подключение |

Рекомендация: второй вариант; токен включается флагом конфигурации службы, чтобы отладка и откат не зависели от auth. Встроенный auth-слой SDK (`AuthSettings` требует `issuer_url` и рассчитан на OAuth) для loopback избыточен: достаточно Starlette middleware на пару десятков строк.

### К5. Обновления: staging reindex это второй процесс с моделью

План честно называет пик памяти от `_run_reindex_at` и предлагает «при необходимости reindex после остановки сервиса». Сегодняшний `REINDEX_FAILED` показывает, что необходимость уже наступила при текущем давлении на память. Хранилища малы (`router_cache.npz` 1.4 MB, `skills_store.npz` 288 KB, `implants_store.npz` 232 KB): их пересборка это секунды embedding, а не вторая модель.

| Стратегия | Простой | Пик памяти | Сложность | Что теряем |
|---|---|---|---|---|
| Текущая: фон готовит worktree и reindex в subprocess, активация при следующем старте | Рестарт плюс warmup | +1.5 GB во время prepare | Высокая (`tests/test_self_update.py` 75 KB) | Ничего, но prepare падает под давлением памяти |
| Упрощённая: `update` = drain → stop → `git fetch/checkout` под exclusive lease → start; индексы пересобираются при старте по существующим `.skills_hash`/`.implants_hash` | Рестарт плюс warmup плюс reindex | Нет второго процесса | Низкая | Near-zero-downtime активацию и часть протестированной машинерии |
| Гибрид: prepare готовит только код (worktree без reindex), индексы строятся при старте новой версии до статуса ready | Как упрощённая | Нет второго процесса | Средняя | Быструю активацию заранее построенных индексов |

Для локального однопользовательского daemon минута недоступности по явной команде приемлема. Рекомендация: гибрид или упрощённая стратегия с сохранением recovery journal и fail-closed поведения. Решение нужно оформить как ADR, потому что оно выбрасывает работающий и протестированный код.

### К6. Окружение launchd не описано

- PATH у LaunchAgent минимальный. Установка использует nix (`/run/current-system/sw/bin/npx` в `~/.claude.json`), поэтому `git` для describer и updater может не найтись. Фиксировать абсолютный путь `git` или задать `EnvironmentVariables.PATH` в plist.
- `StandardOutPath`/`StandardErrorPath` не ротируются. Сегодня логи уходят в stderr клиента; у daemon они будут расти неограниченно. Нужен `RotatingFileHandler` или правило `newsyslog`.
- `KeepAlive`, `ThrottleInterval` (по умолчанию 10 с), `ProcessType`: значение `Background` может понизить приоритет CPU и I/O и замедлить embedding; проверить на baseline.
- Сеть для `git fetch` под launchd: credential helper и `SSH_AUTH_SOCK` ведут себя иначе, чем в терминале. Проверить smoke-тестом.
- Самоперезапуск через `os.execv` можно заменить выходом процесса и перезапуском средствами launchd: меньше собственной логики, окно двойного warmup контролирует launchd, а не наш код.

### К7. Один event loop на все клиенты

Сегодня блокирующий вызов задерживает одного клиента. В daemon он задержит всех. Embedding план выносит в bounded executor, но есть и другое: `fcntl.flock(LOCK_EX)` в `HistoryWriter` ждёт синхронно, git-команды describer, `langfuse.flush` в `atexit`. Предложение: интеграционный тест с asyncio debug-режимом и `loop.slow_callback_duration`, падающий при блокировке цикла дольше порога. Про контекст: `loop.run_in_executor` не копирует `ContextVar`, `asyncio.to_thread` копирует; явная передача путей, как предлагает план, всё равно надёжнее обеих.

### К8. Критерии приёмки без чисел

План откладывает пороги до этапа 1. Baseline теперь есть, предлагаемые цели:

| Метрика | Сейчас | Цель v1 |
|---|---|---|
| Процессов с загруженной моделью при 15 сессиях | 15 | 1 (плюс bridge-процессы без модели) |
| Суммарный physical footprint серверных процессов | ≈ 22.9 GB | ≤ 2.5 GB |
| Время до первого ответа tool у нового клиента | cold start с warmup e5-large | ≤ 1 с при готовом daemon |
| Idle CPU daemon | не измерялось | около 0 %, без polling |
| Restart → ready (warmup плюс reindex) | не измерялось | измерить; верхняя граница в конфиге |
| Потеря записей `router_cache` при параллельных клиентах | есть | нет |

Добавить сценарии: сон и пробуждение Mac (HTTP-соединения рвутся, клиент переподключается); старт клиента при отсутствии daemon (понятная ошибка, без fallback в stdio); 20 параллельных сессий; закрытие клиента с незавершённым `log_interaction`.

### К9. Гигиена документа

В плане нет разделов Non-goals, реестра рисков (вероятность × ущерб), открытых вопросов, ADR с отклонёнными альтернативами и хотя бы грубых оценок объёма (S/M/L по этапам). Диаграмма не показывает stdio-режим отката и bridge.

Пре-мортем. Если план провалится, наиболее вероятные причины: (1) этап 2 растянется и заблокирует всё остальное; (2) миграция конфигов сломает одного из пяти клиентов, и пользователь откатит всё целиком; (3) daemon зависнет на блокирующем вызове и сломает все клиенты разом, чего stdio-модель не делала. Первые две снимаются К1 и К2, третья К7.

### К10. Мелочи

- `.mcp.json` в этом репозитории отслеживается git. После миграции URL с портом станет машинно-специфичным. Использовать фиксированный порт по умолчанию и запись вида `${AGENTS_CORE_URL:-http://127.0.0.1:<port>/mcp}`.
- `scripts/_helpers/inject_mcp.py` перезаписывает запись целиком и знает только `command`/`args`; понадобится ветка для `type`/`url`/`headers` и отдельный путь для TOML Codex. `scripts/init_repo.sh` сегодня детектирует Cursor и Claude Desktop, но не Codex.
- Health endpoint назвать явно: `GET /health` с JSON состояния (state, pid, version, install root, uptime, requests in flight). Его используют установщик, команда `status` и smoke test.
- Модель как рычаг вне архитектуры: квантованный или меньший embedding сократил бы каждый процесс в разы без daemon, ценой качества маршрутизации, которое измерялось многосидовыми evals. Стоит записать как отклонённую альтернативу в ADR, а не умалчивать.
- Stdio-режим отката при работающем daemon пишет тот же `router_cache.npz`. Либо fallback открывает кеш только на чтение, либо документировать, что в режиме отката daemon должен быть остановлен.

## 4. Таблица ключевых решений

| Решение | Вариант A | Вариант B | Рекомендация |
|---|---|---|---|
| Транспорт | Stateful Streamable HTTP (sampling, roots, notifications) | Stateless, JSON responses, workspace в заголовке | B для v1; A при появлении клиента с sampling |
| Жизненный цикл | LaunchAgent, старт при логине, живёт постоянно | Spawn-on-demand первым клиентом, idle-exit | A: детерминированный владелец, нет гонки спавна; B остаётся идеей для Linux/CI |
| Workspace | MCP roots плюс реестр плюс operation token | Заголовок из per-project конфига клиента плюс реестр | B; roots как необязательное дополнение позже |
| Auth | Обязательный bearer из файла | Loopback плюс встроенный Host/Origin; токен опционален, хранится в user-scope конфигах | B |
| Обновление | Staging worktree плюс reindex в subprocess | Stop → update → start; reindex при старте до ready | B или гибрид, оформить как ADR |
| Claude Desktop | Отдельная проверка позже | `mcp-remote` bridge или явный non-goal | Решить в плане сейчас: 3 GB на столе |

## 5. Предлагаемая последовательность поставки

v1, цель: убрать 12 из 15 процессов.

1. Скрипт baseline: footprint всех серверных процессов, swap, время первого ответа; зафиксировать числа в docs.
2. Общий bootstrap для stdio и daemon; `FastMCP(host="127.0.0.1", port=<fixed>, stateless_http=True, json_response=True)`; warmup один раз на процесс; `GET /health`; singleton lock службы отдельно от `.sessions.lock`.
3. `ClientContext` из заголовка `X-Agents-Workspace`; явная передача путей в `HistoryReader`, `HistoryWriter`, `RepoDescriber`, debug logger; реестр `HistoryStore` по canonical root с LRU-границей; без заголовка memory tools возвращают ошибку конфигурации, routing работает.
4. LaunchAgent plist, CLI `install/start/stop/status/restart`, ротация логов, абсолютные пути и PATH.
5. Установщик: миграция Codex (`.codex/config.toml`, `url` и `http_headers`), Claude Code (`~/.claude.json` user или local scope и `"type": "http"` в `.mcp.json` с env-default), Cursor (`headers` с `${workspaceFolder}`). Backup, атомарная запись, откат на сохранённую stdio-конфигурацию.
6. Приёмка по таблице К8; stdio остаётся явным режимом отката и разработки.

v2.

7. Токен по выбранной схеме доставки; команда `update` по выбранной стратегии; периодическая проверка с существующим throttle.
8. Claude Desktop через bridge или зафиксированный non-goal.
9. Тест блокировок event loop, лимит параллельных embedding-задач, correlation id в логах.

v3.

10. Stateful режим при реальной потребности (sampling, roots, notifications); Linux user service; Windows.

## 6. Открытые вопросы к автору плана

1. Нужен ли sampling хоть одному целевому клиенту по HTTP? Если нет, stateful режим не нужен в v1.
2. Схема workspace ID: путь, хеш пути или идентичность репозитория? Политика при двух клонах одного репозитория.
3. Сохраняется ли требование near-zero-downtime обновления, или минута простоя по явной команде приемлема?
4. Claude Desktop: bridge, non-goal или отложенное решение с явной пометкой «3 GB остаются»?
5. Кто владелец каждого этапа и какой объём (S/M/L)?

## 7. Что проверено и что нет

Проверено в этом ревью: код по всем ссылкам плана; версии SDK; сигнатуры `FastMCP`, `StreamableHTTPSessionManager`, `TransportSecuritySettings`, `AuthSettings`, `RequestContext`; документация Claude Code, Cursor и Codex по HTTP-транспорту, заголовкам, подстановке переменных и roots; `data/.last_update.json`; процессы и footprint на этой машине.

Не проверено и помечено как воспоминание: поддержка `--header` у `mcp-remote`; источник соединения Claude Desktop custom connectors взят из справки Anthropic и сторонних статей, не воспроизведён; причина сегодняшнего `REINDEX_FAILED`; поведение Codex и Cursor со stateless-серверами без `Mcp-Session-Id` (по спецификации допустимо, на практике не прогонялось).

## Источники

- [Claude Code: MCP](https://code.claude.com/docs/en/mcp)
- [Cursor: MCP](https://cursor.com/docs/mcp)
- [Codex: MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Claude Help Center: custom connectors using remote MCP](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [Claude docs: third party connectors with remote MCP](https://claude.com/docs/connectors/custom/remote-mcp)
- [anthropics/claude-ai-mcp#112: Bearer header for custom remote MCP](https://github.com/anthropics/claude-ai-mcp/issues/112)
- [Fix: HTTP MCP Server URL won't connect in Claude Desktop](https://startdebugging.net/2026/05/fix-http-mcp-server-url-wont-connect-in-claude-desktop/)
- [MCP transports specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
