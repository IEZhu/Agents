"""Codex worker isolation and the fallback to the Claude worker (#344), with a fake ``codex``.

The fake (tests/fixtures/fake_codex.py) answers ``--version``, ``sandbox`` and ``exec`` and refuses
flags outside the verified set. Probe commands run through an executor: `Emulator` reads the
profile's ``-D`` parameters and applies the profile's rules itself (any platform); on macOS,
`real_executor` runs them under the real ``/usr/bin/sandbox-exec`` with the generated profile. The
fake ``codex`` itself always runs directly (a Python script under the home directory cannot run
under the profile), so it models the nested-sandbox result by its scenario. The keychain is
answered by the tests (`with_security`), never read; only 127.0.0.1 listeners the tests start are
contacted.
"""
import dataclasses
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from src.schedules import broker, cli, codex_probes, codex_proxy, decoys, probes, records, runner, sandbox
from src.schedules import codex_sandbox as cs
from src.schedules import config as autodev_config
from src.schedules.codex_sandbox import CodexPaths, CodexSandboxError
from src.schedules.scan import HitKind, Scanner

FAKE = Path(__file__).parent / "fixtures" / "fake_codex.py"
PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
RUN_ID = "20261014T120000Z-abcdef"
TEST_NET = "192.0.2.1"  # RFC 5737: never routed; the sandbox refuses the connection before any packet
SERVICE = "autodev-codex-test-key"
MODE = cs.KEYCHAIN_PREFIX + SERVICE
KEY = "sk-proj-" + "T" * 4 + "x7Kp2Lm9Nq4Rs8Tv0WyZ3"  # a stand-in; the tests never read a real keychain


def fake_codex(directory: Path, scenario: dict) -> str:
    """An executable ``<package>/bin/codex`` that runs the fake with ``scenario``."""
    package = directory / "pkg"
    (package / "bin").mkdir(parents=True, exist_ok=True)
    (package / cs.PACKAGE_MANIFEST).write_text('{"entrypoint": "bin/codex"}\n')
    scenario_file = directory / "scenario.json"
    scenario_file.write_text(json.dumps(scenario))
    wrapper = package / "bin" / "codex"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} {scenario_file} \"$@\"\n")
    wrapper.chmod(0o755)
    return str(wrapper)


def set_scenario(codex: str, **changes) -> None:
    scenario_file = Path(codex).parent.parent.parent / "scenario.json"
    scenario = json.loads(scenario_file.read_text())
    scenario.update(changes)
    scenario_file.write_text(json.dumps(scenario))


def real(path) -> Path:
    return Path(os.path.realpath(path))


def fake_venv(directory: Path, exit_code: int = 0) -> Path:
    """A stage environment whose ``bin/python`` is a script that exits ``exit_code`` (255 stands
    for a nix-built interpreter, whose wrapper execs a store path the profile does not allow)."""
    python = directory / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    python.chmod(0o755)
    return directory


@pytest.fixture
def temp_root():
    """The run's temporary root: under /private/tmp as in a run, or under the system temporary
    directory where /private/tmp does not exist, so the emulator tests also run off macOS."""
    base = sandbox.TMP_BASE if sandbox.TMP_BASE.is_dir() else Path(tempfile.gettempdir())
    root = sandbox.make_temp_root(base)
    yield root
    sandbox.remove_temp_root(root, base)


class World:
    def __init__(self, base: Path, state: Path, tmp_root: Path):
        self.base, self.state, self.tmp_root = base, state, tmp_root
        self.home = base / "home"
        for name in (".ssh", "Library/Keychains", ".config/agents-autodev"):
            (self.home / name).mkdir(parents=True)
        (self.home / ".ssh" / "known_hosts").write_text("example.org ssh-ed25519 AAAAfake\n")
        for name in ("tree", "materials", "out", "owner-tmp", "exchange", "repo/.git/hooks", "repo/.git/objects/info",
                     "repo/.git/info", "priv", "inst"):
            (base / name).mkdir(parents=True)
        (base / "tree" / "README.md").write_text("tree\n")
        (base / "repo" / ".git" / "config").write_text("[core]\n\tbare = false\n")
        (base / "repo" / "README.md").write_text("one-off\n")
        self.venv = fake_venv(base / "venv")
        self.schema = base / "schema.json"
        self.schema.write_text('{"type": "object"}\n')
        # A stage reads its output schema from its read-only state copy (#347: --output-schema);
        # `stage_paths` adds both, as a run's stage launch has them.
        self.state_copy = base / "stage-state"
        self.state_copy.mkdir()
        self.stage_schema = self.state_copy / "schema-select.json"
        self.stage_schema.write_text('{"type": "object"}\n')
        directories = cs.prepare_run_directories(tmp_root)
        self.codex_home, self.run_home = directories["codex_home"], directories["run_home"]
        self.codex = fake_codex(base / "fake", {"seen": str(base / "seen.jsonl")})
        self.planted = decoys.ensure(state, self.home)

    def paths(self, kind: str = cs.WORKER, **changes) -> CodexPaths:
        values = dict(kind=kind, home=self.home, codex_home=self.codex_home, tmp_root=self.tmp_root,
                      run_home=self.run_home, out_dir=self.base / "out", profile=self.base / "priv" / f"{kind}.sb",
                      state_dir=self.state, installation=self.base / "inst")
        if kind == cs.WORKER:
            values.update(tree=self.base / "tree", materials=self.base / "materials", schema=self.schema)
        else:
            values.update(repository=self.base / "repo", exchange=self.base / "exchange", venv=self.venv)
        values.update(changes)
        return CodexPaths(**values)

    def stage_paths(self, **changes) -> CodexPaths:
        """A stage launch as the run sequence builds it: the state copy and the output schema in it."""
        return self.paths(cs.STAGE, **{"read_roots": (self.state_copy,), "schema": self.stage_schema, **changes})

    def targets(self, *, runner_targets=None) -> codex_probes.CodexTargets:
        return codex_probes.CodexTargets(state_dir=self.state, secrets_dir=self.home / ".config" / "agents-autodev",
                                         owner_temp=self.base / "owner-tmp", runner=runner_targets)

    def seen(self) -> list[dict]:
        return [json.loads(line) for line in (self.base / "seen.jsonl").read_text().splitlines()]


@pytest.fixture
def world(tmp_path_factory, service_state_dir, temp_root):
    return World(real(tmp_path_factory.mktemp("cdx")), service_state_dir, temp_root)


def build(world, paths=None, **options):
    options.setdefault("proxy_port", 40000)
    options.setdefault("api_key", KEY)
    options.setdefault("path", PATH)
    return cs.build(world.codex, paths or world.paths(), **options)


# --- executors ------------------------------------------------------------------------------------------

def _split(argv):
    assert argv[0] == cs.SANDBOX_EXEC and argv[1] == "-f" and Path(argv[2]).is_file()
    params, index = {}, 3
    while index < len(argv) and argv[index] == "-D":
        name, value = argv[index + 1].split("=", 1)
        params[name] = value
        index += 2
    return params, argv[index:]


def _inside(path: str, roots) -> bool:
    path = os.path.realpath(path)
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots if root)


class Emulator:
    """Applies the outer profile's rules from its ``-D`` parameters; runs allowed commands for real.

    ``holes``: substrings of a command that runs unsandboxed (a sandbox hole); ``force``: substring
    -> a fixed result."""

    def __init__(self, codex: str, *, holes=(), force=None):
        self.codex = os.path.realpath(codex)
        self.holes, self.force = tuple(holes), dict(force or {})
        self.calls = []

    def __call__(self, argv, *, env, cwd, timeout=30, capture=False, text="", max_capture=codex_probes.CAPTURE_LIMIT):
        argv = list(argv)
        if argv[0] != cs.SANDBOX_EXEC:
            return codex_probes.execute(argv, env=env, cwd=cwd, timeout=timeout, capture=True, text=text,
                                        max_capture=max_capture)
        params, tail = _split(argv)
        joined = " ".join(tail)
        self.calls.append(tail)
        for marker, result in self.force.items():
            if marker in joined:
                return result
        if os.path.realpath(tail[0]) == self.codex or any(hole in joined for hole in self.holes) or self.allowed(
                params, tail, env):
            return codex_probes.execute(tail, env=env, cwd=cwd, timeout=timeout, capture=True, text=text,
                                        max_capture=max_capture)
        return probes.Captured("", "Operation not permitted\n", 1, False, False)

    @staticmethod
    def allowed(params, tail, env) -> bool:
        interpreters = [value for name, value in params.items() if name.startswith("INTERPRETER_")]
        readable = [params.get(name) for name in ("TMP_DIR", "OUT_DIR", "TREE", "MATERIALS", "SCHEMA", "REPO",
                                                  "EXCHANGE")] + [v for k, v in params.items()
                                                                  if k.startswith("READ_ROOT_")] + interpreters
        writable = [params.get(name) for name in ("TMP_DIR", "OUT_DIR", "REPO", "EXCHANGE")]
        literals = {value for name, value in params.items() if name == "REPO" or (
            name.startswith("REPO_GIT") and name != "REPO_GIT_HOOKS")}

        def blocked(target: str) -> bool:
            target = os.path.realpath(target)
            return target in literals or _inside(target, [params.get("REPO_GIT_HOOKS")]) or not _inside(target, writable)

        command = tail[0]
        if command in ("/bin/cat", "/bin/ls"):
            return _inside(tail[-1], readable)
        if command == "/bin/sh":
            script = tail[2].replace("$TMPDIR", env["TMPDIR"]).replace('"', "")
            moves = re.findall(r"\bmv (\S+) (\S+)", script)
            writes = re.findall(r">>?\s*(\S+)", script)
            return not any(blocked(shlex.split(path)[0]) for pair in moves for path in pair) and not any(
                blocked(shlex.split(path)[0]) for path in writes)
        if command == codex_probes.NC:
            return tail[-2] == "127.0.0.1" and tail[-1] == params["PROXY_PORT"]
        if command == codex_probes.CURL:
            return "-x" in tail  # only through the proxy, which decides
        return _inside(command, interpreters)


