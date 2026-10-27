# Autodev guards: protected paths and the outgoing-text scan

Status: implemented with tests, and in use: the broker (#342) runs both checks before it writes to
GitHub, and the run sequence (#347) runs the change check after the implement stage.
Tracking: [issue #341](https://github.com/IEZhu/Agents/issues/341), part of the autodev epic #336.
Design: [autodev plan](autodev-plan.md), sections 5.3 (protected paths) and 5.6 (session and
worker rights).

The autodev broker (#342) runs two checks before it writes anything to GitHub. Both are
plain functions without network access or state:

- `src/schedules/paths.py`: the path classes read at the pinned `main` commit, and the change
  check over the commits a session hands over.
- `src/schedules/scan.py`: the outgoing-text scan of diffs, PR text, comments and worker output.

```python
from src.schedules.paths import check_changes, load_path_classes
from src.schedules.scan import KnownSecret, Scanner

classes = load_path_classes(trusted_repository, pinned)
verdict = check_changes(trusted_repository, base, head, issue_labels, classes)
# verdict.allowed, or verdict.refusals: (commit, path, reason) for each refusal

scanner = Scanner([KnownSecret("broker token", token), KnownSecret("decoy: ssh key", decoy_text)])
scanner.require_clean(outgoing_text)  # SecretFound lists the hits, never their text
```

## Path lists

`.github/autodev-protected-paths` and `.github/autodev-control-paths` are read with
`git cat-file blob <pinned>:<path>`, never from the working branch. A list file that is missing,
is not a regular file (a symbolic link would read as its target), is not UTF-8 or has no
pattern raises `PathListError`; it never counts as an empty list.

The syntax is a subset of gitignore. One pattern per line; blank lines and lines starting with
`#` are skipped, and trailing whitespace is stripped. Anything the module does not implement is
refused with the file and line, so a list never protects less than it reads: a leading `!`, a
backslash, a `[` character class, `?`, leading whitespace, `**` inside a component (`a**b`), and
empty, `.` or `..` components. `?` is refused because matching runs on folded names, in which one
character can be several code points (`é` is `e` plus a combining accent, `ß` folds to `ss`):
`caf?.md` would not match `café.md`. Use `*` instead.

| Pattern | Matches |
|---|---|
| `*` | Any run of characters within one component; never `/` |
| `a/**/b`, `**/CLAUDE.md` | `**` spans any number of directories, including none, so a leading `**/` also matches at the root |
| `/foo`, `docs/cloud-runs.md` | A slash at the start or in the middle anchors the pattern at the root |
| `pyproject.toml`, `requirements*.txt` | Without a slash, the basename at any depth |
| `.github/**` | Everything below the directory and the directory path itself |
| `build/` | A trailing slash is accepted; it also matches a file of that name |

A path also matches when any of its parent directories matches. Matching is case-insensitive
after Unicode normalization (`fold`: NFD, then case folding), as macOS APFS compares names, so
`Nested/Claude.MD` is protected by `**/CLAUDE.md`.

`PathClasses.classify(path)` returns `control`, `protected` or `None`. Every control path counts
as protected. Both list files are control paths even if the lists stop naming them.

## Change check

`check_changes(repository, base, head, labels, classes)` takes the trusted repository, two
40-character lowercase hex SHAs (validated before any git call), the issue's labels and the
classes from `pinned`. It compares every commit of `base..head` with its single parent using
`git diff-tree -r --no-renames --raw -z`, so both sides of every change count: a rename is a
deletion plus an addition, and deleting a protected file is a change to it. It returns a
`ChangeCheck`: the commits, and one `Refusal` per refused commit, path and `Reason`. The push is
allowed only when there is no refusal.

| Reason | Refused |
|---|---|
| `NOT_DESCENDANT` | `head` does not descend from `base`; nothing else is checked |
| `MERGE_COMMIT`, `ROOT_COMMIT` | A commit without exactly one parent |
| `CONTROL_PATH` | A control path, under any label |
| `PROTECTED_PATH` | Another protected path, unless the labels include `autodev:next` |
| `SYMLINK`, `GITLINK` | Mode 120000 or 160000 on either side of a change |
| `UNSUPPORTED_MODE` | Any mode other than a regular or executable file, a link or a gitlink |
| `CASE_COLLISION` | A path present in the commit's tree that, or whose directory, folds like a different name in that tree; a case-only rename is not a collision |
| `DOT_GIT` | A component that folds to `.git`, also with zero-width characters |
| `INVALID_PATH` | A path that is not UTF-8 or has an empty, `.` or `..` component |

Git runs without a shell and with a minimal environment, `-C <repository>` with
`GIT_CEILING_DIRECTORIES` stopping discovery above it, and `--end-of-options` before revision
arguments. Inherited `GIT_*` variables never reach it. What it reads:

- **Not read**: the system configuration (`GIT_CONFIG_NOSYSTEM=1`), the global one
  (`GIT_CONFIG_GLOBAL` at the null device), replace objects (`--no-replace-objects`,
  `GIT_NO_REPLACE_OBJECTS=1`) and grafts (`GIT_GRAFT_FILE` at the null device). No pager, no
  terminal prompt (`GIT_TERMINAL_PROMPT=0`), literal pathspecs.
- **Read**: the trusted repository's own configuration (`config` and the files it includes),
  with `-c core.fsmonitor=false`, `-c core.hooksPath=/dev/null` (the null device),
  `-c core.commitGraph=false` and `-c core.quotePath=false` on the command line taking
  precedence, so no fsmonitor command and no hook runs, and parents come from the commit objects
  rather than a commit-graph file. Plan 5.4 keeps sessions away from the trusted repository's `.git`.
- **Refused**: a trusted repository with an `info/grafts` or `objects/info/alternates` file in its
  common git directory. The check raises `PathsError` before it reads anything else: grafts
  rewrite parents, so a commit could hide from `base..head`, and alternates add objects from
  another store.

Any git failure or unexpected output raises `GitError`; the broker treats every `PathsError` as a
refusal.

## Outgoing-text scan

The scan is a tripwire, not a protection (plan 5.6): sessions get no secrets and no network,
and the scan only notices a leak in a shape it knows. Any hit refuses the operation and blocks
the run. It looks for:

- **Token-shaped secrets** (`TOKEN_PATTERNS`): GitHub tokens (`gho_`, `ghp_`, `ghs_`, `ghu_`,
  `ghr_` and `github_pat_` followed by the token body), `sk-ant-`, `sk-or-` and `sk-proj-` keys,
  `AKIA` access key IDs, `-----BEGIN ... PRIVATE KEY-----` blocks and JWTs. A prefix alone, as in
  this paragraph, is not a hit. The patterns also run on a view with Unicode format characters
  (category Cf, such as U+200B) removed and lines joined: a line break goes together with the
  blanks around it and, in a unified diff, the next line's marker. Blanks inside a line stay, so
  prose does not run together; one exception: a prefixed token (`gh*_`, `github_pat_`, `sk-*`)
  whose body one blank (space, NBSP, tab) breaks still counts when both parts have at least 8
  characters and together the body's minimum length.
- **High-entropy strings**: runs of at least 24 characters of `[A-Za-z0-9+_-]` with digits and
  both letter cases and at least 4.5 bits of entropy per character. `/` ends a run, so each
  component of a URL or path is measured alone, and hex runs of 7 or more characters inside a run
  (commit SHAs, digests) are left out of the measure: GitHub permalinks and API URLs with SHAs are
  not hits. One context is exempt: the session id of a
  `https://claude.ai/code/session_01<22 letters and digits>` link, as the `Claude-Session:`
  trailer of commits carries it; the same id anywhere else, longer or under another host still
  counts.
- **Known secrets and decoy contents** (`KnownSecret`, label plus value of at least 8
  characters; #342 passes its token and key, #343 the decoys): raw; with whitespace (CR
  included), Cf characters, unified-diff line markers, Markdown quote markers at a line start and
  the string escapes of JSON and `repr` (`\n`, `\r`, `\t` removed; `\"`, `\'`, `\\`, `\/` as the
  character) normalized away in both the value and the text, so CRLF line endings, indentation, a
  missing final newline, a split by a line break, space, NBSP or U+200B, the `+` lines of a diff,
  a quoted reply or diff, or a JSON or `repr` string do not hide it; each line of a multi-line
  value with at least 16 characters once whitespace is removed (`KNOWN_LINE_MIN_LENGTH`) also on
  its own, so a partly copied or reordered decoy file is found; as base64 in either alphabet,
  padded or not, at any byte offset inside a larger blob, and as hex in either case.
- **Encoded forms**: every base64, base64url and hex run is decoded at each alignment, two levels
  deep, and the decoded bytes are scanned for the patterns and the known secrets. Whitespace,
  Cf characters and the line markers of a unified diff are removed first, so wrapped base64 and
  spaced hex are found. Escapes are decoded as one more layer: percent-encoding, `\xNN`,
  `\uNNNN` (also escaped twice) and octal `\NNN`, and hex pairs with `0x` prefixes or `:`, `,`,
  `-` or blank separators (`0x67, 0x68, ...`, `67:68:...`), which become one hex run.
- **Limits**: a text longer than `MAX_TEXT_LENGTH` (4,000,000 characters) is not scanned and is
  one hit of kind `limit` (`text-too-large`); adversarial text scans in about 2 seconds per
  million characters. After `MAX_HITS` (1,000) hits the scan stops and adds a `limit` hit
  (`too-many-hits`). Either way the text is not clean.

A `Hit` has only `start`, `end`, `kind` (`pattern`, `entropy`, `known` or `limit`) and `name` (the
pattern name or the caller's label); for a decoded or unescaped hit, the offsets cover the
encoded characters. No hit, `repr` or `SecretFound` message contains the matched text.

### Entropy coverage by length

Shannon entropy of a string cannot exceed log2 of its length, so 4.5 bits per character needs at
least 23 distinct characters, and a random string reaches it only some of the time. Measured on
random strings (2,000 per length):

| Length | 24 | 28 | 32 | 36 | 40 | 44 | 48 | 64 |
|---|---|---|---|---|---|---|---|---|
| Letters and digits (also with `-`, `_`) | 5% | 25% | 66% | 88% | 97% | 99% | 99.9% | 100% |
| Standard base64 (with `+` and `/`) | 4% | 21% | 44% | 59% | 69% | 74% | 77% | 90% |

Shorter than 24 characters, nothing is an entropy hit. Detection is reliable from about 40
characters of letters and digits; a random 32-character API key is missed about a third of the
time. Standard base64 is measured between its slashes, so a 40-character AWS secret access key
is caught about two times in three. `test_entropy_coverage_by_length_is_as_documented` pins the
rates at 23, 24, 32, 40 and 48 characters of letters and digits and at 40 and 64 characters of
standard base64, with seeded samples.

### Known fixture hits

The repository-wide regression check scans tracked files under `docs/`, `src/`, `flows/`,
`agents/`, `tests/` and `evals/`, excluding the fixture files listed in
`tests/test_schedules_scan.py` (`KNOWN_FIXTURE_FILES`). They contain intentional hits: fake
tokens and keys in
tests, an SSH host key, a recorded GitHub URL with a token-like query value in
`tests/fixtures/autodev_gate_recorded.json`, and an SRI integrity hash in
`evals/templates/report.html.j2`. A session diff whose context lines reach those lines is
refused and the run blocked, which autodev runs that edit `tests/` can meet. The test
`test_tracked_files_are_clean_except_known_fixtures` requires every other scanned file to be
clean; that fixture exclusion belongs to the test, not the broker's outgoing scan.

Not covered: a secret split by other text or several times (only a known secret is found across
any number of blanks; a token pattern takes one blank inside a line, or line breaks), compressed,
encrypted or in another encoding (base32, ROT13, `+` for a space in form encoding); part of a
decoy shorter than a line of 16 characters, unless the caller passes that part as its own
`KnownSecret` (a decoy whose lines are all shorter is found only whole and in order); a random string shorter than the entropy limits above. Decoys (#343) should not
contain lines of 16 or more characters that also occur in ordinary text, since each such line is
a needle.

## Tests

`tests/test_schedules_paths.py` builds throwaway repositories under `tmp_path` with git
plumbing, so trees can hold case collisions, `.git` components, symbolic links, gitlinks and
names that are not UTF-8, and plants an fsmonitor command, grafts and alternates.
`tests/test_schedules_scan.py` plants generated fake secrets in every encoding, split and escaped
form, checks permalinks and session links, and scans the repository's tracked files:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_schedules_paths.py tests/test_schedules_scan.py
```
