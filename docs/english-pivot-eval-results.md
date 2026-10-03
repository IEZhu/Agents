# English pivot rule: A/B results

Date: October 2, 2026. Rule: `rules/rule-english-pivot.mdc` at `f164959`; contexts
built at `2e9eaa2`, branch `feat/english-pivot-rule`. These results describe that
rule text and the recorded models and settings only. The rule was not adopted; its
text stays on that branch.

## Question

`english-pivot` tells the model to translate a non-English request into an English
working brief, work from it in English and answer under `language-match`. Quotes,
identifiers and source material stay in the original, and tasks where language is
the point stay in that language. The hypothesis was that English processing helps
models follow English instructions and reason better on non-English requests.

## Method

The [component ablation harness](../evals/ablation/README.md) with the prepared
case set [`english_pivot_cases.json`](../evals/datasets/english_pivot_cases.json):
24 one-shot, tool-less cases, of which 8 are Russian reasoning tasks, 5 Russian
requests with text to keep verbatim, 6 Russian tasks where language is the task,
2 English requests with Russian instructions and 3 English controls. A separate
review checked the rubric facts; its 13 corrections were in place before any
judging, and the two cases whose user message changed were answered again.

Both arms use the production context of the case's agent; the `without` arm only
cuts the rule's section (1006 characters). All 48 contexts were byte-identical
between the cloud and the local builds (same `ctx_sha256`). A blind pairwise judge
compares the two answers of a case in both orders, 48 verdicts per run.

| Answers | Where | Judge | Settings |
|---|---|---|---|
| `qwen/qwen3.8-27b` | OpenRouter, `deepinfra/bf16` | `google/gemini-3.8-flash` | reasoning medium, `max_tokens` 12000; run 1 temperature 0 and seed 7, run 2 the provider's default temperature |
| `google/gemini-3.8-flash` | OpenRouter, `google-ai-studio` | `google/gemini-3.8-flash` | as for Qwen |
| `claude-opus-5-5` | cloud routine, Claude Code agents | Opus agents, and separately the Gemini judge | `answers.js` and `judges.js` |

The OpenRouter answers and verdicts come from
[`evals/ablation/hosted.py`](../evals/ablation/hosted.py); its judge uses the
criteria of `workflows/judges.js` at temperature 0 with low reasoning. The Opus run
is on branch `claude/ablation-english-pivot` (`bb322cc`); the OpenRouter runs are
kept outside the repository.

## Results

Verdicts per run ("robust" counts cases where the same arm won in both orders):

| Answers | Judge | Run | with | without | tie | net | robust with / without |
|---|---|---|---:|---:|---:|---:|---:|
| Qwen 3.8 27B | Gemini | 1 | 23 | 13 | 12 | +10 | 10 / 5 |
| Qwen 3.8 27B | Gemini | 2 | 13 | 23 | 12 | −10 | 4 / 7 |
| Gemini 3.8 Flash | Gemini | 1 | 8 | 25 | 15 | −17 | 2 / 10 |
| Gemini 3.8 Flash | Gemini | 2 | 11 | 22 | 15 | −11 | 3 / 8 |
| Opus 5.5 | Opus | 1 | 13 | 14 | 21 | −1 | 4 / 4 |
| Opus 5.5 | Gemini | 1 | 16 | 16 | 16 | 0 | 3 / 4 |

Per case, over all runs of a model: a case scores +1 for each verdict the rule's arm
wins and −1 for each it loses. Sign test (exact, two-sided) over the 21 cases where
the rule applies; ties drop out:

| Answers | Runs | rule better | rule worse | even | score sum | p |
|---|---|---:|---:|---:|---:|---:|
| Qwen 3.8 27B | 1 + 2 | 8 | 8 | 5 | −1 | 1.00 |
| Gemini 3.8 Flash | 1 + 2 | 2 | 10 | 9 | −28 | 0.039 |
| Opus 5.5 | both judges | 7 | 6 | 8 | +1 | 1.00 |

The three English controls, where the rule does not apply, summed to +1 (Qwen), 0
(Gemini) and −2 (Opus).

Net verdicts by group, run 1 / run 2 (Opus: Opus judge / Gemini judge):

| Group | Qwen | Gemini | Opus |
|---|---|---|---|
| Russian reasoning (8) | +2 / −5 | −6 / −2 | −5 / 0 |
| Russian, text kept verbatim (5) | 0 / −2 | +1 / +1 | +1 / +1 |
| Russian, language is the task (6) | +4 / −2 | −10 / −8 | +2 / −2 |
| English with Russian instructions (2) | +2 / 0 | −2 / −2 | +2 / +2 |

## Reading

- No model gains from the rule. Qwen's first run looked positive (+10), and its
  second run reversed it (−10): one sample per arm is within the noise of a
  24-case set.
- Gemini 3.8 Flash loses consistently, most in the tasks where language is the
  point (robust 0 / 9 over two runs), although the rule tells the model to work in
  the original language there. Most of its losses are small margins: the judge
  found the rule's answers slightly less complete or natural.
- Opus 5.5 is unaffected under both judges.
- The rule costs 1006 characters in every bundle.

Observed along the way, not measured:

- In two unscored diagnostic requests of the bakery-slogan case with the rule,
  Qwen's reasoning ran in English and its reply called «батон» slang for a stick
  of dynamite, which it is not. The scored answers carry English-derived wordplay
  in both arms (run 2 without the rule: «Батон. Не дирижёрский.», from a
  conductor's baton), so this is Qwen's behavior rather than the rule's. The
  reasoning language without the rule was not inspected.
- With reasoning excluded from the reply, Qwen returned empty content eight times
  in a row for that case under the rule; the ninth attempt succeeded.
- In Gemini's first run, the rule's answer to the quatrain case was visible
  drafting cut off mid-word; a repeat request gave a normal quatrain in both arms,
  each after about 10 000 reasoning tokens.

## Limits

- 24 cases, one or two samples per arm; only the Gemini result passes p < 0.05.
- One-shot, tool-less chat answers. The rule's effect on tool arguments (routing
  queries, `run_flow` requests) and on multi-step flows is not measured.
- Gemini judges its own answers; Opus agents judge Opus answers. Each bias applies
  to both arms of a pair.
- Cloud answers are written by agents that read the context as a file, not by an
  API call with a system prompt.
