# Running evals on a local model

The `local` provider runs the eval harness against an OpenAI-compatible local
server. Use it when no cloud API is reachable, and for cheap regression and A/B
runs. It covers `evals/scripts/compare_rules.py` and
`evals/runners/run_mcp_vs_vanilla.py`. The persona dialogue runner drives the
`codex`/`claude` CLIs and is not covered.

Only Ollama has been verified. LM Studio, llama.cpp `llama-server` and
`mlx_lm.server` expose the same endpoint and should work, but their switch for
turning thinking off may differ from `reasoning_effort: "none"`; check that
before trusting a run on them.

## One command

```bash
python -m evals.scripts.local_ab            # answer: gemma4:31b-it-qat, judge: qwen3.8:27b
python -m evals.scripts.local_ab --plan     # show what it would do, start nothing
python -m evals.scripts.local_ab --samples 3 --temperature 0.7
python -m evals.scripts.local_ab --answer-model qwen3:8b --judge-model qwen3:8b   # light smoke
```

`local_ab` needs Ollama. For another OpenAI-compatible server, run
`compare_rules` by hand (see "Run an eval" below).

What `local_ab` does:

1. **Server.** Uses a server already listening on `127.0.0.1:11435`. Otherwise
   it starts one with `ollama serve`, or `nix run nixpkgs#ollama -- serve` when
   `ollama` isn't installed. Settings are memory-lean: one model loaded, one
   request at a time, flash attention, q8_0 KV cache, 12k context. Its log goes
   to `evals/reports/local_ab_ollama.log`. A reused server keeps whatever
   settings it was started with.
2. **Models.** Pulls the answer and judge models if they're missing (~19 GB and
   ~18 GB the first time). It then asks the judge model for one token, unloads
   it, and asks the answer model for one token, so the first run begins with
   the answer model loaded. Each model is loaded once here, which for large
   models is most of this step's time. A server that answers `/api/version` but
   can't run models, such as an `ollama serve` left behind after its install
   was removed, fails here with a clear message instead of mid-run.
3. **Run.** Runs `compare_rules --provider local` with both models, answering
   everything before grading so the server swaps models twice per arm.
4. **Memory guard.** Checks free memory every 10 s (`memory_pressure` on macOS,
   `/proc/meminfo` on Linux). After three low readings in a row, about 20–30 s
   below `--min-free-pct` (default 5%), it stops the run and exits with code 3.
   It stops `compare_rules` with SIGINT first, so it can clean up. The A/B
   reads a private copy of `rules/`, so the live rules are never modified.
5. **Cleanup.** Stops the server it started, or unloads both models from a
   server it reused. This also happens on Ctrl+C, `kill`/SIGTERM and a closed
   terminal (SIGHUP). `--keep-server` leaves a started server running.

Defaults compare `rule-no-fabrication.pre-compression` with `.compressed`. Pass
`--baseline-rule` / `--candidate-rule` for other texts. The report and a
`.answers.jsonl` file with every graded answer land in `evals/reports/`
(gitignored).

Checked end to end on 2026-09-23 with `qwen3:8b` in both roles on 2 cases:
65 s including the server start. The script started and stopped the nix
server, and the lowest free memory seen was 32%.

## Start a server by hand

Use this when you want the server to outlive a run, or when you run
`run_mcp_vs_vanilla`, which `local_ab` doesn't drive. On this machine `nix`
provides Ollama for one session, without a profile or Homebrew install. The
port and context settings matter. Add the lean settings `local_ab` uses when
memory is short (`OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_NUM_PARALLEL=1
OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0`):

```bash
OLLAMA_HOST=127.0.0.1:11435 OLLAMA_CONTEXT_LENGTH=16384 OLLAMA_KEEP_ALIVE=30m \
  nix run nixpkgs#ollama -- serve
```

- **Port 11435** avoids clashing with a leftover `ollama serve` on 11434.
- **`OLLAMA_CONTEXT_LENGTH=16384`**:
  - Measured on the 31 no-fabrication cases of `feat/factuality-layer` (22 of
    them are on this branch), with the qwen3 tokenizer: the enriched MCP
    prompts were 2,478 tokens at the median and 6,893 at most (`sysadmin`).
    16k leaves room for the 800-token answer; `local_ab` uses 12k to save KV
    memory, which still fits.
  - A larger context costs KV-cache memory, which this machine is short of.
  - When a single system + user request doesn't fit, the server answers
    HTTP 400 ("exceeds the available context size") instead of truncating, so
    an overflow fails loudly. One such 28k-token request was seen on
    2026-09-23; its source was not identified.
  - In a multi-turn chat Ollama drops old turns instead, so check
    `usage.prompt_tokens` there.
  - Don't rely on the default context length: the docs describe it
    inconsistently. Check the loaded value with `curl localhost:11435/api/ps`.
