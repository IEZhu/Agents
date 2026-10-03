# Component ablation sweep

Measures what each rule, skill and implant changes in real answers. For every
component: N in-scope cases (2 by default, the first half in Russian and the rest
in English), one answer with the component and one without it, and a blind
pairwise judge in both orders, so 2N verdicts per component (4 in the sweep, 12
in the 6-case re-tests). This is a screening pass: components with a consistent
with/without gap get a closer look afterwards.

For rules and skills the two arms differ only in that component; the rest of the
prompt is what production builds. Implants are tested alone: the with arm gets
exactly that implant and the without arm gets none, so other implants production
would retrieve are left out of both.

The 2026-09-25 pilot already covered `rule-no-fabrication`,
`skill-content-structure`, `implant-regression-first` and
`implant-iteration-budget`. `components.json` lists the other 129 in 13
batches of 10. Results are in `RESULTS.md`; the raw runs of the sweep, its
re-tests and the cloud pilot are on branch `archive/ablation-runs-2026-09`.
`components.json` is the snapshot that sweep ran, so it still lists components
removed since (`skill-token-economy`, #94); a rerun on `main` records those in
`build_errors.json` as "not in store". `components.py --write --force` regenerates it;
without `--force` it refuses to replace the snapshot. `runs/SOURCES.md` on the archive
branch names the commit each archived run was built from.

## Running one batch (cloud session)

The request names a batch (`batch 3`) or explicit ids (`ids: skill-a implant-b`), and
may set a run name and a case count (`name: retest-a`, `cases: 6`; default 2).
Every step runs from the repository root. Do not route through Agents-Core, even
where the environment connects it: this run is a measurement. Answer the user from
these steps.

1. **Checkout and install**
   ```bash
   git fetch origin main && git checkout main && git merge --ff-only origin/main
   python -m venv .venv && . .venv/bin/activate && pip install -q -r requirements.txt
   ```
   The embedding model (`intfloat/multilingual-e5-large`, set by
   `build_contexts.py`) downloads from Hugging Face on first use, so run the
   batch in an environment that allows it
   ([cloud environment setup](../../docs/cloud-runs.md#cloud-environment-with-agents-core));
   the default cloud environment blocks huggingface.co. Leave `AGENTS_MODEL_PATH`
   unset. The 2026-09 runs fetched fastembed's Google Cloud Storage copy instead,
   which answered `403 AccessDenied` on 2026-10-03.
2. **Pick the components and the run directory**
   ```bash
   IDS=$(python evals/ablation/components.py --batch 3)     # or the explicit ids
   RUN=$(pwd)/evals/ablation/runs/batch-03                  # or runs/smoke-<date>
   mkdir -p $RUN/cases $RUN/answers
   printf '%s\n' $IDS > $RUN/ids.txt                     # the contexts step checks it
   ```
3. **Cases.** Run the Workflow tool with
   `scriptPath: evals/ablation/workflows/cases.js` and
   `args: {"run_dir": "<RUN>", "ids": [<IDS as JSON strings>], "n_cases": <case count>}`.
   Afterwards `ls $RUN/cases` should list one JSON file per component.
4. **Contexts.** `python evals/ablation/build_contexts.py $RUN`. The first call
   builds the vector stores and downloads the embedding model, which takes a few
   minutes. It stops if a component in `ids.txt` has no cases file, or a cases file
   lacks the checker's `"checked": true` or an empty one gives no `untestable`
   reason; rerun step 3 for those. Check `$RUN/build_errors.json`. `$RUN/build_meta.json` records the
   commit the contexts were built from. On a rebuild, an answer survives only if its
   context is known to be unchanged; in a run published before `plan.json` recorded
   context hashes, every answer is deleted and answered again.
5. **Answers.** Get the tokens with
   `python -c "import json;print(json.dumps(sorted(json.load(open('$RUN/plan.json')))))"`,
   then run Workflow with `scriptPath: evals/ablation/workflows/answers.js` and
   `args: {"run_dir": "<RUN>", "tokens": <that list>}`.
6. **Judges.** Run `python evals/ablation/build_judges.py $RUN`. It exits 1 when an
   answer is missing (listed in `$RUN/judge_skipped.json`): rerun step 5 for those
   tokens, or pass `--allow-partial` and say so in the report. Get the stems with
   `python -c "import json;print(json.dumps(sorted(json.load(open('$RUN/judge_plan.json')))))"`,
   then run Workflow with `scriptPath: evals/ablation/workflows/judges.js` and
   `args: {"run_dir": "<RUN>", "files": <that list>}`.
7. **Summary.** `python evals/ablation/aggregate.py $RUN` writes
   `$RUN/results.json` and `$RUN/RESULTS.md`. It exits 1 when a verdict is missing
   or malformed, an answer pair was skipped, or a case's context was not built
   (`build_errors.json`); `results.json` lists them under `missing`. Rerun the step
   that produced the gap, or pass `--allow-partial` and say so in the report.
8. **Publish.** Commit `$RUN` without `ctx/` to a new branch
   `claude/ablation-<run name>` and push it. The contexts are rebuilt by running
   step 4 at the commit in `build_meta.json`; components removed from `main` since
   then are still present there.
   ```bash
   git checkout -b claude/ablation-batch-03
   git add $RUN/cases $RUN/answers $RUN/judge $RUN/*.json $RUN/ids.txt $RUN/RESULTS.md
   git commit -m "eval(ablation): batch-03 results" && git push -u origin HEAD
   ```

If the Workflow tool is unavailable, run the same prompts with parallel Agent
calls instead, at most 16 at a time.

If one step leaves gaps, rerun it for just the missing items: components without
a cases file, tokens without an answer file, stems without a verdict file. Do
not start the next step on a partial set without saying so in the final report.

## Final report

End with:
- the `RESULTS.md` table;
- the counts of components, cases, answers and verdicts, and anything missing and why;
- components the checker marked untestable;
- the pushed branch name.

## Prepared case sets

A case set written by hand replaces step 3 for its component: copy it to
`$RUN/cases/<component>.json` after step 2 and continue with step 4. The file uses
the same format and carries `"checked": true` with `checker_notes` saying who wrote
and reviewed it. `evals/datasets/english_pivot_cases.json` is one, for
`rule-english-pivot`: 19 Russian cases, 2 English requests with Russian
instructions and 3 English controls.

## Hosted models (OpenRouter)

Steps 5 and 6 can run on a model served by OpenRouter instead of Claude Code
agents, from the same `ctx/` and judge files, so one set of contexts compares
several models. Build the contexts once (steps 2–4), copy the run directory per
answer model, and run, with `OPENROUTER_API_KEY` set and the endpoints pinned:

```bash
export OPENROUTER_PROVIDER=deepinfra/bf16,google-ai-studio OPENROUTER_REASONING=medium
python evals/ablation/hosted.py answer $RUN --model qwen/qwen3.8-27b --concurrency 8
python evals/ablation/build_judges.py $RUN
python evals/ablation/hosted.py judge $RUN --model google/gemini-3.8-flash --concurrency 8
python evals/ablation/aggregate.py $RUN
```

`hosted.py` sends the context's operating instructions as the system prompt and the
conversation as chat turns. Its judge gets the criteria, verdict example and allowed
values of `workflows/judges.js`, without tools, so it cannot check facts on the web
as the cloud judge can. `$RUN/hosted.json` records each step's model and request
settings, and a step whose existing outputs used other settings is refused.

## Two revisions or settings (`--arm`)

A change that is not a single component, such as a rewritten rule, an edited agent
prompt or another embedding model, is compared by building each arm where it lives.
The case file's `component` is then the experiment's name (for example
`embed-harrier-270m`):

```bash
# in a checkout of the baseline, for example a worktree of origin/main
python evals/ablation/build_contexts.py $RUN --arm without
# in a checkout of the candidate, or with the candidate's settings
EMBEDDING_MODEL=<candidate> python evals/ablation/build_contexts.py $RUN --arm with
```

Each run keeps the other arm's entries. A case whose two contexts come out equal is
dropped from both arms and listed in `build_errors.json` as "arms identical": the
change does not reach it, so `aggregate.py` needs `--allow-partial` and the report
says how many cases that was. The entry keeps the context's hash, so rebuilding
either arm with the same context leaves the case out again. A rebuilt arm whose
context changed is written and listed as "other arm not built" until the other arm
is rebuilt too. The same happens when a case's history or message changed since the
other arm was built: that arm's context is dropped, since both arms must answer
one conversation. Arms with different embedding models need their own
vector stores: build each in its own checkout, which has its own `data/`. Never
build in the live installation.

## Answering and judging prepared runs (cloud)

The eval routine ([cloud runs](../../docs/cloud-runs.md#eval-routine)) follows this
section for an experiment whose contexts, and possibly other models' answers, are
already committed on a branch. It needs no install and no embedding model:
`build_judges.py`, `aggregate.py` and `compare.py` use only the standard library.
The request names `branch:`, the run directories Opus answers (`answer:`), the run
directories to judge (`judge:`, by default the `answer:` ones) and `name:`. Every
step runs from the repository root.

1. `git fetch origin <branch> && git checkout <branch>`. Each `answer:` run needs
   `cases/`, `ctx/` and `plan.json`; each `judge:` run needs `cases/`, `plan.json`
   and `answers/` (or is also an `answer:` run).
2. **Answers.** For each `answer:` run, `mkdir -p $RUN/answers` and list the tokens
   still without an answer:
   ```bash
   python -c "import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); print(json.dumps(sorted(t for t in json.load(open(r/'plan.json')) if not (r/'answers'/f'{t}.md').exists())))" $RUN
   ```
   Run Workflow with `scriptPath: evals/ablation/workflows/answers.js` and
   `args: {"run_dir": "<absolute RUN>", "tokens": <that list>}`.
3. **Judges.** For each `judge:` run, `python evals/ablation/build_judges.py $RUN`.
   It exits 1 when an answer is missing: rerun step 2 for those tokens, or pass
   `--allow-partial` and say so in the report. List the stems still without a verdict:
   ```bash
   python -c "import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); print(json.dumps(sorted(s for s in json.load(open(r/'judge_plan.json')) if not (r/'judge'/f'{s}.verdict.json').exists())))" $RUN
   ```
   Run Workflow with `scriptPath: evals/ablation/workflows/judges.js` and
   `args: {"run_dir": "<absolute RUN>", "files": <that list>}`.
4. **Summary.** `python evals/ablation/aggregate.py $RUN` for each `judge:` run
   (add `--allow-partial` only for gaps the report names).
5. **Publish.** Commit, for every run, only `answers/`, `judge/`, `judge_plan.json`,
   `judge_skipped.json`, `results.json` and `RESULTS.md`, on a new branch
   `claude/eval-<name>`, and push it:
   ```bash
   git checkout -b claude/eval-<name>
   git add <for each run: $RUN/answers $RUN/judge $RUN/judge_plan.json $RUN/judge_skipped.json $RUN/results.json $RUN/RESULTS.md>
   git commit -m "eval: <name> answers and verdicts" && git push -u origin HEAD
   ```
6. **Final report.** Each run's `RESULTS.md` table, the counts of answers and
   verdicts written, anything missing and why, and the pushed branch.

## Comparing runs

`python evals/ablation/compare.py RUN [RUN ...]` reads the verdicts of one answer
model's runs. It prints each run's verdict counts, net verdicts per case `group`
and an exact two-sided sign test over the cases where the change applies, across
all the runs given. Cases in group `control` are reported separately as the noise
floor. `--json` gives the same as data.
