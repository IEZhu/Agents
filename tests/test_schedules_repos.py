"""The trusted repository, one-off session repositories, bundle import and environments (#339).

Every test builds throwaway repositories under ``tmp_path``: a local "GitHub" repository stands
in for the remote (its absolute path is the explicit URL), and sessions run git in their one-off
repository with the fixture environment. Nothing reaches the network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat
import subprocess
import threading

import pytest

from src.schedules import config as autodev_config
from src.schedules import drift, gitsafe, repos, runner, sequence
from src.schedules.repos import BundleRefused, RepositoryError, TrustedRepository
from tests.test_schedules_paths import fixture_environment, fixture_git

RUN_ID = "20261013T020000Z-abc123"
BRANCH = "claude/autodev-339-repos"
FILES = {"README.md": "# Agents\n", "requirements.txt": "pytest\n", "pyproject.toml": "[project]\n",
         "src/schedules/runner.py": "print('runner')\n", "agents/a/system_prompt.mdc": "prompt\n"}


def write(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(content, encoding="utf-8")


def commit_all(root: Path, message: str = "change") -> str:
    fixture_git(root, "add", "-A")
    fixture_git(root, "commit", "--quiet", "--allow-empty", "-m", message)
    return fixture_git(root, "rev-parse", "HEAD")


def refs(repository: Path) -> dict[str, str]:
    listing = fixture_git(repository, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ") for line in listing.splitlines())


@pytest.fixture
def github(tmp_path):
    """The stand-in for https://github.com/IEZhu/Agents.git: a working repository on main."""
    root = tmp_path / "github"
    fixture_git(tmp_path, "init", "--quiet", "-b", "main", str(root))
    write(root, FILES)
    commit_all(root, "initial")
    return root


@pytest.fixture
def trusted(tmp_path, github):
    repository = TrustedRepository(tmp_path / "runner" / "IEZhu-Agents", str(github))
    repository.ensure().fetch()
    return repository


@pytest.fixture
def exchange(tmp_path):
    path = tmp_path / "exchange"
    path.mkdir()
    return path


def session(trusted, tmp_path, name="session"):
    return trusted.create_session_repository(tmp_path / name)


def bundle(one_off: Path, exchange: Path, *revisions: str, name: str = "work.bundle") -> Path:
    path = exchange / name
    fixture_git(one_off, "bundle", "create", "--quiet", str(path), *revisions)
    return path


def work(one_off: Path, files: dict[str, str], branch: str = BRANCH) -> str:
    fixture_git(one_off, "switch", "--quiet", "-c", branch)
    write(one_off, files)
    return commit_all(one_off)


# --- the trusted repository ------------------------------------------------------------------

def test_trusted_repository_is_bare_and_pins_main_from_the_explicit_url(trusted, github):
    assert trusted.pinned() == fixture_git(github, "rev-parse", "HEAD")
    assert fixture_git(trusted.path, "rev-parse", "--is-bare-repository") == "true"
    assert fixture_git(trusted.path, "remote") == ""  # no remote name to push or fetch by
    assert set(refs(trusted.path)) == {repos.PINNED_REF}
    write(github, {"README.md": "# moved\n"})
    moved = commit_all(github)
    assert trusted.fetch() == moved == trusted.pinned()


def test_every_git_command_runs_with_a_clean_configuration_and_fsck(tmp_path, github, exchange, monkeypatch):
    calls = []

    class RecordingPopen(subprocess.Popen):  # subprocess.run and gitsafe.git_bounded both start Popen
        def __init__(self, command, **kwargs):
            if "GIT_AUTHOR_NAME" not in (kwargs.get("env") or {}):  # not the session's own git (fixture_git)
                calls.append((command, kwargs))
            super().__init__(command, **kwargs)
    monkeypatch.setattr(gitsafe.subprocess, "Popen", RecordingPopen)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    trusted = TrustedRepository(tmp_path / "runner" / "IEZhu-Agents", str(github))
    trusted.ensure().fetch()
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/new.md": "new\n"})
    trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}"), exchange, BRANCH, RUN_ID, one_off.pinned)

    assert calls
    for command, kwargs in calls:
        assert command[1:14] == ["--no-pager", "--no-replace-objects", "--literal-pathspecs",
                                 "-c", "core.quotePath=false", "-c", "core.fsmonitor=false",
                                 "-c", f"core.hooksPath={os.devnull}", "-c", "core.commitGraph=false",
                                 "-c", "advice.graftFileDeprecated=false"]
        assert "transfer.fsckObjects=true" in command and "gc.auto=0" in command
        # review round 2: large blobs streamed, one pack thread, so git's memory stays bounded
        assert "core.bigFileThreshold=1m" in command and "pack.threads=1" in command
        assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1" and kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "GIT_DIR" not in kwargs["env"]
    assert any("--batch-check" in command for command, _ in calls) is False  # no symbolic link here
    assert any("diff-tree" in command and "--stdin" in command for command, _ in calls)
    fetches = [command for command, _ in calls if "fetch" in command]
    assert [command[-2:] for command in fetches][0] == [str(github), repos.MAIN_REFSPEC]
    assert all("origin" not in command for command in fetches)


