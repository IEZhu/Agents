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
                                      #    (converse only — see below)
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

Built end to end and **production-faithful** — a real prompt per golden query
using each label's `expected_agent`, mirroring `server._load_and_enrich`
including the `lite → standard` promotion — the injected prompt shrinks **4.5%**:
1,946,542 → 1,859,078 chars, mean 17,696 → 16,901 per query. Per query: 21
smaller, **52 larger**, 37 unchanged.

Effective tier distribution, which is not the same as the classifier's raw output:

| | `lite` | `standard` | `deep` |
|---|---|---|---|
| legacy | 0 | 67 | 43 |
| classifier | 48 | 31 | 31 |

Three things in that table matter more than the headline.

**`lite` was unreachable in production, in both arms.** 43 of 43 agents declare
`preferred_implants`, and the `lite → standard` promotion fired on every inferred
`lite`. Under the legacy rule that was invisible; under the classifier it would
have reduced the whole `lite` half of the change to nothing but format
suppression. The promotion is now waived when the classifier positively decides a
task needs no implants (`converse`/`retrieve`), which is what makes `lite`
reachable at all. `run_tier --compare` scores raw `infer_tier(query)` with no
agent metadata, so its tier distribution is the classifier's opinion, **not** the
tier the server applies — read it as classifier quality, not as production effect.

**52 of 110 prompts got larger, not smaller.** A query moving `standard → lite`
loses two semantic skills and its implants but gains *full* core-skill bodies
where `standard` rendered one-liners, because the legacy render mapping ties
`compiled` to `standard` alone. For `universal_agent` that is +4,915 chars against
roughly −300. The legacy mapping is internally incoherent: the cheapest tier is
the most verbose per skill.

