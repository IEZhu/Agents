# Case xdist-flaky-cache-order-dependency-ru

## Conversation so far
(none)

## Latest user message
Python 3.11, pytest 8.1. Включили pytest-xdist (-n 4), чтобы ускорить CI, и после этого тесты модуля курсов валют стали падать примерно в каждом пятом прогоне (в проекте ~400 тестов). Последовательно, без -n, всё зелёное. Чаще всего падает test_cached_rate_survives_api_outage с ConnectionError.

Код примерно такой:

```python
# app/rates.py
import time

_CACHE = {}
TTL = 1.0

def get_rate(currency):
    entry = _CACHE.get(currency)
    if entry and time.monotonic() - entry[1] < TTL:
        return entry[0]
    value = fetch_rate_from_api(currency)  # ходит в HTTP
    _CACHE[currency] = (value, time.monotonic())
    return value
```

```python
# tests/test_rates.py
import time
from unittest.mock import patch
from app.rates import get_rate

@patch("app.rates.fetch_rate_from_api", return_value=90.5)
def test_rate_is_cached(mock_fetch):
    get_rate("USD")
    get_rate("USD")
    assert mock_fetch.call_count == 1

@patch("app.rates.fetch_rate_from_api", side_effect=ConnectionError)
def test_cached_rate_survives_api_outage(mock_fetch):
    assert get_rate("USD") == 90.5

@patch("app.rates.fetch_rate_from_api", return_value=101.0)
def test_rate_expires(mock_fetch):
    get_rate("EUR")
    time.sleep(1.1)
    get_rate("EUR")
    assert mock_fetch.call_count == 2
```

Хочу сделать по-быстрому: навесить на эти три теста @pytest.mark.xdist_group("rates"), чтобы они шли в одном воркере, запускать с --dist loadgroup и плюс поставить pytest-rerunfailures с --reruns 2 на всякий случай. Нормальное решение?

## Rubric (what a strong answer does)
1. Correctly explains the failure: test_cached_rate_survives_api_outage does not populate the cache itself and only passes when test_rate_is_cached has run shortly before in the same worker process (shared module-level _CACHE, within the 1 s TTL); under xdist it can land on another worker or run later, so fetch is called and raises ConnectionError
2. Advises against grouping into one worker and reruns as the fix, because they keep the hidden order dependency (e.g. the test still fails when run alone or in a different order) and mask rather than remove the cause
3. Proposes making each test independent: reset the cache before each test (e.g. an autouse fixture clearing _CACHE or monkeypatching it with a fresh dict) and have the outage test arrange its own cached entry
4. Proposes removing the real time.sleep by controlling time in the test (e.g. patching app.rates.time.monotonic or injecting a clock), making the expiry test deterministic and fast

## Answer A
### Коротко

Нет, это не решение. `xdist_group` и `--reruns` спрячут симптом, а в тестах и в коде останутся две настоящие проблемы:

1. **Тесты зависят от порядка запуска.** `test_cached_rate_survives_api_outage` проходит только тогда, когда в том же процессе меньше секунды назад отработал `test_rate_is_cached` и положил `USD` = 90.5 в модульный `_CACHE`. При последовательном прогоне так совпадает по порядку. xdist раскидывает тесты по воркерам, и если outage-тест попадает в «чистый» воркер, кэш пуст, вызывается мок, и летит `ConnectionError`. Отсюда падения «примерно в каждом пятом прогоне»: всё зависит от того, как планировщик распределил тесты.
2. **Тест проверяет не то, что написано в его названии.** В `get_rate` нет никакого fallback при недоступности API. Если запись в кэше протухла (прошло больше `TTL`), `ConnectionError` уходит наверх. Тест зелёный только потому, что успевает попасть в свежий кэш. Проверить легко, xdist не нужен:

```bash
pytest tests/test_rates.py::test_cached_rate_survives_api_outage   # упадёт и без -n
```

Почему предложенный вариант плох:
- `xdist_group` + `--dist loadgroup` загонят три теста в один воркер, но зависимость от порядка и от таймингов (TTL 1 с) никуда не денется. Она просто перестанет быть видна. Любой следующий тест, который тоже дёргает `get_rate("USD")`, опять всё сломает. Например, `test_rate_is_cached` получит `call_count == 0` из-за чужого кэша.
- `--reruns 2` глобально делает CI «зелёным» для любого flaky-теста в проекте, и будущие гонки вы перестанете замечать. К тому же здесь rerun часто и не поможет: при повторе кэш, скорее всего, уже старше секунды.
- Главное: в проде при падении API сервис отдаст исключение, а не закэшированный курс, хотя тест утверждает обратное.

### Как исправить

Сначала вопрос к бизнес-логике: должен ли `get_rate` при недоступности API отдавать устаревший курс? Название теста говорит, что должен. Если так, нужно доделать код, изолировать кэш между тестами и убрать `sleep`.

