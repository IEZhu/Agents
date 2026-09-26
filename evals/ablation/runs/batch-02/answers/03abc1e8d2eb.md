### Analysis

Ниже `get_duration`, сделанная по тем же правилам, что `get_int` и `get_bool`. Если переменная не задана, возвращается `default`, а если нет и его, бросается `ConfigError`. Мусор во значении тоже даёт `ConfigError` с именем ключа. Разбор идёт одним регулярным выражением с фиксированным порядком единиц `h → m → s → ms`. Поэтому `1h30m`, `45s`, `2m`, `500ms`, `1m30s500ms` проходят, а опечатки вроде `30m1h`, `1m1m`, `1d` и пустая строка получают внятную ошибку.

Есть два решения, которые стоит подтвердить:
- **Голое число без единицы (`REQUEST_TIMEOUT=30`) отклоняется.** Непонятно, секунды это или миллисекунды. Лучше упасть при старте, чем тихо выставить таймаут в 30 мс или в 30 000 с. Если нужна обратная совместимость со старыми значениями в секундах, это одна дополнительная ветка, её показал ниже.
- **Дробные значения разрешены** (`1.5h`, `0.5s`), отрицательные нет.

### Implementation

```python
import os
import re

# ...ConfigError, get_int, get_bool без изменений...

_NUM = r"\d+(?:\.\d+)?"
# Fixed order h -> m -> s -> ms rejects typos like "30m1h" or "1m1m".
# (?!s) after "m" keeps "500ms" from being read as "500m" + a stray "s".
_DURATION_RE = re.compile(
    rf"(?:(?P<h>{_NUM})h)?"
    rf"(?:(?P<m>{_NUM})m(?!s))?"
    rf"(?:(?P<s>{_NUM})s)?"
    rf"(?:(?P<ms>{_NUM})ms)?"
)
_SECONDS_PER_UNIT: dict[str, float] = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def get_duration(key: str, default: float | None = None) -> float:
    """Read a duration like '1h30m', '45s', '500ms' and return seconds."""
    raw = os.environ.get(key)
    if raw is None:
        if default is None:
            raise ConfigError(f"{key} is required")
        return default

    match = _DURATION_RE.fullmatch(raw.strip().lower())
    if match is None or not any(match.groupdict().values()):
        raise ConfigError(
            f"{key}: expected duration like '1h30m', '45s' or '500ms', got {raw!r}"
        )

    return sum(
        float(value) * _SECONDS_PER_UNIT[unit]
        for unit, value in match.groupdict().items()
        if value is not None
    )
```

Если всё же нужно принимать голые секунды, вставьте это перед `fullmatch`:

```python
    stripped = raw.strip()
    try:
        return float(stripped)
    except ValueError:
        pass
```

По умолчанию я бы так не делал, причины выше.

Тесты для `tests/test_config.py`:

```python
import pytest

from app.config import ConfigError, get_duration


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("45s", 45.0),
        ("2m", 120.0),
        ("500ms", 0.5),
        ("1h30m", 5400.0),
        ("1h", 3600.0),
        ("1m30s", 90.0),
        ("1h2m3s4ms", 3723.004),
        ("1.5h", 5400.0),
        ("0s", 0.0),
        ("  2M ", 120.0),
    ],
)
def test_get_duration_parses(monkeypatch, raw, expected):
    monkeypatch.setenv("REQUEST_TIMEOUT", raw)
    assert get_duration("REQUEST_TIMEOUT") == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw",
    ["", "30", "1d", "30m1h", "1m1m", "-5s", "1h 30m", "ms", "1.s", "abc"],
)
def test_get_duration_rejects_malformed(monkeypatch, raw):
    monkeypatch.setenv("REQUEST_TIMEOUT", raw)
    with pytest.raises(ConfigError, match="REQUEST_TIMEOUT"):
        get_duration("REQUEST_TIMEOUT")


def test_get_duration_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("REQUEST_TIMEOUT", raising=False)
    assert get_duration("REQUEST_TIMEOUT", default=30.0) == 30.0


def test_get_duration_requires_value_without_default(monkeypatch):
    monkeypatch.delenv("REQUEST_TIMEOUT", raising=False)
    with pytest.raises(ConfigError, match="is required"):
        get_duration("REQUEST_TIMEOUT")
```

### Verification

- Запустить: `pytest tests/test_config.py -q`. Сам я код не запускал, регулярку проверял только вручную на примерах. Сложнее всего здесь `500ms`: сначала группа `m` пытается забрать `500m`, но `(?!s)` её отбрасывает, и значение уходит в группу `ms`. Тот же случай с `1m500ms`: `1m` матчится нормально, потому что после него идёт `5`.
- Пробел внутри (`1h 30m`) намеренно не разрешён. Если нужен, добавьте `\s*` между группами.
- Для чисел используется `pytest.approx`, потому что `0.001 * 4` в float не точно равно `0.004`.
- Сложность O(len(raw)), регулярка компилируется один раз на уровне модуля, катастрофического backtracking нет (все группы опциональны и идут в фиксированном порядке).
- Один нюанс с `default`: как и в `get_int`, `default=None` означает «обязательный параметр», поэтому «необязательный без значения» так не выразить. Это сделано для единообразия с существующим API.

### Confidence

MEDIUM: логика простая и повторяет существующие хелперы, но тесты в этом ответе не прогонялись, так что сначала запустите `pytest`.
