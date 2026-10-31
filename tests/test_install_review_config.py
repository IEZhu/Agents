"""Verify stack detection, composed review configuration and managed-section writes."""
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

INSTALLER = Path(__file__).resolve().parents[1] / "scripts/install_review_config.py"
spec = importlib.util.spec_from_file_location("install_review_config", INSTALLER)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

# CodeRabbit's configuration schema v2 caps one instruction at this length.
INSTRUCTION_LIMIT = 20000

# FIFOs, mode bits and shell hooks have no Windows equivalent. Hard links do, so
# those cases stay enabled there.
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32",
                                reason="POSIX-only: FIFOs, mode bits and shell hooks")


def make_symlink(link: Path, target: Path, **kwargs) -> None:
    """Create a symlink, or skip where the platform withholds the privilege.

    Windows can create symlinks, but only with the create-symbolic-link right or
    Developer Mode, so a capability check belongs here rather than a blanket skip
    that would stop these cases from ever running there. Only that one error is
    hidden: skipping on any OSError would turn a broken fixture into a pass.
    """
    try:
        link.symlink_to(target, **kwargs)
    except OSError as error:
        # 1314 is ERROR_PRIVILEGE_NOT_HELD, which Windows raises without
        # SeCreateSymbolicLinkPrivilege; winerror exists only there, so every
        # other failure, on every platform, is re-raised as the failure it is.
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("creating a symlink needs a privilege this host withholds")
        raise


@pytest.fixture(autouse=True)
def _scrub_git_location(monkeypatch):
    """Keep every test's git calls on its own temporary repository.

    A pytest run started from a Git hook inherits GIT_DIR, GIT_WORK_TREE or
    GIT_INDEX_FILE, which would redirect the git init and git add below to the
    outer repository. The one test that sets these variables on purpose does so
    through monkeypatch after this fixture has run.
    """
    for name in installer.GIT_LOCATION_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def git_repo(root: Path, files: dict[str, str]) -> Path:
    """Create a repository whose tracked files are exactly the given paths."""
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def test_detection_applies_the_per_stack_threshold(tmp_path):
    repo = git_repo(tmp_path / "repo", {
        "pkg/a.py": "x\n", "pkg/b.py": "x\n",
        "cmd/main.go": "x\n", "cmd/util.go": "x\n",
        "db/schema.sql": "x\n",
        "one/Only.cs": "x\n",
    })

    stacks = installer.detect_stacks(installer.repository_files(repo))

    assert "python" in stacks and "go" in stacks
    # One .sql file is decisive, one .cs file does not reach the default threshold.
    assert "sql" in stacks
    assert "csharp" not in stacks
    assert stacks == sorted(stacks)


def test_detection_falls_back_to_a_walk_outside_git(tmp_path):
    plain = tmp_path / "plain"
    (plain / "src").mkdir(parents=True)
    (plain / "src" / "a.py").write_text("x\n", encoding="utf-8")
    (plain / "src" / "b.py").write_text("x\n", encoding="utf-8")

    assert "python" in installer.detect_stacks(installer.repository_files(plain))


def test_inherited_git_variables_do_not_redirect_detection(tmp_path, monkeypatch):
    target = git_repo(tmp_path / "target", {"a.py": "x\n", "b.py": "x\n"})
    other = git_repo(tmp_path / "other", {"m.go": "x\n", "n.go": "x\n"})
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))

    paths = installer.repository_files(target)

    assert sorted(paths) == ["a.py", "b.py"]


def test_walk_fallback_skips_vendored_trees(tmp_path):
    plain = tmp_path / "plain"
    (plain / "node_modules" / "left-pad").mkdir(parents=True)
    (plain / "node_modules" / "left-pad" / "index.js").write_text("x\n", encoding="utf-8")
    (plain / "node_modules" / "left-pad" / "other.ts").write_text("x\n", encoding="utf-8")
    (plain / "src").mkdir()
    (plain / "src" / "a.py").write_text("x\n", encoding="utf-8")
    (plain / "src" / "b.py").write_text("x\n", encoding="utf-8")

    stacks = installer.detect_stacks(installer.repository_files(plain))

    assert "python" in stacks
    assert "frontend" not in stacks


