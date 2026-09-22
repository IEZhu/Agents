# Intent classifier

Replaces `infer_tier`'s single length axis with two orthogonal ones, per
[#64](https://github.com/IEZhu/Agents/issues/64): **what kind of reasoning the
task needs** (`mode`) and **how much enrichment it earns** (`tier`).

Off by default. Enable with `INTENT_CLASSIFIER_ENABLED=1`.

## Why

`infer_tier` (`src/engine/enrichment.py`) decided the budget like this:

```python
if len(stripped) < 50 and not _COMPLEX_SIGNALS.search(stripped): return "lite"
if _COMPLEX_SIGNALS.search(stripped) or len(stripped) > 300:     return "deep"
return "standard"
```

Two measured consequences:

- On the MCP-vs-vanilla bench (`evals/reports/2026-06-06_234304_mcp_vs_vanilla_gemini.json`)
  40 of 50 queries landed in the heaviest `deep` tier, the MCP arm cost 12.6× the
  input tokens of vanilla, and the quality outcome was a statistical tie.
  **35 of those 40 deep assignments came from `len > 300` alone.** Length is not
  cognitive depth.
- On the golden set (`evals/datasets/routing.jsonl`, 110 labeled samples) the rule
  scores 67/110 = 60.9%, and its errors are systematic: 16 of 25 `standard`
  samples are pushed up to `deep`, 18 of 57 `lite` samples up to `standard`.

The legacy regex compounded it: matching bare substrings, it fired `deep` on any
query merely containing `план`, `compare`, `design` or `review`.

## Design

`classify_intent(query) -> TaskProfile` is **pure, synchronous, embedding-free and
dependency-free**. It runs on the hot path before any `await` in
`route_and_load`, and is unit-testable with no vector store — which matters while
[#68](https://github.com/IEZhu/Agents/issues/68) (no `tests/conftest.py`, so
importing `enrichment` can reindex the live stores) is open.

```python
@dataclass(frozen=True)
class TaskProfile:
    mode: TaskMode                    # converse retrieve create explain operate analyze compute
    tier: Tier                        # lite standard deep — unchanged legacy vocabulary
    depth_score: int                  # bounded structural score
    skill_pool_size: int              # -> skill_retriever.retrieve(n_results=...)
    skill_render: SkillRender         # -> format_skills_for_prompt(compiled=...)
    implant_budget: int               # base count, before the preferred_implants floor
    suppress_persona_format: bool     # -> strip the persona's "## Output Format"
    confidence: float
    signals: tuple[str, ...]          # why this profile — for debug logs and eval triage
```

**Mode is detected lexically**, most-specific first, so the costlier method wins
when a query matches several ("write an essay comparing X and Y" is an analysis
delivered as prose). A mode's default tier is a **floor**: the structural score
can promote it one step, never demote it.

**Length contributes at most 2 of the `INTENT_DEEP_AT` (5) points** needed to
promote, so no prompt reaches `deep` on size alone.

An embedding-centroid variant was considered and rejected: it moves ~3.6% of
input tokens while adding 12–53 ms to a hot path whose p95 is 37–58 ms, and it
adds a persisted centroid artifact that can drift out of sync with
`EMBEDDING_MODEL` exactly the way `data/.skills_hash` already does.

## Results

`python -m evals.runners.run_tier --compare`

| set | arm | accuracy | deep-share | under | over |
|---|---|---|---|---|---|
| full (110) | legacy | 67/110 = 60.9% | 39.1% | 5 | 38 |
| full (110) | classifier | **77/110 = 70.0%** | **28.2%** | 11 | 22 |
| held-out (47) | legacy | 27/47 = 57.4% | 44.7% | 1 | 19 |
| held-out (47) | classifier | 29/47 = 61.7% | 29.8% | 5 | 13 |

**The accuracy gain is not statistically significant.** Paired exact McNemar:
p=0.17 on the full set, p=0.80 on the held-out half. Treat +9.1 pp as directional
only.

What *is* solid is the deep-tier share: 39.1% → 28.2% (held-out 44.7% → 29.8%).
That is a deterministic property of the assignment, not a statistical estimate,
and the token saving follows from it directly, because `deep` renders full skill
bodies (~2.4 KB median each) where `standard` renders one-liners (~153 chars).

Honest limitations:

- **The lexicons were written against one half of the golden set.** The 76.2%
  tuning-half vs 61.7% held-out gap is a 14.5 pp generalisation gap. The held-out
  number is the one to believe.
- **All 110 labels are machine-generated**, `human_reviewed: false`, mean
  `label_confidence` 0.672. The measurement ceiling is label quality.
- **The structural promotion path is nearly inert on this dataset** — it changes
  exactly one of 110 assignments at `INTENT_DEEP_AT=5`. Practically, today's win
  comes from lexical mode detection; the scorer exists for the long multi-part
  specs the 110-sample set underrepresents, and its threshold is *not*
  evidence-calibrated.
- **The error profile moved.** Legacy over-provisioned (38 over / 5 under); the
  classifier over-provisions less but under-provisions more (22 over / 11 under).
  Under-provisioning is the riskier direction for answer quality, so the `deep`
  recall drop (23/28 → 19/28) is the thing to watch in the generation A/B.
  An asymmetric-loss retune was tried and made held-out accuracy *worse*
  (59.6%), so this is not fixable by moving thresholds.

## Back-compat

`tier` never stops being the string it was:

- `infer_tier(query) -> Tier` keeps its name, module, signature and sync-ness; it
  is now a projection over `classify_intent`, falling back to
  `_legacy_infer_tier` (kept verbatim and callable, as the A/B's control arm).
- `resolve_profile()` returns `None` when the flag is off, which is the signal to
  every downstream layer to keep deriving the budget from the tier exactly as
  before. Both `pytest tests/` runs — flag off and flag on — pass 958 tests.
- The session cache key carries `profile.cache_token` instead of the bare tier,
  because once render mode and pool size are decoupled from the tier, two
  profiles can share a tier and build different prompts. The token is colon-free,
  so the documented `agent:query_hash:X` three-segment shape survives.
- The agent's declared `preferred_implants` remain a **floor** on the implant
  count (43 of 43 agents declare some; dropping that term would silently starve
  every persona). `tests/test_intent.py::TestImplantBudgetParity` pins the
  unified formula against the legacy per-tier branches.

## Not done here

- `mode → method bundle` (per-mode implants and cross-cutting skills) from #64.
  Today `mode` drives only `suppress_persona_format`; the budget still flows
  through the tier. Wiring implants per mode needs the generation A/B first,
  since it changes *which* implants load, not just how many.
- Rendering `compiled` at the `deep` tier. This is the single largest remaining
  token lever (4 full bodies ≈ 2450 tokens vs ≈ 40 compiled) but it changes what
  `tier="deep"` means for `tests/test_persona_bundle.py`, and its quality effect
  is unmeasured. It belongs behind its own flag and its own A/B.
- The generation-side A/B (`run_mcp_vs_vanilla`) that #64's acceptance criteria
  require: `intent_fit` must not regress and the deep share must drop. This PR
  only establishes the classifier and its offline accuracy.
