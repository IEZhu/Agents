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
| `--agents-md PATH`, `--create-agents-md`, `--no-agents-md` | Choose, create or skip the instruction file. A relative path stays inside the target: one whose parent resolves outside it is refused, and an absolute path writes outside deliberately and is reported as not covered by the target's commit. |
| `--force` | Replace a `.coderabbit.yaml` this installer did not write. Read it first. |
| `--dry-run`, `--print` | Report the actions without writing, or print the composed configuration. |

The installer makes no network call and reads nothing outside the templates and
the target repository. It backs up a file it replaces, through the same helper
the instruction installers use. It refuses a destination it cannot write
safely: a symlink, either file's own or a directory above the instruction file;
a node that is not a regular file, such as a FIFO, which would never finish
being read; and a file carrying a second hard link, whose backup would hold the
content that other name shares. Both destinations are checked before either is
written, so a refusal on one leaves the other untouched; if a write still fails
on a directory it cannot test beforehand, the error names what the run had
already installed.

The managed header marks the installer's own file; it does not prove
authorship. A hand-written `.coderabbit.yaml` that opens with both of those
lines is refreshed without `--force`. The `git ls-files` call is bounded,
because a target's own `.git/config` can include a path git blocks on, and a
listing that does not return falls back to the walk.

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
`php` installation also names `tools/phases/**/*.php` and a `csharp` one names
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
- Stack detection counts file suffixes: two matching files select a stack, and
  one is enough for `bsl-1c` and `sql`, which no linter covers. A repository
  with two incidental `.c` files selects `c-embedded`, and a single stray
  `.sql` file selects `sql`; pass `--stacks` when detection is wrong.
- Detection and the guideline patterns read one file list: the target's tracked
  files in a Git repository, plus the instruction file this run selected. An
  untracked guideline file therefore keeps no pattern until it is added, which
  matches what the reviewer sees on the branch. Outside a Git repository the
  list is the walked tree, minus dependency and build directories.
- A tracked symlink is not counted, on either list, because it may point at a
  file outside the target. A path the index marks skip-worktree, as a sparse
  checkout does, is not counted either: the filesystem decides what the target
  holds, because the index is exactly what a hostile `.git` redirects. The run
  prints a warning naming those paths, so a sparse checkout narrows the
  configuration loudly instead of silently; install from a full checkout, or
  name the stacks with `--stacks`.
- A path in a merge conflict is listed once per stage. It counts once, so three
  records from one conflicted file cannot reach a two-file stack threshold.
- The destination checks run before the write and not through an open
  descriptor, so a process writing inside the target during the run can still
  put another node at a destination that has already passed. Install into a
  target nothing else is writing to.
- The configuration grows with the stacks selected. The six-stack selection
  `universal, docs-ai-instructions, iac-ci, python, shell, sql` measured 57 KB
  and 38 instruction entries; another six-stack selection reaches about 77 KB
  and 50 entries, because the fragments hold 4 to 8 entries each. CodeRabbit
  applies only the entries whose glob matches the file under review, so a
  single `.py` file still receives about twelve of them, but pass `--stacks`
  to narrow the file when a repository selects stacks it barely uses.
- `inheritance: true` keeps the organization's CodeRabbit web settings. Values
  set in the web interface and not in this file still apply.
- The installer does not enable or disable linters. Every CodeRabbit
  static-analysis integration is on by default.