@POSIX_ONLY
def test_the_targets_own_git_config_cannot_run_a_command(tmp_path):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "b.py": "x\n"})
    marker = tmp_path / "hook_ran.txt"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\necho ran >> {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=repo, check=True)

    paths = installer.repository_files(repo)

    assert sorted(paths) == ["a.py", "b.py"]
    assert not marker.exists(), "the target's core.fsmonitor was executed"


def test_a_git_file_redirect_cannot_supply_another_repositorys_files(tmp_path):
    other = git_repo(tmp_path / "other", {"foreign.bsl": "x\n", "foreign.sql": "x\n"})
    target = tmp_path / "target"
    target.mkdir()
    (target / ".git").write_text(f"gitdir: {other / '.git'}\n", encoding="utf-8")
    (target / "b.py").write_text("y\n", encoding="utf-8")
    (target / "c.py").write_text("z\n", encoding="utf-8")

    stacks = installer.detect_stacks(installer.repository_files(target))

    assert "bsl-1c" not in stacks and "sql" not in stacks


@pytest.mark.parametrize("make, needle", [
    (lambda path, outside: os.link(outside, path), "hard links"),
    pytest.param(lambda path, outside: os.mkfifo(path), "not a regular file",
                 marks=POSIX_ONLY),
])
def test_an_unsuitable_instruction_destination_is_refused(tmp_path, capsys, make, needle):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n"})
    outside = tmp_path / "secret.txt"
    outside.write_text("TOKEN=abc123\n", encoding="utf-8")
    make(repo / "AGENTS.md", outside)

    assert installer.main(["--repo", str(repo)]) == 1

    assert needle in capsys.readouterr().err
    assert not (repo / installer.CONFIG_NAME).exists()
    assert outside.read_text(encoding="utf-8") == "TOKEN=abc123\n"


def test_quoting_the_managed_line_does_not_make_a_file_the_installers_own(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "AGENTS.md": "# r\n"})
    hand_written = f"{installer.MANAGED_MARK}\nreviews: {{}}\n"
    (repo / installer.CONFIG_NAME).write_text(hand_written, encoding="utf-8")

    assert installer.main(["--repo", str(repo)]) == 1

    err = capsys.readouterr().err
    assert "not written by this installer" in err
    assert (repo / installer.CONFIG_NAME).read_text(encoding="utf-8") == hand_written
    # A refused configuration must not leave the instruction file rewritten either.
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "# r\n"
    assert not list(repo.glob("AGENTS.md.backup.*"))
    assert "Installed before the failure" not in err


def test_the_walk_does_not_count_a_link_to_a_file_outside_the_target(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.bsl").write_text("x\n", encoding="utf-8")
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x\n", encoding="utf-8")
    (plain / "b.py").write_text("x\n", encoding="utf-8")
    make_symlink(plain / "linked.bsl", outside / "x.bsl")

    stacks = installer.detect_stacks(installer.repository_files(plain))

    assert "bsl-1c" not in stacks
    assert "python" in stacks


def test_a_tracked_link_to_a_file_outside_the_target_is_not_counted(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.bsl").write_text("x\n", encoding="utf-8")
    (outside / "y.sql").write_text("select 1;\n", encoding="utf-8")
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "b.py": "x\n"})
    make_symlink(repo / "linked.bsl", outside / "x.bsl")
    make_symlink(repo / "linked.sql", outside / "y.sql")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    listed = installer.repository_files(repo)

    # The tracked links resolve, so is_file alone would count them and one file
    # is enough to select either stack: the Git path must read them as the walk
    # does, or the same tree yields two configurations.
    assert listed == ["a.py", "b.py"]
    assert installer.detect_stacks(listed) == ["python"]


