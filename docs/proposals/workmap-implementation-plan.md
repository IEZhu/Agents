# Work map implementation plan

**Status:** implementation plan of 2026-11-25 for the [work map plan](../workmap-plan.md); implemented in one pull request that lands all six groups below together, and superseded in part by what shipped: bound workers submit with `workmap_submit` while `validate_subagent_result` stays read-only, `read_records` splits only on newlines, the store writes its own `.gitignore`, and the pull-request grouping below was not followed. The shipped source and [the subagent contract](../subagent-contract.md) are authoritative; the rest of this plan is kept as a historical record. Line numbers refer to `origin/dev` at `ce2bfc22`; re-locate a quoted line by its
text if it has moved.

**Goal:** a parent session keeps a server-side map of its delegated stages, workers submit
their results to the server and hand back one line, and the parent reads node summaries
instead of results.

**Architecture:** a shared safe-file module extracted from the claims store; a work-map
store (`src/workmap/store.py`) that folds an append-only event log; text views
(`src/workmap/view.py`); two MCP tools (`workmap_update`, `workmap_view`); a binding
(`src/workmap/binding.py`) that ties a prepared contract to a node, pins input nodes'
results as artifacts and records a worker's own submission through
`validate_subagent_result`; a verifier rule; a built-in flow; a measurement script.

**Tech Stack:** Python 3.11, pydantic 2, FastMCP (`mcp==1.30.0`), pytest with
pytest-asyncio. No new dependency.

## Global Constraints

- Work in a linked worktree cut from `origin/dev` with `--no-track`; never edit the
  installation checkout the daemon runs from.
- The groups below land together in one pull request against `dev`. The owner merges; no
  agent merges, approves or enables auto-merge. After opening or pushing the PR, run the
  `pr-review` flow in no-merge mode.
- Commit messages: conventional `type(scope): subject`, body wrapped at 72, ending with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Every committed document is in English.
- The interpreter is the installation checkout's `.venv` Python, run from the worktree:
  `PY=<installation checkout>/.venv/bin/python`. Tests run as
  `LANGFUSE_TRACING_ENABLED=false HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 $PY -B -m pytest <files> -q -p no:cacheprovider`;
  one heavy test process at a time.
- `scripts/check_docs.py` reads `git ls-files`: stage a new document before running it.
- Work-map data lives in `<workspace>/data/workmap/` (ignored by `.gitignore` line 7) and is
  never synced.
- Limits from the spec: node id 1–40 characters of `a-z`, `0-9`, `-`; title 120; summary
  300; reason 300; 50 operations per update; 10 input nodes; view budget 3,000 by default,
  500 to 8,000; node record 2,000; five invalid submissions per contract before
  `needs_attention`.
- Each new tool description stays near 1,500 characters.

## Pull requests

| PR | Tasks | Delivers |
|---|---|---|
| 1 | Task 1 | The package states the full contract revision and asks the worker to check its own result |
| 2 | Tasks 2–5 | Shared safe-file module, the store, the views and the two tools |
| 3 | Tasks 6–7 | Binding a package to a node, pinned inputs, worker submission |
| 4 | Task 8 | Verifier verdicts |
| 5 | Task 9 | The `orchestrate` flow and the protocol pointer |
| 6 | Task 10 | The measurement script |

## File structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/engine/subagent_text.py` | Full revision in the assignment; `submission_rule` text | 1, 7 |
| `src/safe_store.py` (new) | Private append-only JSON-lines files: anchored opens, lock, torn lines, cap | 2 |
| `src/claims/store.py` | Uses `src/safe_store.py` instead of its private helpers | 2 |
| `src/workmap/__init__.py` (new) | Package marker | 3 |
| `src/workmap/store.py` (new) | Event log, folding, parent operations, server records, readiness | 3, 8 |
| `src/workmap/view.py` (new) | Map, node and list text | 4 |
| `src/server.py` | Two tools and hints; preparation and validation hooks | 5, 6, 7 |
| `src/workmap/binding.py` (new) | Preparation checks, input pinning, submission recording | 6, 7 |
| `src/schemas/subagent.py` | `workmap_node`, `workmap_inputs`, `WorkmapBinding`, `WorkmapReceipt` | 6, 7 |
| `src/engine/subagent.py` | Binding in `_assemble` and `prepare_subagent` | 6 |
| `src/engine/subagent_exchange.py` | Launch text of a bound package | 7 |
| `src/engine/subagent_validation.py` | `parsed_report` | 7 |
| `flows/orchestrate.md` (new) | The parent's loop | 9 |
| `src/engine/protocol/delegation.md` | One pointer sentence | 9 |
| `evals/scripts/workmap_context.py` (new) | Hand-back size measurement | 10 |

---

## PR 1

### Task 1: The package states the full contract revision

**Files:**
- Modify: `src/engine/subagent_text.py:106-111` (`render_assignment`), `:218-232` (`render_result_format`)
- Modify: `tests/test_subagent_exchange.py:84`
- Modify: `docs/subagent-contract.md:110-111`, `:150-151`
- Test: `tests/test_subagent_exchange.py`

**Interfaces:**
- Produces: `submission_rule(contract: dict) -> str` in `src/engine/subagent_text.py`; Task 7
  extends it for bound contracts.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_subagent_exchange.py`, after `test_text_package_carries_no_json_contract_or_schema`:

```python
def test_text_package_states_the_full_contract_revision_and_the_self_check(worker_tree):
    package = prepared()
    revision = package.contract_revision
    assert f"contract: {revision} ·" in package.worker_context
    assert f'validate_subagent_result(contract_revision="{revision}"' in package.worker_context
```

and change line 84 from

```python
    assert package.contract_revision not in package.worker_context
```

to

```python
    assert package.contract_revision in package.worker_context
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `... $PY -B -m pytest tests/test_subagent_exchange.py -q -p no:cacheprovider -k "text_package"`
Expected: FAIL in both tests (the context holds only the 12-character prefix).

- [ ] **Step 3: Implement**

In `render_assignment`, replace

```python
    revision = contract["contract_revision"][:CONTRACT_PREFIX_CHARS]
    lines = ["## Assignment",
```

with

```python
    revision = contract["contract_revision"]  # whole, so the worker can submit against it
    lines = ["## Assignment",
```

Add this function after `render_assignment`:

```python
def submission_rule(contract: dict) -> str:
    """How the worker checks its result against the stored contract before replying."""
    call = (f'validate_subagent_result(contract_revision="{contract["contract_revision"]}", '
            "result=<your complete result text>)")
    return (f"Before you reply, if the Agents-Core tool validate_subagent_result is available to you, "
            f"call {call}, correct the result until it answers valid: true, and reply with exactly "
            "the text that passed.")
```

In `render_result_format`, insert `submission_rule(contract),` as the list element right
after the string that ends with `"the reply text.",` and before the element that opens the
text template:

```python
        "the reply text.",
        submission_rule(contract),
```

Leave `CONTRACT_PREFIX_CHARS` and the result template's `contract:` line as they are: the
parser at `src/engine/subagent_text.py:546-549` accepts 12 to 64 characters.

- [ ] **Step 4: Update the reference**

In `docs/subagent-contract.md`, replace

```text
the contract revision (the text form shows its first 12 characters; only the
JSON form also names the bundle revision), the complete result shape, and the rules
```

with

```text
the contract revision (the text form states it whole and asks the worker to check its
result with `validate_subagent_result` before replying; only the JSON form also names the
bundle revision), the complete result shape, and the rules
```

and replace `non-goals, pinned artifacts, the first 12 characters of the contract revision) and a`
with `non-goals, pinned artifacts, the whole contract revision) and a`.

- [ ] **Step 5: Run the delegation tests**

Run: `... $PY -B -m pytest tests/test_subagent_exchange.py tests/test_subagent_mcp.py tests/test_subagent.py tests/test_subagent_artifacts.py tests/test_subagent_contract_text.py tests/test_subagent_ledger.py tests/test_subagent_tiers.py tests/test_subagent_combined_budget.py -q -p no:cacheprovider`
Expected: PASS. If a budget test fails on a character count, the package grew by the
52 characters of the revision plus the length of `submission_rule`; raise that test's
expected count by the measured difference and say so in the commit body.

- [ ] **Step 6: Commit and open PR 1**

```bash
git add src/engine/subagent_text.py tests/test_subagent_exchange.py docs/subagent-contract.md
git commit -m "feat(subagent): state the full contract revision to the worker"
```

---

## PR 2

### Task 2: Extract the safe-file helpers into `src/safe_store.py`

**Files:**
- Create: `src/safe_store.py`
- Modify: `src/claims/store.py` (helpers at `:37-61`, `:186-369`, `:449-475`, `:536-560`)
- Modify: `tests/test_claims.py:366-374`
- Test: `tests/test_claims.py`

**Interfaces:**
- Produces, in `src/safe_store.py`: `StoreError(code: str, message: str)`,
  `UnsafeStore(message: str)` (code `unsafe_store`), `store_directory(root: Path, parts: tuple[str, ...], *, create: bool, private: bool)`
  (context manager yielding a descriptor, a `Path` on Windows, or `None`),
  `store_file(directory, name: str, flags: int, mode: str, *, private: bool)` (context
  manager yielding a file object or `None`), `exclusive(handle, path: Path)` (context
  manager), `read_records(raw: bytes, max_bytes: int, path: Path) -> list`,
  `append_line(handle, line: bytes, max_bytes: int, path: Path) -> None`.
- `src/claims/store.py` keeps `ClaimError` importable; it becomes `StoreError`, so
  `except ClaimError` in `src/server.py` still catches `unsafe_store` and `store_full`.

- [ ] **Step 1: Create `src/safe_store.py`**

```python
"""Private append-only JSON-lines files inside a workspace.

The validated-claims store and the work map keep their records the same way: a file under
``<workspace>/data/<name>/`` (directory 0700, file 0600), opened without following links,
appended under an exclusive lock, a torn last line never glued to the next record, and a
size cap above which writes are refused. This module holds that mechanism; each store keeps
its own record format.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
from pathlib import Path
import stat
import time
from typing import Any

from src.file_lock import file_lock

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

# Opening flags that refuse a symbolic link and never block on a FIFO, where the platform has them.
NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
# POSIX opens every component from its parent's descriptor, so neither a link nor a directory
# swapped in between a check and an open can redirect the store. Windows has no dir_fd and checks
# each component with lstat instead, which leaves that swap window open.
ANCHORED = (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
            and os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd)
# What opening a link with O_NOFOLLOW, or a non-directory with O_DIRECTORY, answers.
REFUSED_KINDS = {errno.ELOOP, errno.ENOTDIR, errno.EISDIR, getattr(errno, "EMLINK", errno.ELOOP),
                 getattr(errno, "EFTYPE", errno.ELOOP)}
# Attempts at a creating open. macOS answers a creating open with ENOENT now and then while
# another thread creates the same name in the same directory (measured: hundreds of times in
# 200 rounds of 8 threads, with plain flags too), so a creating open is retried.
CREATE_ATTEMPTS = 20


class StoreError(ValueError):
    """A request a store refuses, with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class UnsafeStore(StoreError):
    def __init__(self, message: str):
        super().__init__("unsafe_store", message)


def owned_by_caller(info: os.stat_result) -> bool:
    """POSIX: the file belongs to this process's user. Windows reports no owner here."""
    return os.name == "nt" or info.st_uid == os.geteuid()


def real_directory(path: Path, *, private: bool = False) -> None:
    """``path`` exists as a directory and is not a symbolic link.

    With ``private`` it must also belong to this user, and group and other permissions
    are removed: ``mkdir``'s mode does nothing for a directory that already exists.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(mode=0o700, exist_ok=True)  # another process may create it first; checked below
        info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeStore(f"{path} is not a plain directory; the store does not follow links")
    if not private or os.name == "nt":
        return
    if not owned_by_caller(info):
        raise UnsafeStore(f"{path} belongs to another user; the store keeps only its own files")
    if info.st_mode & 0o077:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | NO_FOLLOW)
        try:
            if not os.path.samestat(info, os.fstat(descriptor)):
                raise UnsafeStore(f"{path} changed while it was opened")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)


def open_store(path: Path, flags: int, *, private: bool = False) -> int:
    """Open the store file itself, never through a link, and only as a regular file.

    With ``private`` (the writer) the file must belong to this user and loses group and
    other permissions; the mode passed to ``open`` applies only to a file it creates.
    """
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
        if not flags & os.O_CREAT:
            raise
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise UnsafeStore(f"{path} is not a regular file; the store does not follow links")
    descriptor = os.open(path, flags | NO_FOLLOW, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (before is not None and not os.path.samestat(before, opened)):
            raise UnsafeStore(f"{path} changed while it was opened")
        if private and os.name != "nt":
            if not owned_by_caller(opened):
                raise UnsafeStore(f"{path} belongs to another user; the store keeps only its own files")
            if opened.st_mode & 0o077:
                os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def not_plain(path: Path) -> UnsafeStore:
    return UnsafeStore(f"{path} is not a plain directory or file; the store does not follow links")


def private_descriptor(descriptor: int, path: Path, mode: int) -> None:
    """The open object belongs to this user and loses group and other permissions."""
    info = os.fstat(descriptor)
    if not owned_by_caller(info):
        raise UnsafeStore(f"{path} belongs to another user; the store keeps only its own files")
    if info.st_mode & 0o077:
        os.fchmod(descriptor, mode)


@contextlib.contextmanager
def store_directory(root: Path, parts: tuple[str, ...], *, create: bool, private: bool):
    """Yield ``root/<parts…>``: a descriptor on POSIX, the path on Windows, None when missing.

    On POSIX each component is opened from its parent's descriptor with ``O_NOFOLLOW``; the
    workspace root itself is trusted. With ``create`` missing components are made (0700);
    with ``private`` the last component must be this user's and loses group and other
    permissions, which ``mkdir``'s mode does not do for a directory that already exists.
    """
    last = len(parts) - 1
    if not ANCHORED:
        path = root
        for index, name in enumerate(parts):
            path = path / name
            if create:
                real_directory(path, private=private and index == last)
            else:
                try:
                    if not stat.S_ISDIR(os.lstat(path).st_mode):
                        raise not_plain(path)
                except FileNotFoundError:
                    yield None
                    return
        yield path
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    opened: list[int] = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        opened.append(current)
        path = root
        for index, name in enumerate(parts):
            path = path / name
            try:
                current = os.open(name, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                with contextlib.suppress(FileExistsError):  # another process may create it first
                    os.mkdir(name, 0o700, dir_fd=current)
                try:
                    current = os.open(name, flags, dir_fd=current)
                except OSError as error:
                    if error.errno in REFUSED_KINDS:
                        raise not_plain(path) from None
                    raise
            except OSError as error:
                if error.errno in REFUSED_KINDS:
                    raise not_plain(path) from None
                raise
            opened.append(current)
            if private and index == last:
                private_descriptor(current, path, 0o700)
        yield current
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def open_relative(directory: int, name: str, flags: int) -> int:
    for attempt in range(CREATE_ATTEMPTS):
        try:
            return os.open(name, flags | NO_FOLLOW, 0o600, dir_fd=directory)
        except FileNotFoundError:
            if not flags & os.O_CREAT or attempt == CREATE_ATTEMPTS - 1:
                raise
            time.sleep(0.001 * (attempt + 1))
    raise AssertionError("unreachable")


@contextlib.contextmanager
def store_file(directory, name: str, flags: int, mode: str, *, private: bool):
    """Yield the file ``name`` inside ``directory`` (from ``store_directory``), or None when missing."""
    if isinstance(directory, Path):
        try:
            descriptor = open_store(directory / name, flags, private=private)
        except FileNotFoundError:
            yield None
            return
    else:
        try:
            descriptor = open_relative(directory, name, flags)
        except FileNotFoundError:
            yield None
            return
        except OSError as error:
            if error.errno in REFUSED_KINDS:
                raise UnsafeStore(f"{name} is not a regular file; the store does not follow links") from None
            raise
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UnsafeStore(f"{name} is not a regular file; the store does not follow links")
            if private:
                private_descriptor(descriptor, Path(name), 0o600)
        except BaseException:
            os.close(descriptor)
            raise
    with os.fdopen(descriptor, mode) as handle:
        yield handle


@contextlib.contextmanager
def exclusive(handle, path: Path):
    """Serialize writers. POSIX locks the open file itself, so no path is resolved again;
    Windows, which opened the file by name anyway, locks a sidecar file beside it."""
    if fcntl is not None and ANCHORED:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    with file_lock(path.with_name(path.name + ".lock")):
        yield


def read_records(raw: bytes, max_bytes: int, path: Path) -> list[Any]:
    """Every line of ``raw`` that parses as JSON, in order; a torn or foreign line is skipped."""
    if len(raw) > max_bytes:
        raise UnsafeStore(f"{path} is larger than {max_bytes} bytes; archive it by hand")
    records = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            records.append(json.loads(line))
        except (ValueError, RecursionError):
            continue
    return records


def append_line(handle, line: bytes, max_bytes: int, path: Path) -> None:
    """Append ``line`` (one or more newline-terminated records) to a file opened ``r+b`` with
    ``O_APPEND`` while ``exclusive`` holds its lock."""
    size = os.fstat(handle.fileno()).st_size
    if size:
        handle.seek(size - 1)
        if handle.read(1) != b"\n":
            line = b"\n" + line  # an earlier write was torn: never glue a record onto it
    if size + len(line) > max_bytes:
        raise StoreError("store_full", f"{path} would exceed {max_bytes} bytes; "
                                       "archive it by hand to start a new store")
    handle.write(line)
    handle.flush()
```

