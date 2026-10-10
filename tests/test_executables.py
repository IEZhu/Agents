"""Tools by absolute path: a binary planted earlier in PATH never runs in the service (#244)."""
import logging
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from src import executables, version
from src.daemon import autoupdate, service, update
from src.daemon.state import write_json
from src.user_sync import gitcmd

REAL_GIT = executables.find("git")
# A tool every machine keeps in a system directory.
SYSTEM_TOOL = "whoami" if os.name == "nt" else "sh"
needs_protected_git = pytest.mark.skipif(REAL_GIT is None or not REAL_GIT.protected,
                                         reason="needs a git that only an administrator can replace")
# Root owns whatever a test plants, so nothing it plants is replaceable by "the user".
not_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root owns the planted files")


def plant(directory: Path, name: str, marker: Path) -> Path:
    """An executable ``name`` in ``directory``: on POSIX a script that leaves ``marker`` and claims a
    recent git, on Windows a copy of a system program that is not git and fails on git's arguments."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        planted = directory / (name + ".exe")
        shutil.copyfile(Path(executables.system_directories()[0]) / "whoami.exe", planted)
    else:
        planted = directory / name
        planted.write_text(f"#!/bin/sh\ntouch '{marker}'\necho 'git version 99.0.0'\n")
        planted.chmod(0o755)
    return planted


@needs_protected_git
@not_root
@pytest.mark.parametrize("call", ["version", "user_sync"])
def test_a_git_planted_earlier_in_path_is_not_executed(tmp_path, monkeypatch, call):
    marker = tmp_path / "planted-ran"
    planted = plant(tmp_path / "bin", "git", marker)
    monkeypatch.setenv("PATH", str(planted.parent) + os.pathsep + os.environ.get("PATH", ""))
    expected = subprocess.run([REAL_GIT.path, "-C", str(version.ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout

    # install records the git only an administrator can replace...
    tools = executables.find_all()
    assert tools["git"] == REAL_GIT
    # ...and the service starts that one, with its own PATH.
    executables.configure({"git": tools["git"].path, "path": os.environ["PATH"]})
    executables.enter_service_environment()
    if call == "version":
        assert version._git("rev-parse", "HEAD") == expected
    else:
        assert gitcmd.git_version() == gitcmd.git_version(REAL_GIT.path) != (99, 0, 0)
    assert not marker.exists()


@pytest.fixture
def two_directories(tmp_path, monkeypatch):
    """``system`` counts as protected and ``user`` does not, whoever runs the test; both hold the probe."""
    system, user = tmp_path / "system", tmp_path / "user"
    monkeypatch.setattr(executables, "protected", lambda path: Path(path).parent == system)
    return {where: plant(directory, "agents-core-probe", tmp_path / "ran")
            for where, directory in (("system", system), ("user", user))}


def test_install_takes_a_protected_tool_over_an_earlier_replaceable_one(two_directories):
    search = os.pathsep.join(str(path.parent) for path in (two_directories["user"], two_directories["system"]))
    tool = executables.find("agents-core-probe", search)
    assert (tool.path, tool.protected) == (os.path.realpath(two_directories["system"]) if os.name == "nt"
                                           else str(two_directories["system"]), True)
    # Alone, a replaceable one is used, and install warns about it.
    alone = executables.find("agents-core-probe", str(two_directories["user"].parent))
    assert alone == executables.Tool("agents-core-probe", str(two_directories["user"]), False)
    assert str(two_directories["user"]) in executables.warning(alone)


def test_other_processes_run_what_their_path_names_first(two_directories, monkeypatch):
    """A stdio server or a sync command runs what the user's shell would: PATH order."""
    monkeypatch.setenv("PATH", os.pathsep.join(str(path.parent) for path in
                                               (two_directories["user"], two_directories["system"])))
    assert executables.path("agents-core-probe") == str(two_directories["user"])


def test_a_service_starts_its_recorded_tool_whatever_its_path_says_first(two_directories, monkeypatch):
    monkeypatch.setenv("PATH", str(two_directories["user"].parent))
    executables.configure({"git": str(two_directories["system"]), "path": str(two_directories["user"].parent)})
    assert executables.path("git") == str(two_directories["system"])


def test_a_service_looks_a_replaceable_recorded_tool_up_again(two_directories, monkeypatch, caplog):
    """An installation from before #244 recorded git from its PATH alone; the log names the change."""
    system_git = plant(two_directories["system"].parent, "git", Path("unused"))
    user_git = plant(two_directories["user"].parent, "git", Path("unused"))
    monkeypatch.setattr(executables, "_recent", lambda name, candidate: True)
    executables.configure({"git": str(user_git), "path": str(system_git.parent)})
    with caplog.at_level(logging.WARNING, logger="src.executables"):
        found = executables.path("git")
    assert os.path.realpath(found) == os.path.realpath(system_git)
    assert f"instead of {user_git}" in caplog.text
    # Without a protected one, the recorded one stays, with the install warning in the log.
    executables.configure({"git": str(user_git), "path": ""})
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="src.executables"):
        assert executables.path("git") == str(user_git)
    assert "can be replaced by this user's processes" in caplog.text


def test_a_service_looks_up_tools_it_did_not_record_in_the_recorded_path(two_directories, monkeypatch):
    monkeypatch.setenv("PATH", "")  # the service's own PATH names no user directory
    executables.configure({"path": str(two_directories["user"].parent)})
    assert executables.path("agents-core-probe") == str(two_directories["user"])


