"""A fake ``claude`` for the sandbox tests (#343): ``auth status --json`` and a scripted ``-p`` probe session.

Usage: ``python fake_claude.py <scenario.json> <claude arguments...>``; the tests wrap it in an
executable script. It never touches the network, the keychain, launchd or anything outside the
paths the session and the scenario name.

``-p`` reads the probe prompt from standard input, writes ``{"argv", "env", "cwd"}`` to the
scenario's ``seen`` file and answers in ``stream-json``: a ``system/init`` event, one tool call per
listed call and its result, and a ``result`` event. By default it behaves like a working sandbox:

* Bash sub-probes (``<body>; echo __probe__ <id> rc=$?``), inline or in a runner-written script run
  as ``/bin/bash <script>``: a ``control.*`` probe (except the ``sandbox-loaded`` negative
  controls) or an ``observe.*`` probe runs its body for real (only ``printf``, ``cat``, ``ls``,
  ``ln``, ``mkdir``, ``: >>`` and the observe line; ``nc`` is emulated with a Python socket); every
  other probe is denied without running, with exit code 1 (6 for the ``--noproxy`` curl, 3 for
  the listen probe, as a sandbox gives them). ``allow`` and ``deny`` (lists of probe IDs) change
  that, ``rc`` (ID -> code) sets a denied probe's exit code; an allowed probe whose body is none
  of the above (``security``, ``curl``, ``launchctl``…) only reports exit code 0.
  ``tamper_scripts`` (substrings of a script path) rewrites such a script before running it, so
  that every probe in it prints exit code 1 (a forged denial).
* Bash gets ``TMPDIR=$CLAUDE_CODE_TMPDIR/claude-<uid>`` unless that path is longer than 44 bytes;
  then, like the CLI, the short fallback ``/tmp/claude-<uid>``, which the scenario must redirect
  with ``fallback_tmpdir`` (the fake refuses to touch the real one). Its environment holds only
  the session allowlist plus ``CLAUDECODE``, ``CLAUDE_CODE_CHILD_SESSION`` and ``CLAUDE_PID``
  (the documented names the CLI sets for Bash).
* File tools, in the CLI's order as recalled (not verified): the input is validated first, so
  an Edit, or a Write over an existing file, without a successful Read of that file, an Edit
  whose old string is missing, and a path among ``error_paths`` (substrings) fail with an error
  that is not a denial, whatever the permissions say; then the permission check: allowed inside
  the working directories (the one-off repository and the ``--add-dir`` directories; writes only
  outside the state copy and outside any ``.git`` or ``.claude`` directory and ``.mcp.json``,
  which the CLI protects) on resolved paths, and refused with a permission denial otherwise.
  ``allow_paths`` and ``deny_paths`` (substrings of the given path) change that.
* Every permission refusal is a ``permission_denied`` system event and an entry of the result's
  ``permission_denials`` (``no_denial_events`` drops both).

Other scenario keys: ``auth`` (the JSON ``auth status`` prints) and ``auth_code``; ``init`` (fields
merged into the init event); ``skip`` (indices of calls never made); ``extra_call`` (one more tool
call); ``extra_calls`` (several); ``forge`` (``[tool, substring]`` rules: those calls are sent with an extra invalid ``pages``
parameter, which the CLI rejects with an input error, no denial); ``allow_calls`` and
``deny_calls`` (``[tool, substring]`` rules for single file tool calls); ``drop_bypass`` (the
bypass flag is left out);
``honour_bypass`` (a call with the bypass flag runs every sub-probe for real, unsandboxed);
``refuse_inline`` (the permission layer refuses every inline Bash call except the controls);
``refuse_scripts`` (it refuses every script call); ``edit_first`` (an Edit is made before the
Read of its file); ``say`` (assistant text, for leak tests); ``garbage`` (a non-JSON line);
``oversize`` (characters of filler); ``leak_oauth_env`` (Bash sees ``CLAUDE_CODE_OAUTH_TOKEN``);
``extra_env`` (more variables Bash sees); ``extra_fds`` (descriptors the observe probe inherits);
``spawn_child`` (a file: a ``sleep`` child is started in the session's process group and its PID
written there); ``sleep`` (seconds before answering, for the timeout test).

**Worker mode (#345).** ``-p`` with ``--output-format json`` is a worker: it reads the
worker_context from standard input, appends ``{"argv", "env", "cwd", "stdin"}`` to the ``seen``
file (JSON lines), parses ``--json-schema`` (inline JSON, as the CLI does), makes the file tool
calls of ``calls`` (with ``stdin_calls`` also each standard-input line that starts with
``{"tool"``, as the worker check asks for one) with the permission rules above (a denial goes
into ``permission_denials`` with its input) and prints one ``result`` object whose
``structured_output`` is `fake_worker.build` of the outcome for this attempt: ``outcomes`` (a
list, one per start, counted in the ``counter`` file; the last repeats) or ``outcome``. An outcome
may also set ``subtype``, ``is_error``, ``exit``, ``no_output`` (no ``structured_output``),
``structured`` (that object as the ``structured_output``), ``echo_reads`` (what the calls read
becomes the summary and the result text), ``probe_answer`` (the worker check's answer: the
``structured_output`` is ``{"verdict": "ok"}`` with each ``autodev-worker-probe-<tree|materials>-…``
line the calls read under its kind) and ``pad`` (characters of filler in the result object, for
the output cap); ``sleep`` delays the answer.

**Stage mode (#347).** ``-p`` with ``stream-json`` whose prompt is a stage prompt (it starts with
``You are the <kind> stage of an autodev run``) is a stage session; the mode is picked from the
prompt, not the flags, because the probe session of a stage has the same argv. It appends
``{"kind", "argv", "env", "cwd", "prompt", "values", "state_copy"}`` to the scenario's
``stage_seen`` file (JSON lines; ``values`` are the placeholders read back from the prompt with
the template of `src.schedules.stages`, ``state_copy`` lists the run's state copy with each
file's mode and, for ``state.json``, its content). ``stages`` (kind -> ``{"script", "output",
"no_output", "sleep", "say", "pad"}``) drives it (``sleep`` waits after the script; ``pad`` emits that
many characters of filler text events before ``say``, for a stream longer than a capture): each
``script`` line is run with ``/bin/bash -c`` in the
working directory and the session's environment after ``{name}`` (a prompt placeholder, e.g.
``{worker_cmd}`` or ``{exchange_dir}``) and ``{head}`` (``git rev-parse HEAD`` at that line) are
filled in, and is reported as a Bash tool call with its output; then a ``result`` event carries
``output`` as ``structured_output`` (``{head}`` and placeholders filled in its strings) and a
``total_cost_usd``. The exit codes of the lines go to ``stage_seen`` as ``codes``.
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

import fake_worker

SCENARIO = json.loads(Path(sys.argv[1]).read_text())
ARGS = sys.argv[2:]
LINE = re.compile(r"^(?P<body>.*); echo __probe__ (?P<id>\S+) rc=\$\?$")
SCRIPT = re.compile(r"^/bin/bash (?P<path>\S+)$")
TMP_FALLBACK_LIMIT = 44  # the CLI's threshold for "<CLAUDE_CODE_TMPDIR>/claude-<uid>" (review of 2.1.294)
DENIED_RC = {"network.external-noproxy": 6, "network.listen": 3}
SESSION_NAMES = ("PATH", "HOME", "LANG", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_TMPDIR", "TMPDIR", "GH_CONFIG_DIR",
                 "ENABLE_CLAUDEAI_MCP_SERVERS", "DISABLE_AUTOUPDATER")
READ = set()  # resolved paths the session read successfully


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


PROTECTED_DIRS = (".git", ".claude")  # written only with approval, so refused in dontAsk (recalled)
PROTECTED_FILES = (".mcp.json",)


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
        parts = Path(path).parts
        if os.path.islink(path) or inside(path, state) or any(part in PROTECTED_DIRS for part in parts) \
                or parts[-1] in PROTECTED_FILES:
            return False
    return inside(path, roots)


def invalid_input(name, tool_input, path):
    """The input check that runs before the permission check: an error message, or None."""
    if any(text in path for text in SCENARIO.get("error_paths", [])):
        return "Error: the tool failed"
    if name == "Write" and os.path.exists(path) and real(path) not in READ:
        return "Error: File has not been read yet. Read it first before writing to it."
    if name == "Edit":
        if real(path) not in READ:
            return "Error: File has not been read yet. Read it first before writing to it."
        try:
            if tool_input["old_string"] not in Path(path).read_text():
                return "String to replace not found"
        except OSError as error:
            return f"{type(error).__name__}"
    return None


def picked(rules, name, tool_input):
    """Whether a ``[tool, substring]`` rule names this call (the substring is searched in its input)."""
    text = json.dumps(tool_input)
    return any(tool == name and part in text for tool, part in rules)


def run_file_tool(name, tool_input):
    """(result text, is_error, denied by the permission layer)."""
    if name not in ("Read", "Write", "Edit", "Grep", "Glob"):
        return f"Error: No such tool available: {name}", True, False
    path = tool_input.get("file_path") or tool_input.get("path")
    write = name in ("Write", "Edit")
    if (problem := invalid_input(name, tool_input, path)) is not None:
        return problem, True, False
    if picked(SCENARIO.get("deny_calls", []), name, tool_input):
        return "Permission to use this tool was denied for this path.", True, True
    if not picked(SCENARIO.get("allow_calls", []), name, tool_input) and not file_allowed(path, write):
        return "Permission to use this tool was denied for this path.", True, True
    try:
        if name == "Read":
            text = Path(path).read_text()
            READ.add(real(path))
            return "".join(f"{n}\t{line}" for n, line in enumerate(text.splitlines(True), 1)), False, False
        if name == "Write":  # creates missing parent directories, as the CLI's Write does (recalled)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(tool_input["content"])
            return "File created successfully", False, False
        if name == "Edit":
            text = Path(path).read_text()
            Path(path).write_text(text.replace(tool_input["old_string"], tool_input["new_string"], 1))
            return "The file has been updated", False, False
        if name == "Grep":
            hits = []
            target = Path(path)
            for file in ([target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())):
                try:
                    if re.search(tool_input["pattern"], file.read_text()):
                        hits.append(str(file))
                except (OSError, UnicodeDecodeError):
                    pass
            return ("Found %d files\n" % len(hits) + "\n".join(hits)) if hits else "No files found", False, False
        if name == "Glob":
            names = sorted(str(p) for p in Path(path).iterdir() if fnmatch.fnmatch(p.name, tool_input["pattern"]))
            return "\n".join(names) or "No files found", False, False
    except OSError as error:
        return f"{type(error).__name__}", True, False
    return "unknown tool", True, False


def sub_allowed(probe_id, unsandboxed):
    if unsandboxed or probe_id in SCENARIO.get("allow", []):
        return probe_id not in SCENARIO.get("deny", [])
    if probe_id in SCENARIO.get("deny", []):
        return False
    return (probe_id.startswith("control.") and not probe_id.startswith("control.sandbox-loaded.")) or \
        probe_id.startswith("observe.")


def denied_rc(probe_id):
    if probe_id in SCENARIO.get("rc", {}):
        return SCENARIO["rc"][probe_id]
    return DENIED_RC.get(probe_id.rsplit(".", 1)[0], 1)


def run_body(body, env, probe_id):
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
    if not body.startswith(("printf", "cat ", "ls ", "ln ", "mkdir ", ": >>")):
        return 0, ""  # simulated: never run security, curl, launchctl, osascript, open, gh, git, dig
    fds = []
    if probe_id.startswith("observe.") and SCENARIO.get("extra_fds"):
        fds = [os.open(os.devnull, os.O_RDONLY) for _ in range(SCENARIO["extra_fds"])]
    try:
        done = subprocess.run(["/bin/bash", "-c", body], capture_output=True, text=True, env=env, check=False,
                              pass_fds=fds)
    finally:
        for fd in fds:
            os.close(fd)
    return done.returncode, done.stdout


def bash_env():
    env = {name: value for name, value in os.environ.items()
           if name in SESSION_NAMES or name.startswith("AUTODEV_")}
    if SCENARIO.get("leak_oauth_env") and "CLAUDE_CODE_OAUTH_TOKEN" in os.environ:  # credentials.envVars denies it
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    env.update({"CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_PID": str(os.getpid())})
    env.update(SCENARIO.get("extra_env", {}))
    temp = os.path.join(os.environ["CLAUDE_CODE_TMPDIR"], f"claude-{os.getuid()}")
    if len(temp.encode()) > TMP_FALLBACK_LIMIT:  # the CLI's short fallback, here redirected to a test directory
        if not SCENARIO.get("fallback_tmpdir"):
            sys.exit(3)
        temp = SCENARIO["fallback_tmpdir"]
    env["TMPDIR"] = temp
    os.makedirs(temp, exist_ok=True)
    return env


def run_bash(tool_input):
    """(result text, is_error, denied by the permission layer)."""
    command = tool_input["command"]
    script = SCRIPT.match(command)
    first = LINE.match(command.split("\n")[0])
    if script and SCENARIO.get("refuse_scripts"):
        return "Permission to use Bash has been denied.", True, True
    if not script and SCENARIO.get("refuse_inline") and first and not first["id"].startswith("control."):
        return "Permission to use Bash has been denied.", True, True
    if script and any(text in script["path"] for text in SCENARIO.get("tamper_scripts", [])):
        ids = [match["id"] for match in map(LINE.match, Path(script["path"]).read_text().splitlines()) if match]
        Path(script["path"]).write_text("".join(f"echo __probe__ {probe_id} rc=1\n" for probe_id in ids))
        return "\n".join(f"__probe__ {probe_id} rc=1" for probe_id in ids), False, False
    lines = Path(script["path"]).read_text().splitlines() if script else command.split("\n")
    unsandboxed = bool(tool_input.get("dangerouslyDisableSandbox")) and SCENARIO.get("honour_bypass")
    env = bash_env()
    output = []
    for line in lines:
        if not line.strip():
            continue
        match = LINE.match(line)
        if not match:
            return "unparsed probe line", True, False
        probe_id, body = match["id"], match["body"]
        if sub_allowed(probe_id, unsandboxed):
            code, out = run_body(body, env, probe_id)
        else:
            code, out = denied_rc(probe_id), "Operation not permitted\n"
        output.append(f"{out}__probe__ {probe_id} rc={code}")
    return "\n".join(output), False, False


def spawn_child():
    child = subprocess.Popen(["/bin/sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    Path(SCENARIO["spawn_child"]).write_text(str(child.pid))


def worker():
    """One worker attempt: a single JSON result (``--output-format json`` with ``--json-schema``)."""
    text = sys.stdin.read()
    attempt = 1
    if SCENARIO.get("counter"):
        counter = Path(SCENARIO["counter"])
        attempt = (int(counter.read_text()) if counter.exists() else 0) + 1
        counter.write_text(str(attempt))
    if SCENARIO.get("seen"):
        with open(SCENARIO["seen"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"argv": ARGS, "env": dict(os.environ), "cwd": os.getcwd(), "stdin": text}) + "\n")
    time.sleep(SCENARIO.get("sleep", 0))
    schema = json.loads(flag("--json-schema"))
    if not isinstance(schema, dict):
        sys.stderr.write("Error: --json-schema must be a JSON object\n")
        return 1
    denials, reads = [], []
    calls = list(SCENARIO.get("calls", []))
    if SCENARIO.get("stdin_calls"):
        calls += [json.loads(line) for line in text.split("\n") if line.startswith('{"tool"')]
    for index, call in enumerate(calls):
        result, error, denied = run_file_tool(call["tool"], call["input"])
        if denied:
            denials.append({"tool_name": call["tool"], "tool_use_id": f"toolu_{index:04d}", "tool_input": call["input"]})
        elif not error:
            reads.append(result)
    outcomes = SCENARIO.get("outcomes") or [SCENARIO.get("outcome", {})]
    outcome = dict(outcomes[min(attempt, len(outcomes)) - 1])
    result_text = "done"
    if outcome.get("echo_reads") and reads:
        outcome["summary"] = result_text = "\n".join(reads)
    event = {"type": "result", "subtype": outcome.get("subtype", "success"), "is_error": outcome.get("is_error", False),
             "result": result_text, "permission_denials": denials}
    if outcome.get("pad"):
        event["padding"] = "x" * outcome["pad"]
    if outcome.get("probe_answer"):
        answer = {"verdict": "ok"}
        for text in reads:
            for match in re.finditer(r"autodev-worker-probe-(tree|materials)-[0-9a-f]+", text):
                answer[match[1]] = match[0]
        event["structured_output"] = answer
    elif "structured" in outcome:
        event["structured_output"] = outcome["structured"]
    elif not outcome.get("no_output"):
        event["structured_output"] = fake_worker.build(text, outcome)
    print(json.dumps(event))
    return outcome.get("exit", 0)


STAGE_PROMPT = re.compile(r"\AYou are the (select|implement|submit) stage of an autodev run")


def prompt_values(kind, text):
    """The placeholder values of a rendered stage prompt, read back with the kind's template."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the checkout; imported only here
    from src.schedules import stages as stage_templates

    template = stage_templates.TEMPLATES[kind]
    pattern, seen = "", set()
    for part in re.split(r"(\{[a-z_]+\})", template):
        name = part[1:-1] if part.startswith("{") and part.endswith("}") else None
        if name in stage_templates.PLACEHOLDERS:
            pattern += f"(?P={name})" if name in seen else f"(?P<{name}>.+?)"
            seen.add(name)
        else:
            pattern += re.escape(part)
    match = re.fullmatch(pattern, text, re.S)
    return match.groupdict() if match else {}


