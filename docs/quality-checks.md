# Quality checks and badges

The README header reports concrete test, documentation and dependency signals.
This table is the one description of every README badge and its scope; the
README's [Checks](../README.md#checks) section links here. The GitHub badges
show the latest matching `push` run on the integration branch, `dev`, using
[GitHub's workflow badge parameters](https://docs.github.com/en/actions/how-tos/monitor-workflows/add-a-status-badge).
Click a status badge to inspect its run and commit, and use each pull request's
Checks tab for that change's results. A new workflow has no verified status until
it is published and executes; a local test result does not set the remote badge.

| Badge | Evidence | Scope and limits |
|---|---|---|
| Shared runtime | [Workflow](../.github/workflows/shared-runtime.yml) | Linux, macOS and Windows checks for persistent artifacts, scoped workers, cooperation, optional inference, MCP integration, dense adapter contracts and fake startup readiness; POSIX jobs also check fake native/Redis boundaries, which do not run on Windows. Path filters mean this is the latest relevant run, not a fresh full-suite check of every commit |
| Instruction signing | [Workflow](../.github/workflows/instruction-signing.yml) | Signing primitive/interoperability and protected-inventory tests on three platforms. Path-filtered, and does not claim runtime enforcement or a general security audit |
| Documentation | [Workflow](../.github/workflows/documentation-quality.yml) | Every pull request and pushes to `main`/`dev`: local Markdown navigation, MCP tool-index coverage, numeric badge freshness, parser regressions, runtime registration comparison, flow contracts, runtime version regressions and workflow permissions. Uses read-only repository permissions and requires no secrets or live models |
| MCP API docs | [Tool index](mcp-tools.md), [checker](../scripts/check_docs.py), [generated SVG](badges/mcp-api-docs.svg) | Registered main-server tools with a row in the maintained reference index divided by registered tools. Derived from this source revision and checked in CI; measures reference discoverability, not semantic completeness, parameter coverage or test coverage |
| User sync | [Workflow](../.github/workflows/user-sync.yml) | Library history, synchronization and executable discovery on Linux, macOS and Windows, using local bare repositories and fake GitHub APIs. Path-filtered like Shared runtime, so its last run can predate a documentation-only commit |
| Vulnerability scan | [Workflow](../.github/workflows/vulnerability-scan.yml), [gate tests](../tests/test_vulnerability_gate.py) | The complete committed `uv.lock` inventory, including declared extras, plus independently resolved universal `requirements.txt` dependencies. The editable root project itself, whose version is read from `VERSION`, is not a dependency and is not audited. Every severity, unfixed advisory, missing package and scanner/resolver error fails the gate; findings are printed in the job log. Runs on pull requests, merge groups, `main`/`dev` pushes, daily on the default branch and on manual dispatch; [releases](releases.md#release-pipeline) reuse it for their exact merge commit. A pass describes known dependency advisories within that inventory, not an application security audit |

Badges report workflow status; merge enforcement requires the corresponding
required checks in repository branch protection or rulesets.

These badges do not measure line/branch test coverage, maintainability scores,
model quality or the proportion of all repository prose that is correct. See
the [test guide](../tests/README.md) for test selection and the
[documentation audit](documentation-audit-2026-10-18.md) for bounded source review.
The API-reference denominator excludes dynamic aliases supplied by the
[HTTP/stdio MCP proxy](mcp-proxy.md), optional servers and client-side hooks.
Adding their guides does not increase the native-tool coverage count or establish
live-client or release acceptance.

## Update the documentation metric

When adding or removing a public MCP tool, update its row in the tool index and
regenerate the source-derived badge from a development worktree:

```bash
.venv/bin/python scripts/check_docs.py --update-badge
.venv/bin/python scripts/check_docs.py
```

Review and commit the SVG with the corresponding source/reference change. The
default checker is read-only and fails on a missing or stale badge, a missing tool
reference or an obsolete tool row. A failing check can still write an accurate
partial-coverage badge when `--update-badge` is explicitly requested; it does not
turn that partial coverage into a passing result. CI never edits or pushes files.
