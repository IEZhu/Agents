"""Codex worker isolation and the fallback to the Claude worker (#344), with a fake ``codex``.

The fake (tests/fixtures/fake_codex.py) answers ``--version``, ``login status``, ``sandbox`` and
``exec``. Probe commands run through an executor: `Emulator` reads the profile's ``-D`` parameters
and applies the profile's rules itself (any platform); on macOS, `real_executor` runs them under
the real ``/usr/bin/sandbox-exec`` with the generated profile. The fake ``codex`` itself always runs
directly (a Python script under the home directory cannot run under the profile), so it models the
nested-sandbox result by its scenario. Only 127.0.0.1 listeners the tests start are contacted.
"""
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import threading

import pytest

from src.schedules import cli, codex_probes, codex_proxy, decoys, probes, records, runner, sandbox
from src.schedules import codex_sandbox as cs
from src.schedules import config as autodev_config
from src.schedules.codex_sandbox import CodexPaths, CodexSandboxError

FAKE = Path(__file__).parent / "fixtures" / "fake_codex.py"
PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
RUN_ID = "20261014T120000Z-abcdef"
TEST_NET = "192.0.2.1"  # RFC 5737: never routed; the sandbox refuses the connection before any packet


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


@pytest.fixture
def temp_root():
    root = sandbox.make_temp_root()
    yield root
    sandbox.remove_temp_root(root)


class World:
    def __init__(self, base: Path, state: Path, tmp_root: Path):
        self.base, self.state, self.tmp_root = base, state, tmp_root
        self.home = base / "home"
        for name in (".ssh", "Library/Keychains", ".config/agents-autodev"):
            (self.home / name).mkdir(parents=True)
        (self.home / ".ssh" / "known_hosts").write_text("example.org ssh-ed25519 AAAAfake\n")
        for name in ("tree", "materials", "out", "owner-tmp", "exchange", "repo/.git/hooks", "priv"):
            (base / name).mkdir(parents=True)
        (base / "tree" / "README.md").write_text("tree\n")
        (base / "repo" / ".git" / "config").write_text("[core]\n\tbare = false\n")
        (base / "repo" / "README.md").write_text("one-off\n")
        self.schema = base / "schema.json"
        self.schema.write_text('{"type": "object"}\n')
        self.codex_home = cs.codex_home(state)
        self.codex = fake_codex(base / "fake", {"seen": str(base / "seen.jsonl")})
        self.planted = decoys.ensure(state, self.home)

    def paths(self, kind: str = cs.WORKER, **changes) -> CodexPaths:
        run_home = self.tmp_root / "home"
        if not run_home.exists():
            cs.prepare_run_home(self.tmp_root)
        values = dict(kind=kind, home=self.home, codex_home=self.codex_home, tmp_root=self.tmp_root, run_home=run_home,
                      out_dir=self.base / "out", profile=self.base / "priv" / f"{kind}.sb")
        if kind == cs.WORKER:
            values.update(tree=self.base / "tree", materials=self.base / "materials", schema=self.schema)
        else:
            values.update(repository=self.base / "repo", exchange=self.base / "exchange")
        values.update(changes)
        return CodexPaths(**values)

    def targets(self, *, runner_targets=None) -> codex_probes.CodexTargets:
        return codex_probes.CodexTargets(state_dir=self.state, secrets_dir=self.home / ".config" / "agents-autodev",
                                         owner_temp=self.base / "owner-tmp", runner=runner_targets)


@pytest.fixture
def world(tmp_path_factory, service_state_dir, temp_root):
    return World(real(tmp_path_factory.mktemp("cdx")), service_state_dir, temp_root)


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
        readable = [params.get(name) for name in ("CODEX_HOME", "TMP_DIR", "OUT_DIR", "TREE", "MATERIALS", "SCHEMA",
                                                  "REPO", "EXCHANGE")] + [v for k, v in params.items()
                                                                          if k.startswith("READ_ROOT_")]
        writable = [params.get(name) for name in ("CODEX_HOME", "TMP_DIR", "OUT_DIR", "REPO", "EXCHANGE")]
        command = tail[0]
        if command in ("/bin/cat", "/bin/ls"):
            return _inside(tail[-1], readable)
        if command == "/bin/sh":
            match = re.search(r">>?\s*(.+)$", tail[2])
            target = shlex.split(match[1].replace("$TMPDIR", env["TMPDIR"]))[0]
            denied = params.get("REPO_GIT_CONFIG") == os.path.realpath(target) or _inside(
                target, [params.get("REPO_GIT_HOOKS")])
            return _inside(target, writable) and not denied
        if command == codex_probes.NC:
            return tail[-2] == "127.0.0.1" and tail[-1] == params["PROXY_PORT"]
        if command == codex_probes.CURL:
            return "-x" in tail  # only through the proxy, which decides
        return False


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


