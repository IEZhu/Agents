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

## Start a server without installing anything

On this machine `nix` provides Ollama for one session, without a profile or
Homebrew install. The port and context settings matter:

```bash
OLLAMA_HOST=127.0.0.1:11435 OLLAMA_CONTEXT_LENGTH=16384 OLLAMA_KEEP_ALIVE=30m \
  nix run nixpkgs#ollama -- serve
```

- **Port 11435** avoids clashing with a leftover `ollama serve` on 11434.
- **`OLLAMA_CONTEXT_LENGTH=16384`**:
  - Measured on the 31 no-fabrication cases (qwen3 tokenizer), the enriched MCP
    prompts were 2,478 tokens at the median and 6,893 at most (`sysadmin`).
    16k leaves room for the 800-token answer.
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
  --baseline-rule evals/fixtures/rule-no-fabrication.compressed.mdc \
  --candidate-rule evals/fixtures/rule-no-fabrication.factuality.mdc \
  --out evals/reports/no_fabrication_ab_local.md
python -m evals.runners.run_mcp_vs_vanilla --provider local --n 10 \
  --concurrency 1 --max-tokens 2048 --judge-max-tokens 1024
```

- Keep `run_mcp_vs_vanilla` at `--concurrency 1`. Its defaults (8 concurrent
  queries, 8192-token answers) are sized for a cloud API; locally they queue
  behind one GPU and can overflow a 16k context.
- `run_mcp_vs_vanilla` samples queries from Hugging Face datasets, so it needs
  network access or a warm `~/.cache/huggingface`.
- When `--provider local` is given without an explicit judge, the cloud
  `JUDGE_PROVIDER` / `JUDGE_MODEL` variables are ignored. The judge comes from
  `LOCAL_LLM_JUDGE_MODEL` or `--judge-model`.

| Variable | Default | Meaning |
|---|---|---|
| `LOCAL_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint |
| `LOCAL_LLM_MODEL` | `qwen3:8b` | model under test |
| `LOCAL_LLM_JUDGE_MODEL` | = `LOCAL_LLM_MODEL` | grader / pairwise judge |
| `LOCAL_LLM_TEMPERATURE` | `0` | `0` = greedy and repeatable; set e.g. `0.7` to sample |
| `LOCAL_LLM_SEED` | `7` | base seed when sampling; each call gets base + call index |
| `LOCAL_LLM_THINKING` | `0` | `1` turns thinking on for calls of ≥1024 tokens (answers); router picks, graders and judges keep it off |
| `LOCAL_LLM_TIMEOUT` | `900` | client timeout in seconds |
| `LOCAL_LLM_API_KEY` | `local` | only for servers that check a key |

No API key is required, and reported cost is zero.

**Repeated samples need `LOCAL_LLM_TEMPERATURE > 0`.** At temperature 0 decoding
is greedy: every sample and every retry re-roll returns the same text, so
`--samples-per-case 3` only triples the run time. With sampling on, each call
gets its own seed, so samples differ and a whole run stays reproducible.

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

Setup: `compare_rules` on all 31 cases, `qwen3:8b` answering and grading,
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
vs candidate `rule-no-fabrication.factuality`. The server ran with one model
loaded at a time, `OLLAMA_KV_CACHE_TYPE=q8_0`, flash attention and a 12k
context. Lowest free memory seen was 11%, and the memory watchdog never fired.
Both runs used the grader prompt from before commit 8364f65.

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