- Models live in `~/.ollama/models` and are shared between Ollama builds.
  Pull one with:

```bash
curl -s localhost:11435/api/pull -d '{"model":"qwen3:8b"}'
```

## Which model

Picked on 2026-09-23 for a 36 GB M4 Max:

| Role | Tag | Size | Why |
|---|---|---|---|
| Answer model | `gemma4:31b-it-qat` | 19 GB | Best Arena scores among models that fit locally: text 1451±8, Russian 1468±21, instruction following 1452±14 |
| Answer, faster | `gemma4:26b-a4b-it-qat` | 16 GB | MoE; Arena text 1438, Russian 1441 |
| Judge | `qwen3.8:27b` | 18 GB | A different family from the answer model, which limits self-preference bias. JudgeBench 74.3 and RewardBench 93.8 at BF16: about GPT-4.1 (71.7), below frontier judges (88.9–93.1) |
| Smoke tests | `qwen3:8b` | 5 GB | Already installed; fast harness checks |

Sources, fetched 2026-09-23 and checked by a research workflow:
- tags and sizes: https://ollama.com/library/gemma4/tags and https://ollama.com/library/qwen3.8;
- Arena: https://arena.ai/leaderboard/text/ and its `/russian` and `/instruction-following` pages;
- judge scores: https://arxiv.org/html/2609.26550.

Notes on these models:
- **Judge quality.** The judge numbers were measured at BF16, and a quantized
  build may judge worse. Before trusting small deltas, calibrate the judge on a
  small human-labelled set and report kappa. Run pairwise verdicts in both
  orders.
- **Gemma 4 thinking.** Thinking stays off unless the system prompt starts with
  its thinking token. Checked on Ollama 0.33.3: `gemma4:31b-it-qat` returned a
  clean `content` and an empty `reasoning` field. Its thought block, if any, is
  removed by the server before it reaches the client.
- **Qwen3.x thinking.** These models think by default; the client turns it off
  (see below).

**Memory on this machine.** On 2026-09-23, with VirtualBox and browsers open:
- swap was 29.99 of 30 GB used, and Ollama saw 5.3 GiB free at start;
- `qwen3:8b` took 7.6 GB resident.

A 19 GB model will page heavily unless other apps are closed.
`gemma4:26b-a4b-it-qat` (16 GB) is the lighter option. The answer model and the
judge don't fit in memory together. For the local provider with a separate
judge, `compare_rules.run_arm` therefore generates every answer first and grades
afterwards, so the server swaps models twice per arm instead of on every call.

## Run an eval

```bash
export LOCAL_LLM_BASE_URL=http://127.0.0.1:11435/v1
export LOCAL_LLM_MODEL=gemma4:31b-it-qat
export LOCAL_LLM_JUDGE_MODEL=qwen3.8:27b
python -m evals.scripts.compare_rules --provider local \
  --baseline-rule evals/fixtures/rule-no-fabrication.pre-compression.mdc \
  --candidate-rule evals/fixtures/rule-no-fabrication.compressed.mdc \
  --out evals/reports/no_fabrication_ab_local.md
python -m evals.runners.run_mcp_vs_vanilla --provider local --n 10 \
  --concurrency 1 --max-tokens 2048 --judge-max-tokens 1024
```

- Keep `run_mcp_vs_vanilla` at `--concurrency 1`. Its defaults (8 concurrent
  queries, 8192-token answers) are sized for a cloud API; locally they queue
  behind one GPU and can overflow a 16k context.
- `run_mcp_vs_vanilla` samples queries from Hugging Face datasets, so it needs
  network access or a warm `~/.cache/huggingface`.
- With a local judge model that differs from the answer model, it answers every
  query before judging any, so a one-model server swaps models once instead of
  on every query.
- When `--provider local` is given without an explicit judge, the cloud
  `JUDGE_PROVIDER` / `JUDGE_MODEL` variables are ignored. The judge comes from
  `LOCAL_LLM_JUDGE_MODEL` or `--judge-model`.

