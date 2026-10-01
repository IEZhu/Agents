<!-- Generated 2026-09-23 by a research workflow: 7 literature sweeps + 3 gap sweeps, 81 citations verified against the opened source (9 rejected), synthesis revised after a rigor critic. Claims about our eval set and code were re-checked by hand. -->

# Ideas I1–I7 and a multidimensional query classifier: research evidence and its relationship to our measurements F1–F10

Status: research synthesis of 2026-09-23 (branch `feat/factuality-layer`). It
records proposals and an experiment plan (E0–E4), not current behavior; code
references and the "Currently in `classify_intent`" column describe that date.
F1–F6 are the measurements recorded in the
[layer sensitivity plan](layer-sensitivity-plan.md); F7–F10 are the observations
listed in section 4. Current settings:
[implants](../implants/README.md#1-agent-preferences-and-semantic-retrieval) and
[intent classifier](intent-classifier.md).

## 1. Summary

- **Our measurements are weaker than they appear, and this needs to be addressed before drawing conclusions.** I checked the composition of the evaluation set (`routing.jsonl` and `implant_labels.jsonl`, branch feat/factuality-layer):
  - 45 of the 56 “none” labels came from single-line MASSIVE/CLINC voice commands. Only 11 came from WildBench (en).
  - All 20 ru/es queries are short MASSIVE commands with tier=lite, and 18 of them are labeled “none”.
  - This means the features “tier_lite”, “no implant needed”, and “non-English” almost coincide in our set because of how it was assembled.
  - F4 (+0.106) and the tier_lite weight in F5 may measure “is this a voice command?” rather than “does the model need reasoning support?”.
  - The 52% “none” share reflects the proportions of the source datasets, not real traffic.
  - F3 (“0 hits on ru/es”) cannot be separated from query type.
  - In addition, the `intent.py` docstring says the heuristic was tuned on the same 110 ids (it mentions “67/110” and “16 of 25 standard”). F4 was therefore most likely measured on the same sample used to tune the heuristic.
- **I7 (a classifier decides which layers to enable) has the most mature support in the literature:** [Adaptive-RAG, 2024](https://aclanthology.org/2024.naacl-long.389/), [RAGate, 2024](https://arxiv.org/abs/2407.21712), [SKR, 2023](https://arxiv.org/abs/2310.05002), [DOTS, 2024](https://arxiv.org/abs/2410.03864). Our F4 cannot yet count as confirmation because it was obtained in-sample on a biased set. It first needs validation on held-out data.
- **The weak point is cosine comparison, not embeddings themselves.** Without training, both clustering and cosine similarity to item descriptions capture topic. But in [TnT-LLM, 2024](https://arxiv.org/abs/2403.12173), logistic regression on embeddings, trained on GPT-4 pseudo-labels, matched GPT-4 at intent classification against human labels (0.658 versus 0.655). A trained “what does this query need?” head over the already computed e5-large embedding therefore has support in the literature.
- **Splitting the query (I5 and I1) does not fix the main F2 example.** In a conversation about prompting, the topic is in the instruction itself, not in a pasted artifact, so removing artifacts changes nothing. Other approaches address this:
  - a trained need-detection head;
  - an LLM-formulated statement of the need ([Re-Invoke, 2024](https://arxiv.org/abs/2408.01875), [BRIGHT, 2024](https://arxiv.org/abs/2407.12883));
  - the host model selecting implants from a short catalog ([Self-Discover, 2024](https://arxiv.org/abs/2402.03620); `implants.get_catalog()` already exists). However, catalog selection helped only the strongest model in [Select-then-Solve, 2026](https://arxiv.org/abs/2604.06753), so an A/B test is needed.
- **I1 and I2 work better at the prompt level than is commonly assumed, but they are not a security boundary.** Prompt engineering improved GPT-4's separation of instructions from data from 20.8% to 95.3%, with a small utility loss. Larger models nevertheless start out worse at separation than smaller ones ([Can LLMs Separate Instructions From Data?, 2024](https://arxiv.org/abs/2403.06833)). Adaptive attacks bypass 12 defenses, with success above 90% for most of them ([The Attacker Moves Second, 2025](https://arxiv.org/abs/2510.09023)).
- **I4 should be an explicit step-by-step plan within one agent, rather than handoffs between personas.** For sequential tasks, the evidence leans against distributing work across agents. Our case of a single chat request has not been tested directly.
- **F8 (outdated rates) is not fixed by tags and dates in the prompt.** Volatile facts should be moved out of skills and filtered by date on the server ([Mitigating Temporal Misalignment by Discarding Outdated Facts, 2023](https://aclanthology.org/2023.emnlp-main.879/), [Metadata, Structure, or Strategy?, 2026](https://arxiv.org/abs/2606.29645)). The procedural part of skills should remain an instruction.

## 2. The user's ideas against the research

### I1. Parse a query into INSTRUCTIONS / ARTIFACTS / GOALS. Verdict: partly supported; novel for our task

**Supporting evidence:**
- [SEP, 2024](https://arxiv.org/abs/2403.06833): models struggle to distinguish instructions from data, but prompt-level changes help most models substantially. GPT-4's separation improved from 20.8% to 95.3%, with a small utility loss. Some models (Gemma-7B) showed almost no effect.
- [Instructional Segment Embedding, 2024](https://arxiv.org/abs/2410.09102): labeling roles by segment (system / user / data / output) is the right abstraction. The gains are mainly in attack resistance; clean-task quality improves by up to +4.1%.
- [Claude prompting best practices, 2026](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices): tags by content type, long data first, query last (“up to 30%” in the vendor's internal tests; methodology not published).
- [LLMs Get Lost In Multi-Turn Conversation, 2025](https://arxiv.org/abs/2505.06120): consolidating accumulated instructions into one block (Concat) recovers 95.1% of quality. Providing them across turns causes an average loss of 39%.

**Weaknesses:**
- No study first splits a chat request into instruction, artifact, and goal and then measures answer accuracy. The evidence base consists of security research and vendor recommendations.
- The “GOALS” block is our own construction. Explicit goals are already part of the instruction; implicit goals belong to I3.
- I1 helps the classifier only when the triggering text is in a pasted artifact. The code implies that a pasted log containing “compare” or “debug” activates `mode=analyze` (the `_ANALYZE_LEX` branch in [`_detect_mode`](../src/engine/intent.py)). This is an inference from reading the code, not a measurement from F1–F10. I1 does not fix F2's main example (a conversation about prompting).
- Gains from prompt-level delimiters and tags do not mean that the model will follow the hierarchy they specify ([Control Illusion, 2025](https://arxiv.org/abs/2502.15851)).

**What the literature adds:**
- The marking method should depend on the artifact type ([Spotlighting, 2024](https://arxiv.org/abs/2403.14720)). Datamarking was tested on prose. Its effect on code and logs is our inference, not a result of the paper.
- Markers should be repeated for long artifacts because delimiter signals fade over long contexts (ISE).
- The segmenter needs invariance tests: instruction before the artifact, after it, and interleaved with it ([The Illusion of Role Separation, 2025](https://arxiv.org/abs/2505.00626)).

### I2. Artifacts are data, not instructions. Verdict: supported as hygiene; contradicted as a security guarantee

**Supporting evidence:**
- Delimiters roughly halve attack success. Datamarking reduces it for GPT-3.5 from about 50% to below 3%, with no measured quality loss ([Spotlighting, 2024](https://arxiv.org/abs/2403.14720)).

**Contrary evidence:**
- Adaptive attacks bypass 12 defenses ([The Attacker Moves Second, 2025](https://arxiv.org/abs/2510.09023)).
- Separating system and user messages does not establish priority ([Control Illusion, 2025](https://arxiv.org/abs/2502.15851)).
- Even after prompt engineering, GPT-4 still executes hundreds of probes from the data (SEP).

**What the idea omitted:**
- Not every instruction in an artifact should be ignored. A README containing steps the user asks to execute is an aligned instruction ([The Instruction Hierarchy, 2024](https://arxiv.org/abs/2404.13208)). It cannot be distinguished from a hostile instruction without the user's goal (I3).
- The same paper acknowledges that trained models over-refuse benign requests. If we demote content to “data” too aggressively, the same could happen here.
- The threat model in [CaMeL, 2025](https://arxiv.org/abs/2503.18813) explicitly excludes cases where the user pastes untrusted text. I2 addresses that gap only as hygiene.

### I3. Infer implicit goals. Verdict: contradicted in its naive form; supported as “identify what is missing”

**Evidence against the naive form:**
- Models often infer unstated requirements themselves: in 41.1% of cases, accuracy exceeds 98%. But these prompts are twice as likely to degrade when switching models ([What Prompts Don't Say, 2025](https://arxiv.org/abs/2505.13360)).
- A prompted LLM recognizes query ambiguity with 54.25% accuracy ([CLAMBER, 2024](https://arxiv.org/abs/2405.12063); models from 2023).
- Early assumptions are the main source of failures in multi-turn dialogue ([LLMs Get Lost…, 2025](https://arxiv.org/abs/2505.06120)).
- Human agreement on primary intent is κ≈0.55 ([TnT-LLM, 2024](https://arxiv.org/abs/2403.12173)).

**Evidence for “identify what is missing”:**
- “Identify missing details and assess their importance”: a fine-tuned 7B model correctly identifies vagueness in more than 85% of tasks. The tasks are synthetic ([Tell Me More!, 2024](https://arxiv.org/abs/2402.09205)).
- Explicit reasoning about the query before retrieval adds up to +12.2 points ([BRIGHT, 2024](https://arxiv.org/abs/2407.12883)). This is I3 serving retrieval rather than answering.

**Conclusion:**
- Pass inferred goals as assumptions the user can correct.
- If a critical detail is missing (jurisdiction or year for a question about a rate), ask or state the assumption explicitly.
- This cannot be done reliably in the synchronous path without an LLM: see the constraints in section 3.

### I4. Follow the request's steps, with a role for each step. Verdict: planning is prior art; a role per step is unproven, with evidence leaning against it for sequential tasks

**Supporting evidence:**
- [Decomposed Prompting, 2023](https://arxiv.org/abs/2210.02406): separate subtask handlers help on multi-step benchmarks. This is close to I4. But the handlers differ by function (prompt, model, symbolic function), not by persona.

**Contrary evidence:**
- [Towards a Science of Scaling Agent Systems, 2025](https://arxiv.org/abs/2512.08296v3): results range from +80.8% on decomposable financial reasoning to −70.0% on sequential planning. The chain “analysis → comparison → recommendation” is sequential.
- [Rethinking the Bounds of LLM Reasoning, 2024](https://aclanthology.org/2024.acl-long.331/): one agent with a strong prompt nearly matches the best multi-agent debate. Multiple agents win only when the prompt has no demonstrations. The comparison concerned debate, not a “handler per step” pipeline.
- [How we built our multi-agent research system, 2025](https://www.anthropic.com/engineering/multi-agent-research-system): multiple agents pay off on broad research requests at roughly 15× the token cost.

**Conclusion:**
- There is no direct evidence on role handoffs between steps within a single chat request. The appropriate interpretation of I4 is one agent with an explicit plan built only from the instruction and goal, never from artifacts (CaMeL).
- Whether steps should differ in content (skills, fresh facts) or persona is our hypothesis. F9 concerns only factual accuracy. In [Control Illusion, 2025](https://arxiv.org/abs/2502.15851), authority or expertise framing affected behavior more than instruction placement. Persona may therefore help with requirement compliance, and it is too early to discard it. We have not tested this.

### I5. A separate query slice for each layer. Verdict: partly supported; the problem goes deeper than slicing

**Evidence against the naive form:**
- [When Should Queries Be Decomposed?, 2026](https://arxiv.org/abs/2606.08577): subqueries reduce NDCG@10 by 1.6–4.3 during initial dense retrieval but improve it by 3.9–7.6 during reranking. However, this work studies initial retrieval (recall) over large corpora.
- Our pools are small (57 implants, 71 skills), and the z-score gate evaluates every item. There is no separate recall stage, so the distinction between “retrieve candidates using the full query, rerank using a slice” almost disappears. The remaining empirical question is whether embedding a short slice dilutes its signal.

**Supporting evidence:**
- Domain and action are different axes ([Arch-Router, 2025](https://arxiv.org/abs/2506.16655)).
- Clio embeds an LLM-formulated “facet” rather than raw text ([Clio, 2024](https://arxiv.org/html/2412.13678v1)). Clio's authors do not make automatic decisions based solely on clusters, whereas we are proposing automatic routing by facets for every query.
- Re-Invoke extracts intent and compares it with synthetic queries for each tool: +20% (single tool) and +39% (multiple tools) in nDCG@5 ([Re-Invoke, 2024](https://arxiv.org/abs/2408.01875)).
- [Buffer of Thoughts, 2024](https://arxiv.org/html/2406.04271) matches templates against a distilled task rather than the raw query.

**Nuances:**
- Aspect-based retrieval wins only on an unbalanced corpus: MAP@10 0.36 → 0.52 ([Multi-Aspect Reviewed-Item Retrieval…, 2024](https://arxiv.org/abs/2408.00878)).
- Decomposition pays off for multi-part queries in a “merge candidates, then rerank” scheme ([Question Decomposition for RAG, 2025](https://aclanthology.org/2025.acl-srw.32/)).
- Every approach using LLM extraction needs an LLM call per query (see section 3).

**An alternative missing from the idea:** the host model selects implants from a catalog of “when to use” descriptions, with an explicit “none” option.
- Support: [Self-Discover, 2024](https://arxiv.org/abs/2402.03620), where selecting modules by task type improved on CoT by up to +32%; [Equipping agents… Agent Skills, 2025](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills).
- This bypasses F1 and F2 entirely.
- Risk: in Select-then-Solve, catalog selection helped only GPT-5, while weaker models fell below direct answering. In [Meta-Reasoning Prompting, 2024](https://arxiv.org/abs/2406.11698), selection with GPT-3.5 was worse than the best single method.

### I6. Separate sensitivity for each layer. Verdict: separate gates and thresholds are supported; separate vocabularies are contradicted

**Supporting evidence:**
- Absolute cosine similarity depends on how the model was trained ([multilingual-e5-base model card, 2023](https://huggingface.co/intfloat/multilingual-e5-base)). Thresholds should be relative or conformal ([Principled Context Engineering for RAG, 2025](https://arxiv.org/abs/2511.17908)).
- Each layer needs an explicit “none” classifier. In [Guarded Query Routing, 2025](https://arxiv.org/html/2505.14524), routing by embedding similarity detects OOD at only 35–42%, while TF-IDF+WideMLP achieves a harmonic mean of 87.74% in about 3.6 ms.
- Each task gets its own adapter or instruction over a shared encoder ([jina-embeddings-v3, 2024](https://arxiv.org/abs/2409.10173)).

**Evidence against vocabularies:**
- The vocabulary of the query and the required item barely overlaps: ROUGE-L 0.06 ([Retrieval Models Aren't Tool-Savvy, 2025](https://arxiv.org/abs/2503.01763)).
- Lexical matching breaks across languages ([M3-Embedding, 2024](https://arxiv.org/abs/2402.03216)).

**What the code already does:** the existing `IMPLANT_GATING=zscore` multiplies distance by `IMPLANT_TRIGGER_BOOST=0.85` when a trigger phrase matches (see [`ImplantRetriever._zscore_candidates`](../src/engine/implants.py)). Set it to 1.0 for evaluations. Rerunning F5/F6 without the boost (`implant_need_gate --trigger-boost 1.0`) produced the same F6 result (hit@3 0.25 → 0.18): the trigger-index distance itself hurts, not the boost. The `trig_z1` weight in F5 is −0.37 without the boost (−0.19 with a 0.85 boost), and it does not change the gate's decisions. In F2's z-score gate, the few hits came from the boost: without it, hit@3 falls from 0.04 to 0.00.

There are no studies of calibrating multiple layers within a single prompt assembly. Everything above is an extrapolation.

### I7. A multidimensional query classifier. Verdict: supported by the literature; our own evidence remains weak

**Supporting evidence:**
- Query complexity as a retrieval-mode switch ([Adaptive-RAG, 2024](https://aclanthology.org/2024.naacl-long.389/)).
- A binary gate based on human labels: roughly 16% of turns are enriched, with quality comparable to enriching every turn ([RAGate, 2024](https://arxiv.org/abs/2407.21712)).
- Reasoning-action selection trained on outcomes, including an “Empty” action ([DOTS, 2024](https://arxiv.org/abs/2410.03864)).
- CoT is mainly needed for mathematics and logic ([To CoT or not to CoT?, 2024](https://arxiv.org/abs/2409.12183)).
- Separate classifiers for intent and domain ([TnT-LLM, 2024](https://arxiv.org/abs/2403.12173)).

**Cautions:**
- The classifiers' own accuracy is modest. Adaptive-RAG achieves 54.52% overall and 30.52% on the “nothing needed” class.
- Even GPT-5 achieves only about 0.75–0.84 F1 on Bloom levels. Trained classifiers lose 0.25–0.28 on another dataset ([Cross-Dataset Bloom Question Classification, 2026](https://arxiv.org/html/2606.13684v1)).
- Arena-Hard and Magpie labels have been validated only against other LLMs ([From Crowdsourced Data to High-Quality Benchmarks, 2024](https://arxiv.org/abs/2406.11939), [Magpie, 2024](https://arxiv.org/html/2406.08464)).
- Trained models tend to learn shortcuts ([The Illusion of Role Separation, 2025](https://arxiv.org/abs/2505.00626)). Our biased set supplies a ready-made shortcut: the “short command” feature is equivalent to the answer “none”.

## 3. A multidimensional query classifier

### Constraints that determine what is feasible

- `classify_intent` runs on the hot path: it is pure, synchronous, and uses only the standard library. An embedding-based centroid approach was rejected because it added 12–53 ms to a hot path whose p95 is 37–58 ms (intent.py docstring).
- Every dimension therefore falls into one of three classes:
  - **R**: regular expressions in `intent.py`;
  - **H**: a lightweight head (LR, kNN, or SetFit) over the query embedding that skills, implants, and the router **already compute** (see [`SkillRetriever.retrieve`](../src/engine/skills.py), [`ImplantRetriever.retrieve`](../src/engine/implants.py), and [`SemanticRouter.query_nearest`](../src/engine/router.py)). It belongs in enrichment or the router, not intent.py. The additional latency is only the head itself; it must be measured;
  - **O**: offline only. An LLM labels training data here, or the host model selects from a catalog. MCP sampling is not always available, and protocol v2 “never samples”, so a server-side LLM call per query is unsuitable for runtime.
- Any field that changes the prompt must be included in `cache_token` and `with_tier`. Query-specific dimensions must not enter the session-scoped v2 bundle.
- “High” reliability for a regex feature means only that the regex itself is stable. Whether it predicts **need** has not been tested.

### Decomposer outputs (inputs to the dimensions, class R)

| Output | How to obtain it | Current state |
|---|---|---|
| `instruction_span` | Remove `_CODE_FENCE`, `_URL`, long `_LIST_LINE` blocks, and `_CODE_DECLARATION` (see the named patterns in [intent.py](../src/engine/intent.py)) | Absent: the regexes only add points to `depth_score` |
| `artifact_spans[]` (type, length) | The same regexes plus a stack-trace heuristic | Absent |
| `sub_asks[]` | `enumerated`, `multi_question` | Counters only |
| `goal_hint` | Markers such as “чтобы”, “для”, “нужно” (“so that”, “for”, “need”), in ru/en/es | Absent |
| `turn_context` | The `history` parameter | Accepted but unused in [`classify_intent`](../src/engine/intent.py) |

### Dimensions

Letters in the “Signal” column: R — regex; H — head over an existing embedding; O — offline or host model only.

| # | Dimension | Values | Controls | Signal | Label reliability (from the literature) | How to label it here | Evidence | Currently in `classify_intent` |
|---|---|---|---|---|---|---|---|---|
| 1 | **primary_domain** (with a “regulated domain” flag: tax, law, medicine, finance) | tax/legal, finance, code, medical, general… | routing, skill pool, factuality path | H (kNN or LR). Keywords only for exact identifiers | Human agreement on the primary label is κ≈0.62 (0.55 for intent, a moderate difference; 25 classes versus 10). **Multi-label** GPT-4 versus humans: κ 0.102 for domain versus 0.271 for intent, so domain performs worst here. Skills span multiple domains, so this labeling rule cannot simply be transferred to the skill pool | LLM pseudo-labels plus a human audit; one primary label | [TnT-LLM, 2024](https://arxiv.org/abs/2403.12173), [Rethinking Predictive Modeling for LLM Routing, 2025](https://arxiv.org/html/2505.12601v2), [Arch-Router, 2025](https://arxiv.org/abs/2506.16655) | Absent; the router only has keyword_veto |
| 2 | **action / task_shape** | lookup, explain, compare, debug, plan, create, compute, converse | implant slot, format | R over `instruction_span`, then H | Limited by the human intent-agreement ceiling (κ≈0.55) | Same as above | [Arch-Router, 2025](https://arxiv.org/abs/2506.16655) | Partial: `mode` over the full query, first match wins, does not affect implant selection |
| 3 | **cognitive_demand** | LOW (recall, explain) / HIGH | implant budget of 0 or 1+ | R (verbs) | CogRAG does not publish classifier accuracy, so “the binary version is stable” is an author choice, not a measurement. For chat, the only evidence is quadratically weighted κ=0.76 between GPT-4 and the authors on 100 English Copilot dialogues, which is lenient toward adjacent levels | Two annotators, binary labels | [The Use of Generative Search Engines…, 2024](https://arxiv.org/abs/2404.04268), [CogRAG, 2026](https://arxiv.org/html/2604.25928), [Cross-Dataset Bloom…, 2026](https://arxiv.org/html/2606.13684v1) | Indirectly through `tier` |
| 4 | **layer_applicability** (for each layer), with levels 0 / 1 / full | skills: none/any; implants: 0/1/full | layer gates, budget | R+H | Binary labels are more reliable than multi-label annotations. The “none” class is the hardest (30.52% in Adaptive-RAG) | Outcome-derived labels, with the caveats in E1 | [Adaptive-RAG, 2024](https://aclanthology.org/2024.naacl-long.389/), [RAGate, 2024](https://arxiv.org/abs/2407.21712), [Guarded Query Routing, 2025](https://arxiv.org/html/2505.14524), [DOTS, 2024](https://arxiv.org/abs/2410.03864) | Only for implants through `implant_budget` (`IMPLANT_NEED_GATE=intent`, off by default); absent for skills |
| 5 | **formal_symbolic** | yes / no | CoT and computation implants | R (`_MATHY`, code, “=”) | A surface feature; whether it predicts need here is untested | Test in E1 | [To CoT or not to CoT?, 2024](https://arxiv.org/abs/2409.12183) | Partial: `mode=compute` |
| 6 | **answer_volatility** | never / slow / fast; false_premise | fact filter in skills, date requirement | R (rates, “текущий” / “current”, year), then H | The taxonomy is defined; accuracy on ru has not been tested | LLM plus audit | [FreshLLMs, 2023](https://arxiv.org/abs/2310.03214), [Mitigating Temporal Misalignment…, 2023](https://aclanthology.org/2023.emnlp-main.879/), [TIDE, 2026](https://arxiv.org/abs/2608.08512) | Absent |
| 7 | **artifact_kind / volume / has_imperatives** | prose, code, log, URL; none / short / long; yes / no | marking mode, removal before embedding | R | Regex stability ≠ usefulness; untested | Counterfactual pairs needed, E2 | [Spotlighting, 2024](https://arxiv.org/abs/2403.14720), [SEP, 2024](https://arxiv.org/abs/2403.06833) | Only counters in `depth_score` |
| 8 | **intent_multiplicity / constraint_count** | 1 or 2+ subqueries; number of constraints | enables “merge → rerank” | R | The literature sets no threshold on constraint count (the benefit is “concentrated on queries with many constraints”); tune it on data | From data | [When Should Queries Be Decomposed?, 2026](https://arxiv.org/abs/2606.08577), [Re-Invoke, 2024](https://arxiv.org/abs/2408.01875) | Counters |
| 9 | **task_structure / flow_data_dependence** | single operation, sequential chain, or parallel subtasks; fixed or data-dependent flow | I4 plan | R plus O | Unknown | Defer | [Scaling Agent Systems, 2025](https://arxiv.org/abs/2512.08296v3), [CaMeL, 2025](https://arxiv.org/abs/2503.18813) | Absent |
| 10 | **constraint_conflict** | none / format / length / language | rules, removing persona format | R | Medium | Defer | [Control Illusion, 2025](https://arxiv.org/abs/2502.15851), [What Prompts Don't Say, 2025](https://arxiv.org/abs/2505.13360) | Partial: `suppress_persona_format` (converse only) |
| 11 | **specification / ambiguity** | clear or vague; importance of the missing detail 1–3 | clarification or explicit assumption | O | Low: 54.25% for a prompted LLM | Defer | [CLAMBER, 2024](https://arxiv.org/abs/2405.12063), [Tell Me More!, 2024](https://arxiv.org/abs/2402.09205) | Absent |
| 12 | **turn_role** | new task / adds a constraint / correction | consolidating instructions into one block, cache | R over history | Unknown | Defer | [LLMs Get Lost…, 2025](https://arxiv.org/abs/2505.06120) | Absent |
| 13 | **query_language** | ru / en / es / mixed | enabling keywords, metric breakdowns | R (`language.py`) | High | — | [M3-Embedding, 2024](https://arxiv.org/abs/2402.03216) | Absent: not imported at runtime |
| 14 | **external_verifiability** (narrow scope) | presence or absence of a pasted artifact / skill source | permits verification implants | R | The MCP server does not know the client's tools and cannot run tests. Having an artifact ≠ external feedback | Defer | [Large Language Models Cannot Self-Correct Reasoning Yet, 2023](https://arxiv.org/abs/2310.01798) | Absent |

**Stakes.** I removed a separate dimension based on Llama Guard. Category S6 flags **unsafe** specialized advice, so “какая ставка НДС” (“what is the VAT rate?”) would be classified as safe. Its F1 of 0.900 measures danger classification across all 14 categories, not “is this a legal question?”, and ru is unsupported ([Llama Guard 3 8B model card, 2024](https://huggingface.co/meta-llama/Llama-Guard-3-8B)). The regulated-domain flag is more simply derived from #1.

### Priority

0. **Rebuild the evaluation set first** (E0). Without this, no dimension can be evaluated honestly: “none” currently coincides with data source and language.
1. **A lightweight decomposer plus `query_language` (R, almost free).**
   - Removes cases where pasted text activates `mode`.
   - Enables metric breakdowns by language.
   - Provides input for #2, #7, and #8.
   - Does not fix F2 (see I1).
2. **Three-level `layer_applicability` (#4), including a skill gate.** This direction has the most mature literature. Our gain (F4) must be checked again on held-out data.
3. **`primary_domain` (#1, H)**, but only after identifying the router stage where F7 fails (E3).
   - kNN or LR loses Arch-Router's “new route without retraining” property.
   - The calling LLM already reads agent descriptions at ROUTE_REQUIRED and is closer to Arch-Router's mechanism. The fix may lie in the candidates and descriptions rather than adding a head.
4. **`answer_volatility` (#6)** for server-side filtering of volatile facts in skills (F8).
5. **Implant selection:** a “what is needed?” head (H, #2/#3/#5) versus host-model catalog selection (O). An A/B test decides (E2).

Defer #9–#12 and #14: reliability is low, the dimension requires an LLM call per query, or the evidence weighs against it.

## 4. Relationship to our findings

| F | What the literature says | What to change |
|---|---|---|
| **F1** (range 0.12–0.27; thresholds 0.75/0.85 never gate) | **Explains it.** Absolute cosine similarity is an artifact of model training ([multilingual-e5-base model card, 2023](https://huggingface.co/intfloat/multilingual-e5-base)). Similarity alone cannot say “none” ([Guarded Query Routing, 2025](https://arxiv.org/html/2505.14524)). A plausible cause is embedding collapse on long texts ([Length-Induced Embedding Collapse…, 2024](https://arxiv.org/abs/2410.24200)). Local check (not an F measurement): e5-large has a 512-token window; median implant index text is 301 tokens (6 of 57 are truncated), versus 656 for skills (46 of 71 are truncated). The mechanism has not been measured. **Counterargument:** mean subtraction increases isotropy, yet reducing isotropy improves most tasks ([Stable Anisotropic Regularization, 2024](https://arxiv.org/html/2305.19358v3)). A wider range ≠ a better gate | Replace absolute thresholds with z-scores (with `IMPLANT_TRIGGER_BOOST=1.0`) or a conformal threshold per layer. Evaluate every calibration option by gate metrics (none-rate and coverage), not range width. A “when to use” field shorter than 128 tokens is a separate variant in E2 |
| **F2** (implants selected by topic; hit@3 0.14, MRR 0.095) | **Predicts this for unsupervised similarity**: embeddings without task-specific training capture domain ([TnT-LLM, 2024](https://arxiv.org/abs/2403.12173)), and embedding models struggle with relevance that requires reasoning ([BRIGHT, 2024](https://arxiv.org/abs/2407.12883)). **But** trained LR over embeddings in TnT-LLM matches GPT-4 on intent. I withdraw the RAGate comparison: there, 12–16% of turns need **knowledge**; here we mean **reasoning methods**, and our 52% “none” share is determined by dataset composition. Interpreting F2 as instruction/data confusion is our analogy with SEP | Three candidates to replace cosine selection: (a) a trained “what is needed?” head; (b) the host model selects from `get_catalog()` with a “none” option; (c) an extracted statement of need (requires an LLM, unlikely to suit runtime). Analysis / solution / verification slots, each with “none” (DOTS). Add queries “about prompting” to the evaluation set |
| **F3** (0 keyword hits; 0 on ru/es) | **Partly explains it.** Query and required-item vocabularies barely overlap ([Retrieval Models Aren't Tool-Savvy, 2025](https://arxiv.org/abs/2503.01763)) and diverge across languages ([M3-Embedding, 2024](https://arxiv.org/abs/2402.03216)). But our ru/es samples are only voice commands labeled “none”, so “0 on ru/es” cannot be separated from query type | Keep the skill keyword boost only for exact identifiers. Test on ru/es queries that actually need skills (currently absent from the set) |
| **F4** (intent gate: 0.480 → 0.586, CI +0.045..+0.173) | **Matches a published pattern** (Adaptive-RAG, SKR, [To CoT or not to CoT?, 2024](https://arxiv.org/abs/2409.12183), [PET-Select, 2024](https://arxiv.org/abs/2409.16416)). **But the evidence is weak:** the heuristic was tuned on the same 110 ids, and “none” coincides with “voice command” | Run on a held-out set assembled from other sources, including ru/es queries that need implants. Only then enable it by default |
| **F5** (LR on 52 examples ≈ heuristic; weight on tier_lite) | **A warning.** This is most likely a shortcut through source type ([The Illusion of Role Separation, 2025](https://arxiv.org/abs/2505.00626)). Cross-dataset generalization drops by 0.25–0.28 ([Cross-Dataset Bloom…, 2026](https://arxiv.org/html/2606.13684v1)). kNN is a cheap, strong baseline ([Rethinking Predictive Modeling…, 2025](https://arxiv.org/html/2505.12601v2)). SetFit works well at small n ([SetFit, 2022](https://arxiv.org/abs/2209.11055)) | Leave-one-source-out validation (WildBench / MASSIVE / CLINC) and validation by language. Do not split randomly |
| **F6** (trigger-phrase reranking: hit@3 0.25 → 0.18) | **Explains it.** Reranking in tool retrieval often hurts (ToolRet). Added vocabulary creates noise in top-k ([When do Generative Query and Document Expansions Fail?, 2023](https://arxiv.org/abs/2309.08541)). The reranking benefit in the 2026 paper comes from constraint checking, not added vocabulary | Remove the trigger boost from the z-score gate during evaluation. Adopt any reranking only after an A/B test |
| **F7** (a Russian tax question goes to the universal agent) | **Unverified.** `routing.jsonl` has no queries whose expected agent is lawyer and no Russian tax or legal questions. The failing stage (semantic cache, keyword_veto, candidate list, or LLM selection at ROUTE_REQUIRED) has not been identified. The literature suggests hypotheses: domain is a separate axis ([Arch-Router, 2025](https://arxiv.org/abs/2506.16655)); English keywords do not match ru ([M3-Embedding, 2024](https://arxiv.org/abs/2402.03216)) | First trace the stages (E3), then decide whether to fix descriptions and candidates or add a domain head |
| **F8** (outdated rates in skills) | **Explains it, with a remedy stronger than tags.** Models readily accept coherent, convincing evidence ([Adaptive Chameleon or Stubborn Sloth, 2023](https://arxiv.org/abs/2305.13300)). Incorrect context overrides correct model knowledge in more than 60% of cases ([ClashEval, 2024](https://arxiv.org/abs/2404.10198)). A date can make an outdated value seem **more legitimate** ([ConflictBank, 2024](https://arxiv.org/abs/2408.12076)). Provenance tags are barely used, while server-side date filtering works ([Metadata, Structure, or Strategy?, 2026](https://arxiv.org/abs/2606.29645)). “May be outdated” warnings do not improve accuracy ([When Facts Change, 2026](https://aclanthology.org/2026.findings-acl.103/)). Skills contain mixed content: procedures are instructions, rates are data. Demoting an entire skill to data risks making the model ignore procedures ([The Instruction Hierarchy, 2024](https://arxiv.org/abs/2404.13208)) | Move volatile facts out of skill bodies into a separate store with validity-period and effective-date fields. Filter server-side using `answer_volatility`. Keep procedures in the system prompt |
| **F9** (personas do not improve factual accuracy) | **Compatible, but narrow.** F9 concerns facts only. Expertise framing affects behavior more than instruction placement ([Control Illusion, 2025](https://arxiv.org/abs/2502.15851)). We have not measured persona's role in requirement compliance | Do not drop personas for I4 without an A/B test. Get facts from fresh sources rather than the persona |
| **F10** (one LLM annotator, n=110) | **A stronger warning than it first appeared.** Multi-label LLM annotation is the least reliable (TnT-LLM). Outcome-derived labels for open chat require an LLM judge, which is the same single LLM again, only in a different role. Judge–human agreement on MT-Bench is at best κ≈0.51 ([Reliability without Validity, 2026](https://arxiv.org/abs/2606.19544)). Cross-language judge agreement is about 0.3 ([How Reliable is Multilingual LLM-as-a-Judge?, 2025](https://arxiv.org/abs/2505.12201)); judges are weaker on ru ([REPA, 2025](https://arxiv.org/abs/2503.13102)). If the judge is no better than the generator, human labeling can be reduced by at most a factor of two ([Limits to scalable evaluation at the frontier, 2024](https://arxiv.org/abs/2410.13341)) | Human alt-test: at least 3 people on 50–100 stratified examples ([The Alternative Annotator Test, 2025](https://arxiv.org/abs/2501.10970)). The judge scores each arm pointwise against a rubric ([Pairwise or Pointwise?, 2025](https://arxiv.org/abs/2504.14716)). Report κ rather than percentage agreement |

## 5. Experiment plan

Shared rules for all experiments:
- paired comparisons on the same ids (paired bootstrap; McNemar for binary outcomes);
- standard errors clustered by source and agent;
- K≥3 responses per arm ([Adding Error Bars to Evals, 2024](https://arxiv.org/abs/2411.00640)).

F4 (a CI of ±0.06) implies a paired-difference SD of about 0.34. This gives n≈0.92/m²: roughly 92 queries for a margin of 0.10 and roughly 370 for a margin of 0.05. At n=110, non-inferiority can be tested only with a margin of about 0.10.

**E0. Rebuild the set and audit the labels (prerequisite).**
- Add:
  - ru/es queries that need implants and skills (not voice commands);
  - en commands that need implants;
  - Russian tax and legal questions whose expected agent is lawyer;
  - queries “about prompting”.
- Goal: prevent “none” from coinciding with source and language.
- Human audit: at least 3 people on 50–100 examples, stratified by language, domain, and “none”.
- Metric: human–LLM κ separately for binary “is an implant needed?” labels and multi-label “which implants?” annotations; alt-test.
- Baseline: 0 human-reviewed labels (F10).
- Success: cross-tabulated proportions are specified (source × language × none), with no cells where source determines “none”. The binary label passes the alt-test. If the multi-label annotation does not pass, hit@3 and MRR (F2, F6) become secondary metrics.

**E1. A three-level layer gate on held-out data.**
- Arms: no gate; current `classify_intent` (F4); add `formal_symbolic` and `cognitive_demand` over `instruction_span`; kNN or SetFit over the embedding.
- Outcome-derived labels: the cheapest option (0 / 1 / full) that is non-inferior according to the judge.
- Cost: 110 × 3 levels (0 / 1 / full) × K=3 = 990 generations plus 990 judge assessments. The gate arms require no new generations: each arm selects one of the three levels for a query, and its metrics use the already scored responses at that level (the no-gate arm is the “full” level). Validate the judge against the E0 audit.
- Leave-one-source-out validation.
- Metrics: utility, implants per query, false loads on “none”.
- Baselines: 0.480 and 2.0 without a gate; 0.586 and 1.49 with a gate (F4, in-sample).
- Success on the held-out set (at least ~92 queries):
  - the gate is non-inferior to the no-gate arm at a margin of 0.10 (paired bootstrap);
  - implants per query are strictly below 2.0;
  - report ru/es separately. If the F4 gain holds only on MASSIVE/CLINC, treat the gate as a command detector.

**E2. How to select implants.**
- Evaluate all arms with the E1 gate and `IMPLANT_TRIGGER_BOOST=1.0`:
  - (a) the full query, as today;
  - (b) `instruction_span`;
  - (c) a “when to use” field shorter than 128 tokens instead of the description and body;
  - (d) a trained “what is needed?” head over the embedding;
  - (e) the host model selects through `get_catalog()` with a “none” option.
- Two types of counterfactual pairs: (i) the same imperative phrase as a user instruction and inside a pasted log — label the expected decision for each member separately; they may differ because the log phrase must not become an instruction; (ii) the same instruction before and after the artifact — both members have the same expected decision.
- Metrics:
  - hit@3 and MRR (only if the multi-label annotations passed E0);
  - for pairs (i): share of pairs where both members receive their expected decision; for pairs (ii): share of pairs where the decision flips;
  - utility;
  - latency of arm (d) against the 12–53 ms budget from intent.py.
- Baselines: hit@3 0.14, MRR 0.095 (F2); 0.25 for the preferred pool (F6).
- Success: the best arm's 95% paired-bootstrap CI for its difference from (a) excludes 0; it resolves more pairs (i) correctly than (a) and has fewer flips on pairs (ii) than (a) (McNemar for each type); utility is non-inferior to E1.

**E3. Trace F7 and evaluate a domain head.**
- For the E0 set of ru/en/es tax and legal questions, record the outcome at each router stage: cache, keyword_veto, candidate list, and selection at ROUTE_REQUIRED.
- Compare fixes to descriptions and candidates with kNN or LR over `primary_domain`. Add OOD queries to test abstention.
- Metrics: routing accuracy by language, share of correct “not a specialist” decisions.
- Baseline: measure it first. F7 has no numeric baseline.
- Success: Russian questions about rates route to lawyer; the ru/en gap narrows relative to the current router (McNemar).

**E4. Prompt assembly: I1 markup and volatile facts (F8).**
- This directly tests the main unknown: whether I1 changes answer quality.
- Arms:
  - current behavior;
  - tagged artifacts, query last;
  - tags plus an extraneous instruction embedded in the artifact (aligned and misaligned);
  - the entire skill in the system prompt;
  - the skill with volatile facts extracted and filtered by date on the server;
  - no skill.
- Metrics, scored pointwise against a rubric: utility; share of misaligned artifact instructions followed; share of aligned instructions followed; share of answers presenting an outdated number as current; accuracy on stable facts.
- Baseline: no numeric value; F8 is qualitative. Measure the “current behavior” arm first.
- Success: tags do not reduce utility (margin 0.10); the share of outdated numbers falls, while accuracy on stable facts and procedural compliance do not fall.

Not covered by the experiments: #9–#12 and #14. These are deliberately deferred.

## 6. What we do not know

- **Our data.** Until E0 and held-out E1 are complete, we do not know whether F4 and F5 contain any “reasoning needed” signal beyond “voice command”. The “none” share in real traffic is unknown.
- **I1 and I5 for prompt components** have not been studied directly. No one has split a chat request into instruction, artifact, and goal, used those pieces as retrieval keys for skills and implants, and measured answer accuracy. Instruction/data separation as an explanation of F2 is our analogy.
- **Host-model catalog selection.** It helped only the strongest model (Select-then-Solve, MRP). Its behavior with our host and with a “none” option has not been tested.
- **Outcome-derived labels do not solve the judge problem.** For open chat, they require an LLM judge. Adaptive-RAG achieved 54.52% overall and 30.52% on “none” with such labels. Power analysis, not the method, determines how many human labels we need.
- **ru/es.**
  - There are no tool- or strategy-retrieval benchmarks in ru/es.
  - TnT-LLM reports only an aggregate drop on non-English inputs (from −2.7% to ~−10%, depending on the embedding model).
  - Judges are weaker on ru (REPA); cross-language agreement is about 0.3.
- **Per-layer calibration (I6)** is an extrapolation from task-specific adapters and conformal filtering of a single retriever. Conformal guarantees hold only relative to our labels.
- **The F1 mechanism.** The narrow range is consistent with the [multilingual-e5-large model card](https://huggingface.co/intfloat/multilingual-e5-large): low InfoNCE temperature (0.01) compresses cosine similarity into 0.7–1.0. Length-induced collapse is an unmeasured hypothesis. e5-large has a 512-token window, and 46 of the 71 skill index texts exceed it. The retrieval loss caused by truncation is unknown.
- **Personas.** F9 concerns facts only. We have not measured persona's effect on format and requirement compliance, while Control Illusion suggests it may be substantial.
- **I4 within a single chat request** has not been studied. The entire multi-agent evidence base consists of benchmarks (code, mathematics, research evaluations).
- **I2 security.** Which specific 12 defenses “The Attacker Moves Second” bypassed has not been verified. The effect of datamarking on code and logs is our inference.
- **The vendor's “up to 30%”** comes from internal tests without a published methodology; it is not peer-reviewed evidence.