def state_copy_listing():
    roots = add_dirs()[1:]
    if not roots:
        return {}
    found = {}
    for path in sorted(Path(roots[0]).rglob("*")):
        if path.is_file():
            entry = {"mode": oct(path.stat().st_mode & 0o777)}
            if path.name == "state.json":
                entry["content"] = json.loads(path.read_text())
            found[str(path.relative_to(roots[0]))] = entry
    return found


def fill(text, values, head):
    names = "|".join(re.escape(name) for name in values)
    text = re.sub(r"\{(" + names + r")\}", lambda match: values[match[1]], text) if names else text
    return text.replace("{head}", head)


def fill_all(value, values, head):
    if isinstance(value, str):
        return fill(value, values, head)
    if isinstance(value, list):
        return [fill_all(item, values, head) for item in value]
    if isinstance(value, dict):
        return {key: fill_all(item, values, head) for key, item in value.items()}
    return value


def current_head():
    done = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def stage(kind, text):
    """One stage session: the scenario's script for ``kind``, then its structured output."""
    plan = (SCENARIO.get("stages") or {}).get(kind) or {}
    values = prompt_values(kind, text)
    seen = {"kind": kind, "argv": ARGS, "env": dict(os.environ), "cwd": os.getcwd(), "prompt": text,
            "values": values, "state_copy": state_copy_listing(), "codes": []}
    emit({"type": "system", "subtype": "init", "session_id": "s", "tools": flag("--tools").split(","),
          "mcp_servers": [], "permissionMode": flag("--permission-mode"), "plugins": [], "model": "claude-opus-test"})
    left, filler = int(plan.get("pad") or 0), "filler words of a long stage stream " * 2000
    while left > 0:
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": filler[:left]}]}})
        left -= len(filler)
    if plan.get("say"):
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": plan["say"]}]}})
    for index, line in enumerate(plan.get("script", [])):
        command = fill(line, values, current_head())
        use_id = f"toolu_s{index:04d}"
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": use_id, "name": "Bash",
                                                            "input": {"command": command}}]}})
        done = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, check=False)
        seen["codes"].append(done.returncode)
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": use_id,
                                                       "content": (done.stdout + done.stderr)[-4000:],
                                                       "is_error": done.returncode != 0}]}})
    if SCENARIO.get("stage_seen"):
        with open(SCENARIO["stage_seen"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps(seen) + "\n")
    time.sleep(plan.get("sleep", 0))  # after the script: a timeout test starts children first
    event = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
             "total_cost_usd": 0.25}
    if not plan.get("no_output"):
        event["structured_output"] = fill_all(plan.get("output", {}), values, current_head())
    emit(event)
    return 0


