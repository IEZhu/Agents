Прямо: **точное число я дать не могу, и никто не может без прогона на ваших данных.** Моя оценка: **~3–6 часов**, а закладывать в окно я бы стал **8 часов**. Это оценка, не гарантия. Если на планёрке озвучить «4 часа» как точную цифру, это будет обещание, которое вы не контролируете.

Но главное другое: **сам план лучше поменять.** Ту же задачу можно сделать с даунтаймом в секунды или минуты, а не в часы. Подробности ниже.

## Почему одного числа нет

`ALTER COLUMN id TYPE bigint` делает не «смену типа», а полную перезапись таблицы:

- **Rewrite 180 GB heap** под `ACCESS EXCLUSIVE`, то есть блокируются и запись, и **чтение**.
- **Пересборка всех 4 индексов (~65 GB)** после перезаписи, по одному. Длительность сильно зависит от `maintenance_work_mem` и `max_parallel_maintenance_workers`.
- **WAL ≈ объём таблицы + индексов (сотни GB).** Если есть streaming-реплики или архивирование WAL, узким местом может стать не диск, а сеть или архив. Реплики будут отставать.
- **Свободное место:** на время операции нужна вторая копия таблицы и индексов (~250 GB) плюс WAL. Не хватит места, и через N часов операция упадёт с полным откатом.
- **Скрытые множители:** FK из других таблиц на `events.id` (их колонки тоже нужно переводить, а FK перепроверяются), триггеры, `CHECK`, представления.
- **Sequence:** если `id` объявлен как `serial`, у sequence стоит `AS integer`, и её нужно отдельно перевести `ALTER SEQUENCE ... AS bigint`. Для identity-колонки тип sequence, насколько я помню, меняется вместе с колонкой (recalled, не проверено, сверьтесь с документацией PG16).
- **Откат:** если прервать операцию на 5-м часу, откат будет быстрым, но всё окно потеряно, а проблема останется.

Разброс «3 или 6 часов» задаётся именно этими факторами, и по описанию железа их не вычислить.

## Как получить реальную цифру

Единственный честный способ: **репетиция на копии**. Восстановите бэкап или snapshot на похожее железо (или используйте отключённую реплику) и выполните ровно ту же команду:

```sql
-- на копии, НЕ на проде
\timing on
SET maintenance_work_mem = '8GB';                 -- как планируете в проде
SET max_parallel_maintenance_workers = 8;
ALTER TABLE events ALTER COLUMN id TYPE bigint;
```

Измеренное время ×1.5 можно озвучивать менеджменту. Если к завтрашнему дню не успеваете, скажите на планёрке: «оценка 3–6 ч, точная цифра после репетиции такого-то числа». Это честнее любого числа, взятого с потолка.

## Что я бы сделал вместо этого: онлайн-миграция

Даунтайм почти нулевой, работа идёт в фоне:

```sql
-- 1. Новая колонка (мгновенно, без rewrite)
ALTER TABLE events ADD COLUMN id_new bigint;

-- 2. Триггер: новые/изменённые строки сразу пишут id_new
CREATE FUNCTION events_sync_id() RETURNS trigger AS $$
BEGIN NEW.id_new := NEW.id; RETURN NEW; END $$ LANGUAGE plpgsql;
CREATE TRIGGER events_sync_id BEFORE INSERT OR UPDATE ON events
  FOR EACH ROW EXECUTE FUNCTION events_sync_id();

-- 3. Backfill батчами (по 10–50k строк, с паузами; следить за лагом реплик и autovacuum)
UPDATE events SET id_new = id WHERE id BETWEEN :from AND :to AND id_new IS NULL;

-- 4. Уникальный индекс без блокировки записи
CREATE UNIQUE INDEX CONCURRENTLY events_id_new_uidx ON events (id_new);

-- 5. NOT NULL без полного скана под эксклюзивной блокировкой
ALTER TABLE events ADD CONSTRAINT id_new_nn CHECK (id_new IS NOT NULL) NOT VALID;
ALTER TABLE events VALIDATE CONSTRAINT id_new_nn;   -- SHARE UPDATE EXCLUSIVE, запись идёт

-- 6. Переключение: одна короткая транзакция (секунды)
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE events ALTER COLUMN id_new SET NOT NULL;  -- использует валидный CHECK, без скана
ALTER TABLE events DROP CONSTRAINT events_pkey;
ALTER TABLE events ADD CONSTRAINT events_pkey PRIMARY KEY USING INDEX events_id_new_uidx;
ALTER SEQUENCE events_id_seq AS bigint OWNED BY events.id_new;
ALTER TABLE events ALTER COLUMN id_new SET DEFAULT nextval('events_id_seq');
ALTER TABLE events ALTER COLUMN id DROP DEFAULT;
ALTER TABLE events RENAME COLUMN id TO id_old;
ALTER TABLE events RENAME COLUMN id_new TO id;
DROP TRIGGER events_sync_id ON events;
ALTER TABLE events DROP CONSTRAINT id_new_nn;
COMMIT;

-- 7. Позже, отдельно: DROP COLUMN id_old (мгновенно; место освободится при последующих rewrite/VACUUM FULL/pg_repack)
```

Это шаблон: имена sequence и constraint'ов, FK из других таблиц и идентичность колонки нужно сверить с вашей схемой. FK, ссылающиеся на `events.id`, придётся мигрировать по той же схеме и пересоздать через `NOT VALID` + `VALIDATE`.

**Компромиссы:**
- Backfill 410 млн строк создаёт мёртвые версии строк. Таблица распухнет, пока autovacuum не догонит, WAL тоже будет сотни GB, но растянутые во времени.
- Работы больше: несколько дней инженерного времени против одной команды.
- Взамен вы получаете даунтайм в секунды и возможность остановиться на любом шаге.

## Перед любым вариантом

- **Свежий бэкап** (`pg_basebackup` или snapshot) и проверенный restore.
- **Прогон на staging.**
- Проверьте, сколько запаса реально осталось, от этого зависит, есть ли время на онлайн-вариант:

```sql
SELECT max(id), 2147483647 - max(id) AS left FROM events;
-- и темп: сколько id выдаётся в сутки
SELECT count(*) FROM events WHERE created_at > now() - interval '1 day';  -- если есть такая колонка
```

Если запаса меньше недели, делайте offline в окно, но с репетицией и окном 8+ часов. Если запас есть, делайте онлайн-миграцию.
