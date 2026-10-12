"""Protected and control paths of self-development (autodev), and the change check (#341).

The autodev broker (#342) calls this module before it pushes a session's commits
(docs/autodev-plan.md, section 5.3; reference: docs/autodev-guards.md).

* **Path lists.** ``.github/autodev-protected-paths`` and ``.github/autodev-control-paths`` are
  read from the pinned ``main`` commit (#337) with ``git cat-file blob <pinned>:<path>``, never
  from the working branch. A missing, non-regular or empty list file is an error, never an empty
  list. Their syntax is a subset of gitignore: one pattern per line, blank lines and ``#``
  comments skipped, trailing whitespace stripped. Syntax this module does not implement (a
  leading ``!``, a backslash, a ``[`` character class, leading whitespace, ``**`` inside a
  component) raises ``PathListError`` instead of being ignored, so a list never protects less
  than it reads.
* **Matching** follows gitignore, case-insensitively after Unicode normalization (``fold``: NFD,
  then case folding, as macOS APFS compares names): ``*`` and ``?`` never cross ``/``; ``**``
  spans any number of directories, and a leading ``**/`` also matches at the root; a pattern with
  a slash at its start or in its middle is anchored at the root, one without matches the basename
  at any depth; a pattern ending in ``/**`` also matches the directory itself; a path matches
  when any of its parent directories does. A trailing ``/`` is accepted and also matches a file
  of that name, which only widens protection.
* **Classes.** ``PathClasses.classify`` returns ``control``, ``protected`` or ``None``. Every
  control path counts as protected, and both list files are control paths whatever the lists
  say. ``autodev:next`` opens the protected paths, no label opens the control paths.
* **Change check.** ``check_changes`` compares every commit of ``base..head`` with its single
  parent (``git diff-tree -r --no-renames --raw -z``), so both sides of every change count:
  renames are a deletion plus an addition, deletions included. ``ChangeCheck.refusals`` lists
  each refused commit and path with its ``Reason``; the push is allowed only when it is empty.

Git runs without a shell, with a minimal environment and a clean configuration
(``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL`` at the null device, ``GIT_TERMINAL_PROMPT=0``,
no pager, no replace objects, literal pathspecs, ``-c core.quotePath=false``), ``-C`` the trusted
repository with discovery stopped above it, and ``--end-of-options`` before revision arguments.
Every revision is a 40-character lowercase hex SHA validated before any git call.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import re
import subprocess
import unicodedata

from src import executables

PROTECTED_LIST = ".github/autodev-protected-paths"
CONTROL_LIST = ".github/autodev-control-paths"
NEXT_LABEL = "autodev:next"
GIT_TIMEOUT = 60

_SHA = re.compile(r"[0-9a-f]{40}")
_RAW_HEADER = re.compile(r":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) ([ADMT])")
_ABSENT, _REGULAR, _EXECUTABLE, _SYMLINK, _GITLINK = "000000", "100644", "100755", "120000", "160000"
_KNOWN_MODES = frozenset((_ABSENT, _REGULAR, _EXECUTABLE, _SYMLINK, _GITLINK))


class PathsError(Exception):
    """Base of this module's errors; the broker treats every one as a refusal."""


class PathListError(PathsError):
    """A list file that is missing, not a regular file, empty or not understood."""


class GitError(PathsError):
    """A git command that failed or printed output this module does not understand."""


class PathClass(StrEnum):
    CONTROL = "control"
    PROTECTED = "protected"


class Reason(StrEnum):
    """Why the change check refuses a commit or a path."""

    NOT_DESCENDANT = "head is not a descendant of base"
    MERGE_COMMIT = "merge commit"
    ROOT_COMMIT = "commit without a parent"
    CONTROL_PATH = "control path"
    PROTECTED_PATH = "protected path without autodev:next"
    SYMLINK = "symbolic link"
    GITLINK = "gitlink"
    UNSUPPORTED_MODE = "unsupported file mode"
    CASE_COLLISION = "collides with another path after case folding"
    DOT_GIT = "path component .git"
    INVALID_PATH = "invalid path"


