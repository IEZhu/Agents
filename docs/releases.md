# Calendar releases

Feature PRs target `dev`. Release PRs promote `dev` into `main`.

## Procedure

This is the one description of the release procedure; the README links it.

- Every pull request targets `dev`, the integration branch.
- A release is a pull request from `dev` into `main`. The owner reviews it and
  merges it with a merge commit, never a squash or a rebase. Installations switch
  between the branches with `channel set` ([update channel](../README.md#update-channel)):
  a switch counts as a downgrade only when HEAD has non-merge commits the target
  lacks, so the release's merge commit lets `main` → `dev` switch without a
  confirmation. A squash or rebase merge would put commits on `main` that `dev`
  lacks, and `main` → `dev` would then ask for one.
- The release PR's description lists `Closes #N` for every issue merged into `dev`
  since the last release: GitHub acts on closing keywords only when they reach the
  default branch, `main`.
- A hotfix merged into `main` is merged back into `dev`; until then `main` → `dev`
  counts as a downgrade, because `dev` lacks the fix.
- `dev` is never rebased or force-pushed: installations that follow it only
  fast-forward.

## Release tags

The [Release version workflow](../.github/workflows/release-version.yml) records
each merged, same-repository `dev → main` PR with a Git tag at that PR's exact
resulting merge commit. It also tags a squash or rebase merge, which the procedure
above does not use. An ordinary close,
a feature merge into `dev`, or a fork with a branch named `dev` publishes nothing.

On a clean `main` checkout, the displayed Agents-Core version stays
`YY.MM.DD.HHMM`, derived from the installation's HEAD committer time in UTC
by [src/version.py](../src/version.py). An installation on `dev` adds `-dev`,
and tracked uncommitted changes add `-dirty`; release tags use the plain
calendar portion and identify the committed release revision.
The tag normally has the form `v26.10.14.1200`. If another release already owns
that minute, the new release gets `v26.10.14.1200-pr381`, with its PR number.
The date portion retains the displayed version; the suffix distinguishes releases
within one minute. Python package and Claude plugin manifest versions have their
own release procedures and are not changed by this workflow.

Publication uses the merged PR's SHA and its commit time, so later changes to
`main` and later workflow reruns do not select a different revision or date.
An existing lightweight or annotated tag resolving to that commit is a successful
no-op. Annotated tag objects are dereferenced before their targets are compared.
Existing tags are never moved or deleted. A conflicting fallback name, missing
commit, denied access, or API failure fails the job visibly. Retry a failed run
through GitHub Actions after resolving its reported error.

Only the publication job receives `contents: write`, using the built-in
`GITHUB_TOKEN`; no personal token or branch-protection bypass is required.
The workflow does not commit to `main` or `dev`. Repository tag rules can still
deny creation and must permit the release names. Different promotions publish
independently; concurrent attempts to create the same name verify the resulting
ref before succeeding.

The workflow must first reach `main` through the release process before future
promotions can use it. It does not retroactively tag historical merges or publish
packages, plugin updates, binaries, or a GitHub Release entry. On a checkout that
has advanced beyond a release tag, the footer continues to identify that checkout's
HEAD; a tag records the released revision.

Validation: `tests/test_release_version.py` covers event guards, invalid merge
identity, UTC conversion, same-minute releases, reruns, concurrent creation,
annotated tag reuse and conflicts, and API errors with a fake API.
`tests/test_workflow_permissions.py` checks workflow permission boundaries. No test writes to GitHub.

GitHub references: [merge events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#running-your-pull_request-workflow-when-a-pull-request-merges),
[Git refs API](https://docs.github.com/en/rest/git/refs), and
[annotated tag objects](https://docs.github.com/en/rest/git/tags#get-a-tag).
