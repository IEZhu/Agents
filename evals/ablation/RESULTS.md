# Component ablation sweep: results (2026-09-25)

Model: Opus 5.5 (`claude-opus-5-5`) in Claude Code cloud routine sessions.
Scope: 129 components (4 rules, 70 skills, 55 implants); the pilot covered the other 4.
Design: 2 in-scope cases per component (ru + en), written and adversarially checked;
one answer with the component and one without; a blind pairwise judge in both orders.
Totals: 258 cases, 516 answers, 516 verdicts, 0 missing. Raw runs: `evals/ablation/runs/batch-01..13/` on branch `archive/ablation-runs-2026-09`.

## How to read this

- **Implant rows compare the implant alone with no implant.** Other implants that production
  retrieval would add are left out of both arms, so a row measures the implant on its own, not
  its marginal effect inside the production implant set. Rule and skill rows keep the rest of the
  production prompt and remove only the component.
- **Judges favour the second answer.** Of the decisive verdicts, B won 284 and A won 157 (64% B).
  Each pair is judged in both orders for that reason, and the "robust" columns count only
  cases where the same arm won in both orders.
- **Case level:** robust with 81, robust without 56, tie in both orders 13, order-dependent 108 (42%).
- **Aggregate effect is modestly positive.** Decisive verdicts favour the component 57% of the time
  (251 to 190, with 75 ties); among cases with the same winner in both orders it is 59% (81 to 56).
  17 components won both cases robustly, against 9.1 expected if no component mattered.
- **Individual negatives are within noise.** 10 components lost both cases robustly, against 9.1
  expected by chance. Almost every one of those verdicts is "small" and says both answers met the
  rubric. Treat a single component's row as a screening signal, not a verdict.

By kind (sign of robust with minus robust without):

| Kind | positive | zero | negative |
|---|---|---|---|
| rule | 2 | 2 | 0 |
| skill | 36 | 13 | 21 |
| implant | 18 | 20 | 17 |

## Worth a closer look

- `skill-react-pattern`: the only "clear" robust loss with a concrete cause. The with-skill support
  prompt skipped a `get_order` check before `issue_refund`; the without-skill answer had it.
- `skill-jurisdiction-ru`: in the with-skill answer, one judge flagged an apparently invented
  "law of 04.07.2026 No. 228-FZ", and another flagged a wrong break-even derivation.
- Reasoning-scaffold implants lost robustly: `implant-tree-of-thought`, `implant-graph-of-thoughts`,
  `implant-self-refine` and `implant-take-a-deep-breath`. All margins were small. This is consistent
  with the pilot's finding that implants barely change Opus answers.
- Robust winners (both cases, both orders): implant-constitutional-critique, implant-contrastive-cot,
  implant-dynamic-few-shot, implant-role-play-expert, implant-steel-man, rule-serve-the-request,
  skill-bio-protocol-design, skill-clickup-markdown, skill-code-generation, skill-consultative-intake,
  skill-creative-craft, skill-fact-verification, skill-jurisdiction-kz, skill-mcp-development,
  skill-prompt-security, skill-psy-digital-wellbeing, skill-reasoning-logic.

Next step: re-test the flagged components on 6 fresh cases each before changing or removing any.

## Full table