- [ ] **Step 2: Point the claims store at it**

In `src/claims/store.py`:

1. Delete the definitions of `_NO_FOLLOW`, `_ANCHORED`, `_REFUSED_KINDS` (`:52-60`),
   `_UnsafeStore`, `_owned_by_caller`, `_real_directory`, `_open_store`, `_not_plain`,
   `_private_descriptor` (`:186-264`), `_store_directory` (`:267-320`), `_CREATE_ATTEMPTS`,
   `_open_relative` (`:323-337`), `_store_file` (`:340-369`) and the method
   `ClaimStore._exclusive` (`:462-475`). Keep `STORE_FILE`, `MAX_STORE_BYTES`, `KEY`,
   `HEX64` and everything claims-specific.
2. Replace the `ClaimError` class (`:65-70`) with:

```python
# The shared store error: the claims tools catch it as ClaimError, and the safe-file
# helpers raise it (unsafe_store, store_full) without knowing which store called them.
ClaimError = StoreError
```

3. Replace the imports `import errno`, `import stat`, the `try: import fcntl … except ImportError: fcntl = None`
   block and `from src.file_lock import file_lock` with:

```python
from src import safe_store
from src.safe_store import StoreError
```

   Keep `import contextlib` only if `grep -n "contextlib" src/claims/store.py` still finds a
   use after step 4.
4. In `ClaimStore`, add `DIRECTORY = ("data", "claims")` as a class attribute and rewrite the
   file access:

```python
    def _events(self) -> list[dict]:
        with safe_store.store_directory(self.root, self.DIRECTORY, create=False, private=False) as directory:
            if directory is None:
                return []
            return self._read(directory)

    def _read(self, directory) -> list[dict]:
        with safe_store.store_file(directory, STORE_FILE, os.O_RDONLY, "rb", private=False) as handle:
            if handle is None:
                return []
            return self._parse(handle.read(MAX_STORE_BYTES + 1))

    def _parse(self, raw: bytes) -> list[dict]:
        return [event for event in safe_store.read_records(raw, MAX_STORE_BYTES, self.path)
                if _well_formed(event)]
```

   In `record`, replace the `with` header and the write tail:

```python
        with safe_store.store_directory(self.root, self.DIRECTORY, create=True, private=True) as directory, \
                safe_store.store_file(directory, STORE_FILE, os.O_RDWR | os.O_CREAT | os.O_APPEND, "r+b",
                                      private=True) as handle, \
                safe_store.exclusive(handle, self.path):
```

   and replace the block from `line = (json.dumps(event, …` through `handle.flush()` with
   the two lines below, keeping the `updated = Claim(...)` line that follows it:

```python
            line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            safe_store.append_line(handle, line, MAX_STORE_BYTES, self.path)
```

- [ ] **Step 3: Check nothing still names a removed helper**

Run: `grep -nE "_store_directory|_store_file|_UnsafeStore|_exclusive|_NO_FOLLOW|_ANCHORED|_REFUSED_KINDS|_open_store|_real_directory|_owned_by_caller|_private_descriptor|_not_plain|_open_relative|_CREATE_ATTEMPTS|fcntl|file_lock" src/claims/store.py`
Expected: no output.

- [ ] **Step 4: Follow the lock test to the new module**

In `tests/test_claims.py`, `test_writers_lock_the_store_file_itself_not_a_path_beside_it`,
replace `import src.claims.store as module` with `import src.safe_store as module`. The
other two tests that patch `module.os` and read `module.STORE_FILE` stay as they are: `os` is
one module object, and `STORE_FILE` stays in `src/claims/store.py`.

- [ ] **Step 5: Run the claims tests**

Run: `... $PY -B -m pytest tests/test_claims.py -q -p no:cacheprovider`
Expected: PASS, the same count as before the change (record it before step 1 with the same
command).

- [ ] **Step 6: Commit**

```bash
git add src/safe_store.py src/claims/store.py tests/test_claims.py
git commit -m "refactor(claims): move the safe-file helpers into src/safe_store.py"
```

### Task 3: The work-map store

**Files:**
- Create: `src/workmap/__init__.py`, `src/workmap/store.py`
- Test: `tests/test_workmap_store.py`

**Interfaces:**
- Consumes: `src.safe_store` (Task 2).
- Produces, in `src/workmap/store.py`: `WorkmapError` (= `StoreError`), `WorkMap` with
  `task_id`, `revision`, `closed`, `nodes: dict[str, dict]`, `edges: list[tuple[str, str, str]]`,
  `invalid: dict[str, int]`, `targets(node_id, edge_type) -> list[str]`,
  `sources(node_id, edge_type) -> list[str]`; `WorkmapStore(root, *, clock=time.time)` with
  `apply(task_id, ops, expected_revision=None) -> dict`, `load(task_id) -> WorkMap | None`,
  `list_maps() -> list[WorkMap]`, `check_preparable(task_id, node_id, inputs) -> list[tuple[str, str]]`,
  `record_prepared(task_id, node_id, *, contract_revision, attempt_id, complexity) -> int`,
  `record_result(task_id, node_id, *, contract_revision, attempt_id, text, sha256, outcome, handback) -> dict`,
  `record_invalid(task_id, node_id, *, contract_revision, codes) -> int`,
  `result_text(task_id, node_id) -> str | None`; module functions `map_key(task_id) -> str`,
  `required_level(node) -> str`, `settled(node) -> bool`, `is_ready(workmap, node) -> bool`,
  `is_stale(workmap, node) -> bool`; constants `MAX_OPS`, `SUMMARY_MAX`, `INVALID_LIMIT`,
  `NODE_ID`.
- A node dict has `id, kind, title, summary, risk, status, level, reason, complexity, refs,
  checks, findings, limitations, handback, changed`; `refs` may hold `contract_revision`,
  `attempt_id`, `result_sha256`.
- `outcome` passed to `record_result` is
  `{status, level, summary, checks: {passed, failed, not_run}, findings: dict | None, limitations: int}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workmap_store.py`:

```python
"""The work-map store: parent operations, server records, folding and the safe-file pattern."""
import json
import os
import stat
import sys

import pytest

from src.workmap.store import (INVALID_LIMIT, MAX_OPS, WorkmapError, WorkmapStore, is_ready, is_stale,
                               map_key)

TASK = "task-1"
FIRST = "a" * 64
SECOND = "d" * 64
OUTCOME = {"status": "validated", "level": "V2", "summary": "Mapped",
           "checks": {"passed": 1, "failed": 0, "not_run": 0}, "findings": None, "limitations": 0}


@pytest.fixture
def store(tmp_path):
    return WorkmapStore(tmp_path, clock=lambda: 1_800_000_000.0)


def plan(store):
    return store.apply(TASK, [
        {"op": "add_node", "id": "goal", "kind": "goal", "title": "Ship the feature"},
        {"op": "add_node", "id": "map-code", "title": "Map the code", "risk": "routine"},
        {"op": "add_node", "id": "write", "title": "Write it"},
        {"op": "add_edge", "from": "write", "to": "map-code", "type": "depends_on"},
    ])


def prepare(store, node="map-code", revision=FIRST, attempt="1"):
    store.check_preparable(TASK, node, [])
    return store.record_prepared(TASK, node, contract_revision=revision, attempt_id=attempt, complexity=None)


def submit(store, node="map-code", revision=FIRST, sha="b" * 64, text="mapped code"):
    return store.record_result(TASK, node, contract_revision=revision, attempt_id="1", text=text,
                               sha256=sha, outcome=OUTCOME, handback="line")


def map_file(tmp_path):
    return tmp_path / "data" / "workmap" / f"{map_key(TASK)}.jsonl"


def test_a_first_apply_opens_the_map_and_numbers_every_event(store):
    result = plan(store)
    assert result == {"status": "applied", "task_id": TASK, "revision": 5,
                      "changed": ["goal", "map-code", "write"]}
    workmap = store.load(TASK)
    assert list(workmap.nodes) == ["goal", "map-code", "write"]
    assert workmap.nodes["write"]["status"] == "planned" and workmap.nodes["write"]["level"] == "V0"
    assert workmap.targets("write", "depends_on") == ["map-code"]


def test_the_map_is_private_and_append_only(store, tmp_path):
    plan(store)
    store.apply(TASK, [{"op": "update_node", "id": "write", "summary": "Now with a summary"}])
    lines = map_file(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6 and json.loads(lines[0])["op"] == "open"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(map_file(tmp_path)).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(map_file(tmp_path).parent).st_mode) == 0o700


def test_an_operation_list_is_all_or_nothing(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "add_node", "id": "extra", "title": "Fine"},
                           {"op": "add_node", "id": "extra", "title": "Duplicate"}])
    assert raised.value.code == "invalid_request"
    assert "extra" not in store.load(TASK).nodes


@pytest.mark.parametrize("status", ["prepared", "validated", "needs_attention", "verified", "disputed"])
def test_the_parent_cannot_set_a_server_status(store, status):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "update_node", "id": "write", "status": status}])
    assert raised.value.code == "status_reserved"


def test_only_a_settled_node_can_be_accepted(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "update_node", "id": "write", "status": "accepted"}])
    assert raised.value.code == "invalid_transition"


def test_a_depends_on_cycle_is_refused(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "add_edge", "from": "map-code", "to": "write", "type": "depends_on"}])
    assert raised.value.code == "cycle"


def test_a_stale_expected_revision_is_refused(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "close"}], expected_revision=4)
    assert raised.value.code == "revision_conflict"
    assert store.apply(TASK, [{"op": "close"}], expected_revision=5)["revision"] == 6


def test_a_closed_map_refuses_operations(store):
    plan(store)
    store.apply(TASK, [{"op": "close"}])
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "add_node", "id": "late", "title": "Too late"}])
    assert raised.value.code == "map_closed"


def test_too_many_operations_are_refused(store):
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "add_node", "id": f"n{i}", "title": "Node"} for i in range(MAX_OPS + 1)])
    assert raised.value.code == "invalid_request"


def test_a_torn_last_line_is_skipped_and_never_glued(store, tmp_path):
    plan(store)
    with open(map_file(tmp_path), "ab") as handle:
        handle.write(b'{"op": "add_node", "id": "torn"')
    store.apply(TASK, [{"op": "add_node", "id": "after", "title": "After the tear"}])
    workmap = store.load(TASK)
    assert "after" in workmap.nodes and "torn" not in workmap.nodes


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_a_linked_map_file_is_refused(store, tmp_path):
    plan(store)
    target = tmp_path / "elsewhere.jsonl"
    map_file(tmp_path).rename(target)
    map_file(tmp_path).symlink_to(target)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "close"}])
    assert raised.value.code == "unsafe_store"


def test_a_valid_result_settles_its_node_and_readies_the_dependant(store):
    plan(store)
    workmap = store.load(TASK)
    assert not is_ready(workmap, workmap.nodes["write"])
    prepare(store)
    receipt = submit(store)
    assert receipt == {"task_id": TASK, "node": "map-code", "status": "validated", "level": "V2",
                       "revision": 7, "duplicate": False}
    workmap = store.load(TASK)
    assert workmap.nodes["map-code"]["summary"] == "Mapped"
    assert is_ready(workmap, workmap.nodes["write"])
    assert store.result_text(TASK, "map-code") == "mapped code"


def test_a_second_different_result_for_one_contract_is_refused(store):
    plan(store)
    prepare(store)
    submit(store)
    assert submit(store)["duplicate"] is True
    with pytest.raises(WorkmapError) as raised:
        submit(store, sha="c" * 64)
    assert raised.value.code == "already_submitted"


def test_a_result_for_an_earlier_attempt_is_refused(store):
    plan(store)
    prepare(store, revision=FIRST)
    prepare(store, revision=SECOND, attempt="2")
    with pytest.raises(WorkmapError) as raised:
        submit(store, revision=FIRST)
    assert raised.value.code == "stale_contract"


def test_repeated_invalid_submissions_send_the_node_to_needs_attention(store):
    plan(store)
    prepare(store)
    for count in range(1, INVALID_LIMIT + 1):
        assert store.record_invalid(TASK, "map-code", contract_revision=FIRST, codes=["string_too_long"]) == count
    assert store.load(TASK).nodes["map-code"]["status"] == "needs_attention"


def test_a_goal_cannot_be_prepared(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.check_preparable(TASK, "goal", [])
    assert raised.value.code == "node_not_preparable"


def test_an_input_must_be_settled_and_returns_its_stored_text(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.check_preparable(TASK, "write", ["map-code"])
    assert raised.value.code == "input_not_ready"
    prepare(store)
    submit(store)
    assert store.check_preparable(TASK, "write", ["map-code"]) == [("map-code", "mapped code")]


def test_a_refuted_dependency_makes_its_dependant_stale(store):
    plan(store)
    prepare(store)
    submit(store)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "refuted", "reason": "Wrong file"}])
    workmap = store.load(TASK)
    assert is_stale(workmap, workmap.nodes["write"])


def test_an_unknown_map_is_reported(store):
    with pytest.raises(WorkmapError) as raised:
        store.check_preparable(TASK, "write", [])
    assert raised.value.code == "unknown_map"
    assert store.load(TASK) is None and store.list_maps() == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `... $PY -B -m pytest tests/test_workmap_store.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.workmap'`.

