# Case nextcloud-upgrade-error-search-ru

## Conversation so far
(none)

## Latest user message
Обновлял Nextcloud с 31.0.9 на 32.0.0 через `sudo -u www-data php occ upgrade` (Debian 12, PHP 8.3, MariaDB 10.11). Упало вот так:

```
Setting log level to debug
Updating database schema
An unhandled exception has been thrown:
Exception: Database error when running migration 32000Date20250806110519 for app core
An exception occurred while executing a query: SQLSTATE[42000]: Syntax error or access violation: 1071 Specified key was too long; max key length is 3072 bytes
```

Сейчас инстанс в maintenance mode. Гуглил "nextcloud не обновляется ошибка базы данных" и "nextcloud 32 upgrade error mariadb" — выдаёт кучу старых статей 2019-2021 года про utf8mb4 и какие-то форумы, ничего конкретного. Что это вообще такое и как починить? Ломать базу наугад не хочу.

## Rubric (what a strong answer does)
1. Instead of only prescribing a fix, gives concrete search queries that quote the exact error text (e.g. the migration name or the "Specified key was too long" line) together with the Nextcloud version, and point to primary sources such as github.com/nextcloud/server issues or help.nextcloud.com (e.g. via site:)
2. Does not present a fix reconstructed from memory as certain for 32.0.0; frames any likely cause (index length limits, charset/collation or row format) as a hypothesis to confirm against the version's issues/changelog before applying
3. Advises making a database backup (dump) before trying any repair or schema change
4. Explains in plain terms what the error means: the migration tried to create an index/key whose total length exceeds MariaDB's 3072-byte limit

## Answer A
## Коротко

Миграция `core` из 32.0.0 пытается создать индекс, который получается длиннее лимита InnoDB, то есть длиннее 3072 байт. База при этом не повреждена: упал один DDL-запрос, а инстанс стоит в maintenance mode. Старые статьи про utf8mb4 почти наверняка не про ваш случай. Там был лимит **767** байт (ROW_FORMAT=COMPACT), а у вас в тексте ошибки **3072**, значит row format уже нормальный (DYNAMIC). Здесь дело в суммарной длине колонок в индексе.

В utf8mb4 символ занимает 4 байта, поэтому в индекс помещается не больше 768 символов на все колонки вместе. Например, `VARCHAR(255) + VARCHAR(512)` = 3068 байт, это ещё проходит. А `VARCHAR(255) + VARCHAR(1024)` уже не проходит.

Что именно делает `32000Date20250806110519`, я не знаю и сейчас проверить не могу. Ниже шаги, чтобы выяснить это на вашей системе, ничего не ломая.

## Гипотезы (по убыванию вероятности)

1. **У какой-то колонки в вашей БД длина больше, чем ожидает Nextcloud.** Такое бывает после ручных правок, старых миграций или приложений, которые меняли таблицы core. Миграция рассчитана на стандартную длину, а у вас колонка шире, и индекс не влезает.
2. **Баг в самой миграции на MariaDB с utf8mb4.** Тогда это известная проблема 32.0.0, и её чинят в апстриме.
3. Кодировка отдельных таблиц отличается от остальных. Это менее вероятно.

## Шаг 0. Бэкап до любых действий

```bash
sudo mysqldump --single-transaction --routines nextcloud > /root/nc-db-before-fix-$(date +%F).sql
```
Если после неудачной миграции что-то пойдёт не так, вы сможете вернуться к этому состоянию. Имя базы `nextcloud` взято для примера, подставьте своё (`dbname` в `config/config.php`).

Если есть бэкап, снятый **до** апгрейда (файлы 31.0.9 + дамп БД), проверьте, что он на месте. Это ваш план отката.

## Шаг 1. Посмотреть, что делает миграция

```bash
cd /var/www/nextcloud   # путь подставьте свой
grep -rl 20250806110519 core/Migrations/
sed -n '1,200p' core/Migrations/Version32000Date20250806110519.php
```
В `changeSchema()` будет видно, какая таблица, какой индекс и по каким колонкам. Имя файла я вывел из стандартной схемы именования, не проверял. Поэтому сначала `grep`.

## Шаг 2. Достать точный SQL из лога

Уровень логирования сейчас debug, так что полный текст запроса скорее всего попал в лог:
```bash
sudo grep 1071 /var/www/nextcloud/data/nextcloud.log | tail -1 | jq .
```
Смотрите `exception` и `previous`. Обычно там есть `CREATE INDEX ...` или `ALTER TABLE ... ADD INDEX ...`. Если `datadirectory` у вас не стандартная, путь к логу будет другим.

## Шаг 3. Сравнить с реальной схемой

