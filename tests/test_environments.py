"""Versioned Python environments (#181): what an update installs, building, switching, pruning.

No network and no real package installs: ``python -m venv``, ``pip`` and the base
interpreter's version probe run through an injected fake that creates the files a
real build leaves.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src import environments
from tests.test_daemon_update_dependencies import MANIFEST, fake_git

VERSION = (3, 13, 1)
# The project the update fixtures install: python-dotenv at runtime, mcp from requirements.txt only.
PROJECT = MANIFEST.replace('dependencies = ["mcp==1.28.1", "python-dotenv"]', 'dependencies = ["python-dotenv"]')


class FakeBuilder:
    """Stands in for ``self_update._run_command`` running the version probe, ``venv`` and ``pip``."""

    def __init__(self, *, fail=None, hang=None, raises=None, version="3.13.1", stdout="", versions=None):
        self.calls, self.envs = [], []
        self.fail, self.hang, self.raises, self.version, self.stdout = fail, hang, raises, version, stdout
        self.versions = versions or {}  # interpreter path -> the version its probe prints

    @property
    def builds(self):
        """The venv and pip commands, without version probes."""
        return [call for call in self.calls if call[1] == "-m"]

    def __call__(self, args, cwd=None, timeout=None, env=None):
        self.calls.append(list(args))
        self.envs.append(env)
        assert timeout and timeout > 0
        if args[1] == "-c":  # the base interpreter's version probe
            return subprocess.CompletedProcess(args, 0, self.versions.get(args[0], self.version) + "\n", "")
        step = args[2]
        if step == self.raises:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        if step == self.hang:
            raise subprocess.TimeoutExpired(args, timeout)
        if step == "venv":
            assert Path(cwd) == Path(args[3])  # in the new environment's directory, not the checkout
            make_environment(Path(args[3]), record=False, base=Path(args[0]))  # pyvenv.cfg names its base
        elif step == "pip":
            environment = Path(args[0]).parents[1]
            assert Path(cwd) == environment and not (environment / environments.RECORD).exists()
        else:
            raise AssertionError(f"unexpected command {args}")
        if step == self.fail:
            return subprocess.CompletedProcess(args, 1, self.stdout, "ERROR: ResolutionImpossible")
        return subprocess.CompletedProcess(args, 0, "", "")


def make_environment(directory, *, record=True, packages=(), base=None):
    """A venv as ``python -m venv`` leaves it; *record* marks it a complete build.

    *packages* are names (version 1.0) or ``(name, version)`` pairs; one replaces an
    installed version of the same name.
    """
    python = Path(environments.python_path(directory))
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("interpreter")
    executable = base or directory / "base-python"
    (directory / "pyvenv.cfg").write_text(f"home = {Path(executable).parent}\nversion = 3.13.1\n"
                                          f"executable = {executable}\n")
    site = directory / ("Lib/site-packages" if environments.WINDOWS else "lib/python3.13/site-packages")
    site.mkdir(parents=True, exist_ok=True)
    for package in packages:
        name, version = package if isinstance(package, tuple) else (package, "1.0")
        for previous in site.glob(f"{name}-*.dist-info"):
            shutil.rmtree(previous)
        info = site / f"{name}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    if record:
        (directory / environments.RECORD).write_text(json.dumps({"requirements_sha256": "x"}))
    return directory


# Windows refuses dependency delivery into the directory setup created (see
# test_windows_refuses_dependency_changes_on_the_setup_directory), so tests that
# deliver into it, or reach a check behind that refusal, run on POSIX only; the
# `linked` fixture builds on every platform.
posix_delivery = pytest.mark.skipif(os.name == "nt", reason="Windows refuses delivery into the setup directory")


@pytest.fixture
def root(tmp_path):
    """An installation whose ``.venv`` is the directory setup created, on a real base interpreter file."""
    base = tmp_path / "base" / "bin" / "python3"
    base.parent.mkdir(parents=True)
    base.write_text("base interpreter")
    installation = tmp_path / "install"
    # httpx is there only as a dependency of mcp, as in a real installation.
    make_environment(installation / ".venv", record=False, packages=("httpx", "mcp"), base=base)
    return installation


def python(root):
    return environments.python_path(root / ".venv")


def needed(root, *, changed=("requirements.txt",), before=PROJECT, after=PROJECT, requirements="mcp==2\n",
           interpreter=None, run=None):
    git = fake_git(before, after, changed=changed, requirements=requirements)
    return environments.needed(str(root), git, "old", "target", interpreter or python(root), run=run or FakeBuilder())


# --- what an update installs --------------------------------------------------------------------

def test_the_environment_id_names_the_python_and_the_requirements():
    first = environments.environment_id("mcp==2\n", (3, 13, 1))
    assert environments.valid_id(first) and first.startswith("py313-")
    assert environments.environment_id("mcp==2\n", (3, 13, 9)) == first  # a patch release shares packages
    assert environments.environment_id("mcp==2\n", (3, 12, 1)).startswith("py312-")
    assert environments.environment_id("mcp==3\n", (3, 13, 1)) != first


def test_unchanged_or_lock_only_manifests_need_no_environment(root):
    assert needed(root, changed=()) is None
    assert needed(root, changed=("uv.lock",)) is None


@posix_delivery
def test_requirements_and_runtime_dependencies_are_installed(root):
    found = needed(root)
    assert found.version == VERSION and found.base == str(root.parent / "base/bin/python3")
    lines = found.requirements.splitlines()
    # The code declares its runtime dependencies in pyproject.toml; requirements.txt may lag.
    assert lines[0] == "mcp==2" and "python-dotenv" in lines


@posix_delivery
def test_a_comment_only_requirements_edit_does_not_change_the_environment_id(root):
    """#364: the id hashes the requirement lines, not comments or blank lines."""
    plain = needed(root, requirements="mcp==2\nnumpy\n")
    commented = needed(root, requirements="# pinned\nmcp==2   # the server\n\nnumpy\n")
    assert plain.requirements == commented.requirements