- [ ] **Step 3: Implement**

Create `src/workmap/__init__.py` with one line:

```python
"""Per-task work maps: the parent's top-down view of delegated stages (docs/workmap-plan.md)."""
```

Create `src/workmap/store.py`:

```python
"""Append-only per-task work maps: the parent's top-down view of delegated stages.

One JSON line per event in ``<workspace>/data/workmap/<key>.jsonl``, where ``key`` is the
first 16 hexadecimal characters of the SHA-256 of the task ID, and the exact text of each
submitted result in ``<key>.results.jsonl`` beside it. The map's state is never stored: it
is folded from the events on every read. The parent writes structure and its own statuses
through ``apply``; only the server records preparation, results and verdicts.
"""
from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Callable

from src import safe_store
from src.safe_store import StoreError

FORMAT_VERSION = 1
MAX_STORE_BYTES = 20 * 1024 * 1024
DIRECTORY = ("data", "workmap")
MAX_OPS = 50
TITLE_MAX = 120
SUMMARY_MAX = 300
REASON_MAX = 300
INVALID_LIMIT = 5
NODE_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
MAP_FILE = re.compile(r"[0-9a-f]{16}\.jsonl")
NODE_KINDS = ("goal", "stage", "decision", "question")
RISKS = ("routine", "load_bearing", "critical")
EDGE_TYPES = ("depends_on", "verifies", "supersedes", "contradicts")
PARENT_STATUSES = ("planned", "accepted", "refuted", "superseded", "blocked")
SERVER_STATUSES = ("prepared", "validated", "needs_attention", "verified", "disputed")
REASONED = ("refuted", "superseded", "blocked")
PREPARABLE = ("planned", "prepared", "needs_attention", "disputed")
SETTLED = ("validated", "verified", "accepted")
UNSETTLING = ("refuted", "superseded", "disputed")
LEVELS = ("V0", "V1", "V2", "V3", "V4")
OPERATIONS = {
    "add_node": {"op", "id", "kind", "title", "summary", "risk"},
    "update_node": {"op", "id", "title", "summary", "risk", "status", "reason", "level"},
    "add_edge": {"op", "from", "to", "type"},
    "remove_edge": {"op", "from", "to", "type"},
    "close": {"op"},
}

# One error type for the tools and the binding to catch; codes tell the cases apart.
WorkmapError = StoreError


def map_key(task_id: str) -> str:
    """The file name stem: a hash, so a task ID's dots and colons never reach the file system."""
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _task_id(value: object) -> str:
    if not isinstance(value, str) or not TASK_ID.fullmatch(value):
        raise WorkmapError("invalid_request", "task_id is 1 to 128 letters, digits, '_', '.', ':' or '-', "
                                              "starting with a letter or digit")
    return value


def _node_id(value: object, where: str) -> str:
    if not isinstance(value, str) or not NODE_ID.fullmatch(value):
        raise WorkmapError("invalid_request", f"{where}: a node id is 1 to 40 characters of a-z, 0-9 and "
                                              "'-', starting and ending with a letter or digit")
    return value


def _text(where: str, value: object, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise WorkmapError("invalid_request", f"{where} is required text")
    value = " ".join(value.split())
    if len(value) > limit:
        raise WorkmapError("invalid_request", f"{where} is {len(value)} characters; at most {limit}")
    return value


def _choice(where: str, value: object, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise WorkmapError("invalid_request", f"{where} must be one of {', '.join(allowed)}; got {value!r}")
    return value


@dataclass
class WorkMap:
    """A map's state, folded from its events; ``nodes`` keeps creation order."""

    task_id: str
    revision: int = 0
    closed: bool = False
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)
    invalid: dict[str, int] = field(default_factory=dict)

    def targets(self, node_id: str, edge_type: str) -> list[str]:
        return [target for source, target, kind in self.edges if source == node_id and kind == edge_type]

    def sources(self, node_id: str, edge_type: str) -> list[str]:
        return [source for source, target, kind in self.edges if target == node_id and kind == edge_type]


def _new_node(event: dict) -> dict:
    return {"id": event["id"], "kind": event["kind"], "title": event["title"], "summary": event["summary"],
            "risk": event["risk"], "status": "planned", "level": "V0", "reason": None, "complexity": None,
            "refs": {}, "checks": None, "findings": None, "limitations": 0, "handback": None,
            "changed": event["rev"]}


def _apply_event(workmap: WorkMap, event: dict) -> None:
    op, rev = event["op"], event["rev"]
    if op == "add_node":
        workmap.nodes[event["id"]] = _new_node(event)
        return
    if op in ("add_edge", "remove_edge"):
        edge = (event["from"], event["to"], event["type"])
        if op == "add_edge":
            workmap.edges.append(edge)
        else:
            workmap.edges.remove(edge)
        for node_id in edge[:2]:
            workmap.nodes[node_id]["changed"] = rev
        return
    if op == "close":
        workmap.closed = True
        return
    if op == "invalid":
        revision = event["contract_revision"]
        workmap.invalid[revision] = workmap.invalid.get(revision, 0) + 1
        if event.get("status") is None:
            return
    node = workmap.nodes[event["id"]]
    if op == "update_node":
        for key in ("title", "summary", "risk", "status", "reason", "level"):
            if key in event:
                node[key] = event[key]
    elif op == "prepared":
        node.update(status="prepared", reason=None, complexity=event["complexity"], handback=None,
                    refs={"contract_revision": event["contract_revision"], "attempt_id": event["attempt_id"]})
    elif op == "result":
        node.update(status=event["status"], level=event["level"], summary=event["summary"],
                    checks=event["checks"], findings=event["findings"], limitations=event["limitations"],
                    handback=event["handback"])
        node["refs"] = {**node["refs"], "result_sha256": event["result_sha256"]}
    elif op == "invalid":
        node["status"] = event["status"]
    elif op == "verdict":
        node["status"] = event["status"]
        if event.get("level") is not None:
            node["level"] = event["level"]
    else:
        raise ValueError(op)
    node["changed"] = rev


def fold(events: list) -> WorkMap | None:
    """The state after every well-formed event, in order; a damaged or foreign line is skipped."""
    workmap = None
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("rev"), int):
            continue
        if workmap is None:
            if event.get("op") == "open" and isinstance(event.get("task_id"), str):
                workmap = WorkMap(task_id=event["task_id"], revision=event["rev"])
            continue
        try:
            _apply_event(workmap, event)
        except (KeyError, TypeError, ValueError):
            continue
        workmap.revision = event["rev"]
    return workmap


def required_level(node: dict) -> str:
    """What a dependant needs from this node: V3 when it is critical or was prepared as high complexity."""
    return "V3" if node["risk"] == "critical" or node["complexity"] == "high" else "V2"


def settled(node: dict) -> bool:
    return node["status"] in SETTLED and LEVELS.index(node["level"]) >= LEVELS.index(required_level(node))


def is_ready(workmap: WorkMap, node: dict) -> bool:
    """A planned non-goal node whose dependencies are settled and whose verified targets have a result."""
    if node["kind"] == "goal" or node["status"] != "planned":
        return False
    return (all(settled(workmap.nodes[target]) for target in workmap.targets(node["id"], "depends_on"))
            and all("result_sha256" in workmap.nodes[target]["refs"]
                    for target in workmap.targets(node["id"], "verifies")))


def is_stale(workmap: WorkMap, node: dict) -> bool:
    return any(workmap.nodes[target]["status"] in UNSETTLING
               for target in workmap.targets(node["id"], "depends_on"))


def _existing(workmap: WorkMap, node_id: object, where: str) -> dict:
    node = workmap.nodes.get(_node_id(node_id, where))
    if node is None:
        raise WorkmapError("unknown_node", f"{where}: the map has no node {node_id}")
    return node


def _reaches(workmap: WorkMap, start: str, goal: str) -> bool:
    """Whether ``start`` depends on ``goal``, directly or through other nodes."""
    seen, stack = set(), [start]
    while stack:
        node_id = stack.pop()
        if node_id == goal:
            return True
        if node_id not in seen:
            seen.add(node_id)
            stack.extend(workmap.targets(node_id, "depends_on"))
    return False


def _parent_event(workmap: WorkMap, op: object, where: str) -> dict:
    """One parent operation checked against the draft state, as the event that records it."""
    if not isinstance(op, dict) or op.get("op") not in OPERATIONS:
        raise WorkmapError("invalid_request", f"{where} is an object whose op is one of {', '.join(OPERATIONS)}")
    if workmap.closed:
        raise WorkmapError("map_closed", "the map is closed")
    kind = op["op"]
    unknown = set(op) - OPERATIONS[kind]
    if unknown:
        raise WorkmapError("invalid_request", f"{where} has unknown fields: {', '.join(sorted(unknown))}")
    if kind == "add_node":
        node_id = _node_id(op.get("id"), f"{where}.id")
        if node_id in workmap.nodes:
            raise WorkmapError("invalid_request", f"{where}: node {node_id} exists")
        return {"op": kind, "by": "parent", "id": node_id,
                "kind": _choice(f"{where}.kind", op.get("kind", "stage"), NODE_KINDS),
                "title": _text(f"{where}.title", op.get("title"), TITLE_MAX),
                "summary": _text(f"{where}.summary", op.get("summary", ""), SUMMARY_MAX, allow_empty=True),
                "risk": _choice(f"{where}.risk", op.get("risk", "load_bearing"), RISKS)}
    if kind == "update_node":
        node = _existing(workmap, op.get("id"), f"{where}.id")
        event = {"op": kind, "by": "parent", "id": node["id"]}
        if "title" in op:
            event["title"] = _text(f"{where}.title", op["title"], TITLE_MAX)
        if "summary" in op:
            event["summary"] = _text(f"{where}.summary", op["summary"], SUMMARY_MAX, allow_empty=True)
        if "risk" in op:
            event["risk"] = _choice(f"{where}.risk", op["risk"], RISKS)
        if "status" in op:
            if op["status"] in SERVER_STATUSES:
                raise WorkmapError("status_reserved", f"{where}: only the server sets {op['status']}")
            status = _choice(f"{where}.status", op["status"], PARENT_STATUSES)
            if status == "accepted" and node["status"] not in SETTLED:
                raise WorkmapError("invalid_transition",
                                   f"{where}: only a validated or verified node can be accepted")
            event["status"] = status
            event["reason"] = (_text(f"{where}.reason", op.get("reason"), REASON_MAX)
                               if status in REASONED else None)
        elif "reason" in op:
            raise WorkmapError("invalid_request", f"{where}: a reason goes with a status")
        if "level" in op:
            if op["level"] != "V4" or event.get("status") != "accepted":
                raise WorkmapError("invalid_request",
                                   f"{where}: the parent sets only level V4, together with status accepted")
            event["level"] = "V4"
        if len(event) == 3:
            raise WorkmapError("invalid_request", f"{where} changes nothing")
        return event
    if kind in ("add_edge", "remove_edge"):
        source = _existing(workmap, op.get("from"), f"{where}.from")["id"]
        target = _existing(workmap, op.get("to"), f"{where}.to")["id"]
        edge_type = _choice(f"{where}.type", op.get("type"), EDGE_TYPES)
        edge = (source, target, edge_type)
        if kind == "add_edge":
            if source == target:
                raise WorkmapError("invalid_request", f"{where}: a node cannot point to itself")
            if edge in workmap.edges:
                raise WorkmapError("invalid_request", f"{where}: the edge exists")
            if edge_type == "depends_on" and _reaches(workmap, target, source):
                raise WorkmapError("cycle", f"{where}: {source} depends_on {target} would close a cycle")
        elif edge not in workmap.edges:
            raise WorkmapError("invalid_request", f"{where}: no such edge")
        return {"op": kind, "by": "parent", "from": source, "to": target, "type": edge_type}
    return {"op": "close", "by": "parent"}


class WorkmapStore:
    """The work maps of one workspace."""

    def __init__(self, root: Path | str, *, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.directory = self.root.joinpath(*DIRECTORY)
        self.clock = clock

    # --- files ---------------------------------------------------------------------

    def _read(self, name: str) -> list:
        with safe_store.store_directory(self.root, DIRECTORY, create=False, private=False) as directory:
            if directory is None:
                return []
            with safe_store.store_file(directory, name, os.O_RDONLY, "rb", private=False) as handle:
                if handle is None:
                    return []
                return safe_store.read_records(handle.read(MAX_STORE_BYTES + 1), MAX_STORE_BYTES,
                                               self.directory / name)

    @contextlib.contextmanager
    def _writing(self, name: str):
        """The open file under its exclusive lock, and the records it held when the lock was taken."""
        with safe_store.store_directory(self.root, DIRECTORY, create=True, private=True) as directory, \
                safe_store.store_file(directory, name, os.O_RDWR | os.O_CREAT | os.O_APPEND, "r+b",
                                      private=True) as handle, \
                safe_store.exclusive(handle, self.directory / name):
            handle.seek(0)
            yield handle, safe_store.read_records(handle.read(MAX_STORE_BYTES + 1), MAX_STORE_BYTES,
                                                  self.directory / name)

    def _append(self, handle, name: str, records: list[dict]) -> None:
        data = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
        safe_store.append_line(handle, data.encode("utf-8"), MAX_STORE_BYTES, self.directory / name)

    def _commit(self, handle, name: str, events: list[dict]) -> int:
        """Stamp numbered events and append them in one write; returns the last revision."""
        now = _iso(self.clock())
        for event in events:
            event.update(v=FORMAT_VERSION, at=now)
        self._append(handle, name, events)
        return events[-1]["rev"]

    @staticmethod
    def _state(records: list, task_id: str) -> WorkMap | None:
        workmap = fold(records)
        if workmap is not None and workmap.task_id != task_id:
            raise WorkmapError("key_collision", "another task's map holds this file; choose another task_id")
        return workmap

    def _existing_map(self, records: list, task_id: str) -> WorkMap:
        workmap = self._state(records, task_id)
        if workmap is None:
            raise WorkmapError("unknown_map", f"no work map for task {task_id}; create it with workmap_update")
        return workmap

    # --- reading -------------------------------------------------------------------

    def load(self, task_id: object) -> WorkMap | None:
        task_id = _task_id(task_id)
        return self._state(self._read(f"{map_key(task_id)}.jsonl"), task_id)

    def list_maps(self) -> list[WorkMap]:
        with safe_store.store_directory(self.root, DIRECTORY, create=False, private=False) as directory:
            if directory is None:
                return []
            names = sorted(name for name in os.listdir(directory) if MAP_FILE.fullmatch(name))
        maps = [fold(self._read(name)) for name in names]
        return [workmap for workmap in maps if workmap is not None]

    def _result(self, task_id: str, node: dict) -> str | None:
        sha256 = node["refs"].get("result_sha256")
        if sha256 is None:
            return None
        for record in reversed(self._read(f"{map_key(task_id)}.results.jsonl")):
            if (isinstance(record, dict) and record.get("sha256") == sha256
                    and record.get("node") == node["id"] and isinstance(record.get("text"), str)):
                return record["text"]
        return None

    def result_text(self, task_id: object, node_id: object) -> str | None:
        task_id = _task_id(task_id)
        workmap = self._existing_map(self._read(f"{map_key(task_id)}.jsonl"), task_id)
        return self._result(task_id, _existing(workmap, node_id, "node"))

    # --- parent writes -------------------------------------------------------------

    def apply(self, task_id: object, ops: object, expected_revision: object = None) -> dict:
        """Apply parent operations together, or none of them; the first call creates the map."""
        task_id = _task_id(task_id)
        if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPS:
            raise WorkmapError("invalid_request", f"ops is a list of 1 to {MAX_OPS} operations")
        if expected_revision is not None and (isinstance(expected_revision, bool)
                                              or not isinstance(expected_revision, int)):
            raise WorkmapError("invalid_request", "expected_revision is an integer")
        name = f"{map_key(task_id)}.jsonl"
        with self._writing(name) as (handle, records):
            current = self._state(records, task_id)
            revision = current.revision if current is not None else 0
            if expected_revision is not None and expected_revision != revision:
                raise WorkmapError("revision_conflict", f"the map is at revision {revision}, not {expected_revision}")
            draft = copy.deepcopy(current) if current is not None else WorkMap(task_id=task_id)
            events = []
            if current is None:
                revision += 1
                events.append({"op": "open", "by": "parent", "task_id": task_id, "rev": revision})
            for index, op in enumerate(ops):
                event = _parent_event(draft, op, f"ops[{index}]")
                revision += 1
                event["rev"] = revision
                _apply_event(draft, event)
                events.append(event)
            self._commit(handle, name, events)
        changed = sorted({value for event in events for key in ("id", "from", "to")
                          if isinstance(value := event.get(key), str)})
        return {"status": "applied", "task_id": task_id, "revision": revision, "changed": changed}

    # --- server writes -------------------------------------------------------------

    def check_preparable(self, task_id: object, node_id: object, inputs: list) -> list[tuple[str, str]]:
        """Refuse a node that cannot take a package; return the stored text of each input node."""
        task_id = _task_id(task_id)
        workmap = self._existing_map(self._read(f"{map_key(task_id)}.jsonl"), task_id)
        if workmap.closed:
            raise WorkmapError("map_closed", "the map is closed")
        node = _existing(workmap, node_id, "workmap_node")
        if node["kind"] == "goal" or node["status"] not in PREPARABLE:
            raise WorkmapError("node_not_preparable",
                               f"node {node['id']} is a {node['kind']} in status {node['status']}; only a "
                               "planned, prepared, needs_attention or disputed non-goal node takes a package")
        texts = []
        for index, input_id in enumerate(inputs):
            source = _existing(workmap, input_id, f"workmap_inputs[{index}]")
            text = self._result(task_id, source) if source["status"] in SETTLED else None
            if text is None:
                raise WorkmapError("input_not_ready", f"node {source['id']} has no validated result yet")
            texts.append((source["id"], text))
        return texts

    def record_prepared(self, task_id: object, node_id: object, *, contract_revision: str,
                        attempt_id: str, complexity: str | None) -> int:
        task_id = _task_id(task_id)
        name = f"{map_key(task_id)}.jsonl"
        with self._writing(name) as (handle, records):
            workmap = self._existing_map(records, task_id)
            node = _existing(workmap, node_id, "workmap_node")
            if workmap.closed or node["kind"] == "goal" or node["status"] not in PREPARABLE:
                raise WorkmapError("node_not_preparable", f"node {node['id']} no longer takes a package")
            return self._commit(handle, name, [{
                "op": "prepared", "by": "server", "id": node["id"], "rev": workmap.revision + 1,
                "contract_revision": contract_revision, "attempt_id": attempt_id, "complexity": complexity}])

    def _keep_text(self, task_id: str, node_id: str, contract_revision: str, sha256: str, text: str) -> None:
        name = f"{map_key(task_id)}.results.jsonl"
        with self._writing(name) as (handle, _records):
            self._append(handle, name, [{"v": FORMAT_VERSION, "task_id": task_id, "node": node_id,
                                         "contract_revision": contract_revision, "sha256": sha256,
                                         "text": text}])

    def record_result(self, task_id: object, node_id: object, *, contract_revision: str, attempt_id: str,
                      text: str, sha256: str, outcome: dict, handback: str) -> dict:
        """Keep a valid result and record it on its node; the first result of a contract is final."""
        task_id = _task_id(task_id)
        name = f"{map_key(task_id)}.jsonl"
        with self._writing(name) as (handle, records):
            workmap = self._existing_map(records, task_id)
            node = _existing(workmap, node_id, "workmap.node")
            earlier = next((record for record in reversed(records) if isinstance(record, dict)
                            and record.get("op") == "result"
                            and record.get("contract_revision") == contract_revision), None)
            if earlier is not None:
                if earlier.get("result_sha256") != sha256:
                    raise WorkmapError("already_submitted", "a different result is already recorded for this "
                                                            "contract; a new result needs a new attempt")
                return {"task_id": task_id, "node": node["id"], "status": node["status"],
                        "level": node["level"], "revision": workmap.revision, "duplicate": True}
            if node["refs"].get("contract_revision") != contract_revision:
                raise WorkmapError("stale_contract", f"node {node['id']} was prepared again; this contract is "
                                                     "no longer its current attempt")
            self._keep_text(task_id, node["id"], contract_revision, sha256, text)
            event = {"op": "result", "by": "server", "id": node["id"], "rev": workmap.revision + 1,
                     "contract_revision": contract_revision, "attempt_id": attempt_id,
                     "result_sha256": sha256, "handback": handback,
                     **{key: outcome[key] for key in ("status", "level", "summary", "checks", "findings",
                                                      "limitations")}}
            revision = self._commit(handle, name, [event])
        return {"task_id": task_id, "node": node["id"], "status": event["status"], "level": event["level"],
                "revision": revision, "duplicate": False}

    def record_invalid(self, task_id: object, node_id: object, *, contract_revision: str,
                       codes: list[str]) -> int:
        """Count an invalid submission; the limit sends the node's current attempt to needs_attention."""
        task_id = _task_id(task_id)
        name = f"{map_key(task_id)}.jsonl"
        with self._writing(name) as (handle, records):
            workmap = self._existing_map(records, task_id)
            node = _existing(workmap, node_id, "workmap.node")
            count = workmap.invalid.get(contract_revision, 0) + 1
            current = node["status"] == "prepared" and node["refs"].get("contract_revision") == contract_revision
            self._commit(handle, name, [{
                "op": "invalid", "by": "server", "id": node["id"], "rev": workmap.revision + 1,
                "contract_revision": contract_revision, "codes": list(codes)[:20],
                "status": "needs_attention" if current and count >= INVALID_LIMIT else None}])
        return count
```

