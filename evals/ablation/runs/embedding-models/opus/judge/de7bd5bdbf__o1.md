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
**Short answer:** Python floats are IEEE 754 binary64 numbers, and in binary neither 0.1 nor 0.2 nor 0.3 can be stored exactly. Compare floats with a tolerance (`math.isclose`). Don't use float for prices. Use `decimal.Decimal`, or store integer minor units such as cents.

## Why `0.1 + 0.2 != 0.3`

A binary float can only represent fractions of the form k/2ⁿ exactly. 1/10 has no finite binary expansion, in the same way 1/3 has no finite decimal one: 0.1 in binary is 0.000110011001100…, repeating forever. Python stores the nearest representable double:

```python
>>> from decimal import Decimal
>>> Decimal(0.1)   # the exact value actually stored
Decimal('0.1000000000000000055511151231257827021181583404541015625')
>>> 0.1 + 0.2
0.30000000000000004
>>> 0.3
0.3                 # also inexact, just a different nearest double
```

Adding the two rounded inputs, then rounding the result again, lands on a different double from the one closest to 0.3. They differ by one ULP, so `==` returns `False`. This is not a Python bug. You get the same result in JavaScript, Java, C and any other language that uses IEEE 754 doubles.

## Comparing floats correctly

```python
import math

math.isclose(0.1 + 0.2, 0.3)                  # True
math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
```

- **Relative tolerance** (`rel_tol`) scales with the size of the values. Use it for general-purpose comparisons.
- **Absolute tolerance** (`abs_tol`) is needed when comparing against or near **zero**. A relative tolerance alone never treats `1e-17` as close to `0.0`. I recall the defaults as `rel_tol=1e-09` and `abs_tol=0.0`, but I haven't checked them this turn, so confirm in the `math` docs.
- For arrays, use `numpy.isclose` / `numpy.allclose`. Note that their defaults differ from `math.isclose`, and `numpy.isclose` is asymmetric in `a` and `b`.
- In tests, use `pytest.approx`: `assert 0.1 + 0.2 == pytest.approx(0.3)`.

Avoid `abs(a - b) < 1e-9` with a hard-coded epsilon. It is too loose for tiny values and meaningless for large ones, because at 1e17 adjacent doubles are already more than 1e-9 apart.

Choose the tolerance from your domain, meaning how much error your computation can legitimately build up. Don't copy a magic constant.

## Prices: don't use float

Money needs **exact decimal arithmetic and explicit, auditable rounding**. Floats give you neither. The errors are small, but they add up across sums and reports, and a total of `19.999999999999996` that gets truncated instead of rounded is a real bug. Two sound options:

### Option 1: `decimal.Decimal` (most readable)

```python
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")

price = Decimal("19.99")          # construct from str, never from float
qty = 3
subtotal = price * qty            # Decimal('59.97'), exact
tax = (subtotal * Decimal("0.0825")).quantize(CENT, rounding=ROUND_HALF_UP)
```

Pitfalls:
- `Decimal(0.1)` copies the float's binary error into the Decimal. Always pass a `str` (or an int).
- Python's default rounding mode is `ROUND_HALF_EVEN` (banker's rounding). That may or may not match your business or tax rules, so pass `rounding=` explicitly wherever you round.
- Round at defined points (per line item vs. per invoice) and write down which you chose. That decision changes totals.

### Option 2: integer minor units (simplest, fastest)

Store `1999` (cents) as an `int` and format it only for display. Integer arithmetic is exact. This works well when every price has the same fixed precision. It gets awkward with currencies that have 0 or 3 minor digits (JPY, KWD), or with sub-cent unit prices such as fuel or API metering. In those cases, store the scale alongside the amount or use Decimal.

### At the boundaries

- **Database:** use `NUMERIC(p, s)` / `DECIMAL(p, s)` (e.g. `NUMERIC(12, 2)`), or `BIGINT` for minor units. Never `FLOAT`/`REAL`/`DOUBLE`. Most Python drivers (psycopg, SQLAlchemy `Numeric`) hand `NUMERIC` back as `Decimal`.
- **JSON:** `json.loads` turns `19.99` into a float by default. Use `json.loads(s, parse_float=Decimal)`, or send amounts as strings or integer cents in your API contract.
- **Currency:** if you handle more than one currency, keep the amount and the currency code together in a small value object. A bare `Decimal` doesn't stop you adding USD to EUR.

**My recommendation:** use `Decimal` in application code, `NUMERIC` in the database, and strings or integer cents in your APIs, with a rounding policy you set explicitly. Keep float for measurements and scientific values, where an approximation is the point.


## Answer B
**Short answer:** Python floats are IEEE 754 binary doubles, and binary can't represent 0.1, 0.2 or 0.3 exactly. The rounding errors in `0.1 + 0.2` don't land on the same double as the literal `0.3`. To compare floats, use a tolerance (`math.isclose`). **Don't use float for prices.** Use `decimal.Decimal` or integer minor units such as cents.