def fold(name: str) -> str:
    """``name`` as macOS compares it: canonical decomposition, then case folding (and decomposing
    again, as Unicode's canonical caseless match does)."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


@dataclass(frozen=True)
class Pattern:
    """One list line and the regular expression it compiles to (over folded paths)."""

    text: str
    line: int
    regex: re.Pattern[str]

    def matches(self, folded_path: str) -> bool:
        return self.regex.fullmatch(folded_path) is not None


def parse_patterns(text: str, *, source: str = "<patterns>") -> tuple[Pattern, ...]:
    """The patterns of a list file; ``PathListError`` names the first line it cannot take."""
    patterns = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        patterns.append(Pattern(line, number, _compile(line, f"{source}:{number}")))
    return tuple(patterns)


def _compile(line: str, where: str) -> re.Pattern[str]:
    if line[0].isspace():
        raise PathListError(f"{where}: leading whitespace is not supported")
    if line.startswith("!"):
        raise PathListError(f"{where}: a leading '!' (negation) is not supported")
    if "\\" in line:
        raise PathListError(f"{where}: a backslash (escape) is not supported")
    if "[" in line:
        raise PathListError(f"{where}: '[' (character class) is not supported")
    body = fold(line)
    anchored = body.startswith("/")
    body = body.removeprefix("/").removesuffix("/")
    components = body.split("/")
    if any(component in ("", ".", "..") for component in components):
        raise PathListError(f"{where}: empty, '.' or '..' path component")
    if any("**" in component and component != "**" for component in components):
        raise PathListError(f"{where}: '**' must be a whole path component")
    # Consecutive `**` components mean the same as one.
    components = [c for i, c in enumerate(components) if not (c == "**" and i and components[i - 1] == "**")]
    if components == ["**"]:
        return re.compile(r".*", re.DOTALL)
    anchored = anchored or len(components) > 1
    if components[-1] == "**":  # `dir/**`: the directory itself and everything below it
        components.pop()
    regex = "" if anchored else "(?:.*/)?"
    for index, component in enumerate(components):
        last = index == len(components) - 1
        if component == "**":  # not the last one here: zero or more directories
            regex += "(?:.*/)?"
        else:
            regex += _translate(component) + ("" if last else "/")
    # A path also matches when one of its parent directories does.
    return re.compile(regex + "(?:/.*)?", re.DOTALL)


def _translate(component: str) -> str:
    return "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in component)


# Both list files are control paths whatever the lists say: no label lets a session rewrite them.
_BUILTIN_CONTROL = parse_patterns(f"/{PROTECTED_LIST}\n/{CONTROL_LIST}\n", source="<built-in>")


@dataclass(frozen=True)
class PathClasses:
    """The protected and control patterns read from one pinned commit."""

    protected: tuple[Pattern, ...]
    control: tuple[Pattern, ...]

    @classmethod
    def from_text(cls, protected: str, control: str) -> PathClasses:
        lists = (parse_patterns(protected, source=PROTECTED_LIST), parse_patterns(control, source=CONTROL_LIST))
        for name, patterns in zip((PROTECTED_LIST, CONTROL_LIST), lists):
            if not patterns:
                raise PathListError(f"{name} has no patterns")
        return cls(*lists)

    def classify(self, path: str) -> PathClass | None:
        """``control``, ``protected`` or ``None`` for a repository-relative ``/``-separated path."""
        folded = fold(path)
        if any(pattern.matches(folded) for pattern in (*_BUILTIN_CONTROL, *self.control)):
            return PathClass.CONTROL
        if any(pattern.matches(folded) for pattern in self.protected):
            return PathClass.PROTECTED
        return None


def load_path_classes(repository: str | os.PathLike[str], pinned: str) -> PathClasses:
    """Both lists as they are at ``pinned`` in the trusted ``repository``."""
    repository, pinned = _repository(repository), _sha(pinned, "pinned")
    _require_commit(repository, pinned)
    return PathClasses.from_text(_read_list(repository, pinned, PROTECTED_LIST),
                                 _read_list(repository, pinned, CONTROL_LIST))


def _read_list(repository: Path, pinned: str, path: str) -> str:
    entry = _git(repository, "ls-tree", "-z", "--full-tree", "--end-of-options", pinned, "--", path)
    if not entry:
        raise PathListError(f"{path} does not exist at {pinned}")
    mode, kind = entry.split(b" ", 2)[:2]
    if kind != b"blob" or mode.decode("ascii", "replace") not in (_REGULAR, _EXECUTABLE):
        # A symbolic link's blob is its target, which would parse as a one-pattern list.
        raise PathListError(f"{path} at {pinned} is not a regular file")
    content = _git(repository, "cat-file", "blob", "--end-of-options", f"{pinned}:{path}")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PathListError(f"{path} at {pinned} is not UTF-8: {error}") from None


@dataclass(frozen=True)
class Refusal:
    commit: str
    path: str
    reason: Reason


@dataclass(frozen=True)
class ChangeCheck:
    """The verdict on ``base..head``: allowed only when nothing is refused."""

    base: str
    head: str
    commits: tuple[str, ...]
    refusals: tuple[Refusal, ...]

    @property
    def allowed(self) -> bool:
        return not self.refusals


def check_changes(repository: str | os.PathLike[str], base: str, head: str, labels: Iterable[str],
                  classes: PathClasses) -> ChangeCheck:
    """Check every commit of ``base..head`` in the trusted ``repository`` against ``classes``.

    ``labels`` are the issue's labels; only ``autodev:next`` matters, and it opens protected
    paths that are not control paths.
    """
    base, head = _sha(base, "base"), _sha(head, "head")
    if not isinstance(classes, PathClasses):
        raise TypeError("classes must be PathClasses, read with load_path_classes at the pinned commit")
    if isinstance(labels, str):
        raise TypeError("labels must be a collection of label names, not one string")
    labels = frozenset(labels)
    if not all(isinstance(label, str) for label in labels):
        raise TypeError("labels must be strings")
    repository = _repository(repository)
    _require_commit(repository, base)
    _require_commit(repository, head)
    if _git_test(repository, "merge-base", "--is-ancestor", "--end-of-options", base, head).returncode != 0:
        return ChangeCheck(base, head, (), (Refusal(head, "", Reason.NOT_DESCENDANT),))
    opened = NEXT_LABEL in labels
    commits, refusals = [], []
    for commit, parents in _commits(repository, base, head):
        commits.append(commit)
        if len(parents) != 1:
            refusals.append(Refusal(commit, "", Reason.MERGE_COMMIT if parents else Reason.ROOT_COMMIT))
            continue
        refusals.extend(_check_commit(repository, parents[0], commit, classes, opened))
    return ChangeCheck(base, head, tuple(commits), tuple(refusals))


def _check_commit(repository: Path, parent: str, commit: str, classes: PathClasses,
                  opened: bool) -> Iterator[Refusal]:
    names: dict[str, set[str]] | None = None
    for old_mode, new_mode, raw_path in _changes(repository, parent, commit):
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            yield Refusal(commit, raw_path.decode("utf-8", "backslashreplace"), Reason.INVALID_PATH)
            continue
        reasons = _path_reasons(path, (old_mode, new_mode), classes, opened)
        # Only a path the commit's tree holds can alias another one on disk; a deleted path
        # cannot, so a case-only rename is not a collision.
        if new_mode != _ABSENT:
            if names is None:
                names = _folded_names(repository, commit)
            if _collides(path, names):
                reasons.append(Reason.CASE_COLLISION)
        yield from (Refusal(commit, path, reason) for reason in reasons)


def _path_reasons(path: str, modes: tuple[str, str], classes: PathClasses, opened: bool) -> list[Reason]:
    """Every reason to refuse one changed path, from its name, both modes and its class."""
    reasons = []
    components = path.split("/")
    if any(component in ("", ".", "..") for component in components):
        reasons.append(Reason.INVALID_PATH)
    if any(_strip_ignorable(fold(component)) == ".git" for component in components):
        reasons.append(Reason.DOT_GIT)
    for mode in dict.fromkeys(modes):
        if mode == _SYMLINK:
            reasons.append(Reason.SYMLINK)
        elif mode == _GITLINK:
            reasons.append(Reason.GITLINK)
        elif mode not in _KNOWN_MODES:
            reasons.append(Reason.UNSUPPORTED_MODE)
    kind = classes.classify(path)
    if kind is PathClass.CONTROL:
        reasons.append(Reason.CONTROL_PATH)
    elif kind is PathClass.PROTECTED and not opened:
        reasons.append(Reason.PROTECTED_PATH)
    return reasons


def _strip_ignorable(name: str) -> str:
    """Without format characters (zero-width and similar), which HFS+ ignored in names."""
    return "".join(c for c in name if unicodedata.category(c) != "Cf")


def _collides(path: str, names: dict[str, set[str]]) -> bool:
    """Whether ``path`` or a directory above it folds like a different name in the tree."""
    components = path.split("/")
    for end in range(1, len(components) + 1):
        prefix = "/".join(components[:end])
        if names.get(fold(prefix), set()) - {prefix}:
            return True
    return False


def _folded_names(repository: Path, commit: str) -> dict[str, set[str]]:
    """Every path and directory of ``commit``'s tree by its folded form."""
    names: dict[str, set[str]] = {}
    listing = _git(repository, "ls-tree", "-r", "-t", "-z", "--full-tree", "--name-only", "--end-of-options", commit)
    for raw in filter(None, listing.split(b"\0")):
        name = raw.decode("utf-8", "surrogateescape")
        names.setdefault(fold(name), set()).add(name)
    return names