no_keychain = with_security(lambda argv: subprocess.CompletedProcess(argv, 44, "", ""))


def decide(world, *, kind=cs.WORKER, execute=None, mode=cs.LOGIN_API_KEY, platform="darwin", paths=None,
           command_runner=subprocess.run, **options):
    paths = paths or world.paths(kind)
    with codex_proxy.ConnectProxy(cs.allowed_hosts(mode)) as proxy:
        decision = codex_probes.decide(world.codex, paths, path=PATH, proxy=proxy, mode=mode, targets=world.targets(),
                                       persistent=world.planted, directory=world.state, run_id=RUN_ID,
                                       effort="high", platform=platform, execute_=execute or Emulator(world.codex),
                                       command_runner=command_runner, resolver=lambda host: TEST_NET, **options)
    return decision


# --- argv -----------------------------------------------------------------------------------------------

def test_worker_argv_starts_with_the_outer_profile_and_never_widens(world):
    launch = cs.build(world.codex, world.paths(), path=PATH, proxy_port=40000, model="gpt-6.1-sol", effort="high")
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
    assert not any(item == "--add-dir" or item.startswith("--dangerously") or "danger-full-access" in item
                   for item in argv)
    assert cs.check_argv(argv, launch.paths.profile) == []
    assert cs.check_launch(launch) == []


def test_stage_argv_writes_only_the_one_off_repository(world):
    launch = cs.build(world.codex, world.paths(cs.STAGE), path=PATH, proxy_port=40000)
    command = launch.command
    assert command[5:10] == ["-s", "workspace-write", "--skip-git-repo-check", "-C", str(world.base / "repo")]
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in command and "--json" in command
    assert command[-1] == "-" and "--output-schema" not in command
    names = [name for name, _ in launch.parameters]
    assert {"REPO", "EXCHANGE", "REPO_GIT_CONFIG", "REPO_GIT_HOOKS"} <= set(names) and "TREE" not in names
    assert '(deny file-write* (literal (param "REPO_GIT_CONFIG"))' in launch.profile


@pytest.mark.parametrize("argv, fragment", [
    (["--add-dir", "/x"], "--add-dir"), (["--dangerously-bypass-approvals-and-sandbox"], "--dangerously"),
    (["-s", "danger-full-access"], "danger-full-access"), (["-c", 'sandbox_mode="danger-full-access"'], "sandbox_mode"),
])
def test_check_argv_refuses_widening_flags(world, argv, fragment):
    profile = world.base / "priv" / "x.sb"
    problems = cs.check_argv([cs.SANDBOX_EXEC, "-f", str(profile), "/bin/codex", "exec", *argv], profile)
    assert any(fragment in problem for problem in problems)
    assert cs.check_argv(["/bin/codex", "exec"], profile)


def test_effort_values_are_the_known_list(world):
    assert cs.check_effort("ultra") == "ultra" and cs.check_effort(None) is None
    with pytest.raises(CodexSandboxError, match="model_reasoning_effort"):
        cs.build(world.codex, world.paths(), path=PATH, proxy_port=40000, effort="turbo")
    with pytest.raises(CodexSandboxError, match="plain name"):
        cs.build(world.codex, world.paths(), path=PATH, proxy_port=40000, model="gpt; rm -rf /")
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


@pytest.mark.parametrize("login, valid", [("api-key", True), ("chatgpt", True), ("keychain:codex-key", True),
                                          ("keychain:", False), ("config-dir", False)])
def test_codex_login_configuration(service_state_dir, login, valid):
    config = {**autodev_config.load(service_state_dir), "codex": {"login": login}}
    if valid:
        autodev_config.validate(config)
        assert cs.login_mode(config) == login
    else:
        with pytest.raises(autodev_config.ConfigError, match="codex.login"):
            autodev_config.validate(config)
    assert autodev_config.DEFAULTS["codex"] == {"login": "api-key"}


