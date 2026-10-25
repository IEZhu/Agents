"""Versioned Python environments (#181): what an update installs, building, switching, pruning.

No network and no real package installs: ``python -m venv`` and ``pip`` run through
an injected fake that creates the files a real build leaves.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import environments
from tests.test_daemon_update_dependencies import MANIFEST, fake_git

VERSION = (3, 13, 1)


class FakeBuilder:
    """Stands in for ``self_update._run_command`` running ``venv`` and ``pip``."""

    def __init__(self, *, fail=None, hang=None):
        self.calls, self.fail, self.hang = [], fail, hang

    def __call__(self, args, cwd=None, timeout=None, env=None):
        self.calls.append(list(args))
        step = args[2]
        if step == self.hang:
            raise subprocess.TimeoutExpired(args, timeout)
        if step == "venv":
            make_environment(Path(args[3]), record=False, base=Path(args[0]))  # pyvenv.cfg names its base
        elif step != "pip":
            raise AssertionError(f"unexpected command {args}")
        if step == self.fail:
            return subprocess.CompletedProcess(args, 1, "", "ResolutionImpossible")
        return subprocess.CompletedProcess(args, 0, "", "")


def make_environment(directory, *, record=True, packages=(), base=None):
    """A venv as ``python -m venv`` leaves it; *record* marks it a complete build."""
    python = Path(environments.python_path(directory))
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("interpreter")
    executable = base or directory / "base-python"
    (directory / "pyvenv.cfg").write_text(f"home = {Path(executable).parent}\nversion = 3.13.1\n"
                                          f"executable = {executable}\n")
    site = directory / ("Lib/site-packages" if environments.WINDOWS else "lib/python3.13/site-packages")
    site.mkdir(parents=True, exist_ok=True)
    for name in packages:
        info = site / f"{name}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    if record:
        (directory / environments.RECORD).write_text(json.dumps({"requirements_sha256": "x"}))
    return directory


@pytest.fixture
def root(tmp_path):
    """An installation whose ``.venv`` is the directory setup created, on a real base interpreter file."""
    base = tmp_path / "base" / "bin" / "python3"
    base.parent.mkdir(parents=True)
    base.write_text("base interpreter")
    installation = tmp_path / "install"
    make_environment(installation / ".venv", record=False, packages=("httpx", "mcp"), base=base)
    return installation


def python(root):
    return environments.python_path(root / ".venv")


def needed(root, *, changed=("requirements.txt",), before=MANIFEST, after=MANIFEST, requirements="mcp==2\n",
           interpreter=None):
    git = fake_git(before, after, changed=changed, requirements=requirements)
    return environments.needed(str(root), git, "old", "target", interpreter or python(root))


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


def test_requirements_runtime_dependencies_and_installed_extras_are_installed(root):
    after = MANIFEST + 'thread-status = ["claude-agent-sdk==0.2.168"]\n'
    found = needed(root, after=after)
    assert found.version == VERSION and found.base == str(root.parent / "base/bin/python3")
    lines = found.requirements.splitlines()
    assert lines[0] == "mcp==2"
    # The code declares its runtime dependencies in pyproject.toml; requirements.txt may lag.
    assert "python-dotenv" in lines and "mcp==1.28.1" in lines
    # httpx is installed, so the extra that needs it carries over; a new extra was never installed.
    assert "httpx>=0.28,<0.29" in lines and "claude-agent-sdk==0.2.168" not in lines


def test_an_extra_whose_packages_are_missing_is_not_carried(root, tmp_path):
    bare = tmp_path / "bare"
    base = root.parent / "base/bin/python3"
    make_environment(bare / ".venv", record=False, base=base)
    assert "httpx" not in needed(bare).requirements


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


def test_a_target_the_base_interpreter_cannot_run_is_refused_without_a_build(root):
    after = MANIFEST.replace('requires-python = ">=3.11"', 'requires-python = ">=3.14"')
    with pytest.raises(environments.Refused, match="does not satisfy requires-python"):
        needed(root, changed=("pyproject.toml",), after=after)


def test_an_interpreter_outside_the_installation_is_refused(root):
    with pytest.raises(environments.Refused, match="not an interpreter of the installation's .venv"):
        needed(root, interpreter=sys.executable)


def test_requirements_naming_other_files_are_refused(root):
    with pytest.raises(environments.Refused, match="names other files"):
        needed(root, requirements="-r base.txt\nmcp==2\n")


def test_bridge_and_malformed_manifests_are_refused(root):
    with pytest.raises(environments.Refused, match="bridge"):
        needed(root, changed=("bridge/package.json",))
    with pytest.raises(environments.Refused, match="cannot verify"):
        needed(root, after="not valid TOML !")


def test_the_running_interpreter_is_its_own_base(root, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))
    monkeypatch.setattr(sys, "_base_executable", str(root.parent / "base/bin/python3"))
    assert environments.base_python(str(root)) == (str(root.parent / "base/bin/python3"), tuple(sys.version_info[:3]))


# --- building ---------------------------------------------------------------------------------------

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


def test_a_complete_environment_is_reused(root):
    wanted = needed(root)
    first = environments.build(str(root), wanted, run=FakeBuilder(), timeout=60)
    again = FakeBuilder()
    assert environments.build(str(root), wanted, run=again, timeout=60) == first
    assert again.calls == []


@pytest.mark.parametrize("failure", [{"fail": "pip"}, {"fail": "venv"}, {"hang": "pip"}])
def test_a_failed_build_leaves_no_partial_environment(root, failure):
    wanted = needed(root)
    with pytest.raises(environments.BuildFailed):
        environments.build(str(root), wanted, run=FakeBuilder(**failure), timeout=60)
    assert list((root / ".venvs").iterdir()) == []


def test_an_incomplete_environment_is_removed_before_a_rebuild(root):
    wanted = needed(root)
    leftover = root / ".venvs" / environments.environment_id(wanted.requirements, wanted.version)
    make_environment(leftover, record=False)
    (leftover / "stale.txt").write_text("interrupted build")
    record = environments.build(str(root), wanted, run=FakeBuilder(), timeout=60)
    assert record["path"] == str(leftover) and not (leftover / "stale.txt").exists()


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


@pytest.mark.skipif(os.name == "nt", reason="the stub stands in for a junction with a POSIX symlink")
def test_windows_points_venv_with_a_junction(root, monkeypatch):
    junctions = []

    def junction(target, link):
        junctions.append((target, link))
        os.symlink(target, link, target_is_directory=True)
    monkeypatch.setattr(environments, "WINDOWS", True)
    monkeypatch.setattr(environments, "_junction", junction)
    built(root, NEW), built(root, OTHER)
    environments.switch(str(root), environments.plan(str(root), OTHER))
    environments.switch(str(root), environments.plan(str(root), NEW))
    # A junction needs an absolute target, and the old one goes first: no atomic replace exists.
    assert junctions == [(os.path.abspath(root / ".venvs" / OTHER), str(root / ".venv")),
                         (os.path.abspath(root / ".venvs" / NEW), str(root / ".venv"))]
    assert environments.state(str(root)) == {"kind": "link", "name": NEW}
    assert environments.python_path(root / ".venv") == str(root / ".venv" / "Scripts" / "python.exe")


@pytest.mark.skipif(os.name != "nt", reason="a real directory junction")
def test_windows_junction_switch_and_restore(root):
    built(root, NEW)
    planned = environments.plan(str(root), NEW)
    environments.switch(str(root), planned)
    assert environments.is_link(root / ".venv") and environments.state(str(root)) == {"kind": "link", "name": NEW}
    environments.restore(str(root), planned)
    assert environments.state(str(root)) == {"kind": "directory"}


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


def test_activation_refuses_a_missing_or_incomplete_environment(root):
    digest = "d" * 64
    record = {"id": NEW, "requirements_sha256": digest}
    assert "missing or incomplete" in environments.activation_refusal(str(root), record)
    environment = built(root, NEW)
    assert "missing or incomplete" in environments.activation_refusal(str(root), record)
    (environment / environments.RECORD).write_text(json.dumps({"requirements_sha256": digest}))
    assert environments.activation_refusal(str(root), record) is None
    assert "malformed" in environments.activation_refusal(str(root), {"id": "../elsewhere"})


def test_managed_compares_the_environment_not_the_interpreter_link(root, tmp_path):
    assert environments.managed(str(root), python(root))
    assert not environments.managed(str(root), sys.executable)
    assert not environments.managed(str(root), "relative/bin/python")
    built(root, NEW)
    environments.switch(str(root), environments.plan(str(root), NEW))
    assert environments.managed(str(root), environments.python_path(root / ".venvs" / NEW))
    assert environments.managed(str(root), python(root))


def test_git_errors_reach_the_caller(root):
    def git(*args, check=True):
        raise subprocess.CalledProcessError(128, ["git", *args])
    with pytest.raises(subprocess.CalledProcessError):
        environments.needed(str(root), git, "old", "target", python(root))


def test_requirements_text_is_deterministic():
    text = environments.requirements_text("a==1\n", ["b", "c"], {"z": ["z1"], "y": ["y1"]})
    assert text == ("a==1\n\n# project.dependencies of pyproject.toml\nb\nc\n\n"
                    "# optional extra y, installed in the previous environment\ny1\n\n"
                    "# optional extra z, installed in the previous environment\nz1\n")


def test_installed_reads_distribution_names(root):
    assert {"httpx", "mcp"} <= environments.installed(str(root / ".venv"))