def session():
    if flag("--output-format") == "json":
        return worker()
    text = sys.stdin.read()
    if (match := STAGE_PROMPT.match(text)) is not None:
        return stage(match[1], text)
    time.sleep(SCENARIO.get("sleep", 0))
    calls = [json.loads(line) for line in text.split("\n") if line.startswith('{"tool"')]
    if SCENARIO.get("seen"):
        Path(SCENARIO["seen"]).write_text(json.dumps({"argv": ARGS, "env": dict(os.environ), "cwd": os.getcwd(),
                                                      "calls": len(calls)}))
    if SCENARIO.get("spawn_child"):
        spawn_child()
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
    calls = calls + SCENARIO.get("extra_calls", [])
    if SCENARIO.get("edit_first"):
        for index in range(len(calls) - 1):
            one, two = calls[index], calls[index + 1]
            if one["tool"] == "Read" and two["tool"] == "Edit" and \
                    one["input"]["file_path"] == two["input"]["file_path"]:
                calls[index], calls[index + 1] = two, one
    denials = []
    for index, call in enumerate(calls):
        if index in SCENARIO.get("skip", []):
            continue
        tool_input = dict(call["input"])
        if SCENARIO.get("drop_bypass"):
            tool_input.pop("dangerouslyDisableSandbox", None)
        use_id = f"toolu_{index:04d}"
        invalid = picked(SCENARIO.get("forge", []), call["tool"], tool_input)
        if invalid:
            tool_input["pages"] = "x"
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": use_id, "name": call["tool"],
                                                            "input": tool_input}]}})
        if invalid:  # the CLI rejects the input before any permission check
            result, error, denied = "InputValidationError: pages is not valid", True, False
        elif call["tool"] == "Bash":
            result, error, denied = run_bash(tool_input)
        else:
            result, error, denied = run_file_tool(call["tool"], tool_input)
        if denied and not SCENARIO.get("no_denial_events"):
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
