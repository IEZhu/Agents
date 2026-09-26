# Langfuse telemetry: export and analysis

Agents-Core sends a trace to Langfuse for each MCP tool call (`route_and_load`,
`get_agent_context`, `retrieve_skills`, `retrieve_implants`, `read_history`) and one per
`log_interaction`, whose `response` generation holds the logged query and answer. These
scripts export that data and summarise it.

## Procedure

```bash
# 1. Export everything (keys from the environment or .env). Write OUTSIDE the repository:
#    the export holds queries and answers as logged (capped at 2000 and 5000 characters).
python evals/telemetry/export_langfuse.py ~/evals-runs/langfuse-$(date +%F) [--since YYYY-MM-DD]

# 2. Text-free tables: lengths, statuses, agents, loaded components, heuristic language.
python evals/telemetry/extract.py ~/evals-runs/langfuse-$(date +%F)

# 3. First-pass statistics -> stats.json, crossed with evals/ablation/RESULTS.md.
python evals/telemetry/stats.py ~/evals-runs/langfuse-$(date +%F)
```

The export uses the v2 observations API with cursor pagination; the legacy
`/api/public/traces` and `/api/public/observations` endpoints stop working on Langfuse
Cloud on 2026-11-16. `traces.jsonl` is rebuilt from each trace's root observation, in
the shape the legacy endpoint returned, so `extract.py` reads either. Checked against
a legacy export of the same period, every extracted table matched apart from rows at the
cut-off second. 30 days (about 4,500 observations) export in a few minutes. The CSV
tables are small and contain no query or answer text; the raw JSONL files do, so keep
them local.

Read the numbers with the caveats below before drawing conclusions: several metrics
are shaped by how the data is logged rather than by behaviour.

## What the 2026-09 analysis found

Period 2026-08-28 to 2026-09-26: 834 `route_and_load`, 793 `get_agent_context`, 555
logged answers. Four analysts computed the findings from the raw export and a
skeptic per analyst recomputed each one: 10 were confirmed, 12 corrected, none refuted.
The numbers below are the corrected ones.

### Routing costs more than it saves

- **The routing cache serves 3.8% of calls** (32 of 834); only 15 were semantic hits at
  distance < 0.05. A replay of the stored router vectors shows what a looser cutoff
  would do: distance < 0.08 serves 11.9% of calls, < 0.09 serves 17.3%, and the cached
  agent agrees with the LLM's own pick 85–88% of the time. Most queries are simply new,
  so even then over 80% miss. `ROUTER_SIMILARITY_THRESHOLD` is a near-duplicate cutoff
  by design; loosening it trades hits for 12–15% disagreement.
- **Continuing conversations reload the same persona.** 73% of ROUTE_REQUIRED calls end
  with the LLM picking the agent it already had. Each repeat re-sends the full list of
  43 agents (about 8 KB) and a 15–20 KB prompt, and waits about 6 s. Two causes, about
  equal: half of the `context_hash` values passed back were older than the 600 s TTL of
  the hash cache, so the sticky branch never ran; for the rest, the sticky branch
  requires a cached neighbour at distance < 0.05. Protocol 2 (the client keeps the
  persona) avoided both on the days it was used, on a small sample.
- **The candidate list is always all 43 agents**, unranked. Clients follow
  ROUTE_REQUIRED 99.4% of the time, so a ranked short list (about 5 agents with
  descriptions, the rest by name) would cut about 6.5 KB per miss.

### Enrichment injects by default, not by relevance

- **The relevance thresholds never filter.** Every `retrieve_implants` call returned
  exactly `n_results`: semantic picks sat at cosine distance 0.15–0.21 against a cutoff
  of 0.85. The skill cutoff (0.75) never binds either. The cutoffs were calibrated for
  a smaller embedding model than the multilingual e5-large used in production.
- **The capable skill tier is effectively dead.** When preferred skills could fill the
  semantic slots, capable skills won 3 of 5,314 eligible slots (0.06%). Skills declared
  capable, including ablation winners, do not reach prompts.
- **`implant-regression-first` is in 73% of implant calls**, forced as a preferred implant
  of the busiest agents, while its triggers matched no query in 30 days. It is the
  largest single component (about 10% of prompt characters). Removing it from
  `preferred_implants` alone saves little, because the freed slot is refilled by
  another implant (thresholds never filter); the saving needs fewer implants per load
  (a need gate or a lower preferred floor).
- **Removed components still leak in.** `implant-output-priming`, confirmed harmful in the
  ablation and dropped from two agents' preferred lists in #86, still arrives through
  semantic top-up. `skill-token-economy` (#94) had never been injected at all in the
  period, so removing it changed no prompt.
- **Enrichment volume and measured benefit do not line up.** Implants are 60% of
  enrichment characters and only 32% of implant loads have a positive ablation sign.
  `skill-content-structure` loads on 96% of calls yet has no ablation verdict for its
  current text.

### Prompt size

A persona load is a median of 15,000 characters (about 4,000 tokens); the deep tier
doubles it, mostly through full skill texts instead of compiled lines. Persona prompts
plus candidate lists came to about 21 million characters in 30 days. About 18% of that
was the same persona and rules text re-sent within one conversation.

### The telemetry itself

- **Turns cannot be linked.** No trace has a `sessionId` or `userId`; `request_id` is
  not passed from `route_and_load` to `get_agent_context` to `log_interaction` (15% of
  logs carry it); retrieval spans land in separate root traces. How many routed turns
  end with a log can only be bounded: 22–61%.
- **Logged answers are not the answers users saw.** `log_interaction` stores the text the
  model passes, capped at 5,000 characters, and that text was often a digest. The
  footer rate (46% overall) rose in steps, from 5% before 2026-09-18 to 90% after
  2026-09-20, with the logging instructions. English logged answers to Russian
  queries (25–29% of those logs, depending on how the footer is counted) follow the
  same regime. Neither is a behaviour metric.
- **No build, environment, model, token or error data.** `release` and `version` are
  never set, `environment` is always `default`, the service name is `unknown_service`,
  token usage is always 0 and no span is marked as an error. Test and eval traffic
  cannot be separated from interactive use.
- `read_history` had 3 real calls in 30 days (62 were probes from a daemon migration).

### Recommended changes, in order

1. Keep the persona on continuing turns: protocol 2 by default, or have v1 return
   NO_CHANGE for the same agent with a valid hash, and raise the hash-cache TTL to 1–2 h.
2. Return a ranked short candidate list instead of all 43 agents.
3. Telemetry: set `session_id`, client and protocol tags and `release` (the running
   commit) on every trace (`propagate_attributes` in `src/server.py`); chain
   `request_id` from route to load to log; compute `response_len`, `truncated`,
   `has_footer` and languages server-side in `log_interaction`; mark ERROR responses;
   record the nearest router distance on ROUTE_REQUIRED.
4. Recalibrate the skill and implant relevance cutoffs for e5-large, and decide what the
   capable tier is for (compete with a boost, or document it as never auto-loaded).
5. Take harmful components out of the semantic index, not only out of preferred lists.
6. Rank ablation re-tests by production exposure: `skill-content-structure` (current
   text), `implant-chain-of-verification`, `implant-iteration-budget`.

The skeptic reports, the per-finding evidence and the analysis scripts of this run are
kept with the export, outside the repository.