## Why `0.1 + 0.2 != 0.3`

A double stores a number as a binary fraction (mantissa × 2^exponent). Values like 1/2, 1/4 and 3/8 are exact in binary. 1/10 is not: in base 2 it repeats forever (0.0001100110011…₂), the same way 1/3 does in base 10. So each literal gets rounded to the nearest representable double:

```python
>>> from decimal import Decimal
>>> Decimal(0.1)   # the exact value of the double behind 0.1
Decimal('0.1000000000000000055511151231257827021181583404541015625')
>>> 0.1 + 0.2
0.30000000000000004
>>> 0.3
0.3
```

`0.1` and `0.2` are each slightly above their decimal values. Their sum rounds to the double just *above* the one nearest to 0.3, so `==`, which compares bit patterns exactly, returns `False`. This isn't specific to Python. JavaScript, Java, C and Go behave the same way with doubles.

## Comparing floats correctly

Use `math.isclose`, which applies a relative tolerance and optionally an absolute one:

```python
import math

math.isclose(0.1 + 0.2, 0.3)                 # True  (default rel_tol=1e-09)
math.isclose(1e-12, 0.0)                     # False: relative tolerance is useless near zero
math.isclose(1e-12, 0.0, abs_tol=1e-9)       # True
```

Guidelines:
- **Relative tolerance** (`rel_tol`) scales with the size of the numbers, so it's the right default for general values.
- **Absolute tolerance** (`abs_tol`) is needed whenever one side can be zero or very close to it.
- Pick tolerances from your domain, e.g. what measurement precision actually matters. Don't copy them blindly.
- For arrays, `numpy.isclose` / `numpy.allclose` work the same way. Note that NumPy's defaults differ (it applies a non-zero absolute tolerance by default), so check its docs before relying on the defaults.
- In tests, `pytest.approx(0.3) == 0.1 + 0.2` reads well.
- Avoid `abs(a - b) < 1e-9` as a universal rule. A fixed epsilon is too loose for tiny numbers and too strict for huge ones.

## Prices: don't use float

Money needs exact decimal arithmetic and well-defined rounding. Floats give neither. Errors build up across sums, totals stop matching line items by a cent, and `==` checks on balances fail. You have two solid options:

**1. `decimal.Decimal`**: exact base-10 arithmetic with explicit rounding control.

```python
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")

price = Decimal("19.99")          # always construct from str, never from float
qty = 3
tax_rate = Decimal("0.0825")

subtotal = price * qty                                      # Decimal('59.97')
tax = (subtotal * tax_rate).quantize(CENT, rounding=ROUND_HALF_UP)
total = subtotal + tax

assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")    # True
```

Pitfalls:
- `Decimal(0.1)` copies the float's error (see the output above). Always pass strings, or ints.
- Choose one rounding policy (`ROUND_HALF_UP`, or banker's `ROUND_HALF_EVEN`, which is Decimal's default context rounding), document it, and round at defined points such as per line item vs. per invoice. In practice this is a business and accounting decision, not just a technical one.
- Don't mix `Decimal` and `float` in arithmetic. Python raises `TypeError`, which is a good thing, so don't work around it with `float()` casts.

**2. Integer minor units** (e.g. `1999` cents): fast, exact, and portable across languages and APIs. Payment processors commonly use this representation. Watch out for currencies with 0 or 3 decimal places (e.g. JPY, KWD). Store the currency's exponent alongside the amount, or use a money library that knows it.

**Persistence:** use the database's exact type, e.g. `NUMERIC(19, 4)` / `DECIMAL` in PostgreSQL or MySQL, or a `BIGINT` of minor units. Never use `REAL`/`FLOAT`/`DOUBLE PRECISION`. Most drivers (e.g. psycopg) map `NUMERIC` to `Decimal` automatically. Confirm that for your driver and ORM. In JSON, send amounts as strings (`"19.99"`) or integer minor units. JSON numbers are often parsed as doubles on the other side, which brings the problem back.

**Where float is fine:** analytics, ML features, or rough aggregates where a ~1e-15 relative error doesn't matter. It doesn't belong in ledgers, invoices, balances, or anything that has to reconcile.

**Recommendation:** if you're starting fresh in Python, use `Decimal` in the domain layer and `NUMERIC` in the database. If you integrate heavily with payment APIs or multiple languages, use integer minor units instead. Whichever you pick, wrap it in a small `Money` value object (amount + currency) so raw floats can't creep back in.

I didn't run the snippets above in this session. The `Decimal(0.1)` expansion and the `0.30000000000000004` result are standard, well-documented IEEE 754 behavior, but run them in your interpreter if you want to check them yourself.