@POSIX_ONLY
def test_a_wedged_git_listing_falls_back_to_the_walk(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "b.py": "x\n"})
    fifo = repo / "blocks.fifo"
    os.mkfifo(fifo)
    # git blocks in open() on an included path that never delivers, so without a
    # bounded call the installer never returns and never reports anything.
    with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write(f"[include]\n\tpath = {fifo}\n")
    monkeypatch.setattr(installer, "GIT_LIST_TIMEOUT", 2)

    listed = installer.repository_files(repo)

    assert sorted(listed) == ["a.py", "b.py"]
    assert installer.detect_stacks(listed) == ["python"]


def test_a_path_git_did_not_materialise_is_reported_not_guessed(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "backend" / "a.py").write_text("x\n", encoding="utf-8")
    (repo / "backend" / "b.py").write_text("x\n", encoding="utf-8")
    (repo / "docs" / "one.md").write_text("# d\n", encoding="utf-8")
    (repo / "docs" / "two.md").write_text("# d\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    subprocess.run(["git", "sparse-checkout", "set", "--cone", "backend"],
                   cwd=repo, check=True, capture_output=True)
    assert not (repo / "docs").exists()

    listed = installer.repository_files(repo)

    # The filesystem decides what the target holds, because the index is what a
    # hostile .git redirects. The paths git left out are therefore absent from the
    # configuration, and the run says so instead of narrowing the stacks silently.
    assert listed == ["backend/a.py", "backend/b.py"]
    err = capsys.readouterr().err
    assert "skip-worktree" in err
    assert "docs/one.md" in err and "docs/two.md" in err
    assert "--stacks" in err