| Component | Cases | with | without | tie | net | robust with | robust without | clear/large |
|---|---|---|---|---|---|---|---|---|
| skill-dev-testing | 2 | 0 | 4 | 0 | -4 | 0 | 2 | 0 |
| skill-jurisdiction-es | 2 | 0 | 4 | 0 | -4 | 0 | 2 | 0 |
| skill-jurisdiction-ru | 2 | 0 | 4 | 0 | -4 | 0 | 2 | 0 |
| skill-react-pattern | 2 | 0 | 4 | 0 | -4 | 0 | 2 | 2 |
| skill-roblox-development | 2 | 0 | 4 | 0 | -4 | 0 | 2 | 0 |
| skill-token-economy | 2 | 0 | 4 | 0 | -4 | 0 | 2 | 0 |
| implant-graph-of-thoughts | 2 | 0 | 4 | 0 | -4 | 0 | 2 | 0 |
| implant-self-refine | 2 | 0 | 4 | 0 | -4 | 0 | 2 | 0 |
| implant-tree-of-thought | 2 | 0 | 4 | 0 | -4 | 0 | 2 | 0 |
| implant-take-a-deep-breath | 2 | 0 | 4 | 0 | -4 | 0 | 2 | 0 |
| skill-3d-print-search | 2 | 0 | 3 | 1 | -3 | 0 | 1 | 0 |
| implant-automatic-reasoning | 2 | 0 | 3 | 1 | -3 | 0 | 1 | 0 |
| implant-output-priming | 2 | 0 | 3 | 1 | -3 | 0 | 1 | 0 |
| implant-react | 2 | 0 | 3 | 1 | -3 | 0 | 1 | 0 |
| skill-decision-frameworks | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| skill-epistemic-method | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| skill-dev-api-design | 2 | 0 | 2 | 2 | -2 | 0 | 1 | 0 |
| skill-error-recovery | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| skill-dense-summarization | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| skill-jurisdiction-co | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| skill-jurisdiction-cy | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| skill-fitness-programming | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 1 |
| skill-jurisdiction-ge | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| skill-tech-writing | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| skill-report-formats | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| implant-contextual-compression | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| implant-chain-of-code | 2 | 0 | 2 | 2 | -2 | 0 | 1 | 0 |
| implant-chain-of-verification | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| implant-layer-of-thoughts | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| implant-self-harmonized-cot | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| implant-reflexion | 2 | 0 | 2 | 2 | -2 | 0 | 1 | 0 |
| implant-thread-of-thought | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| implant-step-back-prompting | 2 | 1 | 3 | 0 | -2 | 0 | 1 | 0 |
| skill-psy-nvc | 2 | 1 | 2 | 1 | -1 | 0 | 1 | 0 |
| skill-prompt-design-process | 2 | 1 | 2 | 1 | -1 | 0 | 1 | 0 |
| skill-pedagogy | 2 | 1 | 2 | 1 | -1 | 0 | 1 | 0 |
| implant-buffer-of-thoughts | 2 | 1 | 2 | 1 | -1 | 0 | 1 | 0 |
| implant-chain-of-note | 2 | 1 | 2 | 1 | -1 | 0 | 1 | 0 |
| implant-self-consistency | 2 | 0 | 2 | 2 | -2 | 0 | 0 | 0 |
| implant-self-discover | 2 | 0 | 2 | 2 | -2 | 0 | 0 | 0 |
| skill-3d-platforms | 2 | 1 | 2 | 1 | -1 | 0 | 0 | 0 |
| skill-jurisdiction-us | 2 | 1 | 2 | 1 | -1 | 0 | 0 | 0 |
| implant-complexity-based-prompting | 2 | 1 | 2 | 1 | -1 | 0 | 0 | 0 |
| implant-metacognitive-prompting | 2 | 1 | 2 | 1 | -1 | 0 | 0 | 0 |
| implant-program-of-thoughts | 2 | 0 | 1 | 3 | -1 | 0 | 0 | 0 |
| implant-prompt-chaining | 2 | 1 | 2 | 1 | -1 | 0 | 0 | 0 |
| skill-agent-handoff | 2 | 2 | 2 | 0 | +0 | 0 | 0 | 0 |
| rule-anti-sycophancy | 2 | 2 | 2 | 0 | +0 | 0 | 0 | 0 |
| skill-agnotology | 2 | 2 | 2 | 0 | +0 | 1 | 1 | 0 |
| skill-blender-scripting | 2 | 2 | 2 | 0 | +0 | 1 | 1 | 0 |
| skill-confidence-markers | 2 | 2 | 2 | 0 | +0 | 1 | 1 | 1 |
| skill-caveman-tokenomics | 2 | 2 | 2 | 0 | +0 | 1 | 1 | 0 |
| skill-multi-step-planning | 2 | 2 | 2 | 0 | +0 | 0 | 0 | 0 |
| skill-system-design | 2 | 2 | 2 | 0 | +0 | 0 | 0 | 0 |
| skill-temporal-validation | 2 | 2 | 2 | 0 | +0 | 1 | 1 | 0 |
| implant-active-prompting | 2 | 0 | 0 | 4 | +0 | 0 | 0 | 0 |
| implant-chain-of-draft | 2 | 2 | 2 | 0 | +0 | 0 | 0 | 0 |
| implant-least-to-most-prompting | 2 | 2 | 2 | 0 | +0 | 1 | 1 | 0 |
| implant-dr-cot | 2 | 1 | 1 | 2 | +0 | 0 | 0 | 0 |
| implant-premortem | 2 | 2 | 2 | 0 | +0 | 1 | 1 | 0 |
| implant-multi-agent-debate | 2 | 2 | 2 | 0 | +0 | 0 | 0 | 0 |
| implant-rephrase-and-respond | 2 | 2 | 2 | 0 | +0 | 1 | 1 | 0 |
| implant-second-order-thinking | 2 | 2 | 2 | 0 | +0 | 0 | 0 | 0 |
| implant-reverse-cot | 2 | 0 | 0 | 4 | +0 | 0 | 0 | 0 |
| skill-forensic-process | 2 | 2 | 1 | 1 | +1 | 0 | 0 | 0 |
| skill-jurisdiction-mx | 2 | 2 | 1 | 1 | +1 | 0 | 0 | 0 |
| skill-legal-citation | 2 | 2 | 1 | 1 | +1 | 0 | 0 | 0 |
| implant-cumulative-reasoning | 2 | 2 | 1 | 1 | +1 | 0 | 0 | 0 |
| implant-logic-of-thought | 2 | 2 | 1 | 1 | +1 | 0 | 0 | 0 |
| implant-plan-and-solve-plus | 2 | 2 | 1 | 1 | +1 | 0 | 0 | 0 |
| rule-honest-uncertainty | 2 | 2 | 0 | 2 | +2 | 0 | 0 | 0 |
| implant-chain-of-table | 2 | 2 | 0 | 2 | +2 | 0 | 0 | 0 |
| implant-narrative-of-thought | 2 | 2 | 0 | 2 | +2 | 0 | 0 | 0 |
| skill-analysis-critical | 2 | 2 | 1 | 1 | +1 | 1 | 0 | 0 |
| rule-language-match | 2 | 2 | 1 | 1 | +1 | 1 | 0 | 0 |
| skill-bio-mechanism | 2 | 2 | 1 | 1 | +1 | 1 | 0 | 0 |
| skill-dev-clean-code | 2 | 2 | 1 | 1 | +1 | 1 | 0 | 2 |
| skill-mathematical-reasoning | 2 | 2 | 1 | 1 | +1 | 1 | 0 | 0 |
| skill-agentic-loops | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| skill-bio-protocols | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 1 |
| skill-dev-security | 2 | 2 | 0 | 2 | +2 | 1 | 0 | 0 |
| skill-dev-debugging | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| skill-git-conventions | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| skill-jurisdiction-rs | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 2 |
| skill-psy-cbt | 2 | 2 | 0 | 2 | +2 | 1 | 0 | 0 |
| skill-psy-child-dev | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| skill-product-frameworks | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 1 |
| skill-prompt-engineering | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| skill-source-trust-tiers | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 2 |
| skill-purchase-research | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| implant-analogical-prompting | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| skill-wayback-machine | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| implant-causal-reasoning | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| implant-chain-of-symbol | 2 | 2 | 0 | 2 | +2 | 1 | 0 | 1 |
| implant-generated-knowledge | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| implant-output-automata | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| implant-recursion-of-thought | 2 | 2 | 0 | 2 | +2 | 1 | 0 | 2 |
| implant-skeleton-of-thought | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 1 |
| implant-uncertainty-quantification | 2 | 3 | 1 | 0 | +2 | 1 | 0 | 0 |
| skill-dev-performance | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| skill-literary-devices | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 2 |
| skill-narrative-craft | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| skill-mermaid-best-practices | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| skill-prompt-techniques | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| skill-structured-output | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| skill-ux-principles | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| implant-chain-of-abstraction | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| skill-web-search | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| implant-maieutic-prompting | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| implant-decomposed-prompting | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| implant-system-2-attention | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 0 |
| implant-verify-assumptions | 2 | 3 | 0 | 1 | +3 | 1 | 0 | 2 |
| rule-serve-the-request | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| skill-bio-protocol-design | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 2 |
| skill-clickup-markdown | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 4 |
| skill-code-generation | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| skill-consultative-intake | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| skill-creative-craft | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| skill-fact-verification | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| skill-jurisdiction-kz | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 2 |
| skill-mcp-development | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| skill-psy-digital-wellbeing | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| skill-prompt-security | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| skill-reasoning-logic | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| implant-contrastive-cot | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 1 |
| implant-constitutional-critique | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| implant-dynamic-few-shot | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |
| implant-role-play-expert | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 1 |
| implant-steel-man | 2 | 4 | 0 | 0 | +4 | 2 | 0 | 0 |