```python
# app/rates.py
import logging
import time

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, float]] = {}
TTL = 1.0
_now = time.monotonic  # отдельная точка подмены времени в тестах


def get_rate(currency: str) -> float:
    entry = _CACHE.get(currency)
    if entry and _now() - entry[1] < TTL:
        return entry[0]
    try:
        value = fetch_rate_from_api(currency)
    except ConnectionError:
        if entry is None:
            raise
        logger.warning("Rate API unavailable for %s, serving stale rate", currency)
        return entry[0]
    _CACHE[currency] = (value, _now())
    return value
```

Стоит решить, нужен ли потолок «устаревания» (например, не отдавать курс старше суток). Это продуктовое решение, поэтому в код я его не добавлял.

```python
# tests/conftest.py — autouse, чтобы изоляция действовала на все тесты, которые трогают курсы
import pytest
from app import rates


@pytest.fixture(autouse=True)
def clear_rate_cache():
    rates._CACHE.clear()
    yield
    rates._CACHE.clear()


@pytest.fixture
def clock(monkeypatch):
    current = {"t": 1000.0}
    monkeypatch.setattr(rates, "_now", lambda: current["t"])
    return current
```

```python
# tests/test_rates.py
from unittest.mock import patch

import pytest
from app import rates
from app.rates import get_rate


def test_rate_is_cached(clock):
    with patch("app.rates.fetch_rate_from_api", return_value=90.5) as mock_fetch:
        get_rate("USD")
        get_rate("USD")
    assert mock_fetch.call_count == 1


def test_rate_expires(clock):
    with patch("app.rates.fetch_rate_from_api", return_value=101.0) as mock_fetch:
        get_rate("EUR")
        clock["t"] += rates.TTL + 0.1
        get_rate("EUR")
    assert mock_fetch.call_count == 2


def test_cached_rate_survives_api_outage(clock):
    with patch("app.rates.fetch_rate_from_api", return_value=90.5):
        get_rate("USD")
    clock["t"] += rates.TTL + 0.1  # кэш протух: проверяем именно fallback, а не cache hit
    with patch("app.rates.fetch_rate_from_api", side_effect=ConnectionError):
        assert get_rate("USD") == 90.5


def test_outage_without_cache_raises(clock):
    with patch("app.rates.fetch_rate_from_api", side_effect=ConnectionError):
        with pytest.raises(ConnectionError):
            get_rate("USD")
```

Что это даёт:
- Каждый тест сам готовит своё состояние (Arrange внутри теста). Порядок и распределение по воркерам больше не важны, так что `xdist_group` не нужен.
- Время подменяется вместо `time.sleep(1.1)`: тесты становятся детерминированными и на секунду быстрее, а ради скорости вы xdist и включали.
- Outage-тест теперь действительно проверяет fallback на протухшем кэше. Плюс добавлен тест на случай «кэша нет, API лежит».

### Проверка

Я это не запускал. Прогоните:

```bash
pytest tests/test_rates.py -p no:cacheprovider -q                                        # последовательно
pytest tests/test_rates.py::test_cached_rate_survives_api_outage -q                       # изолированно
pytest -n 4 -q --count=20 tests/test_rates.py   # если стоит pytest-repeat; иначе просто несколько прогонов -n 4
```

Если после этого в остальных ~400 тестах всплывут другие падения под xdist, ищите там тот же паттерн: модульное состояние (`_CACHE`, синглтоны, глобальные клиенты) и тесты, которые рассчитывают на побочные эффекты соседей. `--reruns` в CI я бы допускал только точечно, через `@pytest.mark.flaky` на тестах, которые действительно ходят во внешний мир, и с заведённым тикетом.

### Уверенность

MEDIUM: причина видна прямо из кода (зависимость от порядка плюс отсутствие fallback), но исправление я не запускал. `--count` берётся из pytest-repeat (recalled, not verified — confirm in the source).


## Answer B
Нет, так делать не стоит. xdist здесь ничего не сломал. Он показал, что `test_cached_rate_survives_api_outage` с самого начала работал только благодаря случайности: он зависит от того, что до него в **том же процессе** и **меньше чем за 1 секунду** выполнился `test_rate_is_cached`, который положил `USD = 90.5` в глобальный `_CACHE`. `xdist_group` прячет эту зависимость, а `--reruns` для этого теста даже не поможет.

## Почему падает

- Сам тест `test_cached_rate_survives_api_outage` ничего в кэш не кладёт. Если в `_CACHE` нет свежего `USD`, `get_rate` вызывает замоканный `fetch_rate_from_api`, и тот бросает `ConnectionError`.
- В последовательном прогоне тесты идут в порядке файла, и предыдущий тест оставляет кэш «тёплым». С `-n 4` соседние тесты иногда оказываются в разных воркерах (это отдельные процессы, у каждого свой `_CACHE`), отсюда примерно «каждый пятый» прогон.
- Воспроизводится без всякого xdist, детерминированно:
  ```bash
  pytest "tests/test_rates.py::test_cached_rate_survives_api_outage"
  ```
  Если у вас так не падает, значит кэш заполняет что-то ещё (conftest, другой модуль), и это тоже стоит найти.

