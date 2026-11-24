# Turn records

Source of truth for behavior: [`src/memory/record.py`](../src/memory/record.py) (schema and validation), `log_interaction` and `read_history` in [`src/server.py`](../src/server.py), and `HistoryReader.read_recent` in [`src/memory/history.py`](../src/memory/history.py).

A turn can attach one small typed map to its journal entry: `log_interaction(record=...)`. It is written into the entry's existing `**Meta:**` JSON line under the reserved key `metadata.record`, and a later run reads it back whole with `read_history`. It exists so that a flow can measure itself: the thread-close flow writes what a close found and cost, and a retrospective compares those records across closes instead of one session's impression.

## Decision (#584)

[#584](https://github.com/IEZhu/Agents/issues/584) offered three ways to keep a turn's numbers. This page records the choice of the first.

- **Chosen: a caller-settable structured field on `log_interaction`.** The journal already writes `metadata` as one line of JSON that `HistoryReader._parse_block` parses back, and `read_history` returns `metadata` whole in recency and entry mode: listing previews shorten text fields and lists, never `metadata`. So the record needs no new storage, rotation, deduplication, deletion or sync path; it moves wherever its entry moves.
- **Not chosen: a reserved tag namespace with a tag filter.** Writable today and the smallest change, but every number would live inside text (`#rounds-4`), untyped and parsed back by convention, and a listing keeps only 20 tags of an entry. The tag filter it needed is part of this change anyway, as the way to select records.
- **Not chosen: a record file beside the journal**, as autodev keeps `<state_dir>/schedules/runs/<run-id>/record.json`. Proven there, but a turn's record would then sit outside what `read_history` serves, and a retrospective would need a second reader with its own rotation, deletion, workspace isolation and sync scope.

## Writing a record

The record is a closed JSON object, schema v1. `v` and `kind` are required, every other key is optional, and no other key is accepted, also inside list items. Values are bounded integers, enumerated values, slugs and times: there is no free text and no path, because a record is read by machines and a stray path or secret would sit in every entry that carries one.

| Key | Value |
|---|---|
| `v` | `1`, the schema version |
| `kind` | `report` (a close's report), `patch-apply` (an entry that applies patches), `status` (a status entry) |
| `session` | the session id, as a slug |
| `close_ordinal` | which close of that session, 1 to 10,000 |
| `findings` | up to 32 tool-usage rows: `{slug, status, cause, miss_at, level}`; `slug` and `status` required |
| `patches` | up to 32 patches: `{id, layer, slug, state, applied_at}`; `layer`, `slug` and `state` required |
| `verifier` | counts from the independent verifier: `{claims, confirmed, refuted, unconfirmed, invalid_rounds}` |
| `prep_failures` | worker preparations that failed |
| `unexecutable` | up to 16 flow steps that could not run as written: `{step, code}`, both slugs |
| `duration_s` | seconds from the transcript's first entry to its last, up to 10,000,000 |
| `responses` | model responses, deduplicated, up to 1,000,000 |
| `output_tokens` | output tokens of those responses, up to 1,000,000,000 |

Counts are integers from 0 to 100,000; `true` is not a count. The enumerated values follow the thread-close flow's vocabulary:

- `status`: `used`, `caught` (a hook blocked or reminded, and the same turn then made the call; never a miss), `misused`, `missed`, `unavailable`, `n/a`, `uncatalogued`, `no-rule`.
- `cause`: `not-in-context`, `ignored`, `not-loaded`, `no-trigger`, `failed`.
- `level`: `V0`, `V1`, `V2`, `V3`, `V4`, and `refuted`, `stale`, `contested` for ✗, ⚠ and `?`.
- `layer`: `memory`, `user-instruction`, `client-instruction`, `client-config`, `hook`, `flow`, `flow-persona`, `flow-library`, `agents-core`, `issue`.
- `state`: `proposed`, `applied`, `verified`, `declined`, `failed`.

A slug is lowercase letters and digits joined by single dots, dashes or underscores, at most 64 characters: a session id, a catalogue slug, a step number such as `5.3`, an error code. A time is ISO 8601 with its offset; it is stored in UTC as `YYYY-MM-DDTHH:MM:SS+00:00`, the form of the journal's own entry times, so the two compare as text. `miss_at` is the time of the trigger the finding is about, not the time of the close; `applied_at` is the time the patch was applied.

A finding's `slug` names one rule, and the thread-close flow gives each rule exactly one, whichever instruction states it ([step 4.2, **One rule, one slug**](../flows/thread-close.md#4-audit-tool-usage)): a catalogue slug without a dot for an Agents-Core tool, `cooperation-<clause>` for each cooperation clause, `<server>.<tool>` for a rule on another tool, such as `ccd-session.mark-chapter`, and `rule.<name>` for any other rule. A patch of a finding carries that finding's slug, and the finding's miss tag carries the same slug. Records written before that rule may name one rule under several slugs, such as `mark-chapter`, `claude-md.mark-chapter` and `ccd-session.mark-chapter`. The journal keeps them as written, the flow's **Repeats escalate** says how a close still matches them, and the miss-rate report below reads them under their canonical slug.

What the server does with it:

- **Nested, never merged.** The record goes under `metadata.record`. `metadata` also holds the server's own keys, `_agents_write` (the idempotent-write receipt) and `claimed_workspace` (the fallback journal's owner), which replay and `fallback.matches` trust; a record cannot reach them.
- **Refused whole when invalid.** An invalid record writes nothing: the answer is `status: ERROR`, `message: invalid_record`, and `record_errors` lists up to 20 problems, each naming the place and the rule, such as `record.findings[2].level: one of V0, …`. A problem never repeats the refused value, and an unknown key is counted, not named, since either may be what the record was refused for. Fix what it names and call once more. This is stricter than persona attribution, which never costs the turn: an entry written without its record could not get it later, because a corrected retry of the same text is a duplicate.
- **Credentials and paths refused.** A slug cannot hold a path or a space. One that matches a credential pattern of the [user library sync's scanner](user-sync.md) is refused too. The server masks no logged field; callers redact text fields before the call, and a record has no text field to redact.
- **At most 4,096 bytes** of compact JSON. With every text field at its shortest preview, an entry carrying a record at the cap stays a small part of a listing's budget, so the record alone can never make `read_history` reduce its entry to a `stub`, which would drop `metadata`.
- **Not part of the content hash.** The entry id still comes from intent, action and outcome, so a duplicate keeps the entry already written and the record that entry has, if any; this call's record is then not stored, and the answer's `instruction` says so. The answer reports `record: {status, bytes}`, where `status` is `written`, `duplicate`, `not_written` (the record was refused as invalid, an answer that carries no `bytes`, or the journal refused the write) or `unknown` (the write's outcome is uncertain, as `history.state: unknown` says; a keyed retry of the identical payload settles both).
- **Keyed writes.** With an `idempotency_key`, the stored record (times already in UTC) joins the payload fingerprint only when present, so a receipt written before records existed keeps its hash and still replays. The same key with a changed record is an `idempotency_conflict`.

## Reading records back

`read_history(tag="thread-close")` lists the entries carrying exactly that tag, newest first, each with its `metadata.record` whole. A tag matches with or without its leading `#`, and only as a whole tag: `thread-close` does not match `thread-close-x`. Recency reads cover `history.md`, a pending rotation and the `history/YYYY-MM.md` archives, read newest month first until `limit` is filled, also without library sync; with sync, the other machines' segments as well, where a record arrives in the segment's `**Meta:**` line unchanged and the entry carries the writing machine's `machine` label. Under budget pressure a listing leaves later entries out (`omitted`) instead of cutting a record, and `offset` reads them. `read_history(entry_id=...)` returns one entry whole.

Semantic mode (`query`) returns no `metadata`, so a retrospective reads records in recency or entry mode, never through a search.

## What stays the same, and what changed

- Workspace isolation: a record is part of the entry of whichever journal the call resolves to, the project's or the installation's fallback journal, under the same rules as before.
- Content deduplication: unchanged, since the record is not hashed.
- User library sync: unchanged; the record is a value in an entry block the sync already shares after its owner's approval.
- Changed: a recency read without library sync used to see only `history.md` and a pending rotation; it now reaches the archives too, so a record rotated away is still listed. An archive directory that is a link, which `read_history(entry_id=...)` already refused, now fails a recency read the same way instead of being ignored.

## For a retrospective

The thread-close flow writes `kind: report` once per close, which is what separates a close's own record from the patch-apply and status entries that carry the same tags. To decide whether a tool-usage patch held, compare each later finding's `miss_at` for the same `slug` with the patch's `applied_at`: a `missed` or `misused` finding after the patch was applied means it did not hold; a `used` row after it is evidence that it did, and a `caught` row that a hook, not the model, made the call happen.

### A rule's recent miss rate

[`scripts/thread_close_miss_rate.py`](../scripts/thread_close_miss_rate.py) turns those records into one row per slug, as evidence for the flow's "Repeats escalate" step; it never decides an escalation. It needs Python 3.11 or later and nothing beyond the standard library, the checkout's `src/` and the catalogue of its `flows/thread-close.md`. Run it with `--workspace <repository>`; `--json` prints the same as JSON:

- **Outcomes.** It reads the `#thread-close` entries without `#progress` whose record has `kind: report`. Each close gives a slug at most one outcome: a miss for a `missed` or `misused` finding at `V1` to `V4`, held for a `used` or `caught` row. Every other row gives none, a `V0` miss included, as the flow never counts one as a repeat, and so does a row the record schema refuses, which can only have been edited or damaged after it was written. A miss in one row outweighs a held row of the same slug in the same close.
- **Rate.** A slug's outcomes are filtered in close order, at each close's own time, with the fading-memory Beta update of [`src/estimation/rate.py`](../src/estimation/rate.py) and a 14-day half-life, then carried to now. The row shows the closes that gave an outcome, the recent miss rate with its 95 % band, and the all-time misses. The prior is uniform, so a slug seen in one close reads 67 % after a miss and 33 % after a hold: with few closes, read the band, not the rate.
- **Old spellings.** Closes written before the flow's **One rule, one slug** may name one rule under several slugs. The report reads them under the canonical slug each time it runs and never writes the journal, which keeps them as written. An alias is one of three kinds:
  - *derived*: a two-part slug's second part alone, or after an instruction source (`claude-md`, `agents-md`, `memory`), as `mark-chapter` and `claude-md.mark-chapter` are for `ccd-session.mark-chapter` and `link2action` for `rule.link2action`. It applies only when exactly one slug `<first>.<part>` whose first part is not a source was written in some close or is a target of the table below: two such slugs are two rules, so the part they share stays as written. A slug with more than one dot, which the record schema accepts but the flow never writes, is neither a target nor derived. A bare catalogue slug, read from the flow's catalogue, and a cooperation clause are canonical and never derived.
  - *table*: spellings that no rule derives. `task-list` and `claude-md.task-list-first-call` read as `ccd-turn.update-plan`, and `claude-md.websearch-standard` as `client.websearch`.
  - *clause*: a bare `cooperation` miss reads as `cooperation-<clause>` when its close's `ac-missed-` or `tool-missed-` tags name exactly that one clause. A miss tag names only the clause that was missed, so a held bare `cooperation` row stays `cooperation`, and so does a miss whose close names no clause or several.

  Rows read under one slug in one close give that close one outcome, a miss outweighing a hold as before. Every row lists the spellings merged into it, each with the closes it gave an outcome in: the `merged from` column of the table and `merged` (`slug`, `kind`, `closes`) in `--json`, empty for a row whose closes all wrote its slug. A merged count is an inference, not a recorded slug; the flow's **Repeats escalate** still confirms each old spelling against its close's report before counting it as a repeat.
- **What it does not see.** It reads only this checkout's journal, `history.md`, a pending rotation and the archives, never the segments from other machines that the user library sync adds to `read_history`. A close from before records existed gives no outcome, and one whose record left its `used` and `caught` rows out for size gives only its misses, which raises the rate. The header line says how many closes carry a report record. An old spelling the alias map cannot place, one worded otherwise or one whose canonical slug no close has written yet such as `scratchpad`, still shows as a row of its own.
