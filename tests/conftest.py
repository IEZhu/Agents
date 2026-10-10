"""Keep the test suite away from the live install's derived data (issue #68).

The checkout this suite runs from is usually also the live install: the shared
daemon and stdio servers read ``data/skills_store.*``, ``data/implants_store.*``
and their hash files from here. Two things used to let tests rewrite them:

* ``src.engine.enrichment`` builds ``SkillRetriever()``/``ImplantRetriever()`` on
  first use (``get_skill_retriever()``), and they reindex into ``DATA_DIR``
  whenever the stored hash does not match. pytest does not load ``.env``, so ``EMBEDDING_MODEL`` fell back to
  MiniLM, the hash never matched, and the live stores were rebuilt under the
  wrong model.
* ``src.self_update`` derives ``STATE_FILE``/``CHECK_STAMP``/``LOCK_FILE`` from
  ``INSTALL_DATA_DIR`` at import time, so tests that do not redirect them write
  the live ``data/.last_update.json``.

This runs at module level because pytest imports ``conftest.py`` before it
collects test modules, and the retrievers bind their paths when first built. It
patches ``src.engine.config`` attributes instead of adding an environment hook:
an env var would be inherited by the staged-update ``src.reindex`` subprocess
(``src/self_update.py``) and redirect its stores out of the worktree.
"""
import atexit
import os
import shutil
import tempfile
import time

from dotenv import dotenv_values

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIVE_DATA_DIR = os.path.join(_REPO_ROOT, "data")

# Stores and their hash files: copying them lets the retrievers see a matching
# hash and skip re-embedding. On a host without the same fastembed snapshot the
# fingerprint differs and they re-embed, but only into the temporary copy.
_SEEDED_FILES = (
    "skills_store.npz",
    "skills_store.json",
    ".skills_hash",
    "implants_store.npz",
    "implants_store.json",
    ".implants_hash",
)

# Read by src/client_paths.py, which the installers, instruction updates and
# daemon migration/audit all use to locate client configuration files.
CLIENT_CONFIG_OVERRIDES = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "AGENTS_CURSOR_MCP_CONFIG",
    "AGENTS_CLAUDE_DESKTOP_CONFIG",
    "AGENTS_ANTIGRAVITY_MCP_CONFIG",
)


def _pin_embedding_model() -> None:
    """Use the install's model so the copied stores' hashes can match."""
    if os.environ.get("EMBEDDING_MODEL"):
        return
    model = dotenv_values(os.path.join(_REPO_ROOT, ".env")).get("EMBEDDING_MODEL")
    if model:
        os.environ["EMBEDDING_MODEL"] = model


def _isolated_data_dir() -> str:
    """A temporary ``data`` directory seeded with copies of the live stores."""
    root = tempfile.mkdtemp(prefix="agents-tests-")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    data = os.path.join(root, "data")  # Tests assert the directory is named "data".
    os.mkdir(data)
    for name in _SEEDED_FILES:
        source = os.path.join(_LIVE_DATA_DIR, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(data, name))
    return data


_pin_embedding_model()
TEST_DATA_DIR = _isolated_data_dir()
# The router prefers this variable over config.DATA_DIR; a value inherited from
# a stdio/daemon environment would point it at live router state.
os.environ["AGENTS_ROUTER_DATA_DIR"] = os.path.join(TEST_DATA_DIR, "router")
# Claude Code exports its project to the servers and hooks it starts. Inherited,
# it would outrank the cwd that client-root tests control and aim memory and
# flows at the live project.
os.environ.pop("CLAUDE_PROJECT_DIR", None)
# These select client configuration files and outrank the temporary home that
# installer and migration tests pass in. Inherited from a shell or a Claude Code
# session, they would make those tests rewrite the user's real client config.
for _name in CLIENT_CONFIG_OVERRIDES:
    os.environ.pop(_name, None)

from src.engine import config as _config  # noqa: E402  (must follow the env pins)

# Same override as evals/runners/persona_server.py. DATA_DIR is otherwise a
# PEP 562 alias; setting it explicitly keeps both names in step.
_config.DATA_DIR = _config.INSTALL_DATA_DIR = TEST_DATA_DIR
_config.AUTO_UPDATE_STAGING_DIR = os.path.join(TEST_DATA_DIR, ".prepared")


