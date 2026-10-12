"""Protected and control paths, and the change check of self-development (#341).

Change-check tests build throwaway bare repositories under ``tmp_path`` with git plumbing
(``hash-object``, ``mktree``, ``commit-tree``), so trees can hold what a case-insensitive file
system or ``update-index`` would not: case collisions, ``.git`` components, symbolic links and
gitlinks. The live checkout is only read: the two list files' current contents.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from src.schedules import paths
from src.schedules.paths import (CONTROL_LIST, NEXT_LABEL, PROTECTED_LIST, ChangeCheck, GitError, PathClass,
                                 PathClasses, PathListError, Reason, Refusal, check_changes, fold,
                                 load_path_classes, parse_patterns)

ROOT = Path(__file__).resolve().parents[1]
PROTECTED_TEXT = (ROOT / PROTECTED_LIST).read_text(encoding="utf-8")
CONTROL_TEXT = (ROOT / CONTROL_LIST).read_text(encoding="utf-8")
CURRENT = PathClasses.from_text(PROTECTED_TEXT, CONTROL_TEXT)


def fixture_git(repository: Path, *args: str, input: str | None = None) -> str:
    """git for fixtures, isolated from the developer's configuration and inherited GIT_* variables."""
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                       GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com")
    return subprocess.run(["git", "-C", str(repository), *args], env=environment, capture_output=True,
                          input=input, text=True, encoding="utf-8", check=True).stdout.strip()


class Repo:
    """A bare repository whose commits are built from ``{path: content or (mode, object)}``."""

    def __init__(self, path: Path):
        self.path = path
        fixture_git(path.parent, "init", "--quiet", "--bare", path.name)

    def blob(self, content: str) -> str:
        return fixture_git(self.path, "hash-object", "-w", "--stdin", input=content)

    def tree(self, entries: dict[str, tuple[str, str]]) -> str:
        lines, children = [], {}
        for path, (mode, sha) in entries.items():
            name, _, rest = path.partition("/")
            if rest:
                children.setdefault(name, {})[rest] = (mode, sha)
            else:
                lines.append(f"{mode} {'commit' if mode == '160000' else 'blob'} {sha}\t{name}")
        lines += [f"040000 tree {self.tree(child)}\t{name}" for name, child in children.items()]
        return fixture_git(self.path, "mktree", "--missing", input="".join(f"{line}\n" for line in lines))

    def commit(self, files: dict[str, str | tuple[str, str]], *parents: str) -> str:
        entries = {path: value if isinstance(value, tuple) else ("100644", self.blob(value))
                   for path, value in files.items()}
        arguments = [argument for parent in parents for argument in ("-p", parent)]
        return fixture_git(self.path, "commit-tree", self.tree(entries), *arguments, "-m", "change")


BASE_FILES = {
    PROTECTED_LIST: PROTECTED_TEXT,
    CONTROL_LIST: CONTROL_TEXT,
    "CLAUDE.md": "instructions\n",
    "pyproject.toml": "[project]\n",
    "src/server.py": "print('server')\n",
    "docs/README.md": "# Docs\n",
    "flows/autodev-implement.md": "# Implement\n",
}


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "trusted.git")


@pytest.fixture
def base(repo):
    return repo.commit(BASE_FILES)


def check(repo: Repo, base: str, head: str, labels=()) -> ChangeCheck:
    return check_changes(repo.path, base, head, labels, load_path_classes(repo.path, base))


def reasons(result: ChangeCheck) -> set[tuple[str, Reason]]:
    return {(refusal.path, refusal.reason) for refusal in result.refusals}


# --- list syntax ---------------------------------------------------------------------------

def test_parse_skips_blank_lines_and_comments_and_strips_trailing_whitespace():
    patterns = parse_patterns("# a comment\n\n   \nfoo/**  \t\r\n# another\nbar\n")

    assert [(pattern.text, pattern.line) for pattern in patterns] == [("foo/**", 4), ("bar", 6)]


@pytest.mark.parametrize("line", ["!CLAUDE.md", "foo\\ bar", "\\#literal", "[Cc]laude.md", "src/[a-z]*.py",
                                  " leading.txt", "a**b", "src/**.py", "a//b", "/", "a/../b", "./a"])
def test_parse_refuses_unsupported_syntax_instead_of_ignoring_it(line):
    with pytest.raises(PathListError, match=r"<patterns>:2"):
        parse_patterns(f"ok\n{line}\n")