def test_a_redirect_cannot_add_paths_by_marking_them_skip_worktree(tmp_path, capsys):
    # The regression this guards: honouring the flag to keep an unmaterialised
    # path reopened the gitdir redirect, because a foreign index can set it.
    other = git_repo(tmp_path / "other", {"x.bsl": "x\n", "y.sql": "select 1;\n",
                                          "z.py": "x\n"})
    subprocess.run(["git", "update-index", "--skip-worktree", "x.bsl", "y.sql"],
                   cwd=other, check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x\n", encoding="utf-8")
    (repo / ".git").write_text(f"gitdir: {other / '.git'}\n", encoding="utf-8")

    listed = installer.repository_files(repo)

    assert listed == []
    assert installer.detect_stacks(listed) == []
    # The gap is still reported, so a redirect cannot pass silently either.
    assert "skip-worktree" in capsys.readouterr().err


def test_a_conflicted_path_counts_once_toward_its_stack(tmp_path):
    repo = git_repo(tmp_path / "repo", {"a.c": "base\n"})
    commit = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q"]
    subprocess.run([*commit, "-m", "base"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=repo, check=True)
    (repo / "a.c").write_text("side\n", encoding="utf-8")
    subprocess.run([*commit, "-am", "side"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-"], cwd=repo, check=True)
    (repo / "a.c").write_text("main\n", encoding="utf-8")
    subprocess.run([*commit, "-am", "main"], cwd=repo, check=True)
    subprocess.run(["git", "merge", "side"], cwd=repo, check=False, capture_output=True)

    listed = installer.repository_files(repo)

    # ls-files reports an unmerged path once per stage, and c-embedded needs two
    # files, so the three records used to select a stack from one conflicted file.
    assert listed == ["a.c"]
    assert installer.detect_stacks(listed) == []


def test_an_in_target_hard_link_is_refused_without_blaming_outside_content(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "AGENTS.md": "# r\n"})
    os.link(repo / "AGENTS.md", repo / "CLAUDE.md")

    assert installer.main(["--repo", str(repo)]) == 1

    err = capsys.readouterr().err
    assert "hard links" in err
    # The link count proves another name exists, not that it is outside.
    assert "another name" in err
    assert "from outside the target into it" not in err


@POSIX_ONLY
def test_the_configuration_refusal_offers_no_link_to_remove(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "AGENTS.md": "# r\n"})
    os.mkfifo(repo / installer.CONFIG_NAME)

    assert installer.main(["--repo", str(repo)]) == 1

    err = capsys.readouterr().err
    assert "not a regular file" in err
    # The configuration name is fixed: there is no link here and no other path.
    assert "remove the link" not in err
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "# r\n"


def test_a_non_ascii_tracked_name_is_listed_and_counted(tmp_path):
    repo = git_repo(tmp_path / "repo", {"Обработка.bsl": "Процедура\n", "a.py": "x\n"})

    listed = installer.repository_files(repo)

    # Coverage, not a regression test: on a UTF-8 host the decoding and the
    # filesystem lookup agree whichever codec the call pins, so what this proves
    # is that a non-ASCII name survives the listing at all. The codec itself is
    # pinned by the test below.
    assert "Обработка.bsl" in listed
    assert "bsl-1c" in installer.detect_stacks(listed)


def test_the_listing_does_not_decode_with_the_locale_codec(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n"})
    recorded: dict = {}
    real_run = installer.subprocess.run

    def record(*args, **kwargs):
        recorded.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", record)

    installer.repository_files(repo)

    # The process cannot change its own locale, so the codec is asserted rather
    # than exercised: text=True decodes git's path bytes with the ambient
    # encoding and strict errors, and under an ASCII locale one non-ASCII tracked
    # name then aborts the whole run.
    assert recorded.get("encoding") == "utf-8"
    assert recorded.get("errors") == "surrogateescape"
    assert "text" not in recorded


def test_a_name_that_is_not_utf_8_does_not_abort_the_listing(tmp_path, monkeypatch, capsys):
    repo = git_repo(tmp_path / "repo", {"ok.py": "x\n"})
    # Exactly what the call yields for a repository holding a latin-1 filename,
    # which Linux allows: surrogateescape carries those bytes through unchanged.
    stdout = b"H latin-\xe9.py\0H ok.py\0S docs/\xff.md\0".decode("utf-8", "surrogateescape")
    monkeypatch.setattr(installer.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout, ""))

    listed = installer.repository_files(repo)

    # Neither the decode, the stat nor the report may raise on those names. The
    # crafted paths are not on disk, so none of them is kept on any platform.
    assert listed == ["ok.py"]
    # capsys encodes strictly, as a redirected stream or PYTHONIOENCODING does,
    # so a raw surrogate in the message would raise here rather than print.
    err = capsys.readouterr().err
    assert "skip-worktree" in err
    assert r"docs/\udcff.md" in err
    assert "1 tracked path " in err, "the count and its noun have to agree"


def test_the_symlink_helper_hides_only_the_windows_privilege_error(tmp_path, monkeypatch):
    # The Windows branch is simulated, because this host raises neither error:
    # what the test pins is that one error code is skipped and nothing else is.
    def refuse(self, target, **kwargs):
        raise OSError("the fixture is broken, not the privilege")

    monkeypatch.setattr(Path, "symlink_to", refuse)
    with pytest.raises(OSError):
        make_symlink(tmp_path / "link", tmp_path / "target")

    def withhold_privilege(self, target, **kwargs):
        error = OSError("a required privilege is not held by the client")
        error.winerror = 1314
        raise error

    monkeypatch.setattr(Path, "symlink_to", withhold_privilege)
    with pytest.raises(pytest.skip.Exception):
        make_symlink(tmp_path / "link", tmp_path / "target")


@pytest.mark.parametrize("encoding, needle", [
    ("ascii", r"docs/\u041f"),
    ("utf-8", "docs/Пример.md"),
])
def test_the_warning_escapes_only_what_its_stream_cannot_encode(tmp_path, monkeypatch,
                                                                encoding, needle):
    repo = git_repo(tmp_path / "repo", {"ok.py": "x\n"})
    monkeypatch.setattr(installer.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a[0], 0, "S docs/Пример.md\0H ok.py\0", ""))
    # A strict stream is what PYTHONIOENCODING without an error handler gives, and
    # what a caller that replaces sys.stderr gives: the warning has to survive it,
    # because aborting on the message defeats the point of warning at all.
    stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")
    monkeypatch.setattr(sys, "stderr", stream)

    listed = installer.repository_files(repo)

    stream.flush()
    reported = stream.buffer.getvalue().decode(encoding)
    assert listed == ["ok.py"]
    assert needle in reported


def test_an_instruction_file_outside_the_target_is_reported(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n"})
    outside = tmp_path / "elsewhere" / "TEAM.md"
    outside.parent.mkdir()

    assert installer.main(["--repo", str(repo), "--agents-md", str(outside),
                           "--create-agents-md"]) == 0

    out = capsys.readouterr().out
    assert "Outside the target, not covered by its commit" in out
    assert str(outside) in out


def test_walk_fallback_survives_a_vendor_named_ancestor(tmp_path, monkeypatch):
    # The target sits under a directory whose name is also a vendor name.
    repo = tmp_path / "build" / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("x\n", encoding="utf-8")

    def unavailable(*args, **kwargs):
        raise OSError("git is unavailable")

    monkeypatch.setattr(installer.subprocess, "run", unavailable)
    paths = installer.repository_files(repo)

    assert sorted(paths) == ["src/a.py", "src/b.py"]
    assert "python" in installer.detect_stacks(paths)


def test_relative_instruction_path_may_not_escape_through_a_symlink(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    make_symlink(repo / "docs", outside, target_is_directory=True)

    exit_code = installer.main(["--repo", str(repo), "--agents-md", "docs/AGENTS.md",
                                "--create-agents-md"])

    assert exit_code == 1
    assert "resolves to" in capsys.readouterr().err
    assert list(outside.iterdir()) == []
    assert not (repo / installer.CONFIG_NAME).exists()


def test_a_broken_marker_pair_leaves_no_configuration_behind(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n"})
    (repo / "AGENTS.md").write_text(
        f"{installer.SECTION_BEGIN}\nx\n{installer.SECTION_END}\n"
        f"{installer.SECTION_BEGIN}\ny\n{installer.SECTION_END}\n", encoding="utf-8")

    assert installer.main(["--repo", str(repo)]) == 1

    assert "marker pair" in capsys.readouterr().err
    # The configuration is written only after the instruction file passes its checks.
    assert not (repo / installer.CONFIG_NAME).exists()


def test_composed_configuration_parses_and_leads_with_the_universal_set(tmp_path):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "b.py": "x\n", "AGENTS.md": "# r\n"})

    text = installer.compose_config(installer.repository_files(repo), [], None, None, [])
    config = yaml.safe_load(text)

    assert text.startswith(installer.MANAGED_MARK)
    assert config["inheritance"] is True
    universal = yaml.safe_load(
        (installer.INSTRUCTION_DIR / f"{installer.UNIVERSAL_STACK}.yaml").read_text("utf-8"))
    entries = config["reviews"]["path_instructions"]
    assert entries[:len(universal)] == universal
    assert config["knowledge_base"]["code_guidelines"]["filePatterns"] == ["AGENTS.md"]


def test_base_branch_becomes_an_anchored_regex_and_language_is_set(tmp_path):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n"})
    branches = installer.base_branch_patterns(["dev", "release/1.0"], [])

    config = yaml.safe_load(installer.compose_config(
        installer.repository_files(repo), [], "ru-RU", "quiet", branches))

    assert config["reviews"]["auto_review"]["base_branches"] == ["^dev$", "^release/1\\.0$"]
    assert config["language"] == "ru-RU"
    assert config["reviews"]["profile"] == "quiet"


def test_explicit_regex_reaches_the_configuration_unanchored():
    assert installer.base_branch_patterns([], [".*"]) == [".*"]
    assert installer.base_branch_patterns(["dev"], ["^v2-.*$"]) == ["^dev$", "^v2-.*$"]


def test_creating_the_instruction_file_keeps_the_install_idempotent(tmp_path, capsys):
    # The target carries CLAUDE.md but not AGENTS.md, which --create-agents-md adds.
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "b.py": "x\n", "CLAUDE.md": "# c\n"})

    assert installer.main(["--repo", str(repo), "--create-agents-md"]) == 0
    capsys.readouterr()
    first = (repo / installer.CONFIG_NAME).read_bytes()

    assert installer.main(["--repo", str(repo), "--create-agents-md"]) == 0
    second = capsys.readouterr().out

    patterns = yaml.safe_load(first)["knowledge_base"]["code_guidelines"]["filePatterns"]
    assert "AGENTS.md" in patterns and "CLAUDE.md" in patterns
    assert (repo / installer.CONFIG_NAME).read_bytes() == first
    assert "already current" in second
    # A rewrite would have left a backup beside the configuration.
    assert not list(repo.glob(f"{installer.CONFIG_NAME}.backup.*"))


