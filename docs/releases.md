# Calendar releases

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

## Prepare a promotion

1. Fetch `origin` and record the full `origin/dev` and `origin/main` SHAs. Inspect
   `git log --left-right --no-merges --oneline origin/main...origin/dev` for changes
   unique to either branch. Merge any unreleased `main` hotfix back into `dev`
   through the normal review process before preparing the promotion.
2. Run the [documentation refresh](../flows/documentation-refresh.md) against
   current `origin/dev` in a separate worktree. Submit corrections to `dev`, and
   include their reviewed result before freezing the release head. Keep the live
   installation on its configured update channel with a clean working tree.
3. Validate the changed areas from that worktree using the [test guide](../tests/README.md).
   Include `tests/test_release_version.py`, `tests/test_version.py` and
   `tests/test_workflow_permissions.py` for the publication and displayed-version
   contract. Record actual results, skipped checks and environment limitations;
   a passing workflow for an older head is not a current-head check.
4. Open one `dev` into `main` PR, or update the existing promotion PR. Describe
   the complete range since the last release, its validation, and the issue
   closing references required above. If documentation or another prerequisite
   is still awaiting review in `dev`, keep the promotion as a draft and link the
   dependency. Re-read both remote SHAs whenever `dev` advances and update the
   release scope and affected checks.
5. Complete the [PR review flow](../flows/pr-review.md), satisfy the active
   repository protections and obtain the owner's release decision. Merge with
   a merge commit only after those gates pass. Then verify the exact merge SHA,
   tag and published GitHub Release; check Glama separately as described below.

## Release pipeline

The [Release version workflow](../.github/workflows/release-version.yml) publishes
each merged, same-repository `dev → main` PR as a calendar Git tag and a published
GitHub Release at that PR's exact resulting merge commit. This runs after the PR
has been merged through the repository's review and check requirements; it does
not merge PRs or change protection settings. It also handles a squash or rebase merge, which the procedure
above does not use. An ordinary close,
a feature merge into `dev`, or a fork with a branch named `dev` publishes nothing.

A release keeps the version of the `dev` HEAD it promotes. The displayed
Agents-Core version is `YY.MM.DD.HHMM`, the UTC committer time of the
installation's HEAD ([src/version.py](../src/version.py)); an installation on
`dev` adds `-dev`, and tracked uncommitted changes add `-dirty`. On `main`, a
merge commit takes the committer time of the revision it merged, its second
parent: the release that merged a `dev` showing `26.10.15.1814-dev` is
`26.10.15.1814` on `main` and in its tag, not the minute of the merge. A merged
revision older than main's previous HEAD (a hotfix on `main` not yet merged back
into `dev`) and a squash or rebase merge keep the merge commit's own time, so the
version on `main` never goes back; so does a shallow clone that lacks the parents.
The tag normally has the form `v26.10.14.1200` and marks the merge commit on
`main`. If another release already owns that minute, the new release gets
`v26.10.14.1200-pr381`, with its PR number. The date portion retains the
displayed version; the suffix distinguishes releases within one minute. Python
package and Claude plugin manifest versions have their own release procedures and
are not changed by this workflow. Releases up to `v26.10.15.1905` were named by
the merge commit's own time; their tags stay as they are.

Publication uses the merged PR's SHA and the commit times of it and its parents,
so later changes to `main` and later workflow reruns do not select a different
revision or date.
An existing lightweight or annotated tag resolving to that commit is reused.
Annotated tag objects are dereferenced before their targets are compared.
Existing tags are never moved or deleted. A conflicting fallback name, missing
commit, denied access, or API failure fails the job visibly. Retry a failed run
through GitHub Actions after resolving its reported error.

