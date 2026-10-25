# Calendar releases

## Procedure

This is the one description of the release procedure; the README links it.

- General contribution pull requests target `dev`, the integration branch.
- Teams/Hosted contributions target the shared `team` branch, created from `dev`.
  Create feature worktrees from `origin/team` and submit reviewed PRs into `team`.
  Promote integrated Teams/Hosted work through a separate reviewed `team` into
  `dev` PR before including it in a release. Keep `team` current through reviewed
  `dev` into `team` PRs; never rebase or force-push the shared branch.
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
   Prepare the next calendar version in that contribution worktree, after
   fetching `origin/main`:

   ```bash
   python3 scripts/bump_plugin_version.py --base origin/main
   python3 scripts/bump_plugin_version.py --check --base origin/main
   ```

   The helper writes the same `YY.MM.DD.HHMM` value to the root [`VERSION`](../VERSION)
   file and the [plugin manifest](../plugins/agents-core/.claude-plugin/plugin.json).
   Python package metadata reads `VERSION` directly through `pyproject.toml`;
   `uv.lock` records the editable root package as dynamic, so later bumps need
   no lock-file rewrite or dependency resolution. Python metadata normalizes leading
   zeros in numeric components, so `26.10.18.0805` appears as `26.10.18.805` there.
   It resolves the fetched baseline to an exact commit. When `VERSION` is absent
   or equals that baseline, it chooses the current UTC minute or the baseline
   plus one minute, whichever is later. This gives distinct versions for successive
   promotions prepared in the same minute. An already newer `VERSION` is
   preserved, so retries against the same baseline do not bump again; the
   manifest is synchronized to it. A malformed `VERSION` fails, and so does one
   behind the baseline's `VERSION` file: merge that `main` back into `dev` first.
   A baseline without `VERSION` uses the historical commit-time calculation
   described below; it has no `VERSION` to merge back, so a `VERSION` behind it
   is advanced like a missing one. Submit both version files through a reviewed PR into
   `dev` before freezing the promotion head. The helper does not commit, push,
   fetch, create tags or publish anything. `--check` only validates the files.