def _commits(repository: Path, base: str, head: str) -> list[tuple[str, tuple[str, ...]]]:
    """The commits of ``base..head``, parents first, each with its parents."""
    output = _git(repository, "rev-list", "--parents", "--topo-order", "--reverse", "--end-of-options",
                  f"{base}..{head}")
    commits = []
    for line in output.decode("ascii", "replace").splitlines():
        shas = line.split(" ")
        if not all(_SHA.fullmatch(sha) for sha in shas):
            raise GitError(f"unexpected rev-list output: {line[:200]!r}")
        commits.append((shas[0], tuple(shas[1:])))
    return commits


def _changes(repository: Path, parent: str, commit: str) -> Iterator[tuple[str, str, bytes]]:
    """``(old mode, new mode, path)`` for every path ``commit`` changes against ``parent``."""
    output = _git(repository, "diff-tree", "-r", "--no-renames", "--raw", "-z", "--no-commit-id",
                  "--ignore-submodules=none", "--end-of-options", parent, commit)
    fields = output.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise GitError(f"unexpected diff-tree output for {commit}")
    for header, path in zip(fields[::2], fields[1::2]):
        match = _RAW_HEADER.fullmatch(header.decode("ascii", "replace"))
        if match is None or not path:
            raise GitError(f"unexpected diff-tree record for {commit}: {header[:120]!r}")
        yield match.group(1), match.group(2), path


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"{name} must be a 40-character lowercase hex SHA")
    return value


