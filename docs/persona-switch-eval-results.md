# Evaluating persona switching with conversation continuity

Date: September 20, 2026. Implementation: `c063f4a`, branch `codex/persona-switch-gate`.
Baseline: `92c55a1` (main after PR #69).

The server implementation and migration are ready. Version 2 remains an explicit
opt-in through `AGENTS_PERSONA_PROTOCOL=2`; the installer defaults to version 1.
These results apply to the recorded CLIs and models, not to every MCP client.
Codex has not completed acceptance testing: the account usage limit interrupted
the third repeat.

## Behavior under test

The model assesses whether its current role fits the task. On a continuation, it
does not call the router, agent catalog, or enrichment tools. When a switch is
needed, it loads a new role and logically revokes the previous role's instructions
while preserving facts and user constraints. The server does not remove earlier
messages from the context.

The main suite contains 12 Russian and English dialogues, totaling 36 turns, with
three independent repeats: 108 planned turns per client and protocol combination.
It covers continuations, format changes, new tasks within the current role,
explicit engineer-to-lawyer-to-engineer transitions, universal-to-DBA transitions,
a short substantive request, initial implicit selection, restore, refresh, and
simulated loss of instructions during compaction with and without a known role.

A separate supplemental suite contains two dialogues, one in Russian and one in
English, each with three turns and three repeats: 18 turns per combination. After
a greeting from universal_agent, the user asks about a composite PostgreSQL index
and EXPLAIN ANALYZE without naming the desired role or specialization, then asks
for clarification and the project name. The expected behavior is a switch to
database_admin followed by keep. This suite specifically tests whether the model
independently decides that a different specialization is needed.

Datasets: [main dialogues](../evals/datasets/persona_dialogues.jsonl),
[simulated compaction](../evals/datasets/persona_compaction.jsonl),
[implicit switching](../evals/datasets/persona_implicit_switch.jsonl).
Machine-readable results and checksums:
[persona-switch-results.json](../evals/persona-switch-results.json).

## Environment and method

- Codex CLI `0.155.1`, observed model `gpt-6-astra`.
- Claude Code `2.1.278`, observed identifiers `claude-opus-5` and
  `claude-opus-5[1m]`. These are the names reported by the client, with no claim
  that the provider or model weights remain unchanged.
- Real CLI sessions using resume and an MCP tool loop. Expected actions remain
  in the scorer and are never supplied to the model.
- Frozen baseline and candidate source trees and the same cold index seed.
  Candidate sources match the runtime in `c063f4a`. Each dialogue has its own
  caches and history; the seed contains no personal files.
- Each run receives the complete corresponding routing protocol. Claude runs
  with `--bare` and the protocol appended. For Codex, the test wrapper denies
  reads of only two global AGENTS files so that an older unconditional routing
  requirement cannot interfere with the protocol under test. User files,
  HOME/CODEX_HOME, authentication, and the model's base instructions are unchanged.
- Only protocol tools and read_history from the isolated history are available.
  The no-edit restriction applies to application files; every scenario explicitly
  permits protocol logging.

The scorer checks actual calls and results, the activation_id chain, descriptor
stability on keep, the correct role, the exact footer, attribution, and storage of
the final answer in history. Failed call attempts also count as unnecessary calls.
The answer is scored from the final message, not from a concatenation of progress
comments. Byte and latency comparisons use only matched completed turns with the
same case ID, repeat, and turn number.

The original labels for two KeyError scenarios required the word Ash in the first
answer even though the request did not ask the model to repeat the project name.
That requirement was removed only from turn0 of `ru-initial-implicit` and
`en-initial-implicit`. The requests were unchanged, and retained traces were
rescored without rerunning a model. The name check remains on the next turn,
where the user explicitly asks for it. The JSON retains the original verdicts and
hashes as well as the corrected label hash. This correction eliminated three
false negatives for Claude; client errors were not reclassified as successful
answers.

## Dialogue results

"Passed" means compliance with the target v2 rubric, including the final footer
and recording the answer in history. Baseline v1 is deliberately measured against
the same rubric: a failure here is not a regression in the compatible API. In
particular, Claude v1 often placed the substantive answer and footer in an
intermediate message, then ended with an "answer above" acknowledgement. The
strict final-answer check does not accept that behavior.

| Suite | Client | Protocol | Planned turns | Completed | Passed | Selection calls on keep / completed keep turns |
|---|---|---|---:|---:|---:|---:|
| Main | Claude | v1 | 108 | 108 | 1 | 54 / 30 |
| Main | Claude | v2 | 108 | 108 | 108 | 0 / 30 |
| Main | Codex | v1 | 108 | 85 | 47 | 52 / 26 |
| Main | Codex | v2 | 108 | 80 | 80 | 0 / 25 |
| Implicit switching | Claude | v1 | 18 | 18 | 0 | 8 / 6 |
| Implicit switching | Claude | v2 | 18 | 18 | 18 | 0 / 6 |

Claude v2 completed all three repeats of both suites: 126/126 turns. For Codex v2,
all 80 completed turns passed the rubric, 11 attempts were interrupted by the
usage limit, and 17 subsequent turns were not attempted. In the Codex baseline,
the limit interrupted 9 attempts and another 14 turns were not attempted. The
implicit-switching suite was not run for Codex after the limit was confirmed;
18 planned turns remain for each of v1 and v2. Incomplete answers count as neither
behavioral success nor evidence of a protocol defect.

On completed v2 turns, switching precision and recall were 100%: Claude completed
18/18 switches in the main suite and 6/6 implicit switches; Codex completed 12/12
in the available part of the main suite. No unjustified refreshes or unnecessary
selection calls on keep were observed. In all six supplemental Claude dialogues,
the model actually called route_and_load, loaded database_admin, and retained
Alder on the next turn without another selection. Domain accuracy of the technical
explanations was outside the automated rubric.

## Measured cost

The table includes only matched completed turns. Bytes are serialized MCP
responses, including permitted logging, not context-window size or tokens.
Latency includes CLI startup, the model, and tools; concurrent load was not
controlled as it would be in a dedicated performance benchmark.

| Suite / client | Matched turn pairs | Bytes v1 → v2 | Median, seconds v1 → v2 | p95, seconds v1 → v2 |
|---|---:|---:|---:|---:|
| Main / Claude | 108 | 6,518,420 → 4,348,642 | 20.5 → 22.7 | 28.8 → 32.3 |
| Main / Codex | 80 | 5,089,050 → 3,033,515 | 29.7 → 30.6 | 38.3 → 38.8 |
| Implicit switching / Claude | 18 | 874,008 → 551,910 | 22.3 → 20.5 | 33.0 → 42.4 |

On keep turns, MCP result volume decreased by 96.1% for Claude in the main suite,
96.5% for the available Codex pairs, and 95.0% for Claude in the supplemental
suite. Median keep latency decreased from 20.9 to 14.8 seconds, 26.3 to 20.9
seconds, and 20.5 to 14.7 seconds, respectively. Overall median latency did not
improve in the main suites; no universal speedup is claimed.

Numeric CLI usage snapshots are retained per turn. They may contain cumulative
counters from a resumed session, so their sum and any resulting token savings
are not presented as reliable metrics.

## Manual review of role revocation

Rubric: after returning to the engineer role, the answer must explain dict in
technical language, retain the project name and Python, and acknowledge the
standing restriction on editing application files. It must not carry over
contractual definitions, references to the "Parties," legal clause numbering, or
legal disclaimers from the previous role.

All ten available final answers were reviewed: four from Codex and six from
Claude. All ten preserve Cedar (RU) or Juniper (EN), Python, and the constraints;
no carryover of legal style was observed. Two Codex answers are missing because
of the usage limit: `en-switch`, repeat 2, stopped on the first turn;
`ru-switch`, repeat 2, stopped on the second. Neither is counted as passing.

In some Claude answers, an additional constraint statement or list exceeds the
"one sentence" requirement. This is a limitation in format compliance, not
evidence of lost facts or carryover of the revoked role. These checks do not
establish full compliance with every stylistic requirement.

## Deterministic checks

- Full suite, including slow tests: **736 passed** (`pytest tests/ -m '' -q`).
- After final label corrections: **18 passed** for the dialogue runner tests.
- Actual standard bundle assembly for all shipped agents: **43/43**.
- Coverage includes v1/v2 contracts, meta queries, sampling capability, assembly
  failures, strict imports, content revisions, independent descriptors, caches,
  attribution, slash commands, and managed-instruction migration.
- `git diff --check`, Python compilation, and `bash -n scripts/init_repo.sh` passed.
  Native `cmd.exe` execution on Windows was not performed; the shared migration
  helper was tested. The v1 templates are preserved byte for byte from baseline.

## Scope and reproduction

Simulated compaction creates a new CLI session with a fixed factual summary and,
in one variant, a persona descriptor. It does not test the native context
compaction algorithm in Codex or Claude Code. Stale responses, replay, mandatory
component failures, and isolation of two descriptors are covered primarily by
contract tests; each model's behavior under every such failure remains unverified.

Zero unnecessary calls in this suite is not a guarantee for every request. Other
model versions, Desktop clients, and user instructions that mandate routing
require separate testing. Existing rules in arbitrary client memory are not
rewritten automatically.

To reproduce the evaluation, use
[run_persona_dialogues.py](../evals/runners/run_persona_dialogues.py). Freeze two
source trees, one index seed, and an identical request suite combining
persona_dialogues.jsonl and persona_compaction.jsonl. Specify `--repeats 3`, a
separate `--out` for each run, `--source-root`, `--seed-data`, `--dataset`,
`--protocol`, and `--protocol-version`. For Codex on macOS, add
`--codex-isolate-global-instructions`. Run the supplemental
persona_implicit_switch.jsonl suite separately against both protocol versions.
The runner does not overwrite an existing output directory.

Raw local reports and traces are retained in the ignored
`.reports/persona-switch/` directory; only synthetic scenarios and compact results
are included in the repository. Further Codex acceptance testing requires a full
rerun after the usage limit resets, including the separate implicit-switching
suite. The limit was not reset, and no credits were purchased.
