# Calendar releases

Feature PRs target `dev`. Release PRs promote `dev` into `main`.
The [Release version workflow](../.github/workflows/release-version.yml) records
each merged, same-repository `dev → main` PR with a Git tag at that PR's exact
resulting merge commit, including squash and rebase merges. An ordinary close,
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
An existing tag pointing directly to that commit is a successful no-op.
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
conflicts and API errors with a fake API. `tests/test_workflow_permissions.py`
checks workflow permission boundaries. No test writes to GitHub.

GitHub references: [merge events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#running-your-pull_request-workflow-when-a-pull-request-merges),
[Git refs API](https://docs.github.com/en/rest/git/refs).
