# Архитектура Agents-Core — простыми словами

Этот документ — пояснение для людей, а не справочник для машины. Если хочешь точный API
и пороги/таймауты — смотри `CLAUDE.md` и `docs/routing_flow.md`. Здесь объясняется,
**что есть** в системе, **зачем оно**, и **как одно подключается к другому**.

---

## TL;DR одной картинкой

```
                          Запрос пользователя
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │      МАРШРУТИЗАТОР     │
                    │   (выбор агента)       │
                    └─────────┬──────────────┘
                              │
                              ▼
                          ┌───────┐
                          │ Агент │  ← база: его «личность» и стиль
                          └───┬───┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   ┌─────────┐          ┌──────────┐         ┌──────────┐
   │ Правила │          │  Навыки  │         │ Импланты │
   │ (Rules) │          │ (Skills) │         │(Implants)│
   │  для    │          │  ЧТО     │         │   КАК    │
   │  ВСЕХ   │          │ знать    │         │ думать   │
   └─────────┘          └────┬─────┘         └──────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌─────────┐   ┌────────────┐   ┌───────────┐
        │  core   │   │ preferred  │   │  capable  │
        │ (всегда)│   │  (boost в  │   │  (пул для │
        │         │   │  semantic) │   │ semantic) │
        └─────────┘   └────────────┘   └───────────┘
```

**Пять сущностей**: агенты, навыки (с трёхуровневой моделью), импланты, правила,
уровни сложности (tiers). Четыре из них собираются в финальный системный prompt
при каждом запросе. Маршрутизатор решает, чьим именем подписывать ответ.

---

## 1. Агенты (Agents)

**Что это.** Специалисты с фиксированной ролью и стилем. Юрист, программист, фитнес-тренер.
Один агент — одно лицо, один тон, одна область.

**Где живут.** `agents/<name>/system_prompt.mdc`. Имена snake_case: `lawyer`,
`software_engineer`, `bio_hacker`.

**Из чего состоит файл агента.**
```yaml
---
identity:
  name: software_engineer
  display_name: Software Engineer
  role: "Senior Full Stack Engineer — Code Implementation, Debugging & Refactoring"
  tone: Professional, Structured, Technical
routing:
  domain_keywords:
    - "write code"
    - "refactoring"
    - "debug"
  trigger_command: /dev
  aliases: []                   # опционально: дополнительные slash-команды,
                                # регистрируются как отдельные MCP prompts
core_skills:
  - skill-dev-clean-code
preferred_skills:
  - skill-dev-api-design
  - skill-error-recovery
  - skill-dev-debugging
  - skill-dev-security
capable_skills:
  - skill-mcp-development
preferred_implants:
  - implant-regression-first
  - implant-iteration-budget
---

# Тут идёт основной текст: персона, протокол работы, примеры…
```

**Виды агентов.**

| Тип | Пример | Когда выбирается |
|---|---|---|
| **Доменный** | `lawyer`, `medical_expert` | Когда запрос явно про конкретную область. |
| **Универсальный** | `universal_agent` | Когда запрос короткий/общий или агент не определён. |
| **Мета-фреймворковый** | `agent_builder`, `mcp_builder`, `install_to_repo` | Когда сам Agents-Core редактируется. |

Сейчас агентов **43**. Список формируется автоматически по содержимому каталога `agents/`.

### Slash-aliases (`routing.aliases`)

Один агент может слушать несколько slash-команд. Например, `/lawyer` —
канонический trigger, а `/co_lawyer`, `/cy_lawyer`, …, `/us_lawyer` —
исторические алиасы, унаследованные от девяти удалённых country-specific
лавьеров. Все они зарегистрированы как отдельные MCP slash prompts через
`src/server.py::_register_agent_prompts()`, которая итерирует
`[trigger_command] + aliases` и регистрирует prompt для каждой команды.

Дополнительно: алиас (например, `/co_lawyer`) обычно дублируется как keyword
в `domain_keywords` агента и как keyword в соответствующем
`skill-jurisdiction-co.mdc`. Это даёт три параллельных пути активации
правильного контекста — MCP slash prompt, keyword routing, skill retrieval.

---

## 2. Навыки (Skills)

