"""The installers' sync step: scripts/init_repo.sh and init_repo.bat around ``python -m src.user_sync installer``.

Each harness runs the installer's own strict mode and token handling, argument parsing, sync section
and summary line in a disposable checkout whose ``src.user_sync`` is a stub. The stub records its
arguments, whether stdin is a terminal (a console, on Windows), whether it received the GitHub
token and its working directory, and exits with a chosen code. On Unix a pseudo-terminal stands in
for the user's terminal, and fake ``pip``, ``git`` and ``python`` show which other children would
see the token; cmd.exe runs only on Windows (the Windows installer workflow). The step's own
decisions are tested in ``test_user_sync_install.py``; the last test here runs the real step through
the Unix section with a terminal, in a temporary copy of the installation.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ghp_HarnessToken0123456789abcdef"
STUB = '''\
import json, os, sys


def terminal():
    """As src.user_sync.__main__._terminal: a console on Windows, where NUL is a tty too."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if os.name != "nt":
            return True
        import ctypes
        from ctypes import wintypes
        import msvcrt
        get_mode = ctypes.WinDLL("kernel32", use_last_error=True).GetConsoleMode
        get_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_mode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        return bool(get_mode(msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(mode)))
    except (AttributeError, OSError, ValueError):
        return False


with open(os.environ["STUB_EVENTS"], "a", encoding="utf-8") as events:
    events.write(json.dumps({"argv": sys.argv[1:], "terminal": terminal(), "cwd": os.getcwd(),
                             "token": os.environ.get("AGENTS_GITHUB_TOKEN")}) + "\\n")