def test_the_written_configuration_is_not_counted_as_a_target_file(tmp_path, capsys):
    # One iac-ci file is present; the written .coderabbit.yaml must not be the second.
    repo = git_repo(tmp_path / "repo", {
        "a.py": "x\n", "b.py": "x\n", "pyproject.toml": "[project]\n", "AGENTS.md": "# r\n"})

    assert installer.main(["--repo", str(repo)]) == 0
    first = capsys.readouterr().out
    # The flow commits the installed files, so the next run sees it tracked.
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    assert installer.main(["--repo", str(repo)]) == 0
    second = capsys.readouterr().out

    assert "iac-ci" not in first
    assert "iac-ci" not in second
    assert "already current" in second
    assert not list(repo.glob(f"{installer.CONFIG_NAME}.backup.*"))


# getattr, not os.geteuid(): a skipif argument is evaluated while the module is
# imported, and Windows has no geteuid, so collecting it there would raise.
@POSIX_ONLY
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="root ignores the directory mode")
def test_an_unwritable_instruction_directory_installs_nothing(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "docs/CONTRIBUTING.md": "# c\n"})
    (repo / "docs").chmod(0o500)
    try:
        exit_code = installer.main(["--repo", str(repo), "--agents-md", "docs/CONTRIBUTING.md"])
    finally:
        (repo / "docs").chmod(0o700)

    assert exit_code == 1
    assert "Installed before the failure" not in capsys.readouterr().err
    # The instruction file is written first, so the configuration never appeared.
    assert not (repo / installer.CONFIG_NAME).exists()