- [ ] **Step 4: Run the tests**

Run: `... $PY -B -m pytest tests/test_workmap_store.py tests/test_claims.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/workmap/__init__.py src/workmap/store.py tests/test_workmap_store.py
git commit -m "feat(workmap): add the per-task work-map store"
```

### Task 4: Map, node and list views

**Files:**
- Create: `src/workmap/view.py`
- Test: `tests/test_workmap_view.py`

**Interfaces:**
- Consumes: `WorkMap`, `is_ready`, `is_stale`, `required_level`, `WorkmapError` (Task 3).
- Produces: `render_map(workmap, *, budget=DEFAULT_BUDGET, since=None) -> str`,
  `render_node(workmap, node_id) -> str`, `render_list(maps) -> str`, constants
  `DEFAULT_BUDGET = 3000`, `MIN_BUDGET = 500`, `MAX_BUDGET = 8000`, `NODE_BUDGET = 2000`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workmap_view.py`:

```python
"""Work-map views: dependency order, budgets, deltas and node records."""
import pytest

from src.workmap.store import WorkmapStore
from src.workmap.view import render_list, render_map, render_node

TASK = "task-1"


@pytest.fixture
def store(tmp_path):
    store = WorkmapStore(tmp_path, clock=lambda: 1_800_000_000.0)
    store.apply(TASK, [
        {"op": "add_node", "id": "goal", "kind": "goal", "title": "Ship the feature"},
        {"op": "add_node", "id": "write", "title": "Write it", "summary": "The change itself"},
        {"op": "add_node", "id": "map-code", "title": "Map the code", "risk": "routine"},
        {"op": "add_edge", "from": "write", "to": "map-code", "type": "depends_on"},
    ])
    return store


def test_the_view_lists_dependencies_first_and_says_what_is_ready(store):
    lines = render_map(store.load(TASK)).splitlines()
    assert lines[0] == "map task-1 · revision 5"
    assert lines[1:4] == ["[planned V0] goal: Ship the feature",
                          "[planned V0] map-code: Map the code",
                          "[planned V0] write: Write it (after map-code) — The change itself"]
    assert lines[-2:] == ["ready: map-code", "needs attention: none"]


def test_since_shows_only_the_nodes_changed_after_a_revision(store):
    store.apply(TASK, [{"op": "update_node", "id": "write", "summary": "Smaller change"}])
    text = render_map(store.load(TASK), since=5)
    assert "changed after revision 5: 1 of 3 nodes" in text
    assert "write: Write it (after map-code) — Smaller change" in text
    assert "map-code: Map the code" not in text


def test_a_short_budget_drops_summaries_before_nodes(tmp_path):
    store = WorkmapStore(tmp_path)
    store.apply(TASK, [{"op": "add_node", "id": f"n{i}", "title": f"Node {i}", "summary": "s" * 290}
                       for i in range(4)])
    current = store.load(TASK)
    assert len(render_map(current)) > 1000
    shorter = render_map(current, budget=600)
    assert len(shorter) <= 600 and "s" * 290 not in shorter and "n3: Node 3" in shorter


def test_a_budget_too_small_for_every_node_says_how_many_are_left(tmp_path):
    store = WorkmapStore(tmp_path)
    store.apply(TASK, [{"op": "add_node", "id": f"node-{i}", "title": "T" * 100} for i in range(40)])
    text = render_map(store.load(TASK), budget=1000)
    assert len(text) <= 1000
    assert "more nodes: read them with since or node" in text


