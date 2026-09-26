**Short answer: no.** Adding 30 more static examples is probably the weakest way to use 900 verified pairs. Retrieve the relevant examples for each question instead, and build an eval set first so you can measure whether the change helped.

## Why a bigger static block is the wrong lever

- **Coverage doesn't scale.** "Q1 but not Q2" needs an anti-join example. "Week vs week before" needs a period-comparison example. Thirty hand-picked hard cases will cover some patterns and miss others, and the next odd question will fail the same way.
- **Every request pays for all of it.** Roughly 36 examples at 100–200 tokens each adds several thousand tokens to every call, including "how many orders yesterday". That's a rough estimate; count it on your own examples.
- **Few-shot bias cuts both ways.** Examples shape output more than instructions do. A block full of complex CTEs can push simple questions toward over-engineered SQL, so you could lose accuracy where you have it now.
- **You'd be guessing.** Without a held-out test set you can't tell whether 30 examples beat 6, or whether they broke the simple lookups.

## What I'd do instead

### 1. Carve out an eval set first
- Hold out about 150–200 of the 900 pairs, stratified by type: simple lookup, aggregation, multi-join, anti-join, period comparison, window functions.
- Score on **execution accuracy**: run the generated SQL and the gold SQL, then compare result sets. Don't compare SQL strings, because many different queries are correct.
- Report accuracy per category, not just one overall number. Your problem is concentrated in a few categories, and a single average will hide it.

### 2. Dynamic few-shot retrieval (the main fix)
- Embed the questions of the remaining ~700 pairs. At query time, retrieve the top 4–8 most similar pairs and put those in the prompt.
- Keep 2–3 static examples of the basics as a floor.
- Refinement: before embedding, mask literals ("Texas" → `<state>`, "March" → `<month>`). Then retrieval matches on the *shape* of the question ("X in period A but not period B") instead of its surface words. Published text-to-SQL work (DAIL-SQL, from memory, not verified here) found that selecting examples by question/SQL-structure similarity beats random or fixed examples. Your eval set will tell you whether it holds on your data.
- Guard against leakage: when you evaluate, make sure a test question can't retrieve its own pair.

### 3. Put the missing semantics in the schema, not in examples
Many "wrong join" errors come from the model not knowing your business definitions. Annotate the schema directly:
- Explicit join paths: `order_items.order_id → orders.id`, `shipments.order_id → orders.id`, `shipments.warehouse_id → warehouses.id`. Use your real keys; these names are illustrative.
- Definitions, for example "a shipment is **late** when `delivered_at > promised_at`, or when it's undelivered and `now() > promised_at`". If the model has to guess what "late" means, no amount of examples will fix it reliably.
- Time conventions: which timezone "yesterday" means, and whether weeks start on Monday (`date_trunc('week', …)` in Postgres uses ISO weeks, which start Monday).

### 4. A short list of pattern rules for known traps
Keep this to 5–8 rules, not 50:
```
- "in A but not B" → NOT EXISTS subquery. Never NOT IN (NULLs break it) and never a plain INNER JOIN.
- Counting orders while joining order_items → count(DISTINCT o.id) or aggregate items in a subquery first (fan-out).
- "compared to previous period" → aggregate each period in a CTE (or FILTER (WHERE …)), then join/compare the CTEs.
- Date filters: use half-open ranges (created_at >= X AND created_at < Y), not created_at::date = …
```
That last rule applies to your own launch example. `created_at::date = current_date - 1` works, but it can't use a plain index on `created_at`, and it follows the session timezone. Change that example too, or the model will keep copying the pattern.

### 5. Optional: plan, then write SQL, then self-check
For questions that retrieval flags as complex (for example, when the nearest examples contain CTEs or anti-joins), ask the model to list the tables, join keys, filters and grain before it writes the SQL. Then run `EXPLAIN` on the output, and on an error send the message back once for a repair. That catches syntax and column-name errors cheaply. It won't catch wrong semantics; only the eval set does that.

## Order of operations
1. Build the eval set and measure the current prompt, per category.
2. Add schema annotations and pattern rules, and re-measure. This is cheap and may fix a surprising amount.
3. Add dynamic retrieval and re-measure.
4. Only then consider plan-then-SQL for what's still failing.

Change one thing at a time so you know which change moved which category.

## Caveats
- Retrieval quality depends on how varied the 900 pairs are. If they're 70% simple lookups, the hard categories may have too few neighbours. Check the category counts; you may need to write 10–20 extra hard examples. That's the one place your "add hard ones" instinct is right, but they should go into the **retrieval pool**, not the static prompt.
- Security note, since this is ops-facing: run the generated SQL as a read-only role with `statement_timeout` set. Don't rely on the prompt to prevent writes or runaway queries.
