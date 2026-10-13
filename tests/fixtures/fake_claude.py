"""A fake ``claude`` for the sandbox tests (#343): ``auth status --json`` and a scripted ``-p`` probe session.

Usage: ``python fake_claude.py <scenario.json> <claude arguments...>``; the tests wrap it in an
executable script. It never touches the network, the keychain, launchd or anything outside the
paths the session names.

``-p`` reads the probe prompt from standard input, writes ``{"argv", "env", "cwd"}`` to the
scenario's ``seen`` file and answers in ``stream-json``: a ``system/init`` event, one tool call per
listed call and its result, and a ``result`` event. By default it behaves like a working sandbox:

* Bash sub-probes (``<body>; echo __probe__ <id> rc=$?``): a ``control.*`` (except
  ``control.sandbox-loaded``) or ``observe.*`` probe runs its body for real (only ``printf``,
  ``cat``, ``ls``, ``ln``, ``: >>`` and the observe line; ``nc`` is emulated with a Python socket);
  every other probe is denied (exit code 1) without running. ``allow`` and ``deny`` (lists of probe
  IDs) change that; an allowed probe whose body is none of the above (``security``, ``curl``,
  ``launchctl``…) only reports exit code 0.
* File tools are allowed inside the working directories (the one-off repository and the
  ``--add-dir`` directories; writes only outside the state copy) on resolved paths, and refused
  otherwise. ``allow_paths`` and ``deny_paths`` (substrings) change that.

Other scenario keys: ``auth`` (the JSON ``auth status`` prints) and ``auth_code``; ``init`` (fields
merged into the init event); ``skip`` (indices of calls never made); ``extra_call`` (one more tool
call); ``say`` (assistant text, for leak tests); ``garbage`` (a non-JSON line); ``oversize``
(characters of filler); ``denial_events`` (refusals also as ``permission_denied`` events);
``leak_oauth_env`` (Bash sees ``CLAUDE_CODE_OAUTH_TOKEN``, as if the credentials rule failed);
``sleep`` (seconds before answering, for the timeout test).
"""
import fnmatch
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

SCENARIO = json.loads(Path(sys.argv[1]).read_text())
ARGS = sys.argv[2:]
LINE = re.compile(r"^(?P<body>.*); echo __probe__ (?P<id>\S+) rc=\$\?$")


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def flag(name):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else None


def add_dirs():
    if "--add-dir" not in ARGS:
        return []
    found = []
    for value in ARGS[ARGS.index("--add-dir") + 1:]:
        if value.startswith("--"):
            break
        found.append(value)
    return found


def real(path):
    return os.path.realpath(path)


def inside(path, roots):
    path = real(path)
    return any(path == real(root) or path.startswith(real(root).rstrip("/") + "/") for root in roots)


def file_allowed(path, write):
    for text in SCENARIO.get("deny_paths", []):
        if text in path:
            return False
    for text in SCENARIO.get("allow_paths", []):
        if text in path:
            return True
    roots = [os.getcwd(), *add_dirs()]
    if write:
        state = add_dirs()[1:]  # the state copy: an Edit deny rule
        if os.path.islink(path) or inside(path, state):
            return False
    return inside(path, roots)


def run_file_tool(name, tool_input):
    path = tool_input.get("file_path") or tool_input.get("path")
    write = name in ("Write", "Edit")
    if not file_allowed(path, write):
        return "Permission to use this tool was denied for this path.", True
    try:
        if name == "Read":
            return "".join(f"{n}\t{line}" for n, line in enumerate(Path(path).read_text().splitlines(True), 1)), False
        if name == "Write":
            Path(path).write_text(tool_input["content"])
            return "File created successfully", False
        if name == "Edit":
            text = Path(path).read_text()
            if tool_input["old_string"] not in text:
                return "String to replace not found", True
            Path(path).write_text(text.replace(tool_input["old_string"], tool_input["new_string"], 1))
            return "The file has been updated", False
        if name == "Grep":
            hits = []
            target = Path(path)
            for file in ([target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())):
                try:
                    if re.search(tool_input["pattern"], file.read_text()):
                        hits.append(str(file))
                except (OSError, UnicodeDecodeError):
                    pass
            return ("Found %d files\n" % len(hits) + "\n".join(hits)) if hits else "No files found", False
        if name == "Glob":
            names = sorted(str(p) for p in Path(path).iterdir() if fnmatch.fnmatch(p.name, tool_input["pattern"]))
            return "\n".join(names) or "No files found", False
    except OSError as error:
        return f"{type(error).__name__}", True
    return "unknown tool", True