@pytest.fixture
def persona_components(tmp_path, monkeypatch):
    """A minimal installation tree for flow personas: two agents and one of each component.

    Removing a file from the returned root simulates a component deleted later.
    """
    from src.engine import rules
    from src.utils import prompt_loader

    root = tmp_path / "components"
    for agent in ("code_reviewer", "software_engineer"):
        path = root / "agents" / agent / "system_prompt.mdc"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nidentity: {{name: {agent}, role: Role}}\n---\nBody\n", encoding="utf-8")
    for relative, text in (("skills/skill-a.mdc", "---\ndescription: A\n---\nA\n"),
                           ("implants/implant-b.mdc", "---\ndescription: B\n---\nB\n"),
                           ("rules/rule-truth.mdc",
                            "---\nname: truth\ndescription: T\ncategory: honesty\npriority: 1\n---\nT\n")):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")
    monkeypatch.setattr(prompt_loader, "REPO_ROOT", str(root))
    for variable, folder in (("AGENTS_DIR", "agents"), ("SKILLS_DIR", "skills"), ("IMPLANTS_DIR", "implants")):
        monkeypatch.setattr(prompt_loader, variable, str(root / folder))
    monkeypatch.setattr(rules, "RULES_DIR", str(root / "rules"))
    monkeypatch.setattr(rules, "RULES_ENABLED", True)
    return root


@pytest.fixture(autouse=True)
def scheduled_sync_calls(monkeypatch):
    """Tests never reach the OS scheduler (#168) through the daemon.

    ``src.daemon.control.stop_scheduled_sync`` removes this installation's scheduled sync run
    with launchctl, systemctl or crontab; ``install`` and the daemon's sync task call it. Here it
    only records the installation of each call (None for the default) and reports ``none``.
    """
    from src.daemon import control  # standard library only, on every platform

    calls = []

    def record(installation=None):
        calls.append(installation)
        return "none"
    monkeypatch.setattr(control, "stop_scheduled_sync", record)
    return calls


@pytest.fixture
def pending_stdin_read():
    """Windows: this process's stdin is a stdio MCP server's, with the read that waits for a message.

    Node clients (the Claude desktop app, Claude Code) hand a server a synchronous named-pipe
    client end, and the server's reader blocks in ReadFile on it. A child that inherits that
    handle blocks at startup until the read returns: git for Windows queries its standard
    handles there. Yields the seconds a subprocess call may take before it counts as blocked.
    """
    if os.name != "nt":
        pytest.skip("the inherited-handle block is specific to Windows")
    import ctypes
    import ctypes.wintypes as wt
    import threading
    import uuid
    import _winapi

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetStdHandle.argtypes = [wt.DWORD, wt.HANDLE]
    kernel32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    std_input = wt.DWORD(_winapi.STD_INPUT_HANDLE & 0xFFFFFFFF)
    outbound, write_attributes = 0x2, 0x100  # PIPE_ACCESS_OUTBOUND, FILE_WRITE_ATTRIBUTES, as libuv opens it
    name = rf"\\.\pipe\agents-core-test-{uuid.uuid4().hex}"
    server = _winapi.CreateNamedPipe(name, outbound | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE,
                                     0, 1, 65536, 65536, 0, _winapi.NULL)
    client = _winapi.CreateFile(name, _winapi.GENERIC_READ | write_attributes, 0, _winapi.NULL,
                                _winapi.OPEN_EXISTING, 0, _winapi.NULL)
    previous = _winapi.GetStdHandle(_winapi.STD_INPUT_HANDLE)
    assert kernel32.SetStdHandle(std_input, client)
    started = threading.Event()

    def read():
        buffer, count = ctypes.create_string_buffer(1), wt.DWORD()
        started.set()
        kernel32.ReadFile(client, buffer, 1, ctypes.byref(count), None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    started.wait()
    time.sleep(0.2)  # ReadFile is pending once the thread is inside it
    try:
        yield 2.0
    finally:
        kernel32.SetStdHandle(std_input, previous)
        _winapi.WriteFile(server, b"x")  # the message that ends the read
        reader.join(5)
        _winapi.CloseHandle(client)
        _winapi.CloseHandle(server)


@pytest.fixture(autouse=True)
def service_platform(monkeypatch):
    """The daemon's tests see launchd on every platform, as they did before the Windows backend
    (#195), and never reach Task Scheduler: a test of ``src.daemon.service.TaskScheduler`` sets
    ``service.PLATFORM`` to ``win32`` and passes its own runner. Returns the service module.
    """
    from src.daemon import service

    def refuse(argv, **_):
        raise AssertionError(f"a test reached the OS scheduler: {argv}")
    monkeypatch.setattr(service, "PLATFORM", "darwin")
    monkeypatch.setattr(service, "RUNNER", refuse)
    return service


@pytest.fixture(autouse=True)
def no_server_restart(monkeypatch):
    """A test never restarts the test process as the updated server.

    ``self_update._reexec_updated_server`` execs ``sys.argv`` again, which here is pytest: on
    POSIX it would replace the session, and on Windows, which serves the restarted server from
    a child, every child would run the session again. A test that expects a restart replaces
    ``_exec_server`` or ``_reexec_updated_server`` itself.
    """
    from src import self_update

    def refuse(executable, argv):
        # Not AssertionError: activation turns any Exception into the SystemExit a test may expect.
        pytest.fail(f"a test restarted the test process: {argv}")
    monkeypatch.setattr(self_update, "_exec_server", refuse)
