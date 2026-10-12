# Autodev guards: protected paths and the outgoing-text scan

Status: library with tests; the broker that calls it is not implemented yet.
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
backslash, a `[` character class, leading whitespace, `**` inside a component (`a**b`), and
empty, `.` or `..` components.

| Pattern | Matches |
|---|---|
| `*`, `?` | Any run of characters, or one character, within one component; never `/` |
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

Git runs without a shell and with a minimal environment: `GIT_CONFIG_NOSYSTEM=1`,
`GIT_CONFIG_GLOBAL` at the null device, `GIT_TERMINAL_PROMPT=0`, no pager, no replace
objects, literal pathspecs, `-c core.quotePath=false`, `-C <repository>` with
`GIT_CEILING_DIRECTORIES` stopping discovery above it, and `--end-of-options` before revision
arguments. Inherited `GIT_*` variables never reach it. Any git failure or unexpected output
raises `GitError`; the broker treats every `PathsError` as a refusal.

## Outgoing-text scan

The scan is a tripwire, not a protection (plan 5.6): sessions get no secrets and no network,
and the scan only notices a leak in a shape it knows. Any hit refuses the operation and blocks
the run. It looks for:

- **Token-shaped secrets** (`TOKEN_PATTERNS`): GitHub tokens (`gho_`, `ghp_`, `ghs_`, `ghu_`,
  `ghr_` and `github_pat_` followed by the token body), `sk-ant-`, `sk-or-` and `sk-proj-` keys,
  `AKIA` access key IDs, `-----BEGIN ... PRIVATE KEY-----` blocks and JWTs. A prefix alone, as in
  this paragraph, is not a hit.
- **High-entropy strings**: base64-like runs of at least 24 characters with digits and both
  letter cases and at least 4.5 bits of entropy per character. Pure hex, such as commit SHAs
  and digests, never counts. With these values every tracked file under `docs/`, `src/`,
  `flows/` and `agents/` is clean, and a test keeps it so.
- **Known secrets and decoy contents** (`KnownSecret`, label plus value of at least 8
  characters; #342 passes its token and key, #343 the decoys): raw, as base64 in either
  alphabet, padded or not, at any byte offset inside a larger blob, and as hex in either case.
- **Encoded forms**: every base64, base64url and hex run is decoded at each alignment, two levels
  deep, and the decoded bytes are scanned for the patterns and the known secrets. Whitespace,
  and the line markers of a unified diff, are removed first, so wrapped base64 and spaced hex
  are found.

A `Hit` has only `start`, `end`, `kind` (`pattern`, `entropy` or `known`) and `name` (the
pattern name or the caller's label); for a decoded hit, the offsets cover the encoded characters.
No hit, `repr` or `SecretFound` message contains the matched text.

Not covered: a secret split by other text, compressed, encrypted or in another encoding
(base32, URL encoding, ROT13); part of a decoy, unless the caller passes that part as its own
`KnownSecret`; a random string shorter than the entropy limits.

## Tests

`tests/test_schedules_paths.py` builds throwaway bare repositories under `tmp_path` with git
plumbing, so trees can hold case collisions, `.git` components, symbolic links and gitlinks.
`tests/test_schedules_scan.py` plants generated fake secrets in every encoding and scans the
repository's own tracked documentation and code:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest -q tests/test_schedules_paths.py tests/test_schedules_scan.py
```
