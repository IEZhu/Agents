# Where each run was built

`ctx/` (the exact prompts the answering agents saw) was not committed. To rebuild a
run's contexts, check out the commit it was built from, which is an ancestor of
this branch, and run `python evals/ablation/build_contexts.py <run dir>` there.

| Run | Built from | Notes |
|---|---|---|
| batch-01 .. batch-13 | `3df566b` (eval(ablation): runbook fixes from the cloud smoke run) | the 129-component sweep, 2 cases per component |
| retest-a .. retest-c | `45ce1c6` (eval(ablation): configurable case count and fact-checking judges for re-tests) | 14 flagged components, 6 cases each; skill-react-pattern and skill-jurisdiction-ru at the first version of their #85 fixes |
| cloud-pilot-2026-09-25 | the pilot's own notes | smoke run of the cloud routine; no plan or verdict files |

The source branches (`claude/ablation-batch-NN`, `claude/ablation-retest-x`,
`eval/cloud-pilot-2026-09-25`) were deleted after their run directories were copied
here unchanged.