def test_a_node_record_shows_its_relations(store):
    text = render_node(store.load(TASK), "map-code")
    assert text.splitlines()[0] == "node map-code (stage, risk routine, needs V2) [planned V0]"
    assert "needed by write" in text


def test_the_list_names_each_map_with_its_goal(store):
    assert render_list(store.list_maps()) == "task-1 · revision 5 · Ship the feature · planned 3"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `... $PY -B -m pytest tests/test_workmap_view.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.workmap.view'`.

- [ ] **Step 3: Implement**

Create `src/workmap/view.py`:

```python
"""Text views of a work map: what the parent reads instead of the event log or the results."""
from __future__ import annotations

from src.workmap.store import WorkMap, WorkmapError, is_ready, is_stale, required_level

DEFAULT_BUDGET = 3_000
MIN_BUDGET = 500
MAX_BUDGET = 8_000
NODE_BUDGET = 2_000
LIST_MAX = 10
ATTENTION = ("needs_attention", "disputed")
MORE = "more nodes: read them with since or node"


def _ids(values: list[str]) -> str:
    shown = ", ".join(values[:LIST_MAX])
    more = f" and {len(values) - LIST_MAX} more" if len(values) > LIST_MAX else ""
    return shown + more if shown else "none"


def ordered(workmap: WorkMap) -> list[dict]:
    """Nodes with every depends_on target before its dependant; ties keep creation order."""
    placed: set[str] = set()
    order: list[dict] = []
    remaining = list(workmap.nodes)
    while remaining:
        ready = [node_id for node_id in remaining
                 if all(target in placed for target in workmap.targets(node_id, "depends_on"))]
        if not ready:  # a cycle cannot be written, but a damaged log must still render
            ready = remaining[:]
        for node_id in ready:
            order.append(workmap.nodes[node_id])
            placed.add(node_id)
            remaining.remove(node_id)
    return order


def _line(workmap: WorkMap, node: dict, *, summary: bool) -> str:
    marks = f"{node['status']} {node['level']}" + (" stale" if is_stale(workmap, node) else "")
    line = f"[{marks}] {node['id']}: {node['title']}"
    after = workmap.targets(node["id"], "depends_on")
    if after:
        line += f" (after {', '.join(after)})"
    checked = workmap.targets(node["id"], "verifies")
    if checked:
        line += f" (verifies {', '.join(checked)})"
    if summary and node["summary"]:
        line += f" — {node['summary']}"
    return line


def render_map(workmap: WorkMap, *, budget: int = DEFAULT_BUDGET, since: int | None = None) -> str:
    """One line per node in dependency order, then what is ready and what needs the parent.

    Summaries go first when the budget runs short, then the last nodes; the ready and
    attention lists name at most ten nodes each.
    """
    budget = max(MIN_BUDGET, min(budget, MAX_BUDGET))
    everything = ordered(workmap)
    nodes = everything if since is None else [node for node in everything if node["changed"] > since]
    header = [f"map {workmap.task_id} · revision {workmap.revision}" + (" · closed" if workmap.closed else "")]
    if since is not None:
        header.append(f"changed after revision {since}: {len(nodes)} of {len(everything)} nodes")
    footer = [f"ready: {_ids([node['id'] for node in everything if is_ready(workmap, node)])}",
              f"needs attention: {_ids([node['id'] for node in everything if node['status'] in ATTENTION])}"]
    lines: list[str] = []
    for summary in (True, False):
        lines = [_line(workmap, node, summary=summary) for node in nodes]
        text = "\n".join(header + lines + footer)
        if len(text) <= budget:
            return text
    kept: list[str] = []
    for line in lines:
        rest = len(lines) - len(kept) - 1
        note = [f"… {rest} {MORE}"] if rest else []
        if len("\n".join(header + kept + [line] + note + footer)) > budget:
            break
        kept.append(line)
    return "\n".join(header + kept + [f"… {len(lines) - len(kept)} {MORE}"] + footer)


def render_node(workmap: WorkMap, node_id: str) -> str:
    node = workmap.nodes.get(node_id)
    if node is None:
        raise WorkmapError("unknown_node", f"the map has no node {node_id}")
    lines = [f"node {node['id']} ({node['kind']}, risk {node['risk']}, needs {required_level(node)}) "
             f"[{node['status']} {node['level']}]",
             f"title: {node['title']}"]
    if node["summary"]:
        lines.append(f"summary: {node['summary']}")
    if node["reason"]:
        lines.append(f"reason: {node['reason']}")
    relations = [("after", workmap.targets(node_id, "depends_on")),
                 ("needed by", workmap.sources(node_id, "depends_on")),
                 ("verifies", workmap.targets(node_id, "verifies")),
                 ("verified by", workmap.sources(node_id, "verifies")),
                 ("supersedes", workmap.targets(node_id, "supersedes")),
                 ("contradicts", workmap.targets(node_id, "contradicts"))]
    shown = [f"{label} {_ids(ids)}" for label, ids in relations if ids]
    if shown:
        lines.append("; ".join(shown))
    if node["refs"]:
        lines.append(" · ".join(f"{key} {value}" for key, value in node["refs"].items()))
    if node["checks"] is not None:
        checks = node["checks"]
        lines.append(f"checks: passed {checks['passed']}, failed {checks['failed']}, not run "
                     f"{checks['not_run']} · limitations {node['limitations']}")
    if node["findings"]:
        lines.append("findings: " + ", ".join(f"{severity} {count}"
                                              for severity, count in sorted(node["findings"].items())))
    if node["handback"]:
        lines.append(f"hand-back: {node['handback']}")
    invalid = workmap.invalid.get(node["refs"].get("contract_revision", ""), 0)
    if node["status"] == "prepared" and invalid:
        lines.append(f"invalid submissions: {invalid}")
    text = "\n".join(lines)
    return text if len(text) <= NODE_BUDGET else text[:NODE_BUDGET - 1] + "…"


def render_list(maps: list[WorkMap]) -> str:
    if not maps:
        return "no work maps in this workspace"
    lines = []
    for workmap in maps:
        goals = [node["title"] for node in workmap.nodes.values() if node["kind"] == "goal"]
        counts: dict[str, int] = {}
        for node in workmap.nodes.values():
            counts[node["status"]] = counts.get(node["status"], 0) + 1
        tally = ", ".join(f"{status} {count}" for status, count in sorted(counts.items())) or "empty"
        lines.append(f"{workmap.task_id} · revision {workmap.revision}" + (" · closed" if workmap.closed else "")
                     + f" · {goals[0] if goals else 'no goal'} · {tally}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests**

Run: `... $PY -B -m pytest tests/test_workmap_view.py tests/test_workmap_store.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/workmap/view.py tests/test_workmap_view.py
git commit -m "feat(workmap): render map, node and list views within a budget"
```

### Task 5: The `workmap_update` and `workmap_view` tools

**Files:**
- Modify: `src/server.py` (imports near `:73`, `TOOL_HINTS` after `:626`, tools after
  `claim_lookup`, before `# --- MCP Prompts`)
- Modify: `tests/test_workspace_mismatch.py:411-419`, `:442-449`
- Modify: `docs/mcp-tools.md` (after `:99`), `docs/badges/mcp-api-docs.svg`,
  `flows/thread-close.md` (after the `delegation` row, `:610`)
- Test: `tests/test_workmap_tools.py`

**Interfaces:**
- Consumes: `WorkmapStore`, `WorkmapError` (Task 3); `render_map`, `render_node`,
  `render_list`, `DEFAULT_BUDGET` (Task 4).
- Produces: MCP tools `workmap_update(task_id, ops, expected_revision=None, workspace=None) -> str`
  and `workmap_view(task_id=None, node=None, since=None, detail=None, budget=None, workspace=None) -> str`,
  both returning JSON text.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workmap_tools.py`:

```python
"""The two work-map tools over the registered workspace."""
import json

import pytest

import src.server as server
from src.engine import config as engine_config

TASK = "task-1"


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    engine_config._reset_client_repo_root_cache()
    yield tmp_path
    engine_config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_the_tools_write_and_read_a_map(served):
    applied = json.loads(await server.workmap_update(task_id=TASK, ops=[
        {"op": "add_node", "id": "goal", "kind": "goal", "title": "Ship it"},
        {"op": "add_node", "id": "map-code", "title": "Map the code"}]))
    assert applied["status"] == "applied" and applied["revision"] == 3
    assert applied["workspace"]["root"] == str(served)
    viewed = json.loads(await server.workmap_view(task_id=TASK))
    assert viewed["mode"] == "map" and "map-code: Map the code" in viewed["view"]
    node = json.loads(await server.workmap_view(task_id=TASK, node="map-code"))
    assert node["mode"] == "node" and node["view"].startswith("node map-code")
    listed = json.loads(await server.workmap_view())
    assert listed["mode"] == "list" and listed["total"] == 1


@pytest.mark.asyncio
async def test_a_refused_operation_is_an_error_payload(served):
    payload = json.loads(await server.workmap_update(task_id=TASK, ops=[
        {"op": "add_node", "id": "x", "title": "X"},
        {"op": "update_node", "id": "x", "status": "validated"}]))
    assert payload == {"status": "error", "code": "status_reserved", "error": "ops[1]: only the server sets validated"}


@pytest.mark.asyncio
async def test_viewing_an_unknown_map_or_missing_result_is_reported(served):
    assert json.loads(await server.workmap_view(task_id=TASK))["code"] == "unknown_map"
    await server.workmap_update(task_id=TASK, ops=[{"op": "add_node", "id": "x", "title": "X"}])
    assert json.loads(await server.workmap_view(task_id=TASK, node="x", detail="result"))["code"] == "no_result"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `... $PY -B -m pytest tests/test_workmap_tools.py -q -p no:cacheprovider`
Expected: FAIL with `AttributeError: module 'src.server' has no attribute 'workmap_update'`.

- [ ] **Step 3: Implement the tools**

In `src/server.py`, after the `from src.claims.store import (...)` import (`:73-74`), add:

```python
from src.workmap import view as workmap_text
from src.workmap.store import WorkmapError, WorkmapStore
```

In `TOOL_HINTS`, after the `claim_lookup` entry, add:

```python
    # Appends events to the workspace's work map of one task; nothing is overwritten or deleted.
    "workmap_update": tool_hints("Update a work map", read_only=False, destructive=False, idempotent=False),
    "workmap_view": tool_hints("View a work map", read_only=True),
```

After the `claim_lookup` tool, before `# --- MCP Prompts`, add:

```python
async def _workmap_call(tool: str, ctx: Context | None, workspace: Optional[str], *, writing: bool,
                        work) -> str:
    """Run ``work(store)`` in the registered workspace's work-map store and answer as JSON."""
    try:
        client = await resolve_client_context(ctx, workspace)
        root = client.require_root()
        if writing:
            # A map of another repository must not land in this one's store (#576).
            client.require_workspace_match()
        result = await asyncio.get_running_loop().run_in_executor(None, work, WorkmapStore(root))
        result.update(_workspace_report(client, root))
        debug_log(tool, "res", {key: result[key] for key in ("status", "mode", "revision") if key in result})
        return to_json(result)
    except WorkmapError as error:
        payload = {"status": "error", "code": error.code, "error": str(error)}
        debug_log(tool, "error", payload)
        return to_json(payload)
    except WorkspaceError as error:
        payload = {"status": "error", "code": "workspace", "error": str(error),
                   **_workspace_error_details(workspace_inputs(ctx), error)}
        debug_log(tool, "error", payload)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as error:
        logger.error("%s failed: %s", tool, error, exc_info=True)
        payload = {"status": "error", "code": "internal", "error": str(error)}
        debug_log(tool, "error", payload)
        return json.dumps(payload, ensure_ascii=False)


@mcp.tool(**TOOL_HINTS["workmap_update"])
async def workmap_update(
    task_id: Annotated[str, Field(description="The map's task ID, the one its packages use.")],
    ops: ta.unparsed_arg("array", "1 to 50 operations applied together or not at all.",
                         items={"type": "object"}),
    expected_revision: ta.unparsed_arg("integer", "Optional revision the map must be at.") = None,
    ctx: Context | None = None,
    workspace: ta.opt_str(ta.WORKSPACE_DESC) = None,
) -> str:
    """Write the work map of one task: the parent's top-down view of staged, delegated work.

    Operations: add_node {id, kind (goal, stage, decision, question), title, summary?, risk?
    (routine, load_bearing, critical)}; update_node {id, title?, summary?, risk?, status?,
    reason?, level?}; add_edge and remove_edge {from, to, type (depends_on, verifies,
    supersedes, contradicts)}; close {}. The parent sets only planned, accepted, refuted,
    superseded and blocked (the last three with a reason), and level V4 only with accepted,
    when the user accepted the node. prepared, validated, needs_attention, verified and
    disputed come from get_subagent_context and validate_subagent_result and are refused here
    with status_reserved. A depends_on cycle is refused. The first call creates the map.

    The map is append-only in data/workmap/ of this workspace. A workspace argument outside
    the registered root is refused with workspace_mismatch, and nothing is written.

    Returns JSON: {status: applied, task_id, revision, changed, workspace}. Errors: {status:
    error, code (invalid_request, unknown_node, status_reserved, invalid_transition, cycle,
    revision_conflict, map_closed, key_collision, store_full, unsafe_store, workspace), error}.
    """
    debug_log("workmap_update", "req", {"ops": len(ops) if isinstance(ops, list) else None})
    return await _workmap_call("workmap_update", ctx, workspace, writing=True,
                               work=lambda store: store.apply(task_id, ops, expected_revision))


def _workmap_read(store: WorkmapStore, task_id, node, since, detail, budget) -> dict:
    if task_id is None:
        if node is not None or detail is not None:
            raise WorkmapError("invalid_request", "node and detail need task_id")
        maps = store.list_maps()
        return {"status": "ok", "mode": "list", "total": len(maps), "view": workmap_text.render_list(maps)}
    current = store.load(task_id)
    if current is None:
        raise WorkmapError("unknown_map", f"no work map for task {task_id}")
    if detail is not None:
        if detail != "result" or node is None:
            raise WorkmapError("invalid_request", 'detail is "result", together with node')
        text = store.result_text(task_id, node)
        if text is None:
            raise WorkmapError("no_result", f"node {node} has no stored result")
        return {"status": "ok", "mode": "result", "revision": current.revision, "node": node, "result": text}
    if node is not None:
        return {"status": "ok", "mode": "node", "revision": current.revision,
                "view": workmap_text.render_node(current, node)}
    for name, value in (("since", since), ("budget", budget)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise WorkmapError("invalid_request", f"{name} is an integer")
    return {"status": "ok", "mode": "map", "revision": current.revision,
            "view": workmap_text.render_map(current, budget=budget or workmap_text.DEFAULT_BUDGET, since=since)}


@mcp.tool(**TOOL_HINTS["workmap_view"])
async def workmap_view(
    task_id: ta.opt_str("Optional task ID of one map; without it, the workspace's maps, one line each.") = None,
    node: ta.opt_str("Optional node ID: that node's record instead of the map.") = None,
    since: ta.unparsed_arg("integer", "Optional revision: only the nodes changed after it.") = None,
    detail: ta.opt_str('Optional "result", with node: the stored result in full.') = None,
    budget: ta.unparsed_arg("integer", "Characters for the map, 500 to 8000; default 3000.") = None,
    ctx: Context | None = None,
    workspace: ta.opt_str(ta.WORKSPACE_DESC) = None,
) -> str:
    """Read work maps without their stage results: the parent's top-down view.

    - No task_id: the workspace's maps, one line each.
    - task_id: the map within budget characters, one line per node in dependency order,
      [status level] id: title (after ...) — summary, then the ready nodes and those that
      need the parent. Summaries go first when the budget runs short.
    - since: only the nodes changed after that revision; read the map this way after each
      worker's one-line hand-back.
    - node: that node's record: relations, references, check and finding counts.
    - node with detail="result": the node's stored result in full, the one call that brings
      a stage's work into your context.

    It reads the workspace this session is registered for; a workspace argument outside it
    is not followed, and workspace_note says so.

    Returns JSON: {status: ok, mode (list, map, node or result), revision?, view or result,
    workspace}. Errors: {status: error, code (invalid_request, unknown_map, unknown_node,
    no_result, unsafe_store, workspace), error}.
    """
    debug_log("workmap_view", "req", {"task": task_id is not None, "node": node is not None,
                                      "since": since, "detail": detail})
    return await _workmap_call("workmap_view", ctx, workspace, writing=False,
                               work=lambda store: _workmap_read(store, task_id, node, since, detail, budget))
```