The same job then creates a non-draft, non-prerelease GitHub Release for the
verified tag, with a link to the promotion PR, the exact merge SHA, the calendar
version with the commit it was taken from, and GitHub-generated release notes. GitHub's `legacy` policy selects
the Latest release rather than explicitly promoting an old retry as Latest.
An already published stable release for that tag is reused without changing its
notes, assets, or publication time. Drafts, prereleases, and inconsistent API
responses require manual inspection; the job does not publish or edit them.
The tag target is checked again before reporting success. If tag creation
succeeds but release creation fails, rerun the same Actions run: it reuses the tag
and resumes release publication. No cleanup or tag deletion is needed.

Only the publication job receives `contents: write`, using the built-in
`GITHUB_TOKEN`; no personal token or branch-protection bypass is required.
The workflow does not commit to `main` or `dev`. Repository tag rules can still
deny creation and must permit the release names. Different promotions publish
independently; concurrent attempts to create the same name verify the resulting
ref before succeeding.

The workflow change must first reach `main` through the release process. It does
not backfill historical merges or publish packages, plugin updates, or binaries. On a checkout that
has advanced beyond a release tag, the footer continues to identify that checkout's
HEAD; a tag records the released revision.

Validation: `tests/test_release_version.py` covers event guards, invalid merge
identity, UTC conversion, the version of the merged `dev` HEAD and its
never-backwards fallback, same-minute releases, reruns, concurrent creation,
annotated tag reuse and conflicts, GitHub Release publication and partial-failure
recovery, preservation of existing releases, and API errors with a fake API.
`tests/test_workflow_permissions.py` checks workflow permission boundaries. No test writes to GitHub.

## Glama Auto-Release

The intended distribution chain is:

```text
approved dev → main PR merged with a merge commit
  → Release version job creates/verifies the calendar tag
  → the same job publishes/verifies the GitHub Release
  → Glama Auto-Release builds and publishes the container after its checks
```

The listing is [Agents-Core on Glama](https://glama.ai/mcp/servers/IEZhu/Agents).
Its maintainer [Releases settings](https://glama.ai/mcp/servers/IEZhu/Agents/admin/releases)
offer Auto-Release with the description “Builds and publishes a new version on
every GitHub release.” Enable it only with a successful saved configuration on
the [Dockerfile page](https://glama.ai/mcp/servers/IEZhu/Agents/admin/dockerfile).
An explicit commit pin would keep future builds on that revision; leave it empty
for normal updates. The editor describes this mode as using the latest commit,
so verify the source SHA of every automatic build against the GitHub Release tag,
particularly if `main` advances while Glama is processing a release.

GitHub publication and Glama publication are separate outcomes. A green Actions
publication job proves that the tag and GitHub Release exist, not that Glama
has finished. Check Glama's build/startup result and public version before
announcing the container update. The listing currently uses SemVer
`26.10.14+1807` for calendar version `26.10.14.1807`; verify the next automatic
version too, including a same-minute `-prN` tag. These provider mappings and
delivery of a `GITHUB_TOKEN`-created release to Auto-Release have not yet been
confirmed by an end-to-end automatic release in this repository.

The job deliberately does not chain a second Actions workflow on `release`:
events created by `GITHUB_TOKEN` generally do not start another Actions run.
It also does not call an undocumented Glama API or store a Glama credential in
GitHub. If Glama misses an event or rejects a build, inspect its latest test and
repair the saved recipe, then use Glama's documented manual build and release
workflow for the same verified tag. A successful Actions rerun preserves an
existing release and does not generate a second publication event. Do not move
the release tag or claim a Glama version was published from Actions alone.

GitHub references: [merge events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#running-your-pull_request-workflow-when-a-pull-request-merges),
[Git refs API](https://docs.github.com/en/rest/git/refs), and
[annotated tag objects](https://docs.github.com/en/rest/git/tags#get-a-tag),
[GitHub Releases API](https://docs.github.com/en/rest/releases/releases#create-a-release),
and [events created by GITHUB_TOKEN](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow).
Glama reference: [manual release procedure](https://glama.ai/blog/2026-03-15-how-to-make-a-release).