@posix_delivery
def test_an_extra_installed_only_as_a_dependency_is_carried(root):
    """The rule: both revisions declare the extra and every distribution of it is installed,
    also as a dependency of another package (httpx comes with mcp here). A new extra is not."""
    after = PROJECT + 'thread-status = ["claude-agent-sdk==0.2.168"]\n'
    lines = needed(root, after=after).requirements.splitlines()
    assert "httpx>=0.28,<0.29" in lines and "claude-agent-sdk==0.2.168" not in lines


@posix_delivery
def test_a_carried_extra_installs_the_target_pins(root):
    after = PROJECT.replace("httpx>=0.28,<0.29", "httpx>=1")
    lines = needed(root, changed=("requirements.txt", "pyproject.toml"), after=after).requirements.splitlines()
    assert "httpx>=1" in lines and "httpx>=0.28,<0.29" not in lines


@posix_delivery
def test_an_extra_with_a_missing_distribution_is_not_carried(root):
    redis = PROJECT + 'local-inference-redis = ["httpx>=0.28,<0.29", "redis>=7.0.0,<7.1"]\n'
    lines = needed(root, before=redis, after=redis).requirements.splitlines()
    assert "redis>=7.0.0,<7.1" not in lines
    assert "# optional extra local-inference-redis, installed in the previous environment" not in lines


@posix_delivery
def test_identical_requirements_are_listed_once(root):
    lines = needed(root, requirements="mcp==2\npython-dotenv\n").requirements.splitlines()
    assert lines.count("python-dotenv") == 1


@pytest.mark.parametrize("requirements, declared", [
    ("mcp==2\n", '["mcp==1.28.1"]'),
    ("Foo_Bar==1\n", '["foo-bar==2"]'),  # one distribution by PEP 503 normalization
    ("mcp===2\n", '["mcp==3"]'),
    ("uvicorn==0.49.0\n", '["uvicorn[standard]==0.48.0"]'),  # a line with extras pins too
    ("foo==2.0\n", '["foo[a,b]==1.0"]'),
])
@posix_delivery
def test_two_different_exact_pins_of_one_distribution_are_refused(root, requirements, declared):
    project = PROJECT.replace('dependencies = ["python-dotenv"]', f"dependencies = {declared}")
    with pytest.raises(environments.Refused, match="pin .* to different versions") as refused:
        needed(root, before=project, after=project, requirements=requirements)
    assert requirements.strip() in str(refused.value) and declared[2:-2] in str(refused.value)


def test_the_repository_manifests_with_every_extra_carried_merge_into_one_specifier_set():
    """requirements.txt pins pytest>=7.0.0 and anthropic>=0.18.0; evals and signing-tests ask for more."""
    import tomllib
    from src.daemon.update_dependencies import requirement_lines
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    text = environments.requirements_text(requirement_lines((root / "requirements.txt").read_text(encoding="utf-8")),
                                          project["dependencies"], extras)
    lines = text.splitlines()
    assert "pytest>=7.0.0,>=8.0" in lines and "anthropic>=0.18.0,>=0.40" in lines
    assert [line for line in lines if line.startswith(("pytest>", "pytest=", "anthropic"))] == \
        ["pytest>=7.0.0,>=8.0", "anthropic>=0.18.0,>=0.40"]


@pytest.mark.parametrize("first, second", [
    ("uvicorn[standard]==0.48.0", "uvicorn==0.48.0"),
    ('numpy<2; python_version < "3.12"', "numpy>=1.24"),
    ("git+https://example.invalid/a.git", "git+https://example.invalid/b.git"),
    ("pkg @ https://example.invalid/pkg-1.whl", "pkg>=1"),
])
def test_lines_with_extras_markers_or_urls_stay_as_written(first, second):
    lines = environments.requirements_text([first], [second], {}).splitlines()
    assert first in lines and second in lines