def test_current_lists_parse_and_every_control_pattern_is_also_protected():
    assert CURRENT.protected and CURRENT.control
    for pattern in CURRENT.control:
        sample = fold(pattern.text.replace("/**", "/x").replace("*", "x"))
        assert pattern.matches(sample), pattern.text
        assert any(protected.matches(sample) for protected in CURRENT.protected), pattern.text


def test_lists_without_patterns_are_refused():
    with pytest.raises(PathListError, match="has no patterns"):
        PathClasses.from_text("# only a comment\n", CONTROL_TEXT)
    with pytest.raises(PathListError, match="has no patterns"):
        PathClasses.from_text(PROTECTED_TEXT, "\n")


# --- matching ------------------------------------------------------------------------------

@pytest.mark.parametrize("pattern,path,expected", [
    # `*` and `?` stay within one component.
    ("docs/*.md", "docs/a.md", True),
    ("docs/*.md", "docs/sub/a.md", False),
    ("a?c", "abc", True),
    ("a?c", "a/c", False),
    ("src/*/x.py", "src/engine/x.py", True),
    ("src/*/x.py", "src/a/b/x.py", False),
    # `**` spans any number of directories, including none.
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/x/y/b", True),
    ("a/**/b", "x/a/b", False),
    ("**/CLAUDE.md", "CLAUDE.md", True),
    ("**/CLAUDE.md", "deep/er/CLAUDE.md", True),
    ("**/CLAUDE.md", "CLAUDE.md.bak", False),
    # A slash at the start or in the middle anchors the pattern at the root.
    ("/foo", "foo", True),
    ("/foo", "x/foo", False),
    ("docs/cloud-runs.md", "docs/cloud-runs.md", True),
    ("docs/cloud-runs.md", "x/docs/cloud-runs.md", False),
    # Without a slash the basename matches at any depth.
    ("pyproject.toml", "pyproject.toml", True),
    ("pyproject.toml", "pkg/sub/pyproject.toml", True),
    (".envrc", "a/.envrc", True),
    ("requirements*.txt", "requirements-dev.txt", True),
    ("requirements*.txt", "x/requirements.txt", True),
    ("requirements*.txt", "requirements/x.txt", False),
    # `/**` also matches the directory itself; parents make everything below match.
    (".github/**", ".github", True),
    (".github/**", ".github/workflows/x.yml", True),
    (".github/**", "x/.github/y", False),
    ("flows/autodev*", "flows/autodev-implement.md", True),
    ("flows/autodev*", "flows/autodev/deeper/file.md", True),
    ("flows/autodev*", "flows/issue-agent.md", False),
    ("src/executables.py", "src/executables.py/odd", True),
    ("build/", "x/build/out.bin", True),
    ("**", "anything/at/all", True),
    # Case-insensitive after normalization, as APFS compares names.
    ("**/CLAUDE.md", "sub/claude.MD", True),
    ("café.md", "CAFÉ.md", True),
    ("café.md", "Café.md", True),
    ("src/engine/**", "SRC/Engine/x.py", True),
])
def test_matching_follows_gitignore_case_insensitively(pattern, path, expected):
    classes = PathClasses.from_text(f"{pattern}\n", "never-matches-anything\n")

    assert (classes.classify(path) is not None) is expected


def test_path_with_newline_is_still_matched():
    assert CURRENT.classify(".github/work\nflows/x.yml") is PathClass.CONTROL


@pytest.mark.parametrize("path", [
    "CLAUDE.md", "nested/dir/CLAUDE.md", "AGENTS.md", "src/AGENTS.md", "GEMINI.md", "a/b/GEMINI.md",
    ".claude/settings.json", ".claude", "pkg/.claude/commands/x.md",
    "claude.md", "Nested/Claude.MD", "agents.MD", "gemini.md", ".CLAUDE/settings.json", "pkg/.Claude/x",
    "pyproject.toml", "docs/cloud-runs.md", "Docs/Cloud-Runs.md", "uv.lock", ".mcp.json", "install.sh",
])
def test_protected_paths_with_the_current_lists(path):
    assert CURRENT.classify(path) in (PathClass.PROTECTED, PathClass.CONTROL)


@pytest.mark.parametrize("path", ["pyproject.toml", "docs/cloud-runs.md", "CLAUDE.md", ".claude/x.json",
                                  "src/executables.py", "scripts/run_tests.sh"])