def test_a_symlinked_configuration_is_refused_in_the_dry_run(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "AGENTS.md": "# r\n"})
    make_symlink(repo / installer.CONFIG_NAME, tmp_path / "elsewhere.yaml")

    assert installer.main(["--repo", str(repo), "--dry-run"]) == 1

    assert "symlink" in capsys.readouterr().err
    assert not (tmp_path / "elsewhere.yaml").exists()


def test_the_stack_order_does_not_change_the_composed_bytes(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "AGENTS.md": "# r\n"})

    assert installer.main(["--repo", str(repo), "--stacks", "python,go", "--print"]) == 0
    one = capsys.readouterr().out
    assert installer.main(["--repo", str(repo), "--stacks", "go,python", "--print"]) == 0
    other = capsys.readouterr().out

    assert one == other


def test_glob_regex_spans_directories_only_for_a_double_star():
    assert installer.glob_regex("docs/**/*.md").match("docs/a/b/c.md")
    assert installer.glob_regex("docs/**/*.md").match("docs/c.md")
    assert not installer.glob_regex("docs/*.md").match("docs/a/b.md")
    assert installer.glob_regex("AGENTS.md").match("AGENTS.md")
    assert not installer.glob_regex("AGENTS.md").match("sub/AGENTS.md")


def test_a_nested_created_instruction_file_keeps_its_pattern(tmp_path, capsys):
    # docs/ holds nothing else, so only the created file can match docs/**/*.md.
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "b.py": "x\n"})

    assert installer.main(["--repo", str(repo), "--agents-md", "docs/AGENTS.md",
                           "--create-agents-md"]) == 0
    capsys.readouterr()
    first = (repo / installer.CONFIG_NAME).read_bytes()
    patterns = yaml.safe_load(first)["knowledge_base"]["code_guidelines"]["filePatterns"]

    assert installer.main(["--repo", str(repo), "--agents-md", "docs/AGENTS.md",
                           "--create-agents-md"]) == 0

    assert "docs/**/*.md" in patterns
    assert (repo / installer.CONFIG_NAME).read_bytes() == first
    assert "already current" in capsys.readouterr().out


