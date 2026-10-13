"""The trusted repository, one-off session repositories, bundle import and environments (#339).

Every test builds throwaway repositories under ``tmp_path``: a local "GitHub" repository stands
in for the remote (its absolute path is the explicit URL), and sessions run git in their one-off
repository with the fixture environment. Nothing reaches the network.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest

from src.schedules import config as autodev_config
from src.schedules import drift, gitsafe, repos, runner
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
    real_run = subprocess.run

    def recording_run(command, **kwargs):
        if "GIT_AUTHOR_NAME" not in kwargs.get("env", {}):  # not the session's own git (fixture_git)
            calls.append((command, kwargs))
        return real_run(command, **kwargs)
    monkeypatch.setattr(gitsafe.subprocess, "run", recording_run)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    trusted = TrustedRepository(tmp_path / "runner" / "IEZhu-Agents", str(github))
    trusted.ensure().fetch()
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/new.md": "new\n"})
    trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}"), exchange, BRANCH, RUN_ID)

    assert calls
    for command, kwargs in calls:
        assert command[1:12] == ["--no-pager", "--no-replace-objects", "--literal-pathspecs",
                                 "-c", "core.quotePath=false", "-c", "core.fsmonitor=false",
                                 "-c", f"core.hooksPath={os.devnull}", "-c", "core.commitGraph=false"]
        assert "transfer.fsckObjects=true" in command and "gc.auto=0" in command
        assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1" and kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "GIT_DIR" not in kwargs["env"]
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

    imported = trusted.import_bundle(made, exchange, BRANCH, RUN_ID)
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
                                     BRANCH, RUN_ID)

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
                              RUN_ID)

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
                              RUN_ID)

    orphan = session(trusted, tmp_path, "orphan")
    fixture_git(orphan.path, "switch", "--quiet", "--orphan", "claude/autodev-339-orphan")
    write(orphan.path, {"x.md": "x\n"})
    commit_all(orphan.path)
    with pytest.raises(BundleRefused, match="does not descend from pinned") as unrelated:
        trusted.import_bundle(bundle(orphan.path, exchange, "claude/autodev-339-orphan", name="orphan.bundle"),
                              exchange, "claude/autodev-339-orphan", RUN_ID)

    assert merged.value.quarantine_ref.endswith("/1") and unrelated.value.quarantine_ref.endswith("/2")
    assert not any(ref.startswith("refs/heads/") for ref in refs(trusted.path))


def test_bundle_naming_another_branch_or_two_refs_is_refused_before_any_fetch(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/new.md": "new\n"}, branch="claude/autodev-339-other")
    with pytest.raises(BundleRefused, match="not the expected") as other:
        trusted.import_bundle(bundle(one_off.path, exchange, "claude/autodev-339-other", f"^{one_off.pinned}"),
                              exchange, BRANCH, RUN_ID)
    fixture_git(one_off.path, "branch", BRANCH)
    with pytest.raises(BundleRefused, match="names 2 refs") as two:
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, "claude/autodev-339-other",
                                     f"^{one_off.pinned}", name="two.bundle"), exchange, BRANCH, RUN_ID)

    assert other.value.quarantine_ref is None and two.value.quarantine_ref is None
    assert set(refs(trusted.path)) == {repos.PINNED_REF}


def test_symlinked_or_outside_or_oversized_bundle_file_is_refused(trusted, tmp_path, exchange, monkeypatch):
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/new.md": "new\n"})
    real = bundle(one_off.path, tmp_path, BRANCH, f"^{one_off.pinned}", name="real.bundle")
    os.symlink(real, exchange / "linked.bundle")
    with pytest.raises(BundleRefused, match="symbolic link"):
        trusted.import_bundle(exchange / "linked.bundle", exchange, BRANCH, RUN_ID)
    with pytest.raises(BundleRefused, match="not directly inside the exchange directory"):
        trusted.import_bundle(real, exchange, BRANCH, RUN_ID)
    with pytest.raises(BundleRefused, match="not directly inside the exchange directory"):
        trusted.import_bundle(Path("../real.bundle"), exchange, BRANCH, RUN_ID)
    os.mkfifo(exchange / "fifo.bundle")
    with pytest.raises(BundleRefused, match="not a regular file"):
        trusted.import_bundle(exchange / "fifo.bundle", exchange, BRANCH, RUN_ID)
    (exchange / "copy.bundle").write_bytes(real.read_bytes())
    monkeypatch.setattr(repos, "MAX_BUNDLE_BYTES", 10)
    with pytest.raises(BundleRefused, match="larger than"):
        trusted.import_bundle(exchange / "copy.bundle", exchange, BRANCH, RUN_ID)
    assert set(refs(trusted.path)) == {repos.PINNED_REF}


def test_bundle_with_prerequisites_missing_from_the_trusted_repository_is_refused(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    work(one_off.path, {"docs/one.md": "one\n"})
    first = fixture_git(one_off.path, "rev-parse", "HEAD")
    write(one_off.path, {"docs/two.md": "two\n"})
    commit_all(one_off.path)
    with pytest.raises(BundleRefused, match="prerequisites missing"):
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{first}"), exchange, BRANCH, RUN_ID)
    assert set(refs(trusted.path)) == {repos.PINNED_REF}


def test_bundle_without_new_commits_is_refused(trusted, tmp_path, exchange):
    one_off = session(trusted, tmp_path)
    fixture_git(one_off.path, "switch", "--quiet", "-c", BRANCH)
    fixture_git(one_off.path, "commit", "--quiet", "--allow-empty", "-m", "first")
    fixture_git(one_off.path, "reset", "--quiet", "--hard", one_off.pinned)
    with pytest.raises(BundleRefused):
        trusted.import_bundle(bundle(one_off.path, exchange, BRANCH), exchange, BRANCH, RUN_ID)
    assert not any(ref.startswith("refs/heads/") for ref in refs(trusted.path))


def test_branch_and_run_id_are_validated(trusted, exchange):
    for branch in ("main", "claude/autodev-339-x/../y", "claude/autodev-0-x", "refs/heads/claude/autodev-1-x"):
        with pytest.raises(ValueError):
            trusted.import_bundle(exchange / "x.bundle", exchange, branch, RUN_ID)
    with pytest.raises(ValueError):
        trusted.import_bundle(exchange / "x.bundle", exchange, BRANCH, f"{RUN_ID}\n")


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
    trusted.import_bundle(bundle(one_off.path, exchange, BRANCH, f"^{pinned}"), exchange, BRANCH, RUN_ID)

    assert trusted.pinned() == pinned
    assert drift.check(installation, trusted.pinned(), paths) == []
    monkeypatch.setattr(repos, "default_url", lambda repository: str(github))
    config = {**autodev_config.DEFAULTS, "clone": str(trusted.path)}
    checked = runner.default_drift(installation, config)
    assert checked == {"pinned": pinned, "runner_commit": pinned, "changed": []}


def test_pinned_commit_unknown_to_the_installation_is_runner_drift(trusted, installation, github, monkeypatch):
    write(github, {"src/schedules/runner.py": "print('next')\n"})
    ahead = commit_all(github)
    assert trusted.fetch() == ahead

    assert drift.check(installation, ahead, ["src/schedules/runner.py"]) == [f"pinned {ahead} is not in the installation"]
    monkeypatch.setattr(repos, "default_url", lambda repository: str(github))
    config = {**autodev_config.DEFAULTS, "clone": str(trusted.path)}
    result = runner._dry_run(runner.Context(installation=installation, directory=installation.parent),
                             config, None, None)
    assert result["status"] == "failed: infra" and result["reason"].startswith("runner drift: pinned ")


def test_fetch_failure_is_a_network_failure(tmp_path, installation, monkeypatch):
    monkeypatch.setattr(repos, "default_url", lambda repository: str(tmp_path / "missing"))
    config = {**autodev_config.DEFAULTS, "clone": str(tmp_path / "runner" / "trusted")}
    result = runner._dry_run(runner.Context(installation=installation, directory=tmp_path), config, None, None)
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
