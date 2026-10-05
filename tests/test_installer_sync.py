"""The installers' sync step: scripts/init_repo.sh and init_repo.bat around ``python -m src.user_sync installer``.

Each harness runs the installer's own strict mode (Unix), argument parsing, sync section and summary
line in a disposable checkout whose ``src.user_sync`` is a stub. The stub records its arguments,
whether stdin is a terminal and its working directory, and exits with a chosen code. On Unix a
pseudo-terminal stands in for the user's terminal; cmd.exe runs only on Windows (the Windows
installer workflow). The step's own decisions are tested in ``test_user_sync_install.py``; the last
test here runs the real step through the Unix section with a terminal, answering its question.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
STUB = '''\
import json, os, sys
with open(os.environ["STUB_EVENTS"], "a", encoding="utf-8") as events:
    events.write(json.dumps({"argv": sys.argv[1:], "terminal": sys.stdin.isatty(), "cwd": os.getcwd()}) + "\\n")
print("stub: " + " ".join(sys.argv[1:]), flush=True)
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
'''
SYNC_VARIABLES = ("AGENTS_ASSUME_YES", "AGENTS_USER_SYNC_REPO", "AGENTS_USER_SYNC_REMOTE", "AGENTS_USER_SYNC_NAME",
                  "AGENTS_USER_SYNC_EMAIL", "AGENTS_USER_SYNC_LABEL", "AGENTS_GITHUB_TOKEN")


def between(source, start, end):
    first = source.index(start)
    return source[first:source.index(end, first)]


def unix_section(source):
    """init_repo.sh's strict mode, helpers with the ERR trap, arguments, sync step and summary line."""
    return (between(source, "set -e\n", "# ============== ANSI Colors")
            + between(source, "print_header() {", "check_command() {")
            + between(source, "# ============== Parse Arguments ==============", "# ============== Helper Functions")
            + between(source, "# ============== Sync Between Machines ==============", "# ============== Final Summary")
            + between(source, "# How to turn sync between machines on", "# ============== LLM Instructions Block")
            + 'echo "REACHED THE END"\n')


def with_terminal(command, answer=b"", **options):
    """Run ``command`` with a pseudo-terminal on stdin, typed ``answer`` already waiting in it."""
    import pty
    controller, terminal = pty.openpty()
    try:
        if answer:
            os.write(controller, answer)
        return subprocess.run(command, stdin=terminal, capture_output=True, text=True, timeout=60, **options)
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
    events = tmp_path / "events.jsonl"
    env = dict(os.environ, REPO_ROOT=str(checkout), PYTHON_ABS=sys.executable, STUB_EVENTS=str(events),
               PYTHONUTF8="1", RED="", GREEN="", YELLOW="", BLUE="", CYAN="", NC="")
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__", *SYNC_VARIABLES):
        env.pop(name, None)

    if kind == "bash":
        script = checkout / "sync.sh"
        script.write_text(unix_section((ROOT / "scripts" / "init_repo.sh").read_text(encoding="utf-8")),
                          encoding="utf-8")
        command = [interpreter, str(script)]
    else:
        source = (ROOT / "scripts" / "init_repo.bat").read_text(encoding="utf-8")
        script = checkout / "sync.bat"
        script.write_bytes(("@echo off\nsetlocal enabledelayedexpansion\n"
                            + between(source, "REM ============== Parse Arguments ==============", "\n:args_done\n")
                            + "\n:args_done\n"
                            + between(source, "REM ============== Sync Between Machines ==============",
                                      "REM ============== Final Summary")
                            + between(source, "REM How to turn sync between machines on",
                                      "REM ============== LLM Instructions Block")
                            + "echo REACHED THE END\nexit /b 0\n").replace("\n", "\r\n").encode("utf-8"))
        command = [interpreter, "/d", "/c", script.name]

    def run(*arguments, terminal=False, stub_exit=0, extra_env=None):
        """The section's output and the stub's calls; ``terminal`` (Unix) puts a terminal on stdin."""
        options = {"cwd": checkout, "env": {**env, **(extra_env or {}), "STUB_EXIT": str(stub_exit)},
                   "encoding": "utf-8", "errors": "replace"}
        if terminal:
            result = with_terminal(command + list(arguments), **options)
        else:
            result = subprocess.run(command + list(arguments), stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=60, **options)
        calls = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()] \
            if events.exists() else []
        events.unlink(missing_ok=True)
        return result, calls

    return run, kind, checkout


