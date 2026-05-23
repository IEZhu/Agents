# Implants A/B — semantic-only vs preferred-implants fast-path

Ground truth derived per-sample from each ``expected_agent``'s declared
``preferred_implants`` in agent frontmatter. Baseline runs the retriever
with no fast-path; treatment forwards the same implants into the retriever
so they are deterministically loaded ahead of semantic results.

- Total samples: 110
- Drift: 0
- Fetch errors: 0
- Samples with non-empty expected (baseline): 110
- Samples with non-empty expected (treatment): 110

| Metric | Baseline (semantic only) | Treatment (preferred fast-path) | Δ |
| --- | --- | --- | --- |
| precision@1 | 0.02 | 1.00 | +0.98 |
| precision@3 | 0.03 | 0.94 | +0.91 |
| precision@5 | 0.03 | 0.57 | +0.53 |
| recall@1 | 0.01 | 0.37 | +0.35 |
| recall@3 | 0.04 | 1.00 | +0.96 |
| recall@5 | 0.07 | 1.00 | +0.93 |
| MRR | 0.06 | 1.00 | +0.94 |