## Что не так с предложенным решением

1. **`--reruns 2` не спасёт.** Повтор выполняется в том же воркере, а кэш там по-прежнему пустой, поэтому тест упадёт все три раза. Зато на остальных ~400 тестах reruns будет незаметно скрывать настоящую нестабильность.
2. **`xdist_group` + `--dist loadgroup` гарантирует только, что группа попадёт в один воркер.** Порядок тестов внутри группы и то, что между ними не пройдёт больше `TTL`, это уже побочный эффект текущей реализации планировщика. На документированный контракт я бы здесь не полагался (это по памяти, не проверял). Хватит `pytest-randomly`, `-k` или медленного CI-раннера, и тест снова упадёт.
3. **Тест проверяет не то, что написано в его названии.** Сейчас `get_rate` при недоступном API **не** отдаёт устаревшее значение: после истечения TTL `ConnectionError` просто уходит наружу. Тест «survives outage» проверяет только «в пределах TTL в API не ходим», и то лишь при удачном порядке.
4. Заодно `test_rate_is_cached` тоже хрупкий: если какой-то тест в том же воркере недавно закэшировал `USD`, получится `call_count == 0`.

## Нормальный фикс (по времени примерно как навесить маркеры)

Нужно изолировать состояние и управлять временем. Для этого сбрасываем `_CACHE` перед каждым тестом, подменяем часы и сделаем так, чтобы каждый тест сам готовил своё окружение.

```python
# tests/test_rates.py
from unittest.mock import patch

import pytest

from app import rates
from app.rates import get_rate


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clear_rates_cache():
    rates._CACHE.clear()
    yield
    rates._CACHE.clear()


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    # Подменяем ссылку на модуль time только внутри app.rates,
    # а не глобальный time.monotonic, чтобы не задеть pytest и другие модули.
    monkeypatch.setattr(rates, "time", fake)
    return fake


def test_rate_is_cached(clock):
    with patch("app.rates.fetch_rate_from_api", return_value=90.5) as mock_fetch:
        assert get_rate("USD") == 90.5
        assert get_rate("USD") == 90.5
    assert mock_fetch.call_count == 1


def test_cached_rate_survives_api_outage(clock):
    with patch("app.rates.fetch_rate_from_api", return_value=90.5):
        get_rate("USD")  # прогреваем кэш явно, внутри теста

    clock.advance(rates.TTL / 2)

    with patch("app.rates.fetch_rate_from_api", side_effect=ConnectionError) as mock_fetch:
        assert get_rate("USD") == 90.5
    mock_fetch.assert_not_called()


def test_rate_expires(clock):
    with patch("app.rates.fetch_rate_from_api", return_value=101.0) as mock_fetch:
        get_rate("EUR")
        clock.advance(rates.TTL + 0.1)
        get_rate("EUR")
    assert mock_fetch.call_count == 2
```

Что это даёт:
- тесты не зависят от порядка и от воркера, так что никакие `xdist_group` и `--reruns` не нужны;
- из `test_rate_expires` ушёл `time.sleep(1.1)`, а это ещё и прямое ускорение CI, ради которого вы включали xdist;
- если `get_rate` используют и в других тестовых модулях, перенесите `clear_rates_cache` в `tests/conftest.py`.

## Продуктовый вопрос, который стоит решить

Если под «survives API outage» на самом деле имелось в виду «при недоступном API отдаём последнее известное значение, даже устаревшее» (stale-if-error), то это нужно менять в коде, а не в тестах:

```python
def get_rate(currency):
    entry = _CACHE.get(currency)
    if entry and time.monotonic() - entry[1] < TTL:
        return entry[0]
    try:
        value = fetch_rate_from_api(currency)
    except ConnectionError:
        if entry is not None:
            logger.warning("Rates API unavailable, serving stale %s rate", currency)
            return entry[0]
        raise
    _CACHE[currency] = (value, time.monotonic())
    return value
```

Тогда тест на outage должен делать `clock.advance(rates.TTL + 1)` и проверять, что вернулось устаревшее значение. Отдавать ли устаревший курс валют, решает бизнес: для некоторых сценариев (платежи) это может быть хуже, чем честная ошибка.

## Как проверить

```bash
# раньше падал детерминированно, после фикса должен пройти
pytest "tests/test_rates.py::test_cached_rate_survives_api_outage"
# прогон в обратном/случайном порядке (если поставите pytest-randomly)
pytest -p randomly tests/test_rates.py
# несколько прогонов под xdist
for i in $(seq 10); do pytest -n 4 -q || break; done
```

Код выше я не запускал: он написан под ваш фрагмент `app/rates.py`. Если в реальном модуле используется `from time import monotonic`, а не `import time`, то патчить нужно `app.rates.monotonic`.

**Уверенность: MEDIUM.** Диагноз (зависимость от порядка через глобальный кэш и TTL) напрямую следует из показанного кода, но исправление в этом ходе не прогонялось.

