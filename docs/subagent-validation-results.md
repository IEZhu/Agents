# Subagent implementation validation

Recorded 2026-10-11 for Issue #291 on `codex/subagent-personas`, based on `e67bf32`. The implementation includes two MCP tools, strict task/result contracts, all 44 compact profiles, four worker rules, and shared instruction/documentation updates. These results do not establish model quality or token-cost savings.

## Checks

- Final regular suite in the managed worktree: **4257 passed, 71 skipped, 19 deselected, 1 warning in 2943.63s (0:49:03)**.
- Focused candidate checks: **444 passed, 20 skipped, 1 warning in 67.69 seconds**. These cover worker preparation/result validation and MCP parsing, persona bundles/protocol, logging, installer instructions/profiles, Codex instructions, protocol migration and flow loading/MCP/result sizes.
- Focused checks after worktree integration: **402 passed, 20 skipped, 1 warning in 10.15 seconds**. This repeated the worker, persona, logging and instruction checks; the larger candidate selection also included flow tests.
- Agent frontmatter validation: **44/44 agents valid**.
- Independent code review found no verified correctness/regression blockers. It covered the actual FastMCP input parser, strict schemas, fingerprints, source loading, budgets, toggles and shared instruction distribution.
- All 44 roles prepared successfully for four result types with short representative assignments: **176 packages**, **11,787–13,290 context characters**, no optional components omitted for budget. Longer task material can still exceed the default 16,000-character cap and correctly fail preparation.
- `git diff --check` and patch application checks passed. The no-hard-wrap rule for GitHub issue/PR descriptions is recorded in `AGENTS.md` and `flows/pr-review.md`.

No client installation or shared daemon restart was performed. The regular suite excludes tests marked slow by the repository's default pytest configuration; those tests and model-quality evaluations are not included in this result.

## Test environment and reproduction

The runs used Python 3.13.13 from the existing environment with `LANGFUSE_TRACING_ENABLED=false`. The final regular run used an isolated `FASTEMBED_CACHE_DIR`, offline Hugging Face access, local socket access and `AGENTS_MODEL_PATH` pointing to the pinned Harrier revision directory. The source snapshot contained no local secrets or live derived stores.

```bash
LANGFUSE_TRACING_ENABLED=false HF_HUB_OFFLINE=1 FASTEMBED_CACHE_DIR=/tmp/agents-test-fastembed-cache AGENTS_MODEL_PATH=/path/to/the/pinned/model/revision python -m pytest tests/ -q -x --tb=short
```

Use the directory containing `onnx/model.onnx`, its external weights and tokenizer files, not its parent model directory. An earlier invocation used that parent path and failed model loading; the existing loader cleared the local copy on retry. The same pinned cache revision was restored before the final run, and the isolated test cache prevents that retry path from clearing the working cache.

The final regular log is `/private/tmp/subagent-worktree-final-tests.log`; the focused candidate log is `/private/tmp/subagent-final-focused-tests.log`. Earlier snapshot runs encountered missing Git metadata and a sandbox-denied loopback socket; each reproduced on the unchanged base. Those incomplete runs do not count as passing runs. The final worktree run supplies the regular-suite result above.

## Controlled context-size comparison

Counts are Python `len` Unicode characters. Worker counts include the complete
forwarded context: role, common and worker rules, selected compiled skills,
assignment and result schema. Defaults are three skills maximum and zero implants.
All component switches and universal rules were enabled for this measurement.

The ordinary comparison uses `build_persona_bundle(tier="standard")` and its
unchanged fresh rendering, with deterministic retrieval stubs instead of an
embedding model. The skill stub returns the first declared preferred/capable
non-core IDs up to the requested retrieval count; the builder adds declared core
skills. The implant stub returns preferred IDs in declaration order, under the
ordinary builder's count policy. Ordinary counts join the four delivered blocks
with two newlines and exclude its response descriptor, footer and JSON envelope.
The worker uses its real lexical selection. This controlled comparison is not
a trace of live semantic routing.

| Role | Complete worker context | Ordinary instruction blocks |
|---|---:|---:|
| `software_engineer` | 12,085 | 21,167 |
| `code_reviewer` | 11,932 | 17,686 |
| `deep_researcher` | 12,558 | 14,800 |
| `ai_senior_engineer` | 12,042 | 22,283 |
| `lawyer` | 12,624 | 18,383 |
| `universal_agent` | 12,031 | 11,762 |

The worker package is smaller for five of these six inputs. Its assignment and
schema overhead make the universal-agent package slightly larger. Compact roles
do not guarantee a smaller complete package for every role or assignment.
This comparison excludes host prompts, tool schemas outside `worker_context`,
tool results, worker output, retries and parent review. It does not establish
token savings, task quality, runtime enforcement or lower end-to-end cost.

Reproduce the same tasks, component policy and all-role coverage from the checkout
with the test environment installed; no embedding model is loaded:

```bash
python -m evals.scripts.measure_subagent_context --output /tmp/subagent-context-sizes.json
```

## Remaining evaluation

Run matched model tasks against full and compact roles before claiming a quality
or cost improvement. Include review, implementation, research, missing input,
attempt replay, unsupported scope and budget exhaustion. Measure independently
checked task success, scope adherence, first-pass schema validity, truthful check
reporting, total input/output tokens including retries and parent review, and
latency. Keep model/version, sampling settings, dataset and source revision with
the result. Those model evaluations were not run for this implementation.
