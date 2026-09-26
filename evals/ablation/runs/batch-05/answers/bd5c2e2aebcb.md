No. Setting `statement_timeout = 0` would only remove the one safeguard that stopped this. The real problem is that step 3 as I wrote it was a single 18M-row transaction. That was my mistake, and you should batch it instead.

## What actually happened last night

- **Nothing was saved.** A single `UPDATE` is one transaction, so the cancel rolled back all of it. You are at 0%, not 60%. You can confirm this with `SELECT count(*) FROM users WHERE normalized_email IS NOT NULL;`. The only non-null values should be rows your step 2 code wrote since yesterday.
- **You now have leftover bloat.** Every row version the UPDATE wrote before the cancel is a dead tuple, possibly around 10M. Check `SELECT n_dead_tup, last_autovacuum FROM pg_stat_user_tables WHERE relname = 'users';`. If autovacuum hasn't cleaned it up yet, run a plain `VACUUM (VERBOSE) users;` before you do anything else. A plain VACUUM is non-blocking. Don't use `VACUUM FULL`.
- About the "60%": as far as I recall, Postgres has no `pg_stat_progress_*` view for `UPDATE` (I haven't verified this for PG15). So whatever estimate you saw was indirect. It doesn't matter anyway, because nothing was committed.

## Why an unlimited-timeout rerun is worse than a timeout

If you rerun the same statement with no timeout, one transaction runs for about an hour and causes these problems:

- **Row locks on every updated row until it commits.** Any app write to a user row the UPDATE has already touched will wait. With your step 2 callback, profile edits count. So does anything like Devise `trackable` updating `last_sign_in_at` on login. If you use it, that means logins stall or time out.
- **The xmin horizon is held back** for the whole run. Autovacuum can't clean dead tuples anywhere in the database, so bloat builds up beyond `users`.
- **A big WAL burst.** Expect replica lag, and the table roughly doubles in size (18M new row versions) before vacuum can reclaim the space.
- **No safe way to stop.** If anything forces a cancel at minute 55, you lose all of it again.

## Do this instead: a batched, resumable backfill

Run the backfill as many small transactions, each committed on its own. Every batch is short, row locks last milliseconds, vacuum keeps up, and you can kill it and restart at any point without losing work.

A Rails version, run from a console or rake task on one box:

```ruby
User.where(normalized_email: nil).in_batches(of: 10_000) do |batch|
  batch.update_all("normalized_email = lower(trim(email))")
  sleep 0.1 # throttle; tune while watching replica lag and DB CPU
end
```

- Each `update_all` is its own autocommitted statement, so the 45-minute timeout never comes into play.
- `in_batches` walks the primary key, so each batch is an index range scan and not a full-table scan.
- Don't wrap it in a DO block or procedure run from psql. `statement_timeout` applies to the whole top-level statement, so you'd hit the same wall.
- Start with 10k-row batches, watch replica lag (`pg_stat_replication`) and batch latency, and change the batch size or sleep from there. I can't tell you how long it will take for your hardware. Time the first ~50 batches and extrapolate.
- Because it's throttled, you don't have to wait for tonight. You can run it at a low rate during the day.

## Gates before step 4 (the unique index)

Don't let step 4 run automatically "right after". Make it conditional on these checks:

1. **No NULLs left:**
   `SELECT count(*) FROM users WHERE normalized_email IS NULL;` should return 0. If `email` itself can be NULL, decide what to do with those rows now, or the NOT NULL step in step 5 will fail.
2. **No duplicates.** This is the one most likely to bite. If your current uniqueness on `email` is case-sensitive, `Foo@x.com` and `foo@x.com` can both exist, and they collide after normalization:
   ```sql
   SELECT normalized_email, count(*)
   FROM users GROUP BY 1 HAVING count(*) > 1 LIMIT 50;
   ```
   If this returns rows, you need a product decision (merge the accounts, pick a winner, or contact the users) before the index can exist. If `CREATE UNIQUE INDEX CONCURRENTLY` hits a duplicate, it fails and leaves an `INVALID` index behind. You'd have to `DROP INDEX CONCURRENTLY` it before retrying. Also remember that `CONCURRENTLY` can't run inside a transaction, so in Rails you need `disable_ddl_transaction!` plus `algorithm: :concurrently`.
3. **Normalization parity.** Ruby's `strip` removes all leading and trailing whitespace, including tabs and newlines. Postgres `trim()` only removes spaces by default. Unicode `downcase` and `lower()` can also differ depending on the database's ctype. Emails with a stray `\t` or non-ASCII characters could end up normalized one way by the app and another way by the backfill. To find them:
   ```sql
   SELECT count(*) FROM users WHERE email ~ '^\s|\s$' OR email ~ '[^\x00-\x7F]';
   ```
   If the count is nonzero, make the SQL expression match the Ruby one (for example, `lower(regexp_replace(email, '^\s+|\s+$', '', 'g'))`), or normalize those rows through the app.

## Revised timeline, which still fits this week

1. Today: check the dead tuples and vacuum if needed, then start the batched backfill, throttled.
2. When it finishes: run the three gate queries above.
3. Once all three are clean: run `CREATE UNIQUE INDEX CONCURRENTLY` (step 4).
4. Then step 5 as planned: add the `CHECK ... NOT VALID`, run `VALIDATE CONSTRAINT`, run `SET NOT NULL` (PG12+ skips the full scan when a valid CHECK exists), and drop the CHECK.

The duplicate check is the real risk to ops' deadline, not the backfill. I'd run it today against the rows you can already compute (`GROUP BY lower(trim(email))`). That way, if a cleanup decision is needed, you find out now and not after the index build fails.