print("stub: " + " ".join(sys.argv[1:]), flush=True)
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
'''
SYNC_VARIABLES = ("AGENTS_ASSUME_YES", "AGENTS_USER_SYNC_REPO", "AGENTS_USER_SYNC_REMOTE", "AGENTS_USER_SYNC_NAME",
                  "AGENTS_USER_SYNC_EMAIL", "AGENTS_USER_SYNC_LABEL", "AGENTS_GITHUB_TOKEN")


def between(source, start, end):
    first = source.index(start)
    return source[first:source.index(end, first)]


def unix_section(source, *, children=False):
    """init_repo.sh's strict mode and token handling, helpers with the ERR trap, arguments, the sync
    step and the summary line. ``children`` adds the real dependency and model-migration commands
    and a git call, whose fakes report whether they see the token."""
    parts = [between(source, "set -e\n", "# ============== ANSI Colors"),
             between(source, "print_header() {", "check_command() {"),
             between(source, "# ============== Parse Arguments ==============", "# ============== Helper Functions")]
    if children:
        parts += ['SKIP_INSTALL=false\nENV_FILE="$REPO_ROOT/.env"\n',
                  between(source, 'if [ "$SKIP_INSTALL" = false ]; then', "# An earlier install pinned"),
                  between(source, "# An earlier install pinned", "# Pre-download embedding model"),
                  "git --version\n"]  # init_repo.sh runs no git itself; install.sh's is tested apart
    parts += [between(source, "# ============== Sync Between Machines ==============", "# ============== Final Summary"),
              between(source, "# Sync between machines: its state", "# ============== LLM Instructions Block"),
              'echo "REACHED THE END"\n']
    return "".join(parts)


def with_terminal(command, answer=b"", **options):
    """Run ``command`` with a pseudo-terminal on stdin, typed ``answer`` already waiting in it."""
    import pty
    controller, terminal = pty.openpty()
    try:
        if answer:
            os.write(controller, answer)
        return subprocess.run(command, stdin=terminal, capture_output=True, text=True, timeout=120, **options)
    finally:
        os.close(terminal)
        os.close(controller)


@pytest.fixture(params=["bash", "cmd"], ids=["unix", "windows"])
def sync_section(request, tmp_path):
    kind = request.param
    if kind == "cmd" and sys.platform != "win32":
        pytest.skip("requires native cmd.exe")
    if kind == "bash" and sys.platform == "win32":
        pytest.skip("the Unix section runs on Unix; Windows exercises native cmd.exe")
    interpreter = os.environ.get("COMSPEC") if kind == "cmd" else shutil.which("bash")
    if not interpreter:
        pytest.skip(f"{kind} is unavailable")

    checkout = tmp_path / "checkout with spaces"
    package = checkout / "src" / "user_sync"
    package.mkdir(parents=True)
    (checkout / "src" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(STUB)
    (checkout / "requirements.txt").write_text("")
    (checkout / ".env").write_text("")
    events, children = tmp_path / "events.jsonl", tmp_path / "children.txt"
    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    for name in ("pip", "git", "python"):  # what a child of setup would see
        script = fake_bin / name
        script.write_text(f'#!/bin/sh\necho "{name} ${{AGENTS_GITHUB_TOKEN:-none}}" >> "$CHILD_EVENTS"\n')
        script.chmod(0o755)
    env = dict(os.environ, REPO_ROOT=str(checkout), PYTHON_ABS=sys.executable, STUB_EVENTS=str(events),
               CHILD_EVENTS=str(children), PYTHONUTF8="1", RED="", GREEN="", YELLOW="", BLUE="", CYAN="", NC="")
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(name, None)
    env.update({name: "" for name in SYNC_VARIABLES})  # none of the user's own, and .env cannot add one

    source_name = "init_repo.sh" if kind == "bash" else "init_repo.bat"
    source = (ROOT / "scripts" / source_name).read_text(encoding="utf-8")

    def command_for(children_too):
        if kind == "bash":
            script = checkout / "sync.sh"
            script.write_text(unix_section(source, children=children_too), encoding="utf-8")
            return [interpreter, str(script)]
        script = checkout / "sync.bat"
        script.write_bytes(("@echo off\nsetlocal enabledelayedexpansion\n"
                            + between(source, "REM cmd cannot keep a variable", "chcp 65001")
                            + between(source, "REM ============== Parse Arguments ==============", "\n:args_done\n")
                            + "\n:args_done\n"
                            + between(source, "REM ============== Sync Between Machines ==============",
                                      "REM ============== Final Summary")
                            + between(source, "REM Sync between machines: its state",
                                      "REM ============== LLM Instructions Block")
                            + "echo REACHED THE END\nexit /b 0\n").replace("\n", "\r\n").encode("utf-8"))
        return [interpreter, "/d", "/c", script.name]

    def run(*arguments, terminal=False, stub_exit=0, extra_env=None, children_too=False):
        """The section's output, the stub's calls and the fake children's lines."""
        process_env = {**env, **(extra_env or {}), "STUB_EXIT": str(stub_exit)}
        if children_too:
            process_env["PATH"] = str(fake_bin) + os.pathsep + process_env["PATH"]
        options = {"cwd": checkout, "env": process_env, "encoding": "utf-8", "errors": "replace"}
        command = command_for(children_too) + list(arguments)
        if terminal:
            result = with_terminal(command, **options)
        else:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=60, **options)
        calls = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()] \
            if events.exists() else []
        seen = children.read_text(encoding="utf-8").splitlines() if children.exists() else []
        events.unlink(missing_ok=True)
        children.unlink(missing_ok=True)
        return result, calls, seen

    return run, kind, checkout


def test_without_yes_the_step_gets_the_terminal_and_the_summary_follows(sync_section):
    run, kind, checkout = sync_section
    result, calls, _ = run(terminal=kind == "bash")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert [call["argv"] for call in calls] == [["installer"], ["installer", "--summary"]]
    assert all(Path(call["cwd"]).resolve() == checkout.resolve() for call in calls)
    # Unix: a pseudo-terminal. Windows: stdin from NUL, which isatty() calls a tty but is no console.
    assert calls[0]["terminal"] is (kind == "bash")
    assert "Sync Between Machines" in output and "stub: installer" in output and "REACHED THE END" in output


@pytest.mark.parametrize("arguments, extra_env", [
    (["--yes"], None), (["-y"], None), ([], {"AGENTS_ASSUME_YES": "1"}), ([], {"AGENTS_ASSUME_YES": "yes"}),
], ids=["yes", "y", "assume-yes-1", "assume-yes-yes"])
def test_yes_and_assume_yes_never_let_the_step_ask(sync_section, arguments, extra_env):
    run, kind, _ = sync_section
    result, calls, _ = run(*arguments, terminal=kind == "bash", extra_env=extra_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [call["argv"] for call in calls] == [["installer", "--yes"], ["installer", "--summary"]]


def test_a_failing_step_never_fails_setup(sync_section):
    run, _, _ = sync_section
    result, calls, _ = run(stub_exit=1)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "The sync step did not finish; setup continues" in output and "REACHED THE END" in output
    assert "FATAL" not in output and len(calls) == 2


def test_only_the_step_sees_the_github_token(sync_section):
    """Unix: the step's own command gets it as a prefix assignment; pip, git, the python helpers and
    the summary never do. Windows: cmd cannot hide it from children, so nothing gets it."""
    run, kind, _ = sync_section
    result, calls, seen = run("--yes", extra_env={"AGENTS_GITHUB_TOKEN": TOKEN}, children_too=kind == "bash")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    tokens = {tuple(call["argv"]): call["token"] for call in calls}
    if kind == "bash":
        assert tokens == {("installer", "--yes"): TOKEN, ("installer", "--summary"): None}
        assert sorted(seen) == ["git none", "pip none", "pip none", "python none"], seen
    else:
        assert tokens == {("installer", "--yes"): None, ("installer", "--summary"): None}
        assert "AGENTS_GITHUB_TOKEN is not used here" in output and "setup --from-env" in output
    assert TOKEN not in output


def test_the_real_step_asks_in_the_terminal_and_yes_starts_the_wizard(tmp_path):
    """The Unix section with the real step in a copy of the installation: the question in a
    terminal, then the wizard's first one. Nothing is written but the copy's session lease."""
    bash = shutil.which("bash")
    if sys.platform == "win32" or not bash:
        pytest.skip("a pseudo-terminal and bash on Unix")
    if shutil.which("git") is None or shutil.which("ssh-keygen") is None:
        pytest.skip("the step asks only where git and ssh-keygen are installed")
    installation = tmp_path / "installation"
    shutil.copytree(ROOT / "src", installation / "src", ignore=shutil.ignore_patterns("__pycache__"))
    script = tmp_path / "sync.sh"
    script.write_text(unix_section((ROOT / "scripts" / "init_repo.sh").read_text(encoding="utf-8")),
                      encoding="utf-8")
    env = dict(os.environ, REPO_ROOT=str(installation), PYTHON_ABS=sys.executable, PYTHONUTF8="1",
               AGENTS_USER_FLOWS_DIR=str(tmp_path / "library"), XDG_STATE_HOME=str(tmp_path / "state"),
               AGENTS_SERVICE_DIR=str(tmp_path / "service"), RED="", GREEN="", YELLOW="", BLUE="", CYAN="", NC="")
    for name in ("PYTHONHOME", "PYTHONPATH"):
        env.pop(name, None)
    env.update({name: "" for name in SYNC_VARIABLES})

    declined = with_terminal([bash, str(script)], b"n\n", env=env, cwd=tmp_path)
    assert declined.returncode == 0, declined.stdout + declined.stderr
    assert "Set up sync between your machines now? [y/N]" in declined.stdout
    assert "Sync stays off" in declined.stdout and "Sync between machines is off. To turn it on" in declined.stdout
    assert f"cd {installation}" in declined.stdout  # every command says where it runs

    # Yes, then end of input (Ctrl-D) at the wizard's first question: the wizard stops there.
    accepted = with_terminal([bash, str(script)], b"y\n\x04", env=env, cwd=tmp_path)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "Where is the library's repository?" in accepted.stdout
    assert "cancelled; nothing was uploaded" in accepted.stdout and "REACHED THE END" in accepted.stdout
    assert not any((tmp_path / name).exists() for name in ("library", "state", "service"))
    assert (installation / "data" / ".sessions.lock").exists()  # the copy's lease, not the checkout's
