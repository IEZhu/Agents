# Layer sensitivity: calibrate each selection layer separately

Status: proposal, 2026-09-22. Measurements below come from the branch
`feat/factuality-layer` (110 labelled samples in `evals/datasets/routing.jsonl`,
embedding model `intfloat/multilingual-e5-large`, the install's model).
*Correction (2026-09-23):* this line used to name MiniLM, the code default. The
runs inherited `EMBEDDING_MODEL=intfloat/multilingual-e5-large` from the shell.
Re-running every script on a pinned e5-large, isolated copy of `data/`
reproduced the numbers. The few cells that moved are marked below; they come from
this round's edits to two implant texts.

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
   other ("sql injection" / "sql"). *Correction (2026-09-23):* this is not
   cross-talk. Skills are ranked only inside the chosen agent's pool, so "cyprus
   law" legitimately picks `lawyer` at the routing layer and then the Cyprus skill
   among nine jurisdictions. Shared terms only couple layers that rank over the
   same candidates. Routing keywords hit the correct agent in 33/90 English queries
   and 0/20 Russian/Spanish ones.
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
- No disjointness lint between routing and skills (see the correction to finding 5).
  Each implant must declare `triggers` (`tests/test_implant_gating.py`).

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
One feature flag per layer, every default off. Shipped for implants:
`IMPLANT_INDEX_MODE=legacy|triggers`, `IMPLANT_GATING=legacy|zscore` (strictness
`IMPLANT_GATE_Z`) and `IMPLANT_NEED_GATE=off|intent`. An unknown value logs a
warning and falls back to the default. `zscore` implements only the
`μ_q − k·σ_q` half of the section 3 rule. The `d_top1 + m` term and the
calibration head are not implemented, and they get their own flag values once
they exist. `SKILL_GATING` and shadow mode (log would-select vs selected with
`AGENTS_DEBUG=1`) are still planned. Switch one layer at a time.

## Tooling on this branch
- `evals/scripts/calibrate_layers.py` sweeps each layer's knobs separately
  against its own labels (no API calls).
- `evals/runners/run_retrieval.py` now forwards the agent's skill pools.
- `calibrate_layers`, `measure_implant_layer` and `implant_need_gate` run on a
  temporary copy of `data/` (`evals/scripts/_isolated_data.py`) and never modify
  the install's stores. They pin `EMBEDDING_MODEL` from `.env` when it is unset;
  in a checkout without `.env`, set it explicitly.

## Results: implant semantic layer only (2026-09-23)

Setup:
- **Labels:** `evals/datasets/implant_labels.jsonl`, 110 samples, 56 labelled
  "needs none". They were written by a separate agent that saw the queries and the
  implant catalogue but not the retrieval changes.
- **Triggers:** 648 phrases across 57 implants, written by another agent that never
  saw the queries.
- **Split:** by id hash, dev 52 / test 58. `IMPLANT_GATE_Z` was chosen on dev.
- **Measurement:** the semantic layer only, n=3, with no `preferred_implants`.
  This is not the production path; see the next section (P0) for that.
- **Trigger boost:** `IMPLANT_TRIGGER_BOOST=0.85` (the shipped default). It applies
  only to the z-score rows (B, D), so A→B and C→D compare the gate plus the boost;
  B vs D compares index modes at equal boost. `--trigger-boost 1.0` runs the
  no-boost ablation.
- **Command:** `python -m evals.scripts.measure_implant_layer`.

Test split:

| config | index | gating | z | P@1 | hit@3 | MRR | none-acc | utility | loaded | chars |
|---|---|---|---|---|---|---|---|---|---|---|
| A (legacy semantic) | legacy | legacy | — | 0.07 | 0.14 | 0.095 | 0.00 | 0.071 | 3.00 | 4320 |
| B | legacy | zscore | 3.0 | 0.04 | 0.04 | 0.036 | 0.90 | 0.468 | 0.24 | 297 |
| C | triggers | legacy | — | 0.04 | 0.18 | 0.107 | 0.00 | 0.089 | 3.00 | 4157 |
| D | triggers | zscore | 3.0 | 0.00 | 0.00 | 0.000 | 0.97 | 0.483 | 0.09 | 108 |
| reference: load nothing | — | — | — | 0 | 0 | 0 | 1.00 | 0.500 | 0 | 0 |
| reference: agent `preferred_implants` | — | — | — | — | 0.25 | — | 0.00 | 0.125 | 2.81 | — |

`utility` = 0.5·hit@3 + 0.5·none-acc, so loading nothing anywhere scores 0.5.
Re-run after the implant text edits: `chars` in A–C and `loaded` in B moved
(first run: 4358 / 351 / 4152 chars, B loaded 0.28); every other cell is unchanged.
Without the boost (`--trigger-boost 1.0`), B falls to hit@3 0.00 and utility 0.450,
and D loads 0.03 implants per query: the few z-score hits came from the boost.
A literal trigger-only gate (load an implant only when one of its triggers occurs
in the query) scored 0.5 on dev: 3 of 26 needed implants hit, 3 of 26 false fires.

What this shows:
- **Today the semantic implant layer is net negative on these labels.** It
  injects 3 implants (~4.3k chars) on every query, including the 52% that need
  none, and a labelled implant makes the top 3 only 14% of the time.
- **Triggers help ranking a little** (hit@3 0.14 → 0.18, MRR 0.095 → 0.107). With
  28 positive test samples, one sample moves hit@3 by 0.036, so this is within noise.
- **No gate beats "load nothing"** (best: D at 0.483 vs 0.5). On this embedder,
  similarity can't separate "needs this method" from "doesn't". The z-score gate
  mostly learns to abstain.
- **The per-agent static list is the strongest single signal** (hit@3 0.25), but
  it has no way to abstain either.

Decision: defaults stay `IMPLANT_INDEX_MODE=legacy` and `IMPLANT_GATING=legacy`,
so production behaviour is unchanged. The measured `chars` column would favour
abstaining, but retrieval labels are a proxy: whether fewer implants lowers answer
quality needs the end-to-end A/B (blocked: the API proxy was down). Next steps, in
order:
1. Run an answer-quality A/B of A vs D on the no-fabrication and routing sets.
2. Build a "needs any implant?" classifier: per-layer logistic head over [top-1 z,
   trigger hit, query length, tier].
3. Only when (2) says yes, pick the implant from the agent's `preferred_implants`,
   re-ranked by trigger index distance.

## Results: "needs any implant?" gate (2026-09-23)

Correction to the table above: it measured the semantic layer alone. Production
already skips implants on `lite` queries and loads the agent's
`preferred_implants` before any semantic top-up. The honest "before" is that
full pipeline (`P0`). Command: `python -m evals.scripts.implant_need_gate`.
Trigger distances are unboosted by default (`--trigger-boost 1.0`). The first run
used the production 0.85; the policy table is identical at both values, and only
the learned gate's weights differ (quoted below for both).

Test split (58 samples; the learned gate was trained on dev, 52):

| policy | hit@3 | none-acc | utility | Δ vs P0 (95% bootstrap CI) | implants/query |
|---|---|---|---|---|---|
| P0 production | 0.29 | 0.53 | 0.410 | — | 2.07 |
| P1 none | 0.00 | 1.00 | 0.500 | +0.090 [−0.038, +0.216] | 0.00 |
| P2 intent gate | 0.25 | 0.87 | 0.558 | +0.149 [+0.056, +0.242] | 1.36 |
| P3 learned gate (numpy logistic, 7 features) | 0.25 | 0.87 | 0.558 | +0.149 [+0.056, +0.242] | 1.36 |
| P4 learned gate + triggers rerank | 0.18 | 0.87 | 0.523 | +0.113 [+0.006, +0.214] | 0.90 |
| P5 oracle gate (upper bound) | 0.29 | 1.00 | 0.643 | +0.233 [+0.141, +0.333] | 1.36 |

The intent gate has no parameters fitted on these labels, so it can be checked on
all 110 samples. P0 0.480 → P2 0.586, Δ +0.106 [+0.045, +0.173]. Implants per
query go 2.00 → 1.49, and it loses 2 of the 20 correct hits.

What this shows:
- **The existing intent classifier is a good implant-need detector.** Used for
  this layer alone, it cuts implants injected into queries that need none (none-acc
  0.53 → 0.87) at a small recall cost. The learned logistic gate converged to the
  same decisions: its largest weights are `tier_lite` (−0.89) and `intent_budget`
  (+0.89), unboosted; −0.93 and +0.82 at boost 0.85. `trig_z1` got −0.37 unboosted
  (−0.19 at 0.85) and changes no test decision; the other trigger feature stays
  ≤0.09. No model weights are shipped.
- **Trigger reranking of the preferred list hurts** (hit@3 0.25 → 0.18), with or
  without the boost. Keep the agent's declared order.
- **Headroom for gating is +0.085** (P5 vs P2). The remaining gap is recall of
  the right implant, not the gate.

Shipped as `IMPLANT_NEED_GATE=intent` (default `off`). It gates only the implant
layer: tier, skills and persona format stay on the legacy rule. It applies on the
protocol 1 path only. The protocol 2 persona bundle is built once per session, so a
per-query gate there would drop the agent's implants for the whole conversation. That is the point
of per-layer sensitivity, and it differs from `INTENT_CLASSIFIER_ENABLED`, which
switches all layers at once. Before the default is flipped, the answer-quality A/B
must confirm it; labels are a proxy (single labeller, not human-reviewed).
