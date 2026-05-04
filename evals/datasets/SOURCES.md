# Eval Dataset Sources

Public datasets used to seed the routing/retrieval golden set. **Source query texts are
not committed** to this repo: only labels, source pointers (`source_idx`), and
`source_row_hash` (sha256 of the original query text) are stored in
`evals/datasets/routing.jsonl`. Each eval run re-fetches sources via
`evals/scripts/fetch.py`, verifies the hash, and discards the text after the run.

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
- **Why**: tests the `universal_agent` fallback for queries outside the 50 specialist domains.
- **License**: CC-BY-3.0.
- **Card**: https://huggingface.co/datasets/clinc_oos

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
2. Run `python -m evals.scripts.fetch --validate` to confirm schema & accessibility.
3. Add a section to this file with: HF id, config, split, schema fields used, license,
   why it's useful, link to dataset card.
4. Re-run `evals/scripts/label_with_claude.py` for the new source.
