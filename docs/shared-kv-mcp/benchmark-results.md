# Local benchmark per-task results

Five repetitions of each task per performed arm/transport; temperature 0, seed 42, output cap 384, deadline 180 s. Redis arms were unperformed. All 220 requests completed operationally; 180 passed the frozen strict checker. There were no repair calls.

Client time here starts before submit and ends after terminal validation. It excludes prior context/grant creation and later cleanup. The primary whole-arm comparison in the [implementation report](implementation-report.md) includes setup, model startup, contexts/grants, all requests and cleanup. Model time includes adapter validation/private audit I/O; native prefill/decode spans remain in the private source report. p95 is the largest of five observations, not a population estimate.

Source report SHA256: `35959766d69fff6ce854514facbfe9dcf9be67f97bfc10b38f8138d8ab27686d`. Stable profile: `7a15e3bd65494166a2534274d730e4852cfaebac63810d3bbb5dd0b052d56164`. Frozen task SHA256: `d50f158303958f76184cfb969a115bd2e5b11ef515d89b9df286fdfeb9d582fe`.

| Arm / transport | Task | n | Checker | Client p50 / p95 ms | Model p50 ms | Input / evaluated / reused tokens | Output tokens |
|---|---|---:|---:|---:|---:|---:|---:|
| off / mcp | control_a | 5 | 5/5 | 286.12 / 506.07 | 276.26 | 465 / 465 / 0 | 30 |
| off / mcp | control_b | 5 | 5/5 | 294.55 / 390.51 | 286.55 | 465 / 465 / 0 | 30 |
| off / mcp | extract34 | 5 | 5/5 | 1915.38 / 2584.99 | 1906.30 | 3055 / 3055 / 0 | 120 |
| off / mcp | extract56 | 5 | 5/5 | 1983.87 / 2445.54 | 1972.53 | 3055 / 3055 / 0 | 125 |
| off / mcp | implementation | 5 | 0/5 | 1831.96 / 2190.72 | 1820.98 | 3105 / 3105 / 0 | 75 |
| off / mcp | long1 | 5 | 5/5 | 6524.34 / 8708.28 | 6510.05 | 3025 / 3025 / 0 | 920 |
| off / mcp | long2 | 5 | 5/5 | 6952.54 / 13658.20 | 6931.20 | 3035 / 3035 / 0 | 1110 |
| off / mcp | parent | 5 | 5/5 | 1577.62 / 2099.32 | 1568.67 | 3135 / 3135 / 0 | 65 |
| off / mcp | q7 | 5 | 5/5 | 1112.63 / 1242.64 | 1101.54 | 3030 / 3030 / 0 | 65 |
| off / mcp | q8 | 5 | 5/5 | 1227.75 / 1671.14 | 1217.89 | 3030 / 3030 / 0 | 65 |
| off / mcp | reviewer | 5 | 0/5 | 1492.89 / 2834.66 | 1481.46 | 3105 / 3105 / 0 | 10 |
| off / sdk | control_a | 5 | 5/5 | 323.96 / 518.80 | 317.58 | 465 / 465 / 0 | 30 |
| off / sdk | control_b | 5 | 5/5 | 335.31 / 537.85 | 328.97 | 465 / 465 / 0 | 30 |
| off / sdk | extract34 | 5 | 5/5 | 1921.35 / 3259.19 | 1915.24 | 3055 / 3055 / 0 | 120 |
| off / sdk | extract56 | 5 | 5/5 | 2133.48 / 3324.70 | 2115.64 | 3055 / 3055 / 0 | 125 |
| off / sdk | implementation | 5 | 0/5 | 1927.29 / 3097.95 | 1921.07 | 3105 / 3105 / 0 | 75 |
| off / sdk | long1 | 5 | 5/5 | 6322.26 / 9056.21 | 6314.85 | 3025 / 3025 / 0 | 920 |
| off / sdk | long2 | 5 | 5/5 | 7248.34 / 9136.56 | 7238.88 | 3035 / 3035 / 0 | 1110 |
| off / sdk | parent | 5 | 5/5 | 1844.40 / 2403.63 | 1838.14 | 3135 / 3135 / 0 | 65 |
| off / sdk | q7 | 5 | 5/5 | 1094.39 / 1324.13 | 1088.24 | 3030 / 3030 / 0 | 65 |
| off / sdk | q8 | 5 | 5/5 | 1299.38 / 3458.29 | 1293.65 | 3030 / 3030 / 0 | 65 |
| off / sdk | reviewer | 5 | 0/5 | 1645.25 / 2335.63 | 1637.09 | 3105 / 3105 / 0 | 10 |
| resident / mcp | control_a | 5 | 5/5 | 306.15 / 323.10 | 294.56 | 465 / 465 / 0 | 30 |
| resident / mcp | control_b | 5 | 5/5 | 299.06 / 384.37 | 290.53 | 465 / 465 / 0 | 30 |
| resident / mcp | extract34 | 5 | 5/5 | 1573.26 / 1768.73 | 1561.65 | 3055 / 3055 / 0 | 120 |
| resident / mcp | extract56 | 5 | 5/5 | 517.65 / 719.55 | 506.15 | 3055 / 150 / 2905 | 125 |
| resident / mcp | implementation | 5 | 0/5 | 1949.84 / 2185.42 | 1939.70 | 3105 / 3105 / 0 | 75 |
| resident / mcp | long1 | 5 | 5/5 | 5571.94 / 6740.43 | 5562.82 | 3025 / 3025 / 0 | 920 |
| resident / mcp | long2 | 5 | 5/5 | 5594.89 / 6095.86 | 5583.14 | 3035 / 100 / 2935 | 1110 |
| resident / mcp | parent | 5 | 5/5 | 361.77 / 376.91 | 351.70 | 3135 / 245 / 2890 | 65 |
| resident / mcp | q7 | 5 | 5/5 | 1136.33 / 1233.99 | 1126.59 | 3030 / 3030 / 0 | 65 |
| resident / mcp | q8 | 5 | 5/5 | 263.93 / 264.44 | 253.32 | 3030 / 100 / 2930 | 65 |
| resident / mcp | reviewer | 5 | 0/5 | 178.44 / 191.43 | 164.71 | 3105 / 215 / 2890 | 10 |
| resident / sdk | control_a | 5 | 5/5 | 351.77 / 396.31 | 345.78 | 465 / 465 / 0 | 30 |
| resident / sdk | control_b | 5 | 5/5 | 387.78 / 443.30 | 381.30 | 465 / 465 / 0 | 30 |
| resident / sdk | extract34 | 5 | 5/5 | 1880.85 / 3290.83 | 1874.54 | 3055 / 3055 / 0 | 120 |
| resident / sdk | extract56 | 5 | 5/5 | 689.51 / 1125.54 | 683.20 | 3055 / 150 / 2905 | 125 |
| resident / sdk | implementation | 5 | 0/5 | 2497.88 / 2847.28 | 2487.63 | 3105 / 3105 / 0 | 75 |
| resident / sdk | long1 | 5 | 5/5 | 6519.97 / 12193.44 | 6512.91 | 3025 / 3025 / 0 | 920 |
| resident / sdk | long2 | 5 | 5/5 | 5913.82 / 9770.94 | 5898.61 | 3035 / 100 / 2935 | 1110 |
| resident / sdk | parent | 5 | 5/5 | 415.75 / 873.96 | 409.16 | 3135 / 245 / 2890 | 65 |
| resident / sdk | q7 | 5 | 5/5 | 1154.92 / 1965.72 | 1148.59 | 3030 / 3030 / 0 | 65 |
| resident / sdk | q8 | 5 | 5/5 | 269.12 / 1141.70 | 259.86 | 3030 / 100 / 2930 | 65 |
| resident / sdk | reviewer | 5 | 0/5 | 198.01 / 297.45 | 188.83 | 3105 / 215 / 2890 | 10 |

The implementation task returned a JavaScript Math.max expression while the literal checker expected max; the prompt did not explicitly fix a programming language. The reviewer task returned an empty array, which failed the required object/finding schema. Both failures occurred in all repetitions/arms/transports. No prompt or checker was changed after scoring. Each of the eleven task IDs had exactly one distinct output SHA256 across the twenty performed arm runs.
