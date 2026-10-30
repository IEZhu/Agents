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

```bash
<agents-core>/.venv/bin/python <agents-core>/scripts/install_review_config.py \
  --repo <target> --dry-run
```

Read the reported stack list. When detection is wrong, for example two
incidental `.c` files selecting `c-embedded`, rerun with `--stacks`. Then
install for real, adding the options step 1 established, and commit the two
files with a `chore(review):` subject.

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
  justified them;
- whether a review arrived on the pull request, quoting its first finding or
  stating that none was reported;
- the pull request link, left unmerged for the owner;
- anything you could not establish, named as unverified.

## Completion criteria

- The target carries a `.coderabbit.yaml` with the managed header and the
  review-discipline section between its markers in the instruction file, or the
  flow reports why one of them was deliberately skipped.
- The base branch list covers the bases the repository's pull requests use.
- An automated review arrived on the pull request, or the report states the
  exact reason it did not.
- Nothing in the target changed beyond those two files and the backups the
  installer writes beside a file it replaced, named `<file>.backup.<digits>`.
  Leave those backups out of the commit, and tell the caller they are there.
