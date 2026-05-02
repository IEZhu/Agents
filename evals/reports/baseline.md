# Agents-Core eval baseline — 2026-05-03 (34fb44e)

## BLUF

- Routing top-1: **53.6%** (59/110)
- Routing top-3: **60.9%**
- Skills P@3: **0.17** | R@5: **0.27** | MRR: **0.30**
- Tier accuracy: **60.9%** (67/110)

Loader: total=110, drift=0, fetch_errors=0, local_cache=True

## 1. Routing accuracy

- **Top-1 accuracy**: 59/110 = 53.6%
- **Top-3 accuracy**: 67/110 = 60.9%

**Prediction method distribution:**
- fallback: 58
- keyword: 52

**Per-language top-1 accuracy:**
- `en`: 40/90 (44%)
- `es`: 10/10 (100%)
- `ru`: 9/10 (90%)

**Per-source top-1 accuracy:**
- `clinc_oos`: 19/20 (95%)
- `massive_en`: 10/10 (100%)
- `massive_es`: 10/10 (100%)
- `massive_ru`: 9/10 (90%)
- `wildbench`: 11/60 (18%)

**Top-10 miss-cases (sorted by label_confidence desc):**

| id | expected | predicted | method | conf |
|---|---|---|---|---|
| `wildbench-00016` | `medical_expert` | `universal_agent` | fallback | 0.93 |
| `wildbench-00343` | `software_engineer` | `fitness_coach` | keyword | 0.92 |
| `wildbench-00291` | `database_admin` | `code_reviewer` | keyword | 0.92 |
| `wildbench-00578` | `education_tutor` | `child_psychologist` | keyword | 0.90 |
| `wildbench-00286` | `math_scientist` | `devops_engineer` | keyword | 0.85 |
| `wildbench-00252` | `prompt_engineer` | `code_reviewer` | keyword | 0.85 |
| `wildbench-00483` | `sysadmin` | `debate_moderator` | keyword | 0.85 |
| `wildbench-00549` | `prompt_engineer` | `code_reviewer` | keyword | 0.85 |
| `wildbench-00433` | `prompt_engineer` | `debate_moderator` | keyword | 0.85 |
| `wildbench-00349` | `literary_writer` | `child_psychologist` | keyword | 0.85 |

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