def test_protected_but_not_control_paths(path):
    assert CURRENT.classify(path) is PathClass.PROTECTED


@pytest.mark.parametrize("path", [
    "flows/autodev-implement.md", ".github/workflows/x.yml", "src/engine/x.py", "src/schedules/x.py",
    PROTECTED_LIST, CONTROL_LIST, ".GitHub/Autodev-Control-Paths", "agents/x/system_prompt.mdc",
    "rules/x.mdc", "skills/x.mdc", "implants/x.mdc",
])
def test_control_paths_with_the_current_lists(path):
    assert CURRENT.classify(path) is PathClass.CONTROL


@pytest.mark.parametrize("path", ["src/server.py", "docs/README.md", "src/enginex.py", "README.md", "tests/x.py"])
def test_unprotected_paths_with_the_current_lists(path):
    assert CURRENT.classify(path) is None


def test_list_files_are_control_paths_whatever_the_lists_say():
    classes = PathClasses.from_text("unrelated.txt\n", "other.txt\n")

    assert classes.classify(PROTECTED_LIST) is PathClass.CONTROL
    assert classes.classify(CONTROL_LIST.upper()) is PathClass.CONTROL


# --- reading the lists at the pinned commit ------------------------------------------------

def test_lists_are_read_from_the_pinned_commit(repo, base):
    later = repo.commit({**BASE_FILES, PROTECTED_LIST: "only-this\n", CONTROL_LIST: "only-that\n"}, base)

    assert load_path_classes(repo.path, base).classify("pyproject.toml") is PathClass.PROTECTED
    assert load_path_classes(repo.path, later).classify("pyproject.toml") is None


@pytest.mark.parametrize("missing", [PROTECTED_LIST, CONTROL_LIST])
def test_missing_list_file_is_an_error_not_an_empty_list(repo, missing):
    pinned = repo.commit({path: text for path, text in BASE_FILES.items() if path != missing})

    with pytest.raises(PathListError, match="does not exist"):
        load_path_classes(repo.path, pinned)


def test_list_file_that_is_a_symbolic_link_is_refused(repo):
    pinned = repo.commit({**BASE_FILES, CONTROL_LIST: ("120000", repo.blob("autodev-protected-paths"))})

    with pytest.raises(PathListError, match="not a regular file"):
        load_path_classes(repo.path, pinned)


def test_list_file_with_unsupported_syntax_at_pinned_is_refused(repo):
    pinned = repo.commit({**BASE_FILES, PROTECTED_LIST: PROTECTED_TEXT + "!pyproject.toml\n"})

    with pytest.raises(PathListError, match="negation"):
        load_path_classes(repo.path, pinned)


def test_pinned_that_is_not_a_commit_in_the_repository_is_an_error(repo, base):
    with pytest.raises(GitError, match="is not a commit"):
        load_path_classes(repo.path, "0" * 40)


def test_git_does_not_discover_an_enclosing_repository(tmp_path):
    outer = Repo(tmp_path / "outer")
    pinned = outer.commit(BASE_FILES)
    inner = outer.path / "not-a-repository"
    inner.mkdir()

    with pytest.raises(GitError):
        load_path_classes(inner, pinned)


# --- the change check ----------------------------------------------------------------------

def test_unprotected_change_is_allowed(repo, base):
    head = repo.commit({**BASE_FILES, "src/server.py": "print('changed')\n", "docs/new.md": "new\n"}, base)

    result = check(repo, base, head)

    assert result.allowed and result.refusals == ()
    assert result.commits == (head,)


@pytest.mark.parametrize("path", ["CLAUDE.md", "deep/CLAUDE.md", "AGENTS.md", "x/AGENTS.md", "GEMINI.md",
                                  "y/GEMINI.md", ".claude/settings.json", "pkg/.claude/settings.json",
                                  "Pkg/.CLAUDE/Settings.json", "Agents.md"])
def test_protected_instruction_files_are_refused_without_the_label(repo, base, path):
    head = repo.commit({**BASE_FILES, path: "changed\n"}, base)

    result = check(repo, base, head)

    assert not result.allowed
    assert (path, Reason.PROTECTED_PATH) in reasons(result)


