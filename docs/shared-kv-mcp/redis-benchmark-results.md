# Redis controlled-restart per-task results

Five observations per task/arm/transport; identical frozen tasks, temperature 0, seed 42, cap 384 and 180-second deadline. All arms use the same owned engine restart before shared-context follow-up requests. The primary full-arm comparison and interpretation are in the [Redis live report](redis-live-report.md).

The client clock below starts before submit and ends after terminal validation; it excludes context/grant setup, preceding eviction and later cleanup. Model time includes adapter validation/private audit I/O. Full-arm times include these costs. p95 is the largest of five observations, not a population estimate. No repair calls were made.

Source report SHA256: `b62aa321d77d26f9325096414ca9fa159bbd31bb041e5e7809d5e6a7ff2e599d`. Frozen task SHA256: `d50f158303958f76184cfb969a115bd2e5b11ef515d89b9df286fdfeb9d582fe`.

| Arm / transport | Task | n | Checker | Client p50 / p95 ms | Model p50 ms | Input / evaluated / reused tokens | Output tokens |
|---|---|---:|---:|---:|---:|---:|---:|
| off / mcp | control_a | 5 | 5/5 | 325.77 / 383.53 | 316.22 | 465 / 465 / 0 | 30 |
| off / mcp | control_b | 5 | 5/5 | 332.23 / 380.97 | 322.01 | 465 / 465 / 0 | 30 |
| off / mcp | extract34 | 5 | 5/5 | 1934.99 / 2479.99 | 1923.65 | 3055 / 3055 / 0 | 120 |
| off / mcp | extract56 | 5 | 5/5 | 2407.70 / 2737.78 | 1726.01 | 3055 / 3055 / 0 | 125 |
| off / mcp | implementation | 5 | 0/5 | 1884.29 / 2142.25 | 1874.49 | 3105 / 3105 / 0 | 75 |
| off / mcp | long1 | 5 | 5/5 | 5994.27 / 6725.97 | 5983.95 | 3025 / 3025 / 0 | 920 |
| off / mcp | long2 | 5 | 5/5 | 7665.98 / 10319.27 | 6886.13 | 3035 / 3035 / 0 | 1110 |
| off / mcp | parent | 5 | 5/5 | 2193.60 / 2468.41 | 1397.14 | 3135 / 3135 / 0 | 65 |
| off / mcp | q7 | 5 | 5/5 | 1167.69 / 1196.93 | 1157.92 | 3030 / 3030 / 0 | 65 |
| off / mcp | q8 | 5 | 5/5 | 1969.84 / 2238.45 | 1178.25 | 3030 / 3030 / 0 | 65 |
| off / mcp | reviewer | 5 | 0/5 | 1918.86 / 2434.56 | 1191.82 | 3105 / 3105 / 0 | 10 |
| off / sdk | control_a | 5 | 5/5 | 318.82 / 481.48 | 311.91 | 465 / 465 / 0 | 30 |
| off / sdk | control_b | 5 | 5/5 | 327.83 / 438.13 | 320.61 | 465 / 465 / 0 | 30 |
| off / sdk | extract34 | 5 | 5/5 | 1778.13 / 1930.14 | 1771.50 | 3055 / 3055 / 0 | 120 |
| off / sdk | extract56 | 5 | 5/5 | 2466.60 / 2788.14 | 1779.78 | 3055 / 3055 / 0 | 125 |
| off / sdk | implementation | 5 | 0/5 | 2007.03 / 2401.19 | 2000.43 | 3105 / 3105 / 0 | 75 |
| off / sdk | long1 | 5 | 5/5 | 5895.45 / 6474.58 | 5887.48 | 3025 / 3025 / 0 | 920 |
| off / sdk | long2 | 5 | 5/5 | 7501.53 / 7628.51 | 6477.04 | 3035 / 3035 / 0 | 1110 |
| off / sdk | parent | 5 | 5/5 | 1999.73 / 2317.11 | 1311.30 | 3135 / 3135 / 0 | 65 |
| off / sdk | q7 | 5 | 5/5 | 1096.58 / 1120.35 | 1088.49 | 3030 / 3030 / 0 | 65 |
| off / sdk | q8 | 5 | 5/5 | 1844.96 / 2124.08 | 1148.59 | 3030 / 3030 / 0 | 65 |
| off / sdk | reviewer | 5 | 0/5 | 1853.99 / 1963.24 | 1173.20 | 3105 / 3105 / 0 | 10 |
| redis / mcp | control_a | 5 | 5/5 | 344.39 / 567.76 | 241.38 | 465 / 465 / 0 | 30 |
| redis / mcp | control_b | 5 | 5/5 | 392.05 / 578.71 | 253.26 | 465 / 465 / 0 | 30 |
| redis / mcp | extract34 | 5 | 5/5 | 2493.33 / 3747.32 | 2020.25 | 3055 / 3055 / 0 | 120 |
| redis / mcp | extract56 | 5 | 5/5 | 2346.69 / 2395.02 | 451.83 | 3055 / 150 / 2905 | 125 |
| redis / mcp | implementation | 5 | 0/5 | 2976.76 / 3698.96 | 2355.17 | 3105 / 3105 / 0 | 75 |
| redis / mcp | long1 | 5 | 5/5 | 9416.88 / 11073.74 | 8625.63 | 3025 / 3025 / 0 | 920 |
| redis / mcp | long2 | 5 | 5/5 | 9755.81 / 13051.29 | 7624.64 | 3035 / 100 / 2935 | 1110 |
| redis / mcp | parent | 5 | 5/5 | 1839.21 / 2492.95 | 306.53 | 3135 / 245 / 2890 | 65 |
| redis / mcp | q7 | 5 | 5/5 | 1555.46 / 1898.05 | 1151.20 | 3030 / 3030 / 0 | 65 |
| redis / mcp | q8 | 5 | 5/5 | 1913.49 / 3499.29 | 268.84 | 3030 / 100 / 2930 | 65 |
| redis / mcp | reviewer | 5 | 0/5 | 1882.18 / 2608.11 | 141.23 | 3105 / 215 / 2890 | 10 |
| redis / sdk | control_a | 5 | 5/5 | 312.92 / 325.05 | 233.03 | 465 / 465 / 0 | 30 |
| redis / sdk | control_b | 5 | 5/5 | 318.44 / 327.60 | 235.24 | 465 / 465 / 0 | 30 |
| redis / sdk | extract34 | 5 | 5/5 | 1860.92 / 2005.82 | 1416.40 | 3055 / 3055 / 0 | 120 |
| redis / sdk | extract56 | 5 | 5/5 | 1924.75 / 2053.44 | 442.84 | 3055 / 150 / 2905 | 125 |
| redis / sdk | implementation | 5 | 0/5 | 1990.95 / 2578.57 | 1580.84 | 3105 / 3105 / 0 | 75 |
| redis / sdk | long1 | 5 | 5/5 | 6106.64 / 7741.46 | 5604.21 | 3025 / 3025 / 0 | 920 |
| redis / sdk | long2 | 5 | 5/5 | 7637.28 / 8290.83 | 5681.79 | 3035 / 100 / 2935 | 1110 |
| redis / sdk | parent | 5 | 5/5 | 1810.17 / 1864.51 | 309.02 | 3135 / 245 / 2890 | 65 |
| redis / sdk | q7 | 5 | 5/5 | 1531.02 / 1689.12 | 1080.45 | 3030 / 3030 / 0 | 65 |
| redis / sdk | q8 | 5 | 5/5 | 1759.41 / 1821.85 | 261.00 | 3030 / 100 / 2930 | 65 |
| redis / sdk | reviewer | 5 | 0/5 | 1682.16 / 1798.22 | 136.58 | 3105 / 215 / 2890 | 10 |
| resident / mcp | control_a | 5 | 5/5 | 397.24 / 467.53 | 383.20 | 465 / 465 / 0 | 30 |
| resident / mcp | control_b | 5 | 5/5 | 381.17 / 473.33 | 369.09 | 465 / 465 / 0 | 30 |
| resident / mcp | extract34 | 5 | 5/5 | 2361.99 / 2584.63 | 2350.30 | 3055 / 3055 / 0 | 120 |
| resident / mcp | extract56 | 5 | 5/5 | 2767.40 / 3131.63 | 2083.42 | 3055 / 3055 / 0 | 125 |
| resident / mcp | implementation | 5 | 0/5 | 1975.49 / 2791.49 | 1964.30 | 3105 / 3105 / 0 | 75 |
| resident / mcp | long1 | 5 | 5/5 | 7179.12 / 8000.78 | 7168.93 | 3025 / 3025 / 0 | 920 |
| resident / mcp | long2 | 5 | 5/5 | 8492.77 / 9295.68 | 7466.04 | 3035 / 3035 / 0 | 1110 |
| resident / mcp | parent | 5 | 5/5 | 2357.55 / 2913.34 | 1673.32 | 3135 / 3135 / 0 | 65 |
| resident / mcp | q7 | 5 | 5/5 | 1118.45 / 1397.56 | 1103.64 | 3030 / 3030 / 0 | 65 |
| resident / mcp | q8 | 5 | 5/5 | 2033.24 / 3002.57 | 1323.78 | 3030 / 3030 / 0 | 65 |
| resident / mcp | reviewer | 5 | 0/5 | 1920.25 / 2451.30 | 1197.19 | 3105 / 3105 / 0 | 10 |
| resident / sdk | control_a | 5 | 5/5 | 322.97 / 336.34 | 315.83 | 465 / 465 / 0 | 30 |
| resident / sdk | control_b | 5 | 5/5 | 333.24 / 344.65 | 326.10 | 465 / 465 / 0 | 30 |
| resident / sdk | extract34 | 5 | 5/5 | 1700.83 / 2031.69 | 1692.66 | 3055 / 3055 / 0 | 120 |
| resident / sdk | extract56 | 5 | 5/5 | 2341.83 / 2988.31 | 1640.46 | 3055 / 3055 / 0 | 125 |
| resident / sdk | implementation | 5 | 0/5 | 1843.23 / 2138.38 | 1835.71 | 3105 / 3105 / 0 | 75 |
| resident / sdk | long1 | 5 | 5/5 | 5801.73 / 5879.16 | 5795.20 | 3025 / 3025 / 0 | 920 |
| resident / sdk | long2 | 5 | 5/5 | 7573.49 / 7829.80 | 6684.44 | 3035 / 3035 / 0 | 1110 |
| resident / sdk | parent | 5 | 5/5 | 2048.94 / 2148.03 | 1370.97 | 3135 / 3135 / 0 | 65 |
| resident / sdk | q7 | 5 | 5/5 | 1114.41 / 1154.29 | 1107.51 | 3030 / 3030 / 0 | 65 |
| resident / sdk | q8 | 5 | 5/5 | 1881.05 / 1970.44 | 1148.49 | 3030 / 3030 / 0 | 65 |
| resident / sdk | reviewer | 5 | 0/5 | 1854.30 / 1990.86 | 1171.67 | 3105 / 3105 / 0 | 10 |

All 330 native completions succeeded operationally; 270 passed the strict literal JSON checker. Failures are retained and explained in the live report. Rendered input/token hashes matched for each task across all 30 arms. Output-hash cardinalities and private native audit reconciliation are retained in full-summary.json.
