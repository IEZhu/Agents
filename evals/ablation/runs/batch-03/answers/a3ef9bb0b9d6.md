**The cost is almost certainly not the string cleaning. It's `lookup_customer`, which makes one `SELECT` round-trip to Postgres for each of the 1.2M rows.** 50 min ÷ 1.2M rows ≈ 2.5 ms per row. That matches a network round-trip plus a query, not a `.strip().lower()` call. Vectorizing the other three lines won't help much, and PyPy can't speed up time spent waiting on the database. Fix the lookup first. That alone will probably get you well under 15 minutes, and you may not need PyPy.

Check this before rewriting anything:

```python
import cProfile, pstats
cProfile.run("main()", "etl.prof")   # wrap your loop in main()
pstats.Stats("etl.prof").sort_stats("cumulative").print_stats(15)
```

If `lookup_customer` / `cursor.execute` accounts for most of the cumulative time, that confirms it. Also check that `customers.email` has an index (`\d customers`). Without one, each lookup is a full sequential scan.

---

## Option 1 (recommended): do the join in Postgres

Send the raw rows to a staging table with COPY, then resolve `customer_id` with one set-based join. Python only does the light cleaning.

```python
import io
import pandas as pd
import psycopg2

conn = psycopg2.connect(DSN)
cur = conn.cursor()

# dtype=str + keep_default_na=False reproduces csv.DictReader: every field is a string, "" stays ""
df = pd.read_csv("export.csv", dtype=str, keep_default_na=False)

df["email"]   = df["email"].str.strip().str.lower()
df["amount"]  = pd.to_numeric(df["amount"].replace("", "0")).round(2)   # same as float(x or 0); bad values still raise
df["country"] = df["country"].map(COUNTRY_MAP).fillna(df["country"])    # same as COUNTRY_MAP.get(x, x)

buf = io.StringIO()
df[["email", "amount", "country", "ts"]].to_csv(buf, index=False, header=False)
buf.seek(0)

# Staging table with the same column types as sales, dropped at commit
cur.execute("CREATE TEMP TABLE sales_stage (LIKE sales) ON COMMIT DROP")
cur.copy_expert("COPY sales_stage (email, amount, country, ts) FROM STDIN WITH CSV", buf)
cur.execute("""
    INSERT INTO sales (customer_id, email, amount, country, ts)
    SELECT c.id, s.email, s.amount, s.country, s.ts
    FROM sales_stage s
    LEFT JOIN customers c ON c.email = s.email
""")
conn.commit()
```

`LIKE sales` copies the column types, so `ts` and `amount` are parsed exactly as your current COPY into `sales` parses them. Adjust if `sales` has NOT NULL or other constraints that the staging rows (with `customer_id` still NULL) would violate. `LIKE` without `INCLUDING CONSTRAINTS` copies NOT NULL but not CHECK constraints. If `customer_id` is NOT NULL, create the staging table with explicit columns instead.

## Option 2: preload a dict (smallest change)

If you'd rather keep the join in Python, run one query instead of 1.2M:

```python
cur.execute("SELECT email, id FROM customers")
email_to_id = dict(cur.fetchall())

df["customer_id"] = df["email"].map(email_to_id).astype("Int64")
cols = ["customer_id", "email", "amount", "country", "ts"]
buf = io.StringIO()
df[cols].to_csv(buf, index=False, header=False)
buf.seek(0)
cur.copy_expert("COPY sales (customer_id, email, amount, country, ts) FROM STDIN WITH CSV", buf)
conn.commit()
```

Memory scales with the size of `customers`. Tens of millions of rows start to hurt, and Option 1 has no such limit.

---

## Things to watch in the vectorized version

- **`customer_id` dtype.** Once there's a single miss, a plain `.map()` produces float64, so `to_csv` writes `1234.0` and COPY into an integer column fails. Keep the `.astype("Int64")`: the nullable integer writes misses as empty fields, which COPY CSV reads as NULL, the same as your current `None`.
- **Keep everything as strings.** Without `dtype=str, keep_default_na=False`, pandas turns `""`/`"NA"`/`"null"` into NaN and may parse ZIP-like or ID-like columns as numbers. That silently changes the output.
- **Duplicate emails in `customers`.** Today `fetchone()` picks one row arbitrarily. A SQL `LEFT JOIN` would **duplicate the sales row**, and `dict()` keeps the last one. Check `SELECT email, count(*) FROM customers GROUP BY 1 HAVING count(*) > 1`. If that returns rows, dedupe them (e.g. `DISTINCT ON (email)`) or add a unique constraint.
- **Case and collation.** You lowercase the export side only. If `customers.email` stores mixed case, both the old and new code miss those rows. Behaviour is unchanged, but it may be a latent bug worth fixing (`lower(c.email)` with an expression index, or `citext`).
- **Rounding.** Python `round()` and pandas `.round()` both use round-half-to-even on binary floats, so results should match. If `amount` is `numeric` in Postgres, consider letting Postgres do the rounding (`round(s.amount, 2)` in the INSERT) to avoid float artifacts.
- **Validate before cutover.** Run old and new code on the same export and diff the outputs: row count, NULL `customer_id` count, and a checksum over the sorted CSV.

---

## PyPy: probably skip it

The plan has a built-in conflict: **PyPy is fast for pure-Python loops, but slow for C-extension-heavy code like pandas/numpy.** From memory (I haven't verified this against current releases, so check before committing):

- **psycopg2** is a CPython C extension. Under PyPy it either won't build or runs through the cpyext compatibility layer with significant overhead. The usual PyPy options are `psycopg2cffi` (a drop-in, but check how actively it's maintained) or **psycopg 3** in its pure-Python mode.
- **pandas/numpy** also run through cpyext on PyPy. They work, but often slower than on CPython, and wheel availability varies.
- So pick one approach: either **vectorize on CPython** (recommended), or keep a plain-Python row loop (with the dict lookup) on PyPy. Doing both combines the downsides.

After removing the per-row queries, I'd expect the transform to take seconds to a small number of minutes on CPython. That's an estimate, so profile it. The remaining time will likely be CSV parsing and the COPY/INSERT, and PyPy doesn't help with either.

**Priority:** 1) confirm with the profiler, 2) replace the per-row lookup (Option 1), 3) vectorize the cleaning on CPython, 4) consider PyPy only if you're still over budget, which is unlikely.
