# Verifier accuracy: the 20-item pilot

**Status:** run on 2026-11-23; the pilot set is discarded and the full run waits for the
owner's decision. This is the pilot that
[the measurement design](verifier-accuracy-measurement.md#the-control-set) asks for
before the 880-run measurement: it checks the runner, the blinding and the cost of a
run. Its bands are a check of the pipeline, not estimates. The code is
[evals/verifier_accuracy](../evals/verifier_accuracy/__init__.py), driven by the
[verifier accuracy flow](../flows/verifier-accuracy.md). Every number below is an
aggregate; no statement or label of the set appears here.

## What ran

- **Set** `pilot@06a75729caa3d195`, built at commit `7a127be9` of `dev` (after
  IEZhu/Agents#729 and IEZhu/Agents#727 merged): 20 items, 10 true and 10 false, each
  from a different file, in 17 clusters, every stratum represented (3 items each,
  `config_default` 2). The V2 subset has 17 items (8 true, 9 false). The false items
  are 5 near-miss names, 2 paths one segment off, 2 off-by-one numbers and 1 negated
  sentence. No label needed the owner: the deterministic check decided every
  candidate. The builder skipped 6 candidates whose text would raise the worker's
  tier and 2 whose package would select other skills.
- **Configuration**, read from the host records: client `claude`, tier `standard`,
  model `claude-sonnet-5-5` (the `sonnet` alias at the time), effort `medium`, one
  `bundle_revision` for both arms, role `code_reviewer`, result type `research`,
  Claude Code 2.1.311. No run lacked a model and no item mixed configurations.
- **Runs**: 74 attempts (40 V3-arm, 34 V2-arm) and 4 canary probes, 4 workers at a
  time, in 3.5 minutes of wall-clock time.

## Cost

As the Claude Code CLI reported it for each run: the API list-price equivalent, not a
bill.

| Runs | Count | Mean | Median | Maximum | Total |
|---|---|---|---|---|---|
| V3 arm | 40 | $0.168 | $0.166 | $0.187 | $6.73 |
| V2 arm | 34 | $0.167 | $0.166 | $0.174 | $5.67 |
| Canary probes | 4 | $0.053 | $0.022 | $0.152 | $0.21 |
| **All** | 78 | | | | **$12.61** |

A run took 8.6 seconds and 3.2 turns on average. Context dominates the cost: about
38 000 tokens written to the prompt cache and 96 000 read per run, against 750 output
tokens.

**The full run**, 880 runs and 44 probes, would cost about **$150** in the pilot's
worker context, or about **$100** without the user's global instructions (finding 4:
$0.112 per run in the post-fix check). At 4 workers it takes about 45 minutes.

## Blinding

- **Probes.** All four probes, one before each batch, tried the canary four ways (the
  Read tool, `cat`, `head` and a Python `open`). Every attempt got
  `Operation not permitted`, and the canary's token appeared in no transcript.
- **Transcripts.** None of the 74 worker transcripts names the control directory, so
  no run was discarded. None tried another run's session records.
- **What the sandbox enforces.** Each worker runs under `sandbox-exec` with a profile
  written for that run. The control directory is unreadable and unwritable for the
  file tools and the shell alike. The snapshot and the home directory are unwritable,
  except Claude Code's own state. Other runs' transcripts, auto-memory and the prompt
  history are hidden, as is every exchange package but the run's own. Each was probed
  by hand while the runner was built.

## Statuses

| Arm | Class | Verdict | Invalid | Abstained | Unfinished |
|---|---|---|---|---|---|
| V3 | true | 16 | 4 | 0 | 0 |
| V3 | false | 17 | 3 | 0 | 0 |
| V2 | true | 15 | 1 | 0 | 0 |
| V2 | false | 16 | 2 | 0 | 0 |

- **Levels.** Of the 33 V3-arm verdicts, 30 got level V3 and 3 were unsupported
  (finding 6). All 31 V2-arm verdicts got level V2; none fell back to V1.
- **Invalid results.** Nine were valid envelopes whose conclusion gave the verdict in
  prose without the `VERDICT:` marker, recorded `invalid` with the reason the pilot's
  runner wrote, `no_single_verdict`; the runner now writes `no_verdict_marker` for this
  case (finding 5). One started with narration before its header (`invalid_header`).

## Accuracy, as a pipeline check

The 61 fitted verdicts held 2 errors, one per route. Both were refutations of a true
numeric constant, and each was one wrong verdict of the four on its item. Both labels
were re-checked against the snapshot and are correct. With 19 items in the model
family and 17 in the execution family, every band is wide and every parameter falls
under rule 4, as 20 items must:

| Family | Items | `D` | `c` estimate, band | `b` estimate, band |
|---|---|---|---|---|
| model (V3) | 19 | 1.19 | 0.9995, [0.718, 0.9996] | 0.967, [0.758, 0.997] |
| execution (V2) | 17 | 1.21 | 0.9995, [0.692, 0.9995] | 0.968, [0.763, 0.997] |

Specificity was 0.94 (V3) and 0.93 (V2), and sensitivity 1.0 for both; neither
difference band excludes zero. No V3 and V2 failure coincided, against 0.13 expected
under independence. The coverage simulation held at a floor of 1 under `D`, for this
set's structure.

## What proved unworkable, and what changed

1. **An Agent-tool worker cannot be blinded at host or OS level.** It runs in the
   parent's process with the parent's permissions, and the parent's runner must read
   the labels, so a host deny rule or an OS sandbox would block the runner too.
   Workers are headless `claude -p` sessions started by the script under
   `sandbox-exec`, with the tools Read, Grep, Glob and Bash. That is a fresh native
   worker of the `claude` client, but its system prompt is the CLI's rather than a
   subagent's. Preparation and validation call the same functions as the two MCP
   tools.
2. **Two verdicts are only independent if one worker cannot read the other's run.**
   The design blinds the control directory. A worker could also read the other
   verifier's transcript in `~/.claude/projects`, leave notes through auto-memory, or
   read another item's package. The per-run profile closes all three.
3. **`bundle_revision` varies with the statement.** Skills are selected by word
   overlap with the task. So a statement can change the bundle, and with it the
   configuration its verdicts belong to. The builder skips such statements: 2 in the
   pilot, and 24 of the 64 statements that reached the check in a 40-item trial build.
   The alternative is to
   key the configuration on the role and rule revisions without per-task skill
   selection.
4. **The configuration key misses the worker's host context.** Claude Code loads
   `~/.claude/CLAUDE.md` for any working directory under the home directory,
   whatever `--setting-sources` says. The pilot's snapshot lay under the service-state
   directory, so its workers read the user's global instructions, at about 13 000
   tokens and $0.055 a run. A file that changes often then sits inside the measured
   configuration. Each attempt now records the CLI version and a hash of that file
   when the worker loads it. Whether the full run should include it is the owner's
   call.
5. **The verdict marker.** In 9 of 74 results the verdict was in prose only. The task
   now asks for the marker too, and a marker opening the summary also counts. In a
   post-fix check, 22 runs on a throwaway 6-item set outside the pilot, every result
   carried a readable verdict. That check is confounded by finding 4: its snapshot
   lay outside the home directory, so its workers did not see the global
   instructions either.
6. **A read without a match.** `grep` that finds nothing exits 1, and the host records
   that as an error. Three V3-arm verdicts whose only snapshot read was such a `grep`
   were filed as unsupported. A recorded read now counts whatever its result. The
   pilot's records keep what the runner wrote.
7. **Raising `D` until every band holds is not monotone near the boundary.** At `b` =
   0.995 the band's upper edge is the 0.99375 quantile of the marginal posterior. A
   flatter posterior moves that edge below 0.995, so a set of 6 items holds at no
   floor. For the pilot's 20 items, and for the planned 240 and 200 items in clusters
   of two, the bands hold at a floor of 1. With clusters of three, 200 simulated sets
   raised the floor to 1.56, which is Monte Carlo noise of the coverage test; the
   full run's calibration needs about 1000 sets. The measured `D` also sits above 1
   for independent items: a ratio of two noisy variances, floored at 1 and maxed
   over `c` and `b`, averaged 1.16 in simulation. With the posterior's own shape, that
   makes the bands about 13 % wider than the design's planning figures: ±0.033 against
   ±0.029 for `c_model` at 240 items, in 20 simulated sets.
8. **Earlier-commit perturbations are rare.** Few constants and pins changed within
   the last 30 commits that touched their file: 3 of 30 facts in a sample had an
   earlier value, and the pilot drew none. The full set's perturbations will lean on
   the other kinds.
9. **No recorded claims exist yet**, so subjects come from the snapshot. The builder
   gives the files that a claim store names priority when one is passed.

## Not done here

This pilot applies no value to the claim filter. It does not build the production
audit that a raise needs. `VOLATILITY`, V1 and V4 keep their priors, as the design
says.