def sub_allowed(probe_id):
    if probe_id in SCENARIO.get("deny", []):
        return False
    if probe_id in SCENARIO.get("allow", []):
        return True
    return (probe_id.startswith("control.") and probe_id != "control.sandbox-loaded.bash") or \
        probe_id.startswith("observe.")


def run_body(body, env):
    if body.startswith("/usr/bin/nc "):
        parts = body.split()
        try:
            if "-U" in parts:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(parts[parts.index("-U") + 3].strip("'"))
            else:
                sock = socket.create_connection(("127.0.0.1", int(parts[5])), timeout=2)
            sock.close()
            return 0, ""
        except OSError:
            return 1, ""
    if not body.startswith(("printf", "cat ", "ls ", "ln ", ": >>")):
        return 0, ""  # simulated: never run security, curl, launchctl, osascript, open, gh
    done = subprocess.run(["/bin/bash", "-c", body], capture_output=True, text=True, env=env, check=False)
    return done.returncode, done.stdout


def run_bash(tool_input):
    env = dict(os.environ)
    if not SCENARIO.get("leak_oauth_env"):  # credentials.envVars denies it to Bash
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env["TMPDIR"] = os.path.join(os.environ["CLAUDE_CODE_TMPDIR"], f"claude-{os.getuid()}")
    os.makedirs(env["TMPDIR"], exist_ok=True)
    output = []
    for line in tool_input["command"].split("\n"):
        match = LINE.match(line)
        if not match:
            return "unparsed probe line", True
        probe_id, body = match["id"], match["body"]
        if sub_allowed(probe_id):
            code, out = run_body(body, env)
        else:
            code, out = 1, "Operation not permitted\n"
        output.append(f"{out}__probe__ {probe_id} rc={code}")
    return "\n".join(output), False


def session():
    text = sys.stdin.read()
    time.sleep(SCENARIO.get("sleep", 0))
    calls = [json.loads(line) for line in text.split("\n") if line.startswith('{"tool"')]
    if SCENARIO.get("seen"):
        Path(SCENARIO["seen"]).write_text(json.dumps({"argv": ARGS, "env": dict(os.environ), "cwd": os.getcwd(),
                                                      "calls": len(calls)}))
    init = {"type": "system", "subtype": "init", "session_id": "s", "tools": flag("--tools").split(","),
            "mcp_servers": [], "permissionMode": flag("--permission-mode"), "plugins": []}
    init.update(SCENARIO.get("init", {}))
    emit(init)
    if SCENARIO.get("garbage"):
        sys.stdout.write("this is not json\n")
    if SCENARIO.get("say"):
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": SCENARIO["say"]}]}})
    if SCENARIO.get("oversize"):
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * SCENARIO["oversize"]}]}})
    if SCENARIO.get("extra_call"):
        calls = calls + [SCENARIO["extra_call"]]
    denials = []
    for index, call in enumerate(calls):
        if index in SCENARIO.get("skip", []):
            continue
        use_id = f"toolu_{index:04d}"
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": use_id, "name": call["tool"],
                                                            "input": call["input"]}]}})
        if call["tool"] == "Bash":
            result, error = run_bash(call["input"])
        else:
            result, error = run_file_tool(call["tool"], call["input"])
        if error and SCENARIO.get("denial_events"):
            emit({"type": "system", "subtype": "permission_denied", "tool_use_id": use_id})
            denials.append({"tool_name": call["tool"], "tool_use_id": use_id})
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": use_id,
                                                       "content": result, "is_error": error}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "permission_denials": denials})
    return 0


def main():
    if ARGS[:3] == ["auth", "status", "--json"]:
        print(json.dumps(SCENARIO.get("auth", {"loggedIn": True, "authMethod": "claude.ai",
                                                "email": "owner@example.com"})))
        return SCENARIO.get("auth_code", 0)
    if "-p" in ARGS:
        return session()
    return 2


if __name__ == "__main__":
    sys.exit(main())