@pytest.mark.parametrize("where", ["documents", "installation", "relative", "above"])
def test_trusted_repository_location_is_refused_where_a_session_could_reach_it(tmp_path, github, where):
    installation = tmp_path / "installation"
    path = {"documents": Path.home() / "Documents" / "autodev-trusted", "installation": installation / "clone",
            "relative": Path("relative/clone"), "above": tmp_path}[where]
    with pytest.raises(ValueError, match="must"):
        TrustedRepository(path, str(github), installation=installation)
    assert not (Path.home() / "Documents" / "autodev-trusted").exists()


@pytest.mark.parametrize("clone", ["~/Documents/Agents-clone", "relative/clone", "~"])
def test_configuration_refuses_a_clone_under_documents_or_relative(service_state_dir, clone):
    config = autodev_config.load(service_state_dir)
    config["clone"] = clone
    with pytest.raises(autodev_config.ConfigError, match="clone must"):
        autodev_config.validate(config)
    assert autodev_config.runner_location(autodev_config.DEFAULTS["clone"]).is_absolute()


@pytest.mark.parametrize("planted", ["info/grafts", "objects/info/alternates"])
def test_trusted_repository_with_grafts_or_alternates_is_refused(trusted, planted):
    (trusted.path / planted).parent.mkdir(parents=True, exist_ok=True)
    (trusted.path / planted).write_text("\n")
    with pytest.raises(RepositoryError, match=planted):
        trusted.fetch()


def test_a_non_bare_directory_is_not_taken_for_the_trusted_repository(tmp_path, github):
    with pytest.raises(RepositoryError):
        TrustedRepository(github, str(github)).ensure()


@pytest.mark.parametrize("url", ["origin", "-uorigin", "github.com/IEZhu/Agents", "https://evil.example/x.git",
                                 "ext::sh -c touch% /tmp/x"])
def test_url_must_be_explicit(tmp_path, url):
    with pytest.raises(ValueError, match="explicit repository URL"):
        TrustedRepository(tmp_path / "trusted", url)


def test_push_arguments_name_an_explicit_url_and_refspec():
    sha = "a" * 40
    assert repos.push_arguments("git@github.com:IEZhu/Agents.git", sha, 339, "repos") == [
        "push", "--porcelain", "--no-follow-tags", "--no-signed", "git@github.com:IEZhu/Agents.git",
        f"{sha}:refs/heads/claude/autodev-339-repos"]
    for bad in [("origin", sha, 339, "repos"), ("git@github.com:IEZhu/Agents.git", "HEAD", 339, "repos"),
                ("git@github.com:IEZhu/Agents.git", sha, 0, "repos"),
                ("git@github.com:IEZhu/Agents.git", sha, 339, "Repos/../x"),
                ("git@github.com:IEZhu/Agents.git", sha, 339, "a" * 61)]:
        with pytest.raises(ValueError):
            repos.push_arguments(*bad)


# --- one-off repositories ---------------------------------------------------------------------

def test_one_off_repository_is_detached_at_pinned_without_origin_or_shared_objects(trusted, tmp_path):
    one_off = session(trusted, tmp_path)

    assert one_off.pinned == trusted.pinned()
    assert fixture_git(one_off.path, "rev-parse", "HEAD") == trusted.pinned()
    assert subprocess.run(["git", "-C", str(one_off.path), "symbolic-ref", "-q", "HEAD"],
                          env=fixture_environment(), capture_output=True).returncode == 1  # detached
    assert fixture_git(one_off.path, "remote") == ""
    assert fixture_git(one_off.path, "config", "--local", "user.name") == "Alexey Zhuchkov"
    assert fixture_git(one_off.path, "config", "--local", "user.email") == "alexey.zhuchkov@gmail.com"
    git_dir = one_off.path / ".git"
    assert not (git_dir / "objects" / "info" / "alternates").exists()
    assert not (git_dir / "hooks").exists() or not any((git_dir / "hooks").iterdir())
    objects = [path for path in (git_dir / "objects").rglob("*") if path.is_file()]
    assert objects and all(path.stat().st_nlink == 1 for path in objects)
    assert (one_off.path / "README.md").read_text() == FILES["README.md"]


def test_one_off_destination_must_be_new_and_outside_the_trusted_repository(trusted, tmp_path):
    (tmp_path / "taken").mkdir()
    with pytest.raises(RepositoryError, match="already exists"):
        trusted.create_session_repository(tmp_path / "taken")
    with pytest.raises(RepositoryError, match="outside the trusted repository"):
        trusted.create_session_repository(trusted.path / "inside")