**Что это.** Маленький модуль знания на одну тему. «Как писать чистый код», «Как
рассуждать аналитически», «Как форматировать ответ через BLUF». Один навык — один файл.

**Где живут.** `skills/skill-<topic>.mdc`. Например, `skill-dev-clean-code.mdc`,
`skill-analysis-critical.mdc`.

**Из чего состоит файл навыка.**
```yaml
---
description: "Принципы чистого кода. SOLID, DRY, KISS. Role: Principal Engineer."
compiled: "SOLID. DRY. KISS. Boy Scout Rule. Fail fast. No dead code."
---

## Role
Principal Engineer: prioritise long-term maintainability.

## Principles
- SOLID, DRY, KISS, YAGNI, Boy Scout Rule.
...
```

- `description` — краткое описание для семантического поиска.
- `compiled` — однострочная сжатая версия для экономии токенов на `standard` tier.
- Тело — полный текст навыка (используется на `deep` tier).

### Три способа, как навык попадает в prompt

Каждый агент сам решает в трёх категориях. Skills, не указанные ни в одной — недоступны этому агенту.

#### 2.1 Core skills — обязательные для агента

**Где:** поле `core_skills` во frontmatter агента.

**Идея:** skills, без которых агент не функционирует. Например, `skill-mathematical-reasoning`
для math_scientist или `skill-legal-citation` для юристов. Всегда грузятся целиком,
безусловно — даже на `lite` tier.

**Размер:** 0–3 записи на агента.

**Пример (lawyer):**
```yaml
core_skills:
  - skill-legal-citation
```

#### 2.2 Preferred skills — приоритетные для семантического поиска

**Где:** поле `preferred_skills` во frontmatter агента.

**Идея:** skills, которые агент использует часто. При семантическом поиске получают
**boost** к расстоянию (× 0.7) — выходят первыми среди равных. Грузятся когда запрос
семантически близок к ним.

**Размер:** 3–6 записей на агента (повседневный инструментарий).

**Пример (lawyer):**
```yaml
preferred_skills:
  - skill-decision-frameworks
  - skill-analysis-critical
  - skill-consultative-intake
  - skill-dense-summarization
```

#### 2.3 Capable skills — доступный пул

**Где:** поле `capable_skills` во frontmatter агента.

**Идея:** skills, которые **могут пригодиться** для конкретного подзапроса. Участвуют
в семантическом поиске с **базовым весом** (без boost). Если у skill есть keyword
который совпадает с словом из запроса — получают дополнительный bonus (× 0.85).

Пример: у `security_expert` есть `skill-defectdojo-integration` в capable. Если
пользователь спросит про DefectDojo — embedding запроса близок к skill, keywords
матчатся — skill подтянется. Если спрашивает просто про SQL injection — capable skill
не нужен, не грузится.

**Размер:** 5–15 записей на агента (широкий fallback-пул).

**Принцип**: лучше «перебрать» с capable, чем недобрать — router всё равно
фильтрует по семантике + keyword match. Если скилл может пригодиться в 1 из
20 запросов агента, он должен лежать в capable.

**Пример (lawyer):**
```yaml
capable_skills:
  - skill-jurisdiction-co
  - skill-jurisdiction-cy
  - skill-jurisdiction-ge
  - skill-jurisdiction-kz
  - skill-jurisdiction-mx
  - skill-jurisdiction-ru
  - skill-jurisdiction-rs
  - skill-jurisdiction-es
  - skill-jurisdiction-us
  - skill-fact-verification
  - skill-temporal-validation
  - skill-source-trust-tiers
  - skill-reasoning-logic
```

Девять `skill-jurisdiction-XX` — это страновые «выжимки» (кодексы,
аббревиатуры, ставки, аппарат). Каждый имеет в `keywords:` свой
slash-alias (`/co_lawyer`, `/cy_lawyer`, …) и страновые термины
(`DIAN`, `ИП Грузия`, `amparo`, `AIFC`, и т. п.), поэтому
матчатся как при семантическом совпадении, так и при keyword-hit.

#### Что значит "не в списке"

Skill, не указанный ни в `core_skills`, ни в `preferred_skills`, ни в `capable_skills` —
**этому агенту недоступен**. Никогда не грузится для него. Это явная контракт-семантика:
агент декларирует свой набор навыков, всё прочее — за его границей.