@pytest.mark.parametrize("line", ["-rbase.txt", "-r base.txt", "-cx", "-e.", "--requirement=x", "-f x",
                                  "--index-url https://example.invalid/simple", "--hash=sha256:00",
                                  "./pkg", "/opt/pkg", "~/pkg", "C:\\pkg", "c:/pkg", "numpy>=1.24 \\"])
@posix_delivery
def test_pip_options_local_paths_and_continuations_in_requirements_are_refused(root, line):
    with pytest.raises(environments.Refused, match="pip option|local path|continues on the next line"):
        needed(root, requirements=f"mcp==2\n{line}\n")


@pytest.mark.parametrize("line", ["bad package", "numpy 1.24", "numpy>=1.24 extra", "numpy>=1.24,latest",
                                  "numpy (>=1.24)"])
@posix_delivery
def test_unreadable_version_specifiers_are_refused_before_a_build(root, line):
    with pytest.raises(environments.Refused, match="version specifier this updater cannot read"):
        needed(root, requirements=f"mcp==2\n{line}\n")


@pytest.mark.parametrize("line", ["numpy", "numpy >= 1.24, < 3", "numpy~=1.24.0", "numpy==1.*", "numpy===1.24+cpu",
                                  "numpy[extra]>=1.24", "numpy>=1.24 ; python_version < '3.14'",
                                  "numpy; sys_platform == 'win32'", "numpy @ https://example.invalid/numpy-1.whl",
                                  "https://example.invalid/numpy-1.whl"])
def test_readable_requirement_lines_pass_the_line_check(line):
    assert environments.unsafe_requirement(line) is None


@pytest.mark.parametrize("field", ["dependencies", "extra"])
@pytest.mark.parametrize("declared", ['"-e ."', '"python-dotenv\\n--index-url x"'])
@posix_delivery
def test_pip_options_and_line_breaks_in_pyproject_are_refused(root, field, declared):
    if field == "dependencies":
        project = PROJECT.replace('dependencies = ["python-dotenv"]', f"dependencies = [{declared}]")
    else:
        project = PROJECT.replace('local-inference = ["httpx>=0.28,<0.29"]', f"local-inference = [{declared}]")
    with pytest.raises(environments.Refused, match="pip option|several lines"):
        needed(root, after=project, changed=("requirements.txt", "pyproject.toml"))


@pytest.mark.parametrize("specifier, refused", [
    (">=3.11", False), (">=3.14", True), ("<3.13", True), ("~=3.13", False), ("~=3.12.0", True),
    ("==3.13.*", False), ("!=3.13.*", True), (">=3.11,<3.14", False), ("==3.13.1", False), (">3.13.1", True),
    ("^3.11", True), (">=3.11.*", True),
])
def test_requires_python_is_evaluated_for_the_base_interpreter(specifier, refused):
    reason = environments.python_refusal(specifier, VERSION)
    assert bool(reason) == refused
    if reason and "cannot evaluate" not in reason:
        assert "Python 3.13.1 does not satisfy" in reason


@posix_delivery
def test_a_target_the_base_interpreter_cannot_run_is_refused_without_a_build(root):
    after = PROJECT.replace('requires-python = ">=3.11"', 'requires-python = ">=3.14"')
    runner = FakeBuilder()
    with pytest.raises(environments.Refused, match="does not satisfy requires-python"):
        needed(root, changed=("pyproject.toml",), after=after, run=runner)
    assert runner.builds == []


def test_only_the_venv_interpreter_is_managed(root):
    """.venv's interpreter: one under .venvs would not follow a switch, even the one .venv points at."""
    with pytest.raises(environments.Refused, match="not the installation's interpreter .*\\.venv"):
        needed(root, interpreter=sys.executable)
    built(root, NEW)
    environments.switch(str(root), environments.plan(str(root), NEW))
    with pytest.raises(environments.Refused, match="not the installation's interpreter"):
        needed(root, interpreter=environments.python_path(root / ".venvs" / NEW))