def test_detection_outside_git_is_stable_across_runs(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x\n", encoding="utf-8")
    (plain / "b.py").write_text("x\n", encoding="utf-8")
    (plain / "CLAUDE.md").write_text("# c\n", encoding="utf-8")

    assert installer.main(["--repo", str(plain), "--create-agents-md"]) == 0
    capsys.readouterr()
    first = (plain / installer.CONFIG_NAME).read_bytes()

    assert installer.main(["--repo", str(plain), "--create-agents-md"]) == 0

    assert (plain / installer.CONFIG_NAME).read_bytes() == first
    assert "already current" in capsys.readouterr().out


def test_a_symlinked_instruction_file_is_refused_before_any_write(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n"})
    outside = tmp_path / "notes.md"
    outside.write_text("# notes\n", encoding="utf-8")
    make_symlink(repo / "AGENTS.md", outside)

    assert installer.main(["--repo", str(repo)]) == 1

    assert "symlink" in capsys.readouterr().err
    assert not (repo / installer.CONFIG_NAME).exists()
    assert outside.read_text(encoding="utf-8") == "# notes\n"


def test_absent_guideline_files_drop_the_pattern_list(tmp_path):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n"})

    config = yaml.safe_load(installer.compose_config(
        installer.repository_files(repo), [], None, None, []))

    assert "filePatterns" not in config["knowledge_base"]["code_guidelines"]


def test_install_is_idempotent_and_reports_each_target(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "b.py": "x\n", "AGENTS.md": "# r\n\nRule.\n"})

    assert installer.main(["--repo", str(repo)]) == 0
    first = capsys.readouterr().out
    config_bytes = (repo / installer.CONFIG_NAME).read_bytes()
    md_bytes = (repo / "AGENTS.md").read_bytes()

    assert installer.main(["--repo", str(repo)]) == 0
    second = capsys.readouterr().out

    assert "written" in first
    assert "already current" in second
    assert (repo / installer.CONFIG_NAME).read_bytes() == config_bytes
    assert (repo / "AGENTS.md").read_bytes() == md_bytes


def test_dry_run_leaves_the_target_untouched(tmp_path):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "AGENTS.md": "# r\n"})

    assert installer.main(["--repo", str(repo), "--dry-run"]) == 0

    assert not (repo / installer.CONFIG_NAME).exists()
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "# r\n"


def test_unmanaged_configuration_is_refused_until_force(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "AGENTS.md": "# r\n"})
    (repo / installer.CONFIG_NAME).write_text("reviews:\n  profile: chill\n", encoding="utf-8")

    assert installer.main(["--repo", str(repo)]) == 1
    assert "not written by this installer" in capsys.readouterr().err
    assert "profile: chill" in (repo / installer.CONFIG_NAME).read_text(encoding="utf-8")

    assert installer.main(["--repo", str(repo), "--force"]) == 0
    assert installer.MANAGED_MARK in (repo / installer.CONFIG_NAME).read_text(encoding="utf-8")


def test_section_preserves_surrounding_content_and_refreshes_in_place(tmp_path):
    md = tmp_path / "AGENTS.md"
    md.write_text("# Repo\n\nKeep me.\n", encoding="utf-8")

    installer.install_section(md, create=False, dry_run=False)
    once = md.read_text(encoding="utf-8")
    md.write_text(once.replace("Answer every finding", "Stale heading"), encoding="utf-8")
    installer.install_section(md, create=False, dry_run=False)
    twice = md.read_text(encoding="utf-8")

    assert "Keep me." in twice
    assert twice.count(installer.SECTION_BEGIN) == 1
    assert twice.count(installer.SECTION_END) == 1
    assert "Stale heading" not in twice
    assert twice.index("Keep me.") < twice.index(installer.SECTION_BEGIN)


def test_duplicate_markers_are_reported_instead_of_guessed(tmp_path):
    md = tmp_path / "AGENTS.md"
    md.write_text(f"{installer.SECTION_BEGIN}\nx\n{installer.SECTION_END}\n"
                  f"{installer.SECTION_BEGIN}\ny\n{installer.SECTION_END}\n", encoding="utf-8")

    with pytest.raises(installer.InstallError, match="one complete review-discipline marker"):
        installer.install_section(md, create=False, dry_run=False)


