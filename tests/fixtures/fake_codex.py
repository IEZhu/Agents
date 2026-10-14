"""A fake ``codex`` for the Codex sandbox tests (#344): ``--version``, ``sandbox`` and ``exec``.

Usage: ``python fake_codex.py <scenario.json> <codex arguments...>``; the tests wrap it in an
executable ``<package>/bin/codex`` next to a ``codex-package.json``. It never touches the network
(only 127.0.0.1 listeners the tests start), the keychain or anything outside the paths the
scenario and the arguments name. Every call appends ``{"argv", "env", "cwd"}`` to the scenario's
``seen`` file when one is named.

Like the real one, it refuses (exit 2, ``error: unexpected argument``) every flag, value and ``-c``
key outside the set verified against codex-cli 0.162.1 (`EXEC_FLAGS`, `EXEC_OPTIONS`, `FEATURES`,
`CONFIG_KEYS`), so a misspelled switch fails the tests instead of passing silently; ``exec``
without ``CODEX_API_KEY`` exits 1 (not logged in), and every call needs ``CODEX_HOME``.

* ``--version`` prints ``codex-cli <version>`` (scenario ``version``, default 0.162.1).
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
  negative control, and its ``observe.*`` line run for real; every other line reports a denial
  without running: exit 1, 6 for the ``--noproxy`` curl, 3 for the listen probe), ``hole`` (every
  line that stays inside the test's directories and listeners runs for real, unsandboxed; the ones
  that would reach the keychain, gh, launchd, Apple Events, LaunchServices or the internet report
  exit 0 without running) or ``forge`` (a denial is echoed instead of running the script).
  Model commands get only codex's core variables when the argv holds
  ``shell_environment_policy.inherit=core`` (`CORE_VARIABLES`, from the source at the tag), unless
  ``env_leak`` passes them everything. ``wrap`` reports each command as ``bash -lc
  '<command>'``; ``patch`` adds a ``file_change`` item; ``skip`` (indices) leaves commands out;
  ``error_item`` adds codex's own warning item (``{"type": "error", "message": ...}``) and
  ``turn_failed`` ends the turn with ``turn.failed`` and that message instead of ``turn.completed``;
  ``tamper`` appends a line to every probe script it ran; ``listener_leak`` runs the local-listener
  probe for real but reports a denial (exit 1).
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
DENIED_RC = {"network.external-noproxy.script": 6, "network.listen.script": 3}
# Lines the hole scenario reports as allowed without running them: they would touch the real
# keychain, gh's login, launchd, Apple Events, LaunchServices or the internet.
SIMULATED = ("gh.", "git.", "keychain.", "launchd.", "apple-events.", "launch-services.", "network.external",
             "network.metadata", "network.dns", "network.udp", "network.daemon", "socket.extra-")
# What shell_environment_policy.inherit=core passes to a model command (the research, from the source).
CORE_VARIABLES = ("PATH", "SHELL", "TMPDIR", "TEMP", "TMP", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "USER")
EPERM = "sandbox-exec: sandbox_apply: Operation not permitted\n"
# What `codex exec` is verified to accept (codex exec --help, codex features list and the config
# keys in the source at rust-v0.162.1, as used by src/schedules/codex_sandbox.py).
EXEC_FLAGS = {"--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--json"}
EXEC_OPTIONS = {"-s", "-C", "--output-schema", "-o", "-m", "--disable", "-c"}
SANDBOX_MODES = {"read-only", "workspace-write"}
FEATURES = {"hooks", "plugins", "remote_plugin", "apps"}
CONFIG_KEYS = {"project_doc_max_bytes", "project_root_markers", "analytics.enabled", "web_search",
               "skills.bundled.enabled", "skills.include_instructions", "shell_environment_policy.inherit",
               "sandbox_workspace_write.exclude_slash_tmp", "model_reasoning_effort"}
SANDBOX_CONFIG = {'sandbox_mode="workspace-write"', 'sandbox_mode="read-only"'}


def refuse(argument):
    sys.stderr.write(f"error: unexpected argument '{argument}' found\n")
    return 2


def exec_problem(args):
    """The first argument of ``exec`` outside the verified set, or None."""
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "-" and index == len(args) - 1:
            return None
        if argument in EXEC_FLAGS:
            index += 1
            continue
        if argument not in EXEC_OPTIONS or index + 1 >= len(args):
            return argument
        value = args[index + 1]
        if (argument == "-s" and value not in SANDBOX_MODES) or (argument == "--disable" and value not in FEATURES) \
                or (argument == "-c" and value.split("=", 1)[0] not in CONFIG_KEYS):
            return f"{argument} {value}"
        index += 2
    return "<no prompt '-'>"


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def seen():
    if SCENARIO.get("seen"):
        with open(SCENARIO["seen"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"argv": ARGS, "env": dict(os.environ), "cwd": os.getcwd()}) + "\n")


def model_environment():
    """The environment a model command gets: the core variables under inherit=core."""
    if "shell_environment_policy.inherit=core" in ARGS and not SCENARIO.get("env_leak"):
        return {name: value for name, value in os.environ.items() if name in CORE_VARIABLES}
    return dict(os.environ)


def run_line(body):
    result = subprocess.run(["/bin/sh", "-c", body], capture_output=True, text=True, timeout=30,
                            env=model_environment())
    return result.stdout, result.returncode


def run_script(path, mode):
    output = []
    for line in Path(path).read_text().splitlines():
        match = LINE.match(line)
        if not match:
            continue
        check, body = match["id"], match["body"]
        if mode == "hole" and check.startswith(SIMULATED):
            code = 0
        elif mode == "hole" or check.startswith("observe.") or (
                check.startswith("control.") and "sandbox-loaded" not in check):
            text, code = run_line(body)
            output.append(text)
        elif SCENARIO.get("listener_leak") and check == "network.local-listener.script":
            run_line(body)
            code = 1
        else:
            code = DENIED_RC.get(check, 1)
        output.append(f"__probe__ {check} rc={code}\n")
    if SCENARIO.get("tamper"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("# changed during the session\n")
    return "".join(output), 0


def do_exec():
    if (problem := exec_problem(ARGS[1:])) is not None:
        return refuse(problem)
    if not os.environ.get("CODEX_API_KEY"):
        sys.stderr.write("Not logged in\n")
        return 1
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
    if SCENARIO.get("error_item"):
        emit({"type": "item.completed", "item": {"id": "item_warn", "type": "error", "message": SCENARIO["error_item"]}})
    emit({"type": "item.completed", "item": {"id": "item_done", "type": "agent_message", "text": "DONE"}})
    if SCENARIO.get("turn_failed"):
        emit({"type": "turn.failed", "error": {"message": SCENARIO["turn_failed"]}})
        return 1
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
                                              "reasoning_output_tokens": 0}})
    output_file = ARGS[ARGS.index("-o") + 1] if "-o" in ARGS else None
    if output_file:
        Path(output_file).write_text("DONE\n")
    return 0


def do_sandbox():
    if "--" not in ARGS:
        return refuse("<no -- before the command>")
    options = ARGS[1:ARGS.index("--")]
    if options not in ([], *(["-c", value] for value in SANDBOX_CONFIG)):
        return refuse(" ".join(options))
    command = ARGS[ARGS.index("--") + 1:]
    mode = "workspace-write" if 'sandbox_mode="workspace-write"' in options else "read-only"
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
    if not os.environ.get("CODEX_HOME"):
        sys.stderr.write("fake codex: CODEX_HOME is not set (the real one would use ~/.codex)\n")
        return 3
    if ARGS == ["--version"]:
        print(f"codex-cli {SCENARIO.get('version', '0.162.1')}")
        return 0
    if ARGS[:1] == ["sandbox"]:
        return do_sandbox()
    if ARGS[:1] == ["exec"]:
        return do_exec()
    return refuse(" ".join(ARGS[:2]))


sys.exit(main())
