# Layer sensitivity: calibrate each selection layer separately

Status: proposal, 2026-09-22. Measurements below come from the branch
`feat/factuality-layer` (110 labelled samples in `evals/datasets/routing.jsonl`,
embedding model `paraphrase-multilingual-MiniLM-L12-v2`).

## Why

Three layers select context per query: agent routing, skills, and implants
(rules are always on). They answer different questions, so they need different
signals, and a change that tunes one layer must not shift the others. Today they
share one embedding space, overlapping keyword sets and hand-set constants, and
none of them is calibrated against its own labels.

| Layer | Question it answers | Signal that should drive it | Cost of a wrong pick |
|---|---|---|---|
| Routing | *who* — which persona | domain nouns, entities ("НДС", "Kubernetes") | high: wrong persona, wrong skill pool |
| Skills | *what* — domain knowledge, procedures | domain + method terms | missing knowledge (recall matters); extra tokens |
| Implants | *how to think* — task shape | task-shape cues: compare/choose, why/root cause, check facts, assess risk, plan steps | extra tokens, over-verification: precision matters, "load nothing" must be possible |

## What the measurements show

1. **Thresholds never gate.** Query-to-item cosine distances are compressed into
   0.12–0.27 (implant top-1 median 0.182, p90 0.206; skill top-1 median 0.192),
   while the thresholds are `SKILLS_RELEVANCE_THRESHOLD=0.75` and
   `IMPLANTS_RELEVANCE_THRESHOLD=0.85`. Sweeping the implant threshold from 0.70
   to 0.95 and the skill threshold from 0.65 to 0.85 changes no metric. Every
   query gets the top-N of each layer: the layers have no off switch.
2. **Implant ranking is close to noise.** The median gap between the 1st and 3rd
   implant is 0.005. Against the only labels available (the expected agent's
   `preferred_implants`, a proxy), semantic implant retrieval scores MRR 0.07,
   recall@5 0.06.
3. **Implants are matched by topic, not by need.** They are indexed on
   description + body, and the body describes the *technique*. A conversation
   *about* prompting or verification pulls in CoV, Contrastive CoT and Self-Refine
   whether or not the task needs them (observed in this session).
4. **The skill keyword boost does nothing measurable.** On 90 English queries,
   8 hit any skill keyword, 0 hit an expected skill, 7 hit an unexpected one. On
   the 20 Russian/Spanish queries: 0 hits (28 of 662 skill keywords are
   Cyrillic). Setting `keyword_boost` to 1.0, 0.85 or 0.7 gives identical metrics.
   Skill ranking is driven by pool membership: `boost_factor` (preferred skills)
   is the only knob that moves MRR (0.70 → 0.525, 0.85 → 0.518).
5. **Vocabularies overlap across layers.** 76 terms appear verbatim in both
   routing `domain_keywords` and skill `keywords`, and 83 more pairs contain each
   other ("sql injection" / "sql"). One word can move two layers at once, so they
   can't be tuned independently. Routing keywords hit the correct agent in 33/90
   English queries and 0/20 Russian/Spanish ones.
6. The retrieval eval runner called the skill retriever without the agent's
   pool, so skill metrics were stuck at 0.00; fixed on this branch (skills now:
   MRR 0.59, recall@5 0.71 including core skills).

Caveat: the RU/ES samples are 10 each, and they are short MASSIVE intents. Treat the
multilingual numbers as a signal, not an estimate.

## Design

### 1. Disjoint vocabularies, one per layer
- Routing keeps `routing.domain_keywords` (entities, domains).
- Skills keep `keywords` (domain + method terms).
- Implants get a new `triggers:` field: task-shape cues in ru/en/es ("сравни",
  "что выбрать", "почему", "проверь факты", "риски", "пошагово", "compare",
  "root cause", "fact-check"), plus optional `anti_triggers`.
- A lint test fails when a term belongs to two layers, unless it is on an
  explicit allow-list. Start by resolving the 76 exact overlaps.

### 2. Per-layer index text
Implants embed `triggers` + "When to Use" only, not the technique body. This removes
topic leakage from finding 3. Skills keep description + keywords + body.

### 3. Relative gating instead of absolute thresholds
Absolute cosine thresholds don't transfer across embedding models and, on this
one, sit outside the observed range. Gate per layer on a calibrated score:
- **Now (no training):** per-query margin — select item *i* only if
  `d_i ≤ d_top1 + m_layer` and `d_i ≤ μ_q − k_layer·σ_q`, where μ/σ are this
  query's distances over the layer. Implants get strict `k` (precision, allow
  empty); skills get loose `k` (recall).
- **Next ("дообучение", step 1):** a per-layer calibration head. Logistic
  regression on features `[cosine, keyword/trigger hit, in preferred pool,
  in capable pool, query language]` → P(relevant), trained separately per layer
  on (query, item, label) pairs. It needs roughly 300–500 labelled pairs per layer,
  weighs keyword hits by what they actually predict, and gives an interpretable
  threshold on a probability scale.
- **Later (step 2, only if the head saturates):** a per-layer linear adapter over
  the frozen embedding. It is trained contrastively on that layer's pairs, and its
  weights are stored per layer, so tuning implants cannot move skills. It needs about
  1–2k pairs.
- **Not recommended:** fine-tuning the shared embedding model. It re-couples every
  layer (the thing this plan removes), invalidates all stores and caches, and
  the label volume doesn't justify it.

### 4. Labels per layer
- Skills: `expected_skills` exists for 56/110 samples. Extend it to ≥150 with
  balanced ru/en/es.
- Implants: new labels answer "which reasoning method would change the answer,
  or none?", with "none" as a first-class label. Label with
  `evals/scripts/label_with_claude.py` (new prompt) and human-review ≥20%.
- Keep the repo's policy: commit labels and source hashes, never query texts.

### 5. Metrics and gates, per layer
- Routing: accuracy (existing `run_routing`).
- Skills: semantic-pool recall@5 and MRR (`calibrate_layers`, core excluded).
  Gate: no regression vs 0.65 / 0.525.
- Implants: precision@1, plus a new **none-accuracy** (the share of "none" samples
  where the layer loads nothing). Today this is 0% by construction.
- Retrieval metrics are proxies. Every layer change also needs an end-to-end
  answer-quality A/B (the `compare_rules` pattern, with a layer swapped instead of a
  rule).

### 6. Rollout
Put one feature flag per layer (`IMPLANT_GATING=legacy|margin|head`,
`SKILL_GATING=…`) and run shadow mode first: log would-select vs selected with
`AGENTS_DEBUG=1`. Switch one layer at a time.

## Tooling on this branch
- `evals/scripts/calibrate_layers.py` sweeps each layer's knobs separately
  against its own labels (no API calls).
- `evals/runners/run_retrieval.py` now forwards the agent's skill pools.
