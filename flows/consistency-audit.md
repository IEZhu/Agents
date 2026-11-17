---
persona:
  agent: tech_writer
  skills: [skill-content-structure, skill-reasoning-logic, skill-fact-verification, skill-confidence-markers, skill-git-conventions]
  implants: [implant-logic-of-thought, implant-steel-man, implant-chain-of-verification]
---
# Audit instructions and documentation for contradictions, gaps and broken references

This flow finds defects in what a repository *says*, not in what its code does:
statements that cannot all be followed, instructions an executor cannot finish
without guessing, wording two readers would act on differently, references that
lead nowhere, and names that drift. Code defects and claims that disagree with
the implementation are out of scope; see
[what this flow does not check](#what-this-flow-does-not-check).

The result is a verified findings report for the current branch: each finding
with exact quotes, a class, a severity, a confidence and a proposed resolution,
plus a coverage record of what was checked. The default mode is read-only; `fix`
mode also applies the resolutions an authority settles, on a separate branch.

## Defect classes

| ID | Class | A defect when | Not a defect when |
|---|---|---|---|
| `C` | Contradiction | Two statements about one subject cannot both be followed: an obligation and a prohibition, two values for one number, default, limit or name, two orders of the same steps, two owners of one action, two statuses of one feature | They apply to different scopes, conditions, audiences or times, or a precedence statement that is in context wherever either text is followed settles them |
| `G` | Gap | An executor following the text must stop or guess: a missing input or default, a step without an actor or a checkable outcome, no branch for a failing or unavailable check, no done criterion, a term used but never defined | A document the text links to for that piece supplies it |
| `A` | Ambiguity | Two reasonable executors would take different actions from the same text: a vague trigger ("when appropriate"), an unclear referent, an unstated threshold | Only the wording differs; the action is the same either way |
| `R` | Broken reference | A link, anchor, path, step number, tool, command, flag, environment variable or configuration key that does not exist; an empty link destination or link text; a hollow reference whose target exists but does not cover what the sentence promises | It is a placeholder, an example for another repository, a file a plan marks as planned, a path at a revision a historical record states, or a path relative to a component root the text names |
| `T` | Terminology drift | One concept carries several names, or one name several concepts, so a reader cannot tell whether two passages are about the same thing | A glossary or the text itself defines the alias |

Identical copies of one rule in two places are not a finding; copies that have
diverged are a `C`. A difference between a current instruction and a historical
record (a dated plan, audit, report or changelog entry) is not a finding unless a
current document presents that record as current guidance.

Severity: `blocker` when following the text leads to a wrong or harmful action or
the executor cannot proceed; `major` when instructions an executor needs together
disagree, or a required input, step or branch is missing; `minor` when navigation,
naming or wording slows a reader without changing the outcome. Confidence is
`high`, `medium` or `low`, set from the evidence, not from the class.

## How to invoke

```text
Run flows/consistency-audit.md.
Run flows/consistency-audit.md for flows/ and rules/ only.
Run flows/consistency-audit.md for changes since origin/dev. fix
```

Through Agents-Core MCP, call `run_flow(flow="consistency-audit", request=...)`
with `current_persona` and apply its `persona_activation` (`tech_writer`) as a
switch first. The returned `repo_path` is the target; this file's checkout only
supplies the instructions and their links.

## Inputs and defaults

- **Target:** the current checkout on its current branch, with the working tree
  as it is; uncommitted changes are included and recorded.
- **Scope:** by default every document the repository maintains for people or
  models, committed or not ([step 2](#2-inventory-and-classify-the-corpus)):
  guides and READMEs, AI instructions (`AGENTS.md`,
  `CLAUDE.md`, rules, personas, skills, flows, instruction templates),
  contributor and test guides. A list of paths narrows it. `changes since <ref>`
  audits the files changed since the merge base with `<ref>` plus their
  neighbours ([step 1](#1-record-the-state-and-the-scope)).
- **Mode:** `report` (default; nothing is written in the repository) or `fix`
  ([step 7](#7-report-and-apply-in-fix-mode)).
- **External URLs:** listed as unverified unless the request asks to fetch them.
- **Output:** the report in the invocation language, with quotes verbatim. Write
  it to a file only when asked.

## 1. Record the state and the scope

1. Read the target's `AGENTS.md`, `CLAUDE.md` and documentation index when
   present (for Agents-Core, the [documentation map](../docs/README.md)). They
   are instructions for this run and part of the audited corpus at once.
2. Record `git rev-parse --abbrev-ref HEAD`, `git rev-parse HEAD` and
   `git status --short`. Every finding refers to this state. Reading a live
   installation checkout is fine; `fix` mode never writes there.
3. For `changes since <ref>`, take the files changed since the merge base,
   committed or not, with `git diff --name-only $(git merge-base <ref> HEAD)`,
   plus the untracked ones with `git ls-files --others --exclude-standard`, which
   that diff omits. Then add their neighbours: the documents that link to
   them, the documents they link to, and the documents that state rules about the
   same subjects (search the corpus for the subjects the changed passages
   govern). One side of a contradiction is usually outside the diff.
4. State the selected scope and mode, then continue.

## 2. Inventory and classify the corpus

List the documents with `git ls-files --cached --others --exclude-standard`, so
that documents not yet committed are included: `*.md`, `*.mdc`, `*.txt`,
instruction templates, and the YAML or JSON files whose text fields people or
models read (descriptions and instructions, such as those of plugin manifests),
each named in the inventory. Classify each; the class decides how its findings
count.

| Class | Examples | Treatment |
|---|---|---|
| Normative | `AGENTS.md`, repository notes in `CLAUDE.md`, rules, flows, personas, skills, contributor and test guides | Fully audited; usually the side of a contradiction an executor follows |
| Reference | User guides, READMEs, tool and API references | Fully audited |
| Generated | Text rendered from a source; for Agents-Core, the managed routing sections rendered from `src/engine/protocol/` and the repository memory block written by `describe_repo` | Audited, but each finding is filed against its source and fixed there |
| Historical | Dated plans, audits, reports, changelogs | Checked for `R` only, and for being presented elsewhere as current |
| Vendored or fixture | Third-party text, test inputs and expected outputs | Out of scope |

Record the count per class for the coverage record.

## 3. Mechanical pass

Run the cheap deterministic checks first. They seed the `R` class and leave the
reading pass for meaning. Every search covers the inventory of step 2, untracked
documents included, which is why the `git grep` commands below carry
`--untracked`. The commands are written for the default scope. When the scope
is a list of paths or `changes since <ref>`, give them the scope's documents
instead of the repository-wide patterns: as pathspecs after `--` for
`git grep`, and as file arguments for the link checker. Existence is still
checked against the whole tree (the `known` list of item 3 stays
repository-wide), so a document in scope that points outside the scope is
still checked, and a finding that lies only outside the scope is not reported.

1. **Link checker.** Run the target's own checker. For Agents-Core,
   `scripts/check_docs.py` checks Markdown links, images and local anchors, but by
   default only in tracked `.md` files, so pass the `.mdc` files and any untracked
   `.md` file explicitly. In a worktree without `.venv`, use any interpreter that
   has `markdown-it-py`.

   ```bash
   .venv/bin/python scripts/check_docs.py
   .venv/bin/python scripts/check_docs.py --skip-tool-index \
     $(git ls-files --cached --others --exclude-standard '*.mdc') \
     $(git ls-files --others --exclude-standard '*.md')
   ```

   It does not fetch external URLs, and it accepts an empty destination
   (`[text]()`, `[text](#)`) and an empty link text (`[](path)`) as valid. It
   parses Markdown only. The other inventoried formats (`.txt` files such as
   `llms.txt`, and the YAML or JSON files of step 2) get the plain-text checks
   of items 2 and 3, and a link relative to such a file is checked while reading
   it. A format left unchecked is listed in the coverage record with the reason.
2. **Empty links.** Search for what the checker accepts:

   ```bash
   git grep --untracked -nE '\]\(#?\)' -- '*.md' '*.mdc' '*.txt' '*.yml' '*.yaml' '*.json'
   git grep --untracked -nE '(^|[^!])\[\]\(' -- '*.md' '*.mdc' '*.txt' '*.yml' '*.yaml' '*.json'
   ```

   A match inside a code span or a fenced block is an example, not a link; in
   Agents-Core this flow's own file matches both searches for that reason.

3. **Paths.** List the repository paths with a directory part that the
   documents mention, in prose and in code spans alike, and that do not exist,
   each once:

   ```bash
   t=$(mktemp -d)
   git ls-files --cached --others --exclude-standard \
     | awk -F/ '{p=$1; print p; for (i=2; i<=NF; i++) {p=p"/"$i; print p}}' \
     | sort -u > "$t/known"
   git grep --untracked -hoP '(?<![\w./<*-])(?:[\w.-]+/)+[\w.-]*\w(?![\w*<>/{\[-]|[^\x00-\x7f])' \
     -- '*.md' '*.mdc' '*.txt' '*.yml' '*.yaml' '*.json' | sort -u > "$t/mentioned"
   awk -F/ 'NR==FNR {top[$1]=1; next} top[$1]' "$t/known" "$t/mentioned" \
     | comm -23 - "$t/known" \
     | git check-ignore --no-index -v -n --stdin | awk '$1=="::" {print $2}'
   ```

   It keeps the paths whose first segment exists at the repository root, drops
   those that exist as a file or directory (untracked ones included) and those
   that are ignored local state, and skips globs, placeholders and brace
   patterns. Two kinds of mention are therefore not listed and are left to the
   reading pass, which the coverage record states: a path under a top-level
   directory that no longer exists, and a bare file name without a directory,
   such as `README.md`, whose matches against the tree's file names are mostly
   runtime files, examples for other repositories and product names. A bare name
   written as a Markdown link is still checked by item 1. It needs a Git built
   with PCRE for `git grep -P`. Locate each path it prints with
   `git grep -nF --untracked -- '<path>'`, and judge it against the `R` row of
   the class table: many are examples, planned files or quoted command output. For a
   path that did exist, find the commit that removed it with
   `git log --diff-filter=D --format=%h -1 -- <path>`, then
   `git show -M --name-status --format= <commit>`: a path limited `git log`
   shows a rename only as a deletion.
4. **Names.** For tools, commands, flags, environment variables and configuration
   keys the documents mention, confirm that a definition exists in source
   (`git grep` the name in the defining files). For Agents-Core, the checker
   covers only the tool table in [docs/mcp-tools.md](../docs/mcp-tools.md);
   mentions elsewhere are not checked mechanically.
5. **Internal cross-references.** "Step N", "section X" and "see above" point at
   something that exists and still says what the reference claims; numbered lists
   have no duplicate or skipped numbers that a cross-reference depends on.
6. **Placeholders and empty sections.** `TODO`, `TBD`, `FIXME`, "coming soon", and
   a heading followed directly by another heading of the same or a higher level.
7. **Counts and catalogs.** Recount every stated total (of flows, tools,
   personas, files, tests) against the tree. Compare every index or catalog with
   what it lists, both ways: an item without an entry, an entry without an item.

Before reporting that a search found nothing, run the same pattern once on a
sample that must match, so that zero means "none" and not "the pattern cannot
match". `git grep` sees only the repository's own files, so put the sample in a
scratch directory outside the repository and search it from there with
`git grep --no-index -nE '<pattern>' -- <sample>`, which keeps the pattern
engine the same. Record each command with its result.

## 4. Reading pass

Contradictions, gaps and ambiguity are found here; no script finds them.

1. **Build a subject index.** From every normative and reference document in
   scope, extract the statements that oblige, forbid, set a default or a limit,
   order steps, assign an owner or declare a status: typically sentences with
   *must, never, always, only, default, at most, before, after, requires*, and
   numbers. Record each as `subject | statement | path:line`, where the subject is
   the action or object it governs ("branch for new work", "result size limit",
   "who merges a pull request"). Give one subject one key; the name variants met
   while doing so feed the `T` class.
2. **Compare within each subject.** Contradictions are found by comparing the
   entries under one subject across files, not by reading files one after
   another. Check obligation against prohibition, value against value, order
   against order, owner against owner, status against status, and overlapping
   conditions without a stated precedence.
3. **Read each executable instruction as its executor.** For every flow, how-to,
   step list and rule that directs an action, assume only what the file and its
   links provide, and ask: are the inputs and their defaults stated; does each
   step have an actor, an action and a checkable outcome; what happens when a
   check fails or a tool is unavailable; is there an observable done criterion;
   is each term defined before use; does each link deliver what its sentence
   promises. A "no" is a `G`, or an `R` for a hollow link. Report an `A` only when
   you can name the two different actions two reasonable executors would take.
4. **Check terminology.** List the domain terms (from a glossary when one exists,
   otherwise from the subject index) and look for one concept under several names
   and one name used for several concepts.

For a large corpus, split this pass by area across prepared workers under the
[delegation contract](README.md#delegated-workers): each worker returns its
subject-index entries and its local findings, and one integrator runs item 2
across all areas, because a contradiction usually spans two of them. Validate
every worker result before using it.

## 5. Verify every candidate

A candidate becomes a finding only after this step, on the recorded state and
never from memory of an earlier read.

1. Re-open each location and quote the exact text, one or two lines per side.
2. Look for a reading under which both statements hold: a different scope,
   condition, audience or time, an explicit exception, a precedence statement in
   context wherever either text is followed. If such a reading needs no
   guessing, drop the candidate; if it does, reclassify it as `A`.
3. For an `R`, re-run the check that found it; for a `G`, confirm that no linked
   document supplies the missing piece.
4. Set the severity and confidence. Note which side changed last
   (`git log -1 --format='%h %cs' -- <path>`, or `git blame -L` for a line); it
   informs the resolution but does not decide it.

## 6. Propose a resolution

For each finding, name the side that should win and why, using this order of
authority:

1. executable truth: code, tests, schemas, and the sources that other text is
   generated from (for Agents-Core, `src/engine/protocol/` over the surfaces it
   renders);
2. normative instructions, ordered by an explicit precedence statement when one
   exists (it decides the resolution even where it is not in context for the
   losing text, which is what made that text a finding);
3. reference documentation;
4. historical records.

Never resolve by majority: three copies of a wrong value do not outvote one
source of truth. When the order does not decide, for example two normative
documents of equal standing disagree and nothing states precedence, mark the
finding **owner decision** and give both options with the consequence of each.
For a `T`, propose the canonical term (the glossary's, or the most authoritative
document's) and list the places to change.

When a candidate turns out to be documentation disagreeing with code, it belongs
to [documentation refresh](documentation-refresh.md): record it as a handoff,
not as a finding.

## 7. Report, and apply in `fix` mode

The report contains, in this order:

1. **Header:** branch, `HEAD`, uncommitted changes, scope, mode and date.
2. **Summary:** counts by class and severity, and the number of owner decisions.
3. **Findings,** most severe first. Each has an ID (`C1`, `G3`, ...), class,
   severity, confidence, the locations as `path:line` with their quotes, one
   sentence on why it is a defect, the proposed resolution, and whether it needs
   an owner decision.
4. **Owner decisions and deferred findings:** the findings the order of
   authority could not settle, each with its options, and in `fix` mode the
   findings deferred because their quotes are not in the fix worktree.
5. **Coverage:** the inventory counts per class; each mechanical command with its
   result; the areas read in [step 4](#4-reading-pass) and those sampled or
   skipped, with the reason; the handoffs to documentation refresh.

A class without findings states what was checked for it. Never report an
unperformed check as passed.

In `fix` mode, after the report:

1. Cut a separate worktree and branch from the recorded `HEAD`, so that it
   holds the audited text, under the target's branch naming rules and never in
   the live installation checkout. When the audit covered the integration
   branch, cut from its remote tip instead (for Agents-Core, `origin/dev` with
   `--no-track`, as its contributor rules require). A fix branch cut from
   another audited branch builds on that branch and goes back to its owner, not
   to the integration branch.
2. Apply only the findings whose resolution the order of authority settles, and
   `R` fixes with a single correct target. Fix generated text at its source and
   regenerate it (for Agents-Core, `python3 -m src.engine.protocol_text --write`).
   Leave owner decisions untouched.
3. Before applying a finding, re-read every location it quotes in the fix
   worktree. When a quote is missing or reads differently there, typically
   because it sits in uncommitted changes of the audited checkout or the remote
   tip moved since the audit, do not apply a resolution to that other state of
   the file: report the finding as deferred, with the reason.
4. Re-run the checks of [step 3](#3-mechanical-pass) and the target's
   documentation checks, read the full diff, and commit under the target's
   convention (for Agents-Core, a Conventional Commit of type `docs`). Push and
   open a pull request only when the request asks for it, then follow
   [pr-review](pr-review.md).

## Done criteria

- The branch, `HEAD`, uncommitted changes and scope are recorded.
- Every class in the table was checked over the scope, or the report says where
  and why it was not.
- Every finding carries quotes from the recorded state, a class, a severity, a
  confidence, and a resolution or an owner decision; no candidate that failed
  [step 5](#5-verify-every-candidate) is reported.
- The coverage record names each mechanical command and its result.
- In `fix` mode, only authority-settled findings whose quotes read the same in
  the fix worktree were changed, on a separate branch, with the checks re-run;
  owner decisions and deferred findings stay open in the report.

## What this flow does not check

Code correctness (use a code review); claims about behavior that need the
implementation to confirm them (use [documentation refresh](documentation-refresh.md));
prose style; and the documentation language policy, which
[documentation refresh](documentation-refresh.md#documentation-language) owns.