def test_planted_hook_and_config_affect_neither_the_trusted_repository_nor_the_next_session(
        trusted, tmp_path, exchange, github):
    first = session(trusted, tmp_path, "first")
    head = work(first.path, {"docs/new.md": "new\n"})
    made = bundle(first.path, exchange, BRANCH, f"^{first.pinned}")
    # The session now rewrites its .git: hooks, hooksPath, fsmonitor, an alias and an include.
    marker = tmp_path / "pwned"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {marker}\n")
    script.chmod(0o755)
    hooks = first.path / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-checkout", "pre-commit", "reference-transaction", "post-index-change", "pre-auto-gc"):
        (hooks / name).write_text(script.read_text())
        (hooks / name).chmod(0o755)
    included = tmp_path / "included.config"
    included.write_text(f"[core]\n\tfsmonitor = {script}\n")
    with (first.path / ".git" / "config").open("a") as config:
        config.write(f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = {script}\n[alias]\n\tfetch = !{script}\n"
                     f"[include]\n\tpath = {included}\n[remote \"origin\"]\n\turl = {script}\n")
    trusted_config = (trusted.path / "config").read_text()

    imported = trusted.import_bundle(made, exchange, BRANCH, RUN_ID, first.pinned)
    write(github, {"README.md": "# moved\n"})
    commit_all(github)
    trusted.fetch()
    second = session(trusted, tmp_path, "second")

    assert imported.head == head and not marker.exists()
    assert (trusted.path / "config").read_text() == trusted_config
    second_config = (second.path / ".git" / "config").read_text()
    assert str(script) not in second_config and str(hooks) not in second_config
    assert not (second.path / ".git" / "hooks").exists() or not any((second.path / ".git" / "hooks").iterdir())
    fixture_git(second.path, "status")  # the next session's own git does not reach the planted script
    assert not marker.exists()


# --- bundles ----------------------------------------------------------------------------------

def test_valid_bundle_is_promoted_and_its_quarantine_ref_removed(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/new.md": "new\n"})
    os.symlink("../README.md", one_off.path / "docs" / "readme-link.md")
    head = commit_all(one_off.path)

    imported = trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}"), exchange,
                                     BRANCH, RUN_ID, one_off.pinned)

    assert imported.head == head and imported.branch == BRANCH and len(imported.commits) == 2
    assert refs(trusted.path) == {repos.PINNED_REF: trusted.pinned(), f"refs/heads/{BRANCH}": head}


def gitlink(one_off: Path) -> None:
    fixture_git(one_off, "update-index", "--add", "--cacheinfo", f"160000,{'b' * 40},vendor/sub")


def symlink(target: str):
    def plant(one_off: Path) -> None:
        os.makedirs(one_off / "docs", exist_ok=True)
        os.symlink(target, one_off / "docs" / "link")
        fixture_git(one_off, "add", "-A")
    return plant


@pytest.mark.parametrize("plant,reason", [
    (gitlink, "gitlink vendor/sub"),
    (symlink("/etc/passwd"), "outside the tree"),
    (symlink("../../outside"), "outside the tree"),
    (symlink("sub/../../x"), "outside the tree"),
])
def test_bundle_with_a_gitlink_or_an_outside_symlink_is_refused_and_quarantined(trusted, tmp_path, exchange,
                                                                               plant, reason):
    one_off = session(trusted, tmp_path)
    fixture_git(one_off.path, "switch", "--quiet", "-c", BRANCH)
    plant(one_off.path)
    fixture_git(one_off.path, "commit", "--quiet", "-m", "planted")
    head = fixture_git(one_off.path, "rev-parse", "HEAD")

    with pytest.raises(BundleRefused, match=reason) as refused:
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}"), exchange, BRANCH,
                              RUN_ID, one_off.pinned)

    assert refused.value.quarantine_ref == f"{repos.QUARANTINE}/{RUN_ID}/1"
    assert refs(trusted.path) == {repos.PINNED_REF: trusted.pinned(), refused.value.quarantine_ref: head}


def test_merge_commit_and_head_not_descending_from_pinned_are_refused(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    fixture_git(one_off.path, "switch", "--quiet", "-c", "side")
    write(one_off.path, {"docs/side.md": "side\n"})
    commit_all(one_off.path)
    fixture_git(one_off.path, "switch", "--quiet", "--detach", one_off.pinned)
    work(one_off.path, {"docs/main.md": "main\n"})
    fixture_git(one_off.path, "merge", "--quiet", "--no-ff", "--no-edit", "side")
    with pytest.raises(BundleRefused, match="2 parents") as merged:
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}"), exchange, BRANCH,
                              RUN_ID, one_off.pinned)

    orphan = session(trusted, tmp_path, "orphan")
    fixture_git(orphan.path, "switch", "--quiet", "--orphan", "claude/autodev-339-orphan")
    write(orphan.path, {"x.md": "x\n"})
    commit_all(orphan.path)
    with pytest.raises(BundleRefused, match="does not descend from the session base") as unrelated:
        trusted.import_bundle(bundle(orphan.path, exchange, "claude/autodev-339-orphan", name="orphan.bundle"),
                              exchange, "claude/autodev-339-orphan", RUN_ID, orphan.pinned)

    assert merged.value.quarantine_ref.endswith("/1") and unrelated.value.quarantine_ref.endswith("/2")
    assert not any(ref.startswith("refs/heads/") for ref in refs(trusted.path))


