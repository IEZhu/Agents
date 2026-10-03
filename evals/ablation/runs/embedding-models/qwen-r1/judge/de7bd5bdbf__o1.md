# Case en-float-compare

## Conversation so far
(none)

## Latest user message
Why does 0.1 + 0.2 == 0.3 evaluate to False in Python, and what's the right way to compare floats? We also store prices — should we use float for those?

## Rubric (what a strong answer does)
1. Explains binary floating point: 0.1 and 0.2 have no exact binary form, so the sum is 0.30000000000000004.
2. Recommends math.isclose with a tolerance for comparisons.
3. Recommends Decimal or integer minor units, not float, for prices.

## Answer A
### Analysis

Three related questions: the IEEE 754 root cause, the correct comparison idiom, and the storage question for monetary values. All three are settled knowledge; no tools needed.

### Why `0.1 + 0.2 == 0.3` is `False`

Python floats are **IEEE 754 binary64** (64-bit double-precision). The number 0.1 has no finite binary representation — it's `0.00011001100110011…` repeating, just as 1/3 is `0.333…` in decimal. The hardware stores the *nearest* representable double, which is slightly off.

```python
>>> 0.1 + 0.2
0.30000000000000004
>>> 0.3
0.3
>>> 0.1 + 0.2 == 0.3
False
```

`0.1 + 0.2` rounds to the double `0.30000000000000004440892098500626…`, while the literal `0.3` rounds to `0.29999999999999998889776975374843…`. They are two *different* 64-bit patterns, so `==` (exact bit comparison) returns `False`.

This isn't a Python bug — it's a property of binary floating-point arithmetic in every language that uses IEEE 754 (C, Java, JS, Rust, …).

### The right way to compare floats

**General-purpose: `math.isclose`** (Python ≥ 3.5)

```python
import math

math.isclose(0.1 + 0.2, 0.3)          # True
math.isclose(0.1 + 0.2, 0.3, rel_tol=1e-9, abs_tol=0.0)  # explicit
```

- `rel_tol` — relative tolerance (default `1e-9`). Scales with the magnitude of the values.
- `abs_tol` — absolute tolerance (default `0.0`). Needed when comparing values near zero, where relative tolerance is meaningless.

**When to still use `==`:** comparing against a value you *know* is exactly representable (integers, powers of two, `0.0`), or checking `x == 0.0` after a computation that should yield exact zero.

**For arrays:** `numpy.isclose(a, b, rtol=1e-5, atol=1e-8)`.

### Should you store prices as `float`?

**No.** Use one of these, in order of preference:

| Approach | When | Example |
|---|---|---|
| **Integer minor units** (cents, pence) | Simplest, fastest, no rounding surprises. Best for most e-commerce / ledger systems. | `price_cents: int = 1999`  →  $19.99 |
| **`decimal.Decimal`** | You need arbitrary precision, variable decimal places (e.g. 4 dp for FX rates), or you're doing arithmetic in Python. | `Decimal("19.99") + Decimal("0.01")` → `Decimal("20.00")` |
| **`float`** | Scientific / engineering computation where a few ULPs of error are acceptable. **Not for money.** | — |

Practical notes:

- **In Python**, always construct `Decimal` from a *string*, not a float: `Decimal("0.1")` is exact; `Decimal(0.1)` inherits the binary error.
- **In a database**, use `NUMERIC(10, 2)` / `DECIMAL(10, 2)` (Postgres, MySQL, SQLite) or an integer column for cents. Avoid `FLOAT` / `DOUBLE PRECISION` for monetary columns.
- **In JSON / API contracts**, serialise as a string (`"19.99"`) or as an integer in minor units to avoid the client re-introducing float error.
- **Rounding:** decide your rounding mode explicitly (`ROUND_HALF_UP` for most business rules, `ROUND_HALF_EVEN` / banker's rounding for some financial standards). `Decimal` lets you set this per-operation.

```python
from decimal import Decimal, ROUND_HALF_UP

price = Decimal("19.99")
tax   = (price * Decimal("0.08")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
total = price + tax  # Decimal("21.59")
```

### Verification

- The IEEE 754 explanation and the `0.30000000000000004` output are the canonical, well-documented behaviour (see the [Python docs on floating-point](https://docs.python.org/3/tutorial/floatingpoint.html) and the [IEEE 754-2019 standard]).
- `math.isclose` signature and defaults are from the [Python 3.12 docs](https://docs.python.org/3/library/math.html#math.isclose).
- `Decimal` string-construction rule is in the [decimal module docs](https://docs.python.org/3/library/decimal.html).

### Confidence

**HIGH** — IEEE 754 behaviour, `math.isclose`, and `Decimal` usage are long-settled, well-documented facts; no version-specific or ambiguous API involved.


## Answer B
`0.1 + 0.2 == 0.3` is `False` because Python `float` values are **IEEE 754 binary64** numbers, not exact decimal numbers.

### Why it happens

Decimal values like `0.1` and `0.2` cannot be represented exactly in binary, just like `1/3` cannot be represented exactly in decimal. Python stores the nearest representable binary approximation.

```python
>>> 0.1 + 0.2
0.30000000000000004

>>> 0.1 + 0.2 == 0.3
False
```

The literal `0.3` is also an approximation, but it is a *different* approximation from the result of `0.1 + 0.2`. `==` compares the exact binary values, so they are not equal.

This is not a Python bug. It is standard floating-point behavior in most languages.

---

### The right way to compare floats

It depends on what you mean by “equal”.

#### 1. For numerical results: use a tolerance

If you are comparing computed floating-point values, use approximate equality.

```python
import math

math.isclose(0.1 + 0.2, 0.3)
# True
```

`math.isclose` uses relative and absolute tolerances:

```python
math.isclose(a, b, rel_tol=1e-9, abs_tol=0.0)
```

- `rel_tol` is useful when comparing values of similar magnitude.
- `abs_tol` is useful when comparing values near zero.

Example:

```python
math.isclose(1e-12, 0.0)
# False

math.isclose(1e-12, 0.0, abs_tol=1e-9)
# True
```

For zero comparisons, a simple absolute tolerance is often clearer:

```python
abs(x) < 1e-9
```

#### 2. For exact decimal values: do not use `float`

If the value has exact decimal meaning — money, quantities, IDs, configuration values, etc. — use a decimal or integer representation instead.

```python
from decimal import Decimal

Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
# True
```

---

### Should you use `float` for prices?

No. For prices, do not use `float` for storage or financial calculations.

Use one of these instead:

## Option 1: `Decimal`

Best for general monetary calculations.

```python
from decimal import Decimal, ROUND_HALF_UP

price = Decimal("19.99")
quantity = Decimal("3")

total = (price * quantity).quantize(
    Decimal("0.01"),
    rounding=ROUND_HALF_UP
)

print(total)
# 59.97
```

Important: construct `Decimal` from strings, not floats.

```python
# Good
Decimal("19.99")

# Bad
Decimal(19.99)
```

`Decimal(19.99)` captures the inexact binary float value, which defeats the purpose.

## Option 2: integer minor units

For simple fixed-decimal currencies like USD or EUR, store cents as integers.

```python
price_cents = 1999
quantity = 3

total_cents = price_cents * quantity
# 5997
```

Display it as:

```python
def format_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"

format_cents(total_cents)
# "59.97"
```

This is simple and exact, but less flexible for currencies with different decimal places, exchange rates, or high-precision calculations.

---

### Practical recommendation

For a price system:

- Store prices as `Decimal` or integer minor units.
- Use `Decimal` for calculations involving taxes, discounts, exchange rates, or rounding.
- Use integer cents if your domain is simple and fixed to two decimal places.
- Avoid `float` for money.
- Choose rounding rules explicitly, for example `ROUND_HALF_UP` or `ROUND_HALF_EVEN`, depending on your business requirements.

In short:

```python
# Bad for money
price = 19.99
total = price * 3

# Better
from decimal import Decimal

price = Decimal("19.99")
total = price * Decimal("3")
```

`float` is fine for scientific, graphical, statistical, or engineering calculations where tiny rounding error is acceptable. It is the wrong tool for exact decimal business values like prices.

**Confidence: HIGH** — standard IEEE 754 floating-point behavior and well-established monetary arithmetic guidance.