@pytest.mark.parametrize("kind", ["file", "foreign"])
def test_a_venv_that_is_a_file_or_a_link_elsewhere_is_refused(root, tmp_path, kind):
    os.rename(root / ".venv", tmp_path / "setup-venv")
    if kind == "file":
        (root / ".venv").write_text("not an environment")
    else:
        try:
            os.symlink(tmp_path / "setup-venv", root / ".venv", target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
    with pytest.raises(environments.Refused, match="neither an environment directory nor a link"):
        needed(root)


def test_a_venv_directory_that_is_no_environment_is_refused(root):
    (root / ".venv/pyvenv.cfg").unlink()
    with pytest.raises(environments.Refused, match="has no pyvenv.cfg"):
        needed(root)


def test_bridge_and_malformed_manifests_are_refused(root):
    with pytest.raises(environments.Refused, match="bridge"):
        needed(root, changed=("bridge/package.json",))
    with pytest.raises(environments.Refused, match="cannot verify"):
        needed(root, after="not valid TOML !")


def test_the_running_interpreter_is_its_own_base(root, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))
    monkeypatch.setattr(sys, "_base_executable", str(root.parent / "base/bin/python3"))
    probe = FakeBuilder()
    assert environments.base_python(str(root), probe) == (str(root.parent / "base/bin/python3"),
                                                          tuple(sys.version_info[:3]))
    assert probe.calls == []


def test_the_base_from_pyvenv_cfg_must_run_its_recorded_version(root):
    with pytest.raises(environments.Refused, match="runs Python 3.13, which made it: .*Python 3.12.4"):
        environments.base_python(str(root), FakeBuilder(version="3.12.4"))
    patch = FakeBuilder(version="3.13.9")  # a patch release of the same Python
    assert environments.base_python(str(root), patch) == (str(root.parent / "base/bin/python3"), (3, 13, 9))
    assert patch.calls == [[str(root.parent / "base/bin/python3"), "-c", environments._PROBE]]


def test_the_first_candidate_of_the_recorded_version_is_the_base(root):
    home = root.parent / "home"
    home.mkdir()
    (home / "python3.13").write_text("python3.13")
    executable = root.parent / "base/bin/python3"
    (root / ".venv/pyvenv.cfg").write_text(f"home = {home}\nversion = 3.13.1\nexecutable = {executable}\n")
    if environments.WINDOWS:
        pytest.skip("Windows looks for python.exe only")
    # pyvenv.cfg's executable became another Python (a profile link updated); home still has 3.13.
    probe = FakeBuilder(versions={str(executable): "3.14.0"})
    assert environments.base_python(str(root), probe) == (str(home / "python3.13"), (3, 13, 1))
    with pytest.raises(environments.Refused, match="no base interpreter .* runs Python 3.13"):
        environments.base_python(str(root), FakeBuilder(version="3.12.0"))


def test_a_base_interpreter_that_does_not_run_is_refused(root):
    def broken(args, cwd=None, timeout=None, env=None):
        raise OSError("exec format error")
    with pytest.raises(environments.Refused, match="does not run"):
        environments.base_python(str(root), broken)
    assert "python3 (does not run)" in str(pytest.raises(environments.Refused, environments.base_python,
                                                          str(root), broken).value)


def test_home_prefers_the_versioned_interpreter_name(root):
    home = root.parent / "home"
    home.mkdir()
    for name in ("python3.13", "python3", "python"):
        (home / name).write_text(name)
    (root / ".venv/pyvenv.cfg").write_text(f"home = {home}\nversion = 3.13.1\n")
    if environments.WINDOWS:
        pytest.skip("Windows looks for python.exe only")
    assert environments.base_python(str(root), FakeBuilder())[0] == str(home / "python3.13")


def test_a_missing_base_interpreter_is_refused(root):
    (root.parent / "base/bin/python3").unlink()
    with pytest.raises(environments.Refused, match="no longer exists"):
        environments.base_python(str(root), FakeBuilder())


# --- building ---------------------------------------------------------------------------------------

@posix_delivery
def test_a_build_installs_the_requirements_and_writes_its_record_last(root):
    wanted = needed(root)
    builder = FakeBuilder()
    record = environments.build(str(root), wanted, run=builder, timeout=60)
    environment = root / ".venvs" / record["id"]
    assert record["path"] == str(environment) and record["python"] == environments.python_path(environment)
    assert builder.calls[0] == [wanted.base, "-m", "venv", str(environment)]
    assert builder.calls[1] == [record["python"], "-m", "pip", "install", "--disable-pip-version-check",
                                "--no-input", "-r", str(environment / environments.REQUIREMENTS)]
    assert (environment / environments.REQUIREMENTS).read_text() == wanted.requirements
    assert json.loads((environment / environments.RECORD).read_text())["requirements_sha256"] == \
        record["requirements_sha256"]
    assert environments.complete(str(environment), record["requirements_sha256"])
    # The live environment is untouched while the installation serves.
    assert not environments.is_link(root / ".venv") and (root / ".venv/pyvenv.cfg").exists()


