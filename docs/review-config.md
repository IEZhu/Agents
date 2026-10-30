# Review configuration for a repository

`scripts/install_review_config.py` installs two things into a target
repository: a `.coderabbit.yaml` with review guidance for the stacks that
repository actually contains, and a managed review-discipline section in its
`AGENTS.md`. Both come from
[`scripts/templates/review/`](../scripts/templates/review/README.md).

The guidance is not general style advice. It is derived from the findings that
CodeRabbit, Copilot and Cursor reported in this owner's repositories: 3328
findings on 381 pull requests across 22 repositories between March and October
2026. The defect classes that recurred in every stack live in
`path-instructions/universal.yaml`; the rest are per stack.

## Why both files

- `.coderabbit.yaml` reaches one reviewer. CodeRabbit reads it from the branch
  under review, so a branch cut before the file existed still gets a skipped
  review on a pull request into a base the file does not list.
- The `AGENTS.md` section reaches every AI client that reads repository
  instructions, and CodeRabbit as well: `knowledge_base.code_guidelines`
  points at those files, which is why its findings can cite coding guidelines.
  It is also where the reply format lives, and that format is what keeps a
  review cycle converging.

## Install

```bash
.venv/bin/python scripts/install_review_config.py --repo /path/to/target
```

The installer detects stacks from the target's tracked files, composes the
configuration, and injects the section between its markers, preserving every
other byte of `AGENTS.md`. Running it again writes nothing when the result is
unchanged.

| Option | Effect |
|---|---|
| `--stacks python,go` | Replace detection with an explicit list. `--list-stacks` prints the available names. |
| `--language ru-RU` | Review language as an ISO code. The default is `en-US`. |
| `--profile quiet\|chill\|assertive` | CodeRabbit review profile. The template ships `assertive`. |
| `--base-branch dev` | A literal branch that pull requests target besides the repository default. Each becomes an anchored regex. |
| `--base-branch-regex '.*'` | An unanchored pattern, for a repository whose pull requests target many bases. |
| `--agents-md PATH`, `--create-agents-md`, `--no-agents-md` | Choose, create or skip the instruction file. |
| `--force` | Replace a `.coderabbit.yaml` this installer did not write. Read it first. |
| `--dry-run`, `--print` | Report the actions without writing, or print the composed configuration. |

The installer makes no network call and reads nothing outside the templates and
the target repository. It backs up a file it replaces, through the same helper
the instruction installers use, and refuses to write through a symlink.

## The stacks

`--list-stacks` prints the installed set. `universal` always ships with them.

| Stack | Selected by | Covers |
|---|---|---|
| `universal` | always | The classes that recurred in every stack: diverging contracts, absolutes without exceptions, boundary input, fail-open handling, escaping into another format, irreversible actions, disclosure, period-scoped metrics, evidence and drift. |
| `python` | `.py` | None against NaN against falsy, late-bound names, asyncio and executor lifetimes, symlink-following writes, call preconditions, tests that cannot fail. |
| `go` | `.go` | Nullable columns from outer joins, period-scoped aggregates, template and handler contracts, concurrency and write ordering. |
| `shell` | `.sh`, `.bash` | Exit status and strict mode, quoting, interpolation into other configuration formats, teardown state. |
| `windows-scripts` | `.bat`, `.cmd`, `.ps1`, `.psm1`, `.nsi` | cmd.exe quoting and paths with spaces, PowerShell and NSIS installer semantics. |
| `rust` | `.rs` | Panics on delivery paths, per-platform guards applied unevenly, Tauri and WebView lifetimes, bundled resources. |
| `c-embedded` | `.c`, `.h` | Flipper Zero application code on FreeRTOS: thread and view-model ownership, stack budgets, callbacks on error paths. |
| `php` | `.php` | WordPress query and option APIs, phased migration scripts with apply, rollback and import modes. |
| `csharp` | `.cs` | Entity Framework Core query behavior, logging gaps, financial calculation edge cases, test fixtures. |
| `bsl-1c` | `.bsl` | BSL reference semantics, 1C form modules, embedded SQL across dialects. The one stack with no linter integration behind it. |
| `frontend` | `.ts`, `.tsx`, `.js`, `.jsx`, `.cjs`, `.mjs`, `.vue`, `.svelte`, `.html`, `.css`, `.scss` | Partial DOM updates, client state write ordering, Node test harnesses, build tooling. |
| `sql` | `.sql` | Outer-join nullability, window functions without a period filter, migration pairs, DDL against the declared minimum version. |
| `iac-ci` | `.yml`, `.yaml`, `.nix`, `.tf`, `.tfvars`, `.toml` | Workflows that silently skip, gates that pass while the work failed, scanner coverage, dependency metadata, Nix environments. |
| `docs-ai-instructions` | `.md`, `.mdc`, `.rst` | Instruction documents as executable specification, including a body that changed while its `compiled` summary did not. |

Every fragment was verified against its findings corpus: the YAML parses, each
instruction stays under the schema limit, and each glob matches at least one
path in the corpus. Those glob checks ran against a regex or `fnmatch`
translation of the pattern, not against CodeRabbit's own matcher, so treat the
coverage as strong evidence rather than proof.