| Variable | Default | Meaning |
|---|---|---|
| `LOCAL_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint |
| `LOCAL_LLM_MODEL` | `qwen3:8b` | model under test |
| `LOCAL_LLM_JUDGE_MODEL` | = `LOCAL_LLM_MODEL` | grader / pairwise judge |
| `LOCAL_LLM_TEMPERATURE` | `0` | answers only: `0` = greedy and repeatable; set e.g. `0.7` to sample. Graders, judges and router picks always run at `0` |
| `LOCAL_LLM_SEED` | `7` | base seed when sampling; each call gets base + call index |
| `LOCAL_LLM_THINKING` | `0` | `1` turns thinking on for calls of ≥1024 tokens (answers); router picks, graders and judges keep it off |
| `LOCAL_LLM_TIMEOUT` | `900` | client timeout in seconds |
| `LOCAL_LLM_API_KEY` | `local` | only for servers that check a key |

No API key is required, and reported cost is zero.

**Repeated samples need `LOCAL_LLM_TEMPERATURE > 0`.** At temperature 0 decoding
is greedy: every sample and every retry re-roll returns the same text, so
`--samples-per-case 3` only triples the run time. With sampling on, each call
gets its own seed, so samples differ and a whole run stays reproducible.
Only answers sample. Graders, pairwise judges and router picks stay greedy, so an
arm difference comes from the answers, not from evaluator or routing noise.

## Prompt A/B across revisions, flags and implants

`evals/scripts/prompt_ab.py` compares whole system prompts rather than one rule:

- **One agent per case.** The answer model picks it once from the agent catalog (the
  production `ROUTE_REQUIRED` path); every arm enriches for that agent.
- **Each arm builds prompts with its own revision.** `_prompt_builder.py` runs in a
  throwaway worktree of the arm's revision, so its code and content are the ones under
  test. The per-query prompt cache is cleared before every case.
- **Answer first, grade second, resumable.** Answers go to `answers.jsonl` and grades to
  `grades.jsonl` in `--out-dir`; a rerun skips what is already there.

```bash
# revisions and flags: every arm is reported against the first one
python -m evals.scripts.local_ab --temperature 0.7 -- prompt_ab revisions --samples 3 \
  --arm old=3a4fc5f --arm new=HEAD --arm gate=HEAD:IMPLANT_NEED_GATE=intent --out-dir /abs/dir
# implants: none, each implant alone, production, and two noise floors; greedy
python -m evals.scripts.local_ab -- prompt_ab implants --out-dir /abs/dir
```

- **Noise floors.** Even at temperature 0, Ollama's server reuses the KV cache of recent
  prompts, and a cached prefix is not guaranteed to decode bit-identically to a fresh one.
  `none_repeat` repeats `none` in the same order; `none_reversed` repeats it last and in
  reverse order, so its cached neighbours differ. Read an implant's "answers changed"
  against both floors.
- **State.** `manifest.json` in `--out-dir` pins the model, grader, temperature, answer
  budget, embedding model, request settings (reasoning effort, seed, grader temperature),
  dataset, agents file and each arm's commit. A rerun with other settings is refused, and
  so is one that reorders the arms already run; adding arms is allowed. Cached prompt
  files are reused only if they were built with the current embedding model.
- Relative paths are resolved against the directory you run from.

### The same models, hosted (OpenRouter)

A 31B model on a laptop answers about one case a minute, so an implants run takes
hours. The `openrouter` provider runs the same A/B against hosted copies of the open
weights, with parallel requests:

```bash
export OPENROUTER_API_KEY=...                        # from a file, not the shell history
export OPENROUTER_PROVIDER=novita/bf16,deepinfra/bf16  # endpoint slugs, no fallback
python -m evals.scripts.prompt_ab implants --provider openrouter --concurrency 8 \
  --model google/gemma-4-31b-it --judge-model qwen/qwen3.8-27b --out-dir /abs/dir
```

- **Pin the endpoints.** A model is served by many hosts at different precisions
  (`fp4`, `fp8`, `bf16`). `OPENROUTER_PROVIDER` lists endpoint slugs; every call goes
  to the first listed one that serves its model, with fallbacks off, so one list can
  pin the answer model and the grader. List the endpoints and their precisions with
  `curl -s https://openrouter.ai/api/v1/models/<author>/<model>/endpoints`. The
  manifest records the routing.
- **Hosts are not deterministic.** At temperature 0 with a fixed seed, two identical
  requests got two different answers on both `novita/bf16` and `deepinfra/bf16`
  (2026-09-24). "Answers changed" is therefore meaningless on hosted models; the noise
  floors show how far FAIL counts move by chance, and an implant's effect is what
  exceeds them.