def test_bundle_naming_another_branch_or_two_refs_is_refused_before_any_fetch_and_kept(trusted, tmp_path,
                                                                                       exchange):
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/new.md": "new\n"}, branch="claude/autodev-339-other")
    other_bundle = bundle(one_off.path, exchange, "claude/autodev-339-other", f"^{one_off.pinned}")
    with pytest.raises(BundleRefused, match="not the expected") as other:
        trusted.import_bundle(other_bundle, exchange, BRANCH, RUN_ID, one_off.pinned)
    fixture_git(one_off.path, "branch", BRANCH)
    with pytest.raises(BundleRefused, match="names 2 refs") as two:
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, "claude/autodev-339-other",
                                     f"^{one_off.pinned}", name="two.bundle"), exchange, BRANCH, RUN_ID, one_off.pinned)

    assert other.value.quarantine_ref is None and two.value.quarantine_ref is None
    assert set(refs(trusted.path)) == {repos.PINNED_REF}
    kept = trusted.path / repos.QUARANTINE_FILES / RUN_ID
    assert other.value.quarantine_file == kept / "1.bundle" and two.value.quarantine_file == kept / "2.bundle"
    assert (kept / "1.bundle").read_bytes() == other_bundle.read_bytes()
    assert not stat.S_IMODE((kept / "1.bundle").stat().st_mode) & 0o277
    assert str(kept / "1.bundle") in str(other.value)


def test_symlinked_or_outside_or_oversized_bundle_file_is_refused(trusted, tmp_path, exchange, monkeypatch):
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/new.md": "new\n"})
    real = bundle(one_off.path, tmp_path, BRANCH, f"^{one_off.pinned}", name="real.bundle")
    os.symlink(real, exchange / "linked.bundle")
    with pytest.raises(BundleRefused, match="symbolic link"):
        trusted.import_bundle(exchange / "linked.bundle", exchange, BRANCH, RUN_ID, one_off.pinned)
    with pytest.raises(BundleRefused, match="not directly inside the exchange directory"):
        trusted.import_bundle(real, exchange, BRANCH, RUN_ID, one_off.pinned)
    with pytest.raises(BundleRefused, match="not directly inside the exchange directory"):
        trusted.import_bundle(Path("../real.bundle"), exchange, BRANCH, RUN_ID, one_off.pinned)
    os.mkfifo(exchange / "fifo.bundle")
    with pytest.raises(BundleRefused, match="not a regular file"):
        trusted.import_bundle(exchange / "fifo.bundle", exchange, BRANCH, RUN_ID, one_off.pinned)
    (exchange / "dir.bundle").mkdir()
    with pytest.raises(BundleRefused, match="not a regular file"):
        trusted.import_bundle(exchange / "dir.bundle", exchange, BRANCH, RUN_ID, one_off.pinned)
    os.link(real, exchange / "hard.bundle")  # a hard link to a file outside the exchange directory
    with pytest.raises(BundleRefused, match="2 hard links"):
        trusted.import_bundle(exchange / "hard.bundle", exchange, BRANCH, RUN_ID, one_off.pinned)
    (exchange / "copy.bundle").write_bytes(real.read_bytes())
    monkeypatch.setattr(repos, "MAX_BUNDLE_BYTES", 10)
    with pytest.raises(BundleRefused, match="larger than"):
        trusted.import_bundle(exchange / "copy.bundle", exchange, BRANCH, RUN_ID, one_off.pinned)
    assert set(refs(trusted.path)) == {repos.PINNED_REF}


def test_bundle_with_prerequisites_missing_from_the_trusted_repository_is_refused(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/one.md": "one\n"})
    first = fixture_git(one_off.path, "rev-parse", "HEAD")
    write(one_off.path, {"docs/two.md": "two\n"})
    commit_all(one_off.path)
    with pytest.raises(BundleRefused, match="prerequisites missing") as missing:
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{first}"), exchange, BRANCH, RUN_ID,
                              one_off.pinned)
    assert set(refs(trusted.path)) == {repos.PINNED_REF}
    assert missing.value.quarantine_file == trusted.path / repos.QUARANTINE_FILES / RUN_ID / "1.bundle"
    assert missing.value.quarantine_file.is_file()


def test_bundle_without_new_commits_is_refused(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    fixture_git(one_off.path, "switch", "--quiet", "-c", BRANCH)
    fixture_git(one_off.path, "commit", "--quiet", "--allow-empty", "-m", "first")
    fixture_git(one_off.path, "reset", "--quiet", "--hard", one_off.pinned)
    with pytest.raises(BundleRefused):
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH), exchange, BRANCH, RUN_ID, one_off.pinned)
    assert not any(ref.startswith("refs/heads/") for ref in refs(trusted.path))


def test_branch_and_run_id_are_validated(trusted, exchange):
    for branch in ("main", "claude/autodev-339-x/../y", "claude/autodev-0-x", "refs/heads/claude/autodev-1-x"):
        with pytest.raises(ValueError):
            trusted.import_bundle(exchange / "x.bundle", exchange, branch, RUN_ID, "a" * 40)
    with pytest.raises(ValueError):
        trusted.import_bundle(exchange / "x.bundle", exchange, BRANCH, f"{RUN_ID}\n", "a" * 40)
    with pytest.raises(ValueError):
        trusted.import_bundle(exchange / "x.bundle", exchange, BRANCH, RUN_ID, "HEAD")


@pytest.mark.parametrize("path,target,escapes", [
    ("link", "README.md", False), ("docs/link", "../README.md", False), ("a/b/link", "../../x", False),
    ("link", "../x", True), ("docs/link", "../../x", True), ("link", "/etc", True), ("link", "", True),
    ("link", "sub/../x", True), ("docs/link", "./../x", False)])
def test_symbolic_link_targets(path, target, escapes):
    assert repos._link_escapes(path, target.encode()) is escapes