# --- the profile ----------------------------------------------------------------------------------------

def test_profile_names_no_path_and_a_path_cannot_inject_rules(world):
    hostile = world.base / 'mat")(allow default)(allow file-write* (subpath "/'
    hostile.mkdir()
    normal = cs.build(world.codex, world.paths(), path=PATH, proxy_port=40000)
    injected = cs.build(world.codex, world.paths(materials=hostile, profile=world.base / "priv" / "h.sb"), path=PATH,
                        proxy_port=40000)
    assert injected.profile == normal.profile
    for launch in (normal, injected):
        for _, value in launch.parameters:
            assert value not in launch.profile
    assert ("MATERIALS", str(hostile)) in injected.parameters
    assert "(allow default)" not in injected.profile and "(deny default)" in injected.profile
    found = os.lstat(world.base / "priv" / "h.sb")
    assert stat.S_IMODE(found.st_mode) == 0o600 and cs.read_profile(world.base / "priv" / "h.sb") == injected.profile


def test_parameters_are_resolved_and_ancestors_get_metadata(world):
    deep = world.home / "Library" / "Application Support" / "state" / "codex-home"
    deep.mkdir(parents=True, mode=0o700)
    link = world.base / "tree-link"
    link.symlink_to(world.base / "tree")
    launch = cs.build(world.codex, world.paths(codex_home=deep, tree=link), path=PATH, proxy_port=40000)
    params = dict(launch.parameters)
    assert params["TREE"] == str(world.base / "tree") and params["CODEX_HOME"] == str(deep)
    ancestors = sorted(value for name, value in launch.parameters if name.startswith("ANCESTOR_"))
    assert ancestors == sorted(str(path) for path in (world.home / "Library", world.home / "Library" / "Application Support",
                                                      world.home / "Library" / "Application Support" / "state"))
    for name in params:
        if name.startswith("ANCESTOR_"):
            assert f'(literal (param "{name}"))' in launch.profile
    assert params["CODEX_PKG"] == str(real(world.codex).parent.parent) and params["PROXY_PORT"] == "40000"


def test_a_tampered_profile_or_changed_argv_is_refused(world):
    launch = cs.build(world.codex, world.paths(), path=PATH, proxy_port=40000)
    world.paths().profile.write_text(launch.profile + "(allow default)\n")
    assert any("profile on disk differs" in problem for problem in cs.check_launch(launch))
    launch = cs.build(world.codex, world.paths(), path=PATH, proxy_port=40000)
    os.chmod(launch.paths.profile, 0o644)
    assert any("0600" in problem for problem in cs.check_launch(launch))
    launch = cs.build(world.codex, world.paths(), path=PATH, proxy_port=40000)
    import dataclasses
    widened = dataclasses.replace(launch, argv=(*launch.argv[:-1], "--add-dir", "/", "-"))
    assert cs.check_launch(widened)


@pytest.mark.parametrize("change, fragment", [
    (lambda w: {"out_dir": w.home}, "equals or contains home"),
    (lambda w: {"tree": w.base / "out" / "t"}, "inside the writable out_dir"),
    (lambda w: {"profile": w.base / "out" / "p.sb"}, "the profile lies inside out_dir"),
    (lambda w: {"materials": w.home / ".ssh"}, "equals or contains ssh"),
    (lambda w: {"out_dir": w.tmp_root / "o"}, "must not lie inside one another"),
    (lambda w: {"tree": None}, "a worker needs tree"),
    (lambda w: {"schema": Path("relative.json")}, "must be absolute"),
    (lambda w: {"kind": cs.STAGE, "repository": w.base / "tree", "exchange": w.base / "exchange"},
     ".git must be a directory"),
])
def test_unsafe_paths_are_refused(world, change, fragment):
    paths = world.paths(**change(world))
    assert any(fragment in problem for problem in paths.problems())
    with pytest.raises(CodexSandboxError, match="unsafe Codex paths"):
        cs.build(world.codex, paths, path=PATH, proxy_port=40000)


def test_the_package_must_be_a_codex_package_outside_the_run(world, tmp_path):
    loose = tmp_path / "loose" / "codex"
    loose.parent.mkdir()
    loose.write_text("#!/bin/sh\n")
    loose.chmod(0o755)
    with pytest.raises(CodexSandboxError, match="codex package"):
        cs.build(str(loose), world.paths(), path=PATH, proxy_port=40000)
    inside = fake_codex(world.base / "tree" / "fake", {})
    with pytest.raises(CodexSandboxError, match="overlap"):
        cs.build(inside, world.paths(), path=PATH, proxy_port=40000)