## Re-test of flagged components (2026-09-26)

14 components (the 10 robust losers plus the 4 at net −3) re-run with 6 fresh cases each
(3 ru + 3 en), same design; judges could WebSearch a contested fact. Raw runs:
`evals/ablation/runs/retest-a..c/` on branch `archive/ablation-runs-2026-09`. skill-react-pattern and skill-jurisdiction-ru ran with the
first version of their #85 fixes. Judges still favour position B (64% of decisive verdicts);
41 of 84 case pairs were order-dependent.

Confirmed negative: skill-token-economy, implant-self-refine, implant-output-priming,
skill-jurisdiction-es (acted on in #86 and #88; skill-token-economy was removed in #94).
The other ten came out neutral or positive, so their sweep result was noise.

| Component | Cases | with | without | tie | net | robust with | robust without | clear/large |
|---|---|---|---|---|---|---|---|---|
| skill-token-economy | 6 | 3 | 9 | 0 | -6 | 1 | 4 | 0 |
| implant-self-refine | 6 | 2 | 8 | 2 | -6 | 1 | 4 | 0 |
| implant-output-priming | 6 | 2 | 8 | 2 | -6 | 1 | 3 | 3 |
| skill-jurisdiction-es | 6 | 4 | 7 | 1 | -3 | 1 | 3 | 0 |
| implant-graph-of-thoughts | 6 | 5 | 6 | 1 | -1 | 1 | 1 | 2 |
| implant-react | 6 | 4 | 4 | 4 | +0 | 1 | 0 | 0 |
| skill-dev-testing | 6 | 6 | 5 | 1 | +1 | 2 | 1 | 2 |
| skill-3d-print-search | 6 | 7 | 5 | 0 | +2 | 3 | 2 | 1 |
| implant-take-a-deep-breath | 6 | 4 | 2 | 6 | +2 | 1 | 0 | 0 |
| implant-automatic-reasoning | 6 | 4 | 2 | 6 | +2 | 1 | 0 | 0 |
| skill-jurisdiction-ru | 6 | 6 | 4 | 2 | +2 | 3 | 1 | 3 |
| skill-react-pattern | 6 | 7 | 4 | 1 | +3 | 3 | 1 | 0 |
| implant-tree-of-thought | 6 | 7 | 3 | 2 | +4 | 3 | 1 | 3 |
| skill-roblox-development | 6 | 7 | 2 | 3 | +5 | 2 | 0 | 0 |