If `Optional` is not already imported in `src/server.py`, use `str | None` in the
`_workmap_call` signature instead.

- [ ] **Step 4: Register the tools where the repository enumerates them**

1. `tests/test_workspace_mismatch.py`, after `CLAIM_TOOLS = ...` (`:413`):

```python
WORKMAP_WRITE_TOOLS = frozenset({"workmap_update"})
WORKMAP_READ_TOOLS = frozenset({"workmap_view"})
```

   add `| WORKMAP_WRITE_TOOLS | WORKMAP_READ_TOOLS` to `WORKSPACE_TOOLS`, and in
   `test_the_workspace_parameter_is_true_for_every_tool_that_shares_it` extend the two loops
   to `REPOSITORY_TOOLS | FLOW_TOOLS | CLAIM_WRITE_TOOLS | WORKMAP_WRITE_TOOLS` and
   `HISTORY_TOOLS | CLAIM_READ_TOOLS | SHARED_MEMORY_TOOLS | WORKMAP_READ_TOOLS`.
2. `docs/mcp-tools.md`, after the `validate_subagent_result` row (`:99`):

```markdown
| `workmap_update` | Write a task's work map: add nodes and edges, set the parent's own statuses, close it; up to 50 operations applied together, refused on a stale `expected_revision`; append-only, and server-owned statuses are refused | [Work map plan](workmap-plan.md) |
| `workmap_view` | Read a work map top-down within a character budget, only the nodes changed after a revision, one node's record, or a node's stored result; read-only | [Work map plan](workmap-plan.md) |
```

3. `flows/thread-close.md`, after the `delegation` row of the catalogue (`:610`):

```markdown
| `workmap` | `workmap_update`, `workmap_view` | staged work delegated to several workers whose results should stay out of the parent's context | stage results read in full by the parent without a stated reason; a node accepted without `validated` or `verified` |
```

4. Regenerate the badge: `$PY -B scripts/check_docs.py --update-badge`.

- [ ] **Step 5: Run the tests and checks**

Run: `... $PY -B -m pytest tests/test_workmap_tools.py tests/test_workmap_store.py tests/test_workmap_view.py tests/test_tool_annotations.py tests/test_workspace_mismatch.py tests/test_check_docs.py tests/test_flows.py tests/test_context_size_audit.py tests/test_claims.py -q -p no:cacheprovider`
Expected: PASS.
Run: `git add -A docs src tests flows && $PY -B scripts/check_docs.py`
Expected: `0 navigation/tool-index errors`.
Run: `$PY -B -c "import asyncio, src.server as s; print({t.name: len(t.description or '') for t in asyncio.run(s.mcp.list_tools()) if t.name.startswith('workmap')})"`
Expected: both lengths near 1,500 or below; shorten the docstrings if either exceeds 1,700.

- [ ] **Step 6: Commit and open PR 2**

```bash
git add src/server.py tests/test_workmap_tools.py tests/test_workspace_mismatch.py docs/mcp-tools.md docs/badges/mcp-api-docs.svg flows/thread-close.md
git commit -m "feat(workmap): add the workmap_update and workmap_view tools"
```

Update the `**Status:**` line of `docs/workmap-plan.md` to say that the store and the two
tools are implemented, in the same PR.

---

## PR 3

### Task 6: Bind a package to a node and pin its inputs

**Files:**
- Modify: `src/schemas/subagent.py:61-82` (`PreparationRequest`), `:129-159` (`TaskContract`)
- Modify: `src/engine/subagent.py:172-189` (`_assemble`), `:226-264` (`prepare_subagent`)
- Modify: `src/server.py:1388-1445` (`get_subagent_context`)
- Create: `src/workmap/binding.py`
- Test: `tests/test_workmap_binding.py`

**Interfaces:**
- Consumes: `WorkmapStore.check_preparable`, `record_prepared` (Task 3);
  `artifact_tools.resolve_service(ctx) -> (client, service, owned)`,
  `service.call(name, client, arguments) -> {"status": "ok", "data": ...} | {"status": "error", "error": {...}}`,
  `service.verify_snapshot(client, manifest) -> dict` (existing, `src/artifacts/tools.py:18`,
  `src/artifacts/service.py:145`, `:177`).
- Produces: `WorkmapNodeId`, `WorkmapBinding(node, root)` in `src/schemas/subagent.py`;
  `TaskContract.workmap: WorkmapBinding | None`; `prepare_subagent(arguments, *, workmap_root=None)`;
  `binding.prepare(ctx, task_id, node, inputs, artifact_manifest) -> tuple[str, dict | None]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workmap_binding.py`:

```python
"""Binding packages to work-map nodes, and the submission a worker makes itself."""
import json

import pytest

import src.server as server
from src.engine import config as engine_config
from src.workmap.store import WorkmapStore
from tests.test_subagent import request, worker_tree  # noqa: F401  (fixture)
from tests.test_subagent_artifacts import artifact_worker_tree  # noqa: F401  (fixture)
from tests.test_subagent_exchange import call

TASK = "task-1"


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    engine_config._reset_client_repo_root_cache()
    WorkmapStore(tmp_path).apply(TASK, [
        {"op": "add_node", "id": "goal", "kind": "goal", "title": "Ship it"},
        {"op": "add_node", "id": "map-code", "title": "Map the code"},
        {"op": "add_node", "id": "write", "title": "Write it"},
        {"op": "add_edge", "from": "write", "to": "map-code", "type": "depends_on"}])
    yield tmp_path
    engine_config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_a_bound_package_marks_its_node_prepared_and_binds_the_workspace(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    assert receipt["status"] == "prepared"
    node = WorkmapStore(served).load(TASK).nodes["map-code"]
    assert node["status"] == "prepared" and node["refs"]["contract_revision"] == receipt["contract_revision"]
    stored = json.loads((server.subagent_exchange.exchange_dir()
                         / f"{receipt['contract_revision']}.contract.json").read_text())
    assert stored["workmap"] == {"node": "map-code", "root": str(served)}


@pytest.mark.asyncio
async def test_a_goal_node_is_refused_before_any_package_is_written(worker_tree, served):
    refused = await call("get_subagent_context", request(workmap_node="goal"))
    assert refused["status"] == "error" and refused["errors"][0]["code"] == "node_not_preparable"


@pytest.mark.asyncio
async def test_workmap_inputs_need_a_node_and_a_settled_input(worker_tree, served):
    without_node = await call("get_subagent_context", request(workmap_inputs=["map-code"]))
    assert without_node["errors"][0]["code"] == "invalid_request"
    unsettled = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"]))
    assert unsettled["errors"][0]["code"] == "input_not_ready"


@pytest.mark.asyncio
async def test_a_settled_input_is_pinned_as_an_artifact(artifact_worker_tree, served):
    store = WorkmapStore(served)
    store.record_prepared(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", complexity=None)
    store.record_result(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", text="mapped code",
                        sha256="b" * 64, handback="line",
                        outcome={"status": "validated", "level": "V2", "summary": "Mapped",
                                 "checks": {"passed": 1, "failed": 0, "not_run": 0}, "findings": None,
                                 "limitations": 0})
    receipt = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"]))
    assert receipt["status"] == "prepared"
    package = open(receipt["context_path"], encoding="utf-8").read()
    assert "### Pinned artifacts" in package and "Stored result of work-map node map-code" in package
```

- [ ] **Step 2: Run them to verify they fail**

Run: `... $PY -B -m pytest tests/test_workmap_binding.py -q -p no:cacheprovider`
Expected: FAIL: `get_subagent_context` rejects the unknown argument `workmap_node`.

- [ ] **Step 3: Extend the schemas**

In `src/schemas/subagent.py`, after the `Revision = ...` line add:

```python
WorkmapNodeId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")]
```

Before `class TaskContract` add:

```python
class WorkmapBinding(StrictModel):
    """The node a contract's result is recorded on. The server sets root from the workspace it
    resolved at preparation; a caller never supplies it."""

    node: WorkmapNodeId
    root: Text
```

In `PreparationRequest`, after `client: Client | None = None`, add the fields and a validator:

```python
    workmap_node: WorkmapNodeId | None = None
    workmap_inputs: Annotated[list[WorkmapNodeId], Field(min_length=1, max_length=10)] | None = None

    @model_validator(mode="after")
    def workmap_needs_task_and_text(self) -> PreparationRequest:
        if self.workmap_node is not None or self.workmap_inputs is not None:
            if self.task_id is None:
                raise ValueError("workmap_node and workmap_inputs need task_id, the map's task ID")
            if self.result_format != "text":
                raise ValueError("a work-map package uses result_format text")
        return self
```

In `TaskContract`, after `execution: ContractExecution | None = None`, add
`workmap: WorkmapBinding | None = None`, and change the serializer's first line to:

```python
        data = _omit_none(handler(self), "artifact_manifest", "execution", "workmap")
```

- [ ] **Step 4: Bind in the engine**

In `src/engine/subagent.py`, change the `_assemble` signature to accept the binding:

```python
def _assemble(request: PreparationRequest, task_id: str, body: str, rules: list[tuple[str, str]],
              components: list[Component], schema: dict,
              execution: ExecutionPlan | None, workmap: dict | None = None
              ) -> tuple[str, TaskContract, WorkerDescriptor]:
```

and after `if execution is not None: contract_data["execution"] = execution.contract_part()` add:

```python
    if workmap is not None:
        contract_data["workmap"] = workmap
```

Change `prepare_subagent`:

```python
def prepare_subagent(arguments: dict, *, workmap_root: str | None = None) -> PreparedPackage | PreparationError:
```

and right after `request = PreparationRequest.model_validate(arguments)` add:

```python
        workmap = None
        if request.workmap_node is not None:
            if workmap_root is None:
                raise ContractError("workmap_unavailable", "workmap_node needs a workspace the server "
                                                           "resolved.", "$.workmap_node")
            workmap = {"node": request.workmap_node, "root": workmap_root}
```

Pass `workmap=workmap` to each of the three `_assemble(` calls in `prepare_subagent`
(`grep -n "_assemble(" src/engine/subagent.py` lists them at `:239`, `:250`, `:264`).

- [ ] **Step 5: Create the binding module**

Create `src/workmap/binding.py`:

```python
"""Ties prepared packages to work-map nodes and records what their workers submit."""
from __future__ import annotations

import asyncio

from src.artifacts import tools as artifact_tools
from src.artifacts.store import ArtifactError
from src.daemon.workspaces import resolve_client_context
from src.workmap.store import WorkmapError, WorkmapStore

TEXT_MEDIA_TYPE = "text/markdown"


async def prepare(ctx, task_id: str | None, node: str | None, inputs: list | None,
                  artifact_manifest: dict | None) -> tuple[str, dict | None]:
    """Check the node and its inputs; return the workspace root and the inputs' manifest."""
    if task_id is None:
        raise WorkmapError("invalid_request", "workmap_node and workmap_inputs need task_id, the map's task ID")
    if inputs is not None and node is None:
        raise WorkmapError("invalid_request", "workmap_inputs needs workmap_node")
    if inputs is not None and artifact_manifest is not None:
        raise WorkmapError("invalid_request", "workmap_inputs builds the artifact manifest; "
                                              "do not pass artifact_manifest too")
    if inputs is not None and not isinstance(inputs, list):
        raise WorkmapError("invalid_request", "workmap_inputs is a list of node IDs")
    client = await resolve_client_context(ctx, None)
    root = client.require_root()
    client.require_workspace_match()
    texts = await asyncio.to_thread(WorkmapStore(root).check_preparable, task_id, node, inputs or [])
    manifest = await _pin(ctx, task_id, texts) if texts else None
    return str(root), manifest


async def _pin(ctx, task_id: str, texts: list[tuple[str, str]]) -> dict:
    """Publish each input node's stored result and pin them in one manifest for the task."""
    client, service, owned = await artifact_tools.resolve_service(ctx)
    try:
        entries = []
        for node_id, text in texts:
            published = _data(await service.call("artifact_publish", client, {
                "content": text, "name": f"workmap-{node_id}.md", "media_type": TEXT_MEDIA_TYPE}))
            entries.append({"artifact_id": published["artifact_id"], "sha256": published["sha256"],
                            "required": True, "purpose": f"Stored result of work-map node {node_id}"})
        created = _data(await service.call("artifact_manifest_create", client,
                                           {"task_id": task_id, "entries": entries}))
        return await service.verify_snapshot(client, created)
    finally:
        if owned:
            await service.close()


def _data(answer: dict) -> dict:
    if answer.get("status") != "ok":
        error = answer.get("error") or {}
        raise ArtifactError(error.get("code", "artifact_unavailable"),
                            error.get("message", "Artifact storage is unavailable."))
    return answer["data"]
```

