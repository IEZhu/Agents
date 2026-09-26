I can't run a web search from here, so I can't confirm the current release state live. Here's what I know from memory. Please check the two sources linked at the end before anything goes into the ticket.

**Short answer (from memory, not checked today)**

- **Newest stable major: PostgreSQL 18**, released September 2025. One caveat on timing: PostgreSQL ships a new major every September, and today is 26 September 2026, so **PostgreSQL 19 may have come out in the last few days or may be about to**. That's the fact you most need to check.
- **PostgreSQL 13 no longer gets security patches.** Community support ended in **November 2025**, and 13.23 was the final minor release, as I recall. Production on 13 has now gone about 10 months without upstream CVE fixes. If you're on a vendor build (RDS, Azure, Crunchy, EDB and so on), their support dates may differ. RDS, for example, has paid "extended support" for EOL majors.

**What to put in the ticket**

Name a major version and a minimum minor version. Don't pin an exact minor, because a new one comes out every quarter. For example:

> Target: PostgreSQL 18 (latest 18.x minor at time of upgrade). Reason: PostgreSQL 13 reached community EOL in November 2025 and no longer receives security fixes.

If 19 is out by the time you check, I'd still lean towards **18**. A .0 release rarely belongs in production, and 18 will have about a year of bug fixes behind it. Community support for 18 should run to about November 2030 (five-year policy, date not checked).

**Upgrade notes for 13 → 18** (from memory, check the release notes)

- `pg_upgrade --link` supports going straight from 13 to 18, so you don't need to stop at intermediate versions. Logical replication is the other option if you need near-zero downtime.
- As I recall, `initdb` in 18 **turns data checksums on by default**. `pg_upgrade` needs the old and new clusters to match, so if your 13 cluster has no checksums, create the new cluster with `initdb --no-data-checksums` (or turn checksums on in 13 first with `pg_checksums`, which needs the server stopped).
- **MD5 password auth is deprecated** in 18. Plan to move to `scram-sha-256`.
- Before upgrading, check that each extension has a build for 18, and look at `pgBouncer` and driver versions too.
- Take a full backup (`pg_basebackup` or a snapshot) and rehearse the whole upgrade on a staging copy first. Run `pg_upgrade --check` against a production-sized copy to get real downtime numbers.
- After the upgrade, run `vacuumdb --all --analyze-in-stages`. PG 18 can carry planner statistics across the upgrade, but as I recall extended statistics still need a fresh `ANALYZE`.

**Where to check (about two minutes)**

- https://www.postgresql.org/support/versioning/ has the table of supported versions, current minors and EOL dates. This is the one to cite in the ticket.
- https://www.postgresql.org/about/newsarchive/ shows whether the 19 release announcement is out.

Paste the versioning table here if you'd like, and I'll tighten the ticket wording.
