---
persona:
  agent: data_analyst
  skills: [skill-analysis-critical, skill-mathematical-reasoning, skill-content-structure]
  implants: [implant-uncertainty-quantification]
---
# Verifier accuracy: control set, pilot and measurement runs

This is an executable instruction for an AI. It runs the measurement that
[verifier-accuracy-measurement.md](../docs/verifier-accuracy-measurement.md)
designs: a known-answer control set, verifier runs through the normal worker
exchange with the answers out of every worker's reach, and the analysis of
`c` and `b` per family. The owner approves it in stages. The pilot comes
first, and the full run starts only after the owner says so having seen the
pilot's cost. Applying measured values to [model.py](../src/claims/model.py) or
the claim store is a separate change, outside this flow.

The persona contributes statistical judgment. Its own protocol and Output
Format do not apply: this flow's steps and its report in step 7 do. The steps
name Agents-Core paths because the harness lives in this repository
([evals/verifier_accuracy](../evals/verifier_accuracy/__init__.py)); run the flow
against an Agents-Core checkout on macOS.

## How to invoke

```text
Run flows/verifier-accuracy.md: pilot.
Run flows/verifier-accuracy.md: full run, approved by the owner after the pilot report of <date>.
Run flows/verifier-accuracy.md: stage of 220 runs of the full set, approved by the owner.
Run flows/verifier-accuracy.md: analyze the current set.
```

## Inputs and authority

- **Stage.** `pilot` builds a 20-item set and runs it (about 80 worker runs).
  `full` builds the 240-item set and runs it (880 runs); it needs the owner's
  explicit go-ahead in the conversation, given after the pilot report and naming
  the cost it accepts. The owner may approve the full set in stages instead: a
  stage runs the leading complete clusters of the set's schedule that fit a run
  count, is analyzed with the planning look, and decides only whether the next
  stage starts. A stage is part of the full run, never a replacement for it, and
  it proposes no value. `analyze` only reads the current set's records.