# --- the runner's drift check -------------------------------------------------------------------

@pytest.fixture
def installation(tmp_path, github):
    root = tmp_path / "installation"
    fixture_git(tmp_path, "clone", "--quiet", str(github), str(root))
    return root


def test_working_branch_changing_src_schedules_does_not_change_what_drift_compares(
        trusted, installation, tmp_path, exchange, monkeypatch, github):
    paths = ["src/schedules/runner.py", *drift.TREES]
    pinned = trusted.pinned()
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"src/schedules/runner.py": "print('rewritten by a session')\n"})
    trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{pinned}"), exchange, BRANCH, RUN_ID, one_off.pinned)

    assert trusted.pinned() == pinned
    assert drift.check(installation, trusted.pinned(), paths, repository=trusted.path) == []
    monkeypatch.setattr(repos, "default_url", lambda repository: str(github))
    config = {**autodev_config.DEFAULTS, "clone": str(trusted.path)}
    checked = runner.default_drift(installation, config)
    assert checked == {"pinned": pinned, "runner_commit": pinned, "changed": [], "pruned": []}  # pruned: #347


def test_docs_only_main_change_unknown_to_the_installation_is_no_drift(trusted, installation, github, monkeypatch):
    write(github, {"README.md": "# docs only\n"})
    ahead = commit_all(github)
    assert trusted.fetch() == ahead
    assert drift.check(installation, ahead, ["src/schedules/runner.py", *drift.TREES], repository=trusted.path) == []

    write(github, {"agents/a/system_prompt.mdc": "next\n"})
    moved = commit_all(github)
    monkeypatch.setattr(repos, "default_url", lambda repository: str(github))
    config = {**autodev_config.DEFAULTS, "clone": str(trusted.path)}
    result = sequence.check_drift(runner.Context(installation=installation, directory=installation.parent), config)
    assert trusted.pinned() == moved
    assert result["status"] == "failed: infra" and result["reason"] == "runner drift: agents/a/system_prompt.mdc"


def test_os_error_from_the_trusted_repository_is_a_trusted_repository_failure(tmp_path, installation):
    def unreadable(path):
        raise PermissionError(13, "Permission denied", str(tmp_path / "runner"))
    result = sequence.check_drift(runner.Context(installation=installation, directory=tmp_path, drift_check=unreadable),
                                  dict(autodev_config.DEFAULTS))
    assert result["status"] == "failed: infra"
    assert result["reason"].startswith("cli: trusted repository: ") and "Permission denied" in result["reason"]


def test_fetch_failure_is_a_network_failure(tmp_path, installation, monkeypatch):
    monkeypatch.setattr(repos, "default_url", lambda repository: str(tmp_path / "missing"))
    config = {**autodev_config.DEFAULTS, "clone": str(tmp_path / "runner" / "trusted")}
    result = sequence.check_drift(runner.Context(installation=installation, directory=tmp_path), config)
    assert result["status"] == "failed: infra" and result["reason"].startswith("network: fetch of main")


# --- environments -------------------------------------------------------------------------------

class FakeBuilder:
    def __init__(self):
        self.calls = []

    def __call__(self, venv: Path, files):
        self.calls.append((venv, dict(files)))
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\n")


def test_environment_is_keyed_by_the_dependency_files_built_once_and_read_only(trusted, tmp_path, github):
    builder = FakeBuilder()
    runner_dir = tmp_path / "runner"
    first = trusted.environment(trusted.pinned(), runner_dir, builder)
    again = trusted.environment(trusted.pinned(), runner_dir, builder)

    assert first == again and len(builder.calls) == 1
    assert builder.calls[0][1] == {"requirements.txt": b"pytest\n", "pyproject.toml": b"[project]\n"}
    assert first.parent == (runner_dir / "venvs").resolve()
    for path in [first, *first.rglob("*")]:
        assert not stat.S_IMODE(path.lstat().st_mode) & 0o222, path
    write(github, {"README.md": "# docs only\n"})
    commit_all(github)
    assert trusted.environment(trusted.fetch(), runner_dir, builder) == first  # same dependency files
    write(github, {"requirements.txt": "pytest\nhttpx\n"})
    commit_all(github)
    assert trusted.environment(trusted.fetch(), runner_dir, builder) != first and len(builder.calls) == 2


def test_incomplete_environment_is_rebuilt(trusted, tmp_path):
    builder = FakeBuilder()
    runner_dir = tmp_path / "runner"
    venv = trusted.environment(trusted.pinned(), runner_dir, builder)
    for path in [venv, *venv.rglob("*")]:
        path.chmod(0o700)
    (venv / repos.COMPLETE_MARKER).unlink()

    assert trusted.environment(trusted.pinned(), runner_dir, builder) == venv and len(builder.calls) == 2
    assert (venv / repos.COMPLETE_MARKER).read_text().strip() == repos.dependency_hash(
        trusted.dependency_files(trusted.pinned()))


def test_environment_directory_must_lie_outside_the_trusted_repository_and_documents(trusted):
    with pytest.raises(RepositoryError, match="outside the trusted repository"):
        trusted.environment(trusted.pinned(), trusted.path / "venvs", FakeBuilder())
    with pytest.raises(ValueError, match="outside ~/Documents"):
        trusted.environment(trusted.pinned(), Path.home() / "Documents" / "autodev-venvs", FakeBuilder())


