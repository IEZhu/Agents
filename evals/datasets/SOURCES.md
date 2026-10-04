# Eval Dataset Sources

Public datasets configured in `evals/scripts/fetch.py:DATASETS`. WildBench, MASSIVE and
CLINC150 seed the routing/retrieval golden set; `run_mcp_vs_vanilla` samples its queries
from the same list (`--dataset`, default `wildbench`). **Source query texts are
not committed** to this repo: only labels, source pointers (`source_idx`), and
`source_row_hash` (`sha256:` plus the first 16 hex digits of the SHA-256 of the original
query text) are stored in `evals/datasets/routing.jsonl`. Each eval run re-fetches the
texts through the `fetch.py` dataset specs, verifies the hash, and discards the text
after the run. On a machine that ran `label_with_claude --prepare`, the texts stay on
disk in the gitignored `evals/datasets/_unlabeled.jsonl`, and runs read them from there.

## Datasets

### 1. AI2 WildBench

- **HF id**: `allenai/WildBench`
- **Config**: `v2`
- **Split**: `test`
- **Schema fields used**: `conversation_input[*].role==user → content` (first user turn)
- **Why**: curated hard real-user prompts derived from Chatbot Arena with quality filtering.
- **License**: AI2 ImpACT (non-commercial / non-commercial-derivatives by default — consult dataset card before redistributing source content).
- **Card**: https://huggingface.co/datasets/allenai/WildBench

### 2. MASSIVE (multilingual)

- **HF id**: `mteb/amazon_massive_intent` (parquet mirror of `AmazonScience/MASSIVE`; the original
  script-based loader is no longer supported by recent `datasets` versions)
- **Configs**: `ru`, `en`, `es`
- **Split**: `test`
- **Schema fields used**: `text` (utterance), `lang`, `label` (string intent name)
- **Why**: only reliable multilingual real-user-utterance source providing balanced RU/EN/ES coverage.
- **License**: CC-BY-4.0 (inherited from upstream Amazon MASSIVE).
- **Card**: https://huggingface.co/datasets/mteb/amazon_massive_intent
- **Upstream attribution**: FitzGerald et al. (2022) — Amazon Science.

### 3. CLINC150 (out-of-scope)

- **HF id**: `clinc_oos`
- **Config**: `plus`
- **Split**: `test`
- **Schema fields used**: `text`, `intent` (integer; `42` corresponds to the `oos` label in the `plus` config)
- **Why**: tests the `universal_agent` fallback for queries outside every specialist agent's domain.
- **License**: CC-BY-3.0.
- **Card**: https://huggingface.co/datasets/clinc_oos

### 4. LMSYS-Chat-1M (MCP-vs-vanilla bench only)

- **HF id**: `lmsys/lmsys-chat-1m`
- **Config**: none
- **Split**: `train`
- **Schema fields used**: `conversation[*].role==user → content` (first user turn)
- **Why**: real-world user/LLM chats for `run_mcp_vs_vanilla --dataset lmsys_chat_1m`; not
  used in `routing.jsonl`.
- **License**: LMSYS-Chat-1M License (research-only, gated). Requires an `HF_TOKEN` with
  access granted on the dataset card.
- **Card**: https://huggingface.co/datasets/lmsys/lmsys-chat-1m

## Hand-written sets

`evals/datasets/routing_ru.jsonl` holds Russian routing and retrieval labels written
in-house, with each request inline in `query` and no source pointer. The loader reads
inline texts without a fetch and checks `source_row_hash` only when a row has one;
pass such a set to `run_retrieval` or `run_cache_routing` with `--dataset`
(repeatable). The other files here (`no_fabrication.jsonl`, `persona_*.jsonl`,
`implant_labels.jsonl`, and the ablation case sets `english_pivot_cases.json` and
`embedding_ab_cases.json`) are also written in-house and hold no texts from the
sources above.

## Storage policy

- We commit only: derivative labels (our IP), source pointers (`source_idx`,
  `source_split`, `source_config`), and `source_row_hash` for drift detection.
- We do **not** commit: raw query texts from any of the above sources.
- HuggingFace `datasets` library caches downloads under `~/.cache/huggingface/`
  by default. That cache is system-wide and **not** managed or committed by this repo.

## Drift handling

If an upstream row changes (hash mismatch on re-fetch), the corresponding eval sample
is skipped and counted in the report's `drift_count` metric. Persistent drift across
many rows means the source dataset was updated upstream — re-label and re-baseline.

## Adding a new source

1. Add a `DatasetSpec` entry to `evals/scripts/fetch.py:DATASETS`.
2. Run `python -m evals.scripts.fetch --validate` to confirm schema & accessibility. It
   probes every configured source: without an `HF_TOKEN` that has LMSYS access it
   reports `lmsys_chat_1m` as FAIL and exits 1.
3. Add a section to this file with: HF id, config, split, schema fields used, license,
   why it's useful, link to dataset card.
4. To add the source to the golden set, give it a sample count in `DEFAULT_ALLOC` in
   `evals/scripts/label_with_claude.py` (and a row filter in `_SOURCE_FILTERS` if only
   part of the split applies), then relabel the whole set: run `./scripts/eval.sh prepare`,
   have each `evals/datasets/_batches/batch_NNN.md` labelled, save its JSON array as
   `evals/datasets/_batches/labels_NNN.json`, and run `./scripts/eval.sh aggregate`.
   `python -m evals.scripts.label_with_claude --label` (needs `ANTHROPIC_API_KEY`) is the
   one-step alternative. Both rewrite `routing.jsonl` from the full allocation. Do not
   pass `--source KEY` with the default output: it replaces the golden set with that one
   source, or with nothing for a key missing from `DEFAULT_ALLOC`.