def _repository(repository: str | os.PathLike[str]) -> Path:
    path = Path(repository).resolve()
    if not path.is_dir():
        raise PathsError(f"the trusted repository {path} is not a directory")
    return path


def _require_commit(repository: Path, sha: str) -> None:
    """``sha`` must name a commit object itself, not a tag that peels to one."""
    result = _git_test(repository, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^{{commit}}")
    if result.returncode != 0 or result.stdout.strip() != sha.encode("ascii"):
        raise GitError(f"{sha} is not a commit in {repository}")


def _environment(repository: Path) -> dict[str, str]:
    """Only what git needs; nothing inherited steers it (``GIT_DIR``, config, pager, prompts)."""
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    environment.update(LC_ALL="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat", GIT_OPTIONAL_LOCKS="0",
                       GIT_NO_REPLACE_OBJECTS="1", GIT_CEILING_DIRECTORIES=str(repository.parent))
    return environment


def _run(repository: Path, args: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    try:  # the lookup is inside: a missing git is a GitError like one that cannot start
        command = [executables.path("git"), "--no-pager", "--no-replace-objects", "--literal-pathspecs",
                   "-c", "core.quotePath=false", "-C", str(repository), *args]
        return subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT,
                              env=_environment(repository), check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitError(f"git {args[0]} could not run: {error}") from None


def _failure(args: tuple[str, ...], result: subprocess.CompletedProcess[bytes]) -> GitError:
    stderr = result.stderr.decode("utf-8", "replace").strip()[:500]
    return GitError(f"git {args[0]} failed with exit code {result.returncode}: {stderr}")


def _git(repository: Path, *args: str) -> bytes:
    """The standard output of ``git <args>`` in ``repository``; ``GitError`` unless it succeeds."""
    result = _run(repository, args)
    if result.returncode != 0:
        raise _failure(args, result)
    return result.stdout


def _git_test(repository: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """A yes-or-no git command, which exits with 0 or 1; ``GitError`` for anything else."""
    result = _run(repository, args)
    if result.returncode not in (0, 1):
        raise _failure(args, result)
    return result