Each fragment carries the globs of the repositories that produced it, so a
`php` installation also names `tools/phases/**` and a `csharp` one names
`**/CalculationService/*.cs`. A glob that matches nothing in a given
repository is inert, and `c-embedded` is the one fragment whose globs follow a
single project's tree: another C project selects the stack and matches nothing
until its own globs are added.

## Decide the base branches before installing

CodeRabbit reviews the repository default branch plus whatever
`reviews.auto_review.base_branches` lists. A repository whose pull requests go
somewhere else needs that branch named, or it gets no automatic review at all.
List the bases a repository actually uses before installing:

```bash
gh api --paginate "repos/<owner>/<repo>/pulls?state=all&per_page=100" --jq '.[].base.ref' | sort -u
```

`--paginate` matters: without it the listing stops at the newest 100 pull
requests, and a base that only older ones used never reaches
`base_branches`.

Observed on 2026-10-30, the owner's repositories with bot review history split
into three groups. Values are the branches pull requests targeted, not a
recommendation to keep them. The listing behind this table read the newest 100
pull requests of each repository, which is all of them except in Agents-Core,
the one repository here that is not an install target anyway.

| Repository | Default branch | Other bases seen | Install with |
|---|---|---|---|
| `IEZhu/Agents` | `main` | `dev`, temporary `review/*` | not a target, see [Agents-Core itself](#agents-core-itself) |
| `IEZhu/pyAviStocks_bot` | `master` | `dev`, `Dev`, feature bases | `--base-branch dev,Dev` |
| `IEZhu/PyCharts` | `master` | `main`, `test` | `--base-branch main,test` |
| `WonderMr/blincho` | `main` | `test` | `--base-branch test` |
| `IEZhu/whoer` | `master` | `v2-pnl` | `--base-branch-regex '^v2-.*$'` |
| `IEZhu/class` | `main` | `feat/s1-11-events` | `--base-branch-regex '^feat/.*$'` |
| `IEZhu/pikali` | `main` | several feature bases | `--base-branch-regex '.*'` |
| `IEZhu/AviStocks` | `master` | many long-lived bases | `--base-branch-regex '.*'` |
| `WonderMr/unleashed-firmware` | `dev` | `testing` only | `--base-branch testing` |
| `IEZhu/Nikita`, `IEZhu/BaRenta`, `IEZhu/avistocks.com-infra`, `IEZhu/AviScripts`, `IEZhu/sshgate`, `IEZhu/Agents-Core-Helm`, `IEZhu/AviMunin`, `WonderMr/Loggie`, `WonderMr/nixos-darwin`, `WonderMr/saladict`, `WonderMr/tools_ui_1c`, `WonderMr/yfinance` | as listed by the API | none | no base-branch option |

`WonderMr/unleashed-firmware` is the case that justifies the check: its
default branch is `dev` while every pull request targets `testing`, so the
configuration must list `testing` for CodeRabbit to review anything.

## Agents-Core itself

This repository is not a target of the installer. Its review procedure is
[flows/pr-review.md](../flows/pr-review.md), which goes further than the
installed section: bot review rounds, thread replies, convergence and merge
conditions. Its `.coderabbit.yaml` stays hand-written, because the comment
about `inheritance` and the `^dev$` base belongs with the release procedure.
Adopting the generated configuration here would change how every pull request
in this repository is reviewed, so it is a separate decision for the owner.

## Review language

Install with `--language ru-RU` where the repository states its own language.
`IEZhu/whoer` ("Язык проекта — русский") and `IEZhu/avistocks.com-infra` do;
the other repositories carry no such statement, so they keep `en-US`.

## Forks

`WonderMr/saladict`, `WonderMr/unleashed-firmware`, `WonderMr/yfinance` and
`WonderMr/tools_ui_1c` are forks with no `AGENTS.md`. Install the configuration
there with `--no-agents-md`, or with `--create-agents-md` when the fork's own
work should carry the discipline section. Do not add instruction files that the
upstream would have to carry.

## After installing

1. Open a pull request with the change and confirm the review arrives on it.
   The configuration only takes effect for a branch that already contains it.
2. Answer the first round of findings in the format the section defines, so the
   repository's learnings record that format.
3. Raise `reviews.pre_merge_checks.description.mode` to `error` once the
   descriptions in that repository are reliably accurate. The template ships
   `warning`, which comments without blocking.

## Limitations

- The composed file replaces the managed file as a whole. Local edits belong in
  the templates, not in the target, because the next run overwrites them.
- Stack detection counts file suffixes. A repository with two incidental `.c`
  files selects `c-embedded`; pass `--stacks` when detection is wrong.
- The configuration grows with the stacks selected. A repository that selects
  six of them produces roughly 57 KB and 38 instruction entries. CodeRabbit
  applies only the entries whose glob matches the file under review, so a
  single `.py` file still receives about twelve of them, but pass `--stacks`
  to narrow the file when a repository selects stacks it barely uses.
- `inheritance: true` keeps the organization's CodeRabbit web settings. Values
  set in the web interface and not in this file still apply.
- The installer does not enable or disable linters. Every CodeRabbit
  static-analysis integration is on by default.
