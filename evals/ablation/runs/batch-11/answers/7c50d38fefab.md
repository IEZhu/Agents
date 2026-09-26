**For the change request, set `minReplicas: 12`.** But pods aren't what will limit you on Black Friday. Two other problems come first, and 12 pods with your current config will make one of them worse.

## Pod math

- Now: 4 pods × 60% CPU = 2.4 pods' worth of CPU for 400 rps.
- 3x traffic: 1,200 rps → about 7.2 pods' worth, if CPU scales linearly with rps. That's roughly true for a stateless API, but check it with a load test.
- To keep the same ~60% headroom: 7.2 / 0.6 = **12 pods**.
- Set `maxReplicas` to about 16 so HPA can absorb a spike above the forecast.
- Pre-scale a day or two ahead, and make sure the node group, Cluster Autoscaler or Karpenter can actually place 12–16 pods. Otherwise you'll have pending pods at 00:01.

This assumes "3x normal peak" means 3 × 400 rps. If marketing meant 3x average traffic, the number is lower. Confirm with them, but 12 is the safe choice.

## Blocker 1: connection pool overflow

- Today: 4 × 50 = 200 connections.
- At 12 pods: 12 × 50 = **600**, more than `max_connections = 400`. RDS also keeps a few connections for its own admin user, so the real limit is slightly lower.
- New pods will fail to get connections once the database hits the limit. That shows up as 5xx errors or pods failing readiness, right at peak.

**Fix: shrink the Hikari pool to about 20 per pod.**
- Little's law: 1,200 rps × 0.18 s ≈ 216 requests in flight across the whole fleet, or about 18 per pod at 12 pods. The time each request spends in the database is shorter than that, so 50 connections per pod is far more than you need.
- 16 pods × 20 = 320 connections, which stays under 400 even at `maxReplicas`.
- Freeze deploys during the event. A rolling update's `maxSurge` adds extra pods, and each one opens another pool.

## Blocker 2: the Postgres primary

- Database CPU is 45% at 400 rps. Scaled linearly, that's **about 135% at 1,200 rps**. The primary saturates, and adding pods only puts more queued load on it.
- It might not be perfectly linear, since caching and query mix matter, but I wouldn't bet Black Friday on it.
- Options, roughly in order of effort:
  1. **Move the RDS instance up a size class** (for example, double the vCPUs). Do it weeks ahead, not days. With Multi-AZ the resize still triggers a failover of about 1–2 minutes, so schedule a window. It costs more, so cover it in the change request, and you can size back down afterwards.
  2. **Send read-only queries to a read replica** (product and price lookups, for example). Checkout writes stay on the primary. This needs code changes plus a second Hikari pool.
  3. **Fix the top queries.** `pg_stat_statements` will show the heaviest ones, and one missing index can easily be worth 20% CPU.
  4. Cache read-heavy lookups in Redis or ElastiCache.

## What I'd put in the change request

| Item | Value |
|---|---|
| HPA `minReplicas` | 12 (from 4) |
| HPA `maxReplicas` | 16 |
| Hikari `maximumPoolSize` | 20 (from 50) |
| RDS instance | one size up, scheduled weeks ahead |
| Deploy freeze | Black Friday window |
| Pre-scale timing | 24–48 h before |

**Blast radius:** if the pool change goes wrong (too small), requests time out waiting for a connection. You'll see it in HikariCP's pending-threads metric and in p95 latency. If the RDS resize fails, the database is unavailable during the failover.

**Rollback:** reverting `minReplicas` and the pool size is a config revert plus a rolling restart. You can also resize RDS back down, which triggers another failover.

**Before the event, verify** with a load test at 1,200 rps or more against a staging copy at production size. Watch pod CPU, Hikari active and pending connections, RDS CPU, and `numbackends`. Without that test, the 12-pod number is an estimate, not a guarantee.