@posix_delivery
def test_venv_and_pip_run_without_settings_that_redirect_them(root, monkeypatch):
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PIP_USER", "PIP_TARGET",
                "PIP_PREFIX"):
        monkeypatch.setenv(key, "/elsewhere")
    monkeypatch.setenv("AGENTS_TEST_KEPT", "1")
    builder = FakeBuilder()
    environments.build(str(root), needed(root), run=builder, timeout=60)
    for env in builder.envs:
        assert env["AGENTS_TEST_KEPT"] == "1"
        assert not {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PIP_USER", "PIP_TARGET",
                    "PIP_PREFIX"}.intersection(env)


@posix_delivery
def test_a_complete_environment_is_reused(root):
    wanted = needed(root)
    first = environments.build(str(root), wanted, run=FakeBuilder(), timeout=60)
    again = FakeBuilder()
    assert environments.build(str(root), wanted, run=again, timeout=60) == first
    assert again.calls == []


@pytest.mark.parametrize("failure", [{"fail": "pip"}, {"fail": "venv"}, {"hang": "pip"}, {"raises": "pip"}])
@posix_delivery
def test_a_failed_build_leaves_no_partial_environment(root, failure):
    wanted = needed(root)
    with pytest.raises(environments.BuildFailed):
        environments.build(str(root), wanted, run=FakeBuilder(**failure), timeout=60)
    assert list((root / ".venvs").iterdir()) == []


@posix_delivery
def test_a_failure_reports_pip_errors_first_and_the_end_of_its_output(root):
    with pytest.raises(environments.BuildFailed) as failed:
        environments.build(str(root), needed(root), run=FakeBuilder(fail="pip", stdout="Collecting x\n" * 500),
                           timeout=60)
    message = str(failed.value)
    assert "exit code 1): ERROR: ResolutionImpossible\n... " in message
    assert message.endswith("Collecting x") and len(message) < 600


@posix_delivery
def test_a_build_past_its_deadline_stops_and_leaves_nothing(root, monkeypatch):
    clock = iter([0, 1, 100])
    monkeypatch.setattr(environments, "_now", lambda: next(clock))
    with pytest.raises(environments.BuildFailed, match="pip install did not start before the environment timeout"):
        environments.build(str(root), needed(root), run=FakeBuilder(), timeout=10)
    assert list((root / ".venvs").iterdir()) == []


@posix_delivery
def test_an_incomplete_environment_is_removed_before_a_rebuild(root):
    wanted = needed(root)
    leftover = root / ".venvs" / environments.environment_id(wanted.requirements, wanted.version)
    make_environment(leftover, record=False)
    (leftover / "stale.txt").write_text("interrupted build")
    record = environments.build(str(root), wanted, run=FakeBuilder(), timeout=60)
    assert record["path"] == str(leftover) and not (leftover / "stale.txt").exists()