- [ ] **Step 6: Call it from `get_subagent_context`**

In `src/server.py`, add to the imports:

```python
from src.workmap import binding as workmap_binding
```

Add two arguments to `get_subagent_context` after `result_format`:

```python
    workmap_node: ta.unparsed_arg("string", "Optional work-map node this package serves; needs task_id, the map's task ID. The node becomes prepared, and the worker submits its result to the map.") = None,
    workmap_inputs: ta.unparsed_arg("array", "Optional 1..10 work-map nodes whose stored results the worker needs, pinned as artifacts by the server. Needs workmap_node; not with artifact_manifest.", items={"type": "string"}) = None,
```

Replace the block from `if artifact_manifest is not None:` through
`result = await asyncio.to_thread(prepare_subagent, arguments)` with:

```python
    workmap_root = None
    if workmap_node is not None or workmap_inputs is not None:
        try:
            workmap_root, inputs_manifest = await workmap_binding.prepare(
                ctx, task_id, workmap_node, workmap_inputs, artifact_manifest)
        except (WorkmapError, ArtifactError) as error:
            return PreparationResponse(PreparationError(errors=diagnostics(ContractError(
                error.code, getattr(error, "message", None) or str(error), "$.workmap_node"))))
        except WorkspaceError as error:
            return PreparationResponse(PreparationError(errors=diagnostics(ContractError(
                "workspace", str(error), "$.workmap_node"))))
        arguments["workmap_node"] = workmap_node
        if inputs_manifest is not None:
            arguments["artifact_manifest"] = inputs_manifest
    elif artifact_manifest is not None:
        try:
            arguments["artifact_manifest"] = await artifact_tools.verify_preparation(ctx, artifact_manifest)
        except ArtifactError as error:
            return PreparationResponse(PreparationError(errors=diagnostics(
                ContractError(error.code, error.message, "$.artifact_manifest"))))
    result = await asyncio.to_thread(prepare_subagent, arguments, workmap_root=workmap_root)
```

and, after the `try:` block that stores the receipt (the line
`receipt = await asyncio.to_thread(subagent_exchange.store, result)` and its `except`),
before the final `return`, add:

```python
    if workmap_root is not None:
        try:
            await asyncio.to_thread(WorkmapStore(workmap_root).record_prepared, task_id, workmap_node,
                                    contract_revision=result.contract_revision, attempt_id=result.attempt_id,
                                    complexity=complexity)
        except WorkmapError as error:
            return PreparationResponse(PreparationError(errors=diagnostics(ContractError(
                error.code, str(error), "$.workmap_node"))))
```

A bound package with `delivery="inline"` still records the node: the contract is stored
either way.

- [ ] **Step 7: Run the tests**

Run: `... $PY -B -m pytest tests/test_workmap_binding.py tests/test_subagent_exchange.py tests/test_subagent_mcp.py tests/test_subagent.py tests/test_subagent_artifacts.py tests/test_tool_annotations.py tests/test_context_size_audit.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/schemas/subagent.py src/engine/subagent.py src/server.py src/workmap/binding.py tests/test_workmap_binding.py
git commit -m "feat(workmap): bind a prepared package to a work-map node"
```

### Task 7: The worker submits its own result

**Files:**
- Modify: `src/engine/subagent_text.py` (`submission_rule`, `render_assignment`)
- Modify: `src/engine/subagent_exchange.py:62-84` (`launch_text`, `store`)
- Modify: `src/engine/subagent_validation.py` (new `parsed_report` after `assess_result`)
- Modify: `src/schemas/subagent.py:278-286` (`ValidationResponse`)
- Modify: `src/server.py:1452-1488` (`validate_subagent_result`)
- Modify: `src/workmap/binding.py`
- Modify: `docs/subagent-contract.md` (new section before `## Compact exchange`)
- Test: `tests/test_workmap_binding.py`

**Interfaces:**
- Consumes: `WorkmapStore.record_result`, `record_invalid` (Task 3); the binding of Task 6.
- Produces: `WorkmapReceipt` and `ValidationResponse.workmap`; `parsed_report(contract, result)`;
  `binding.outcome_of(report) -> dict`; `binding.record_submission(contract, result, response, *, handback) -> ValidationResponse`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workmap_binding.py`:

```python
from types import SimpleNamespace

from tests.test_subagent_exchange import text_result


def text_result_for(receipt):
    """A completed generic text result for a receipt: summary "Checked the log path.", one passed check."""
    package = SimpleNamespace(task_id=receipt["task_id"], attempt_id=receipt["attempt_id"],
                              contract_revision=receipt["contract_revision"],
                              contract=SimpleNamespace(result_type="generic"))
    return text_result(package)


@pytest.mark.asyncio
async def test_a_worker_submission_is_recorded_on_its_node(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    package_text = open(receipt["context_path"], encoding="utf-8").read()
    assert "submitted task-1/map-code" in package_text
    assert "Submit the result and reply exactly as it says." in receipt["launch"]
    result = text_result_for(receipt)
    validated = await call("validate_subagent_result", {"contract_revision": receipt["contract_revision"],
                                                        "result": result})
    assert validated["valid"] is True
    assert validated["workmap"] == {"task_id": TASK, "node": "map-code", "status": "validated",
                                    "level": "V2", "revision": 7, "duplicate": False}
    node = WorkmapStore(served).load(TASK).nodes["map-code"]
    assert node["summary"] == "Checked the log path." and node["handback"] == "line"
    assert WorkmapStore(served).result_text(TASK, "map-code") == result


@pytest.mark.asyncio
async def test_a_different_second_submission_is_refused(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    first = text_result_for(receipt)
    await call("validate_subagent_result", {"contract_revision": receipt["contract_revision"], "result": first})
    second = first.replace("Checked the log path.", "Checked another path.")
    refused = await call("validate_subagent_result", {"contract_revision": receipt["contract_revision"],
                                                      "result": second})
    assert refused["valid"] is False and refused["errors"][0]["code"] == "already_submitted"


@pytest.mark.asyncio
async def test_an_invalid_submission_is_counted(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    invalid = await call("validate_subagent_result", {"contract_revision": receipt["contract_revision"],
                                                      "result": "not a result"})
    assert invalid["valid"] is False and "workmap" not in invalid
    assert WorkmapStore(served).load(TASK).invalid == {receipt["contract_revision"]: 1}
```

`text_result` (`tests/test_subagent_exchange.py:25`) reads only `task_id`, `attempt_id`,
`contract_revision` and `contract.result_type` from its package, and its result carries one
passed check, so the node's level is `V2`. Move the two new imports to the top of the file.

- [ ] **Step 2: Run them to verify they fail**

Run: `... $PY -B -m pytest tests/test_workmap_binding.py -q -p no:cacheprovider -k "submission"`
Expected: FAIL: the package has no `submitted task-1/map-code` line.

- [ ] **Step 3: The bound submission rule and launch text**

In `src/engine/subagent_text.py`, replace `submission_rule` (Task 1) with:

```python
def submission_rule(contract: dict) -> str:
    """How the worker checks or submits its result against the stored contract."""
    call = (f'validate_subagent_result(contract_revision="{contract["contract_revision"]}", '
            "result=<your complete result text>)")
    binding = contract.get("workmap")
    if binding is None:
        return (f"Before you reply, if the Agents-Core tool validate_subagent_result is available to you, "
                f"call {call}, correct the result until it answers valid: true, and reply with exactly "
                "the text that passed.")
    return (f"This result is recorded on node {binding['node']} of the work map of task "
            f"{contract['task_id']}. Submit it with {call}. If it answers valid: false, correct the "
            "result as its errors say and submit again. When it answers valid: true, reply with exactly "
            f"one line: submitted {contract['task_id']}/{binding['node']} <the first 12 characters of "
            "result_sha256> <status>. If the tool is not available to you, reply with the full result instead.")
```

In `src/engine/subagent_exchange.py`, replace `launch_text` and its call in `store`:

```python
def launch_text(path: Path, chars: int, *, bound: bool = False) -> str:
    ending = ("Submit the result and reply exactly as it says." if bound
              else "Reply with only the result it specifies.")
    return (f"You are a fresh worker. Read the file {path} ({chars} characters) completely before doing "
            "anything else and follow it exactly: it is your whole assignment, role, rules and result "
            f"format. {ending}")
```

```python
        launch=launch_text(context_path, package.context_chars, bound=package.contract.workmap is not None),
```

- [ ] **Step 4: `parsed_report` and the response field**

In `src/engine/subagent_validation.py`, after `assess_result`, add:

```python
def parsed_report(contract: dict, result: Any):
    """The result model of an output that ``assess_result`` already found valid."""
    expected = TaskContract.model_validate(contract)
    if is_text_result(result):
        value, _errors, _sources = parse_result(result, expected)
        value = bounded_object(value, max_chars=MAX_RESULT_CHARS)
    else:
        value = bounded_object(result, max_chars=MAX_RESULT_CHARS, allow_json=True)
    return RESULT_MODELS[expected.result_type].model_validate(value)
```

In `src/schemas/subagent.py`, before `class ValidationResponse`, add:

```python
class WorkmapReceipt(StrictModel):
    task_id: Identifier
    node: WorkmapNodeId
    status: str
    level: str
    revision: int
    duplicate: bool = False
```

and in `ValidationResponse` add `workmap: WorkmapReceipt | None = None` after
`truncated`, with the serializer line changed to:

```python
        return _omit_none(handler(self), "escalation", "result_sha256", "truncated", "workmap")
```

- [ ] **Step 5: Record submissions in the binding**

Append to `src/workmap/binding.py`:

```python
from src.engine.subagent_validation import canonical_json, diagnostic, parsed_report
from src.schemas.subagent import ValidationResponse, WorkmapReceipt
from src.workmap.store import SUMMARY_MAX


def outcome_of(report) -> dict:
    """What a valid result tells the map: status, level, summary and counts, never the text."""
    checks = {"passed": 0, "failed": 0, "not_run": 0}
    for check in report.checks:
        checks[check.status] += 1
    if report.status == "completed" and checks["failed"] == 0:
        status, level = "validated", "V2" if checks["passed"] else "V1"
    else:
        status, level = "needs_attention", "V0"
    findings = None
    if hasattr(report.result, "findings"):
        findings = {}
        for finding in report.result.findings:
            findings[finding.severity] = findings.get(finding.severity, 0) + 1
    summary = " ".join(report.result.summary.split())
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_MAX - 1].rstrip() + "…"
    return {"status": status, "level": level, "summary": summary, "checks": checks, "findings": findings,
            "limitations": len(report.limitations)}


def record_submission(contract: dict, result, response: ValidationResponse, *, handback: str) -> ValidationResponse:
    """Record a submission against a contract the server stored; refusals come back as errors."""
    binding = contract["workmap"]
    store = WorkmapStore(binding["root"])
    try:
        if not response.valid:
            store.record_invalid(contract["task_id"], binding["node"], contract_revision=contract["contract_revision"],
                                 codes=[error.code for error in response.errors])
            return response
        text = result if isinstance(result, str) else canonical_json(result)
        receipt = store.record_result(contract["task_id"], binding["node"],
                                      contract_revision=contract["contract_revision"],
                                      attempt_id=contract["attempt_id"], text=text, sha256=response.result_sha256,
                                      outcome=outcome_of(parsed_report(contract, result)), handback=handback)
    except WorkmapError as error:
        if not response.valid:
            return response
        return response.model_copy(update={"valid": False, "errors": [
            diagnostic("$.contract_revision", error.code, str(error))]})
    return response.model_copy(update={"workmap": WorkmapReceipt(**receipt)})
```

Move the three new imports to the top of the module with the others.

- [ ] **Step 6: Call it from `validate_subagent_result`**

In `src/server.py`, replace the handler's last three lines

```python
    response, outcome = assess_result(contract, result)
    await asyncio.to_thread(subagent_ledger.record_validation, outcome)
    return response
```

with

```python
    response, outcome = assess_result(contract, result)
    await asyncio.to_thread(subagent_ledger.record_validation, outcome)
    # Only a contract the server stored may name a workspace to write to: a caller can
    # fingerprint an inline contract it wrote itself.
    if contract_revision is not None and isinstance(contract, dict) and contract.get("workmap"):
        response = await asyncio.to_thread(workmap_binding.record_submission, contract, result, response,
                                           handback="full" if result_ref is not None else "line")
    return response
```

`handback` is a heuristic: a parent that validates by `result_ref` had the full result
delivered to it, while a worker submits with `result`.

- [ ] **Step 7: Document the binding**

In `docs/subagent-contract.md`, before `## Compact exchange`, add:

```markdown
## Work-map binding

A package prepared with `workmap_node` (and `task_id`, the map's task ID) is bound to that
node of the [work map](workmap-plan.md). The server resolves the workspace, refuses a node
that cannot take a package, marks it `prepared` and writes `{node, root}` into the contract,
so the binding is fingerprinted. `workmap_inputs` names up to ten settled nodes whose stored
results the server publishes and pins as artifacts for the worker. The package tells the
worker to submit its result with `validate_subagent_result` and to hand back one line,
`submitted <task>/<node> <sha> <status>`. A valid submission is stored and recorded on the
node; the first one is final for the contract, and five invalid ones send the node to
`needs_attention`. Only a contract the server stored records anything: an inline contract
never does. The parent reads the node with `workmap_view`, not the result.
```

- [ ] **Step 8: Run the tests**

Run: `... $PY -B -m pytest tests/test_workmap_binding.py tests/test_workmap_store.py tests/test_subagent_exchange.py tests/test_subagent_mcp.py tests/test_subagent.py tests/test_subagent_contract_text.py -q -p no:cacheprovider`
Expected: PASS.
Run: `git add -A docs && $PY -B scripts/check_docs.py && $PY -B -m src.engine.subagent_contract_text --check`
Expected: no errors, no stale block.

- [ ] **Step 9: Commit and open PR 3**

```bash
git add src tests docs
git commit -m "feat(workmap): let a bound worker submit its result to the map"
```

Update the `**Status:**` line of `docs/workmap-plan.md` in the same PR.

---

## PR 4

### Task 8: Verifier verdicts

**Files:**
- Modify: `src/workmap/store.py` (`record_result`)
- Test: `tests/test_workmap_store.py`

**Interfaces:**
- Consumes: the `verifies` edges and `findings` counts of Task 3.
- Produces: `verdict` events: a verifier's validated result moves each `verifies` target that
  has a result to `verified` (level V3) or, with a `critical` or `high` finding, to `disputed`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workmap_store.py`:

```python
def verify(store, findings):
    store.apply(TASK, [{"op": "add_node", "id": "check", "title": "Check the map"},
                       {"op": "add_edge", "from": "check", "to": "map-code", "type": "verifies"}])
    store.record_prepared(TASK, "check", contract_revision=SECOND, attempt_id="1", complexity=None)
    return store.record_result(TASK, "check", contract_revision=SECOND, attempt_id="1", text="review",
                               sha256="e" * 64, handback="line",
                               outcome={**OUTCOME, "summary": "Reviewed", "findings": findings})


def test_a_clean_verifier_result_verifies_its_target(store):
    plan(store)
    prepare(store)
    submit(store)
    verify(store, {"low": 2})
    node = store.load(TASK).nodes["map-code"]
    assert node["status"] == "verified" and node["level"] == "V3"


def test_a_high_finding_disputes_the_target_and_staleness_follows(store):
    plan(store)
    prepare(store)
    submit(store)
    verify(store, {"high": 1})
    workmap = store.load(TASK)
    assert workmap.nodes["map-code"]["status"] == "disputed" and workmap.nodes["map-code"]["level"] == "V2"
    assert is_stale(workmap, workmap.nodes["write"])


def test_a_critical_node_readies_its_dependants_only_once_verified(store):
    plan(store)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "risk": "critical"}])
    prepare(store)
    submit(store)
    assert not is_ready(store.load(TASK), store.load(TASK).nodes["write"])
    verify(store, {})
    assert is_ready(store.load(TASK), store.load(TASK).nodes["write"])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `... $PY -B -m pytest tests/test_workmap_store.py -q -p no:cacheprovider -k "verif or critical"`