# --- review round 1: bounds, base, quarantine, promotion, environments ----------------------------

def link_object(one_off: Path, target: bytes, path: str = "docs/link") -> None:
    """Stage a symbolic link whose target is ``target``, however long (no file system link)."""
    obj = subprocess.run(["git", "-C", str(one_off), "hash-object", "-w", "--stdin"], input=target,
                         env=fixture_environment(), capture_output=True, check=True).stdout.decode().strip()
    fixture_git(one_off, "update-index", "--add", "--cacheinfo", f"120000,{obj},{path}")


def test_symbolic_link_with_a_huge_target_is_refused_by_size_without_reading_it(trusted, tmp_path, exchange,
                                                                                monkeypatch):
    one_off = session(trusted, tmp_path)
    fixture_git(one_off.path, "switch", "--quiet", "-c", BRANCH)
    link_object(one_off.path, b"a" * (1024 * 1024))
    fixture_git(one_off.path, "commit", "--quiet", "-m", "bomb")
    made = bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}")
    assert made.stat().st_size < 64 * 1024
    read = []
    real_bounded = gitsafe.git_bounded

    def recording_bounded(repository, *args, **kwargs):
        output = real_bounded(repository, *args, **kwargs)
        read.append((args, kwargs["max_output"], len(output)))
        return output
    monkeypatch.setattr(gitsafe, "git_bounded", recording_bounded)
    real_git = gitsafe.git
    monkeypatch.setattr(gitsafe, "git", lambda repository, *args, **kwargs: (
        pytest.fail("cat-file blob of a link") if args[:2] == ("cat-file", "blob") else
        real_git(repository, *args, **kwargs)))

    with pytest.raises(BundleRefused, match="1048576 bytes, more than 4096") as refused:
        trusted.import_bundle(made, exchange, BRANCH, RUN_ID, one_off.pinned)

    assert refused.value.quarantine_ref == f"{repos.QUARANTINE}/{RUN_ID}/1"
    assert not any("--batch" in args for args, _, _ in read)  # only --batch-check ran on links
    assert all(size <= 4096 for _, _, size in read)
    assert not any(ref.startswith("refs/heads/") for ref in refs(trusted.path))


def test_symbolic_link_targets_within_the_limit_are_read_in_one_batch(trusted, tmp_path, exchange, monkeypatch):
    one_off = session(trusted, tmp_path)
    fixture_git(one_off.path, "switch", "--quiet", "-c", BRANCH)
    link_object(one_off.path, b"../README.md", "docs/one")
    link_object(one_off.path, b"x" * 4096, "docs/two")
    fixture_git(one_off.path, "commit", "--quiet", "-m", "links")
    calls = []
    real_bounded = gitsafe.git_bounded
    monkeypatch.setattr(gitsafe, "git_bounded", lambda repository, *args, **kwargs: (
        calls.append(args), real_bounded(repository, *args, **kwargs))[1])

    imported = trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}"), exchange,
                                     BRANCH, RUN_ID, one_off.pinned)

    assert len(imported.commits) == 1
    assert [args[:2] for args in calls] == [("diff-tree", "--stdin"), ("cat-file", "--batch-check"),
                                            ("cat-file", "--batch")]


def test_too_many_commits_are_refused_before_the_walk(trusted, tmp_path, exchange, monkeypatch):
    monkeypatch.setattr(repos, "MAX_IMPORT_COMMITS", 3)
    one_off = session(trusted, tmp_path)
    fixture_git(one_off.path, "switch", "--quiet", "-c", BRANCH)
    for number in range(4):
        fixture_git(one_off.path, "commit", "--quiet", "--allow-empty", "-m", f"c{number}")
    walked = []
    real_bounded = gitsafe.git_bounded
    monkeypatch.setattr(gitsafe, "git_bounded", lambda *args, **kwargs: (walked.append(args),
                                                                         real_bounded(*args, **kwargs))[1])

    with pytest.raises(BundleRefused, match="4 commits, more than 3") as refused:
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}"), exchange, BRANCH,
                              RUN_ID, one_off.pinned)
    assert refused.value.quarantine_ref and walked == []


def test_every_commit_of_the_batched_walk_is_checked_not_only_the_head(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    fixture_git(one_off.path, "switch", "--quiet", "-c", BRANCH)
    write(one_off.path, {"docs/a.md": "a\n"})
    commit_all(one_off.path, "first")
    symlink("/etc/passwd")(one_off.path)
    fixture_git(one_off.path, "commit", "--quiet", "-m", "middle")
    fixture_git(one_off.path, "rm", "--quiet", "docs/link")
    commit_all(one_off.path, "removes it again")
    with pytest.raises(BundleRefused, match="outside the tree"):
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}"), exchange, BRANCH,
                              RUN_ID, one_off.pinned)


