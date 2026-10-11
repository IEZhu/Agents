# Subagent contract implementation plan

Status: implementation plan, 2026-10-11.
Tracking: [Issue #291](https://github.com/IEZhu/Agents/issues/291).
Specification: [Task-scoped subagent contract](subagent-contract.md).
Branch: `codex/subagent-personas`, based on `e67bf32` (`origin/main`).

## 1. Freeze the contract and track the work

Save the English specification and this plan, create one GitHub issue containing
the scope and acceptance criteria, and link its number here. Reuse the dedicated
clean worktree; preserve unrelated main-checkout documentation work.

## 2. Implement schemas, preparation and validation

A developer working as `software_engineer` owns the new internal schemas,
task-scoped builder, validation helper, public MCP tool integration and their
tests. Keep the session persona protocol unchanged. Use source reads and existing
retrieval policies; do not add an LLM summarization service or a runtime scheduler.

Implement in this order:

1. Strict task contract and four result payloads; canonical fingerprints.
2. Compact profile/rule loading, component eligibility and request-specific ranking.
3. Whole-component selection, character-budget accounting and explicit errors.
4. Two public tools with structured output and compatible advertised inputs.
5. Positive and negative tests through the registered MCP tools.

The developer may adapt internal factoring but must preserve the specified
external behavior. Report necessary contract changes before changing the spec.

## 3. Author compact profiles and worker rules in parallel

A prompt specialist owns `agents/*/subagent.mdc`, `rules/subagents/*`, and their
authoring documentation. Write every persona profile individually from its full
source, retaining professional judgment and necessary boundaries. Keep each body
under 250 words and select eligible role-declared component candidates. Avoid
full dialogue formats and copied broad workflows. Do not modify full persona
prompts, universal rule behavior or implementation files owned by the developer.

## 4. Integrate instruction distribution and documentation

Update the routing template first, then regenerate the tracked CLAUDE.md section.
Synchronize the memory reminder (archive its previous bytes before editing),
MCP initialization and APPLY_INSTRUCTION. Update installer migration test fixtures
for the newly archived reminder. Document prepare/spawn/validate/assess with
usable request/result examples, errors and validation limits. Add the maintained
reference and this plan to `docs/README.md` in the appropriate categories.

## 5. Validate and independently review

Run only one test or embedding process at a time. Reuse the existing Python
environment with imports resolving to this worktree; never link live derived
stores or copy secrets into it. Start with new unit/MCP tests and affected
persona, schema, instruction and migration tests. Run the regular suite once
when the focused checks pass and resources permit; report actual results and any
environmental blocker instead of substituting an old count.

Compare actual compact-package sizes with ordinary persona packages for a small
representative set using deterministic fixtures or clearly documented local
inputs. Treat these as context-size measurements, not a task-quality benchmark.
Have an independent `code_reviewer` examine the full diff and specification,
including strict JSON handling, task/attempt isolation, component toggle behavior,
all profile coverage, generated instructions and error paths. Fix verified issues
and rerun the relevant checks.

## 6. Deliver

Keep all work on the dedicated branch, commit coherent changes, and report the
issue, specification, implementation, validation and remaining limitations.
PR submission, if performed, follows `flows/pr-review.md` in no-merge mode;
never merge or reinstall/restart clients as part of this implementation request.
Record deferred model evaluations explicitly; do not present them as completed.