def test_without_yes_the_step_gets_the_terminal_and_the_summary_follows(sync_section):
    run, kind, checkout = sync_section
    result, calls = run(terminal=kind == "bash")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert [call["argv"] for call in calls] == [["installer"], ["installer", "--summary"]]
    assert all(Path(call["cwd"]).resolve() == checkout.resolve() for call in calls)
    if kind == "bash":  # cmd.exe cannot be given a console here; the step decides about it itself
        assert calls[0]["terminal"] is True
    assert "Sync Between Machines" in output and "stub: installer" in output and "REACHED THE END" in output


@pytest.mark.parametrize("arguments, extra_env", [
    (["--yes"], None), (["-y"], None), ([], {"AGENTS_ASSUME_YES": "1"}), ([], {"AGENTS_ASSUME_YES": "yes"}),
], ids=["yes", "y", "assume-yes-1", "assume-yes-yes"])
def test_yes_and_assume_yes_never_let_the_step_ask(sync_section, arguments, extra_env):
    run, kind, _ = sync_section
    result, calls = run(*arguments, terminal=kind == "bash", extra_env=extra_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [call["argv"] for call in calls] == [["installer", "--yes"], ["installer", "--summary"]]


def test_a_failing_step_never_fails_setup(sync_section):
    run, _, _ = sync_section
    result, calls = run(stub_exit=1)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "The sync step did not finish; setup continues" in output and "REACHED THE END" in output
    assert "FATAL" not in output and len(calls) == 2


def test_the_real_step_asks_in_the_terminal_and_yes_starts_the_wizard(tmp_path):
    """The Unix section with the real step: the question in a terminal, then the wizard's first one."""
    bash = shutil.which("bash")
    if sys.platform == "win32" or not bash:
        pytest.skip("a pseudo-terminal and bash on Unix")
    if shutil.which("git") is None or shutil.which("ssh-keygen") is None:
        pytest.skip("the step asks only where git and ssh-keygen are installed")
    if (ROOT / "data" / ".shared-service.json").exists():
        pytest.skip("a daemon serves this checkout: yes would open its settings page")
    script = tmp_path / "sync.sh"
    script.write_text(unix_section((ROOT / "scripts" / "init_repo.sh").read_text(encoding="utf-8")),
                      encoding="utf-8")
    env = dict(os.environ, REPO_ROOT=str(ROOT), PYTHON_ABS=sys.executable, PYTHONUTF8="1",
               AGENTS_USER_FLOWS_DIR=str(tmp_path / "library"), XDG_STATE_HOME=str(tmp_path / "state"),
               AGENTS_SERVICE_DIR=str(tmp_path / "service"), RED="", GREEN="", YELLOW="", BLUE="", CYAN="", NC="")
    for name in ("PYTHONHOME", "PYTHONPATH", *SYNC_VARIABLES):
        env.pop(name, None)

    declined = with_terminal([bash, str(script)], b"n\n", env=env, cwd=tmp_path)
    assert declined.returncode == 0, declined.stdout + declined.stderr
    assert "Set up sync between your machines now? [y/N]" in declined.stdout
    assert "Sync stays off" in declined.stdout and "Sync between machines is off. To turn it on" in declined.stdout

    # Yes, then end of input (Ctrl-D) at the wizard's first question: the wizard stops there.
    accepted = with_terminal([bash, str(script)], b"y\n\x04", env=env, cwd=tmp_path)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "Where is the library's repository?" in accepted.stdout
    assert "cancelled; nothing was uploaded" in accepted.stdout and "REACHED THE END" in accepted.stdout
    assert not any(path.is_file() for path in tmp_path.rglob("*") if path != script)
