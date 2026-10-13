"""The Claude session launcher (#343): argv, sandbox settings, environment and login, with a fake ``claude``."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from src.schedules import config as autodev_config
from src.schedules import sandbox
from src.schedules.sandbox import SandboxError, SessionPaths

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"
SESSION_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
RUN_ID = "20261013T120000Z-abcdef"


def fake_claude(directory: Path, scenario: dict) -> str:
    """An executable ``claude`` that runs tests/fixtures/fake_claude.py with ``scenario``."""
    scenario_file = directory / "scenario.json"
    scenario_file.write_text(json.dumps(scenario))
    wrapper = directory / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} {scenario_file} \"$@\"\n")
    wrapper.chmod(0o755)
    return str(wrapper)


@pytest.fixture
def temp_root():
    """A fresh short temporary root under /private/tmp (`sandbox.make_temp_root`), removed afterwards."""
    root = sandbox.make_temp_root()
    yield root
    sandbox.remove_temp_root(root)


def make_paths(base: Path, state: Path, tmp_root: Path, *, socket: Path | None = None) -> SessionPaths:
    """A session layout under ``base`` (no wildcard characters in it) with a fresh home."""
    home = base / "home"
    (home / ".ssh").mkdir(parents=True)
    root = base / "s"
    for name in ("repo/.git", "exchange", "state", "gh", "venv/bin", "owner-tmp", "claude-tmp", "claude-shared", "conf"):
        (root / name).mkdir(parents=True)
    (root / "repo" / "README.md").write_text("one-off\n")
    return SessionPaths(home=home, repository=root / "repo", exchange=root / "exchange", state_copy=root / "state",
                        venv=root / "venv", config_dir=sandbox.config_dir(state), gh_config=root / "gh",
                        settings_file=root / "conf" / "settings.json", tmp_root=tmp_root, state_dir=state,
                        socket=socket, owner_temp=root / "owner-tmp", claude_temp=root / "claude-tmp",
                        claude_shared=root / "claude-shared")


@pytest.fixture
def paths(tmp_path_factory, service_state_dir, temp_root):
    base = tmp_path_factory.mktemp("sbx")
    return make_paths(base, service_state_dir, temp_root, socket=base / "run" / "control.sock")


@pytest.fixture
def claude(tmp_path_factory):
    return fake_claude(tmp_path_factory.mktemp("bin"), {})


def test_stage_argv_is_exact(paths):
    argv = sandbox.argv("/opt/claude", "implement", paths, model="opus", effort="xhigh")
    assert argv == ["/opt/claude", "-p", "--restricted", "--safe-mode", "--tools", "Bash,Read,Edit,Write,Glob,Grep",
                    "--strict-mcp-config", "--permission-mode", "dontAsk", "--permission-prompts", "none",
                    "--disallowedTools", "mcp__*", "--no-session-persistence", "--settings", str(paths.settings_file),
                    "--add-dir", str(paths.exchange), str(paths.state_copy),
                    "--output-format", "stream-json", "--verbose", "--model", "opus", "--effort", "xhigh"]
    assert sandbox.argv("/opt/claude", "submit", paths)[5] == "Bash,Read,Edit,Write,Glob,Grep"


def test_selection_stage_has_no_bash(paths):
    argv = sandbox.argv("/opt/claude", "select", paths)
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert "Bash" not in " ".join(argv)


@pytest.mark.parametrize("kind", sandbox.KINDS)
def test_every_kind_is_restricted_safe_and_strict(paths, kind):
    argv = sandbox.argv("/opt/claude", kind, paths)
    for flag in ("-p", "--restricted", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__*"


def test_argv_refuses_a_relative_executable_and_odd_model(paths):
    with pytest.raises(SandboxError):
        sandbox.argv("claude", "implement", paths)
    with pytest.raises(SandboxError):
        sandbox.argv("/opt/claude", "implement", paths, model="opus --dangerously-skip-permissions")
    with pytest.raises(ValueError):
        sandbox.argv("/opt/claude", "worker", paths)


def test_settings_match_the_amended_plan(paths):
    content = sandbox.settings("implement", paths)
    assert content["permissions"] == {
        "blockReadsOutsideWorkingDirectories": True,
        "allow": [f"Edit(/{paths.repository}/**)", f"Edit(/{paths.exchange}/**)"],
        "deny": [f"Edit(/{paths.state_copy}/**)", f"Edit(/{paths.venv}/**)"]}
    assert content["permissions"]["allow"][0].startswith("Edit(//")
    box = content["sandbox"]
    assert (box["enabled"], box["failIfUnavailable"], box["autoAllowBashIfSandboxed"],
            box["allowUnsandboxedCommands"]) == (True, True, True, False)
    assert box["filesystem"] == {
        "disabled": False,
        "denyRead": ["~/", "~/Library/Keychains", str(paths.owner_temp), str(paths.claude_temp),
                     str(paths.claude_shared), str(paths.state_dir), str(paths.config_dir)],
        "allowRead": [str(paths.repository), str(paths.venv), str(paths.exchange), str(paths.state_copy),
                      str(paths.tmp_root), str(paths.socket)],
        "allowWrite": [str(paths.repository), str(paths.exchange), str(paths.tmp_root)],
        "denyWrite": [str(paths.owner_temp), str(paths.claude_temp), str(paths.claude_shared)]}
    assert box["network"] == {"allowedDomains": [], "strictAllowlist": True, "allowUnixSockets": [str(paths.socket)]}
    assert box["credentials"] == {"envVars": [{"name": "CLAUDE_CODE_OAUTH_TOKEN", "mode": "deny"}]}
    assert content["disableClaudeAiConnectors"] is True
    for key in ("excludedCommands", "allowAppleEvents", "enableWeakerNetworkIsolation", "ignoreViolations"):
        assert key not in box
    for key in ("allowAllUnixSockets", "allowLocalBinding", "allowMachLookup", "httpProxyPort", "socksProxyPort"):
        assert key not in box["network"]
    assert sandbox.check_settings(content, "implement", paths) == []


def test_a_session_without_a_socket_allows_no_unix_socket(paths):
    content = sandbox.settings("select", SessionPaths(**{**paths.__dict__, "socket": None}))
    assert content["sandbox"]["network"]["allowUnixSockets"] == []


@pytest.mark.parametrize("loosen, problem", [
    (lambda c: c["sandbox"].update(failIfUnavailable=False), "sandbox.failIfUnavailable must be true"),
    (lambda c: c["sandbox"].update(excludedCommands=["git"]), "sandbox.excludedCommands must not be set"),
    (lambda c: c["sandbox"]["network"].update(allowLocalBinding=True), "sandbox.network.allowLocalBinding must not be set"),
    (lambda c: c["sandbox"]["network"].update(allowMachLookup=["*"]), "sandbox.network.allowMachLookup must not be set"),
    (lambda c: c["sandbox"]["network"].update(allowAllUnixSockets=True), "allowAllUnixSockets must not be set"),
    (lambda c: c["sandbox"].update(allowAppleEvents=True), "sandbox.allowAppleEvents must not be set"),
    (lambda c: c["sandbox"]["filesystem"].update(disabled=True), "sandbox.filesystem.disabled must be false"),
    (lambda c: c["sandbox"]["filesystem"]["denyRead"].remove("~/Library/Keychains"), "denyRead lacks ~/Library/Keychains"),
    (lambda c: c["sandbox"]["network"].update(allowedDomains=["github.com"]), "allowedDomains must be []"),
    (lambda c: c["sandbox"]["network"].update(strictAllowlist=False), "strictAllowlist must be true"),
    (lambda c: c["sandbox"].update(allowUnsandboxedCommands=True), "allowUnsandboxedCommands must be false"),
    (lambda c: c["permissions"].update(blockReadsOutsideWorkingDirectories=False), "blockReadsOutside"),
    (lambda c: c.update(disableClaudeAiConnectors=False), "disableClaudeAiConnectors must be true"),
    (lambda c: c["sandbox"]["filesystem"]["allowWrite"].append("/Users"), "filesystem.allowWrite"),
    (lambda c: c["sandbox"]["filesystem"]["denyWrite"].pop(), "denyWrite lacks"),
    (lambda c: c["sandbox"]["filesystem"]["denyRead"].pop(4), "denyRead lacks"),  # the shared /private/tmp/claude
    (lambda c: c["sandbox"]["filesystem"]["denyRead"].pop(5), "denyRead lacks"),  # the state directory
    (lambda c: c["permissions"]["allow"].append("Bash(curl *)"), "permissions.allow"),
])
def test_a_loosened_setting_is_named(paths, loosen, problem):
    content = sandbox.settings("implement", paths)
    loosen(content)
    problems = sandbox.check_settings(content, "implement", paths)
    assert any(problem in item for item in problems), problems


def test_settings_file_is_private_and_checked_on_disk(paths, claude):
    spec = sandbox.build(claude, "implement", paths, path=SESSION_PATH)
    mode = stat.S_IMODE(os.stat(paths.settings_file).st_mode)
    assert mode == 0o600
    assert json.loads(paths.settings_file.read_text()) == spec.settings
    assert sandbox.check_spec(spec) == []
    loosened = json.loads(paths.settings_file.read_text())
    loosened["sandbox"]["failIfUnavailable"] = False
    paths.settings_file.write_text(json.dumps(loosened))
    paths.settings_file.chmod(0o600)
    assert "the settings file on disk differs from the session's settings" in sandbox.check_spec(spec)


def test_check_spec_names_an_extra_tool_and_a_linked_worktree(paths, claude):
    spec = sandbox.build(claude, "select", paths, path=SESSION_PATH)
    argv = list(spec.argv)
    argv[argv.index("--tools") + 1] = "Read,Grep,Glob,Bash"
    widened = dataclasses.replace(spec, argv=tuple(argv))
    assert "the argv differs from the launcher's" in sandbox.check_spec(widened)
    (paths.repository / ".git").rmdir()
    (paths.repository / ".git").write_text("gitdir: /elsewhere\n")
    assert any("gitdir" in problem for problem in sandbox.check_spec(spec))


@pytest.mark.parametrize("field, value, problem", [
    ("repository", "relative/repo", "must be absolute"),
    ("exchange", "/tmp/ex*change", "wildcard"),
    ("settings_file", None, "settings_file lies inside the session-writable"),
    ("state_copy", None, "state_copy lies inside the session-writable"),
])
def test_unsafe_paths_are_refused(paths, field, value, problem):
    if value is None:
        value = paths.exchange / ("settings.json" if field == "settings_file" else "state")
    changed = SessionPaths(**{**paths.__dict__, field: Path(value)})
    assert any(problem in item for item in changed.problems()), changed.problems()
    with pytest.raises(SandboxError):
        sandbox.settings("implement", changed)


def test_environment_is_an_allowlist(paths, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 36)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "y" * 30)
    env = sandbox.environment(paths, path=SESSION_PATH, run_variables={"AUTODEV_RUN_ID": RUN_ID})
    assert env == {"PATH": SESSION_PATH, "HOME": str(paths.home), "LANG": "en_US.UTF-8",
                   "CLAUDE_CONFIG_DIR": str(paths.config_dir), "CLAUDE_CODE_TMPDIR": str(paths.tmp_root),
                   "TMPDIR": str(paths.tmp_root), "GH_CONFIG_DIR": str(paths.gh_config),
                   "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "DISABLE_AUTOUPDATER": "1", "AUTODEV_RUN_ID": RUN_ID}
    assert not any(word in name for name in env for word in ("TOKEN", "KEY", "SECRET", "PASSWORD"))
    assert env["TMPDIR"] != os.environ.get("TMPDIR")


@pytest.mark.parametrize("variables", [{"AUTODEV_API_KEY": "x"}, {"GITHUB_TOKEN": "x"}, {"AUTODEV_X": "ghp_" + "a" * 36}])
def test_environment_refuses_secret_names_and_values(paths, variables):
    with pytest.raises(SandboxError) as error:
        sandbox.environment(paths, path=SESSION_PATH, run_variables=variables)
    assert "ghp_" not in str(error.value)


def test_environment_refuses_a_non_empty_gh_config_and_a_relative_path(paths):
    with pytest.raises(SandboxError):
        sandbox.environment(paths, path="bin:/usr/bin")
    (paths.gh_config / "hosts.yml").write_text("github.com: {}\n")
    with pytest.raises(SandboxError, match="GH_CONFIG_DIR must be an empty directory"):
        sandbox.environment(paths, path=SESSION_PATH)


def test_oauth_token_only_in_the_keychain_mode(paths):
    token = "sk-ant-oat01-" + "Z" * 40
    with pytest.raises(SandboxError):
        sandbox.environment(paths, path=SESSION_PATH, oauth_token=token)
    env = sandbox.environment(paths, path=SESSION_PATH, mode="keychain:claude-autodev", oauth_token=token)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert sandbox.check_environment(env, paths, "keychain:claude-autodev") == []
    assert "CLAUDE_CODE_OAUTH_TOKEN is passed only in the keychain login mode" in sandbox.check_environment(env, paths)


def test_login_mode_from_the_config():
    assert sandbox.login_mode(autodev_config.DEFAULTS) == "config-dir"
    assert sandbox.login_mode({"sandbox": {"login": "keychain:claude-autodev"}}) == "keychain:claude-autodev"
    with pytest.raises(SandboxError):
        sandbox.login_mode({"sandbox": {"login": "token:abc"}})


def test_config_validates_the_sandbox_key(service_state_dir):
    assert autodev_config.load(service_state_dir)["sandbox"] == {"login": "config-dir", "decoy_keychain": False,
                                                                 "allowed_plugins": []}
    for value, problem in (({"login": "env"}, "sandbox must have login, decoy_keychain and allowed_plugins"),
                           ({"login": "env", "decoy_keychain": False, "allowed_plugins": []}, "sandbox.login must be"),
                           ({"login": "config-dir", "decoy_keychain": "yes", "allowed_plugins": []},
                            "sandbox.decoy_keychain")):
        with pytest.raises(autodev_config.ConfigError) as error:
            autodev_config.validate({**autodev_config.DEFAULTS, "sandbox": value})
        assert any(problem in item for item in error.value.problems)


def test_config_dir_is_persistent_and_private(service_state_dir):
    first = sandbox.config_dir(service_state_dir)
    (first / "marker").write_text("kept")
    assert sandbox.config_dir(service_state_dir) == first and (first / "marker").exists()
    assert stat.S_IMODE(os.stat(first).st_mode) == 0o700


def test_login_check_refuses_with_the_login_command(tmp_path, paths):
    claude = fake_claude(tmp_path, {"auth": {"loggedIn": False}, "auth_code": 1})
    with pytest.raises(sandbox.LoginRequired) as error:
        sandbox.require_login(claude, paths.config_dir, path=SESSION_PATH, home=paths.home)
    assert error.value.command == f"CLAUDE_CONFIG_DIR={paths.config_dir} claude auth login"
    assert error.value.command in str(error.value)


def test_login_check_passes_without_keeping_account_details(tmp_path, paths):
    claude = fake_claude(tmp_path, {})
    status = sandbox.require_login(claude, paths.config_dir, path=SESSION_PATH, home=paths.home)
    assert status == {"logged_in": True, "detail": "logged in"}
    assert "example.com" not in json.dumps(status)


def test_login_check_on_garbage_and_on_a_missing_binary(tmp_path, paths):
    claude = fake_claude(tmp_path, {"auth": "not an object"})
    assert sandbox.login_status(claude, paths.config_dir, path=SESSION_PATH, home=paths.home)["logged_in"] is False
    missing = sandbox.login_status(str(tmp_path / "absent"), paths.config_dir, path=SESSION_PATH, home=paths.home)
    assert missing["logged_in"] is False and "FileNotFoundError" in missing["detail"]


def test_keychain_secret_never_echoes_output():
    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 44, "secret-looking output", "")

    with pytest.raises(SandboxError) as error:
        sandbox.keychain_secret("claude-autodev", runner=failing)
    assert "secret-looking" not in str(error.value) and "44" in str(error.value)

    def found(argv, **kwargs):
        assert argv == ["/usr/bin/security", "find-generic-password", "-s", "claude-autodev", "-w"]
        return subprocess.CompletedProcess(argv, 0, "token-value\n", "")

    assert sandbox.keychain_secret("claude-autodev", runner=found) == "token-value"


def test_managed_sources_are_found(tmp_path):
    managed = tmp_path / "ClaudeCode"
    managed.mkdir()
    preferences = tmp_path / "Managed Preferences"
    (preferences / "owner").mkdir(parents=True)
    assert sandbox.managed_sources(managed, preferences, "owner") == []
    (managed / "managed-settings.json").write_text("{}")
    (preferences / "owner" / sandbox.MANAGED_DOMAIN).write_text("")
    assert sandbox.managed_sources(managed, preferences, "owner") == [
        str(managed / "managed-settings.json"), str(preferences / "owner" / sandbox.MANAGED_DOMAIN)]


def test_tree_problems_find_hard_links_and_outward_links(paths):
    outside = paths.home / "secret.txt"
    outside.write_text("x")
    assert sandbox.tree_problems(paths.writable) == []
    os.link(outside, paths.exchange / "hard")
    os.symlink(outside, paths.repository / "link")
    os.symlink(paths.repository / "README.md", paths.repository / "inner")
    problems = sandbox.tree_problems(paths.writable)
    assert len(problems) == 2 and any("hard links" in p for p in problems) and any("symbolic link" in p for p in problems)


def test_layout_and_interpreter_roots(tmp_path_factory):
    root = tmp_path_factory.mktemp("sessions")
    layout = sandbox.session_layout(root, RUN_ID, "implement")
    sandbox.prepare_layout(layout)
    assert layout["repository"] == root / RUN_ID / "implement" / "repo"
    assert layout["exchange"].is_dir() and not layout["repository"].exists()
    assert stat.S_IMODE(os.stat(layout["base"]).st_mode) == 0o700
    with pytest.raises(ValueError):
        sandbox.session_layout(root, "../x", "implement")
    python = Path(os.path.realpath(sys.executable))
    assert sandbox.interpreter_roots(sys.executable) == (python.parent.parent if python.parent.name == "bin"
                                                          else python.parent,)


def test_spec_digest_changes_with_the_environment(paths, claude):
    spec = sandbox.build(claude, "implement", paths, path=SESSION_PATH)
    other = dataclasses.replace(spec, env={**spec.env, "LANG": "C"})
    assert spec.digest() != other.digest() and spec.has_bash and spec.cwd == paths.repository


def test_config_validates_allowed_plugins():
    assert autodev_config.DEFAULTS["sandbox"]["allowed_plugins"] == []
    for value in ("x", [1], ["has space"]):
        with pytest.raises(autodev_config.ConfigError) as error:
            autodev_config.validate({**autodev_config.DEFAULTS,
                                     "sandbox": {"login": "config-dir", "decoy_keychain": False, "allowed_plugins": value}})
        assert any("allowed_plugins" in item for item in error.value.problems)


# --- the run's temporary root (decision 2) -----------------------------------------------------------

def test_the_temporary_root_is_short_private_and_removable(temp_root):
    assert temp_root.parent == sandbox.TMP_BASE and sandbox.TMP_NAME.fullmatch(temp_root.name)
    assert stat.S_IMODE(os.lstat(temp_root).st_mode) == 0o700 and not temp_root.is_symlink()
    assert len(str(sandbox.bash_temp(temp_root)).encode()) <= sandbox.TMP_PATH_LIMIT <= 44
    assert sandbox.temp_root_problems(temp_root) == []
    with pytest.raises(SandboxError):
        sandbox.remove_temp_root(temp_root.parent / "elsewhere")


def test_an_over_long_temporary_root_is_refused(paths, tmp_path):
    long_root = tmp_path / ("x" * 40)
    long_root.mkdir(mode=0o700)
    changed = dataclasses.replace(paths, tmp_root=long_root)
    assert any("temporary root is too long" in problem for problem in changed.problems())
    with pytest.raises(SandboxError, match="too long"):
        sandbox.settings("implement", changed)


def test_a_temporary_root_the_runner_did_not_create_is_refused(paths, claude, tmp_path):
    spec = sandbox.build(claude, "implement", paths, path=SESSION_PATH)
    os.chmod(paths.tmp_root, 0o755)
    assert any("0700 directory" in problem for problem in sandbox.check_spec(spec))
    os.chmod(paths.tmp_root, 0o700)
    assert sandbox.check_spec(spec) == []


@pytest.mark.parametrize("field", ["claude_temp", "owner_temp", "claude_shared"])
def test_the_temporary_root_must_lie_outside_the_shared_temp_roots(paths, field):
    changed = dataclasses.replace(paths, **{field: paths.tmp_root.parent})
    assert any(f"around {field}" in problem for problem in changed.problems())


# --- read roots (decision 9) --------------------------------------------------------------------------

@pytest.mark.parametrize("root, protected", [
    (lambda p: p.home, "home"),
    (lambda p: p.home.parent, "home"),
    (lambda p: p.home / ".ssh", "ssh"),
    (lambda p: p.home / "Library", "library"),
    (lambda p: p.home / "Library/Keychains", "keychains"),
    (lambda p: p.state_dir, "state_dir"),
    (lambda p: p.config_dir.parent, "config_dir"),
    (lambda p: p.owner_temp, "owner_temp"),
])
def test_a_read_root_that_reopens_a_denied_place_is_refused(paths, root, protected):
    changed = dataclasses.replace(paths, read_roots=(root(paths),))
    assert any(f"equals or contains {protected}" in problem for problem in changed.problems()), changed.problems()
    with pytest.raises(SandboxError):
        sandbox.settings("implement", changed)


def test_an_interpreter_prefix_under_home_is_a_valid_read_root(paths):
    prefix = paths.home / ".local/share/uv/python/cpython-3.12"
    (prefix / "bin").mkdir(parents=True)
    changed = dataclasses.replace(paths, read_roots=sandbox.interpreter_roots(prefix / "bin" / "python3"))
    assert changed.problems() == []
    assert sandbox.interpreter_roots(paths.home / "bin" / "python3") == (paths.home,)
    assert any("equals or contains home" in problem for problem in dataclasses.replace(
        paths, read_roots=sandbox.interpreter_roots(paths.home / "bin" / "python3")).problems())


# --- the executable (decision 5) ----------------------------------------------------------------------

def test_the_spec_binds_the_executables_path_and_hash(paths, tmp_path):
    first = Path(fake_claude(tmp_path, {}))
    link = tmp_path / "claude-link"
    link.symlink_to(first)
    spec = sandbox.build(str(link), "implement", paths, path=SESSION_PATH)
    assert spec.executable == (os.path.realpath(first), hashlib.sha256(first.read_bytes()).hexdigest())
    assert sandbox.check_spec(spec) == []
    digest = spec.digest()
    first.write_text(first.read_text() + "# upgraded\n")  # an upgrade behind the same link
    assert any("claude executable differs" in problem for problem in sandbox.check_spec(spec))
    rebuilt = sandbox.build(str(link), "implement", paths, path=SESSION_PATH)
    assert rebuilt.digest() != digest
    with pytest.raises(SandboxError):
        sandbox.build(str(tmp_path / "absent"), "implement", paths, path=SESSION_PATH)


def test_the_suffixed_claude_keychain_item():
    name = sandbox.claude_keychain_item(Path("/state/schedules/claude-config"))
    assert name.startswith("Claude Code-credentials-") and len(name) == len("Claude Code-credentials-") + 8