def real_sandbox_usable() -> bool:
    if sys.platform != "darwin" or not os.path.exists(cs.SANDBOX_EXEC):
        return False
    try:
        return subprocess.run([cs.SANDBOX_EXEC, "-p", '(version 1)(allow default)(deny file-read* (literal "/x-344"))',
                               "/usr/bin/true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_seatbelt = pytest.mark.skipif(not real_sandbox_usable(), reason="needs macOS sandbox-exec (not nested)")


def real_executor(codex: str):
    """The real sandbox-exec for every probe command; the fake codex runs directly."""
    codex = os.path.realpath(codex)

    def run(argv, *, env, cwd, timeout=30, capture=False, text="", max_capture=codex_probes.CAPTURE_LIMIT):
        argv = list(argv)
        if argv[0] == cs.SANDBOX_EXEC:
            _, tail = _split(argv)
            if os.path.realpath(tail[0]) == codex:
                argv = tail
        return codex_probes.execute(argv, env=env, cwd=cwd, timeout=timeout, capture=capture, text=text,
                                    max_capture=max_capture)
    return run


def with_security(answer):
    """A command runner that answers ``security`` itself (never the real keychain) and runs the rest."""
    def run(argv, **kwargs):
        if argv[0] == sandbox.SECURITY:
            return answer(argv)
        return subprocess.run(argv, **kwargs)
    return run


def _keychain_answer(argv, key=KEY):
    if SERVICE in argv and "-w" in argv:
        return subprocess.CompletedProcess(argv, 0, key + "\n", "")
    return subprocess.CompletedProcess(argv, 44, "", "")


keychain = with_security(_keychain_answer)
no_keychain = with_security(lambda argv: subprocess.CompletedProcess(argv, 44, "", ""))


def decide(world, *, kind=cs.WORKER, execute=None, mode=MODE, platform="darwin", paths=None,
           command_runner=keychain, proxy=None, persistent=None, resolver=lambda host: TEST_NET, **options):
    paths = paths or world.paths(kind)
    with codex_proxy.ConnectProxy(cs.allowed_hosts()) as own:
        return codex_probes.decide(world.codex, paths, path=PATH, proxy=proxy or own, mode=mode,
                                   targets=world.targets(), persistent=world.planted if persistent is None else persistent,
                                   directory=world.state, run_id=RUN_ID, effort="high", platform=platform,
                                   execute_=execute or Emulator(world.codex), command_runner=command_runner,
                                   resolver=resolver, **options)


def worker_ids(world, *extra) -> set[str]:
    """Every model-free probe ID of a worker in `World` (no read roots, no decoy keychain item)."""
    reads = {f"read.{decoy.label}" for decoy in world.planted if decoy.path} | {"read.owner-temp-decoy"}
    return reads | {"control.codex-start", "control.proxy-port", "control.write-output", "control.write-tmpdir",
                    "control.read-materials", "control.list-tree", "list.ssh-dir", "list.keychains-dir",
                    "list.secrets-dir", "list.home", "list.state-dir", "write.decoy-dir", "write.private-dir",
                    "write.tree", "write.materials", "network.direct", "network.proxy-refused",
                    "network.local-listener", "launchd.submit", *extra}


def stage_ids(world, *extra) -> set[str]:
    """Every model-free probe ID of a stage in `World`."""
    reads = {f"read.{decoy.label}" for decoy in world.planted if decoy.path} | {"read.owner-temp-decoy"}
    return reads | {"control.codex-start", "control.proxy-port", "control.write-output", "control.write-tmpdir",
                    "control.read-exchange", "control.write-repository", "control.write-exchange",
                    "control.venv-python", "control.git", "list.ssh-dir", "list.keychains-dir", "list.secrets-dir",
                    "list.home",
                    "list.state-dir", "write.decoy-dir", "write.private-dir", "write.venv", "write.repo-git-config",
                    "write.repo-git-hooks", "write.repo-git-rename", "network.direct", "network.proxy-refused",
                    "network.local-listener", "launchd.submit", *extra}


def exec_ids(world, *extra) -> set[str]:
    """Every check ID of the stage probe session through ``codex exec`` (`stage`'s runner targets)."""
    reads = {f"read.{decoy.label}.script" for decoy in world.planted if decoy.path} | {"read.owner-temp-decoy.script"}
    if (world.state / "schedules" / decoys.REGISTRY_NAME).exists():
        reads.add("read.decoy-registry.script")
    return reads | {
        "control.exchange.script", "control.repo.script", "control.tmpdir.script", "control.seed.script",
        "control.git.script", "control.sandbox-loaded.script", "list.ssh-dir.script", "list.keychains-dir.script",
        "list.home.script",
        "list.secrets-dir.script", "list.state-dir.script", "list.owner-temp.script", "git.config-global.script",
        "keychain.autodev-test-item.script", "network.external.script", "network.external-noproxy.script",
        "network.metadata.script", "network.dns.script", "network.udp.script", "network.listen.script",
        "network.local-listener.script", "socket.foreign.script", "launchd.submit.script",
        "apple-events.osascript.script", "launch-services.open.script", "write.home.script", "write.state-dir.script",
        "write.owner-temp.script", "write.private-dir.script", "write.runs.script", "write.installation.script",
        "write.schedules.script", "write.venv.script", "write.repo-git-config.script", "write.repo-git-hooks.script",
        "link.symlink-read.script", "link.hardlink.script", "link.dir-symlink.script", "observe.env.script", *extra}


def keychain_decoy() -> decoys.Decoy:
    """A registered decoy keychain item (never created: the tests answer ``security`` themselves)."""
    return decoys.Decoy("keychain-item", None, decoys.new_marker(), "keychain", "autodev-decoy-svc", "autodev-decoy-acct")


def keychain_with_decoy(argv):
    """`_keychain_answer`, plus: the decoy item exists outside the sandbox."""
    if "autodev-decoy-svc" in argv and "-w" not in argv:
        return subprocess.CompletedProcess(argv, 0, "", "")
    return _keychain_answer(argv)


# --- argv -----------------------------------------------------------------------------------------------

def test_worker_argv_starts_with_the_outer_profile_and_never_widens(world):
    launch = build(world, model="gpt-6.1-sol", effort="high")
    argv = list(launch.argv)
    assert argv[:3] == [cs.SANDBOX_EXEC, "-f", str(world.base / "priv" / "worker.sb")]
    command = launch.command
    codex_real = os.path.realpath(world.codex)
    assert command[:12] == [codex_real, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "-s",
                            "read-only", "--skip-git-repo-check", "-C", str(world.base / "tree"), "--output-schema",
                            str(world.schema)]
    assert command[12:14] == ["-o", str(world.base / "out" / cs.OUTPUT_NAME)]
    assert command[-5:] == ["-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high", "-"]
    for switch in ("hooks", "plugins", "remote_plugin", "apps"):
        assert ["--disable", switch] in [command[i:i + 2] for i in range(len(command))]
    assert "shell_environment_policy.inherit=core" in command and "project_doc_max_bytes=0" in command
    assert not any(item.startswith(("--add-dir", "--dangerously", "--approve-for-me", "--profile", "-p"))
                   or "danger-full-access" in item for item in launch.command)
    assert cs.check_argv(argv, launch.paths.profile) == []
    assert cs.check_launch(launch) == []


def test_stage_argv_writes_only_the_one_off_repository(world):
    launch = build(world, world.stage_paths())
    assert list(launch.argv[:3]) == [cs.SANDBOX_EXEC, "-f", str(world.base / "priv" / "stage.sb")]
    command = launch.command
    assert command[5:10] == ["-s", "workspace-write", "--skip-git-repo-check", "-C", str(world.base / "repo")]
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in command and "--json" in command
    assert command[-1] == "-"
    # The stage's output schema (#347), read from its state copy, a read-only root.
    assert command[command.index("--output-schema") + 1] == str(world.stage_schema)
    assert command.index("--json") < command.index("--output-schema") < command.index("-o")
    assert "--output-schema" not in build(world, world.paths(cs.STAGE)).command
    with pytest.raises(cs.CodexSandboxError, match="schema must lie inside one of its read-only roots"):
        build(world, world.stage_paths(read_roots=()))
    assert not any(item.startswith("--add-dir") for item in launch.argv)
    names = [name for name, _ in launch.parameters]
    assert {"REPO", "EXCHANGE", "REPO_GIT", "REPO_GIT_CONFIG", "REPO_GIT_HOOKS", "REPO_GIT_COMMONDIR",
            "REPO_GIT_OBJECTS_ALTERNATES", "INTERPRETER_0"} <= set(names) and "TREE" not in names
    assert dict(launch.parameters)["INTERPRETER_0"] == str(world.venv)
    assert '(deny file-write* (literal (param "REPO")) (literal (param "REPO_GIT"))' in launch.profile
    assert '(allow process-exec (subpath (param "INTERPRETER_0")))' in launch.profile


@pytest.mark.parametrize("argv, fragment", [
    (["--add-dir", "/x"], "--add-dir"), (["--add-dir=/x"], "--add-dir"), (["--approve-for-me"], "--approve-for-me"),
    (["--dangerously-bypass-approvals-and-sandbox"], "--dangerously"), (["-p", "work"], "-p"),
    (["--profile", "work"], "--profile"), (["-s", "danger-full-access"], "danger-full-access"),
    (["-c", 'sandbox_mode="danger-full-access"'], "sandbox_mode"),
    (["-c", "sandbox_permissions=[\"disk-full-read-access\"]"], "sandbox_permissions"),
    (["--config=sandbox_workspace_write.writable_roots=[\"/\"]"], "sandbox_workspace_write.writable_roots"),
    (["-csandbox_workspace_write.network_access=true"], "sandbox_workspace_write.network_access"),
])
def test_check_argv_refuses_widening_flags(world, argv, fragment):
    profile = world.base / "priv" / "x.sb"
    problems = cs.check_argv([cs.SANDBOX_EXEC, "-f", str(profile), "/bin/codex", "exec", *argv], profile)
    assert any(fragment in problem for problem in problems), problems
    assert cs.check_argv(["/bin/codex", "exec"], profile)
    assert cs.check_argv([cs.SANDBOX_EXEC, "-f", str(profile), "/bin/codex", "exec", *cs.STAGE_SWITCHES], profile) == []


def test_effort_values_are_the_known_list(world):
    assert cs.check_effort("ultra") == "ultra" and cs.check_effort(None) is None
    with pytest.raises(CodexSandboxError, match="model_reasoning_effort"):
        build(world, effort="turbo")
    with pytest.raises(CodexSandboxError, match="plain name"):
        build(world, model="gpt; rm -rf /")
    config = autodev_config.load(world.state)
    config["delegates"]["code-review-pass"]["codex"]["effort"] = "turbo"
    config["delegates"]["code-review-pass"]["effort_followup"] = "bogus"
    with pytest.raises(autodev_config.ConfigError) as raised:
        autodev_config.validate(config)
    assert len([p for p in raised.value.problems if "Codex effort must" in p]) == 1
    assert len([p for p in raised.value.problems if "Codex effort_followup" in p]) == 1
    config = autodev_config.load(world.state)
    config["stages"]["select"]["claude"]["effort"] = "turbo"  # a Claude effort is not checked against this list
    autodev_config.validate(config)


@pytest.mark.parametrize("login, problem", [
    ("keychain:codex-key", None), (None, None),
    ("api-key", "codex.login api-key was removed"), ("chatgpt", "codex.login chatgpt was removed"),
    ("keychain:", "codex.login must be keychain:<service> or null"),
    ("config-dir", "codex.login must be keychain:<service> or null"),
])
def test_codex_login_configuration(service_state_dir, login, problem):
    config = {**autodev_config.load(service_state_dir), "codex": {"login": login}}
    if problem is None:
        autodev_config.validate(config)
        assert cs.login_mode(config) == login
    else:
        with pytest.raises(autodev_config.ConfigError) as raised:
            autodev_config.validate(config)
        assert any(problem in item for item in raised.value.problems)
        if login in ("api-key", "chatgpt"):
            assert any("keychain:<service>" in item for item in raised.value.problems)
        with pytest.raises(CodexSandboxError, match="keychain:<service>"):
            cs.login_mode(config)
    assert autodev_config.DEFAULTS["codex"] == {"login": None}
    assert cs.login_mode(autodev_config.load(service_state_dir)) is None


# --- the profile ----------------------------------------------------------------------------------------

def test_profile_names_no_path_and_a_path_cannot_inject_rules(world):
    hostile = world.base / 'mat")(allow default)(allow file-write* (subpath "/'
    hostile.mkdir()
    normal = build(world)
    injected = build(world, world.paths(materials=hostile, profile=world.base / "priv" / "h.sb"))
    assert injected.profile == normal.profile
    for launch in (normal, injected):
        for _, value in launch.parameters:
            assert value not in launch.profile
    assert ("MATERIALS", str(hostile)) in injected.parameters
    assert "(allow default)" not in injected.profile and "(deny default)" in injected.profile
    found = os.lstat(world.base / "priv" / "h.sb")
    assert stat.S_IMODE(found.st_mode) == 0o600 and cs.read_profile(world.base / "priv" / "h.sb") == injected.profile


def test_the_profile_limits_sysctl_and_drops_preferences(world):
    profile = build(world).profile
    assert "(allow sysctl-read)" not in profile and '(sysctl-name "kern.bootargs")' in profile
    assert '(sysctl-name-prefix "kern.proc.pid.")' in profile and "/Library/Preferences" not in profile
    assert '(deny process-exec (regex #"^/usr/bin/(perl|' in profile
    assert '(literal "/Library/Developer/CommandLineTools/usr/bin/git")' in profile


@pytest.mark.parametrize("kind", cs.KINDS)
def test_read_roots_are_readable_but_never_writable(world, kind):
    root = world.base / "state-copy"
    root.mkdir()
    launch = build(world, world.paths(kind, read_roots=(root,)))
    assert dict(launch.parameters)["READ_ROOT_0"] == str(root)
    lines = launch.profile.splitlines()
    reads = [line for line in lines if line.startswith("(allow file-read* (subpath (param \"TMP_DIR\"))")]
    writes = [line for line in lines if line.startswith("(allow file-write*")]
    assert len(reads) == 1 and '(subpath (param "READ_ROOT_0"))' in reads[0]
    assert len(writes) == 1 and "READ_ROOT" not in writes[0] and "INTERPRETER" not in writes[0]


def test_parameters_are_resolved_and_ancestors_get_metadata(world):
    deep = world.home / "Library" / "Application Support" / "state" / "out"
    deep.mkdir(parents=True, mode=0o700)
    link = world.base / "tree-link"
    link.symlink_to(world.base / "tree")
    # The run's output directory inside the state, which lives in ~/Library on macOS (the only root
    # allowed inside either).
    launch = build(world, world.paths(out_dir=deep, tree=link, state_dir=deep.parent))
    params = dict(launch.parameters)
    assert params["TREE"] == str(world.base / "tree") and params["OUT_DIR"] == str(deep)
    assert "CODEX_HOME" not in params and params["TMP_DIR"] == str(real(world.tmp_root))
    ancestors = sorted(value for name, value in launch.parameters if name.startswith("ANCESTOR_"))
    assert ancestors == sorted(str(path) for path in (world.home / "Library", world.home / "Library" / "Application Support",
                                                      world.home / "Library" / "Application Support" / "state"))
    for name in params:
        if name.startswith("ANCESTOR_"):
            assert f'(literal (param "{name}"))' in launch.profile
    assert params["CODEX_PKG"] == str(real(world.codex).parent.parent) and params["PROXY_PORT"] == "40000"


def test_a_tampered_profile_or_changed_argv_is_refused(world):
    launch = build(world)
    world.paths().profile.write_text(launch.profile + "(allow default)\n")
    assert any("profile on disk differs" in problem for problem in cs.check_launch(launch))
    launch = build(world)
    os.chmod(launch.paths.profile, 0o644)
    assert any("0600" in problem for problem in cs.check_launch(launch))
    launch = build(world)
    widened = dataclasses.replace(launch, argv=(*launch.argv[:-1], "--add-dir", "/", "-"))
    assert cs.check_launch(widened)


@pytest.mark.parametrize("change, fragment", [
    (lambda w: {"out_dir": w.home}, "equals or contains home"),
    (lambda w: {"tree": w.base / "out" / "t"}, "overlap"),
    (lambda w: {"profile": w.base / "out" / "p.sb"}, "the profile lies inside out_dir"),
    (lambda w: {"materials": w.home / ".ssh"}, "equals or contains ssh"),
    (lambda w: {"materials": w.home / ".config"}, "equals or contains secrets"),
    (lambda w: {"materials": w.base}, "equals or contains installation"),
    (lambda w: {"tree": w.state.parent}, "equals or contains state"),
    (lambda w: {"out_dir": w.tmp_root / "o"}, "must not lie inside one another"),
    (lambda w: {"codex_home": w.base / "out"}, "codex_home must be a directory directly inside the temporary root"),
    (lambda w: {"run_home": w.codex_home}, "must be different directories"),
    (lambda w: {"tree": None}, "a worker needs tree"),
    (lambda w: {"venv": w.venv}, "only a stage runs its environment's interpreter"),
    (lambda w: {"schema": Path("relative.json")}, "must be absolute"),
    (lambda w: {"kind": cs.STAGE, "repository": w.base / "tree", "exchange": w.base / "exchange", "venv": w.venv},
     ".git must be a directory"),
    (lambda w: {"kind": cs.STAGE, "repository": w.base / "repo", "exchange": w.base / "exchange", "venv": None},
     "a stage needs venv"),
    (lambda w: {"kind": cs.STAGE, "repository": w.base / "repo", "exchange": w.base / "exchange", "venv": Path("/usr")},
     "too broad to read and execute"),
    # Nor inside a place that holds secrets (the round-2 review read a fake key through such a root).
    (lambda w: {"read_roots": (w.home / ".ssh" / "keys",)}, "read_roots[0] (%s) lies inside ssh"),
    (lambda w: {"materials": w.home / "Library" / "Mail"}, "materials (%s) lies inside library"),
    (lambda w: {"tree": w.home / ".config" / "agents-autodev" / "t"}, "tree (%s) lies inside secrets"),
    (lambda w: {"schema": w.state / "schedules" / "schema.json"}, "schema (%s) lies inside state"),
    (lambda w: {"tree": w.state / "schedules" / "runs" / "r" / "codex-out"}, "tree (%s) lies inside state"),
    (lambda w: {"out_dir": w.home / ".ssh" / "out"}, "out_dir (%s) lies inside ssh"),
])
def test_unsafe_paths_are_refused(world, change, fragment):
    values = change(world)
    if values.get("kind") == cs.STAGE:
        values = {"tree": None, "materials": None, "schema": None, **values}
    if "%s" in fragment:
        root = next(value for value in values.values() if value is not None)
        fragment = fragment % real(root[0] if isinstance(root, tuple) else root)
    paths = world.paths(**values)
    assert any(fragment in problem for problem in paths.problems()), paths.problems()
    with pytest.raises(CodexSandboxError, match="unsafe Codex paths"):
        build(world, paths)


def test_only_the_output_directory_may_lie_inside_the_state(world):
    # On macOS the state lives in ~/Library/Application Support, and a run's output directory in it.
    state = world.home / "Library" / "Application Support" / "Agents-Core" / "autodev"
    out = state / "schedules" / "runs" / RUN_ID / "codex-out"
    out.mkdir(parents=True)
    assert world.paths(state_dir=state, out_dir=out).problems() == []
    assert world.paths(state_dir=state, out_dir=out, read_roots=(state / "copy",)).problems() == [
        f"read_roots[0] ({real(state / 'copy')}) lies inside library ({real(world.home / 'Library')})",
        f"read_roots[0] ({real(state / 'copy')}) lies inside state ({real(state)})"]
    elsewhere = world.home / "Library" / "Caches" / "out"  # inside ~/Library, outside the state
    assert world.paths(state_dir=state, out_dir=elsewhere).problems() == [
        f"out_dir ({real(elsewhere)}) lies inside library ({real(world.home / 'Library')})"]


def test_the_package_must_be_a_codex_package_outside_the_run(world, tmp_path):
    loose = tmp_path / "loose" / "codex"
    loose.parent.mkdir()
    loose.write_text("#!/bin/sh\n")
    loose.chmod(0o755)
    with pytest.raises(CodexSandboxError, match="codex package"):
        cs.build(str(loose), world.paths(), path=PATH, proxy_port=40000, api_key=KEY)
    inside = fake_codex(world.base / "tree" / "fake", {})
    with pytest.raises(CodexSandboxError, match="overlap"):
        cs.build(inside, world.paths(), path=PATH, proxy_port=40000, api_key=KEY)


# --- environment ----------------------------------------------------------------------------------------

def test_environment_is_an_allowlist(world):
    paths = world.paths()
    env = cs.environment(paths, path=PATH, proxy_port=40000, api_key=KEY)
    assert set(env) == {"PATH", "HOME", "LANG", "CODEX_HOME", "TMPDIR", "HTTPS_PROXY", "HTTP_PROXY", "CODEX_API_KEY"}
    assert env["HOME"] == str(real(world.run_home)) and env["CODEX_HOME"] == str(real(world.codex_home))
    assert env["TMPDIR"] == str(real(world.tmp_root) / cs.RUN_TMP_NAME) and env["HTTPS_PROXY"] == "http://127.0.0.1:40000"
    assert env["CODEX_API_KEY"] == KEY and "OPENAI_API_KEY" not in env
    for directory in (world.run_home, world.codex_home, real(world.tmp_root) / cs.RUN_TMP_NAME):
        assert Path(directory).parent == real(world.tmp_root) and stat.S_IMODE(os.lstat(directory).st_mode) == 0o700
    with pytest.raises(CodexSandboxError, match="needs the API key"):
        cs.environment(paths, path=PATH, proxy_port=40000, api_key="")
    assert "GH_TOKEN is not on the Codex allowlist (it names a secret)" in cs.check_environment(
        {**env, "GH_TOKEN": "x"}, paths, 40000)
    assert "CODEX_API_KEY is missing" in cs.check_environment(
        {name: value for name, value in env.items() if name != "CODEX_API_KEY"}, paths, 40000)
    assert any("secret-like" in p for p in cs.check_environment(
        {**env, "LANG": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"}, paths, 40000))
    assert any("must not be empty" in p for p in cs.check_environment({**env, "CODEX_HOME": ""}, paths, 40000))
    with pytest.raises(CodexSandboxError, match="PATH"):
        cs.environment(paths, path="bin:/usr/bin", proxy_port=40000, api_key=KEY)


def tool_dir(world, name: str = "nixbin", *, exit_code: int = 126) -> Path:
    """A directory outside the profile's executable directories with a ``git`` and a ``gh`` that
    exit ``exit_code`` (126 stands for a program the profile does not let start, like a nix git)."""
    directory = world.base / name
    directory.mkdir()
    for program in ("git", "gh"):
        (directory / program).write_text(f"#!/bin/sh\nexit {exit_code}\n")
        (directory / program).chmod(0o755)
    return directory


def test_the_launch_path_holds_only_directories_the_profile_can_execute(world):
    tools = tool_dir(world)
    runner_path = f"{tools}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    launch = build(world, world.paths(cs.STAGE), path=runner_path)
    assert launch.env["PATH"] == "/usr/bin:/bin"  # the first git on the runner's PATH is not reachable
    assert shutil.which("git", path=launch.env["PATH"]) == "/usr/bin/git"
    package = Path(launch.package)
    assert cs.launch_path(f"{package}/bin:{tools}:/usr/sbin", launch.paths, package) == f"{package}/bin:/usr/bin:/bin"
    assert cs.launch_path("/usr/local/bin", launch.paths, package) == "/usr/bin:/bin"
    venv_bin = str(real(world.venv) / "bin")  # a stage's environment may run
    assert cs.launch_path(f"{venv_bin}:/bin", launch.paths, package) == f"{venv_bin}:/bin:/usr/bin"
    assert cs.launch_path(f"{venv_bin}:/bin", world.paths(), package) == "/bin:/usr/bin"  # a worker's may not
    widened = dataclasses.replace(launch, env={**launch.env, "PATH": f"{tools}:/usr/bin:/bin"})
    assert any("PATH must name only directories" in problem for problem in cs.check_launch(widened))
    assert cs.check_launch(launch) == []


# --- the run's CODEX_HOME and the login -----------------------------------------------------------------

@pytest.mark.parametrize("entry, directory", [
    (".env", False), ("AGENTS.md", False), ("agents.override.MD", False), ("hooks.json", False), ("config.toml", False),
    ("work.config.toml", False), ("rules", True), ("plugins", True), ("skills", True), ("models_cache.json", False),
    ("auth.json", False)])
def test_hygiene_refuses_what_codex_would_load(world, entry, directory):
    path = world.codex_home / entry
    path.mkdir() if directory else path.write_text("x\n")
    assert cs.hygiene_problems(world.codex_home) == [f"CODEX_HOME holds {entry}, which codex would load"]


def test_hygiene_is_an_allowlist_of_what_codex_creates(world):
    assert cs.hygiene_problems(world.codex_home) == []
    for name in ("tmp/arg0", "log"):
        (world.codex_home / name).mkdir(parents=True)
    for name in ("state_5.sqlite", "logs_1.sqlite-wal"):
        (world.codex_home / name).write_text("")
    assert cs.hygiene_problems(world.codex_home) == []
    (world.codex_home / "sessions").mkdir()
    (world.codex_home / "notes.txt").write_text("x\n")
    assert cs.hygiene_problems(world.codex_home) == ["CODEX_HOME holds notes.txt, which codex does not create",
                                                     "CODEX_HOME holds sessions, which codex does not create"]
    (world.codex_home / "sessions").rmdir()
    (world.codex_home / "notes.txt").unlink()
    os.rename(world.codex_home / "log", world.base / "elsewhere")
    (world.codex_home / "log").symlink_to(world.base / "elsewhere")
    assert cs.hygiene_problems(world.codex_home) == ["CODEX_HOME holds log, which codex does not create"]
    os.unlink(world.codex_home / "log")
    os.chmod(world.codex_home, 0o755)
    assert cs.hygiene_problems(world.codex_home) == ["CODEX_HOME must be a 0700 directory of this user, not a link"]
    os.chmod(world.codex_home, 0o700)


def test_run_directories_are_new_and_private(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    found = cs.prepare_run_directories(root)
    assert set(found) == {"run_home", "codex_home"} and (root / cs.RUN_TMP_NAME).is_dir()
    with pytest.raises(FileExistsError):
        cs.prepare_run_directories(root)


# --- the proxy ------------------------------------------------------------------------------------------

class Echo:
    """A local TCP listener that echoes (each connection in its own thread), standing in for an
    upstream host."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while True:
            try:
                connection, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.echo, args=(connection,), daemon=True).start()

    @staticmethod
    def echo(connection):
        with connection:
            try:
                while data := connection.recv(4096):
                    connection.sendall(data)
            except OSError:
                pass

    def connect(self, address, timeout):
        self.address = address
        return socket.create_connection(self.sock.getsockname(), timeout=timeout)

    def close(self):
        self.sock.close()


def _request(port: int, head: bytes, payload: bytes = b"") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(head)
        answer = client.recv(4096)
        if answer.startswith(b"HTTP/1.1 200") and payload:
            client.sendall(payload)
            answer += client.recv(4096)
        return answer


CONNECT = b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\n\r\n"


def _tunnel(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(CONNECT)
    assert client.recv(4096).startswith(b"HTTP/1.1 200")
    return client


def _until_closed(client: socket.socket, seconds: float = 5.0) -> bool:
    """Whether the proxy closes ``client`` within ``seconds`` (echoed bytes are read and dropped)."""
    client.settimeout(seconds)
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            if client.recv(65536) == b"":
                return True
    except (TimeoutError, socket.timeout):
        return False
    except OSError:
        return True
    return False


def test_proxy_tunnels_only_allowlisted_hosts_on_443():
    echo = Echo()
    assert cs.allowed_hosts() == ("api.openai.com",)
    with codex_proxy.ConnectProxy(cs.allowed_hosts(), connect=echo.connect) as proxy:
        answer = _request(proxy.port, CONNECT, b"tls-bytes")
        assert answer.startswith(b"HTTP/1.1 200") and answer.endswith(b"tls-bytes")
        assert echo.address == ("api.openai.com", 443)
        for head in (b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", b"CONNECT api.openai.com:80 HTTP/1.1\r\n\r\n",
                     b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n", b"CONNECT auth.openai.com:443 HTTP/1.1\r\n\r\n",
                     b"CONNECT evil.api.openai.com:443 HTTP/1.1\r\n\r\n", b"GET http://api.openai.com/ HTTP/1.1\r\n\r\n",
                     b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n"):
            assert _request(proxy.port, head).startswith(b"HTTP/1.1 403")
        assert _request(proxy.port, b"X" * (codex_proxy.HEADER_LIMIT + 10)).startswith(b"HTTP/1.1 403")
        assert proxy.targets(allowed=True) == ["api.openai.com:443"]
        assert {"example.com:443", "api.openai.com:80", "chatgpt.com:443", "auth.openai.com:443",
                "evil.api.openai.com:443", "127.0.0.1:443"} <= set(proxy.targets(allowed=False))
        assert {"method": "GET", "target": "http:??api.openai.com?", "allowed": False} in proxy.requests
        assert proxy.tunnels == 1 and proxy.accepted >= 8
    with pytest.raises(ValueError):
        codex_proxy.ConnectProxy(["not a host"])
    with pytest.raises(ValueError, match="max_tunnels"):
        codex_proxy.ConnectProxy(cs.API_HOSTS, max_tunnels=0)
    echo.close()


def test_proxy_bounds_the_whole_request_head():
    echo = Echo()
    with codex_proxy.ConnectProxy(cs.API_HOSTS, connect=echo.connect, max_clients=1, header_timeout=0.6) as proxy:
        slow = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        started, closed = time.monotonic(), False
        for byte in CONNECT[:-4]:  # one byte every 0.2 s: each recv is far within the timeout
            try:
                slow.sendall(bytes([byte]))
                slow.settimeout(0.2)
                closed = slow.recv(16) == b""
            except (TimeoutError, socket.timeout):
                pass
            except OSError:
                closed = True
            if closed or time.monotonic() - started > 4:
                break
        assert closed and time.monotonic() - started < 2.5
        slow.close()
        assert codex_probes._wait_for(lambda: proxy._clients == 0)
        assert _request(proxy.port, CONNECT).startswith(b"HTTP/1.1 200")  # the slot is free again
    with pytest.raises(ValueError, match="header_timeout"):
        codex_proxy.ConnectProxy(cs.API_HOSTS, header_timeout=0)
    echo.close()


def test_proxy_close_ends_open_tunnels():
    echo = Echo()
    proxy = codex_proxy.ConnectProxy(cs.API_HOSTS, connect=echo.connect).start()
    client = _tunnel(proxy.port)
    proxy.close()
    assert _until_closed(client)
    client.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", proxy.port), timeout=1).close()
    echo.close()


def test_proxy_caps_connections_and_tunnels():
    echo = Echo()
    with codex_proxy.ConnectProxy(cs.API_HOSTS, connect=echo.connect, max_clients=2, max_tunnels=1) as proxy:
        first = _tunnel(proxy.port)
        assert _request(proxy.port, CONNECT).startswith(b"HTTP/1.1 503")  # a second tunnel
        assert {"method": "CONNECT", "target": "api.openai.com:443", "allowed": False,
                "reason": "tunnel limit"} in proxy.requests
        idle = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)  # holds the second client slot
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as third:
            third.settimeout(5)
            assert third.recv(4096).startswith(b"HTTP/1.1 503")
        assert proxy.busy == 2 and proxy.tunnels == 1
        first.sendall(b"still-open")
        assert first.recv(4096) == b"still-open"
        idle.close()
        first.close()
    echo.close()


@pytest.mark.parametrize("limits, reason, keep_talking", [
    ({"idle_timeout": 0.4}, "idle", False),
    ({"tunnel_seconds": 0.8, "idle_timeout": 5}, "time", True),
    ({"tunnel_bytes": 1000}, "bytes", True),
])
def test_proxy_ends_idle_long_and_large_tunnels(limits, reason, keep_talking):
    echo = Echo()
    with codex_proxy.ConnectProxy(cs.API_HOSTS, connect=echo.connect, **limits) as proxy:
        client = _tunnel(proxy.port)
        closed = False
        deadline = time.monotonic() + 5
        while keep_talking and time.monotonic() < deadline and not closed:
            try:
                client.sendall(b"x" * 300)
                client.settimeout(0.2)
                closed = client.recv(65536) == b""
            except (TimeoutError, socket.timeout):
                pass
            except OSError:
                closed = True
        assert closed or _until_closed(client)
        assert codex_probes._wait_for(lambda: proxy.ended[reason] == 1), proxy.ended
        client.close()
    echo.close()


def test_proxy_closes_when_the_process_exits():
    with codex_proxy.ConnectProxy(cs.API_HOSTS) as proxy:
        process = subprocess.Popen(["/bin/sleep", "0.3"])
        proxy.close_when(process)
        assert not proxy.closed
        process.wait()
        assert codex_probes._wait_for(lambda: proxy.closed)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", proxy.port), timeout=1).close()


# --- the decision ---------------------------------------------------------------------------------------

def test_nested_eperm_gives_the_claude_worker_with_the_reason(world):
    decision = decide(world)
    assert (decision.client_requested, decision.client_applied, decision.reason) == (
        "codex", "claude", "nested_sandbox_unavailable")
    report = decision.report
    assert report.nested == {"available": False, "exit_code": 71,
                             "detail": "sandbox-exec: sandbox_apply: Operation not permitted (an inner Seatbelt profile "
                                       "cannot apply)"}
    assert all(entry["ok"] for entry in report.checks), report.checks
    assert [entry["id"] for entry in report.checks].count("write.materials") == 1
    assert {entry["id"] for entry in report.checks} == worker_ids(world)  # the whole plan, no more, no less
    assert report.observed["keychain"] == "untested: no decoy keychain item (sandbox.decoy_keychain)"
    assert "example.com:443" in report.observed["proxy"]["refused"]
    written = codex_probes.record_decision(RUN_ID, world.state, "code-review-pass", decision, world.planted)
    entry = written["clients"][0]
    assert (entry["step"], entry["client_requested"], entry["client_applied"], entry["fallback_reason"]) == (
        "code-review-pass", "codex", "claude", "nested_sandbox_unavailable")
    text = json.dumps(written)
    assert not any(decoy.marker in text for decoy in world.planted) and KEY not in text
    assert not list(Path(world.base / "out").glob("probe-*")) and not list(Path(world.tmp_root).rglob("probe-*"))
    assert not list(Path(world.base / "materials").iterdir())


def test_codex_is_applied_only_when_every_check_passes_and_start_is_guarded(world, monkeypatch):
    set_scenario(world.codex, nested="ok")
    started = []

    class Popen:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs))
            self.stdin = None
            self.exited = False

        def poll(self):
            return 0 if self.exited else None

    with codex_proxy.ConnectProxy(cs.allowed_hosts()) as proxy:
        decision = decide(world, proxy=proxy)
        assert decision.codex and decision.reason is None and decision.report.ok
        with codex_proxy.ConnectProxy(cs.API_HOSTS) as other:
            with pytest.raises(CodexSandboxError, match="proxy is not running"):
                codex_probes.start(decision, "review", proxy=other, platform="darwin")
        with pytest.raises(CodexSandboxError, match="older than"):
            codex_probes.start(decision, "review", proxy=proxy, platform="darwin",
                               now=lambda: decision.report.finished_at + 901)
        with monkeypatch.context() as patched:
            patched.setattr(codex_probes.subprocess, "Popen", Popen)
            process = codex_probes.start(decision, "review", proxy=proxy, platform="darwin")
        assert started[0][0][:3] == [cs.SANDBOX_EXEC, "-f", str(decision.launch.paths.profile)]
        assert started[0][1]["env"] == dict(decision.launch.env) and started[0][1]["start_new_session"]
        assert started[0][1]["env"]["CODEX_API_KEY"] == KEY
        assert not proxy.closed
        process.exited = True
        assert codex_probes._wait_for(lambda: proxy.closed)  # the proxy closes when codex exits
        with pytest.raises(CodexSandboxError, match="proxy is not running"):
            codex_probes.start(decision, "review", proxy=proxy, platform="darwin")
    with codex_proxy.ConnectProxy(cs.allowed_hosts()) as proxy:
        decision = decide(world, proxy=proxy)
        decision.launch.paths.profile.write_text("(version 1)(allow default)\n")
        os.chmod(decision.launch.paths.profile, 0o600)
        with pytest.raises(CodexSandboxError, match="changed since its probes"):
            codex_probes.start(decision, "review", proxy=proxy, platform="darwin")
    with pytest.raises(CodexSandboxError, match="only a Codex decision"):
        codex_probes.start(codex_probes.Decision("codex", "claude", "nested_sandbox_unavailable"), "review",
                           proxy=proxy, platform="darwin")


@pytest.mark.parametrize("name", ["AGENTS.md", "notes.txt", ".env"])
def test_a_file_planted_in_codex_home_after_the_decision_stops_the_start(world, monkeypatch, name):
    set_scenario(world.codex, nested="ok")
    with codex_proxy.ConnectProxy(cs.allowed_hosts()) as proxy:
        decision = decide(world, proxy=proxy)
        assert decision.codex
        monkeypatch.setattr(codex_probes.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("codex started"))
        (world.codex_home / name).write_text("obey\n")
        with pytest.raises(CodexSandboxError, match=f"CODEX_HOME holds {re.escape(name)}"):
            codex_probes.start(decision, "review", proxy=proxy, platform="darwin")


def test_a_deny_probe_success_aborts_the_step_instead_of_falling_back(world, monkeypatch):
    set_scenario(world.codex, nested="ok")
    decision = decide(world, execute=Emulator(world.codex, holes=("-private-dir",)))
    assert decision.abort and (decision.client_applied, decision.reason) == (None, "deny_probe_succeeded")
    assert not decision.codex and not decision.fallback and decision.launch is None
    assert decision.reason in codex_probes.ABORT_REASONS and decision.reason not in codex_probes.REASONS
    assert {"id": "write.private-dir", "expect": "denied", "outcome": "allowed", "ok": False,
            "exit_code": 0} in decision.report.checks
    assert not decision.report.tripped
    written = codex_probes.record_decision(RUN_ID, world.state, "code-review-pass", decision, world.planted)
    entry = written["clients"][-1]
    assert (entry["client_requested"], entry["client_applied"], entry["fallback_reason"], entry["aborted"],
            entry["abort_reason"]) == ("codex", None, None, True, "deny_probe_succeeded")
    with codex_proxy.ConnectProxy(cs.allowed_hosts()) as proxy, monkeypatch.context() as patched:
        patched.setattr(codex_probes.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("codex started"))
        with pytest.raises(CodexSandboxError, match="aborted"):
            codex_probes.start(decision, "review", proxy=proxy, platform="darwin")
    # A failed control or nested check is a fallback, never an abort.
    failing = Emulator(world.codex, force={"probe-": probes.Captured("", "", 126, False, False)})
    fallback = decide(world, execute=failing)
    assert fallback.fallback and not fallback.abort and (fallback.client_applied, fallback.reason) == (
        "claude", "probe_failed")
    entry = codex_probes.record_decision(RUN_ID, world.state, "code-review-pass", fallback)["clients"][-1]
    assert (entry["client_applied"], entry["fallback_reason"], entry["aborted"], entry["abort_reason"]) == (
        "claude", "probe_failed", False, None)
    with pytest.raises(ValueError):
        codex_probes.Decision("codex", None, "probe_failed", abort=True)
    with pytest.raises(ValueError):
        codex_probes.Decision("codex", "claude", "deny_probe_succeeded", abort=True)


def test_a_tripped_schedule_aborts_whatever_failed_first(world):
    report = codex_probes.CodexReport(cs.WORKER)
    report.fail("probe_failed", "control.read-materials: expected allowed, got failed")
    report.tripped = True
    decision = codex_probes._conclude(report, "probe_failed", ())
    assert decision.abort and decision.reason == "deny_probe_succeeded"
    report.tripped = False
    assert codex_probes._conclude(report, "probe_failed", ()).fallback


def test_a_decoy_read_trips_the_schedule(world):
    set_scenario(world.codex, nested="ok")
    credentials = next(decoy for decoy in world.planted if decoy.label == "credentials")
    decision = decide(world, execute=Emulator(world.codex, holes=(str(credentials.path),)))
    assert decision.reason == "deny_probe_succeeded" and decision.report.tripped and decision.abort
    assert "a decoy was read or leaked during a Codex probe: credentials" in decision.report.failures
    assert any(hit["name"] == "decoy credentials" for hit in decision.report.hits)
    assert (world.state / "schedules" / decoys.TRIPWIRE_NAME).exists()
    assert credentials.marker not in json.dumps(decision.to_record())


@pytest.mark.parametrize("force, reason", [
    ({"--version": probes.Captured("", "sandbox-exec: syntax error", 65, False, False)}, "profile_broken"),
    ({"probe-": probes.Captured("", "", 126, False, False)}, "probe_failed"),
    ({"/bin/ls": probes.Captured("", "", 0, False, False)}, "deny_probe_succeeded"),
])
def test_broken_profiles_and_failed_controls_fall_back(world, force, reason):
    set_scenario(world.codex, nested="ok")
    decision = decide(world, execute=Emulator(world.codex, force=force))
    aborted = reason == "deny_probe_succeeded"
    expected = (None if aborted else "claude", reason, aborted)
    assert (decision.client_applied, decision.reason, decision.abort) == expected


@pytest.mark.parametrize("kind, hole, check_id", [
    (cs.WORKER, "-materials", "write.materials"), (cs.WORKER, "-tree", "write.tree"),
    (cs.WORKER, "-decoy-dir", "write.decoy-dir"), (cs.STAGE, "/.git/hooks/probe-", "write.repo-git-hooks"),
    (cs.STAGE, ": >> ", "write.repo-git-config"), (cs.STAGE, "-venv", "write.venv"),
])
def test_each_write_probe_detects_its_own_hole(world, kind, hole, check_id):
    set_scenario(world.codex, nested="ok")
    decision = decide(world, kind=kind, execute=Emulator(world.codex, holes=(hole,)))
    assert (decision.client_applied, decision.reason, decision.abort) == (None, "deny_probe_succeeded", True)
    failed = {entry["id"] for entry in decision.report.checks if not entry["ok"]}
    # A hook planted through the hole also changes .git/hooks, which the rename probe watches.
    assert failed == {check_id} | ({"write.repo-git-rename"} if check_id == "write.repo-git-hooks" else set())
    assert not list(world.base.rglob("probe-*")) and not list((world.base / "repo" / ".git" / "hooks").iterdir())


@pytest.mark.parametrize("kind", [cs.WORKER, cs.STAGE])
def test_read_roots_are_write_probed(world, kind):
    root = world.base / "state-copy"
    root.mkdir()
    (root / "autodev.json").write_text("{}\n")
    set_scenario(world.codex, nested="ok")
    paths = world.paths(kind, read_roots=(root,))
    decision = decide(world, paths=paths)
    assert decision.codex, decision.report.failures
    expected = (worker_ids if kind == cs.WORKER else stage_ids)(world, "write.read-root-0")
    assert {entry["id"] for entry in decision.report.checks} == expected
    hole = decide(world, paths=paths, execute=Emulator(world.codex, holes=("state-copy/probe-",)))
    assert hole.reason == "deny_probe_succeeded"
    assert {"id": "write.read-root-0", "expect": "denied", "outcome": "allowed", "ok": False,
            "exit_code": 0} in hole.report.checks
    assert sorted(os.listdir(root)) == ["autodev.json"]


def test_the_keychain_decoy_item_is_probed_through_securityd(world):
    set_scenario(world.codex, nested="ok")
    item = keychain_decoy()
    persistent = [*world.planted, item]
    runner_ = with_security(keychain_with_decoy)
    decision = decide(world, persistent=persistent, command_runner=runner_)
    assert decision.codex, decision.report.failures
    assert {entry["id"] for entry in decision.report.checks} == worker_ids(world, "keychain.decoy")
    assert decision.report.observed["keychain"] == "the decoy item"
    # The probe names the owner's login keychain explicitly (the run's HOME has no search list).
    seen = Emulator(world.codex)
    decide(world, persistent=persistent, command_runner=runner_, execute=seen)
    lookup = next(call for call in seen.calls if call[0] == codex_probes.SECURITY)
    assert lookup == [codex_probes.SECURITY, "find-generic-password", "-s", "autodev-decoy-svc", "-a", "autodev-decoy-acct",
                      "-w", str(real(world.home) / "Library" / "Keychains" / "login.keychain-db")]
    # Not found proves nothing; the value in the capture is a read decoy (the schedule trips).
    absent = decide(world, persistent=persistent, command_runner=runner_, execute=Emulator(world.codex, force={
        "autodev-decoy-svc": probes.Captured("", "item not found", probes.NOT_FOUND, False, False)}))
    assert absent.reason == "probe_failed" and "keychain.decoy: expected denied, got inconclusive" in absent.report.failures
    leaked = decide(world, persistent=persistent, command_runner=runner_, execute=Emulator(world.codex, force={
        "autodev-decoy-svc": probes.Captured(item.marker + "\n", "", 0, False, False)}))
    assert leaked.reason == "deny_probe_succeeded" and leaked.report.tripped
    assert item.marker not in json.dumps(leaked.to_record())
    # No probe when the item is absent outside the sandbox (an absent item would "deny" by not being found).
    missing = decide(world, persistent=persistent)
    assert "keychain.decoy" not in {entry["id"] for entry in missing.report.checks}


def test_another_local_port_and_launchd_are_probed(world):
    set_scenario(world.codex, nested="ok")
    hole = decide(world, execute=Emulator(world.codex, holes=("-z -w 2 127.0.0.1",)))
    assert hole.reason == "deny_probe_succeeded"
    assert {"id": "network.local-listener", "expect": "denied", "outcome": "allowed", "ok": False,
            "exit_code": 0} in hole.report.checks
    assert any("the runner's listener accepted 1 connection(s)" in failure for failure in hole.report.failures)
    submitted = decide(world, execute=Emulator(world.codex, force={
        "launchctl submit": probes.Captured("", "", 0, False, False)}))
    assert submitted.reason == "deny_probe_succeeded"
    assert {"id": "launchd.submit", "expect": "denied", "outcome": "allowed", "ok": False,
            "exit_code": 0} in submitted.report.checks
    asked = []

    def launchd(argv, **kwargs):
        if argv[0] == probes.LAUNCHCTL:
            asked.append(argv[1])
            return subprocess.CompletedProcess(argv, 0, "", "")  # the job exists: a submit got through
        return keychain(argv, **kwargs)

    job = decide(world, command_runner=launchd)
    assert job.reason == "deny_probe_succeeded" and "launchd.submit: the probe's launchd job exists" in job.report.failures
    assert asked == ["list", "remove"]


@pytest.mark.parametrize("case", ["proxy-log", "proxy-port", "unresolvable"])
def test_network_probes_without_proof_fall_back(world, case):
    set_scenario(world.codex, nested="ok")
    with codex_proxy.ConnectProxy(cs.allowed_hosts()) as proxy:
        if case == "proxy-log":  # curl failed, but the proxy never saw (and refused) the CONNECT
            options = dict(execute=Emulator(world.codex, force={
                "https://example.com/": probes.Captured("", "", 7, False, False)}))
            check_id, outcome = "network.proxy-refused", "inconclusive"
        elif case == "proxy-port":  # nc says connected, but the proxy counted no connection
            options = dict(execute=Emulator(world.codex, force={
                f"-z -w 3 127.0.0.1 {proxy.port}": probes.Captured("", "", 0, False, False)}))
            check_id, outcome = "control.proxy-port", "failed"
        else:
            options = dict(resolver=lambda host: None)
            check_id, outcome = None, None
        decision = decide(world, proxy=proxy, **options)
    assert (decision.client_applied, decision.reason) == ("claude", "probe_failed")
    if check_id is None:
        assert "network.direct" not in {entry["id"] for entry in decision.report.checks}
        assert any("network.direct untested" in failure for failure in decision.report.failures)
    else:
        assert {entry["id"]: entry["outcome"] for entry in decision.report.checks}[check_id] == outcome


def test_a_proxy_with_other_hosts_is_refused(world):
    with codex_proxy.ConnectProxy(("api.openai.com", "example.org")) as proxy:
        decision = decide(world, proxy=proxy)
    assert (decision.client_applied, decision.reason) == ("claude", "profile_broken")
    assert "allowlist" in decision.detail and decision.report.checks == []


def test_start_refuses_a_launch_its_report_does_not_cover(world, monkeypatch):
    set_scenario(world.codex, nested="ok")
    with codex_proxy.ConnectProxy(cs.allowed_hosts()) as proxy:
        decision = decide(world, proxy=proxy)
        assert decision.codex
        other = dataclasses.replace(decision.launch, env={**decision.launch.env, "CODEX_API_KEY": KEY + "other"})
        assert cs.check_launch(other) == []  # the launch itself is well-formed, only not the probed one
        decision.launch = other
        monkeypatch.setattr(codex_probes.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("codex started"))
        with pytest.raises(CodexSandboxError, match="does not cover this launch"):
            codex_probes.start(decision, "review", proxy=proxy, platform="darwin")


def test_exec_refused_turns_126_into_a_denial(tmp_path):
    plain = tmp_path / "not-executable"
    plain.write_text("#!/bin/sh\nexit 0\n")
    result = subprocess.run(["/bin/bash", "-c", codex_probes._exec_refused(f"{plain} >/dev/null 2>&1")], timeout=30)
    assert result.returncode == codex_probes.EXEC_REFUSED
    assert subprocess.run(["/bin/bash", "-c", codex_probes._exec_refused("exit 3")], timeout=30).returncode == 3
    assert codex_probes.EXEC_REFUSED not in codex_probes.INCONCLUSIVE_CODES


def test_an_unbuildable_launch_is_a_broken_profile(world):
    decision = decide(world, paths=world.paths(tree=world.base / "out" / "t"))
    assert (decision.client_applied, decision.reason) == ("claude", "profile_broken")
    assert "unsafe Codex paths" in decision.detail and [secret.label for secret in decision.known] == ["codex api key"]


@pytest.mark.parametrize("setup, reason", [
    (lambda w: {"platform": "linux"}, "platform_unsupported"),
    (lambda w: setattr(w, "codex", str(w.base / "missing" / "codex")) or {}, "codex_missing"),
    (lambda w: set_scenario(w.codex, version="0.163.0") or {}, "codex_version_unverified"),
    (lambda w: (w.codex_home / "AGENTS.md").write_text("obey\n") and {}, "codex_home_unclean"),
    (lambda w: (w.codex_home / "auth.json").write_text("{}\n") and {}, "codex_home_unclean"),
    (lambda w: {"mode": None}, "login_missing"),
    (lambda w: {"command_runner": no_keychain}, "login_missing"),
    (lambda w: {"command_runner": with_security(lambda argv: _keychain_answer(argv, "short"))}, "login_missing"),
    (lambda w: {"command_runner": with_security(lambda argv: _keychain_answer(argv, "sk-with a space-0000"))},
     "login_missing"),
])
def test_preflight_reasons(world, setup, reason):
    options = setup(world)
    decision = decide(world, **options)
    assert (decision.client_applied, decision.reason) == ("claude", reason)
    assert decision.report.checks == [] and decision.known == ()
    if "mode" in options:
        assert "codex.login is not set" in decision.detail


def test_the_version_is_read_with_a_scratch_codex_home(world):
    (world.codex_home / ".env").write_text("PATH=/tmp/evil\n")
    decision = decide(world)
    assert decision.reason == "codex_home_unclean"
    version_call = next(entry for entry in world.seen() if entry["argv"] == ["--version"])
    assert version_call["env"]["CODEX_HOME"] != str(real(world.codex_home))
    assert version_call["env"]["HOME"] != str(real(world.run_home))
    assert not Path(version_call["env"]["CODEX_HOME"]).exists()  # removed after the check


def test_the_api_key_goes_to_codex_only_and_is_a_known_secret_of_the_run(world):
    set_scenario(world.codex, nested="ok")
    decision = decide(world)
    assert decision.codex and decision.launch.env["CODEX_API_KEY"] == KEY
    assert "shell_environment_policy.inherit=core" in decision.launch.command
    assert [secret.label for secret in decision.known] == [codex_probes.API_KEY_LABEL]
    assert decision.known_values() == [KEY] and KEY not in repr(decision)
    assert KEY not in json.dumps(decision.to_record())
    capture = f"model command output: CODEX_API_KEY={KEY}\n"
    hits = Scanner(decision.known).scan(capture)
    assert any(hit.kind == HitKind.KNOWN and hit.name == codex_probes.API_KEY_LABEL for hit in hits)
    # The run's outgoing-text scan: the broker refuses the key in any text and masks it in its log.
    gate = broker.Broker(broker.Run(RUN_ID, "IEZhu/Agents", 1, "a" * 40, "dry-run"), credentials=None, git=None,
                         known=decision.known)
    with pytest.raises(broker.BrokerRefused, match="codex api key"):
        gate._scan("comment", capture)
    assert KEY not in json.dumps(gate.log.entries)
    # A probe capture holding the key is flagged like a decoy: the start control fails on it.
    leaky = decide(world, execute=Emulator(world.codex, force={"--version": probes.Captured(
        "codex-cli 0.162.1 " + KEY, "", 0, False, False)}))
    assert leaky.reason == "profile_broken" and leaky.known_values() == [KEY]
    assert {"probe": "control.codex-start", "kind": "known", "name": codex_probes.API_KEY_LABEL} in leaky.report.hits
    written = codex_probes.record_decision(RUN_ID, world.state, "code-review-pass", leaky, world.planted)
    assert KEY not in json.dumps(written)


# --- the installed version ------------------------------------------------------------------------------

def test_installed_checks_with_the_fake(world):
    found = codex_probes.installed_checks(world.codex, path=PATH)
    assert found["version"] == "0.162.1" and found["read_only_restricts_reads"] is False
    assert found["workspace_write_blocks_network"] is True and "ultra" in found["efforts"]
    set_scenario(world.codex, ws_network="open", ro_reads="restricted")
    found = codex_probes.installed_checks(world.codex, path=PATH)
    assert found["workspace_write_blocks_network"] is False and found["read_only_restricts_reads"] is True
    set_scenario(world.codex, sandbox_broken=True)
    found = codex_probes.installed_checks(world.codex, path=PATH)
    assert found["sandbox_runs"] is False
    assert found["workspace_write_blocks_network"] is None and found["read_only_restricts_reads"] is None
    assert all(entry["env"]["CODEX_HOME"] != str(world.codex_home) for entry in world.seen())


def test_the_fake_refuses_flags_outside_the_verified_set(world):
    env = {"PATH": PATH, "CODEX_HOME": str(world.codex_home), "HOME": str(world.run_home), "CODEX_API_KEY": KEY}
    for argv in (["exec", "--bogus", "-"], ["exec", "--disable", "hook", "-"], ["exec", "-c", "sandbox_mode=x", "-"],
                 ["exec", "-s", "danger-full-access", "-"], ["sandbox", "--full-auto", "--", "/usr/bin/true"],
                 ["--version", "--verbose"]):
        result = subprocess.run([world.codex, *argv], env=env, capture_output=True, text=True, input="", timeout=30)
        assert result.returncode == 2 and "unexpected argument" in result.stderr, argv
    launch = build(world, world.paths(cs.STAGE))
    result = subprocess.run(launch.command, env={**env, "CODEX_API_KEY": ""}, capture_output=True, text=True,
                            input="", timeout=30, cwd=launch.cwd)
    assert result.returncode == 1 and "Not logged in" in result.stderr  # every flag of the launch is verified


# --- Codex as a stage client (Q14) -----------------------------------------------------------------

def stage(world, *, keychain_item=True, paths=None, path=PATH, **scenario):
    set_scenario(world.codex, **scenario)
    items = (("autodev-test-item",) if keychain_item else ())
    runner_targets = probes.Targets(installation=world.base / "inst", runs=world.state / "schedules" / "runs",
                                    schedules=world.state / "schedules", config_file=world.state / "x.json",
                                    keychain_items=items, internet_items=())

    def security(argv):
        if SERVICE in argv and "-w" in argv:
            return subprocess.CompletedProcess(argv, 0, KEY + "\n", "")
        return subprocess.CompletedProcess(argv, 0 if keychain_item and "autodev-test-item" in argv else 44, "", "")

    with codex_proxy.ConnectProxy(cs.allowed_hosts()) as proxy:
        return codex_probes.stage_availability(
            world.codex, paths or world.paths(cs.STAGE), path=path, proxy=proxy, mode=MODE,
            targets=world.targets(runner_targets=runner_targets), persistent=world.planted, directory=world.state,
            run_id=RUN_ID, platform="darwin", execute_=Emulator(world.codex), command_runner=with_security(security),
            resolver=lambda host: TEST_NET, timeout=60)


def stored_entry(world) -> dict:
    return json.loads(codex_probes.availability_path(world.state).read_text())["stage"]


def test_stage_unavailable_while_the_nested_sandbox_fails(world):
    decision = stage(world)
    assert (decision.client_applied, decision.reason) == ("claude", "nested_sandbox_unavailable")
    stored = codex_probes.read_availability(world.state, version="0.162.1", shape="x")
    assert stored["available"] is False and stored["reason"] == "nested_sandbox_unavailable"
    assert not decision.report.exec_session  # no model turn
    # The installed checks are stored with the fallback, not added after the write.
    assert stored_entry(world)["installed"]["workspace_write_blocks_network"] is True
    assert stored_entry(world)["version"] == "0.162.1"


def test_stage_unavailable_when_workspace_write_reaches_the_network(world):
    decision = stage(world, nested="ok", ws_network="open")
    assert decision.reason == "workspace_write_network_open" and not decision.report.exec_session
    assert stored_entry(world)["installed"]["workspace_write_blocks_network"] is False


def test_a_stage_environment_that_cannot_run_keeps_codex_unavailable(world):
    broken = fake_venv(world.base / "nix-like-venv", exit_code=255)
    decision = stage(world, nested="ok", exec="sandboxed", paths=world.paths(cs.STAGE, venv=broken))
    assert (decision.client_applied, decision.reason) == ("claude", "venv_unusable")
    assert {"id": "control.venv-python", "expect": "allowed", "outcome": "failed", "ok": False,
            "exit_code": 255} in decision.report.checks
    assert stored_entry(world)["available"] is False and stored_entry(world)["reason"] == "venv_unusable"
    assert not decision.report.exec_session


def test_the_rename_and_edit_sequence_on_git_is_probed(world):
    git = world.base / "repo" / ".git"
    config = (git / "config").read_bytes()
    set_scenario(world.codex, nested="ok")
    decision = decide(world, kind=cs.STAGE)
    assert decision.codex, decision.report.failures
    check = next(entry for entry in decision.report.checks if entry["id"] == "write.repo-git-rename")
    assert check["ok"] and check["outcome"] == "denied"
    assert (git / "config").read_bytes() == config and sorted(os.listdir(git / "hooks")) == []
    assert {entry["id"] for entry in decision.report.checks} == stage_ids(world)
    hole = decide(world, kind=cs.STAGE, execute=Emulator(world.codex, holes=("mv ",)))
    assert hole.reason == "deny_probe_succeeded"
    assert {"id": "write.repo-git-rename", "expect": "denied", "outcome": "allowed", "ok": False,
            "exit_code": 0} in hole.report.checks
    assert git.is_dir() and not list(world.base.glob("repo/.git-probe-*"))
    assert not list((git / "hooks").iterdir())


@pytest.mark.parametrize("scenario, reason", [
    ({"exec": "eperm"}, "stage_probe_failed"),
    ({"exec": "forge"}, "stage_probe_failed"),
    ({"exec": "sandboxed", "patch": True}, "stage_probe_failed"),
    ({"exec": "sandboxed", "skip": [1]}, "stage_probe_failed"),
    ({"exec": "sandboxed", "turn_failed": "stream disconnected"}, "stage_probe_failed"),
    ({"exec": "hole"}, "deny_probe_succeeded"),
])
def test_a_failing_stage_probe_keeps_codex_unavailable(world, scenario, reason):
    decision = stage(world, nested="ok", **scenario)
    aborted = reason == "deny_probe_succeeded"
    expected = (None if aborted else "claude", reason, aborted)
    assert (decision.client_applied, decision.reason, decision.abort) == expected
    assert stored_entry(world)["reason"] == reason
    assert codex_probes.read_availability(world.state, version="0.162.1", shape="x")["available"] is False
    assert decision.report.exec_session["commands"] >= 1 or scenario.get("skip")
    if scenario["exec"] == "hole":
        assert decision.report.tripped
    if "turn_failed" in scenario:
        assert "the probe session failed: stream disconnected" in decision.report.failures
        assert decision.report.exec_session["failure"] == "stream disconnected"
    assert not list((world.base / "exchange").glob("probe-*")) and not list((world.base / "repo").glob(".autodev-*"))


@pytest.mark.parametrize("wrap", [False, True])
def test_stage_available_when_every_probe_is_denied(world, wrap):
    decision = stage(world, nested="ok", exec="sandboxed", wrap=wrap, error_item="model rerouted: gpt-x to gpt-y")
    assert decision.codex, decision.report.failures
    assert decision.report.exec_session["warnings"] == ["model rerouted: gpt-x to gpt-y"]
    current = dict(version="0.162.1", shape=decision.launch.shape())
    stored = codex_probes.read_availability(world.state, **current)
    assert stored["available"] is True and stored["version"] == "0.162.1"
    assert stored["installed"]["workspace_write_blocks_network"] is True
    ids = [entry["id"] for entry in decision.report.checks]
    assert set(ids) == stage_ids(world) | exec_ids(world) and len(ids) == len(set(ids))
    assert decision.report.observed["exit_codes"] == {"git.config-global.script": 1}
    seen_names = decision.report.observed["model_command_env"]
    assert "PATH" in seen_names and not set(seen_names) & {"CODEX_API_KEY", "HTTPS_PROXY", "HTTP_PROXY", "CODEX_HOME"}
    executed = [entry for entry in world.seen() if entry["argv"][:1] == ["exec"]]
    assert executed and executed[-1]["argv"] == decision.launch.command[1:]
    given = executed[-1]["env"]  # plus what the wrapper's shell and the platform add (PWD, SHLVL, ...)
    assert {name: given.get(name) for name in decision.launch.env} == dict(decision.launch.env)
    assert not any(cs.FORBIDDEN_NAME.search(name) for name in given if name != cs.API_KEY_VARIABLE)
    assert KEY not in json.dumps(decision.to_record())


@pytest.mark.parametrize("scenario, reason, failure", [
    ({"env_leak": True}, "deny_probe_succeeded", "observe.env.script: expected observe, got exposed"),
    ({"tamper": True}, "stage_probe_failed", "the probe script probe-"),
    ({"listener_leak": True}, "deny_probe_succeeded",
     "network.local-listener.script: the decoy listener accepted 1 connection(s)"),
])
def test_the_stage_session_detects_what_its_exit_codes_hide(world, scenario, reason, failure):
    decision = stage(world, nested="ok", exec="sandboxed", **scenario)
    aborted = reason == "deny_probe_succeeded"
    expected = (None if aborted else "claude", reason, aborted)
    assert (decision.client_applied, decision.reason, decision.abort) == expected
    assert any(item.startswith(failure) for item in decision.report.failures), decision.report.failures
    if "env_leak" in scenario:  # names only: the key's value never reaches the report
        assert {"CODEX_API_KEY", "HTTPS_PROXY", "HTTP_PROXY"} <= set(decision.report.observed["model_command_env_exposed"])
        assert KEY not in json.dumps(decision.to_record())
    if "listener_leak" in scenario:  # the probe itself reported a denial
        assert {"id": "network.local-listener.script", "group": "C6", "expect": "denied", "outcome": "denied",
                "ok": True, "exit_codes": [1]} in decision.report.checks
    assert stored_entry(world)["available"] is False


def test_gh_and_programs_the_profile_cannot_execute(world):
    bin_dir = world.base / "ghbin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "gh").chmod(0o755)
    decision = stage(world, nested="ok", exec="sandboxed", path=f"{bin_dir}:{PATH}")
    assert decision.codex, decision.report.failures
    assert decision.report.observed["gh"] == "probed"
    checks = {entry["id"]: entry for entry in decision.report.checks}
    assert set(checks) == stage_ids(world) | exec_ids(world, "gh.token.script")
    assert checks["gh.token.script"]["group"] == "C4" and checks["gh.token.script"]["ok"]
    # The plan runs the gh the runner found, and osascript, so that a refused exec counts as denied.
    launch = build(world, world.paths(cs.STAGE, profile=world.base / "priv" / "plan.sb"), path=f"{bin_dir}:{PATH}")
    assert str(bin_dir) not in launch.env["PATH"].split(":")  # the profile cannot execute from it
    run = codex_probes.ProbeRun("0" * 16, list(world.planted), gh=codex_probes.find_gh(f"{bin_dir}:{PATH}"))
    text = "\n".join(call.text for call in codex_probes.exec_plan(launch, run, world.targets()))
    assert codex_probes._exec_refused(f"{bin_dir / 'gh'} auth token >/dev/null 2>&1") in text
    assert f"exit {codex_probes.EXEC_REFUSED}; exit $rc ); echo __probe__ apple-events.osascript.script" in text
    assert codex_probes.find_gh(PATH) is None or os.path.dirname(codex_probes.find_gh(PATH)) in PATH.split(":")


def test_the_stored_availability_is_bound_to_version_shape_and_age(world):
    decision = stage(world, nested="ok", exec="sandboxed")
    assert decision.codex, decision.report.failures
    shape = decision.launch.shape()
    assert codex_probes.read_availability(world.state, version="0.162.1", shape=shape)["available"] is True
    for options, reason in (({"version": "0.163.0", "shape": shape}, "version_changed"),
                            ({"version": None, "shape": shape}, "version_changed"),
                            ({"version": "0.162.1", "shape": "0" * 64}, "shape_changed"),
                            ({"version": "0.162.1", "shape": shape,
                              "now": lambda: time.time() + codex_probes.AVAILABILITY_MAX_AGE + 60}, "stale"),
                            ({"version": "0.162.1", "shape": shape, "now": lambda: time.time() - 3600}, "stale")):
        found = codex_probes.read_availability(world.state, **options)
        assert (found["available"], found["reason"]) == (False, reason)
    other = build(world, world.paths(cs.STAGE, profile=world.base / "priv" / "other.sb"), effort="low",
                  proxy_port=decision.launch.proxy_port)
    assert other.shape() != shape  # another effort is another launch shape
    same = build(world, world.paths(cs.STAGE, profile=world.base / "priv" / "same.sb"), proxy_port=41000)
    assert same.shape() == shape and same.digest() != decision.launch.digest()  # paths and the port are per run


def test_a_git_first_on_the_runners_path_outside_the_profile_does_not_break_the_stage(world):
    tools = tool_dir(world)  # its git and gh exit 126, as a nix git does under the stage profile
    decision = stage(world, nested="ok", exec="sandboxed", path=f"{tools}:{PATH}")
    assert decision.codex, decision.report.failures
    checks = {entry["id"]: entry for entry in decision.report.checks}
    assert checks["control.git"]["ok"] and checks["control.git.script"]["ok"]
    assert decision.launch.env["PATH"] == "/usr/bin:/bin"
    assert decision.report.observed["gh"] == "probed"  # the runner's gh, run by absolute path
    run = codex_probes.ProbeRun("0" * 16, list(world.planted))
    plan = codex_probes.exec_plan(decision.launch, run, world.targets())
    text = "\n".join(call.text for call in plan)
    assert "/usr/bin/git config --global --list" in text and "git --version" in text


@pytest.mark.parametrize("where", ["model-free", "exec"])
def test_an_unusable_git_has_its_own_reason(world, where):
    if where == "model-free":
        set_scenario(world.codex, nested="ok")
        decision = decide(world, kind=cs.STAGE, execute=Emulator(world.codex, force={
            "git --version": probes.Captured("", "", 126, False, False)}))
        failed = "control.git: expected allowed, got failed"
    else:
        decision = stage(world, nested="ok", exec="sandboxed", git_broken=True)
        failed = "control.git.script: expected allowed, got failed"
        assert stored_entry(world)["reason"] == "git_unusable"
    assert (decision.client_applied, decision.reason) == ("claude", "git_unusable")
    assert failed in decision.report.failures


def test_the_stored_availability_reads_from_the_configuration_alone(world):
    decision = stage(world, nested="ok", exec="sandboxed", paths=world.stage_paths())
    assert decision.codex, decision.report.failures
    config = {**autodev_config.DEFAULTS,
              "stages": {**autodev_config.DEFAULTS["stages"], "implement": {"client": "codex", "codex": {}}},
              "delegates": {**autodev_config.DEFAULTS["delegates"], "code-review-pass": {"client": "claude"}}}
    assert codex_probes.codex_settings(config, cs.STAGE) == {"model": None, "effort": None}  # what `stage` used
    # Per stage kind (#347): a stage's own Codex settings, else the worker's.
    selecting = {**config, "stages": {**config["stages"], "select": {"client": "codex",
                                                                     "codex": {"model": "gpt-x", "effort": "low"}}}}
    assert codex_probes.codex_settings(selecting, cs.STAGE, "select") == {"model": "gpt-x", "effort": "low"}
    assert codex_probes.codex_settings(selecting, cs.STAGE) == {"model": None, "effort": None}
    assert codex_probes.stage_shape(world.codex, selecting, "select") != codex_probes.stage_shape(world.codex, config)
    shape = codex_probes.stage_shape(world.codex, config)
    assert shape == decision.launch.shape() == stored_entry(world)["shape"]
    version = codex_probes.installed_version(world.codex, path=PATH)
    assert version == "0.162.1"
    assert codex_probes.read_availability(world.state, version=version, shape=shape)["available"] is True
    changed = {**config, "stages": {**config["stages"], "implement": {"client": "codex", "codex": {"effort": "low"}}}}
    other = codex_probes.stage_shape(world.codex, changed)
    found = codex_probes.read_availability(world.state, version=version, shape=other)
    assert (found["available"], found["reason"]) == (False, "shape_changed")
    # The helper needs neither a key nor a run: nothing was written and no codex was started.
    assert not (cs.SHAPE_ROOT).exists()
    seen = len(world.seen())
    codex_probes.stage_shape(world.codex, changed)
    assert len(world.seen()) == seen
    # A fallback is stored with the configuration's shape too.
    fallback = stage(world, nested="eperm")  # the nested check fails
    assert fallback.reason == "nested_sandbox_unavailable" and stored_entry(world)["shape"] == shape


@pytest.mark.parametrize("content", [{"stage": ["x"]}, {"stage": "available"}, ["stage"], {"stage": {"available": 1}}])
def test_a_damaged_stored_availability_reads_as_not_checked(world, content):
    path = codex_probes.availability_path(world.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content))
    found = codex_probes.read_availability(world.state, version="0.162.1", shape="s")
    assert found == {"available": False, "reason": "not_checked", "checked": None}


def test_stage_without_any_keychain_probe_is_untested(world):
    decision = stage(world, keychain_item=False, nested="ok", exec="sandboxed")
    assert decision.reason == "stage_probe_failed" and any("C3 untested" in f for f in decision.report.failures)


def test_exec_commands_are_matched_exactly():
    assert codex_probes.normalized_command("bash -lc '/bin/bash /x/probe.sh'") == ["/bin/bash", "/x/probe.sh"]
    assert codex_probes.normalized_command("/bin/zsh -c \"/bin/bash '/x/a b.sh'\"") == ["/bin/bash", "/x/a b.sh"]
    assert codex_probes.normalized_command("echo '") is None
    stream = codex_probes.parse_exec_stream("\n".join([
        json.dumps({"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": "ls",
                                                       "aggregated_output": "x", "exit_code": 0, "status": "completed"}}),
        json.dumps({"type": "item.completed", "item": {"id": "2", "type": "web_search", "query": "q"}}),
        json.dumps({"type": "item.started", "item": {"id": "3", "type": "command_execution", "command": "pwd"}}),
        json.dumps({"type": "item.completed", "item": {"id": "4", "type": "error", "message": "config warning\x1b[0m"
                                                       + "!" * 300}}),
        "not json", json.dumps({"type": "turn.failed", "error": {"message": "m"}}),
        json.dumps({"type": "error", "message": "second"})]))
    assert [c["command"] for c in stream.commands] == ["ls"] and stream.unexpected == ["web_search"]
    assert stream.unparsed == 1 and stream.failed and stream.failure == "m"
    assert stream.warnings == [("config warning?[0m" + "!" * 300)[:codex_probes.WARNING_LIMIT]]
    many = "\n".join(json.dumps({"type": "item.completed", "item": {"id": str(i), "type": "error", "message": "w"}})
                     for i in range(codex_probes.MAX_WARNINGS + 3))
    found = codex_probes.parse_exec_stream(many)
    assert len(found.warnings) == codex_probes.MAX_WARNINGS and found.warnings_dropped == 3 and not found.failed


def test_a_warning_holding_a_secret_is_withheld(world):
    decision = stage(world, nested="ok", exec="sandboxed", error_item="token ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
    assert decision.reason == "stage_probe_failed"  # the whole stream is scanned: a hit fails the session
    assert decision.report.exec_session["warnings"] == ["<withheld: scan hit>"]


# --- the CLI --------------------------------------------------------------------------------------------

def _ctx(world, **changes):
    values = dict(installation=world.base / "inst", directory=world.state, home=world.home, platform="darwin",
                  codex=world.codex, codex_execute=Emulator(world.codex), codex_resolver=lambda host: TEST_NET,
                  command_runner=keychain)
    values.update(changes)
    return runner.Context(**values)


def test_cli_refuses_without_the_confirmation(world, capsys):
    assert cli.main(["codex-probe"], _ctx(world)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "confirmation_required" and "--confirm-live-probe" in result["message"]
    runs = records.runs_dir(world.state)
    assert not runs.exists() or not any(runs.iterdir())


def test_cli_without_a_codex_login_records_login_missing(world, capsys):
    code = cli.main(["codex-probe", "--kind", "worker", "--confirm-live-probe"], _ctx(world))
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["fallback_reason"] == "login_missing"
    assert "codex.login is not set" in result["detail"]


def test_cli_worker_check_records_the_fallback(world, capsys, monkeypatch):
    autodev_config.save({**autodev_config.DEFAULTS, "codex": {"login": MODE}}, world.state)
    temp_base = real(world.base / "tmpbase")
    temp_base.mkdir()
    code = cli.main(["codex-probe", "--kind", "worker", "--confirm-live-probe"], _ctx(world, temp_base=temp_base))
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["status"] == "refused"
    assert (result["client_requested"], result["client_applied"], result["fallback_reason"]) == (
        "codex", "claude", "nested_sandbox_unavailable")
    assert result["codex_probes"]["installed"]["workspace_write_blocks_network"] is True
    stored = records.read_record(result["run_id"], world.state)
    assert stored["fallback_reason"] == "nested_sandbox_unavailable" and stored["trigger"] == {"kind": "codex-probe"}
    assert not (records.run_dir(result["run_id"], world.state) / "codex-work").exists()
    assert KEY not in json.dumps(stored)
    # Per-run HOME, CODEX_HOME and TMPDIR: removed with the temporary root; no persistent codex-home.
    assert list(temp_base.iterdir()) == [] and not (world.state / "schedules" / cs.CODEX_HOME_NAME).exists()
    used = {entry["env"]["CODEX_HOME"] for entry in world.seen() if entry["argv"][:1] == ["sandbox"]}
    assert any(path.startswith(str(temp_base)) for path in used)


def test_cli_records_an_aborted_step(world, capsys):
    autodev_config.save({**autodev_config.DEFAULTS, "codex": {"login": MODE}}, world.state)
    set_scenario(world.codex, nested="ok")
    temp_base = real(world.base / "tmpbase")
    temp_base.mkdir()
    ctx = _ctx(world, temp_base=temp_base, codex_execute=Emulator(world.codex, holes=("-private-dir",)))
    code = cli.main(["codex-probe", "--kind", "worker", "--confirm-live-probe"], ctx)
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["status"] == "refused" and result["aborted"] is True
    assert (result["client_applied"], result["fallback_reason"], result["abort_reason"]) == (
        None, None, "deny_probe_succeeded")
    assert "neither Codex nor the Claude worker" in result["reason"]
    assert list(temp_base.iterdir()) == []


def test_the_temporary_root_is_removed_whatever_a_session_did_to_it(tmp_path):
    base = real(tmp_path / "base")
    base.mkdir()
    outside = base / "outside"
    outside.mkdir(mode=0o755)
    root = sandbox.make_temp_root(base)
    directories = cs.prepare_run_directories(root)
    log = directories["codex_home"] / "log"
    log.mkdir()
    (log / "f").write_text("x")
    (log / "inner").mkdir()
    os.chmod(log / "inner", 0)
    os.chmod(log, 0)  # what a model command may do inside the writable temporary root
    (directories["run_home"] / "link").symlink_to(outside, target_is_directory=True)
    if hasattr(os, "chflags"):
        locked = directories["run_home"] / "locked"
        locked.write_text("x")
        os.chflags(locked, stat.UF_IMMUTABLE)
    sandbox.remove_temp_root(root, base)
    assert not root.exists() and sorted(os.listdir(base)) == ["outside"]
    assert stat.S_IMODE(os.lstat(outside).st_mode) == 0o755  # a link is never followed


# --- the real Seatbelt (macOS) --------------------------------------------------------------------------

@needs_seatbelt
def test_the_generated_profile_under_the_real_sandbox(world):
    hostile = world.base / 'mat")(allow default)(x'
    hostile.mkdir()
    (hostile / "m.txt").write_text("material\n")
    (world.home / "secret.txt").write_text("secret\n")
    allowed, other = socket.socket(), socket.socket()
    for sock in (allowed, other):
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
    try:
        launch = build(world, world.paths(materials=hostile), proxy_port=allowed.getsockname()[1])

        def rc(*command):
            return subprocess.run(launch.wrap(command), env=dict(launch.env), cwd=launch.cwd, capture_output=True,
                                  timeout=30).returncode

        assert rc("/bin/cat", str(hostile / "m.txt")) == 0 and rc("/bin/ls", str(world.base / "tree")) == 0
        assert rc("/bin/cat", str(world.home / "secret.txt")) != 0 and rc("/bin/ls", str(world.home / ".ssh")) != 0
        assert rc("/bin/ls", str(world.state)) != 0
        assert rc("/bin/sh", "-c", f"printf x > '{world.base}/out/w'") == 0
        assert rc("/bin/sh", "-c", 'printf x > "$TMPDIR/w" && printf x > "$HOME/w" && printf x > "$CODEX_HOME/w"') == 0
        assert cs.hygiene_problems(world.codex_home) == ["CODEX_HOME holds w, which codex does not create"]
        (world.codex_home / "w").unlink()
        for target in (world.base / "tree" / "w", hostile / "w", world.home / "w", world.base / "priv" / "w"):
            assert rc("/bin/sh", "-c", f"printf x > {shlex.quote(str(target))}") != 0 and not target.exists()
        assert rc("/usr/bin/nc", "-z", "-w", "2", "127.0.0.1", str(allowed.getsockname()[1])) == 0
        assert rc("/usr/bin/nc", "-z", "-w", "2", "127.0.0.1", str(other.getsockname()[1])) != 0
        assert rc("/usr/bin/nc", "-z", "-G", "2", "-w", "2", TEST_NET, "443") != 0
        assert rc("/usr/bin/perl", "-e", "1") != 0 and rc("/bin/echo", "ok") == 0
        # A program the profile does not let start: bash's 126, which the probes count as denied.
        assert rc("/bin/bash", "-c", codex_probes._exec_refused("/usr/bin/osascript -e 1 >/dev/null 2>&1")) == 77
        stage_launch = build(world, world.paths(cs.STAGE), proxy_port=allowed.getsockname()[1])
        repo, git = world.base / "repo", world.base / "repo" / ".git"
        config = (git / "config").read_bytes()

        def stage_rc(*command):
            return subprocess.run(stage_launch.wrap(command), env=dict(stage_launch.env), cwd=stage_launch.cwd,
                                  capture_output=True, timeout=30).returncode

        assert stage_rc("/bin/sh", "-c", f"printf x > '{repo}/w'") == 0
        assert stage_rc("/bin/sh", "-c", f"printf x > '{world.base}/exchange/w'") == 0
        assert stage_rc(str(world.venv / "bin" / "python"), "-c", "0") == 0  # the stage's interpreter runs
        assert stage_rc("/bin/sh", "-c", f": >> '{git}/config'") != 0
        assert stage_rc("/bin/sh", "-c", f"printf x > '{git}/hooks/pre-commit'") != 0
        assert stage_rc("/bin/mv", str(git), str(repo / ".git2")) != 0
        assert stage_rc("/bin/mv", str(git / "hooks"), str(git / "h2")) != 0
        for name in ("commondir", "info/alternates", "objects/info/alternates"):
            assert stage_rc("/bin/sh", "-c", f"printf /x > '{git}/{name}'") != 0 and not (git / name).exists()
        assert stage_rc("/bin/sh", "-c", f"mkdir -p '{git}/objects/ab' && printf x > '{git}/objects/ab/probe'") == 0
        assert (git / "objects" / "ab" / "probe").read_text() == "x"
        sequence = (f"mv '{git}' '{repo}/.g3' && printf '[core]\\n\\thooksPath = /x\\n' >> '{repo}/.g3/config' "
                    f"&& printf x > '{repo}/.g3/hooks/post-checkout'; mv '{repo}/.g3' '{git}'")
        assert stage_rc("/bin/sh", "-c", sequence) != 0
        assert git.is_dir() and (git / "config").read_bytes() == config and not (git / "hooks" / "pre-commit").exists()
        assert sorted(os.listdir(git / "hooks")) == [] and not (repo / ".g3").exists()
    finally:
        allowed.close()
        other.close()


@needs_seatbelt
def test_git_by_name_runs_under_the_stage_profile_whatever_comes_first_on_the_runners_path(world):
    tools = tool_dir(world, exit_code=0)  # outside the profile's executable directories
    launch = build(world, world.paths(cs.STAGE), path=f"{tools}:{PATH}")

    def rc(command: str, env: dict) -> int:
        return subprocess.run(launch.wrap(["/bin/sh", "-c", command]), env=env, cwd=launch.cwd, capture_output=True,
                              timeout=30).returncode

    assert rc("git --version", dict(launch.env)) == 0
    assert rc("/usr/bin/git config --global --list", dict(launch.env)) not in codex_probes.INCONCLUSIVE_CODES
    # Without the filter the first git on the PATH could not even start (the round-2 review: 126).
    assert rc("git --version", {**launch.env, "PATH": f"{tools}:{PATH}"}) == 126


@needs_seatbelt
def test_the_worker_probes_under_the_real_sandbox(world):
    root = world.base / "state-copy"
    root.mkdir()
    paths = world.paths(read_roots=(root,))
    decision = decide(world, paths=paths, execute=real_executor(world.codex))
    assert decision.reason == "nested_sandbox_unavailable"
    failed = [entry for entry in decision.report.checks if not entry["ok"]]
    assert failed == [] and len(decision.report.failures) == 1
    assert {entry["id"] for entry in decision.report.checks} == worker_ids(world, "write.read-root-0")
    set_scenario(world.codex, nested="ok")
    decision = decide(world, paths=paths, execute=real_executor(world.codex))
    assert decision.codex, decision.report.failures
    assert os.listdir(root) == []


@needs_seatbelt
def test_the_stage_probes_under_the_real_sandbox(world):
    root = world.base / "state-copy"
    root.mkdir()
    set_scenario(world.codex, nested="ok")
    decision = decide(world, paths=world.paths(cs.STAGE, read_roots=(root,)), execute=real_executor(world.codex))
    assert decision.codex, decision.report.failures
    assert {entry["id"] for entry in decision.report.checks if entry["ok"]} == stage_ids(world, "write.read-root-0")
    assert os.listdir(root) == [] and not list(world.venv.glob("probe-*"))


def sh_codex(directory: Path) -> str:
    """A ``codex`` package whose executable is a shell script, so that it runs under the real
    profile (the Python fake cannot): ``--version``, and ``sandbox`` failing as on this macOS."""
    package = directory / "shpkg"
    (package / "bin").mkdir(parents=True)
    (package / cs.PACKAGE_MANIFEST).write_text('{"entrypoint": "bin/codex"}\n')
    executable = package / "bin" / "codex"
    executable.write_text('#!/bin/sh\ncase "$1" in\n  --version) echo "codex-cli 0.162.1" ;;\n'
                          '  sandbox) echo "sandbox-exec: sandbox_apply: Operation not permitted" >&2; exit 71 ;;\n'
                          '  *) exit 2 ;;\nesac\n')
    executable.chmod(0o755)
    return str(executable)


@needs_seatbelt
def test_a_broken_profile_under_the_real_sandbox_falls_back_and_is_recorded(world):
    world.codex = sh_codex(world.base)
    decision = decide(world, execute=codex_probes.execute)  # every probe, codex included, under the profile
    assert decision.reason == "nested_sandbox_unavailable", decision.report.failures
    assert all(entry["ok"] for entry in decision.report.checks)  # control.codex-start ran under the profile
    assert decision.report.nested["exit_code"] == 71
    broken = []

    def breaking(argv, **kwargs):
        if not broken:  # after the launch was built and checked: the profile on disk no longer parses
            profile = Path(argv[2])
            profile.write_text("(version 1)\n(deny default\n")
            broken.append(profile)
        return codex_probes.execute(argv, **kwargs)

    decision = decide(world, execute=breaking)
    assert (decision.client_applied, decision.reason) == ("claude", "profile_broken")
    assert {"id": "control.codex-start", "expect": "allowed", "outcome": "failed", "ok": False} == {
        key: value for key, value in decision.report.checks[0].items() if key != "exit_code"}
    assert not decision.report.nested  # the nested check needs a codex that starts
    written = codex_probes.record_decision(RUN_ID, world.state, "code-review-pass", decision, world.planted)
    entry = written["clients"][-1]
    assert (entry["client_requested"], entry["client_applied"], entry["fallback_reason"]) == (
        "codex", "claude", "profile_broken")
    assert records.read_record(RUN_ID, world.state)["clients"][-1]["codex_probes"]["reason"] == "profile_broken"