@posix_delivery
def test_a_redirected_venvs_is_neither_built_in_nor_pruned(root, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    old = make_environment(elsewhere / ("py313-" + "c" * 12))
    try:
        os.symlink(elsewhere, root / ".venvs", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(environments.BuildFailed, match="is a link"):
        environments.build(str(root), needed(root), run=FakeBuilder(), timeout=60)
    assert environments.prune(str(root)) == [] and (old / environments.RECORD).exists()


ACTIVE = "py313-" + "d" * 12


@pytest.fixture
def linked(root):
    """The installation after its first switch: ``.venv`` links into ``.venvs``, by a junction on Windows,
    the only layout Windows delivers into."""
    shutil.copytree(root / ".venv", root / ".venvs" / ACTIVE)
    environments.switch(str(root), environments.plan(str(root), ACTIVE, now=0))
    return root


def test_a_build_beside_a_linked_venv_is_reused_and_a_failed_one_leaves_nothing(linked):
    """Runs on Windows too, where the tests above stop at the setup directory's refusal."""
    wanted = needed(linked)
    record = environments.build(str(linked), wanted, run=FakeBuilder(), timeout=60)
    assert record["python"] == environments.python_path(record["path"])
    assert environments.complete(record["path"], record["requirements_sha256"])
    again = FakeBuilder()
    assert environments.build(str(linked), wanted, run=again, timeout=60) == record and again.calls == []
    with pytest.raises(environments.BuildFailed):
        environments.build(str(linked), needed(linked, requirements="mcp==3\n"), run=FakeBuilder(fail="pip"),
                           timeout=60)
    assert sorted(path.name for path in (linked / ".venvs").iterdir()) == \
        sorted([ACTIVE, "legacy-19700101T000000Z", record["id"]])
    assert environments.state(str(linked)) == {"kind": "link", "name": ACTIVE}


# --- switching -------------------------------------------------------------------------------------

def built(root, name):
    return make_environment(root / ".venvs" / name)


NEW, OTHER = "py313-" + "a" * 12, "py313-" + "b" * 12


def test_the_first_switch_renames_the_setup_directory_and_rollback_renames_it_back(root):
    built(root, NEW)
    planned = environments.plan(str(root), NEW, now=0)
    assert planned == {"to": NEW, "previous": {"kind": "directory", "legacy": "legacy-19700101T000000Z"}}
    environments.switch(str(root), planned)
    assert environments.state(str(root)) == {"kind": "link", "name": NEW}
    assert (root / ".venvs/legacy-19700101T000000Z/pyvenv.cfg").exists()
    assert os.path.realpath(python(root)) == os.path.realpath(environments.python_path(root / ".venvs" / NEW))
    environments.restore(str(root), planned)
    assert not environments.is_link(root / ".venv") and (root / ".venv/pyvenv.cfg").exists()
    assert not (root / ".venvs/legacy-19700101T000000Z").exists()
    environments.restore(str(root), planned)  # idempotent: nothing left to undo
    assert environments.state(str(root)) == {"kind": "directory"}


@pytest.mark.skipif(os.name == "nt", reason="the POSIX order of the first switch")
def test_the_first_switch_makes_the_link_before_moving_the_directory(root, monkeypatch):
    built(root, NEW)
    planned = environments.plan(str(root), NEW, now=0)
    rename, seen = os.rename, []

    def recorded(source, destination):
        seen.append(sorted(name for name in os.listdir(root) if name.startswith(".venv.")))
        return rename(source, destination)
    monkeypatch.setattr(environments.os, "rename", recorded)
    environments.switch(str(root), planned)
    assert seen == [[f".venv.{os.getpid()}.tmp"]]
    assert environments.state(str(root)) == {"kind": "link", "name": NEW}


def test_a_link_is_replaced_and_restored(root):
    built(root, NEW), built(root, OTHER)
    environments.switch(str(root), environments.plan(str(root), OTHER))
    planned = environments.plan(str(root), NEW)
    assert planned["previous"] == {"kind": "link", "name": OTHER}
    environments.switch(str(root), planned)
    assert environments.state(str(root)) == {"kind": "link", "name": NEW}
    if not environments.WINDOWS:
        assert os.readlink(root / ".venv") == os.path.join(".venvs", NEW)  # relative: survives no move, needs none
    environments.restore(str(root), planned)
    assert environments.state(str(root)) == {"kind": "link", "name": OTHER}


def test_a_restore_after_an_interrupted_first_switch_renames_back(root):
    built(root, NEW)
    planned = environments.plan(str(root), NEW, now=0)
    os.rename(root / ".venv", root / ".venvs" / planned["previous"]["legacy"])  # renamed, link not yet made
    environments.restore(str(root), planned)
    assert environments.state(str(root)) == {"kind": "directory"}


@pytest.mark.parametrize("planned", [{"to": "../x", "previous": {"kind": "missing"}},
                                     {"to": NEW, "previous": {"kind": "link", "name": "../elsewhere"}},
                                     {"to": NEW, "previous": {"kind": "directory", "legacy": "../outside"}}])
def test_a_malformed_switch_is_rejected(root, planned):
    with pytest.raises(ValueError, match="malformed"):
        environments.restore(str(root), planned)
    with pytest.raises(ValueError, match="malformed"):
        environments.switch(str(root), planned)


def test_a_foreign_venv_is_never_switched(root, tmp_path):
    elsewhere = make_environment(tmp_path / "elsewhere")
    os.rename(root / ".venv", tmp_path / "setup-venv")
    try:
        os.symlink(elsewhere, root / ".venv", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert environments.state(str(root)) == {"kind": "foreign"}
    with pytest.raises(environments.Refused):
        environments.plan(str(root), NEW)


@pytest.fixture
def windows(monkeypatch):
    """The Windows code paths on any platform: a junction stub that makes a symbolic link."""
    if os.name == "nt":
        pytest.skip("the stub stands in for a junction with a POSIX symlink")
    junctions = []

    def junction(target, link):
        junctions.append((target, link))
        os.symlink(target, link, target_is_directory=True)
    monkeypatch.setattr(environments, "WINDOWS", True)
    monkeypatch.setattr(environments, "_junction", junction)
    return junctions


def test_windows_points_venv_with_a_junction(root, windows):
    built(root, NEW), built(root, OTHER)
    environments.switch(str(root), environments.plan(str(root), OTHER))
    environments.switch(str(root), environments.plan(str(root), NEW))
    # A junction needs an absolute target, and the old one goes first: no atomic replace exists.
    assert windows == [(os.path.abspath(root / ".venvs" / OTHER), str(root / ".venv")),
                       (os.path.abspath(root / ".venvs" / NEW), str(root / ".venv"))]
    assert environments.state(str(root)) == {"kind": "link", "name": NEW}
    assert environments.python_path(root / ".venv") == str(root / ".venv" / "Scripts" / "python.exe")


def test_windows_refuses_dependency_changes_on_the_setup_directory(root, windows):
    """Windows cannot rename the directory of the running interpreter."""
    with pytest.raises(environments.Refused, match="Windows cannot rename"):
        needed(root)


def test_windows_removes_the_empty_directory_a_failed_junction_left(root, windows, monkeypatch):
    built(root, NEW), built(root, OTHER)
    environments.switch(str(root), environments.plan(str(root), OTHER))

    def failed(target, link):
        os.mkdir(link)
        raise OSError("junction refused")
    monkeypatch.setattr(environments, "_junction", failed)
    planned = environments.plan(str(root), NEW)
    with pytest.raises(OSError, match="junction refused"):
        environments.switch(str(root), planned)
    assert not os.path.lexists(root / ".venv")
    monkeypatch.setattr(environments, "_junction", lambda target, link: os.symlink(target, link, True))
    environments.restore(str(root), planned)
    assert environments.state(str(root)) == {"kind": "link", "name": OTHER}


def test_a_restore_of_a_switch_that_never_ran_changes_nothing(root):
    built(root, NEW)
    planned = environments.plan(str(root), NEW, now=0)
    (root / ".venv/pyvenv.cfg").unlink()  # whatever the directory holds, it was never renamed
    environments.restore(str(root), planned)
    assert environments.state(str(root)) == {"kind": "directory"}


def test_a_restore_replaces_an_empty_venv_directory_with_the_setup_directory(root):
    built(root, NEW)
    planned = environments.plan(str(root), NEW, now=0)
    os.rename(root / ".venv", root / ".venvs" / planned["previous"]["legacy"])
    (root / ".venv").mkdir()  # what a failed junction can leave
    environments.restore(str(root), planned)
    assert environments.state(str(root)) == {"kind": "directory"} and (root / ".venv/pyvenv.cfg").exists()


@pytest.mark.skipif(os.name != "nt", reason="a real directory junction")
def test_windows_junction_switch_and_restore(root):
    built(root, NEW), built(root, OTHER)
    environments.switch(str(root), environments.plan(str(root), OTHER))
    planned = environments.plan(str(root), NEW)
    environments.switch(str(root), planned)
    assert environments.is_link(root / ".venv") and environments.state(str(root)) == {"kind": "link", "name": NEW}
    environments.restore(str(root), planned)
    assert environments.state(str(root)) == {"kind": "link", "name": OTHER}


def test_prune_keeps_the_active_and_the_previous_environment(root):
    names = ["py313-" + digit * 12 for digit in "1234"]
    for name in names:
        built(root, name)
    (root / ".venvs/notes").mkdir()  # not ours: never touched
    environments.switch(str(root), environments.plan(str(root), names[3], now=0))
    removed = environments.prune(str(root), keep=(names[3], "legacy-19700101T000000Z"))
    assert removed == names[:3]
    assert sorted(path.name for path in (root / ".venvs").iterdir()) == \
        sorted([names[3], "legacy-19700101T000000Z", "notes"])


@pytest.mark.parametrize("decoy", ["file", "directory"])
def test_an_interrupted_prune_leaves_nothing_complete_and_the_next_one_finishes(root, monkeypatch, decoy):
    victim, kept = "py313-" + "1" * 12, "py313-" + "2" * 12
    built(root, victim), built(root, kept)
    stale = root / f".venv.{os.getpid() + 1}.tmp"
    try:
        os.symlink(os.path.join(".venvs", kept), stale, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    # Named like a temporary link, but not a link: never removed.
    if decoy == "file":
        (root / ".venv.7.tmp").write_text("not a link of ours")
    else:
        (root / ".venv.7.tmp").mkdir()

    def interrupted(path, *args, **kwargs):
        raise OSError("interrupted")
    monkeypatch.setattr(environments.shutil, "rmtree", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        environments.prune(str(root), keep=(kept,))
    leftovers = list((root / ".venvs").iterdir())
    assert not environments.complete(str(root / ".venvs" / victim))
    assert [path.name for path in leftovers if environments.complete(str(path))] == [kept]
    assert not os.path.lexists(stale) and (root / ".venv.7.tmp").exists()
    monkeypatch.undo()
    assert environments.prune(str(root), keep=(kept,)) == []
    assert [path.name for path in (root / ".venvs").iterdir()] == [kept]


def test_activation_refuses_a_missing_or_incomplete_environment(root):
    digest = "d" * 64
    record = {"id": NEW, "requirements_sha256": digest}
    assert "missing or incomplete" in environments.activation_refusal(str(root), record)
    environment = built(root, NEW)
    assert "missing or incomplete" in environments.activation_refusal(str(root), record)
    (environment / environments.RECORD).write_text(json.dumps({"requirements_sha256": digest}))
    assert environments.activation_refusal(str(root), record) is None
    assert "malformed" in environments.activation_refusal(str(root), {"id": "../elsewhere"})


def test_an_installation_reached_through_a_link_is_managed(root, tmp_path):
    alias = tmp_path / "alias"
    try:
        os.symlink(root, alias, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert environments.managed(str(root), environments.python_path(alias / ".venv"))
    assert environments.managed(str(alias), python(root))
    built(root, NEW)
    assert not environments.managed(str(root), environments.python_path(alias / ".venvs" / NEW))


def test_managed_names_venv_lexically(root):
    assert environments.managed(str(root), python(root))
    assert not environments.managed(str(root), sys.executable)
    assert not environments.managed(str(root), "relative/bin/python")
    built(root, NEW)
    environments.switch(str(root), environments.plan(str(root), NEW))
    assert not environments.managed(str(root), environments.python_path(root / ".venvs" / NEW))
    assert environments.managed(str(root), python(root))


def test_a_process_started_on_the_setup_directory_is_replaced_by_a_switch(root, monkeypatch):
    monkeypatch.setattr(environments, "RUNNING", os.path.realpath(root / ".venv"))
    assert environments.ours(str(root)) and not environments.replaced(str(root))
    built(root, NEW)
    environments.switch(str(root), environments.plan(str(root), NEW))
    assert environments.replaced(str(root))
    monkeypatch.setattr(environments, "RUNNING", os.path.realpath(root / ".venvs" / NEW))
    assert environments.ours(str(root)) and not environments.replaced(str(root))
    monkeypatch.setattr(environments, "RUNNING", sys.prefix)
    assert not environments.ours(str(root)) and not environments.replaced(str(root))


def test_git_errors_reach_the_caller(root):
    def git(*args, check=True):
        raise subprocess.CalledProcessError(128, ["git", *args])
    with pytest.raises(subprocess.CalledProcessError):
        environments.needed(str(root), git, "old", "target", python(root))


def test_requirements_text_is_deterministic():
    text = environments.requirements_text(["a==1"], ["b", "c"], {"z": ["z1"], "y": ["y1"]})
    assert text == ("a==1\n\n# project.dependencies of pyproject.toml\nb\nc\n\n"
                    "# optional extra y, installed in the previous environment\ny1\n\n"
                    "# optional extra z, installed in the previous environment\nz1\n")


@pytest.mark.parametrize("requirement, installed, met", [
    ("mcp==1.31.0", "1.30.0", False),  # an exact pin bumped, the old version installed
    ("mcp==1.31.0", "1.31.0", True),
    ("mcp>=1.31", "1.30.0", False),
    ("mcp>=1.31,<2", "1.31.2.post1", True),
    ("mcp~=1.30.0", "1.30.4", True),
    ("mcp==1.*", "2.0", False),
    ("mcp==1.31.0rc1", "1.31.0", False),  # a clause this cannot evaluate holds
    ("mcp[cli]>=1.31", "1.31.0", True),
    ('mcp>=9; python_version < "3"', "1.0", False),  # markers are not evaluated
    ("mcp @ https://example.invalid/mcp.whl", "1.0", False),
    ("mcp>=1", "unknown", False),
])
def test_unmet_requirements_compare_names_and_release_versions(root, requirement, installed, met):
    make_environment(root / ".venv", record=False, packages=[("mcp", installed)])
    found = environments.unmet(requirement + "\npython-dotenv\n", str(root / ".venv"))
    assert "python-dotenv (not installed)" in found
    assert (found == ["python-dotenv (not installed)"]) == met


def test_a_bare_url_makes_the_requirements_unknown(root):
    assert environments.distribution_names("mcp\ngit+https://example.invalid/x.git\n") is None
    assert environments.unmet("mcp\nhttps://example.invalid/x.whl\n", str(root / ".venv")) is None
    assert environments.distribution_names('mcp; python_version < "3"\nHTTPX>=1\n') == {"mcp", "httpx"}


def test_an_installation_reached_through_a_link_knows_its_environments(root, tmp_path, monkeypatch):
    alias = tmp_path / "alias"
    try:
        os.symlink(root, alias, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(environments, "RUNNING", os.path.realpath(root / ".venv"))
    assert environments.ours(str(alias)) and not environments.replaced(str(alias))
    built(root, NEW)
    environments.switch(str(alias), environments.plan(str(alias), NEW))
    assert environments.replaced(str(alias))
    monkeypatch.setattr(environments, "RUNNING", os.path.realpath(root / ".venvs" / NEW))
    assert environments.ours(str(alias)) and not environments.replaced(str(alias))
    monkeypatch.setattr(environments, "RUNNING", os.path.realpath(tmp_path / "elsewhere" / ".venv"))
    assert not environments.ours(str(alias))


def test_installed_reads_distribution_names(root):
    assert {"httpx", "mcp"} <= environments.installed(str(root / ".venv"))
