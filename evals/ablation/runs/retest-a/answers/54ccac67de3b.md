## Итог

Ощущение верное. Тест проверяет не поведение, а то, что `checkout` вызывает свои же методы в нужном порядке. Самое важное для денег не проверяется: сколько списали и записали ли оплату. 100% покрытия тут значит только одно: строки выполнились. Что они сделали правильно, никто не проверил.

**Вердикт**: REQUEST CHANGES · **Риск**: HIGH (это оплата)

## Замечания

### [BLOCKER] Замоканы приватные методы самого тестируемого класса
`svc._calc_total` и `svc._apply_discount` подменены. Получается, что бизнес-логику (подсчёт суммы, скидку) этот тест вообще не исполняет.
- **Почему важно**: если в `_apply_discount` скидка считается неправильно, тест останется зелёным. Он зелёный при любой реализации этих методов.
- **Про покрытие**: в показанном фрагменте `_calc_total` и `_apply_discount` не определены. Если отчёт показывает 100% по файлу, то либо эти методы покрыты другими тестами, либо фрагмент обрезан, либо они живут в другом месте. Стоит посмотреть, откуда в отчёте взялись 100%. Этим тестом их тела точно не покрыты.
- **Как исправить**: мокать только внешние границы (`repo`, `payment_gateway`), а класс тестировать целиком: на вход реальная корзина, на выходе сумма.

### [BLOCKER] Не проверяется сумма, которую списали
`svc.gw.charge.assert_called_once()` без аргументов. Это главный эффект метода, и он не проверен.
- **Сценарий**: кто-то по ошибке передаёт в `charge` `total` до скидки (1000 вместо 900). `result == 900` пройдёт, `assert_called_once()` тоже пройдёт, а клиент заплатит лишнее.
- **Как исправить**: `gw.charge.assert_called_once_with(cart.user_id, 900)`.

### [BLOCKER] `mark_paid` не проверяется совсем
Если удалить строку `self.repo.mark_paid(...)`, тест останется зелёным. Деньги списаны, а заказ не помечен как оплаченный. Это наглядный пример того, что строка покрыта, но не проверена.
- **Как исправить**: `repo.mark_paid.assert_called_once_with(42, gw.charge.return_value.id)`.

### [MAJOR] Нет тестов на ошибки
Сейчас проверен только happy path. Не хватает как минимум:
- `gw.charge` бросает исключение, значит `mark_paid` не должен вызываться, а исключение должно выйти наружу;
- `repo.get_cart` не нашёл корзину (что возвращает `None` или какое исключение бросает, зависит от контракта репозитория, уточните);
- граничные суммы: пустая корзина, `total == 0`, скидка больше суммы. Надо решить, списываем ли 0 или отрицательную сумму.

### [MAJOR] `MagicMock()` без `spec`
Мок без спецификации принимает любой атрибут. Если в gateway метод переименуют (`charge` → `create_charge`), тест продолжит проходить. Опечатка вида `assert_caled_once()` в старых версиях Python тоже молча проходила (в новых часть таких опечаток ловится, но не все, это по памяти, не проверял). Лучше `create_autospec(PaymentGateway, instance=True)` или простые fake-классы.

### [MINOR] Не проверено, что корзина запрошена по правильному id
`repo.get_cart.assert_called_once_with(42)` отсутствует. Вызов `get_cart` с чем угодно тест пропустит.

### [MINOR] Один тест на всё
Один `test_checkout` с кучей assert'ов. Когда он упадёт, по названию будет непонятно, что сломалось. Лучше несколько тестов с именами-сценариями: `test_charges_discounted_total`, `test_marks_cart_paid_with_charge_id`, `test_does_not_mark_paid_when_charge_fails`.

### [POSITIVE] Зависимости передаются через конструктор
`CheckoutService(repo, payment_gateway)` устроен так, что границы легко подменить, и правильный тест пишется без `patch`. Хорошая основа.

## Как это могло бы выглядеть

```python
from unittest.mock import create_autospec
import pytest

def make_svc(cart):
    repo = create_autospec(CartRepository, instance=True)
    gw = create_autospec(PaymentGateway, instance=True)
    repo.get_cart.return_value = cart
    return CheckoutService(repo, gw), repo, gw

def test_charges_discounted_total_and_marks_paid():
    cart = make_cart(user_id=7, items=[item(price=1000)], discount_pct=10)
    svc, repo, gw = make_svc(cart)

    result = svc.checkout(42)

    assert result == 900
    repo.get_cart.assert_called_once_with(42)
    gw.charge.assert_called_once_with(7, 900)
    repo.mark_paid.assert_called_once_with(42, gw.charge.return_value.id)

def test_does_not_mark_paid_when_charge_fails():
    svc, repo, gw = make_svc(make_cart(user_id=7, items=[item(price=1000)]))
    gw.charge.side_effect = PaymentError("declined")

    with pytest.raises(PaymentError):
        svc.checkout(42)

    repo.mark_paid.assert_not_called()
```

Имена `CartRepository`, `PaymentGateway`, `PaymentError`, `make_cart`, `item` условные, подставьте реальные из проекта. Расчёт суммы и скидки лучше покрыть отдельными табличными тестами (`pytest.mark.parametrize`) через публичный `checkout` или вынести в чистую функцию.

## Вне скоупа тестов, но стоит обсудить

В самом `checkout` есть проблема. Если `charge` прошёл, а `mark_paid` упал, деньги списаны, а корзина не оплачена. При повторной попытке клиента спишут второй раз. Нужен идемпотентный ключ для gateway (например, `cart_id`) или статус «pending» до списания. Тест на этот сценарий сразу покажет проблему.

## Сводка
- 3 blocker, 2 major, 2 minor.
- Главное, что стоит сказать джуну: **coverage показывает, какие строки выполнились, но не показывает, какие результаты проверены.** Мокать надо внешние границы, а не внутренности тестируемого класса. Проверять надо эффекты (сколько списали, что записали), а не сам факт вызова.
