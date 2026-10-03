# Embedding models: retrieval and answer A/B results

Date: October 3, 2026. Code: branch `feat/embedding-prompts` at `278b713`; runs on
branch `eval/embedding-models`. These results describe the recorded models,
settings and sets only.

## Question

Is there an embedding model that does better than `intfloat/multilingual-e5-large`
for this system, especially on Russian requests, and does better retrieval give
better answers?

## Method

The candidate branch adds what the comparison needed. Each model gets the prompts
from its model card (`src/engine/embedding_prompts.py`). Before this, e5 embedded
unprefixed text, because fastembed adds no `query:` / `passage:` prefix;
`EMBEDDING_PROMPTS=off` keeps that behavior as the baseline. Two more additions:
harrier models register from their ONNX exports, and the embedder caps inputs at
2048 tokens and embeds documents four at a time (see [the incident](#incident)).

**Track A, retrieval (local, free).** It uses two sets:
`evals/datasets/routing.jsonl` (110 queries, 90 of them English and 60 labeled
`universal_agent`) and the new `evals/datasets/routing_ru.jsonl` (69 hand-written
Russian requests for all 43 agents, labels reviewed independently). Skills and
implants come from `run_retrieval --expected-from-agent`; implant labels are each
agent's preferred implants, a proxy. Routing comes from the new
`run_cache_routing`: a leave-one-out nearest neighbour over both sets, which
simulates the semantic cache.

**Track B, answers.** Production e5 (no prompts) against harrier-oss-v1-270m with
its prompts, on `evals/datasets/embedding_ab_cases.json` (the checked english-pivot
requests, grouped by language). 18 of the 24 cases got different contexts; 6 were
identical and are left out, so this set has no controls. Answers came from three
models:

- Gemma 4 31B (`novita/bf16`) and Qwen 3.8 27B (`deepinfra/bf16`), through
  OpenRouter. Two runs each: temperature 0 with seed 7, then the provider default.
  Reasoning medium.
- Opus 5.5, through the `Agents-eval` routine.

Opus agents judged every run blind, in both orders
([flows/ab-eval.md](../flows/ab-eval.md)). OpenRouter spend: $0.57.

## Results

Track A, in each model's best configuration:

| Model | Skills MRR, base / ru | Skills R@3, base / ru | Implants MRR, base / ru | Cache NN accuracy, en / ru | Russian precision at 20% coverage | Peak memory |
|---|---|---|---|---|---:|---:|
| e5-large, production (no prompts) | 0.590 / 0.566 | 51% / 62% | 0.062 / 0.209 | 54% / 29% | 25% | 2.1 GB |
| e5-large with its prefixes | 0.587 / 0.570 | 53% / 61% | 0.066 / 0.228 | 53% / 34% | 25% | — |
| harrier-oss-v1-270m | 0.593 / 0.571 | 58% / 66% | 0.137 / 0.353 | 61% / 30% | 50% | 2.8 GB |
| harrier-oss-v1-0.6b (batch 2) | 0.595 / 0.571 | 56% / 64% | 0.205 / 0.313 | 64% / 32% | 50% | 3.7 GB |
| EmbeddingGemma-300m | 0.589 / 0.564 | 57% / 60% | 0.095 / 0.351 | 59% / 42% | 62% | 2.7 GB |
| Qwen3-Embedding-0.6B-Q | 0.592 / 0.563 | 55% / 62% | 0.053 / 0.230 | 57% / 29% | 50% | 8.9 GB |

Peak memory is the largest resident set of an index build. Per-model similarity
scales differ: at 20% coverage, e5's threshold is 0.86 and harrier-270m's 0.65.
None reaches the configured `ROUTER_SIMILARITY_THRESHOLD` of 0.95 for
non-duplicates.

Track B, case-level sign test across each model's runs (a case scores +1 for each
verdict the harrier arm wins and −1 for each it loses):

| Answers | Runs | harrier better | worse | even | score | p |
|---|---|---:|---:|---:|---:|---:|
| Opus 5.5 | 1 | 7 | 3 | 8 | +7 | 0.34 |
| Gemma 4 31B | 2 | 5 | 8 | 5 | −9 | 0.58 |
| Qwen 3.8 27B | 2 | 4 | 6 | 8 | −7 | 0.75 |

Per run, net verdicts were +7 for Opus, −5 and −4 for Gemma, −4 and −3 for Qwen.

## Reading

- harrier-270m, harrier-0.6b and EmbeddingGemma beat e5 clearly on implant
  retrieval and moderately on skills R@3. EmbeddingGemma separates Russian requests
  best in the cache simulation.
- Adding e5's own prefixes changes little.
- Better retrieval did not translate into measurably better answers. No model's
  answers changed significantly. Gemma and Qwen lean slightly against the harrier
  contexts and Opus slightly towards them. The retrieval labels are proxies, and
  answers depend little on which preferred and capable skills are added. The
  evidence does not support switching the production model for answer quality.
- Qwen3-Embedding-0.6B-Q is no better than e5 on implants and needs about three
  times the memory.

## Limits

- Track A has one run per model on 179 labeled queries. The implant labels are a
  proxy, and 10 Russian rows carry core skills only, which every model gets.
- Track B covers 18 cases. Opus judged every run, its own answers included. There
  are no control cases.
- Cache-routing numbers simulate the cache over labeled queries. They do not replay
  production traffic.

## Incident

The first track A attempt rebooted the 36 GB laptop at 06:46. fastembed embeds
documents in batches of 256, padded to the longest text, and harrier-270m was
loaded with a raised token limit. All 127 skill and implant texts, up to about
2.8k tokens, went into one batch, while the shared daemon also held a large amount of memory
(investigated separately). Commit
`278b713` caps inputs at 2048 tokens and passes `batch_size=4`; afterwards index
builds peaked at 2.1–3.7 GB, and at 8.9 GB for Qwen3-0.6B-Q.