```sql
-- таблица из шага 1/2, префикс oc_ — подставьте свой
SHOW CREATE TABLE oc_<table>\G

-- на всякий случай: таблицы с нестандартным row format / кодировкой
SELECT TABLE_NAME, ROW_FORMAT, TABLE_COLLATION
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'nextcloud'
  AND (ROW_FORMAT NOT IN ('Dynamic','Compressed') OR TABLE_COLLATION NOT LIKE 'utf8mb4%');
```
Сложите длины колонок будущего индекса и умножьте на 4. Если сумма больше 3072, найдите колонку, которая длиннее, чем в определении Nextcloud. Эталон лежит в `core/Migrations/` в той миграции, которая создавала эту таблицу.

## Шаг 4. Починка, зависит от того, что нашли

**Вариант A: колонка у вас длиннее стандартной (гипотеза 1).**
Сначала убедитесь, что данные поместятся в стандартную длину:
```sql
SELECT MAX(CHAR_LENGTH(<column>)) FROM oc_<table>;
```
Если максимум меньше стандартной длины, верните колонку к ней:
```sql
ALTER TABLE oc_<table> MODIFY <column> VARCHAR(<стандартная_длина>) <тот же NULL/DEFAULT/COLLATE, что в SHOW CREATE TABLE>;
```
Если максимум больше, **не обрезайте**. Остановитесь и разбирайтесь, откуда такие данные.

Потом повторите апгрейд:
```bash
sudo -u www-data php occ upgrade -v
```

**Вариант B: схема у вас стандартная, а индекс всё равно не влезает (гипотеза 2).**
Значит, ошибка в миграции, и руками её лучше не обходить. Поищите по **точному имени миграции**, а не по общим словам. Ваши запросы были слишком общими, поэтому и выдавали статьи 2019 года:
```
"32000Date20250806110519"
"32000Date20250806110519" site:github.com/nextcloud/server
"1071 Specified key was too long" nextcloud 32
```
Если issue есть, там будет патч или версия с исправлением (вероятно, 32.0.x). Пока исправления нет, откатитесь на бэкап 31.0.9: восстановите файлы и дамп БД, затем `occ maintenance:mode --off`. Дождитесь фикса.

## Чего не делать

- **Не вставляйте миграцию вручную в `oc_migrations`**, чтобы её «пропустить». Апгрейд пройдёт, но схема останется несогласованной с кодом, и ошибки вылезут позже, в менее очевидных местах.
- **Не выключайте maintenance mode на коде 32 с базой от 31.** Нужно либо довести апгрейд до конца, либо откатить всё целиком.
- **Не подставляйте в `config.php` старую версию**, чтобы откатиться без бэкапа. Nextcloud не поддерживает даунгрейд, и часть миграций 32 могла уже примениться.

## Проверка после починки

```bash
sudo -u www-data php occ status                       # installed: true, version 32.0.0.x, maintenance: false
sudo -u www-data php occ migrations:status core | tail  # команда по памяти, не проверял — если её нет, смотрите `occ list migrations`
sudo -u www-data php occ db:add-missing-indices
sudo -u www-data php occ maintenance:mode --off
```
И посмотрите «Администрирование → Обзор» на предупреждения о схеме БД.

Если пришлёте вывод шагов 1–3 (код `changeSchema()`, SQL из лога и `SHOW CREATE TABLE`), скажу точно, какой из вариантов у вас и какой `ALTER` нужен.


## Answer B
## Что это за ошибка

Коротко: миграция core `32000Date20250806110519` пытается создать или изменить индекс, суммарная длина которого в байтах больше лимита InnoDB. Сама база от этого не повреждена. Сломано только обновление, которое остановилось на середине. Починить можно без экспериментов над данными.

Как считается лимит:
- В InnoDB с `ROW_FORMAT=DYNAMIC/COMPRESSED` ключ индекса не может быть длиннее **3072 байт**. В сообщении стоит именно 3072, значит large prefix уже работает и старые советы 2019–2021 годов («включите `innodb_large_prefix`, поменяйте row format») тут, скорее всего, не помогут. Те статьи были про лимит 767 байт.
- В `utf8mb4` один символ занимает до 4 байт. Поэтому индекс по `VARCHAR` (или по нескольким колонкам сразу), где в сумме больше 768 символов, в лимит не влезает. Например, `VARCHAR(1000)` даёт 4000 байт, и получается ошибка 1071.

Что именно индексирует эта миграция, я не знаю: содержимое `Version32000Date20250806110519` не проверял. Ниже гипотеза, её нужно подтвердить.

**Гипотеза:** миграция добавляет индекс по длинной строковой колонке (или по составному ключу). В `utf8mb4` он выходит за 3072 байта. Это либо баг Nextcloud 32.0.0 с MariaDB, либо особенность вашей схемы: например, у конкретной таблицы нестандартные charset или row format, или колонка длиннее, чем ожидает миграция.