Expected: FAIL: the target stays `validated`.

- [ ] **Step 3: Implement**

In `WorkmapStore.record_result`, replace

```python
            revision = self._commit(handle, name, [event])
```

with

```python
            events = [event]
            if event["status"] == "validated":
                severe = sum((event["findings"] or {}).get(level, 0) for level in ("critical", "high"))
                for target in workmap.targets(node["id"], "verifies"):
                    if "result_sha256" not in workmap.nodes[target]["refs"]:
                        continue
                    events.append({"op": "verdict", "by": "server", "id": target, "verifier": node["id"],
                                   "rev": events[-1]["rev"] + 1, "result_sha256": sha256,
                                   "status": "disputed" if severe else "verified",
                                   "level": None if severe else "V3"})
            revision = self._commit(handle, name, events)
```

- [ ] **Step 4: Run the tests**

Run: `... $PY -B -m pytest tests/test_workmap_store.py tests/test_workmap_view.py tests/test_workmap_binding.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit and open PR 4**

```bash
git add src/workmap/store.py tests/test_workmap_store.py
git commit -m "feat(workmap): record verifier verdicts on their targets"
```

---

## PR 5

### Task 9: The `orchestrate` flow and the protocol pointer

**Files:**
- Create: `flows/orchestrate.md`
- Modify: `flows/README.md` (the table under `## Available flows`, `:20-43`)
- Modify: `src/engine/protocol/delegation.md:17` (end of the first paragraph of the `tool-prepare` variant)
- Modify: `docs/subagent-contract.md:119` (the paragraph on how much to delegate)
- Modify: `docs/workmap-plan.md` (`**Status:**`)
- Test: `tests/test_flows.py`, `tests/test_protocol_text.py`

- [ ] **Step 1: Write the flow**

Create `flows/orchestrate.md`:

```markdown
# Orchestrate staged work on a work map

Use this flow when a task splits into stages that workers can do, and the parent should
keep a top-down view instead of the workers' results. The parent holds the map; the work
stays with the workers, the server and, where the risk warrants one, a verifier.

## Inputs

- The user's task and its boundaries.
- A `task_id` for the map, reused by every package of the task.

## Procedure

1. **Write the map.** One `workmap_update` call: a `goal` node, one `stage` node per part
   that genuinely needs its own worker, a `risk` for each (`critical` where a wrong result
   would be costly or hard to notice), and `depends_on` edges. Decide the fan-out as the
   description of `get_subagent_context` says.
2. **Launch the ready stages.** For each node in the view's `ready:` list, call
   `get_subagent_context` with `task_id`, `workmap_node` and, when the stage builds on
   earlier ones, `workmap_inputs`; rate `complexity`. Launch the worker in the background
   with the receipt's `launch` text and `execution.spawn` arguments, and keep working.
3. **Read the map, not the result.** A bound worker hands back one line,
   `submitted <task>/<node> <sha> <status>`. Do not validate that line. Call
   `workmap_view(task_id, since=<the last revision you read>)` and trust the node's status.
   A worker that could not reach the server hands back its full result: validate it by
   `result_ref` as usual; the node is updated the same way.
4. **Handle what needs you.** For a node under `needs attention:` or one that stayed
   `prepared` after its worker finished, read `workmap_view(task_id, node=<id>)` and decide:
   a new attempt (new `attempt_id`, the escalation tier if validation gave one), a re-plan
   with `workmap_update`, or a question to the user.
5. **Verify by risk.** For a node that needs V3 (`needs V3` in its record), add a verifier
   node with a `verifies` edge to it and prepare a `review` package bound to the verifier
   with `workmap_inputs=[<target>]`. A clean review makes the target `verified`; a
   `critical` or `high` finding makes it `disputed`, which is your decision, not a
   refutation.
6. **Accept.** Mark nodes `accepted` with `workmap_update` when they meet the task's need;
   use level `V4` only when the user accepted the node. Record a fact that outlives the task
   with `claim_record`, citing the node's `contract_revision` and `result_sha256`.
7. **Report and close.** Report to the user from the view, then `close` the map.

## Rules

- Read a stored result (`workmap_view` with `detail="result"`) only for a reason you state
  to the user; it is the one call that brings a stage's work into your context.
- Report how many workers ran and what each was for.
- Never set a server-owned status; `workmap_update` refuses it.
```

- [ ] **Step 2: Catalogue it**

In `flows/README.md`, add a row to the table under `## Available flows`, after the
`Consistency audit` row:

```markdown
| [Orchestrate staged work](orchestrate.md) | Split a task into stages on a work map, delegate each stage to a prepared worker bound to its node, and keep only the map in the parent's context | A closed map whose nodes are validated, verified or accepted, and a report from its view |
```

- [ ] **Step 3: Point to it from the delegation tool description**

In `src/engine/protocol/delegation.md`, append this sentence to the end of the first
paragraph of the `tool-prepare` variant (after `Report how many workers ran and what each
was for.`):

```text
For staged work whose results should stay out of your context, keep a work map: the built-in orchestrate flow binds each package to a node with workmap_node, and you read workmap_view instead of the results.
```

In `docs/subagent-contract.md`, append to the paragraph that begins `The parent also
decides how much to delegate`:

```text
For staged work it can keep a [work map](workmap-plan.md) instead, following the built-in
`orchestrate` flow, so that only node summaries reach its context.
```

- [ ] **Step 4: Run the tests and checks**

Run: `... $PY -B -m pytest tests/test_flows.py tests/test_protocol_text.py tests/test_instruction_pointers.py tests/test_context_size_audit.py tests/test_check_docs.py -q -p no:cacheprovider`
Expected: PASS.
Run: `$PY -B -m src.engine.protocol_text --check && git add -A flows docs && $PY -B scripts/check_docs.py`
Expected: no stale surface, `0 navigation/tool-index errors`.

- [ ] **Step 5: Commit and open PR 5**

```bash
git add flows/orchestrate.md flows/README.md src/engine/protocol/delegation.md docs/subagent-contract.md docs/workmap-plan.md
git commit -m "feat(flows): add the orchestrate flow for work-map delegation"
```

---

## PR 6

### Task 10: Measure what reaches the parent

**Files:**
- Create: `evals/scripts/workmap_context.py`
- Test: `tests/test_workmap_context.py`

**Interfaces:**
- Produces: `handbacks(session_dir: Path) -> list[dict]` (one `{agent, chars, line}` per
  hand-back) and `summary(rows) -> dict` (`count`, `total_chars`, `median_chars`,
  `line_handbacks`); a CLI printing the summary as JSON.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workmap_context.py`:

```python
"""The hand-back measurement reads worker transcripts."""
import json

from evals.scripts.workmap_context import handbacks, summary


def transcript(path, *inputs):
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "SubagentHandback", "input": value}]}} for value in inputs]
    lines.insert(0, {"type": "user", "message": {"content": "start"}})
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_each_handback_is_counted_and_one_line_receipts_are_recognised(tmp_path):
    agents = tmp_path / "subagents"
    agents.mkdir()
    transcript(agents / "agent-a.jsonl", {"report": "task: t\n## summary\n" + "x" * 500})
    transcript(agents / "agent-b.jsonl", {"report": "submitted t/map-code 0123456789ab completed"})
    rows = handbacks(tmp_path)
    assert [row["agent"] for row in rows] == ["agent-a", "agent-b"]
    assert rows[1]["line"] is True and rows[0]["line"] is False
    result = summary(rows)
    assert result["count"] == 2 and result["line_handbacks"] == 1
    assert result["total_chars"] == rows[0]["chars"] + rows[1]["chars"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `... $PY -B -m pytest tests/test_workmap_context.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `evals/scripts/workmap_context.py`:

```python
"""How many characters of worker hand-backs reached the parent of one Claude Code session.

Usage: python -m evals.scripts.workmap_context <session directory>

The session directory is projects/<project>/<session>/ under the Claude Code configuration
directory; its subagents/agent-*.jsonl transcripts hold each worker's SubagentHandback call,
whose input is what the host delivers to the parent. A hand-back that is one line starting
with "submitted " is a work-map receipt.
"""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def handbacks(session_dir: Path) -> list[dict]:
    rows = []
    for path in sorted((Path(session_dir) / "subagents").glob("agent-*.jsonl")):
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            content = (record.get("message") or {}).get("content") if record.get("type") == "assistant" else None
            for item in content if isinstance(content, list) else []:
                if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == "SubagentHandback":
                    value = item.get("input")
                    texts = _strings(value)
                    rows.append({"agent": path.stem,
                                 "chars": len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
                                 "line": any("\n" not in text and text.startswith("submitted ") for text in texts)})
    return rows


def summary(rows: list[dict]) -> dict:
    sizes = [row["chars"] for row in rows]
    return {"count": len(rows), "total_chars": sum(sizes),
            "median_chars": statistics.median(sizes) if sizes else 0,
            "line_handbacks": sum(1 for row in rows if row["line"])}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    print(json.dumps(summary(handbacks(Path(argv[1]))), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run the test, then measure a real session**

Run: `... $PY -B -m pytest tests/test_workmap_context.py -q -p no:cacheprovider`
Expected: PASS.
Run it on the session that wrote this plan, which delegated without a map:
`$PY -B -m evals.scripts.workmap_context ~/.claude/projects/<project>/<session-id>`
Expected: a JSON summary with `line_handbacks: 0`. Record the numbers in the PR body as the
baseline; a run of the `orchestrate` flow on a four-to-six-stage task gives the comparison.

- [ ] **Step 5: Commit and open PR 6**

```bash
git add evals/scripts/workmap_context.py tests/test_workmap_context.py
git commit -m "feat(evals): measure the worker hand-backs that reach the parent"
```

## Spec coverage

| Spec section | Task |
|---|---|
| Problem 1, the hand-back is the whole result | 7, 9, 10 |
| Problem 2, corrections through the parent | 1, 7 |
| Problem 3, the parent assesses evidence | 8, 9 |
| Problem 4, state lives in the conversation | 3, 5 |
| Data model, statuses, derived `ready` and `stale` | 3, 4, 8 |
| Storage | 2, 3 |
| Tools: `workmap_update`, `workmap_view` with `since` and `detail` | 5 |
| Tools: `workmap_node`, `workmap_inputs` | 6 |
| Tools: recording in `validate_subagent_result`, `already_submitted`, the submission limit | 3, 7 |
| Worker submission and fallback | 7, 9 |
| Verification by risk | 3 (`required_level`), 8, 9 |
| The parent's loop | 9 |
| Cost: tool list, views, workers | 5 (description length), 4 (`since`) |
| Evaluation: store, view, binding, measurement | 3, 4, 6, 7, 8, 10 |
| Order of work 1–6 | PRs 1–6 |
