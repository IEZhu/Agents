**Short answer:** Python floats are IEEE 754 binary doubles, and they cannot represent 0.1, 0.2 or 0.3 exactly. The rounding errors don't cancel, so the sum lands on a slightly different double than the literal `0.3`. To compare floats, use a tolerance (`math.isclose`). For prices, **don't use float**. Use `decimal.Decimal` or integer minor units (cents).

## Why `0.1 + 0.2 != 0.3`

A binary fraction can only represent values of the form `k / 2^n` exactly. In base 2, 0.1 is a repeating fraction (`0.0001100110011…`), just as 1/3 is `0.333…` in decimal. Python stores the nearest 53-bit-mantissa approximation:

```python
>>> 0.1 + 0.2
0.30000000000000004
>>> from decimal import Decimal
>>> Decimal(0.1)      # the exact value actually stored
Decimal('0.1000000000000000055511151231257827021181583404541015625')
>>> Decimal(0.3)
Decimal('0.299999999999999988897769753748434595763683319091796875')
```

The stored 0.1 and 0.2 are each a little too large. Their sum rounds to the double just above 0.3, while the literal `0.3` rounds to the double just below it. `==` compares bits exactly, so you get `False`. This isn't a Python bug. C, Java, JavaScript and any other language using IEEE 754 doubles give the same result.

## Comparing floats correctly

Use `math.isclose` from the standard library:

```python
import math

math.isclose(0.1 + 0.2, 0.3)                 # True (default rel_tol=1e-09)
math.isclose(x, 0.0, abs_tol=1e-12)          # comparing against zero needs abs_tol
```

- **Relative tolerance** (`rel_tol`) scales with the size of the values, so it works across magnitudes.
- **Absolute tolerance** (`abs_tol`) is required near zero, because any relative tolerance of 0 is 0. With the default `abs_tol=0.0`, `math.isclose(1e-20, 0.0)` returns `False`.
- Choose the tolerances based on your domain's precision, not by habit.
- In tests, `pytest.approx` does the same job: `assert 0.1 + 0.2 == pytest.approx(0.3)`.
- For arrays, use `numpy.isclose` / `numpy.allclose`. Be aware that their defaults differ from `math.isclose`: they add an absolute tolerance and the formula is asymmetric.

Avoid a hand-rolled `abs(a - b) < 1e-9`. A fixed absolute epsilon is too loose for tiny values and meaningless for large ones.

## Prices: don't use float

With money you need **exact decimal arithmetic and controlled rounding**, which floats can't give you. Errors add up across sums, and tax and discount calculations, and you end up with totals like `19.999999999999996` and reconciliations that are off by a cent.

You have two sound options.

**1. `decimal.Decimal`** is the most natural fit in Python:

```python
from decimal import Decimal, ROUND_HALF_UP

price = Decimal("19.99")            # construct from str, never from float
qty = 3
subtotal = price * qty              # Decimal('59.97')
tax = (subtotal * Decimal("0.0825")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
```

- Always construct from a **string** (or an int). `Decimal(0.1)` faithfully copies the float's error, as shown above.
- Round explicitly with `quantize` at defined points (per line item or per invoice, whatever your business or tax rules say), and choose the rounding mode on purpose. Python's default context uses `ROUND_HALF_EVEN` (banker's rounding), which may not match what your accountants expect.

**2. Integer minor units**: store `1999` (cents) as an `int`. This is exact and fast, and it is common in payment APIs. The catch is that currencies differ in how many decimal places they use (e.g. JPY has 0, and some have 3), so store the currency alongside the amount and avoid hardcoding `/ 100`.

**Across the whole stack:**

- **Database:** use `NUMERIC(precision, scale)` / `DECIMAL`, not `FLOAT`/`REAL`/`DOUBLE`. Most Python drivers (e.g. psycopg for PostgreSQL) map `NUMERIC` to `Decimal`, but check that yours does.
- **JSON / APIs:** JSON numbers are often parsed as floats on the other side (JavaScript always does this). Send money as a string (`"19.99"`) or as integer minor units, and parse with `json.loads(..., parse_float=Decimal)` if you must accept numeric literals.
- **Pydantic / ORMs:** declare fields as `Decimal` (SQLAlchemy `Numeric(asdecimal=True)`, Pydantic `condecimal` / `Decimal` with constraints) so a float never slips in at the boundary.
- **Pair amounts with currency.** A small value object such as `Money(amount: Decimal, currency: str)` stops you from adding USD to EUR and centralizes the rounding rules.

## When float is fine

Floats are the right tool for measurements, scientific and statistical computation, ML, graphics and similar work. In those fields the inputs are already approximate and speed matters. Just compare floats with a tolerance and never use them where values must be exact to the cent.

**Confidence: HIGH.** IEEE 754 behavior, `math.isclose` semantics and the `Decimal` guidance are well established. I didn't run the snippets in this session, so check the exact `Decimal(0.1)` expansion locally if it matters to you.