def test_import_has_an_overall_deadline(trusted, tmp_path, exchange, monkeypatch):
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/new.md": "new\n"})
    made = bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}")
    clock = iter([0.0, *[0.0] * 6, 10_000.0, 10_000.0, 10_000.0, 10_000.0])
    monkeypatch.setattr(repos.time, "monotonic", lambda: next(clock, 10_000.0))
    with pytest.raises(BundleRefused, match="took longer than"):
        trusted.import_bundle(made, exchange, BRANCH, RUN_ID, one_off.pinned)
    assert not any(ref.startswith("refs/heads/") for ref in refs(trusted.path))


def test_main_moving_between_session_and_import_does_not_refuse_the_bundle(trusted, tmp_path, exchange, github):
    one_off = session(trusted, tmp_path)
    head = work(one_off.path, {"docs/new.md": "new\n"})
    made = bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}")
    write(github, {"README.md": "# moved\n"})
    moved = commit_all(github)
    assert trusted.fetch() == moved != one_off.pinned

    imported = trusted.import_bundle(made, exchange, BRANCH, RUN_ID, one_off.pinned)

    assert imported.head == head and imported.commits == (head,)
    assert refs(trusted.path) == {repos.PINNED_REF: moved, f"refs/heads/{BRANCH}": head}


def test_session_base_must_be_an_ancestor_of_pinned(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    head = work(one_off.path, {"docs/new.md": "new\n"})
    made = bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}")
    trusted.import_bundle(made, exchange, BRANCH, RUN_ID, one_off.pinned)
    for base in (head, "c" * 40):  # an imported session commit; an unknown object
        with pytest.raises(RepositoryError, match="not an ancestor of pinned"):
            trusted.import_bundle(made, exchange, BRANCH, RUN_ID, base)


def test_promotion_is_fast_forward_only_and_reports_the_commits_it_added(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    first = work(one_off.path, {"docs/one.md": "one\n"})
    trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}"), exchange, BRANCH,
                          RUN_ID, one_off.pinned)
    write(one_off.path, {"docs/two.md": "two\n"})
    second = commit_all(one_off.path)
    again = trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}", name="2.bundle"),
                                  exchange, BRANCH, RUN_ID, one_off.pinned)
    assert again.commits == (second,) and first != second
    same = trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}", name="3.bundle"),
                                 exchange, BRANCH, RUN_ID, one_off.pinned)
    assert same.commits == ()

    fixture_git(one_off.path, "reset", "--quiet", "--hard", one_off.pinned)
    write(one_off.path, {"docs/rewritten.md": "x\n"})
    commit_all(one_off.path)
    with pytest.raises(BundleRefused, match="not a fast-forward") as rewritten:
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}", name="4.bundle"),
                              exchange, BRANCH, RUN_ID, one_off.pinned)
    assert refs(trusted.path)[f"refs/heads/{BRANCH}"] == second
    assert rewritten.value.quarantine_ref in refs(trusted.path)


def test_prune_quarantine_removes_refs_files_and_objects_of_old_runs_only(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    fixture_git(one_off.path, "switch", "--quiet", "-c", BRANCH)
    gitlink(one_off.path)
    fixture_git(one_off.path, "commit", "--quiet", "-m", "planted")
    planted = fixture_git(one_off.path, "rev-parse", "HEAD")
    made = bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}")
    recent_run = "20261112T020000Z-def456"
    for run_id in (RUN_ID, recent_run):
        with pytest.raises(BundleRefused, match="gitlink"):
            trusted.import_bundle(made, exchange, BRANCH, run_id, one_off.pinned)
        with pytest.raises(BundleRefused, match="not the expected"):
            trusted.import_bundle(made, exchange, "claude/autodev-339-other", run_id, one_off.pinned)
    files = trusted.path / repos.QUARANTINE_FILES
    assert sorted(os.listdir(files)) == [RUN_ID, recent_run]

    now = datetime(2026, 11, 13, tzinfo=timezone.utc)  # 31 days after RUN_ID, 1 day after recent_run
    assert trusted.prune_quarantine(now) == [f"{repos.QUARANTINE}/{RUN_ID}/1", str(files / RUN_ID)]

    assert set(refs(trusted.path)) == {repos.PINNED_REF, f"{repos.QUARANTINE}/{recent_run}/1"}
    assert sorted(os.listdir(files)) == [recent_run]
    assert trusted.prune_quarantine(now) == []
    assert trusted.prune_quarantine(now + timedelta(days=30))
    assert set(refs(trusted.path)) == {repos.PINNED_REF} and os.listdir(files) == []
    exists = subprocess.run(["git", "-C", str(trusted.path), "cat-file", "-e", planted], env=fixture_environment(),
                            capture_output=True)
    assert exists.returncode != 0  # gc dropped the objects only the quarantine kept


def test_git_failure_keeps_the_tail_of_stderr_without_the_grafts_hint(trusted):
    with pytest.raises(gitsafe.GitError) as failed:
        gitsafe.git(trusted.path, "rev-parse", "--verify", "--end-of-options", "no-such-ref")
    assert "Needed a single revision" in str(failed.value) and "graft" not in str(failed.value)
    long = subprocess.CompletedProcess(["fetch"], 1, b"", b"hint: noise\n" * 200 + b"fatal: the real reason")
    assert str(gitsafe.failure(("fetch",), long)).endswith("fatal: the real reason")