### Свойство `alwaysApply`

В frontmatter навыка можно указать `alwaysApply: true`. Это **не** автозагрузка
навыка в каждый prompt — это разрешение на `@import` (другие файлы могут
встроить содержимое этого навыка через `@skills/skill-X.mdc`). На практике мало
кто использует.

### Чего НЕ существует (legacy)

- ❌ `static_skills` — поле для fallback-режима, движок никогда его не читал. Удалено.
- ❌ `context: { file_globs: [...] }` — блок из формата Cursor MDC. Маршрутизация
  идёт через `domain_keywords` + семантический поиск, типы файлов не используются. Удалено.
- ❌ `globs` в навыках — поле из формата Cursor MDC. В движке Agents-Core не используется.
- ❌ `core_skills.yaml` (глобальный файл) — был механизмом auto-injection. Удалён —
  агенты декларируют core_skills сами.
- ❌ `agents/capabilities/registry.yaml` (35 bundles + directives) — capability как
  именованный bundle skills удалена. Skills теперь декларируются прямо в трёх per-agent
  списках. Directives мигрированы: дубли удалены, уникальные — в body соответствующих
  агентов, переиспользуемые — стали новыми skills (например `skill-legal-citation`,
  `skill-consultative-intake`).
- ❌ Прямое поле `skills` — устарело, заменено на 3-tier модель.

---

## 3. Способности (Capabilities) — УДАЛЕНЫ

Capability как именованный bundle skills + directive **больше не существует**.
Skills теперь декларируются прямо в trois-tier модели (раздел 2).

**Что произошло с 35 capabilities и их directives:**

- **Skills** из bundles распределены по `core_skills`/`preferred_skills`/`capable_skills`
  агентов, которые эти capabilities подключали.
