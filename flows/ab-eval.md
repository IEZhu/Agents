---
persona:
  agent: ai_senior_engineer
  skills: [skill-prompt-engineering, skill-analysis-critical, skill-content-structure]
  implants: [implant-verify-assumptions, implant-uncertainty-quantification]
---
# Evaluate a change with an A/B on hosted models and Opus

This is an executable instruction for an AI. It decides with evidence whether a
change should ship: a rule, skill, implant or agent prompt, a rewritten text, or an
engine setting such as the embedding model. It runs the whole cycle: hypothesis,
case set, contexts, answers from hosted models and from Opus, blind judging,
statistics, a recorded result and a recommendation. The owner's part is the request
and the final decision; ask in between only for a decision listed under
[Inputs and authority](#inputs-and-authority) as theirs.

The persona contributes evaluation judgment. Its own protocol and Output Format do
not apply: this flow's steps and its report in step 9 do.

The steps name Agents-Core paths, because the harness lives in this repository
([evals/ablation](../evals/ablation/README.md)). Run the flow against an
Agents-Core checkout.

## How to invoke

```text
Run flows/ab-eval.md for rules/rule-english-pivot.mdc.
Run flows/ab-eval.md: embedding model microsoft/harrier-oss-v1-270m against intfloat/multilingual-e5-large.
Run flows/ab-eval.md for branch feat/new-tutor-prompt against main, without retrieval metrics.
```

## Inputs and authority

| Input | Default |
|---|---|
| Change | Required: a component to cut (`rule-*`, `skill-*`, `implant-*`), a candidate branch or revision, or a setting such as `EMBEDDING_MODEL=<model>` |
| Baseline | `origin/main` |
| Name | Short kebab-case name of the change, such as `english-pivot` or `embed-harrier-270m` |
| Case set | Written in step 3, or an existing file that the request names |
| Hosted models | `google/gemma-4-31b-it` on endpoint `novita/bf16` and `qwen/qwen3.8-27b` on `deepinfra/bf16`, through OpenRouter |
| Hosted runs | Two per model: temperature 0 with seed 7, then the provider's default temperature |
| Answer reasoning | `OPENROUTER_REASONING=medium` |
| Opus | `claude-opus-5-5` answers through the `Agents-eval` cloud routine |
| Judge | Opus agents in the cloud for every run, blind and in both orders |
| Retrieval metrics | When the change affects retrieval or routing: embedding model, thresholds, keywords, skill or implant metadata |
| OpenRouter budget | $10 per experiment |

Granted by the request: worktrees and `eval/<name>` branches, OpenRouter calls within
the budget, creating and firing the `Agents-eval` routine, a results document and a
pull request left unmerged. The owner's decisions: a budget above the default,
other models, merging, and any change to the checkout a running service uses (in
Agents-Core, the installation that runs the daemon). Other routines stay untouched.

Credentials: `OPENROUTER_API_KEY` from `~/.config/agents-evals.env`
(`set -a; source ~/.config/agents-evals.env; set +a`). Never print it. Python: an
environment with `openai`, `fastembed` and `datasets`
(`python -c 'import openai, fastembed, datasets'`); in the reference installation
that is `.venv-tests`, because `.venv` lacks `openai`.

## 1. Frame the experiment

1. Read the change and state the hypothesis in two sentences: what should improve,
   for which requests, and what it could harm.
2. Choose 3–5 case groups from it: where the change should help, where it could hurt
   (its exceptions and side effects), and `control` cases it should not affect.
3. Choose the arm build. Use a component cut when the change is the presence of one
   rule, skill or implant. Use `--arm` builds for anything else: a rewritten text, a
   branch, a setting
   ([two revisions or settings](../evals/ablation/README.md#two-revisions-or-settings---arm)).
4. Decide whether step 7 (retrieval metrics) applies.
5. Estimate the OpenRouter cost: cases × 2 arms × runs × models answers plus the
   same number of judge calls if a hosted judge is added, priced from
   `https://openrouter.ai/api/v1/models` (about 3k prompt and 5k completion
   tokens per answer with reasoning). Record the key's `usage_daily` from
   `https://openrouter.ai/api/v1/key` as the starting point. Above the budget, ask.

## 2. Prepare the workspace

1. Never work in the checkout a running service uses. Create the eval worktree from
   a revision that holds the component or change under test: the candidate for an
   addition, an edit or a setting, and the baseline only for a cut of a component
   that the candidate removes:
   `git worktree add --no-track -b eval/<name> .worktrees/eval-<name> <revision>`.
   For `--arm` builds, also create a baseline worktree; for two embedding models, one
   worktree per model, so each has its own `data/`.
2. Copy `evals/datasets/_unlabeled.jsonl` from the installation when it exists; it is
   git-ignored and spares the Hugging Face fetches of step 7.
3. Probe each hosted model with the run's settings, for answers and as a judge:
   ```bash
   export OPENROUTER_PROVIDER=novita/bf16,deepinfra/bf16 OPENROUTER_REASONING=medium
   python evals/ablation/hosted.py probe --model google/gemma-4-31b-it
   python evals/ablation/hosted.py probe --model google/gemma-4-31b-it --judge
   ```
   When OpenRouter answers `No endpoints found` with "Filter by Parameters", list the
   model's endpoints (`https://openrouter.ai/api/v1/models/<id>/endpoints`), pin one
   that passes the probe and record it. On 2026-10-02, `google-vertex/global`
   rejected these parameters for Gemini 3.8 Flash and `google-ai-studio` accepted them.

## 3. Write and check the case set

Skip the writing when the request names a case set; still check it.

1. Write 20–30 cases in the [ablation case format](../evals/ablation/CASES.md) with a
   `group` field: at least four per group where the change should help or hurt, and
   at least three `control` cases. Use the languages the hypothesis is about, real
   user voice and concrete details; never name the change. Rubrics have 3–4
   checkable items, at least one about the change's behavior; every factual claim in
   them must be right.
2. Give the set to one independent subagent with web access, not the writer, to
   check every rubric item and user message: wrong or outdated facts, claims a
   correct expert answer could violate, inconsistent dates or constraints. Apply the
   fixes, add `"checked": true` and `checker_notes` naming the review, and validate
   that case ids are unique.
3. Save it as `evals/datasets/<name>_cases.json` and commit it on `eval/<name>`.

## 4. Build the contexts once

1. `RUNS=$(pwd)/evals/ablation/runs/<name>`, `RUN=$RUNS/base`;
   `mkdir -p $RUN/cases`; copy the case set to `$RUN/cases/<component>.json`, where
   the component is the cut one or the experiment's name; write it to `$RUN/ids.txt`.
2. Build, one heavy process at a time and after checking free memory:
   a component cut runs `python evals/ablation/build_contexts.py $RUN`; `--arm` builds
   run `--arm without` in the baseline worktree and `--arm with` in the candidate
   worktree or with the candidate's settings, both on the same absolute `$RUN`.
3. Check `build_errors.json`: only "arms identical" entries may remain, and the report
   counts them. `plan.json` must hold two contexts per other case.
4. Make one run directory per answer run from `base`: `opus`, `gemma-r1`, `gemma-r2`,
   `qwen-r1`, `qwen-r2` (`rsync -a $RUN/ $RUNS/<run>/`). All runs then answer
   byte-identical contexts.

## 5. Hosted answers (OpenRouter)

1. For each hosted model, the first run:
   `python evals/ablation/hosted.py answer $RUNS/gemma-r1 --model google/gemma-4-31b-it --concurrency 8 --attempts 6`;
   the second run adds `OPENROUTER_TEMPERATURE=default`. Rerun a step to retry what
   failed; it skips finished answers. An answer that still fails is reported, not
   filled in.
2. Commit to `eval/<name>`: `opus` with `cases/`, `ctx/`, `plan.json` and the build
   files, and each hosted run with `cases/`, `plan.json`, `answers/` and
   `hosted.json`, without `ctx/`. Push the branch. It is never merged: the
   [session playbook](../docs/session-playbook.md) keeps eval outputs out of the main
   line, and the cloud routine reads the runs from this branch.

## 6. Opus answers and judging (cloud)

1. Find the routine `Agents-eval` with `RemoteTrigger` `list`. When it is missing,
   create it exactly as [eval routine](../docs/cloud-runs.md#eval-routine) says,
   clear its connectors and read it back.
2. Fire it three times with `RemoteTrigger` `run`, so the sessions run in parallel.
   The routine judges the runs it answers unless the text names `judge:`, so the
   first fire answers the Opus run and judges it:
   - `branch: eval/<name>` / `answer: evals/ablation/runs/<name>/opus` / `name: <name>-opus`;
   - `branch: eval/<name>` / `judge: evals/ablation/runs/<name>/gemma-r1 evals/ablation/runs/<name>/gemma-r2` / `name: <name>-gemma`;
   - `branch: eval/<name>` / `judge: evals/ablation/runs/<name>/qwen-r1 evals/ablation/runs/<name>/qwen-r2` / `name: <name>-qwen`.
3. Wait for the branches `claude/eval-<name>-opus`, `-gemma` and `-qwen` with a
   monitor that polls `git ls-remote` every minute; do not sleep in the foreground.
   When a branch is late, read `RemoteTrigger` `list_runs` and `get_run_log`. Fix the
   cause and fire again only after reading what the failed run left.
4. Fetch each branch and copy its run files into the local run directories.

## 7. Retrieval and routing metrics (when they apply)

Run these in each arm's worktree with its settings, one model loaded at a time.
After changing `EMBEDDING_MODEL` in a worktree, rebuild its stores with
`python -m src.reindex`.

1. Language coverage: when the hypothesis concerns a language with fewer than about
   30 labeled queries in `evals/datasets/routing.jsonl`, first write
   `evals/datasets/routing_<lang>.jsonl` with inline `query`, `language` and
   `expected_agent` fields (the loader reads inline texts) and check the labels as
   in step 3.
2. Skills and implants:
   `python -m evals.runners.run_retrieval --expected-from-agent --json`, then again with
   `--dataset evals/datasets/routing_<lang>.jsonl` when a set for the hypothesis
   language exists (step 1). `--dataset` replaces the default set, so the two runs
   report the base set and the language set separately.
3. Semantic cache routing:
   `python -m evals.runners.run_cache_routing --dataset evals/datasets/routing.jsonl --json`,
   adding `--dataset evals/datasets/routing_<lang>.jsonl` when a set for the
   hypothesis language exists (step 1). The report counts each dataset's rows,
   drift, fetch errors and repeated queries; explain any it lists.
   Compare models at equal coverage, because similarity scales differ, and propose
   the `ROUTER_SIMILARITY_THRESHOLD` that keeps the baseline's precision. The router
   needs a similarity above its threshold, so set the threshold just below the
   cutoff similarity that the report gives for that coverage.
4. An embedding model needs its query and passage prompts from its model card in
   `src/engine/embedding_prompts.py` before any measurement; without them the comparison is
   not fair to it.

## 8. Analyze

1. Run `python evals/ablation/aggregate.py` on every run directory (with
   `--allow-partial` only for gaps the report names). Then run
   `python evals/ablation/compare.py` once per model: the Gemma runs together, the
   Qwen runs together, Opus alone.
2. Read the `control` row first. A model whose controls move as much as its effect
   gives no result.
3. Read the 3–5 most decisive pairs (robust wins and losses, largest margins) and
   check the judge's reasons against the answers. Report judge errors you find.
4. Recommend:
   - ship when no model is worse with p < 0.05 and the models the change targets
     gain;
   - do not ship when any model is significantly worse;
   - otherwise call it inconclusive and name what would decide it (more cases, more
     runs, another group).

## 9. Record and report

1. Write `docs/<name>-eval-results.md` like
   [docs/english-pivot-eval-results.md](../docs/english-pivot-eval-results.md):
   question, method with a settings table, results from `compare.py`, retrieval
   metrics when measured, reading and limits. Link it under recorded results in
   [docs/README.md](../docs/README.md).
2. Commit it on the change's branch when the change ships, otherwise on
   `eval/<name>`. Push and open a pull request; never merge it.
3. Keep `eval/<name>` and the `claude/eval-<name>-*` branches until the owner decides;
   offer the archive step from the [runbook](../evals/ablation/README.md).
4. Report in the invocation language: the recommendation and its evidence, the
   `compare.py` tables per model, retrieval metrics when measured, OpenRouter spend
   (`usage_daily` now minus at step 1) and cloud runs used, links to the PR, branches
   and routine runs, and every gap or unverified point.

## Validation

- The case set is fact-checked before any answer is judged. A case changed after
  answering is answered and judged again.
- Every run's `plan.json` has the same `ctx_sha256` per token as `base`.
- `aggregate.py` exits 0, or the report names each gap; `compare.py` shows no
  unexplained missing verdicts.
- Controls stay near zero, or the model's result is reported as noise.
- OpenRouter spend stays within the budget.
- When a check cannot run (an endpoint down, the routine refused), report it and
  what it leaves open; never fill a gap with an estimate.

## Done when

- Every planned run has answers and verdicts, or the report names each gap and why.
- The results document is pushed, and a pull request is open and unmerged.
- The final report gives the recommendation with its evidence, the spend and the
  links.
