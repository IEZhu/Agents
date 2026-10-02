**Short answer:** `0.1`, `0.2` and `0.3` cannot be stored exactly in binary floating point, so the rounding errors in `0.1 + 0.2` do not land on the same double as `0.3`. To compare floats, use a tolerance (`math.isclose`). For prices, **don't use `float`**. Use `decimal.Decimal` or integer minor units (cents).

### Why `0.1 + 0.2 != 0.3`

Python's `float` is an IEEE 754 double: a 53-bit binary fraction. A value like 1/10 has no finite binary representation, much as 1/3 has none in decimal, so each literal is rounded to the nearest representable double:

```python
>>> 0.1 + 0.2
0.30000000000000004
>>> 0.3
0.3
>>> (0.1).as_integer_ratio()
(3602879701896397, 36028797018963968)   # not exactly 1/10
```

The stored value of `0.1` is slightly above 0.1, and `0.2` is too. Their sum rounds to the double just above the one nearest `0.3`. This is not a Python bug. You get the same result in C, Java and JavaScript, or in any language that uses IEEE 754 doubles.

### How to compare floats

Use a tolerance-based comparison instead of `==`:

```python
import math

math.isclose(0.1 + 0.2, 0.3)                  # True (default rel_tol=1e-09)
math.isclose(x, 0.0, abs_tol=1e-12)           # comparing near zero needs abs_tol
```

Some guidance:

- **Relative tolerance** (`rel_tol`) scales with the size of the values. It is the right default for most comparisons.
- **Absolute tolerance** (`abs_tol`) is required when one side may be `0.0`. A relative tolerance alone never matches zero unless the other value is exactly zero.
- Choose tolerances from your domain, such as sensor precision or how much error your algorithm accumulates. Don't treat the defaults as universally correct.
- In tests, `pytest.approx(0.3) == 0.1 + 0.2` does the same thing. For arrays, use `numpy.isclose` / `numpy.allclose`. Note that NumPy's default tolerances differ from `math.isclose` (as I recall `rtol=1e-05, atol=1e-08`; please check the docs).
- Avoid hand-rolled `abs(a - b) < 1e-9`. It is an absolute check only, so it fails for both very large and very small magnitudes.

### Prices: don't use `float`

Money is defined in decimal units and has legal or contractual rounding rules. Float errors are small, but they still cause real problems:

- Totals drift. Summing many line items can end up one cent off.
- Rounding becomes unpredictable. For example, `round(2.675, 2)` returns `2.67`, because the stored value is actually 2.67499999…
- Equality checks such as "is the invoice paid in full?" become unreliable.

You have two sound options.

**1. `decimal.Decimal`.** This is the most natural fit in Python:

```python
from decimal import Decimal, ROUND_HALF_UP

price = Decimal("19.99")          # construct from str, NOT from float
qty = 3
total = (price * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
```

- Always build a `Decimal` from a string or an int. `Decimal(0.1)` faithfully copies the float's binary error (`0.1000000000000000055511151231257827…`).
- Decide your rounding mode on purpose. `Decimal`'s default context uses `ROUND_HALF_EVEN` (banker's rounding). Your business or tax rules may require `ROUND_HALF_UP`.
- Round at defined points, such as per line item or per invoice, as your accounting rules specify. Don't round after every operation.

**2. Integer minor units** (`1999` cents). This is simple, fast and exact for addition and subtraction. It is a common choice for payment APIs and high-volume systems. The drawbacks: you have to handle currencies with different numbers of decimal places (JPY has 0, BHD and KWD have 3). Division and percentages such as tax and discounts still need an explicit rounding policy.

**Across the whole stack:**

- **Database:** use `NUMERIC(p, s)` / `DECIMAL(p, s)` (for example `NUMERIC(12, 2)`), or an integer column for minor units. Never use `REAL` or `DOUBLE PRECISION`. Most drivers map `NUMERIC` to `Decimal` (psycopg does for PostgreSQL, and SQLAlchemy's `Numeric` returns `Decimal` by default). Check your specific driver and ORM.
- **JSON/API boundaries:** standard `json` parses `19.99` into a float. Send prices as strings (`"19.99"`) or integer cents, or parse with `json.loads(s, parse_float=Decimal)`.
- **Store the currency** next to the amount. An amount without a currency is a bug waiting to happen.

### Recommendation

If you already store prices as floats anywhere, plan a migration to `NUMERIC` or integer cents. Treat it as a schema change: back up the data, convert with explicit rounding, and check the totals against the old values before you switch.

**Confidence: HIGH** on the explanation and the recommendation, because these are established IEEE 754 and `decimal` behavior. The snippets were not executed here, so run them once in your environment. The NumPy default tolerances and the driver mappings are from memory and need confirming.
