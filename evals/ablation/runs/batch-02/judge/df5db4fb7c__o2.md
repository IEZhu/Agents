# Case nginx-413-upload-terse-ru

## Conversation so far
(none)

## Latest user message
Привет! У нас nginx 1.24 стоит как reverse proxy перед Django (gunicorn на 127.0.0.1:8000). Пользователи загружают PDF через форму, и всё, что больше примерно мегабайта, падает с "413 Request Entity Too Large" — причём страница ошибки от nginx, до Django запрос даже не доходит. Нужно разрешить файлы до 50 МБ. Что поправить в конфиге?

## Rubric (what a strong answer does)
1. The answer opens with the actual fix (client_max_body_size set to at least 50m) in its first sentence or first code line, with no greeting, praise or restatement of the user's problem before it.
2. The fix is correct: it states that client_max_body_size defaults to 1m, places it in the http, server or location block that handles the upload, and says to test and reload nginx (e.g. nginx -t, then nginx -s reload or systemctl reload nginx).
3. The prose around the config is compressed: short fragments or a short list, no filler openers or closers (e.g. 'Давайте разберёмся', 'Надеюсь, это поможет', 'Если будут вопросы — пишите'), and no general explanation of what a reverse proxy or HTTP 413 is.
4. Any extra caveat (e.g. timeouts for slow large uploads, or a second proxy/load balancer in front with its own limit) takes at most one or two short lines and is not presented as the main fix.

## Answer A
Нужно поднять `client_max_body_size`. По умолчанию в nginx он равен `1m`, поэтому всё, что больше мегабайта, nginx отклоняет с 413 и до gunicorn запрос не доходит. Это совпадает с тем, что вы видите.

## Исправление

```nginx
server {
    listen 443 ssl;
    server_name example.com;

    # Лимит на размер тела запроса. Запас сверх 50 МБ нужен на multipart-обёртку
    # и остальные поля формы
    client_max_body_size 55m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Директива работает в контекстах `http`, `server` и `location`. Действует значение из самого вложенного блока, поэтому:

- лимит нужно ставить либо в `server` нужного сайта, либо прямо в `location` с эндпоинтом загрузки (например, `location /upload/ { client_max_body_size 55m; ... }`), если большие файлы не нужны на всём сайте;
- проверьте, что ниже не осталось другого `client_max_body_size` с меньшим значением, иначе он перекроет ваш. Найти все вхождения:
  ```bash
  sudo nginx -T 2>/dev/null | grep -n client_max_body_size
  ```

## Что ещё проверить для файлов по 50 МБ

1. **Таймауты для медленных клиентов.** `client_body_timeout` (по умолчанию 60s) задаёт паузу между двумя операциями чтения, а не время на весь запрос, так что обычно его хватает. При плохих каналах можно поднять до `120s`.
2. **Буферизация.** По умолчанию `proxy_request_buffering on`: nginx сначала целиком принимает тело во временный файл (`client_body_temp_path`) и только потом отдаёт его gunicorn. Для sync-воркеров gunicorn это как раз хорошо: воркер не висит на медленном клиенте. Убедитесь, что на диске с временным каталогом хватает места и у пользователя nginx есть права на запись.
3. **Django.** На сами файлы `DATA_UPLOAD_MAX_MEMORY_SIZE` (по умолчанию 2.5 МБ) не распространяется: файловые части multipart в этот лимит не входят. `FILE_UPLOAD_MAX_MEMORY_SIZE` определяет только порог, после которого файл пишется во временный файл на диске вместо памяти, отказа он не вызывает. Если в форме или во view есть своя проверка размера, её тоже поднимите до 50 МБ.
4. **Gunicorn `--timeout`** (по умолчанию 30s). Передача от nginx по localhost быстрая, но если после загрузки PDF синхронно обрабатывается (парсинг, OCR), 30 секунд может не хватить, и воркер будет убит с 502/504. Тогда поднимайте таймаут или выносите обработку в фоновую задачу.
5. **Слой перед nginx.** Если перед сервером стоит CDN или балансировщик (Cloudflare, облачный LB), у него свой лимит на размер тела, и 413 может прийти уже от него.

## Применение

```bash
sudo nginx -t && sudo systemctl reload nginx
```

`reload` применяет конфиг без обрыва текущих соединений. Если `nginx -t` найдёт ошибку, reload не выполнится и старый конфиг продолжит работать.

## Проверка

```bash
# Тестовый файл на 45 МБ
dd if=/dev/urandom of=/tmp/test.pdf bs=1M count=45

curl -s -o /dev/null -w '%{http_code}\n' \
  -F "file=@/tmp/test.pdf" https://example.com/upload/