def test_missing_instruction_file_requires_an_explicit_choice(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n"})

    assert installer.main(["--repo", str(repo)]) == 1
    assert "--create-agents-md" in capsys.readouterr().err
    # A refusal must not leave the configuration installed on its own.
    assert not (repo / installer.CONFIG_NAME).exists()

    assert installer.main(["--repo", str(repo), "--create-agents-md"]) == 0
    assert installer.SECTION_BEGIN in (repo / "AGENTS.md").read_text(encoding="utf-8")


def test_relative_instruction_path_belongs_to_the_target(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "docs/CONTRIBUTING.md": "# c\n"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "docs").mkdir()
    (elsewhere / "docs" / "CONTRIBUTING.md").write_text("# untouched\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)

    assert installer.main(["--repo", str(repo), "--agents-md", "docs/CONTRIBUTING.md"]) == 0

    assert installer.SECTION_BEGIN in (repo / "docs/CONTRIBUTING.md").read_text(encoding="utf-8")
    assert (elsewhere / "docs/CONTRIBUTING.md").read_text(encoding="utf-8") == "# untouched\n"


def test_unknown_stack_and_bad_language_are_rejected(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo", {"a.py": "x\n", "AGENTS.md": "# r\n"})

    assert installer.main(["--repo", str(repo), "--stacks", "klingon"]) == 1
    assert "Unknown stacks" in capsys.readouterr().err
    assert installer.main(["--repo", str(repo), "--language", "Russian"]) == 1
    assert "not an ISO language code" in capsys.readouterr().err


@pytest.mark.parametrize("fragment", sorted(
    (Path(__file__).resolve().parents[1] / "scripts/templates/review/path-instructions")
    .glob("*.yaml")), ids=lambda path: path.stem)
def test_every_shipped_fragment_matches_the_coderabbit_schema(fragment):
    entries = yaml.safe_load(fragment.read_text(encoding="utf-8"))

    assert isinstance(entries, list) and entries, f"{fragment} must hold a non-empty sequence"
    for entry in entries:
        assert set(entry) == {"path", "instructions"}, f"{fragment}: unexpected keys {set(entry)}"
        assert isinstance(entry["path"], str) and entry["path"].strip()
        instructions = entry["instructions"]
        assert isinstance(instructions, str) and instructions.strip()
        assert len(instructions) <= INSTRUCTION_LIMIT, f"{fragment}: instruction too long"
        # The repository writes review guidance with ordinary punctuation.
        assert "—" not in instructions and "–" not in instructions, (
            f"{fragment}: em or en dash in instructions")


@pytest.mark.parametrize("stack", sorted(installer.available_stacks()))
def test_each_stack_composes_and_round_trips_without_loss(tmp_path, stack):
    repo = git_repo(tmp_path / "repo", {"AGENTS.md": "# r\n"})
    universal = yaml.safe_load(
        (installer.INSTRUCTION_DIR / f"{installer.UNIVERSAL_STACK}.yaml").read_text("utf-8"))
    fragment = yaml.safe_load((installer.INSTRUCTION_DIR / f"{stack}.yaml").read_text("utf-8"))

    config = yaml.safe_load(installer.compose_config(
        installer.repository_files(repo), [stack], None, None, []))

    # The dumper rewrites every instruction as a block scalar, so reparsing it has
    # to return the fragment unchanged, character for character.
    assert config["reviews"]["path_instructions"] == universal + fragment


def test_every_fragment_is_reachable_through_the_stack_names():
    stacks = set(installer.available_stacks())
    suffix_stacks = set(installer.STACK_SUFFIXES)

    assert stacks, "no stack fragments are installed"
    assert stacks <= suffix_stacks, f"fragments without detection rules: {stacks - suffix_stacks}"
    assert set(installer.STACK_MIN_FILES) <= suffix_stacks


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