**So the cost lever is gated behind the render decision, which is out of scope
here.** Reverting `compiled`-at-`lite` (correctly, on quality grounds) also
removed most of the saving. The honest summary: this change buys a **better tier
signal** and the groundwork for #64's method bundles; it does **not** buy the
order-of-magnitude token reduction #64 is chasing. That needs the render
question — `compiled` at `deep`, or a render mode chosen per mode rather than per
tier — settled by a quality A/B. Tracked in
[#73](https://github.com/IEZhu/Agents/issues/73).

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

## Format suppression is narrower than #64 proposes

#64's table suppresses the persona's `## Output Format` for `converse`, `create`
and `retrieve`. Only `converse` does so here. Two reasons, both found by review
against the real personas:

- A persona's Output Format is not always a response template. For
  `medical_expert` it is where the mandated `### Safety` section lives — red
  flags requiring emergency care, contraindications, analysis limits. `create`
  fires on `summarize`/`draft`/`write`, so "draft a note summarizing these labs"
  would have removed it.
- Widening this needs a per-section allowlist, not a per-mode boolean. Nothing in
  a mode tells you which sections of a given persona are safe to drop.

`strip_output_format` is a line scanner, not a regex, because 10 of 23 personas
put a fenced template with **level-2 inner headings** inside that section, and
`prompt_engineer` teaches a prompt skeleton containing the literal line
`## Output Format` inside a fence. A regex stopping at the next `^## ` deleted a
fence opener while leaving its closer — flipping backtick parity and swallowing
the rest of the persona into a code block — and on 8 personas removed only the
heading, promoting the surviving template to apparent top-level sections. The
scanner matches only the first non-fenced `## Output Format` and ends the section
at the next non-fenced level-2 heading, so fence parity is preserved by
construction. `tests/test_intent.py::TestStripOutputFormatFenceAware` asserts
both properties across every persona in `agents/`.

## Second review round

A second high-effort review of the first two commits produced five more findings,
all reproduced before fixing:

- **The `converse` gate checked only total length**, so any request under
  `INTENT_CONVERSE_MAX_CHARS` that merely *contained* a greeting was classified
  `converse`: "Hi, compare Postgres vs MySQL" and "hey, debug this stack trace"
  got the lite tier, zero skills, zero implants and a stripped persona format.
  This is the same false positive the module exists to remove — it had only been
  fixed for long queries. The gate is now subtractive: remove every greeting
  token and permitted filler, and require that no alphabetic content survives.
- **A per-query suppression was baked into a session-scoped artifact.**
  `persona.load_persona` returns `NO_CHANGE` while the same agent stays active, so
  a v2 bundle is built once and reused. If the activating turn was a greeting,
  the persona kept its `## Output Format` stripped for the whole conversation.
  Suppression now lives only in the v1 per-query path, where `SESSION_CACHE` is
  keyed on the query hash.
- **`_CODE_ISH` used `re.DOTALL` with unanchored `.+`**, so "Select the best
  framework from this list" and "the import duties from China rose" were read as
  pasted code and won the mode. Replaced by `_looks_like_code`: a declaration
  match is decisive, while SQL needs three distinct keywords plus a structural
  token.
- **`lite` render reverted to full bodies** (see above).
- **Per-mode budget was dead configuration.** The budget was read with
  `next(p for p in _MODE_POLICY.values() if p["tier"] == tier)`, which only ever
  sees the first mode declaring a tier. `_TIER_BUDGET` is now an explicit
  tier→budget table and `_MODE_POLICY` keeps only what a mode owns (`tier`,
  `suppress_format`); a test pins that separation.

## Third review round

Six more findings; two were the same class of defect surviving a second fix.

- **Code detection read prose as code, for the third time.** `re.DOTALL` with
  unanchored `.+` (round 1), then three-SQL-keyword counting plus a "structural"
  token (round 2), both classified ordinary English as a systems operation —
  "Set the meeting on Monday and update the values from the deck" and
  "Import duties from China rose 12%". The cause is not a threshold: SQL's
  vocabulary *is* ordinary English (`on`, `set`, `from`, `values`, `update`) and
  `;`, `*`, `word.word` occur in prose. Keyword-counted SQL detection was
  **removed** rather than retuned a third time. What remains matches only
  declarations (`def x(`, `class X:`, a whole-line `import x`, `from x import y`),
  case-sensitively — "Import duties" begins a sentence, `import os` does not.
  Accepted trade-off: unfenced SQL is no longer detected and merely gets
  mis-budgeted; a fenced block is still scored as `code_fence`.
- **`_is_pure_greeting` ignored digits and operators**, so "hi, 2+2?" and
  "hi, 1234567 * 89 = ?" were small talk — lite tier, no implants, persona format
  stripped. The remainder check now rejects surviving alphanumerics and `_MATHY`.
- **`classify_intent` ran up to three times per request**, synchronously on the
  event loop, linear in query length (~12 ms per pass at 100 KB on this
  checkout). Memoized via `lru_cache(maxsize=8)`; repeats now cost nothing.
- **An unbalanced fence made `strip_output_format` delete the persona to EOF** —
  `in_fence` never cleared, so the terminating-heading branch never ran and Rules,
  Constraints and Safety were dropped silently. A malformed persona is now
  returned unchanged.
- **The promotion ran before the profile** (see Results).

## Back-compat

`tier` never stops being the string it was:

- `infer_tier(query) -> Tier` keeps its name, module, signature and sync-ness; it
  is now a projection over `classify_intent`, falling back to
  `_legacy_infer_tier` (kept verbatim and callable, as the A/B's control arm).
- `resolve_profile()` returns `None` when the flag is off, which is the signal to
  every downstream layer to keep deriving the budget from the tier exactly as
  before. Both `pytest tests/` runs — flag off and flag on — pass 1034 tests.
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
  is unmeasured. It belongs behind its own flag and its own A/B — see
  [#73](https://github.com/IEZhu/Agents/issues/73), which carries the measurements
  and the experiment design.
- The generation-side A/B (`run_mcp_vs_vanilla`) that #64's acceptance criteria
  require: `intent_fit` must not regress and the deep share must drop. This PR
  only establishes the classifier and its offline accuracy.