```

Вы должны получить ответ от Django: 200/302, либо 403, если мешает CSRF. Главное, чтобы это был не 413. Файл на 60 МБ по-прежнему должен получать 413 от nginx.

## Откат

Верните прежнее значение (или удалите строку) и снова выполните `nginx -t && systemctl reload nginx`. Что затрагивает изменение: только максимальный размер принимаемых запросов для этого `server`/`location`. Побочный риск в том, что клиенты смогут слать большие тела, поэтому ограничение по `location` для эндпоинта загрузки безопаснее, чем глобальное.


## Answer B
Нужно поправить директиву `client_max_body_size`. По умолчанию в nginx она равна `1m`. Отсюда и ошибка 413 на файлах примерно от мегабайта: nginx отклоняет тело запроса сам и до gunicorn его не пропускает.

## Что поправить

Лучше поднимать лимит только там, куда идут загрузки, а не на весь сайт. Так остальные эндпоинты по-прежнему не примут огромные тела запросов.

```nginx
server {
    listen 443 ssl;
    server_name example.com;

    # Для всего сайта оставляем небольшой лимит
    client_max_body_size 2m;

    # Эндпоинт, на который отправляется форма с PDF
    location /upload/ {
        # 50 МБ файла плюс запас на multipart-обёртку и остальные поля формы
        client_max_body_size 55m;

        # Большой файл дольше идёт от клиента, поэтому таймауты чуть щедрее
        client_body_timeout 120s;
        proxy_read_timeout  120s;

        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        # ... остальные ваши настройки
    }
}
```

Если отдельного location под загрузку нет или разделять не хочется, можно просто написать `client_max_body_size 55m;` на уровне `server` (или `http`). Директива работает в контекстах `http`, `server` и `location`.

Почему `55m`, а не ровно `50m`. Тело multipart-запроса больше самого файла: к нему добавляются boundary, заголовки частей, CSRF-токен и другие поля. Если поставить ровно `50m`, файл размером ровно 50 МБ снова получит 413. Точный лимит на сам файл лучше проверять в Django, в валидаторе формы, и показывать пользователю понятное сообщение.

## Частые подвохи

- **Значение переопределено где-то ещё.** Самый специфичный контекст перекрывает общий. Если во вложенном `location` или в подключённом файле из `conf.d/` / `sites-enabled/` стоит своё `client_max_body_size`, ваше значение из `server` там не действует. Посмотреть все вхождения в итоговом конфиге можно так:
  ```bash
  sudo nginx -T 2>/dev/null | grep -n client_max_body_size
  ```
- **Правка попала не в тот `server`.** Если server-блоков несколько (например, `default_server` и ваш домен), убедитесь, что меняете тот, который обслуживает запрос.
- **Перед nginx есть ещё прокси** (балансировщик, CDN, ingress). У него может быть свой лимит. Тогда ответ 413 придёт уже от него, и страница ошибки будет выглядеть иначе.

## Django и gunicorn

После nginx запрос дойдёт до Django, и там, скорее всего, ничего менять не придётся:

- `DATA_UPLOAD_MAX_MEMORY_SIZE` (по умолчанию 2.5 МБ) на содержимое загружаемых файлов не распространяется, только на остальные данные тела запроса.
- `FILE_UPLOAD_MAX_MEMORY_SIZE` лимитом не является. Он определяет, когда файл перестаёт держаться в памяти и начинает писаться во временный файл на диске. Проверьте, что во временном каталоге (`FILE_UPLOAD_TEMP_DIR` или системный `/tmp`) хватает места.
- В gunicorn по умолчанию нет лимита на размер тела запроса. Кроме того, nginx по умолчанию сначала целиком буферизует тело (`proxy_request_buffering on`), а потом быстро отдаёт его gunicorn. Поэтому медленный клиент обычно не упирается в `--timeout` gunicorn (по умолчанию 30 с). Если долго идёт уже обработка PDF в самом Django, `--timeout` придётся увеличить.

Эти значения по умолчанию я привожу по памяти. Если что-то ведёт себя иначе, сверьтесь с документацией Django и gunicorn для своих версий.

## Применение

```bash
sudo nginx -t && sudo systemctl reload nginx
```

`reload` перечитывает конфиг без обрыва текущих соединений. Если `nginx -t` найдёт ошибку, reload не выполнится и продолжит работать старый конфиг.

## Проверка

```bash
# Тестовый файл на 45 МБ
dd if=/dev/zero of=/tmp/test.pdf bs=1M count=45

curl -s -o /dev/null -w "%{http_code}\n" \
  -F "file=@/tmp/test.pdf" https://example.com/upload/
```

Вместо 413 должен прийти ответ от Django. Если форма защищена CSRF, это может быть 403, но это уже означает, что запрос дошёл до приложения. Надёжнее всего проверить загрузку через саму форму в браузере.

## Откат

Чтобы откатиться, верните прежнее значение (или удалите строку) и снова выполните `nginx -t && systemctl reload nginx`. Если что-то сломается, пострадают только загрузки на этом location, остальной трафик изменение не затрагивает.

