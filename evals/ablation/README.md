# Component ablation sweep

Measures what each rule, skill and implant changes in real answers. For every
component: two in-scope cases (ru + en), one answer with the component and one
without it, and a blind pairwise judge in both orders. That gives 4 verdicts per
component. This is a screening pass: components with a consistent
with/without gap get a closer look afterwards.

The 2026-09-25 pilot already covered `rule-no-fabrication`,
`skill-content-structure`, `implant-regression-first` and
`implant-iteration-budget`. `components.json` lists the other 129 in 13
batches of 10.

## Running one batch (cloud session)

The request names a batch (`batch 3`) or explicit ids (`ids: skill-a implant-b`), and
may set a run name and a case count (`name: retest-a`, `cases: 6`; default 2).
Every step runs from the repository root. The Agents-Core MCP server is not
available here and is not needed: do not route. Answer the user from these steps.

1. **Checkout and install**
   ```bash
   git fetch origin claude/ablation-sweep && git checkout claude/ablation-sweep
   python -m venv .venv && . .venv/bin/activate && pip install -q -r requirements.txt
   ```
   Cloud egress blocks huggingface.co, so fetch the embedding model from
   fastembed's Google Cloud Storage copy and point the embedder at it. Export
   the variable in every shell that runs `build_contexts.py`:
   ```bash
   mkdir -p /tmp/e5 && curl -sSfL https://storage.googleapis.com/qdrant-fastembed/fast-multilingual-e5-large.tar.gz | tar xz -C /tmp/e5
   find /tmp/e5 -name '._*' -delete
   export AGENTS_MODEL_PATH=/tmp/e5/fast-multilingual-e5-large
   ```
2. **Pick the components and the run directory**
   ```bash
   IDS=$(python evals/ablation/components.py --batch 3)     # or the explicit ids
   RUN=$(pwd)/evals/ablation/runs/batch-03                  # or runs/smoke-<date>
   mkdir -p $RUN/cases $RUN/answers
   ```
3. **Cases.** Run the Workflow tool with
   `scriptPath: evals/ablation/workflows/cases.js` and
   `args: {"run_dir": "<RUN>", "ids": [<IDS as JSON strings>], "n_cases": <case count>}`.
   Afterwards `ls $RUN/cases` should list one JSON file per component.
4. **Contexts.** `python evals/ablation/build_contexts.py $RUN`. The first call
   builds the vector stores and downloads the embedding model, which takes a few
   minutes. Check `$RUN/build_errors.json`.
5. **Answers.** Get the tokens with
   `python -c "import json;print(json.dumps(sorted(json.load(open('$RUN/plan.json')))))"`,
   then run Workflow with `scriptPath: evals/ablation/workflows/answers.js` and
   `args: {"run_dir": "<RUN>", "tokens": <that list>}`.
6. **Judges.** Run `python evals/ablation/build_judges.py $RUN`. Get the stems with
   `python -c "import json;print(json.dumps(sorted(json.load(open('$RUN/judge_plan.json')))))"`,
   then run Workflow with `scriptPath: evals/ablation/workflows/judges.js` and
   `args: {"run_dir": "<RUN>", "files": <that list>}`.
7. **Summary.** `python evals/ablation/aggregate.py $RUN` writes
   `$RUN/results.json` and `$RUN/RESULTS.md`.
8. **Publish.** Commit `$RUN` without `ctx/` (the contexts can be rebuilt from the
   cases) to a new branch `claude/ablation-<run name>` and push it:
   ```bash
   git checkout -b claude/ablation-batch-03
   git add $RUN/cases $RUN/answers $RUN/judge $RUN/*.json $RUN/RESULTS.md
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
