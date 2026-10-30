---
persona:
  agent: devops_engineer
  skills: [skill-git-conventions, skill-tech-writing, skill-error-recovery]
  implants: [implant-verify-assumptions, implant-plan-and-solve-plus]
---
# Roll the review configuration out to a repository

This is an executable instruction for an AI working on one target repository.
Install the shared review configuration and the review-discipline section,
verify that an automated review actually arrives, and hand back a pull request.
The reference for the installer and its options is
[docs/review-config.md](../docs/review-config.md).

Run this flow for one repository at a time. Two repositories are two runs, two
branches and two pull requests.

## Inputs

- The target repository, as an owner and name or a local path.
- The Agents-Core checkout that holds `scripts/install_review_config.py`.
- Optional: an explicit stack list, review language, profile, or base branches
  the caller already decided.

## 1. Establish the target's facts

Do not install from assumptions. Establish each of these and state the value
you found:

1. The default branch, and every branch its pull requests actually target:

   ```bash
   gh api "repos/<owner>/<repo>" --jq '.default_branch'
   gh api --paginate "repos/<owner>/<repo>/pulls?state=all&per_page=100" --jq '.[].base.ref' | sort -u
   ```

   Paginate that listing. Without `--paginate` it returns only the newest 100
   pull requests, so a base used by older ones is missing from the result and
   from the installed `base_branches`, which is the omission this step exists
   to prevent.

   A base that is not the default branch must reach
   `reviews.auto_review.base_branches`, or the repository gets no automatic
   review. A repository whose pull requests target many bases takes
   `--base-branch-regex '.*'`.
2. Whether `.coderabbit.yaml` already exists, and who wrote it. A file without
   the installer's managed header is a human decision: read it, report what it
   sets, and ask before replacing it.
3. Whether `AGENTS.md` exists. A fork without one takes `--no-agents-md` unless
   the owner wants the section there.
4. Whether the repository states its own documentation language. Install with
   `--language` for that language, otherwise leave the default.
5. Whether the repository is a fork. Never add instruction files that the
   upstream would have to carry.

## 2. Work on a branch of the target

Create a branch in the target repository and make the change there. Use a
worktree when the target is a checkout another session or a service runs from.
Never commit the installation on the target's default branch directly.

## 3. Install

Map the target's state from step 1 to the options before the first run. The
installer refuses rather than guesses, so a missing flag ends the run before it
reports anything.

| State established in step 1 | Option for both runs |
|---|---|
| The instruction file for this run exists | none |
| It does not exist and the section belongs in it | `--create-agents-md` |
| The target should carry the configuration only | `--no-agents-md` |
| `.coderabbit.yaml` exists and carries the installer's managed header | none; it is refreshed in place |
| `.coderabbit.yaml` exists without that header | read it, report what it sets, get the owner's decision, then `--force` |
| The repository states its own documentation language | `--language <code>` |
| Pull requests target a branch other than the default | `--base-branch` or `--base-branch-regex` |
| Detection picks a stack the repository barely uses | `--stacks` |

Keep the instruction file inside the target. This flow commits what it changed,
so an absolute `--agents-md` outside the checkout can never be part of the
target's pull request. When the owner wants the section in a file outside the
target, install the configuration here with `--no-agents-md`, write that file
in its own place, and report that it is not covered by this pull request.

```bash
<agents-core>/.venv/bin/python <agents-core>/scripts/install_review_config.py \
  --repo <target> --dry-run
```

Read the reported stack list. When detection is wrong, for example two
incidental `.c` files selecting `c-embedded`, rerun with `--stacks`.

When the dry run reports `already current` for every target file, the target
already carries this configuration: report that, make no commit, and finish
without steps 4 and 5. There is nothing to open a pull request for.

Otherwise install for real with the same options and commit the files the
installer reported as written, with a `chore(review):` subject. Those are
`.coderabbit.yaml` plus the instruction file this run selected, which is
`AGENTS.md` by default, the path given to `--agents-md`, or nothing at all
under `--no-agents-md`.

## 4. Verify, do not assume

CodeRabbit reads its configuration from the branch under review, so the
installation only takes effect for a branch that already contains it.

1. Open the pull request for this branch against the base the repository uses.
2. Wait for the automated review on that pull request. A review that arrives
   proves the configuration parses and the base branch is covered. No review,
   or a skipped review, means the base branch list or the account settings are
   wrong: fix them in this pull request.
3. Confirm that `.coderabbit.yaml` parses locally:

   ```bash
   <agents-core>/.venv/bin/python -c "import sys,yaml;yaml.safe_load(open(sys.argv[1]))" <target>/.coderabbit.yaml
   ```

   Use the same interpreter that ran the installer. A plain `python3` on the
   host often has no PyYAML, so the check then fails on the import instead of
   on the file.

4. Answer every finding on this pull request in the format the installed
   section defines. This is the first record of that format in the
   repository's review learnings, so it sets the pattern.

## 5. Report

Report in the invocation language:

- the stacks installed and the options used, with the facts from step 1 that
  justified them, or that the target was already current and nothing changed;
- any instruction file written outside the target, which this pull request
  does not cover;
- whether a review arrived on the pull request, quoting its first finding or
  stating that none was reported;
- the pull request link, left unmerged for the owner;
- anything you could not establish, named as unverified.

## Completion criteria

- An already-current target ends the run at step 3: the report says so, no
  commit and no pull request exist, and the criteria below do not apply.
- The target carries a `.coderabbit.yaml` with the managed header, and the
  review-discipline section sits between its markers in the instruction file
  this run selected, or the flow reports why one of them was deliberately
  skipped. A `--no-agents-md` run installs the configuration alone, and
  `--agents-md PATH` puts the section in that file instead of `AGENTS.md`.
- The base branch list covers the bases the repository's pull requests use.
- An automated review arrived on the pull request, or the report states the
  exact reason it did not.
- Nothing in the target changed beyond the selected target files and the
  backups the installer writes beside a file it replaced, named
  `<file>.backup.<digits>`. Leave those backups out of the commit, and tell
  the caller they are there.