- **Directives** мигрированы тремя путями:
  - **(A) дубль skill body** (60% directives) — просто удалены. Их содержание уже
    было внутри связанных skills, дублирование убрано.
  - **(B) уникальный single-use** (12 directives) — перенесены в **body соответствующего
    агента** как отдельная секция (например, "Review Discipline" у `code_reviewer`,
    "Search Strategy" у `3d_print_finder`).
  - **(C) переиспользуемые** (8 directives) — превращены в **новые skills**:
    - `skill-consultative-intake` (Phase 1/2/3 workflow)
    - `skill-legal-citation` (cite statute verbatim, jurisdiction)
    - `skill-source-trust-tiers` (peer-reviewed > expert > media)
    - `skill-forensic-process` (timeline + dedup + triangulation)
    - `skill-bio-protocol-design` (mechanism + dosage + safety stop)
    - `skill-epistemic-method` (semantic voids + cui bono)
    - `skill-prompt-design-process` (R+T+C + eval loop)
    - `skill-creative-craft` (show don't tell + POV + subtext)

**Почему убрали.** Capability смешивала две сущности (bundle + directive), а
большинство directives оказывались либо дублями skill body, либо meta-инструкциями
которые логичнее жить в body соответствующего агента.

---

## 4. Импланты (Implants)

**Что это.** Когнитивные стратегии — *как* думать, а не *что* знать. Например:

- **Chain-of-Verification** — сначала набросай ответ, потом задай к нему
  проверочные вопросы, исправь.
- **Step-Back** — прежде чем решать конкретную задачу, сделай шаг назад и
  спроси про общий принцип.
- **Pre-Mortem** — представь, что план уже провалился; почему он провалился?

Навык говорит «знай X», имплант говорит «думай способом Y».

**Где живут.** `implants/implant-<name>.mdc`. Сейчас в системе около 57 имплантов.

**Как подключаются.**

1. **Семантически** — движок ищет 2–3 наиболее релевантных к запросу импланта
   и подсовывает агенту.
2. **Через `preferred_implants`** — агент в frontmatter может указать список
   предпочитаемых имплантов. Они будут выбраны с приоритетом, если подходят
   по релевантности.
3. **По запросу** — агент в процессе диалога может вызвать `load_implants(query=...)`,
   чтобы догрузить дополнительные импланты по конкретному вопросу.

**Пример объявления у агента.**
```yaml
preferred_implants:
  - implant-regression-first
  - implant-iteration-budget
```

**Когда подключаются.** На уровнях `standard` (2 импланта) и `deep` (3+ импланта).
На `lite` импланты не загружаются.

---

## 5. Правила (Rules)

**Что это.** Универсальные, всегда-включённые директивы для **всех** агентов без
исключения. Это не знания (это в навыках), не стратегии рассуждения (это в
имплантах) — это **поведенческие инварианты**.

**Текущий набор (5 правил).**

| Правило | Приоритет | Суть |
|---|---|---|
| `no-fabrication` | 10 | Не выдумывай факты, API, ссылки, числа. Не уверен — скажи «не уверен». |
| `honest-uncertainty` | 20 | Помечай каждое утверждение по уровню уверенности (Verified / Reasoned / Unknown). |
| `anti-sycophancy` | 30 | Не соглашайся ради согласия. Видишь ошибку — скажи + объясни почему. |
| `content-structure` | 40 | BLUF / Minto / MECE. Скимабельность, атомарные параграфы. (Промотировано из `skill-content-structure` — раньше дублировалось в 12 агентах.) |
| `language-match` | 90 | Отвечай на языке последнего сообщения пользователя. |

**Архитектурный инвариант.** Правила **не имеют opt-in / opt-out** на уровне агента.
В файле правила запрещены поля `applies_to` и `exclude_agents` — если их добавить,
движок откажется загружать такое правило и напишет в лог:

> Per-agent guidance belongs in `skills/`, not in `rules/`.

**Где живут.** `rules/rule-<name>.mdc`. 5 файлов.

**Рендеринг.** `src/engine/rules.format_rules_for_prompt()` оборачивает
каждое правило в `### Rule: <name>` и удаляет ведущий H1 в теле, если он
есть. Поэтому подзаголовки в теле правила нужно делать **не выше**
`####`, иначе они визуально окажутся над wrapper'ом и сломают
иерархию (см. `rule-content-structure.mdc` как образец).

**Управление.** `RULES_ENABLED=0` в `.env` — полностью выключает слой (для
диагностики/сравнения).

**Чем правила отличаются от core skills.**

| Свойство | Rules | Core skills |
|---|---|---|
| Где живут | `rules/*.mdc` | поле `core_skills` во frontmatter агента |
| Применяются на любом tier | Да | Зависит от tier-политики |
| Семантический поиск | Нет (всегда все) | Нет (всегда все из списка) |
| Видимы клиенту в footer | Да (`Rules:`) | Сейчас идут в общий `Skills:` |
| Назначение | Поведенческие инварианты | Универсальные знания (форматирование, сжатие) |

---

## 6. Уровни сложности (Tiers)

Движок не загружает всё подряд — он определяет, насколько сложен запрос, и
по этому выбирает «глубину» обогащения.

**Три уровня.**

| Tier | Когда срабатывает | Что загружается |
|---|---|---|
| **`lite`** | Очень короткий простой запрос (< 50 символов, без сложных слов) | Базовый prompt агента + правила + **`core_skills` агента** (грузятся безусловно). Семантический пул `preferred`/`capable` и импланты — НЕ грузятся. |
| **`standard`** | По умолчанию для большинства запросов | Базовый prompt + правила + навыки (core + top-2 из preferred ∪ capable) + 2 импланта. Навыки рендерятся в сжатом виде (`compiled`). |
| **`deep`** | Длинный (> 300 символов) или сложный (есть «сложные» сигнальные слова в запросе) | Базовый prompt + правила + навыки (core + top-4 из preferred ∪ capable) в **полном** виде + 3+ имплантов. |

**Как определяется.** Функция `infer_tier(query)` в `src/engine/enrichment.py`.
Использует регулярку `_COMPLEX_SIGNALS` и порог длины. Текущие сигнальные
маркеры (подсказывающие `deep`):

- code-fence маркер (тройной backtick)
- `архитектур`, `рефактор`, `оптимиз`, `анализ`, `исследу`, `сравни`, `план`, `ревью`
- `debug`, `investigate`, `compare`, `design`, `review`, `audit`, `deep dive`, `/deep`

**Promotion lite → standard.** Если запрос автоматически попал в `lite`, но
у агента в frontmatter объявлены `preferred_implants` — tier поднимается до
`standard`, чтобы импланты загрузились (на `lite` семантический пайплайн
имплантов выключен). Объявленные `preferred_skills` / `capable_skills` сами по
себе promotion **не** вызывают; их семантический пул просто пропускается на `lite`.
Promotion работает только при автодетекции — явно переданный клиентом `tier`
сохраняется.

**Принудительно задать tier.** Клиент может передать `tier="lite"` / `"standard"` / `"deep"`
в API явно — тогда автодетекция и promotion пропускаются.

---

## 7. Маршрутизация (как запрос находит агента)

Полная диаграмма — в `docs/routing_flow.md`. Здесь — главные ступени по-простому.

```
Запрос
  │
  ▼
1. Sticky-агент?  ─── есть сильное соответствие текущему агенту → продолжаем с ним
  │
  ▼ (нет)
2. Семантический кэш  ─── похожий запрос уже был? агент закэширован?
  │
  ▼ (нет)
3. Meta-запрос?  ─── «привет», «hi», короткие → universal_agent (lite tier)
  │
  ▼ (нет)
4. ROUTE_REQUIRED  ─── вернуть LLM список всех агентов; пусть выберет
                      LLM выбирает → get_agent_context(agent_name) → обогащение
```

**Sticky-агент.** Если в предыдущем ходе уже был выбран агент и новый запрос
очень похож (близкое расстояние косинуса), мы остаёмся на нём. Это даёт
стабильность в многоходовых диалогах.

**Семантический кэш.** Все прошлые маршрутные решения хранятся как векторы.
Когда приходит похожий запрос — выдаём прошлый ответ. Порог — `distance < 0.05`.

**Keyword veto.** Даже при совпадении кэша есть проверка по ключевым словам.
Если в запросе явно «налоговый кодекс РФ», но кэш предлагает `software_engineer`
— keywords могут переопределить.

**Meta-query.** Очень короткие сообщения («привет», «спасибо», «help»)
направляются на `universal_agent` без полноценного маршрута.

**ROUTE_REQUIRED.** Когда кэш не помог и meta не сработало, движок возвращает
клиенту список всех 43 агентов с их `display_name` и `role`. **Внешний LLM
выбирает** наиболее подходящего по описанию и зовёт `get_agent_context(agent_name)`.

**Что важно понять:** навыки/импланты/правила **не участвуют в выборе агента**.
Они влияют только на то, *чем нагрузят* выбранного агента — но не на сам выбор.

---

## 8. Финальный prompt — порядок сборки слоёв

Когда агент выбран, движок собирает финальный системный prompt в таком порядке:

```
┌───────────────────────────────────────────────┐
│ 1. Базовый prompt агента                       │  ← персона, протокол, примеры
│    (тело файла agents/<name>/system_prompt.mdc)│
├───────────────────────────────────────────────┤
│ 2. Правила (Rules) — всегда                    │  ← 5 универсальных директив
├───────────────────────────────────────────────┤
│ 3. Навыки (Skills) — 3-tier                    │  ← core (всегда) + top-N из preferred ∪ capable
├───────────────────────────────────────────────┤
│ 4. Импланты (Implants) — standard и deep        │  ← когнитивные стратегии
└───────────────────────────────────────────────┘
```

**Эффект последовательности.**

- Базовый prompt задаёт *кто говорит*.
- Правила добавляют поведенческие ограничения.
- Навыки добавляют знания по теме (core гарантированно + семантически релевантные).
- Импланты подсказывают *как рассуждать* над задачей.

Каждый слой может пустовать в зависимости от tier и наполнения агента, но
**порядок** всегда тот же. Сбой одного слоя (например, импланты не загрузились)
не валит весь pipeline — он «деградирует» к меньшему числу слоёв.

**Конвейер skills внутри слоя 3:**

```
1. mandatory_skills = agent.core_skills    →  загружаем ВСЕ (даже на lite)
2. pool = agent.preferred_skills ∪ agent.capable_skills
3. для каждого s из pool:
     d = embedding_distance(query, skill)
     если s в preferred:    d *= 0.7     (boost)
     если keyword(s) ∈ query: d *= 0.85  (keyword bonus)
     если d < threshold (0.75): кандидат для top-N
4. top-N по адаптированному distance (N зависит от tier: lite=0, standard=2, deep=4)
5. final_skills = mandatory + top-N (с дедупом)
```

---

## 9. Краткий глоссарий

| Термин | Одной строкой |
|---|---|
| **Агент** | Специалист с фиксированной ролью и стилем (юрист, инженер). |
| **Навык (skill)** | Модуль знания на одну тему. |
| **Core skill** | Обязательный навык агента — всегда грузится. |
| **Preferred skill** | Навык агента с boost в семантическом поиске. |
| **Capable skill** | Навык в семантическом пуле с базовым весом — грузится если запрос подходит. |
| **Keywords (у skill)** | Ключевые слова для дополнительного boost при capable-routing. |
| **Имплант (implant)** | Когнитивная стратегия — *как* думать. |
| **Правило (rule)** | Универсальная всегда-включённая директива, без opt-out. |
| **Tier** | Уровень обогащения prompt (lite / standard / deep). |
| **Frontmatter** | YAML-блок между `---` в начале файла. |
| **Маршрутизация** | Процесс выбора подходящего агента под запрос. |
| **Sticky-агент** | Привязка к текущему агенту в многоходовом диалоге. |
| **ROUTE_REQUIRED** | Запрос не нашёл агента автоматически — LLM выбирает из списка. |
| **Enrichment** | Сборка финального prompt из слоёв. |

---

## 10. Кейс: как унифицировался `lawyer`

До PR #52 в репозитории было девять country-specific лавьеров —
`colombian_lawyer`, `cypriot_lawyer`, `georgian_lawyer`, `kazakh_lawyer`,
`mexican_lawyer`, `russian_lawyer`, `serbian_lawyer`, `spanish_lawyer`,
`us_lawyer`. Все они декларировали **идентичный** 3-tier skill-набор
(один core, пять preferred, пустой capable) и отличались только страновым
блоком в body (~110 строк на каждого). Total: ~990 дублирующих строк.

**Что сделано:**

1. **Скилы**. Страновые знания вынесены в девять `skill-jurisdiction-{co,cy,ge,kz,mx,ru,rs,es,us}.mdc`:
   primary sources, key institutions, tax system, corporate forms,
   immigration/residence, country-specific mechanisms (`amparo`, AIFC,
   non-dom, Ley Beckham, Virtual Zone и т. п.), common pitfalls.

2. **Агент**. Один `lawyer` с:
   - `core_skills: [skill-legal-citation]` (всегда нужен для citation discipline);
   - `preferred_skills`: четыре thinking-tools (decision-frameworks, analysis-critical, consultative-intake, dense-summarization);
   - `capable_skills`: девять jurisdiction skills + verification (fact-verification, temporal-validation, source-trust-tiers, reasoning-logic).

3. **Маршрутизация**. Старые `/co_lawyer`, …, `/us_lawyer` сохранены тремя путями:
   - В `routing.aliases` агента — регистрируются как реальные MCP slash prompts.
   - В `routing.domain_keywords` агента — для keyword-based agent routing.
   - В `keywords:` соответствующего `skill-jurisdiction-XX.mdc` — для keyword-boost при skill retrieval.

4. **Cap-honesty**. `SkillRetriever.retrieve()` использует `n_results=2`
   для семантического пула, поэтому при запросе «сравни IT-режимы Кипр vs
   Грузия vs Сербия» загружаются обычно top-1–2 наиболее релевантных
   jurisdiction skills, а не все девять. Body агента это явно признаёт и
   рекомендует sequential queries для thorough multi-country comparison.

Эта схема — рабочий шаблон для будущих агентских семейств: общий
поведенческий протокол в body + per-variant знания в capable skills с
правильно выставленными keywords.

---

## 11. Куда копать дальше

- **Точные пороги и числа** → `CLAUDE.md` (раздел «Key Thresholds»).
- **Диаграмма маршрутизации** → `docs/routing_flow.md`.
- **Список навыков** → `skills/README.md` (+ сами файлы `skills/*.mdc`).
- **Список имплантов** → `implants/README.md` (+ файлы `implants/*.mdc`).
- **Создать нового агента** → команда `/new_agent` (агент `agent_builder`).