# --- environment ----------------------------------------------------------------------------------------

def test_environment_is_an_allowlist(world):
    paths = world.paths()
    env = cs.environment(paths, path=PATH, proxy_port=40000)
    assert set(env) == {"PATH", "HOME", "LANG", "CODEX_HOME", "TMPDIR", "HTTPS_PROXY", "HTTP_PROXY"}
    assert env["HOME"] == str(real(world.tmp_root) / "home") and env["CODEX_HOME"] == str(real(world.codex_home))
    assert env["TMPDIR"] == str(real(world.tmp_root)) and env["HTTPS_PROXY"] == "http://127.0.0.1:40000"
    keyed = cs.environment(paths, path=PATH, proxy_port=40000, mode="keychain:svc", api_key="sk-test-not-a-key")
    assert keyed["CODEX_API_KEY"] == "sk-test-not-a-key" and "OPENAI_API_KEY" not in keyed
    with pytest.raises(CodexSandboxError, match="only in the keychain login mode"):
        cs.environment(paths, path=PATH, proxy_port=40000, api_key="sk-test-not-a-key")
    with pytest.raises(CodexSandboxError, match="needs the API key"):
        cs.environment(paths, path=PATH, proxy_port=40000, mode="keychain:svc")
    assert "GH_TOKEN is not on the Codex allowlist" in cs.check_environment({**env, "GH_TOKEN": "x"}, paths, 40000)
    assert any("secret-like" in p for p in cs.check_environment(
        {**env, "LANG": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"}, paths, 40000))
    assert any("must not be empty" in p for p in cs.check_environment({**env, "CODEX_HOME": ""}, paths, 40000))
    with pytest.raises(CodexSandboxError, match="PATH"):
        cs.environment(paths, path="bin:/usr/bin", proxy_port=40000)


# --- CODEX_HOME and login -----------------------------------------------------------------------------

@pytest.mark.parametrize("entry, directory", [("AGENTS.md", False), ("agents.override.MD", False), ("hooks.json", False),
                                              ("config.toml", False), ("work.config.toml", False), ("rules", True),
                                              ("plugins", True)])
def test_hygiene_refuses_instruction_and_extension_sources(world, entry, directory):
    path = world.codex_home / entry
    path.mkdir() if directory else path.write_text("x\n")
    assert any(entry in problem for problem in cs.hygiene_problems(world.codex_home))


def test_hygiene_allows_codex_state_but_not_user_skills(world):
    for name in ("tmp/arg0", "skills/.system/bundled", "sessions", "log"):
        (world.codex_home / name).mkdir(parents=True)
    (world.codex_home / "auth.json").write_text("{}\n")
    assert cs.hygiene_problems(world.codex_home) == []
    (world.codex_home / "skills" / "mine").mkdir()
    assert cs.hygiene_problems(world.codex_home) == ["CODEX_HOME holds user skills: mine"]
    (world.codex_home / "skills" / "mine").rmdir()
    os.rename(world.codex_home / "skills" / ".system", world.base / "elsewhere")
    (world.codex_home / "skills" / ".system").symlink_to(world.base / "elsewhere")
    assert cs.hygiene_problems(world.codex_home) == ["CODEX_HOME holds skills/.system, which is not a plain directory"]
    os.unlink(world.codex_home / "skills" / ".system")
    os.chmod(world.codex_home, 0o755)
    assert cs.hygiene_problems(world.codex_home) == ["CODEX_HOME must be a 0700 directory of this user, not a link"]


def test_login_status_keeps_only_the_exit_status(world, tmp_path):
    status = cs.login_status(world.codex, world.codex_home, path=PATH, home=tmp_path)
    assert status == {"logged_in": True, "detail": "logged in"}
    seen = [json.loads(line) for line in (world.base / "seen.jsonl").read_text().splitlines()]
    assert seen[-1]["argv"] == ["login", "status"] and seen[-1]["env"]["CODEX_HOME"] == str(world.codex_home)
    set_scenario(world.codex, logged_in=False)
    with pytest.raises(cs.CodexLoginRequired) as raised:
        cs.require_login(world.codex, world.codex_home, path=PATH, home=tmp_path)
    assert raised.value.command == f"CODEX_HOME={world.codex_home} codex login"
    assert "Not logged in" not in str(raised.value) and "exit code 1" in str(raised.value)


# --- the proxy ------------------------------------------------------------------------------------------

class Echo:
    """A local TCP listener that echoes, standing in for an upstream host."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while True:
            try:
                connection, _ = self.sock.accept()
            except OSError:
                return
            with connection:
                while data := connection.recv(4096):
                    connection.sendall(data)

    def connect(self, address, timeout):
        self.address = address
        return socket.create_connection(self.sock.getsockname(), timeout=timeout)


def _request(port: int, head: bytes, payload: bytes = b"") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(head)
        answer = client.recv(4096)
        if answer.startswith(b"HTTP/1.1 200") and payload:
            client.sendall(payload)
            answer += client.recv(4096)
        return answer


def test_proxy_tunnels_only_allowlisted_hosts_on_443():
    echo = Echo()
    with codex_proxy.ConnectProxy(cs.API_HOSTS, connect=echo.connect) as proxy:
        answer = _request(proxy.port, b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\n\r\n",
                          b"tls-bytes")
        assert answer.startswith(b"HTTP/1.1 200") and answer.endswith(b"tls-bytes")
        assert echo.address == ("api.openai.com", 443)
        for head in (b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", b"CONNECT api.openai.com:80 HTTP/1.1\r\n\r\n",
                     b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n", b"GET http://api.openai.com/ HTTP/1.1\r\n\r\n",
                     b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n"):
            assert _request(proxy.port, head).startswith(b"HTTP/1.1 403")
        assert _request(proxy.port, b"X" * (codex_proxy.HEADER_LIMIT + 10)).startswith(b"HTTP/1.1 403")
        assert proxy.targets(allowed=True) == ["api.openai.com:443"]
        assert {"example.com:443", "api.openai.com:80", "chatgpt.com:443", "127.0.0.1:443"} <= set(
            proxy.targets(allowed=False))
        assert {"method": "GET", "target": "http:??api.openai.com?", "allowed": False} in proxy.requests
        assert proxy.tunnels == 1 and proxy.accepted >= 6
    with codex_proxy.ConnectProxy(cs.allowed_hosts(cs.LOGIN_CHATGPT), connect=echo.connect) as proxy:
        for host in ("chatgpt.com", "auth.openai.com", "api.openai.com"):
            assert _request(proxy.port, f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 200")
    with pytest.raises(ValueError):
        codex_proxy.ConnectProxy(["not a host"])


def test_proxy_close_ends_open_tunnels():
    echo = Echo()
    proxy = codex_proxy.ConnectProxy(cs.API_HOSTS, connect=echo.connect).start()
    client = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    client.sendall(b"CONNECT api.openai.com:443 HTTP/1.1\r\n\r\n")
    assert client.recv(4096).startswith(b"HTTP/1.1 200")
    proxy.close()
    client.settimeout(5)
    assert client.recv(4096) == b""
    client.close()
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
    ids = {entry["id"] for entry in report.checks}
    assert {"control.codex-start", "control.read-materials", "control.write-output", "control.proxy-port",
            "network.direct", "network.proxy-refused", "list.ssh-dir", "list.keychains-dir", "list.secrets-dir",
            "list.state-dir", "write.tree", "write.decoy-dir", "write.private-dir"} <= ids
    assert {f"read.{decoy.label}" for decoy in world.planted if decoy.path} <= ids
    assert "example.com:443" in report.observed["proxy"]["refused"]
    written = codex_probes.record_decision(RUN_ID, world.state, "code-review-pass", decision, world.planted)
    entry = written["clients"][0]
    assert (entry["step"], entry["client_requested"], entry["client_applied"], entry["fallback_reason"]) == (
        "code-review-pass", "codex", "claude", "nested_sandbox_unavailable")
    text = json.dumps(written)
    assert not any(decoy.marker in text for decoy in world.planted)
    assert not list(Path(world.base / "out").glob("probe-*")) and not list(Path(world.tmp_root).glob("probe-*"))
    assert not list(Path(world.base / "materials").iterdir())


def test_codex_is_applied_only_when_every_check_passes_and_start_is_guarded(world, monkeypatch):
    set_scenario(world.codex, nested="ok")
    decision = decide(world)
    assert decision.codex and decision.reason is None and decision.report.ok
    started = []

    class Popen:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs))
            self.stdin = None

    with codex_proxy.ConnectProxy(cs.API_HOSTS) as other:
        with pytest.raises(CodexSandboxError, match="proxy is not running"):
            codex_probes.start(decision, "review", proxy=other, platform="darwin")
    proxy = SimpleProxy(decision.launch.proxy_port, cs.API_HOSTS)
    with pytest.raises(CodexSandboxError, match="older than"):
        codex_probes.start(decision, "review", proxy=proxy, platform="darwin", now=lambda: decision.report.finished_at + 901)
    monkeypatch.setattr(codex_probes.subprocess, "Popen", Popen)
    codex_probes.start(decision, "review", proxy=proxy, platform="darwin")
    assert started[0][0][:3] == [cs.SANDBOX_EXEC, "-f", str(decision.launch.paths.profile)]
    assert started[0][1]["env"] == dict(decision.launch.env) and started[0][1]["start_new_session"]
    decision.launch.paths.profile.write_text("(version 1)(allow default)\n")
    os.chmod(decision.launch.paths.profile, 0o600)
    with pytest.raises(CodexSandboxError, match="changed since its probes"):
        codex_probes.start(decision, "review", proxy=proxy, platform="darwin")
    with pytest.raises(CodexSandboxError, match="only a Codex decision"):
        codex_probes.start(codex_probes.Decision("codex", "claude", "nested_sandbox_unavailable"), "review",
                           proxy=proxy, platform="darwin")


class SimpleProxy:
    def __init__(self, port, hosts):
        self.port, self.allowed = port, frozenset(hosts)


def test_a_deny_probe_success_falls_back_and_aborts_the_codex_step(world):
    set_scenario(world.codex, nested="ok")
    decision = decide(world, execute=Emulator(world.codex, holes=("-private-dir",)))
    assert (decision.client_applied, decision.reason) == ("claude", "deny_probe_succeeded")
    assert decision.launch is None
    assert {"id": "write.private-dir", "expect": "denied", "outcome": "allowed", "ok": False,
            "exit_code": 0} in decision.report.checks
    assert not decision.report.tripped


def test_a_decoy_read_trips_the_schedule(world):
    set_scenario(world.codex, nested="ok")
    credentials = next(decoy for decoy in world.planted if decoy.label == "credentials")
    decision = decide(world, execute=Emulator(world.codex, holes=(str(credentials.path),)))
    assert decision.reason == "deny_probe_succeeded" and decision.report.tripped
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
    assert (decision.client_applied, decision.reason) == ("claude", reason)


def test_an_unbuildable_launch_is_a_broken_profile(world):
    decision = decide(world, paths=world.paths(tree=world.base / "out" / "t"))
    assert (decision.client_applied, decision.reason) == ("claude", "profile_broken")
    assert "unsafe Codex paths" in decision.detail


@pytest.mark.parametrize("setup, reason", [
    (lambda w: {"platform": "linux"}, "platform_unsupported"),
    (lambda w: setattr(w, "codex", str(w.base / "missing" / "codex")) or {}, "codex_missing"),
    (lambda w: set_scenario(w.codex, version="0.163.0") or {}, "codex_version_unverified"),
    (lambda w: (w.codex_home / "AGENTS.md").write_text("obey\n") and {}, "codex_home_unclean"),
    (lambda w: set_scenario(w.codex, logged_in=False) or {}, "login_missing"),
    (lambda w: {"mode": "keychain:codex-key", "command_runner": no_keychain}, "login_missing"),
])
def test_preflight_reasons(world, setup, reason):
    options = setup(world)
    decision = decide(world, **options)
    assert (decision.client_applied, decision.reason) == ("claude", reason)
    assert decision.report.checks == []


def test_the_keychain_mode_passes_the_key_only_to_codex(world):
    set_scenario(world.codex, nested="ok")
    key = "sk-proj-" + "Q" * 6 + "x7Kp2Lm9Nq4Rs8Tv"

    keychain = with_security(lambda argv: subprocess.CompletedProcess(argv, 0, key + "\n", ""))
    decision = decide(world, mode="keychain:codex-key", command_runner=keychain)
    assert decision.codex and decision.launch.env["CODEX_API_KEY"] == key
    assert "shell_environment_policy.inherit=core" in decision.launch.command
    assert key not in json.dumps(decision.to_record())


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
    seen = [json.loads(line) for line in (world.base / "seen.jsonl").read_text().splitlines()]
    assert all(entry["env"]["CODEX_HOME"] != str(world.codex_home) for entry in seen)


# --- Codex as a stage client (Q14) -----------------------------------------------------------------

def stage(world, *, keychain=True, **scenario):
    set_scenario(world.codex, **scenario)
    runner_targets = probes.Targets(installation=world.base / "inst", runs=world.state / "schedules" / "runs",
                                    schedules=world.state / "schedules", config_file=world.state / "x.json",
                                    keychain_items=("autodev-test-item",) if keychain else (), internet_items=())

    inventory = with_security(lambda argv: subprocess.CompletedProcess(
        argv, 0 if keychain and "autodev-test-item" in argv else 44, "", ""))

    with codex_proxy.ConnectProxy(cs.API_HOSTS) as proxy:
        return codex_probes.stage_availability(
            world.codex, world.paths(cs.STAGE), path=PATH, proxy=proxy, mode=cs.LOGIN_API_KEY,
            targets=world.targets(runner_targets=runner_targets), persistent=world.planted, directory=world.state,
            run_id=RUN_ID, platform="darwin", execute_=Emulator(world.codex), command_runner=inventory,
            resolver=lambda host: TEST_NET, timeout=60)


def test_stage_unavailable_while_the_nested_sandbox_fails(world):
    decision = stage(world)
    assert (decision.client_applied, decision.reason) == ("claude", "nested_sandbox_unavailable")
    stored = codex_probes.read_availability(world.state)
    assert stored["available"] is False and stored["reason"] == "nested_sandbox_unavailable"
    assert not decision.report.exec_session  # no model turn


def test_stage_unavailable_when_workspace_write_reaches_the_network(world):
    decision = stage(world, nested="ok", ws_network="open")
    assert decision.reason == "workspace_write_network_open" and not decision.report.exec_session


@pytest.mark.parametrize("scenario, reason", [
    ({"exec": "eperm"}, "stage_probe_failed"),
    ({"exec": "forge"}, "stage_probe_failed"),
    ({"exec": "sandboxed", "patch": True}, "stage_probe_failed"),
    ({"exec": "sandboxed", "skip": [1]}, "stage_probe_failed"),
    ({"exec": "hole"}, "deny_probe_succeeded"),
])
def test_a_failing_stage_probe_keeps_codex_unavailable(world, scenario, reason):
    decision = stage(world, nested="ok", **scenario)
    assert (decision.client_applied, decision.reason) == ("claude", reason)
    assert codex_probes.read_availability(world.state)["available"] is False
    assert decision.report.exec_session["commands"] >= 1 or scenario.get("skip")
    if scenario["exec"] == "hole":
        assert decision.report.tripped
    assert not list((world.base / "exchange").glob("probe-*")) and not list((world.base / "repo").glob(".autodev-*"))


@pytest.mark.parametrize("wrap", [False, True])
def test_stage_available_when_every_probe_is_denied(world, wrap):
    decision = stage(world, nested="ok", exec="sandboxed", wrap=wrap)
    assert decision.codex, decision.report.failures
    stored = codex_probes.read_availability(world.state)
    assert stored["available"] is True and stored["version"] == "0.162.1"
    assert stored["installed"]["workspace_write_blocks_network"] is True
    ids = {entry["id"] for entry in decision.report.checks}
    assert {"control.exchange.script", "control.repo.script", "control.sandbox-loaded.script", "network.external.script",
            "keychain.autodev-test-item.script", "write.repo-git-config.script"} <= ids
    seen = [json.loads(line) for line in (world.base / "seen.jsonl").read_text().splitlines()]
    executed = [entry for entry in seen if entry["argv"][:1] == ["exec"]]
    assert executed and executed[-1]["argv"] == decision.launch.command[1:]
    given = executed[-1]["env"]  # plus what the wrapper's shell and the platform add (PWD, SHLVL, ...)
    assert {name: given.get(name) for name in decision.launch.env} == dict(decision.launch.env)
    assert not any(cs.FORBIDDEN_NAME.search(name) for name in given)


def test_stage_without_any_keychain_probe_is_untested(world):
    decision = stage(world, keychain=False, nested="ok", exec="sandboxed")
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
        "not json", json.dumps({"type": "turn.failed", "error": {"message": "m"}})]))
    assert [c["command"] for c in stream.commands] == ["ls"] and stream.unexpected == ["web_search"]
    assert stream.unparsed == 1 and stream.failed


# --- the CLI --------------------------------------------------------------------------------------------

def _ctx(world, **changes):
    values = dict(installation=world.base / "inst", directory=world.state, home=world.home, platform="darwin",
                  codex=world.codex, codex_execute=Emulator(world.codex), codex_resolver=lambda host: TEST_NET,
                  command_runner=subprocess.run)
    values.update(changes)
    return runner.Context(**values)


def test_cli_refuses_without_the_confirmation(world, capsys):
    assert cli.main(["codex-probe"], _ctx(world)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "confirmation_required" and "--confirm-live-probe" in result["message"]
    runs = records.runs_dir(world.state)
    assert not runs.exists() or not any(runs.iterdir())


def test_cli_worker_check_records_the_fallback(world, capsys):
    code = cli.main(["codex-probe", "--kind", "worker", "--confirm-live-probe"], _ctx(world))
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["status"] == "refused"
    assert (result["client_requested"], result["client_applied"], result["fallback_reason"]) == (
        "codex", "claude", "nested_sandbox_unavailable")
    assert result["codex_probes"]["installed"]["workspace_write_blocks_network"] is True
    stored = records.read_record(result["run_id"], world.state)
    assert stored["fallback_reason"] == "nested_sandbox_unavailable" and stored["trigger"] == {"kind": "codex-probe"}
    assert not (records.run_dir(result["run_id"], world.state) / "codex-work").exists()


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
        launch = cs.build(world.codex, world.paths(materials=hostile), path=PATH, proxy_port=allowed.getsockname()[1])

        def rc(*command):
            return subprocess.run(launch.wrap(command), env=dict(launch.env), cwd=launch.cwd, capture_output=True,
                                  timeout=30).returncode

        assert rc("/bin/cat", str(hostile / "m.txt")) == 0 and rc("/bin/ls", str(world.base / "tree")) == 0
        assert rc("/bin/cat", str(world.home / "secret.txt")) != 0 and rc("/bin/ls", str(world.home / ".ssh")) != 0
        assert rc("/bin/ls", str(world.state)) != 0
        assert rc("/bin/sh", "-c", f"printf x > '{world.base}/out/w'") == 0
        for target in (world.base / "tree" / "w", hostile / "w", world.home / "w", world.base / "priv" / "w"):
            assert rc("/bin/sh", "-c", f"printf x > {shlex.quote(str(target))}") != 0 and not target.exists()
        assert rc("/usr/bin/nc", "-z", "-w", "2", "127.0.0.1", str(allowed.getsockname()[1])) == 0
        assert rc("/usr/bin/nc", "-z", "-w", "2", "127.0.0.1", str(other.getsockname()[1])) != 0
        assert rc("/usr/bin/nc", "-z", "-G", "2", "-w", "2", TEST_NET, "443") != 0
        stage_launch = cs.build(world.codex, world.paths(cs.STAGE), path=PATH, proxy_port=allowed.getsockname()[1])

        def stage_rc(*command):
            return subprocess.run(stage_launch.wrap(command), env=dict(stage_launch.env), cwd=stage_launch.cwd,
                                  capture_output=True, timeout=30).returncode

        assert stage_rc("/bin/sh", "-c", f"printf x > '{world.base}/repo/w'") == 0
        assert stage_rc("/bin/sh", "-c", f"printf x > '{world.base}/exchange/w'") == 0
        assert stage_rc("/bin/sh", "-c", f": >> '{world.base}/repo/.git/config'") != 0
        assert stage_rc("/bin/sh", "-c", f"printf x > '{world.base}/repo/.git/hooks/pre-commit'") != 0
        assert not (world.base / "repo" / ".git" / "hooks" / "pre-commit").exists()
    finally:
        allowed.close()
        other.close()


@needs_seatbelt
def test_the_worker_probes_under_the_real_sandbox(world):
    decision = decide(world, execute=real_executor(world.codex))
    assert decision.reason == "nested_sandbox_unavailable"
    failed = [entry for entry in decision.report.checks if not entry["ok"]]
    assert failed == [] and len(decision.report.failures) == 1
    set_scenario(world.codex, nested="ok")
    decision = decide(world, execute=real_executor(world.codex))
    assert decision.codex, decision.report.failures