def test_next_label_opens_pyproject(repo, base):
    head = repo.commit({**BASE_FILES, "pyproject.toml": "[project]\nname = 'x'\n"}, base)

    assert reasons(check(repo, base, head)) == {("pyproject.toml", Reason.PROTECTED_PATH)}
    assert check(repo, base, head, labels=["autodev", NEXT_LABEL]).allowed


@pytest.mark.parametrize("path", ["flows/autodev-implement.md", ".github/workflows/x.yml", "src/engine/x.py",
                                  PROTECTED_LIST, CONTROL_LIST])
def test_next_label_does_not_open_control_paths(repo, base, path):
    head = repo.commit({**BASE_FILES, path: "changed\n"}, base)

    result = check(repo, base, head, labels=[NEXT_LABEL])

    assert reasons(result) == {(path, Reason.CONTROL_PATH)}


def test_renaming_a_protected_file_to_an_unprotected_path_is_refused(repo, base):
    files = {path: text for path, text in BASE_FILES.items() if path != "CLAUDE.md"}
    head = repo.commit({**files, "notes/instructions.txt": BASE_FILES["CLAUDE.md"]}, base)

    assert reasons(check(repo, base, head)) == {("CLAUDE.md", Reason.PROTECTED_PATH)}


def test_deleting_a_protected_file_is_refused(repo, base):
    head = repo.commit({path: text for path, text in BASE_FILES.items() if path != "pyproject.toml"}, base)

    assert reasons(check(repo, base, head)) == {("pyproject.toml", Reason.PROTECTED_PATH)}


def test_deleting_a_control_file_is_refused_with_the_label(repo, base):
    head = repo.commit({path: text for path, text in BASE_FILES.items() if path != CONTROL_LIST}, base)

    assert reasons(check(repo, base, head, labels=[NEXT_LABEL])) == {(CONTROL_LIST, Reason.CONTROL_PATH)}


@pytest.mark.parametrize("added", ["SRC/server.py", "src/Server.py", "Docs/new.md", "docs/readme.md"])
def test_case_collision_is_refused(repo, base, added):
    head = repo.commit({**BASE_FILES, added: "shadow\n"}, base)

    assert (added, Reason.CASE_COLLISION) in reasons(check(repo, base, head))


def test_unicode_normalization_collision_is_refused(repo):
    base = repo.commit({**BASE_FILES, "docs/café.md": "nfc\n"})
    head = repo.commit({**BASE_FILES, "docs/café.md": "nfc\n", "docs/café.md": "nfd\n"}, base)

    assert ("docs/café.md", Reason.CASE_COLLISION) in reasons(check(repo, base, head))


def test_case_only_rename_is_not_a_collision(repo, base):
    files = {path: text for path, text in BASE_FILES.items() if path != "docs/README.md"}
    head = repo.commit({**files, "docs/readme.md": BASE_FILES["docs/README.md"]}, base)

    assert check(repo, base, head).allowed


def test_symbolic_link_is_refused(repo, base):
    head = repo.commit({**BASE_FILES, "src/link": ("120000", repo.blob("../../outside"))}, base)

    assert reasons(check(repo, base, head)) == {("src/link", Reason.SYMLINK)}


def test_replacing_a_symbolic_link_with_a_file_is_refused(repo):
    base = repo.commit({**BASE_FILES, "src/link": ("120000", repo.blob("server.py"))})
    head = repo.commit({**BASE_FILES, "src/link": "now a file\n"}, base)

    assert reasons(check(repo, base, head)) == {("src/link", Reason.SYMLINK)}


def test_gitlink_is_refused(repo, base):
    head = repo.commit({**BASE_FILES, "vendor/module": ("160000", base)}, base)

    assert reasons(check(repo, base, head)) == {("vendor/module", Reason.GITLINK)}


@pytest.mark.parametrize("path", [".git/config", "sub/.GIT/hooks/post-checkout", "x/.Git"])
def test_dot_git_component_is_refused(repo, base, path):
    head = repo.commit({**BASE_FILES, path: "payload\n"}, base)

    assert (path, Reason.DOT_GIT) in reasons(check(repo, base, head))


def test_every_commit_is_checked_not_only_the_head(repo, base):
    middle = repo.commit({**BASE_FILES, "CLAUDE.md": "rewritten\n"}, base)
    head = repo.commit(BASE_FILES, middle)

    result = check(repo, base, head)

    assert result.commits == (middle, head)
    refused = {(refusal.commit, refusal.path) for refusal in result.refusals}
    assert refused == {(middle, "CLAUDE.md"), (head, "CLAUDE.md")}