def test_a_git_too_old_for_sync_counts_only_without_a_newer_one(two_directories, monkeypatch):
    system_git = plant(two_directories["system"].parent, "git", Path("unused"))
    user_git = plant(two_directories["user"].parent, "git", Path("unused"))
    probed = []
    versions = {str(system_git): (2, 25), str(user_git): (2, 45)}
    monkeypatch.setattr(executables, "_version", lambda candidate: probed.append(candidate) or versions[candidate])
    search = os.pathsep.join(str(path.parent) for path in (user_git, system_git))
    assert executables.find("git", search).path == str(user_git)
    versions[str(user_git)] = (2, 20)
    assert os.path.realpath(executables.find("git", search).path) == os.path.realpath(system_git)
    # The replaceable git runs only when no protected one is recent enough.
    versions[str(system_git)] = (2, 40)
    probed.clear()
    executables.find("git", search)
    assert probed == [str(system_git)]


@pytest.mark.parametrize("search", ["", ".", os.curdir + os.pathsep + "relative" + os.sep + "bin"])
def test_the_current_directory_is_never_searched(tmp_path, monkeypatch, search):
    plant(tmp_path, "agents-core-probe", tmp_path / "ran")
    plant(tmp_path / "relative" / "bin", "agents-core-probe", tmp_path / "ran")
    monkeypatch.chdir(tmp_path)
    for prefer_protected in (True, False):
        assert executables.find("agents-core-probe", search, prefer_protected=prefer_protected) is None


@not_root
def test_system_tools_are_protected_and_planted_ones_are_not(tmp_path):
    planted = plant(tmp_path / "bin", SYSTEM_TOOL, tmp_path / "ran")
    tool = executables.find(SYSTEM_TOOL, str(planted.parent))
    assert tool.protected and tool.path != str(planted)
    assert executables.protected(tool.path) and not executables.protected(str(planted))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@not_root
def test_a_link_in_a_writable_directory_is_not_protected(tmp_path):
    system = executables.find(SYSTEM_TOOL, "")
    link = tmp_path / SYSTEM_TOOL
    link.symlink_to(system.path)
    assert system.protected and not executables.protected(str(link))


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions")
def test_a_junction_to_a_system_directory_records_the_real_path(tmp_path):
    junction = tmp_path / "junction"
    system32 = executables.system_directories()[0]
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), system32], check=True, capture_output=True)
    try:
        tool = executables.find(SYSTEM_TOOL, str(junction))
        assert tool.protected
        assert os.path.normcase(os.path.dirname(tool.path)) == os.path.normcase(os.path.realpath(system32))
    finally:
        os.rmdir(junction)  # removes the junction, never its target


def test_a_missing_tool_is_looked_up_again(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(FileNotFoundError, match="agents-core-probe"):
        executables.path("agents-core-probe")
    assert not executables.available("agents-core-probe")
    planted = plant(tmp_path / "bin", "agents-core-probe", tmp_path / "ran")
    assert executables.path("agents-core-probe") == str(planted)


def test_update_starts_the_tools_in_service_json(two_directories, tmp_path):
    write_json(tmp_path / "service.json", {"installation": str(tmp_path), "git": str(two_directories["system"])})
    update.load_service(SimpleNamespace(directory=tmp_path, config={}))
    assert executables.path("git") == str(two_directories["system"])


def test_the_service_environment_has_system_directories_and_no_msystem(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("MSYSTEM", "MINGW64")  # Git Bash's, which keeps Git for Windows off its own PATH
    environment = executables.service_environment()
    assert environment["PATH"] == executables.service_path() and "MSYSTEM" not in environment
    assert executables.service_path().split(os.pathsep) == executables.system_directories()
    assert os.environ["MSYSTEM"] == "MINGW64"  # a copy
    executables.enter_service_environment()
    assert os.environ["PATH"] == executables.service_path() and "MSYSTEM" not in os.environ


def test_the_updaters_git_runs_in_the_service_environment(two_directories, tmp_path, monkeypatch):
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    executables.configure({"git": str(two_directories["system"])})
    seen = []
    monkeypatch.setattr(autoupdate.subprocess, "run", lambda argv, **options: seen.append((argv, options["env"])))
    autoupdate._git(SimpleNamespace(config={"installation": str(tmp_path)}), "status")
    (argv, environment), = seen
    assert argv == [str(two_directories["system"]), "status"]
    assert environment["PATH"] == executables.service_path() and "MSYSTEM" not in environment


def test_the_launchagent_gets_the_service_path(tmp_path, monkeypatch):
    user_bin = str(tmp_path / "bin")
    plist = tmp_path / "agent.plist"
    controller = SimpleNamespace(label="local.agents-core.test", plist=plist, directory=tmp_path,
                                 config={"python": "/usr/bin/python3", "installation": str(tmp_path),
                                         "path": user_bin + os.pathsep + "/usr/bin"})
    service.Launchd(controller).write()
    assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["PATH"] == executables.service_path()


@pytest.mark.parametrize("developer_tools", ["present", "missing", "unselected"])
def test_macos_git_shim_counts_only_with_its_developer_tools(tmp_path, monkeypatch, developer_tools):
    """/usr/bin/git is root's, but only forwards to the Command Line Tools or Xcode: without them a
    working git elsewhere must win."""
    developer = tmp_path / "CommandLineTools"
    if developer_tools == "present":
        (developer / "usr" / "bin").mkdir(parents=True)
        (developer / "usr" / "bin" / "git").write_text("")
    monkeypatch.setattr(executables.sys, "platform", "darwin")
    monkeypatch.setattr(executables.subprocess, "run", lambda argv, **_: SimpleNamespace(
        returncode=2 if developer_tools == "unselected" else 0, stdout=f"{developer}\n"))
    assert executables._usable("/usr/bin/git") is (developer_tools == "present")
    assert executables._usable("/usr/bin/ssh-keygen") and executables._usable("/opt/local/bin/git")