- **The owner decides** the labels that no deterministic check settles (the
  manifest's `owner_review` count), whether to accept the statements the builder
  skipped to keep one configuration, a raise of the floor under `D`, and every
  value that would later be applied.
- **Never** apply a value to `src/claims/model.py` or the store, and never start a
  second stage in the turn that reports the first. A pilot, a stage and the rest
  of the full run are each a stage in this sense.

## Blinding rules for the parent

Every command prints aggregates only. The parent never opens, prints, searches or
copies a file of the control directory (`claims-control` under the service-state
directory): not the statements, the labels, the attempt records, the owner's list
or the canary. It never quotes a statement into chat, memory, a history entry, an
issue or a pull request. The owner reads `owner-review.jsonl` personally when its
count is above zero.

Workers are headless Claude Code sessions (`claude -p`) that the script starts
under macOS `sandbox-exec`. The profile denies the control directory to every
process the worker starts. It makes the snapshot and the home directory
unwritable except Claude Code's own state. It hides the other runs' session
records and prompt history and every package but the run's own, so an item's
two verdicts stay independent. A package's `scope` is an instruction, not a
sandbox, and an Agent-tool worker cannot be blinded this way, because it runs in
the parent's process with the parent's permissions.

## 1. Preconditions

- macOS with `/usr/bin/sandbox-exec`, the `claude` CLI signed in, and an
  interpreter with this repository's dependencies (numpy among them). Run every
  command from the repository root as `python -m evals.scripts.verifier_accuracy`.
- `--installation <path>` names the installed service whose state directory holds
  the control directory. When `AGENTS_SERVICE_DIR` is set, that directory is used
  instead; the script refuses one inside the installation root or inside a
  checkout a worker receives.

## 2. Build the set

```bash
python -m evals.scripts.verifier_accuracy --installation <path> build --name <pilot|control> --items <20|240> --seed <n> --commit <pinned commit>
```

Pin a commit of `dev` that holds no measurement code under test. Check the
printed manifest: classes balanced, at least half of each class
`command_checkable`, `owner_review` zero or handed to the owner, and the
`skipped` counts (`risk_floor`, `bundle_change`) for the report. A set already in
the control directory must be discarded first (step 7).

## 3. Simulate coverage

```bash
python -m evals.scripts.verifier_accuracy --installation <path> simulate
```

It simulates the analysis at the priors and at `b` = 0.995 on the set's own
cluster structure and raises the floor under `D` until every band holds its level:
the lower 95 % bound of its simulated coverage reaches the nominal level, which
takes the default 1000 simulated sets. `run` accepts only a simulation of the
final look.
It writes `simulation.json`, which `run` requires. Exit status 1 means no floor up
to 16 held: stop and report.

## 4. Probe the blinding

```bash
python -m evals.scripts.verifier_accuracy --installation <path> probe
```

The probe worker must report `blind: true` with reason `denied`. Any other result
stops the flow: the blinding is broken, and no verdict may be collected.

## 5. Run

```bash
python -m evals.scripts.verifier_accuracy --installation <path> run --concurrency 4 --batch-size 20 --max-total-usd <cap>
```

For a stage, add `--stage-runs <n>`. The schedule orders the set's clusters by the
seed and keeps every slot of a cluster's items together; the stage takes the
leading complete clusters whose slots fit `n`, so it is a cluster sample of the
set and its design effect estimates the whole set's. Keep the seed of the first
stage for every later one; a later run with a larger `--stage-runs`, or none,
continues where the stage stopped.

Run it in the background; wait for it to finish instead of polling. A probe
precedes each batch, and a failed probe stops the run with every remaining
attempt recorded `unfinished` (`probe_failed`). A failed attempt record stops it
too. Running the command again resumes: a slot whose attempt ran is skipped, and a
slot whose only record is a placeholder for a stop (`probe_failed`,
`runner_stopped`, `budget`) runs again; the analysis counts each slot once, by
its latest record, and the budget starts from what the set already spent. Cap the
pilot at $30 and the full run at the pilot's mean cost per run times the runs,
times 1.5.

## 6. Analyze

```bash
python -m evals.scripts.verifier_accuracy --installation <path> analyze --output <file outside the repository>
```

Use `--look interim` for the first half of a full run (it applies only rule 3).
After a stage, use `--look planning`: the final look's bands with no
replacement-rule case, and a `planning` section that projects the whole set. The
projection draws `(c, b)` from the stage's posterior, simulates the full design
(every item of the set, its clusters, the stage's verdict yield per arm) under the
larger of the calibrated floor and the stage's own `D`, and reports the projected
half-widths, the share of simulated sets that reach ±0.03 and the share in each
rule case, with the slots left and their cost at the stage's mean.
The pilot's 20 items give wide bands; read its analysis as a check of the
pipeline, not as an estimate.

## 7. Report and stop

Report from the analysis and `status` output, never from the control files:

- cost per run by arm (mean, median, maximum), the probes' cost and the total,
  as the host reported them (API list-price equivalent);
- each batch's probe result;
- statuses per arm and class, the levels assigned (`unsupported` V3-arm verdicts,
  V2-arm verdicts counted as V1), runs set aside (`excluded`, `discarded`) and
  items left out for mixed configurations;
- for a full run, the bands, the replacement rule's case per parameter and the
  findings; for a stage, the bands, the measured `D` and yields, and the
  projection, with no rule case; for the pilot, what in the design proved
  unworkable.

After a stage, end with one question to the owner: whether to start the next stage
or the rest of the set, with the projection and its cost. Keep the set: the next
stage continues it.

After a pilot, end with one question to the owner: whether to start the full run,
with its projected cost. Then move the pilot set aside, which keeps it inside the
control directory, out of every worker's reach:

```bash
python -m evals.scripts.verifier_accuracy --installation <path> discard
```

## Done when

- The probe before every batch was blind, or the run stopped at the first one
  that was not.
- Every slot the stage scheduled has exactly one current record, its latest; the placeholders
  a resume superseded stay in the file as the record of the stop.
- The report states cost, blinding, statuses and findings, and nothing in it
  names a statement or a label.
- The next stage waits for the owner.
