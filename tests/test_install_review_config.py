"""Verify stack detection, composed review configuration and managed-section writes."""
import importlib.util
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
    (lambda path, outside: os.mkfifo(path), "not a regular file"),
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

    assert "not written by this installer" in capsys.readouterr().err
    assert (repo / installer.CONFIG_NAME).read_text(encoding="utf-8") == hand_written


def test_the_walk_does_not_count_a_link_to_a_file_outside_the_target(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.bsl").write_text("x\n", encoding="utf-8")
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x\n", encoding="utf-8")
    (plain / "b.py").write_text("x\n", encoding="utf-8")
    (plain / "linked.bsl").symlink_to(outside / "x.bsl")

    stacks = installer.detect_stacks(installer.repository_files(plain))

    assert "bsl-1c" not in stacks
    assert "python" in stacks


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
    (repo / "docs").symlink_to(outside, target_is_directory=True)

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


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the directory mode")
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
    (repo / installer.CONFIG_NAME).symlink_to(tmp_path / "elsewhere.yaml")

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
    (repo / "AGENTS.md").symlink_to(outside)

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
