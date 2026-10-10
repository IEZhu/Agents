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
GIT_VERSION = executables._version(REAL_GIT.path) if REAL_GIT else None
# Older git keeps the user's global configuration (git_environment, #279).
needs_isolating_git = pytest.mark.skipif(
    REAL_GIT is None or not REAL_GIT.protected or (GIT_VERSION or (0, 0)) < executables.SAFE_DIRECTORY_ON_COMMAND_LINE,
    reason="needs a protected git that reads safe.directory from the command line (2.38 or later)")


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
    # A git in this machine's system directories (/usr/bin/git on POSIX) is a candidate too: unknown, not recent.
    monkeypatch.setattr(executables, "_version", lambda candidate: probed.append(candidate) or versions.get(candidate))
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


def test_the_service_environment_has_system_directories_and_nothing_that_steers_git(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("MSYSTEM", "MINGW64")  # Git Bash's, which keeps Git for Windows off its own PATH
    # From launchctl setenv or the user's environment: they redirect git or /usr/bin/git (#279).
    monkeypatch.setenv("GIT_EXEC_PATH", str(tmp_path / "exec"))
    monkeypatch.setenv("DEVELOPER_DIR", str(tmp_path / "Xcode.app"))
    monkeypatch.setenv("AGENTS_KEPT", "1")
    left_out = ("MSYSTEM", "GIT_EXEC_PATH", "DEVELOPER_DIR")
    environment = executables.service_environment()
    assert environment["PATH"] == executables.service_path() and environment["AGENTS_KEPT"] == "1"
    assert not any(name in environment for name in left_out)
    assert executables.service_path().split(os.pathsep) == executables.system_directories()
    assert os.environ["MSYSTEM"] == "MINGW64"  # a copy
    executables.enter_service_environment()
    assert os.environ["PATH"] == executables.service_path() and not any(name in os.environ for name in left_out)


def test_git_on_the_installation_runs_without_the_users_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(executables, "_installed_git_version", lambda: (2, 45))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    environment = executables.git_environment(tmp_path, {**os.environ, "GIT_CONFIG_GLOBAL": "~/.gitconfig"})
    assert "GIT_DIR" not in environment and environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GIT_CONFIG_NOSYSTEM" not in environment  # the system configuration stays
    assert (environment["GIT_CONFIG_KEY_0"], environment["GIT_CONFIG_VALUE_0"]) == \
        ("safe.directory", tmp_path.resolve().as_posix())
    # Then the line-ending settings of the user's git, if any (test_the_users_line_endings_come_along).
    assert all(environment[f"GIT_CONFIG_KEY_{index}"] in ("core.autocrlf", "core.eol")
               for index in range(1, int(environment["GIT_CONFIG_COUNT"])))


def test_git_before_2_38_keeps_the_users_configuration(tmp_path, monkeypatch):
    """It reads safe.directory only from the system and global configuration: hiding the global one
    would refuse an installation that relied on it there."""
    monkeypatch.setattr(executables, "_installed_git_version", lambda: (2, 37))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    environment = executables.git_environment(tmp_path)
    assert not any(name.startswith("GIT_") for name in environment)


def setup_git(*args, cwd):
    """git for building fixtures, never with the hostile configuration below."""
    environment = {**{name: value for name, value in os.environ.items() if not name.startswith("GIT_")},
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run([REAL_GIT.path, *args], cwd=cwd, env=environment, check=True, capture_output=True,
                          text=True).stdout.strip()


def hostile_configuration(tmp_path, monkeypatch, *, insteadof=None, extra=()):
    """The user's global git configuration, as HOME's .gitconfig: a core.fsmonitor that leaves a marker
    (it runs on `git status`), ``extra`` lines in its [core] section and, with ``insteadof=(url, base)``,
    a fetch redirected from base to url."""
    marker, hook = tmp_path / "fsmonitor-ran", tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\nexit 1\n")
    hook.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    # Git runs the hook through a shell: quoted, a temporary path with a space still works.
    lines = ["[core]", f"\tfsmonitor = \"'{hook.as_posix()}'\"", *extra]
    if insteadof:
        lines += [f'[url "{insteadof[0]}"]', f"\tinsteadOf = {insteadof[1]}"]
    (home / ".gitconfig").write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_DIR"):
        monkeypatch.delenv(name, raising=False)
    return marker


def plain_git(*args, cwd):
    """git as the user's shell runs it, with the hostile configuration."""
    return subprocess.run([REAL_GIT.path, *args], cwd=cwd, capture_output=True, text=True,
                          env={name: value for name, value in os.environ.items() if name != "MSYSTEM"})


def committed_repository(directory):
    setup_git("init", "--quiet", "--initial-branch=main", str(directory), cwd=directory.parent)
    (directory / "README.md").write_bytes(b"line one\nline two\n")
    setup_git("add", "README.md", cwd=directory)
    setup_git("commit", "--quiet", "-m", "first", cwd=directory)
    return directory


@needs_isolating_git
def test_the_version_footers_git_ignores_the_users_configuration(tmp_path, monkeypatch):
    # A repository of its own: the real checkout may enable FSMonitor in its own configuration.
    monkeypatch.setattr(version, "ROOT", committed_repository(tmp_path / "installation"))
    marker = hostile_configuration(tmp_path, monkeypatch)
    status = ("--no-optional-locks", "-C", str(version.ROOT), "status", "--porcelain", "--untracked-files=no")
    plain_git(*status, cwd=tmp_path)
    assert marker.exists(), "the hostile configuration must be live for this test to mean anything"
    marker.unlink()
    assert version._git("status", "--porcelain", "--untracked-files=no") is not None
    assert not marker.exists()


@needs_isolating_git
def test_the_updaters_check_ignores_the_users_configuration(tmp_path, monkeypatch):
    """Neither a global core.fsmonitor runs nor a global url.insteadOf redirects the update's fetch."""
    origin, evil, installation, other = (tmp_path / name for name in ("origin.git", "evil.git", "installation", "other"))
    for bare in (origin, evil):
        setup_git("init", "--quiet", "--bare", "--initial-branch=main", str(bare), cwd=tmp_path)
    setup_git("init", "--quiet", "--initial-branch=main", str(installation), cwd=tmp_path)
    (installation / "README.md").write_text("installed\n")
    setup_git("add", "README.md", cwd=installation)
    setup_git("commit", "--quiet", "-m", "installed", cwd=installation)
    setup_git("remote", "add", "origin", origin.as_posix(), cwd=installation)
    setup_git("push", "--quiet", "origin", "main", cwd=installation)
    head = setup_git("rev-parse", "HEAD", cwd=installation)
    setup_git("clone", "--quiet", origin.as_posix(), str(other), cwd=tmp_path)
    (other / "README.md").write_text("updated\n")
    setup_git("commit", "--quiet", "-am", "update", cwd=other)
    setup_git("push", "--quiet", "origin", "main", cwd=other)
    target = setup_git("rev-parse", "HEAD", cwd=other)
    setup_git("commit", "--quiet", "--allow-empty", "-m", "evil", cwd=other)
    setup_git("push", "--quiet", evil.as_posix(), "main", cwd=other)
    evil_head = setup_git("rev-parse", "HEAD", cwd=other)

    marker = hostile_configuration(tmp_path, monkeypatch, insteadof=(evil.as_posix(), origin.as_posix()))
    # The hostile configuration is live for plain git: the hook runs, the fetch goes to evil.
    plain_git("status", "--porcelain", cwd=installation)
    assert marker.exists()
    assert plain_git("ls-remote", "origin", "main", cwd=installation).stdout.split()[0] == evil_head
    marker.unlink()

    controller = SimpleNamespace(config={"installation": str(installation)})
    assert autoupdate.check_target(controller, "origin", "main") == {"state": "available", "head": head,
                                                                      "target": target}
    assert not marker.exists()


def test_the_updaters_git_on_the_installation_drops_the_users_configuration(tmp_path, monkeypatch):
    from src import self_update
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    seen = []
    monkeypatch.setattr(self_update, "_run_command", lambda args, cwd, timeout, env: seen.append(env))
    self_update._run_git(["status"], str(tmp_path), 5)
    (environment,) = seen
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull and "GIT_DIR" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_the_updaters_git_runs_in_the_service_environment(two_directories, tmp_path, monkeypatch):
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    executables.configure({"git": str(two_directories["system"])})
    monkeypatch.setattr(executables, "_installed_git_version", lambda: (2, 45))
    monkeypatch.setattr(executables, "_line_endings", lambda repository: [])
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


@needs_isolating_git
def test_the_installation_is_a_safe_directory_without_the_users_configuration(tmp_path):
    repository = committed_repository(tmp_path / "installation")
    # As if another user owned it, which a global safe.directory entry would have allowed.
    dubious = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    command = [REAL_GIT.path, "-C", str(repository), "rev-parse", "HEAD"]
    refused = subprocess.run(command, capture_output=True, text=True,
                             env={**executables.service_environment(), "GIT_CONFIG_GLOBAL": os.devnull, **dubious})
    assert refused.returncode and "dubious ownership" in refused.stderr
    allowed = subprocess.run(command, capture_output=True, text=True,
                             env={**executables.git_environment(repository), **dubious})
    assert allowed.returncode == 0, allowed.stderr


def test_the_users_line_endings_come_along(tmp_path, monkeypatch):
    monkeypatch.setattr(executables, "_installed_git_version", lambda: (2, 45))
    repository = committed_repository(tmp_path / "installation")
    hostile_configuration(tmp_path, monkeypatch, extra=("\teol = crlf",))
    environment = executables.git_environment(repository)
    settings = {environment[f"GIT_CONFIG_KEY_{index}"]: environment[f"GIT_CONFIG_VALUE_{index}"]
                for index in range(int(environment["GIT_CONFIG_COUNT"]))}
    assert settings["core.eol"] == "crlf" and settings["safe.directory"] == repository.resolve().as_posix()
    assert "core.fsmonitor" not in settings


@needs_isolating_git
def test_a_checkout_written_with_the_users_autocrlf_stays_clean(tmp_path, monkeypatch):
    """A global core.autocrlf=true wrote CRLF files; without it the updater would see them as modified."""
    if plain_git("config", "--system", "--get", "core.autocrlf", cwd=tmp_path).stdout.strip():
        pytest.skip("the system configuration sets core.autocrlf for every git here")
    origin = committed_repository(tmp_path / "origin")
    hostile_configuration(tmp_path, monkeypatch, extra=("\tautocrlf = true",))
    installation = tmp_path / "installation"
    assert plain_git("clone", "--quiet", str(origin), str(installation), cwd=tmp_path).returncode == 0
    assert b"\r\n" in (installation / "README.md").read_bytes()
    status = ("--no-optional-locks", "status", "--porcelain", "--untracked-files=no")
    without = subprocess.run([REAL_GIT.path, *status], cwd=installation, capture_output=True, text=True,
                             env={**executables.service_environment(), "GIT_CONFIG_GLOBAL": os.devnull})
    assert without.stdout.strip(), "without the user's autocrlf the checkout looks modified"
    clean = subprocess.run([REAL_GIT.path, *status], cwd=installation, capture_output=True, text=True,
                           env=executables.git_environment(installation))
    assert clean.returncode == 0 and clean.stdout == ""


@pytest.mark.parametrize("returncode, stderr", [(0, ""), (128, "fatal: could not read Username for 'https://x'")])
def test_enabling_updates_warns_when_the_remote_needs_the_users_configuration(tmp_path, monkeypatch, returncode,
                                                                                 stderr):
    monkeypatch.setattr(autoupdate, "status", lambda controller: {"enabled": True})
    monkeypatch.setattr(autoupdate, "_git", lambda controller, *args, check=True: subprocess.CompletedProcess(
        args, returncode, "", stderr))
    result = autoupdate._enabled(SimpleNamespace(config={"installation": str(tmp_path)}))
    if returncode:
        assert "could not read Username" in result["warning"] and "repository configuration" in result["warning"]
    else:
        assert result == {"enabled": True}