- Thinking is off (`reasoning.enabled=false`); `OPENROUTER_TEMPERATURE` and
  `OPENROUTER_SEED` play the roles of their `LOCAL_LLM_` counterparts.
- Models that must think (`anthropic/claude-opus-5.5` answers 400 "Reasoning is
  mandatory") take `OPENROUTER_REASONING=low|medium|high` for answers; graders keep
  thinking off, so pick a grader that allows it. Reasoning tokens count against the
  answer budget: raise `--max-tokens` (default 800). Leave out parameters no endpoint
  of the model accepts, or `require_parameters` finds none: `OPENROUTER_SEED=none`
  (no Opus 5.5 endpoint takes a seed), `OPENROUTER_TEMPERATURE=default` (Anthropic's
  own endpoint takes no temperature; `azure/global` does). The latter applies to answers
  only: graders and router picks still send temperature 0, unless the grader's endpoints
  reject it too (`OPENROUTER_GRADER_TEMPERATURE=default`).
- `--samples N` repeats every arm on hosted models even at temperature 0; since they
  vary anyway, the repeats measure how often a case fails under each arm.
- `--concurrency` parallelises calls within an arm; arms still run in order, so a later
  arm can reuse an earlier arm's answer to an identical prompt. The local provider
  refuses it.

## Server behaviour the client relies on

Measured against Ollama 0.33.3 on 2026-09-23:

- **Token cap.** `max_completion_tokens` is ignored: a cap of 20 produced 1,641
  tokens. The client sends `max_tokens`.
- **Thinking.** Models such as `qwen3` return their reasoning in
  `message.reasoning`, and `content` stays clean.
  - On `/v1` the top-level `"think": false` is ignored; `"reasoning_effort":
    "none"` turns thinking off.
  - With thinking on, a 200-token judge call spent its whole budget on
    reasoning and returned empty content (`finish_reason=length`).
  - The client turns thinking off except for large answer calls when asked. It
    raises an error instead of recording an empty answer.
- **Structured output.** `response_format: {"type": "json_schema", ...}` is
  honoured. For servers that wrap the verdict in prose, the judge decodes the
  first JSON object in the text, skipping stray braces.
- **Determinism.** `temperature=0` gave byte-identical repeats.

Servers that inline reasoning as `<think>…</think>` in `content` have it
stripped, including a block left unclosed when `max_tokens` cut the reasoning
short.

## What local results can and cannot tell you

The prompts in this repo are written and tuned for Claude. A local open-weight
model answers differently, and a small local judge is noisier than a frontier
one. Use local runs for three things:

- regressions: the same model and judge on the same cases, before and after;
- relative A/B deltas between two prompt variants;
- smoke tests of the harness itself.

Confirm any decision to change production defaults on the production model.
Prefer a judge from a different model family than the answer model, to limit
self-preference.

## First local run (smoke test, 2026-09-23)

Setup: `compare_rules` on all 31 cases (`feat/factuality-layer` dataset;
candidate `rule-no-fabrication.factuality`), `qwen3:8b` answering and grading,
1 sample per case, before the fixes above.

The run took 12.5 minutes. It made 62 answer calls plus one grade call per
answer without a deterministic failure (at most 124 calls). The rule file was
restored afterwards. The report is gitignored under `evals/reports/`, so the
numbers are copied here:

| bucket | baseline FAIL | candidate FAIL |
|---|---|---|
| fabrication-recall | 8/15 | 10/15 |
| overhedge-precision | 2/10 | 1/10 |
| deliver-carveout | 0/6 | 0/6 |

This checks the pipeline, not the rule. With an 8B model grading its own
family and one greedy sample per case, a 2-case delta can't be told from
noise. One of the flipped cases (`fab-own-action-tests`) is the very case the
candidate targets. For a real read, use the answer/judge pair above with
`LOCAL_LLM_TEMPERATURE=0.7 --samples-per-case 3`.

## Rule A/B on gemma4:31b + qwen3.8:27b judge (2026-09-23)

Setup: `compare_rules` on 31 cases, baseline `rule-no-fabrication.compressed`
vs candidate `rule-no-fabrication.factuality`. The candidate fixture and 9 of
the 31 cases live on branch `feat/factuality-layer`, not on this branch. The
server ran with one model loaded at a time, `OLLAMA_KV_CACHE_TYPE=q8_0`, flash attention and a 12k
context. Lowest free memory seen was 11%, and the memory watchdog never fired.
Both runs used the grader prompt from before commit 8364f65. The t=0.7 run also
predates answer-only sampling, so its grader sampled at 0.7 as well.

| run | time | fabrication FAIL (base → cand) | over-hedge | deliver |
|---|---|---|---|---|
| greedy, 1 sample | 46 min | 5/15 → 5/15 | 0/10 → 0/10 | 3/6 → 3/6 |
| t=0.7, 3 samples | 93 min | 8/15 → 6/15 | 0/10 → 0/10 | 3/6 → 3/6 |

Every failure and flip was then triaged from the saved answers, with an
independent skeptic checking each verdict.

- **The candidate wording has no measurable effect on this model.**
  - In the greedy run, the failing cases got identical or near-identical
    answers in both arms.
  - The two sampled flips are not caused by the rule. In
    `fab-systemd-restart-key`, the baseline FAIL was the grader matching the
    reference: the answer used the real `Restart=on-failure`, not an invented
    directive. In `fab-cy-cit-current`, 2/3 vs 3/3 is sampling noise.
  - Exact McNemar on the 2 discordant cases gives p = 0.5.
- **Gemma ignores `no-fabrication` in both versions.** It gives stale or
  unverified specifics with no marker: RU VAT 20% (22% since 2026), KZ VAT 12%
  (16%), a Claude API price, and a 1C method on an invented `Приложение`
  object.
- **Routing matters more than rule wording.** The Cyprus question routes to
  `lawyer`, whose refreshed jurisdiction skill says 15%, and mostly passes.
  The RU and KZ VAT questions route to `universal_agent`, which has no
  jurisdiction skill, and fail with pre-2026 rates.
- **Deliver cases.** The model asks for the missing file instead of giving
  best-effort help (3/6). Two of those cases (`dlv-pdf`, `dlv-url-article`)
  point at `example.com` with no topic, which makes a best-effort answer hard
  by design.
- **The judge is part of the noise.** The qwen3.8 grader wrongly failed a
  correctly marked answer (`fab-model-mmlu-score`; grader prompt fixed in
  8364f65). It also judged `fab-systemd-restart-key` against the reference
  instead of the rubric's "invents a directive" condition. At n=15, judge
  errors are the same size as the effect being measured.

Transcripts are in `evals/reports/nofab_ab_gemma31b_qwen38judge_*.answers.jsonl`
(gitignored).

## #78 content and `IMPLANT_NEED_GATE` on gemma4:31b + qwen3.8:27b judge (2026-09-24)

`prompt_ab revisions` on the 31 `no_fabrication` cases, 3 samples at t=0.7, run through
`local_ab` (lowest free memory 11%). Arms: `old` = 3a4fc5f (before #78), `new` = 33600be,
`gate` = 33600be with `IMPLANT_NEED_GATE=intent`. The gate changed 15 of 31 prompts; the
other 16 reuse `new`. Every failing or flipped case was then triaged by an analyst and an
adversarial skeptic.

| bucket | old FAIL | new FAIL | gate FAIL |
|---|---|---|---|
| fabrication-recall | 7/15 | 5/15 | 5/15 |
| overhedge-precision | 0/10 | 0/10 | 0/10 |
| deliver-carveout | 3/6 | 3/6 | 3/6 |

- **#78: one real fix, no regression.** `fab-kz-vat-current` now answers 16%, taken from
  the refreshed `skill-jurisdiction-kz`. `fab-cy-cit-current` flipped to PASS only because
  the grader read the headline 15%; two of three answers still give 12.5% to ordinary
  companies. McNemar on the raw flips: 2 vs 0, p = 0.5.
- **Refreshed facts rarely reach the prompt.** Even with `lawyer` picked, the jurisdiction
  skill loads for KZ and CY only; RU, ES and US get other skills, so their updated figures
  are never shown to the model.
- **The rewritten factuality implants and the rules header changed nothing visible.** No
  answer in any arm uses a "recalled, not verified" marker, and the three
  deliver-carveout failures (asking for the file instead of a best effort) are the same.
- **`IMPLANT_NEED_GATE=intent` is neutral on gemma.** It removes all implants from 15
  prompts (median 27% shorter, 14% for the whole set) and changes no verdict; the
  triage found no systematic quality difference on any of the 15.

These say how gemma reads the prompts, not how Claude does.


## Implant sensitivity on Opus 5.5, Gemma 4 31B and Qwen3.8 27B (2026-09-24)

`prompt_ab implants` on the 31 `no_fabrication` cases, prompts from `7c01f5a`: no
implant (three repeats), each of eight implants alone, and the production selection.
Hosted through OpenRouter: Opus 5.5 on `azure/global` (reasoning `low`, t=0, 2
samples), Gemma on `novita/bf16` and Qwen on `deepinfra/bf16` (t=0, 1 sample); Qwen
graded Opus and Gemma, Gemma graded Qwen. Per-sample FAIL:

| model | no implant | single implants (8 arms) | production |
|---|---|---|---|
| Opus 5.5 | 10/186 (5.4%) | 2–5 of 62 per arm | 5/62 |
| Gemma 4 31B | 22/93 | 7–9 of 31 per arm | 7/31 |
| Qwen3.8 27B | 14/93 | 4–6 of 31 per arm | 4/31 |

- **Implants change answers, not outcomes.** On the local, deterministic gemma run
  RegressionFirst, CoV and IterBudget changed 23, 24 and 20 of 31 answers against 0
  for the no-implant repeat, yet no arm moves FAIL beyond the no-implant spread on any
  model. On Opus, a per-case review of all 31 cases per arm (an 18-agent workflow with
  an adversarial check of every claimed effect) found no substantive quality
  difference and no leaked implant vocabulary: Opus ignores implants that do not fit.
- **This dataset only shows misfires.** Its cases are factual questions and simple
  deliverables; none is a debugging thread, a regression report or a formal-logic
  problem, so no implant ran in its intended scope. Benefits need scope-matched cases.
- **Harm on weaker models.** Qwen answered "I'll run the full test suite to confirm."
  (nothing else) under RegressionFirst, IterBudget and VerifyAssumptions in 5 of 6
  samples, against 0 of 12 without an implant. Gemma's answers on recently changed
  facts swing between the old and the new value with any prompt change: the KZ VAT
  16% → 12% reversion reproduced deterministically for RegressionFirst, CoV and
  IterBudget, and Cyprus CIT 15% ↔ 12.5% flips with whichever arm is loaded. That is
  sensitivity to prompt perturbation on facts the model holds weakly, not a mechanism
  of one implant, and rewording one implant does not fix it.
- **Selection sends implants out of scope.** Preferred implants load with distance
  0.0 and bypass `IMPLANTS_RELEVANCE_THRESHOLD`: production loaded RegressionFirst on
  13 of 31 cases, all tech how-to questions with no regression in them (the
  `IMPLANT_NEED_GATE` flag addresses this entry point).

### Bounded implant preamble (`exp/implant-rewrites`)

The block header "The following cognitive implants have been loaded to augment
reasoning" was replaced by a preamble that says the patterns were picked
automatically and may not fit, that they shape reasoning but never replace the
latest known facts, and that checks the agent cannot run go to the user. RegressionFirst
and VerifyAssumptions got v2 texts with a scope condition and a no-tools fallback.
Same cases, two samples, five implants plus production pooled (372 samples per cell):

| model | no implant | old header | preamble v1 | preamble v2 |
|---|---|---|---|---|
| Qwen3.8 27B | 58 (15.6%) | 64 (17.2%) | 50 (13.4%) | 53 (14.2%) |
| Gemma 4 31B (`deepinfra/fp8`) | 90 (24.2%) | 96 (25.8%) | 95 (25.5%) | — |
| Opus 5.5 | 20 (5.4%) | 27 (7.3%) | 28 (7.5%) | — |

- The Qwen tool-action stub is gone: 0 of 6 samples with preamble v1 and 0 of 16 with
  v2, against 5 of 6 with the old header; every answer now says it cannot run the
  tests. This is the one clean effect of the experiment.
- Preamble v1 made Opus hedge a settled fact: "about 100 °C" opened 6 of 8 implant
  samples of the boiling-point case (overhedge FAIL 6/120 against 2/120). v2 states
  settled facts plainly and flags only facts that may have changed: 2 of 18 and
  2/180, back to the old level (Opus overhedge cases only, 3 samples).
- Gemma is unchanged: the preamble moves which recent facts flip, not how many.
- RegressionFirst2 and VerifyAssumptions2 match their originals within noise on every
  model (e.g. Qwen 9 vs 9 and 8 vs 9 of 62 with preamble v2); they are kept for their
  scope condition and no-tools fallback, not for a measured gain.

Cost: about $47 on OpenRouter, most of it the two Opus passes (about $0.025 per answer
with reasoning `low`).
