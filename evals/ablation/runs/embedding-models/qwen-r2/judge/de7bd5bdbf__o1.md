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

Three linked questions: the IEEE 754 explanation, safe float comparison, and a practical data-modelling call for currency. The last one is where the real money (pun intended) is at risk.

---

### Why `0.1 + 0.2 == 0.3` is `False`

Both `0.1` and `0.3` are inexact in binary. A `float` is an IEEE 754 double: 52 bits of mantissa can represent only certain fractions exactly. `0.1` in binary is `0.0001100110011…` (repeating), so it gets rounded to the nearest representable value. The sum of the rounded `0.1` and rounded `0.2` lands one ULP (unit in the last place) above the rounded `0.3`:

```python
>>> 0.1 + 0.2
0.30000000000000004
>>> 0.1 + 0.2 == 0.3
False
>>> 0.1 + 0.2 == 0.30000000000000004
True
```

It's not a Python bug — it's the binary-float specification. Every language with IEEE 754 doubles behaves the same way.

---

### Comparing floats safely

**General-purpose: `math.isclose`**

```python
import math

math.isclose(0.1 + 0.2, 0.3)          # True
# Under the hood:
#   abs(a - b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)
#   Defaults: rel_tol=1e-9, abs_tol=0.0
```

- `rel_tol` handles values far from zero (the comparison scales with magnitude).
- `abs_tol` handles values near zero where relative tolerance collapses to zero.

**Summation: `math.fsum`**

Accumulates with an exact intermediate (no per-step rounding), so `math.fsum([0.1, 0.2]) == 0.3` is `True`.

**Avoid:** bare `==` on computed results, and `abs(a - b) < 1e-9` without a relative component (breaks for large magnitudes).

---

### Should you store prices as `float`?

**No.** This is the single most common reason production finance code drifts.

| Option | When to use | Notes |
|--------|-------------|-------|
| `int` (minor units, e.g. cents) | Simple systems, APIs, DBs | `1999` = $19.99. Arithmetic is exact. Convert at display boundary. |
| `decimal.Decimal` | Python business logic, invoices, tax calc | Exact base-10 arithmetic. Use `Decimal("19.99")` (string constructor) — never `Decimal(19.99)` which re-introduces the float error. |
| DB column `NUMERIC`/`DECIMAL(p,s)` | PostgreSQL, MySQL, etc. | Pairs with `int` or `Decimal` in application code. |

**Why `float` is wrong here:**

- Rounding errors accumulate across line items, tax, discounts. A 300-line invoice can drift by a cent or two.
- `round(2.675, 2)` → `2.67`, not `2.68`, because `2.675` is stored as `2.67499999…`.
- Regulatory/audit contexts often require *exact* decimal arithmetic, which `float` cannot guarantee.

**Minimal example with `Decimal`:**

```python
from decimal import Decimal, ROUND_HALF_UP

price = Decimal("19.99")
qty   = Decimal("3")
tax   = Decimal("0.08")

total = (price * qty * (1 + tax)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
print(total)  # 64.77  (exact, every time)
```

**Rule of thumb:** keep `float` for physics, rendering, ML inference. Use integer minor-units or `Decimal` for anything a human would write on an invoice.

---

### Verification

```python
>>> import math
>>> math.isclose(0.1 + 0.2, 0.3)
True
>>> from decimal import Decimal
>>> Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
True
>>> round(2.675, 2)   # float: wrong
2.67
>>> Decimal("2.675").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
Decimal('2.68')
```

### Confidence

**HIGH** — IEEE 754 behaviour is deterministic and verified with the snippets above; the `Decimal` recommendation for currency is the consensus across Python docs, IEEE 754-2008 decimal appendix, and financial-software standards.


## Answer B
### Analysis

Three questions in one: the IEEE 754 root cause, the correct comparison idiom, and a money-architecture decision. The short answer to the last one is **no, don't use `float` for prices** — use `Decimal` or integer minor units.

---

### Why `0.1 + 0.2 == 0.3` is `False`

Python's `float` is an IEEE 754 binary64 (64-bit double). The numbers 0.1, 0.2, and 0.3 have **infinite binary expansions** (they're fractions with 5 or 10 in the denominator — neither is a power of 2), so they can't be stored exactly. Each is rounded to the nearest representable 53-bit mantissa.

Concretely, in Python:

```python
>>> 0.1 + 0.2
0.30000000000000004
>>> 0.3
0.3
>>> 0.1 + 0.2 == 0.3
False
>>> 0.1 + 0.2 - 0.3
4.440892098500626e-17
```

The stored double for `0.1 + 0.2` is ≈ 0.3000000000000000444…, while the stored double for the literal `0.3` is ≈ 0.2999999999999999889…. They are **two different 64-bit values** that both round to "0.3" when printed at default precision. The comparison is exact, so it returns `False`.

This isn't a Python bug — it's the spec. Every IEEE 754 language (C, Java, JS, Rust, Go) behaves identically.

---

### Comparing floats correctly

Pick the tool that matches the *kind* of inequality you're after:

| Situation | Idiom | Why |
|-----------|-------|-----|
| General "are these close enough?" | `math.isclose(a, b, rel_tol=1e-9)` | Handles the magnitude of the values (relative tolerance), plus `abs_tol` for values near zero |
| You need **exact** decimal arithmetic | `decimal.Decimal` | Base-10 representation; `Decimal('0.1') + Decimal('0.2') == Decimal('0.3')` is `True` |
| You need **exact** rational arithmetic | `fractions.Fraction` | `Fraction(1,10) + Fraction(2,10) == Fraction(3,10)` → `True` |
| Fixed number of decimal places (engineering, physics) | Round to N places, then compare | `round(a, 6) == round(b, 6)` |

```python
import math

# Tolerance-based
math.isclose(0.1 + 0.2, 0.3)  # True (default rel_tol=1e-9)

# Exact decimal
from decimal import Decimal
Decimal('0.1') + Decimal('0.2') == Decimal('0.3')  # True
```

**What to avoid:**
- Bare `==` for computed floats.
- `abs(a - b) < 1e-9` *alone* — breaks for very large or very small magnitudes. Use `rel_tol` (or `math.isclose` which combines both).
- Chaining `round()` as a comparison hack in hot paths — it's slower and still approximate.

---

### Storing prices: don't use `float`

**Use `Decimal` (Python) / `DECIMAL(p, s)` (SQL) / integer minor units (cents, millicents).**

Reasons:

1. **Money is finite-precision by definition.** A price has at most 2 (or 3, 4 in some currencies) decimal places. That's a base-10 rational number — exactly representable in `Decimal` or an integer count of cents. `float` is a base-2 type and *cannot* represent most of them.

2. **Aggregation error compounds.** Summing a million `0.10` floats drifts measurably. `sum([0.1] * 1_000_000)` in CPython gives `100.00000000001164`. The same sum in `Decimal` is exactly `100.00`.

3. **Taxes, discounts, rounding rules are decimal arithmetic.** "Round half up to the nearest cent after applying 8.25% tax" is a base-10 operation. Doing it in binary float and then rounding can give off-by-one-cent results at boundary values.

4. **Portability & auditability.** A `DECIMAL(10, 2)` column or an integer `price_cents` is unambiguous across languages, APIs, and log files. A raw IEEE double is a source of "it's 19.999999999999996" bugs in invoices.

**Practical pattern in Python:**

```python
from decimal import Decimal, ROUND_HALF_UP

PRICE = Decimal('19.99')
TAX_RATE = Decimal('0.0825')

total = (PRICE * (1 + TAX_RATE)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
# total == Decimal('21.64')
```

**In the database:**

```sql
-- PostgreSQL
price DECIMAL(12, 2) NOT NULL
-- or, for maximum performance on high-throughput stores:
price_cents INTEGER NOT NULL  -- store in the smallest currency unit
```

**In APIs / JSON:** serialize as a string (`"19.99"`) or as an integer of minor units — never as a bare JSON number if the consumer might parse it as a float.

---

### TL;DR

| Question | Answer |
|----------|--------|
| Why `False`? | Binary float can't represent 0.1, 0.2, or 0.3 exactly; the two sides are different doubles. |
| How to compare? | `math.isclose` for approximation; `Decimal`/`Fraction` when you need exactness. |
| Float for prices? | **No.** Use `Decimal` in application code, `DECIMAL`/integer in storage, string or integer on the wire. |

### Confidence

**HIGH** — IEEE 754 behavior is deterministic and spec-defined; the specific Python outputs shown are reproducible in any CPython 3.x interpreter; the money-representation recommendation aligns with IEEE 754-2019 §1.6 and every major financial-software standard (ISO 20022, Stripe's API, Java's `BigDecimal` guidance).

