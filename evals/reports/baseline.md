# Agents-Core eval baseline — 2026-05-03 (affe551)

## BLUF

- Routing top-1: **66.4%** (73/110)
- Routing top-3: **71.8%**
- Skills P@3: **0.17** | R@5: **0.27** | MRR: **0.30**
- Tier accuracy: **60.9%** (67/110)

Loader: total=110, drift=0, fetch_errors=0, local_cache=True

## 1. Routing accuracy

- **Top-1 accuracy**: 73/110 = 66.4%
- **Top-3 accuracy**: 79/110 = 71.8%

**Prediction method distribution:**
- fallback: 60
- keyword: 50

**Per-language top-1 accuracy:**
- `en`: 54/90 (60%)
- `es`: 10/10 (100%)
- `ru`: 9/10 (90%)

**Per-source top-1 accuracy:**
- `clinc_oos`: 19/20 (95%)
- `massive_en`: 10/10 (100%)
- `massive_es`: 10/10 (100%)
- `massive_ru`: 9/10 (90%)
- `wildbench`: 25/60 (42%)

**Top-10 miss-cases (sorted by label_confidence desc):**

| id | expected | predicted | method | conf |
|---|---|---|---|---|
| `wildbench-00578` | `education_tutor` | `data_analyst` | keyword | 0.90 |
| `wildbench-00252` | `prompt_engineer` | `universal_agent` | fallback | 0.85 |
| `wildbench-00483` | `sysadmin` | `database_admin` | keyword | 0.85 |
| `wildbench-00549` | `prompt_engineer` | `literary_writer` | keyword | 0.85 |
| `wildbench-00433` | `prompt_engineer` | `medical_expert` | keyword | 0.85 |
| `wildbench-00388` | `child_psychologist` | `universal_agent` | fallback | 0.85 |
| `wildbench-00562` | `software_engineer` | `sysadmin` | keyword | 0.85 |
| `wildbench-00656` | `literary_writer` | `deep_researcher` | keyword | 0.85 |
| `wildbench-00515` | `math_scientist` | `investigative_analyst` | keyword | 0.82 |
| `wildbench-00224` | `security_expert` | `daily_briefing` | keyword | 0.80 |

## 2. Skill retrieval

### Skills
- Samples with non-empty `expected_skills`: 56/110
- precision@1: 0.20
- precision@3: 0.17
- precision@5: 0.12
- recall@1: 0.11
- recall@3: 0.24
- recall@5: 0.27
- MRR: 0.30

## 3. Implant retrieval (informational)

### Implants
- Samples with non-empty `expected_implants`: 0/110
- No labeled expectations — metrics not computed.

## 4. Tier inference

- Accuracy: 67/110 = 60.9%

**Confusion matrix (rows=expected, cols=predicted):**

| expected \ predicted | `lite` | `standard` | `deep` |
|---|---|---|---|
| `lite` | 35 | 18 | 4 |
| `standard` | 0 | 9 | 16 |
| `deep` | 0 | 5 | 23 |