def test_merge_commit_is_refused(repo, base):
    side = repo.commit({**BASE_FILES, "docs/side.md": "side\n"}, base)
    main = repo.commit({**BASE_FILES, "docs/main.md": "main\n"}, base)
    merge = repo.commit({**BASE_FILES, "docs/side.md": "side\n", "docs/main.md": "main\n"}, main, side)

    result = check(repo, base, merge)

    assert Refusal(merge, "", Reason.MERGE_COMMIT) in result.refusals
    assert not result.allowed


def test_head_that_is_not_a_descendant_of_base_is_refused(repo, base):
    unrelated = repo.commit({**BASE_FILES, "docs/x.md": "x\n"})

    result = check(repo, base, unrelated)

    assert result.refusals == (Refusal(unrelated, "", Reason.NOT_DESCENDANT),)
    assert check(repo, repo.commit(BASE_FILES, base), base).refusals[0].reason is Reason.NOT_DESCENDANT


@pytest.mark.parametrize("bad", ["HEAD", "main", "A" * 40, "a" * 39, "a" * 41, f"{'a' * 40}\n", "--all", "a" * 64])
def test_revisions_are_validated_before_any_git_call(repo, base, monkeypatch, bad):
    def no_git(*args, **kwargs):
        raise AssertionError("git must not run")
    monkeypatch.setattr(paths, "_run", no_git)

    with pytest.raises(ValueError, match="40-character lowercase hex SHA"):
        check_changes(repo.path, bad, base, [], CURRENT)
    with pytest.raises(ValueError, match="40-character lowercase hex SHA"):
        check_changes(repo.path, base, bad, [], CURRENT)
    with pytest.raises(ValueError, match="40-character lowercase hex SHA"):
        load_path_classes(repo.path, bad)


def test_labels_must_be_a_collection_of_names_and_classes_must_be_given(repo, base):
    with pytest.raises(TypeError):
        check_changes(repo.path, base, base, NEXT_LABEL, CURRENT)
    with pytest.raises(TypeError):
        check_changes(repo.path, base, base, [], None)


def test_annotated_tag_is_not_taken_for_the_commit_it_points_to(repo, base):
    tag = fixture_git(repo.path, "mktag", input=f"object {base}\ntype commit\ntag v1\n"
                      "tagger Fixture <fixture@example.com> 1700000000 +0000\n\nrelease\n")
    head = repo.commit({**BASE_FILES, "docs/x.md": "x\n"}, base)

    with pytest.raises(GitError, match="is not a commit"):
        check_changes(repo.path, tag, head, [], CURRENT)


def test_empty_range_is_allowed(repo, base):
    result = check_changes(repo.path, base, base, [], CURRENT)

    assert result.allowed and result.commits == ()


def test_git_runs_with_a_clean_configuration_and_end_of_options(repo, base, monkeypatch):
    head = repo.commit({**BASE_FILES, "src/server.py": "changed\n"}, base)
    calls = []
    real_run = subprocess.run

    def recording_run(command, **kwargs):
        calls.append((command, kwargs))
        return real_run(command, **kwargs)
    monkeypatch.setattr(paths.subprocess, "run", recording_run)
    monkeypatch.setenv("GIT_DIR", str(repo.path.parent / "elsewhere"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.pager'='evil'")

    assert check_changes(repo.path, base, head, [], load_path_classes(repo.path, base)).allowed

    assert calls
    for command, kwargs in calls:
        assert command[1:8] == ["--no-pager", "--no-replace-objects", "--literal-pathspecs", "-c",
                                "core.quotePath=false", "-C", str(repo.path.resolve())]
        assert "--end-of-options" in command
        assert kwargs.get("shell") in (None, False)
        environment = kwargs["env"]
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_DIR" not in environment and "GIT_CONFIG_PARAMETERS" not in environment
    commands = [command[8] for command, _ in calls]
    assert {"cat-file", "ls-tree", "rev-parse", "merge-base", "rev-list", "diff-tree"} <= set(commands)
    diff_tree = next(command for command, _ in calls if command[8] == "diff-tree")
    assert {"-r", "--no-renames", "--raw", "-z"} <= set(diff_tree)
    cat_file = next(command for command, _ in calls if command[8] == "cat-file")
    assert cat_file[9:] == ["blob", "--end-of-options", f"{base}:{PROTECTED_LIST}"]
