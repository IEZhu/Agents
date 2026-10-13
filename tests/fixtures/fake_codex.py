"""A fake ``codex`` for the Codex sandbox tests (#344): ``--version``, ``login status``, ``sandbox`` and ``exec``.

Usage: ``python fake_codex.py <scenario.json> <codex arguments...>``; the tests wrap it in an
executable ``<package>/bin/codex`` next to a ``codex-package.json``. It never touches the network
(only 127.0.0.1 listeners the tests start), the keychain or anything outside the paths the
scenario and the arguments name. Every call appends ``{"argv", "env", "cwd"}`` to the scenario's
``seen`` file when one is named.

* ``--version`` prints ``codex-cli <version>`` (scenario ``version``, default 0.162.1).
* ``login status`` exits 0 with ``Logged in using an API key - sk-proj-...`` on stderr, like the
  real one, or 1 with ``Not logged in`` when the scenario says ``"logged_in": false``.
* ``sandbox [-c sandbox_mode="..."] -- <command>``: ``/usr/bin/true`` is the nested check, answered
  by ``nested``: ``eperm`` (default, like this macOS: ``sandbox-exec: sandbox_apply: Operation not
  permitted``, exit 71) or ``ok``. In ``workspace-write`` mode a command fails with exit 1 without
  running unless ``ws_network`` is ``open`` (then it runs, so ``nc`` reaches the test's listener);
  in the default read-only mode it runs for real (reads are unrestricted) unless ``ro_reads`` is
  ``restricted`` (then only ``/bin/echo`` runs). ``sandbox_broken`` makes every other command fail.
* ``exec ... -`` reads the prompt from standard input, takes the command lines after
  ``Commands:`` and answers with ``--json`` events (``thread.started``, ``turn.started``, one
  completed ``command_execution`` item per command, ``turn.completed``). ``exec`` decides what a
  command does: ``eperm`` (default: exit 71 with the sandbox_apply message, as under a nested
  sandbox), ``sandboxed`` (the probe script's ``control.*`` lines, except the sandbox-loaded
  negative control, run for real; every other line reports a denial without running: exit 1, 6
  for the ``--noproxy`` curl), ``hole`` (every line runs for real, unsandboxed) or ``forge`` (a
  denial is echoed instead of running the script). ``wrap`` reports each command as ``bash -lc
  '<command>'``; ``patch`` adds a ``file_change`` item; ``skip`` (indices) leaves commands out.
"""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

SCENARIO = json.loads(Path(sys.argv[1]).read_text())
ARGS = sys.argv[2:]
LINE = re.compile(r"^(?P<body>.*); echo __probe__ (?P<id>\S+) rc=\$\?$")
DENIED_RC = {"network.external-noproxy.script": 6}
EPERM = "sandbox-exec: sandbox_apply: Operation not permitted\n"


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def seen():
    if SCENARIO.get("seen"):
        with open(SCENARIO["seen"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"argv": ARGS, "env": dict(os.environ), "cwd": os.getcwd()}) + "\n")


def run_line(body):
    result = subprocess.run(["/bin/sh", "-c", body], capture_output=True, text=True, timeout=30)
    return result.stdout, result.returncode


def run_script(path, mode):
    output = []
    for line in Path(path).read_text().splitlines():
        match = LINE.match(line)
        if not match:
            continue
        check, body = match["id"], match["body"]
        if mode == "hole" or (check.startswith("control.") and "sandbox-loaded" not in check):
            text, code = run_line(body)
            output.append(text)
        else:
            code = DENIED_RC.get(check, 1)
        output.append(f"__probe__ {check} rc={code}\n")
    return "".join(output), 0


def do_exec():
    prompt = sys.stdin.read()
    commands = prompt.split("Commands:\n", 1)[1].strip().splitlines() if "Commands:\n" in prompt else []
    mode = SCENARIO.get("exec", "eperm")
    emit({"type": "thread.started", "thread_id": "fake-thread"})
    emit({"type": "turn.started"})
    for index, command in enumerate(commands):
        if index in SCENARIO.get("skip", []):
            continue
        words = shlex.split(command)
        if mode == "eperm":
            output, code = EPERM, 71
        elif mode == "forge":
            command = "echo forged"
            output, code = "", 0
        else:
            output, code = run_script(words[1], mode)
        shown = f"bash -lc {shlex.quote(command)}" if SCENARIO.get("wrap") else command
        emit({"type": "item.completed", "item": {"id": f"item_{index}", "type": "command_execution", "command": shown,
                                                  "aggregated_output": output, "exit_code": code, "status": "completed"}})
    if SCENARIO.get("patch"):
        emit({"type": "item.completed", "item": {"id": "item_patch", "type": "file_change",
                                                  "changes": [{"path": "x", "kind": "add"}], "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "item_done", "type": "agent_message", "text": "DONE"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
                                              "reasoning_output_tokens": 0}})
    output_file = ARGS[ARGS.index("-o") + 1] if "-o" in ARGS else None
    if output_file:
        Path(output_file).write_text("DONE\n")
    return 0


def do_sandbox():
    command = ARGS[ARGS.index("--") + 1:]
    mode = "workspace-write" if 'sandbox_mode="workspace-write"' in ARGS else "read-only"
    if command == ["/usr/bin/true"]:
        if SCENARIO.get("nested", "eperm") == "ok":
            return 0
        sys.stderr.write(EPERM)
        return 71
    if SCENARIO.get("sandbox_broken"):
        sys.stderr.write("fake codex: the sandbox cannot start\n")
        return 1
    if mode == "workspace-write" and SCENARIO.get("ws_network") != "open":
        return 1
    if mode == "read-only" and SCENARIO.get("ro_reads") == "restricted" and command[0] != "/bin/echo":
        return 1
    return subprocess.run(command, timeout=30).returncode


def main():
    seen()
    if ARGS[:1] == ["--version"]:
        print(f"codex-cli {SCENARIO.get('version', '0.162.1')}")
        return 0
    if ARGS[:2] == ["login", "status"]:
        if SCENARIO.get("logged_in", True):
            sys.stderr.write("Logged in using an API key - sk-proj-****fake\n")
            return 0
        sys.stderr.write("Not logged in\n")
        return 1
    if ARGS[:1] == ["sandbox"]:
        return do_sandbox()
    if ARGS[:1] == ["exec"]:
        return do_exec()
    sys.stderr.write(f"fake codex: unsupported arguments {ARGS[:2]}\n")
    return 2


sys.exit(main())
