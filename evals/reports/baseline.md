# Agents-Core eval baseline — 2026-05-03 (0ecf0a2)

## BLUF

- Routing top-1: **77.3%** (85/110)
- Routing top-3: **78.2%**
- Skills P@3: **0.17** | R@5: **0.27** | MRR: **0.30**
- Tier accuracy: **60.9%** (67/110)

Loader: total=110, drift=0, fetch_errors=0

## 1. Routing accuracy

- **Top-1 accuracy**: 85/110 = 77.3%
- **Top-3 accuracy**: 86/110 = 78.2%

**Prediction method distribution:**
- fallback: 63
- keyword: 47

**Per-language top-1 accuracy:**
- `en`: 66/90 (73%)
- `es`: 10/10 (100%)
- `ru`: 9/10 (90%)

**Per-source top-1 accuracy:**
- `clinc_oos`: 20/20 (100%)
- `massive_en`: 10/10 (100%)
- `massive_es`: 10/10 (100%)
- `massive_ru`: 9/10 (90%)
- `wildbench`: 36/60 (60%)

**Top-10 miss-cases (sorted by label_confidence desc):**

| id | expected | predicted | method | conf |
|---|---|---|---|---|
| `wildbench-00388` | `child_psychologist` | `universal_agent` | fallback | 0.85 |
| `wildbench-00562` | `software_engineer` | `universal_agent` | fallback | 0.85 |
| `wildbench-00224` | `security_expert` | `universal_agent` | fallback | 0.80 |
| `wildbench-00337` | `math_scientist` | `universal_agent` | fallback | 0.78 |
| `wildbench-00061` | `math_scientist` | `universal_agent` | fallback | 0.78 |
| `wildbench-00231` | `deep_researcher` | `medical_expert` | keyword | 0.75 |
| `wildbench-00838` | `deep_researcher` | `universal_agent` | fallback | 0.70 |
| `wildbench-00205` | `data_analyst` | `fitness_coach` | keyword | 0.70 |
| `wildbench-00880` | `math_scientist` | `software_engineer` | keyword | 0.70 |
| `wildbench-00189` | `security_expert` | `sysadmin` | keyword | 0.70 |

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