3. Validate the changed areas from that worktree using the [test guide](../tests/README.md).
   Include `tests/test_release_version.py`, `tests/test_bump_plugin_version.py`, `tests/test_version.py`,
   `tests/test_workflow_permissions.py` and `tests/test_vulnerability_gate.py` for the
   publication, dependency-gate and displayed-version contract. Record actual
   results, skipped checks and environment limitations;
   a passing workflow for an older head is not a current-head check.
   If release documentation was prepared from neighboring feature branches,
   require their reviewed implementations in `dev` and reconcile overlapping
   pages against that integrated head first. Preserve dynamic `VERSION` package
   metadata when adding optional dependencies, refresh the lock file through its
   normal tooling, and rerun the relevant checks. Pending-feature banners and
   branch-pinned examples must be updated against integrated source before they
   become released instructions. The dated [parallel-feature checklist](documentation-audit-2026-10-18.md#parallel-feature-integration)
   records the current documentation submission's dependencies.
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

Publication waits for the [Vulnerability scan](../.github/workflows/vulnerability-scan.yml)
of the exact merge commit. The workflow's `vulnerability-scan` job runs under the
same conditions as publication and calls the scan with the PR's merge SHA, and
the `publish` job declares `needs: vulnerability-scan`. An advisory of any
severity, an unfixed advisory, a package missing from the scanned inventory or a
scanner/resolver error fails the scan, and then no tag or GitHub Release is
created. The same scan runs on the promotion PR, so a finding normally appears
before the merge; the release run repeats it with the advisory data current at
publication. Rerunning the failed jobs scans the same commit again. To publish
with fixed dependencies, merge the fix into `dev` and make a new promotion with
a higher `VERSION`, because `main` already carries the unpublished one. The
[quality guide](quality-checks.md) states the scan's inventory and limits.

Before merge, its `check` job requires a same-repository `dev → main` promotion's
`VERSION` to exceed the version at the event's pinned `main` base SHA, and the
plugin manifest's `version` to equal `VERSION`. The checkout fetches history for
that exact comparison; CI uses `--check` and writes no files. It also checks
the Python package's version source and dynamic root package in `uv.lock`. A reused version
or manifest mismatch fails visibly even when only server code changed. Ordinary
feature PRs still run the focused regressions without requiring another version
bump on every commit. If `main` advances, refresh the promotion's base/current-head
checks before merging.

The root `VERSION` is the canonical base number for the runtime, Claude plugin
manifest and release tag. Its `YY.MM.DD.HHMM` value records the UTC release
preparation minute, advanced when necessary to exceed the published baseline;
it does not change with each later commit. For example, `26.10.18.0805` appears
as `26.10.18.0805-dev` on `dev` and as `26.10.18.0805` on `main`. Tracked
uncommitted changes add `-dirty`. These runtime labels are not written to the
manifest or tag ([src/version.py](../src/version.py)). Python package metadata
reads the same base number, using its standard numeric normalization.

Publication reads `VERSION` and the plugin manifest at the PR's exact merge SHA,
requires their versions to match, and creates `vVERSION` there, for example
`v26.10.18.0805`. Later commits and workflow reruns cannot select a different
revision or version. An existing lightweight or annotated tag resolving to that
merge commit is reused; annotated tag objects are dereferenced before comparison.
If the canonical tag targets another object, publication fails: prepare a higher
`VERSION` through `dev` and a new reviewed promotion. It never adds a `-prN`
suffix to a canonical release, moves a tag, or deletes one. Missing commits,
denied access and API failures also fail visibly. Retry a failed Actions run
after resolving its reported error.

Legacy checkouts and release commits without `VERSION` retain the historical
commit-time calculation. It uses the UTC committer time of HEAD; a two-parent
merge on `main` uses the promoted second parent's time when it is not older than
the previous `main` parent. Otherwise it keeps the merge commit's time, as do
squash/rebase merges and runtime shallow clones that lack the parents. Historical
publication can reuse its `-prN` collision fallback. Existing release tags and
published releases retain their names and content; releases through
`v26.10.15.1905` used the merge commit's own time.

The same job then creates a non-draft, non-prerelease GitHub Release for the
verified tag, with a link to the promotion PR, the exact merge SHA, the calendar
version and its source (`VERSION` at that SHA, or the legacy commit time), and
GitHub-generated release notes. GitHub's `legacy` policy selects
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

An already published calendar tag is not repaired by moving it or editing its
release commit. If a previous promotion retained an old plugin version, prepare
a higher canonical version and matching manifest in `dev`, then make a new
reviewed promotion.

The workflow change must first reach `main` through the release process. It does
not backfill historical merges or publish packages, plugin updates, or binaries. On a checkout that
has advanced beyond a release tag, its footer retains the prepared base number
until `VERSION` changes; a tag records the exact released revision.

Validation: `tests/test_bump_plugin_version.py` covers preparation, monotonic
versions, idempotency and the promotion gate in temporary Git repositories.
`tests/test_version.py` covers the stored runtime version, its labels and legacy
fallback. `tests/test_release_version.py` covers canonical version/manifest
validation at the merge SHA, strict tag collisions, legacy behavior, tag reuse,
GitHub Release publication and partial-failure recovery with a fake API.
`tests/test_workflow_permissions.py` checks workflow permission boundaries.
`tests/test_vulnerability_gate.py` covers the scan's inventory gate and that
publication needs the scan of the published commit.
These checks do not publish to GitHub.

## Glama Auto-Release

The intended distribution chain is:

```text
approved dev → main PR merged with a merge commit
  → Vulnerability scan of that merge commit passes
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
version too. These provider mappings and
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