## Шаг 0: бэкап перед любыми действиями

```bash
sudo mysqldump --single-transaction --routines --triggers nextcloud > ~/nc-db-before-fix-$(date +%F).sql
sudo tar -C /var/www -czf ~/nc-code-config-$(date +%F).tgz nextcloud/config
```
Дамп нужен, чтобы откатиться, если следующие шаги что-то сломают. Имя базы `nextcloud` и путь `/var/www/nextcloud` у вас могут быть другими, подставьте свои.

Важно: в MariaDB DDL не транзакционный. Всё, что миграции успели сделать до падения (в том числе предыдущие миграции 32.x), уже применено. Поэтому дамп отражает «полуобновлённое» состояние. Если у вас есть бэкап, сделанный **до** запуска `occ upgrade`, это ваша главная точка отката.

## Шаг 1: найти, какой именно запрос упал

Вы включили debug, поэтому полный SQL, скорее всего, есть в логе:
```bash
sudo grep -n '32000Date20250806110519' /var/www/nextcloud/data/nextcloud.log | tail -n 5
```
Там будет `ALTER TABLE ... ADD INDEX ...` или `CREATE INDEX ...` с именем таблицы и колонок.

И сам код миграции:
```bash
less /var/www/nextcloud/core/Migrations/Version32000Date20250806110519.php
```
Путь привожу по памяти, он соответствует обычной схеме именования в Nextcloud. Если файла нет, выполните `grep -rl 32000Date20250806110519 /var/www/nextcloud/core/`.

## Шаг 2: проверить таблицу и настройки

```sql
-- подставьте таблицу из лога
SELECT table_name, table_collation, row_format
FROM information_schema.tables
WHERE table_schema = 'nextcloud' AND table_name = 'oc_XXX';

SHOW CREATE TABLE oc_XXX\G

SHOW VARIABLES LIKE 'innodb_default_row_format';
SHOW VARIABLES LIKE 'character_set_server';
```
```bash
sudo -u www-data php occ config:system:get mysql.utf8mb4
```
Что смотреть:
- `row_format` должен быть `Dynamic`. Если у таблицы `Compact` или `Redundant`, это аномалия, которую можно исправить (см. ниже).
- Длина колонок, которые попадают в индекс, умноженная на 4 (для utf8mb4). Если сумма больше 3072, то ошибку даёт сама миграция, а не ваша конфигурация.

## Шаг 3: варианты исправления (от безопасного к рискованному)

1. **Поискать баг по ID миграции.** Запрос `32000Date20250806110519` или `"1071" nextcloud 32` в GitHub Issues проекта `nextcloud/server` и на help.nextcloud.com даст гораздо больше, чем общие запросы на русском. Если это баг 32.0.0, его, вероятно, исправили в точечном релизе 32.0.x. Я этого не проверял, посмотрите changelog. В таком случае положите код новой 32.0.x поверх (с сохранением `config/` и `data/`) и снова запустите `sudo -u www-data php occ upgrade`. Миграции, которые уже прошли, повторно не выполнятся.

2. **Если у таблицы `row_format` не `Dynamic`**, приведите её к нему:
   ```sql
   ALTER TABLE oc_XXX ROW_FORMAT=DYNAMIC;
   ```
   Этот запрос перестраивает таблицу, но данные не меняет. На большой таблице он займёт время и заблокирует её, но инстанс и так в maintenance. После этого снова `occ upgrade`.

3. **Откат на 31.0.9**, если исправления пока нет. Восстановите БД из бэкапа, сделанного до обновления, и код 31.0.9, затем:
   ```bash
   sudo -u www-data php occ maintenance:mode --off
   ```
   Без восстановления базы код 31 на схеме, которую частично обновили до 32, работать не будет. Nextcloud не поддерживает даунгрейд.

Чего **не** делать: вручную править `oc_migrations`, чтобы «пропустить» миграцию, и вручную обрезать или менять тип колонок по советам со старых форумов. Так можно получить схему, которую сломает следующее обновление.

## Проверка после исправления

```bash
sudo -u www-data php occ upgrade
sudo -u www-data php occ status
sudo -u www-data php occ db:add-missing-indices
sudo -u www-data php occ maintenance:mode --off
```
В `occ status` должна быть версия 32.0.x и `maintenance: false`. После этого проверьте страницу «Обзор» в админке: там не должно быть предупреждений о схеме БД.

Если пришлёте строку с SQL из `nextcloud.log` и вывод `SHOW CREATE TABLE` для этой таблицы, скажу точно, какой из вариантов ваш.