def test_bounded_git_kills_a_command_that_prints_too_much(trusted):
    pinned = trusted.pinned()
    with pytest.raises(gitsafe.OutputTooLarge):
        gitsafe.git_bounded(trusted.path, "cat-file", "--batch", input=f"{pinned}\n".encode(), max_output=10)
    assert gitsafe.git_bounded(trusted.path, "cat-file", "--batch-check", input=f"{pinned}\n".encode(),
                               max_output=100).startswith(pinned.encode())


def test_environment_refuses_an_imported_session_head(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    head = work(one_off.path, {"requirements.txt": "--index-url https://evil.example/simple\nevilpkg\n"})
    trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}"), exchange, BRANCH,
                          RUN_ID, one_off.pinned)
    builder = FakeBuilder()
    with pytest.raises(RepositoryError, match="not a commit of main"):
        trusted.environment(head, tmp_path / "runner", builder)
    with pytest.raises(RepositoryError, match="not a commit of main"):
        trusted.dependency_files(head)
    assert builder.calls == []


@pytest.mark.parametrize("planted", ["key", "venvs"])
def test_symlinked_environment_directories_are_refused(trusted, tmp_path, planted):
    runner_dir = tmp_path / "runner"
    key = repos.dependency_hash(trusted.dependency_files(trusted.pinned()))
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / repos.COMPLETE_MARKER).write_text(key + "\n")
    if planted == "key":
        (runner_dir / "venvs").mkdir(parents=True)
        os.symlink(attacker, runner_dir / "venvs" / key[:32])
    else:
        (attacker / key[:32]).mkdir()
        (attacker / key[:32] / repos.COMPLETE_MARKER).write_text(key + "\n")
        os.symlink(attacker, runner_dir / "venvs")
    builder = FakeBuilder()
    with pytest.raises(RepositoryError, match="symbolic link"):
        trusted.environment(trusted.pinned(), runner_dir, builder)
    assert builder.calls == [] and stat.S_IMODE(attacker.stat().st_mode) & 0o200


def test_environment_is_built_in_a_private_sibling_and_renamed_into_place(trusted, tmp_path):
    runner_dir = tmp_path / "runner"
    seen = []

    def builder(build: Path, files):
        venvs = (runner_dir / "venvs").resolve()
        seen.append(build)
        assert build.parent == venvs and build.name.startswith(".build-")
        assert not any(name for name in os.listdir(venvs) if not name.startswith(".build-") and not name.endswith(".lock"))
        (build / "bin").mkdir()
        (build / "bin" / "tool").write_text(f"#!{build}/bin/python\n")
        (build / "pyvenv.cfg").write_text(f"command = python -m venv {build}\n")

    venv = trusted.environment(trusted.pinned(), runner_dir, builder)

    assert sorted(os.listdir(venv.parent)) == [venv.name, f"{venv.name}.lock"] and not seen[0].exists()
    assert (venv / "bin" / "tool").read_text() == f"#!{venv}/bin/python\n"
    assert str(seen[0]) not in (venv / "pyvenv.cfg").read_text()


def test_default_builder_runs_with_a_minimal_environment(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(repos.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    monkeypatch.setenv("PIP_INDEX_URL", "https://evil.example/simple")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    repos.default_builder(tmp_path / "venv", {"requirements.txt": b"pytest\n"})
    assert len(calls) == 2
    for _, kwargs in calls:
        environment = kwargs["env"]
        assert "PIP_INDEX_URL" not in environment and "PYTHONPATH" not in environment
        assert environment["PIP_CONFIG_FILE"] == os.devnull and environment["PYTHONNOUSERSITE"] == "1"


# --- review round 2 ------------------------------------------------------------------------------

def test_a_git_timeout_inside_the_import_refuses_the_bundle(trusted, tmp_path, exchange, monkeypatch):
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/new.md": "new\n"})
    made = bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}")
    real = gitsafe.git

    def slow_rev_list(repository, *args, **kwargs):
        if args[:2] == ("rev-list", "--count"):
            raise gitsafe.GitTimeout("git rev-list could not run: timed out")
        return real(repository, *args, **kwargs)
    monkeypatch.setattr(repos.gitsafe, "git", slow_rev_list)

    with pytest.raises(BundleRefused, match="took longer than") as refused:
        trusted.import_bundle(made, exchange, BRANCH, RUN_ID, one_off.pinned)
    assert refused.value.quarantine_ref and refused.value.quarantine_ref in refs(trusted.path)


def test_concurrent_callers_build_one_environment_under_the_key_lock(trusted, tmp_path):
    runner_dir = tmp_path / "runner"
    started, release, calls = threading.Event(), threading.Event(), []

    def slow(build: Path, files):
        calls.append(build)
        started.set()
        release.wait(10)
        (build / "bin").mkdir()
        (build / "bin" / "python").write_text("#!/bin/sh\n")
    results = []
    first = threading.Thread(target=lambda: results.append(trusted.environment(trusted.pinned(), runner_dir, slow)))
    first.start()
    assert started.wait(10)
    second = threading.Thread(target=lambda: results.append(trusted.environment(trusted.pinned(), runner_dir, slow)))
    second.start()
    second.join(0.5)
    assert second.is_alive()  # waits for the key lock instead of building or removing anything
    release.set()
    first.join(10)
    second.join(10)
    assert len(calls) == 1 and len(results) == 2 and results[0] == results[1]
