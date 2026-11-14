"""The thread inventory helper reads a Claude Code transcript, not the model's memory."""
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("thread_inventory", ROOT / "scripts" / "dev" / "thread_inventory.py")
thread_inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(thread_inventory)

SESSION = "11111111-2222-3333-4444-555555555555"
SCRATCH = f"/tmp/claude-501/-proj/{SESSION}/scratchpad"


def user(content, ts, **extra):
    return {"type": "user", "timestamp": ts, "sessionId": SESSION, "cwd": "/work/repo",
            "message": {"role": "user", "content": content}, **extra}


def human(content, ts, **extra):
    return user(content, ts, origin={"kind": "human"}, **extra)


def assistant(blocks, ts, message_id, output_tokens=10, model="claude-opus-5-5"):
    """One response split into several entries, each repeating the response's usage."""
    return [{"type": "assistant", "timestamp": ts, "sessionId": SESSION, "cwd": "/work/repo",
             "message": {"id": message_id, "role": "assistant", "model": model, "content": [block],
                         "usage": {"input_tokens": 1, "output_tokens": output_tokens,
                                   "cache_read_input_tokens": 100}}}
            for block in blocks]


def tool(name, data, tool_id):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": data}


def result(tool_id, text, ts):
    return {"type": "user", "timestamp": ts, "sessionId": SESSION, "cwd": "/work/repo",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                     "content": text}]}}


def bash(command, tool_id, ts="2026-10-02T09:00:00Z", message_id=None, **extra):
    return assistant([tool("Bash", {"command": command, **extra}, tool_id)], ts, message_id or f"m_{tool_id}")


@pytest.fixture
def entries():
    return [
        {"type": "bridge-session", "sessionId": SESSION, "bridgeSessionId": "cse_ABC"},
        user("<command-name>/clear</command-name>", "2026-10-02T08:00:00Z"),
        human("Сделай флоу закрытия треда", "2026-10-02T08:00:01Z"),
        user("internal", "2026-10-02T08:00:02Z", isMeta=True),
        *assistant([{"type": "thinking", "thinking": "..."},
                    tool("Bash", {"command": "git status --short && cd ../other && git commit -m x && "
                                             "gh pr create --title t --body b"}, "t1")],
                   "2026-10-02T08:01:00Z", "msg_1", output_tokens=40),
        result("t1", "https://github.com/Owner/Repo/pull/7\nsee also https://github.com/Owner/Repo/issues/3",
               "2026-10-02T08:01:05Z"),
        {"type": "pr-link", "sessionId": SESSION, "prNumber": 7, "prUrl": "https://github.com/Owner/Repo/pull/7",
         "prRepository": "Owner/Repo", "timestamp": "2026-10-02T08:01:06Z"},
        *assistant([tool("Write", {"file_path": f"{SCRATCH}/plan.md", "content": "x"}, "t2"),
                    tool("Edit", {"file_path": "/home/u/.claude/projects/p/memory/fact.md",
                                  "old_string": "a", "new_string": "b"}, "t3"),
                    tool("Edit", {"file_path": "/work/repo/src/memory/history.py",
                                  "old_string": "a", "new_string": "b"}, "t3b"),
                    tool("RemoteTrigger", {"action": "list"}, "t4"),
                    tool("RemoteTrigger", {"action": "update", "trigger_id": "trig_1", "body": {"secret": "x"}}, "t5"),
                    tool("mcp__claude_ai_ClickUp__clickup_create_task", {"name": "Patient data", "list_id": "9"}, "t6"),
                    tool("mcp__claude_ai_ClickUp__clickup_get_task", {"task_id": "1"}, "t7")],
                   "2026-10-02T08:02:00Z", "msg_2", output_tokens=25),
        *assistant([tool("Bash", {"command": "sleep 60", "run_in_background": True,
                                  "description": "wait for review"}, "t8"),
                    tool("Bash", {"command": "sleep 99", "run_in_background": True,
                                  "description": "never finishes"}, "t9")],
                   "2026-10-02T08:03:00Z", "msg_3", output_tokens=5),
        result("t8", "Command running in background with ID: bg1. Output is being written to: x", "2026-10-02T08:03:01Z"),
        result("t9", "Command running in background with ID: bg2. Output is being written to: y", "2026-10-02T08:03:02Z"),
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-02T08:05:00Z",
         "sessionId": SESSION,
         "content": "<task-notification> <task-id>bg1</task-id> <status>completed</status> </task-notification>"},
        *assistant([{"type": "text", "text": "Предыдущий подсчёт был неверным: я суммировал повторы."},
                    {"type": "text", "text": "My mistake: the branch was not pushed."}],
                   "2026-10-02T08:06:00Z", "msg_4", output_tokens=7),
        {"type": "ai-title", "aiTitle": "Thread close", "sessionId": SESSION},
    ]


def test_inventory_counts_each_response_once(entries):
    inv = thread_inventory.inventory(entries)
    assert inv["responses"] == 4
    assert inv["models"] == {"claude-opus-5-5": 4}
    assert inv["tokens"]["output_tokens"] == 40 + 25 + 5 + 7
    assert inv["tokens"]["cache_read_input_tokens"] == 400


def test_inventory_lists_prompts_refs_files_and_writes(entries):
    inv = thread_inventory.inventory(entries)
    assert inv["prompts"] == [{"ts": "2026-10-02T08:00:01Z"}]
    assert inv["title"] == "Thread close" and inv["bridge_sessions"] == ["cse_ABC"]
    refs = [(r["kind"], r["number"]) for r in inv["github_refs"]]
    assert ("issues", 3) in refs and ("pull", 7) in refs
    paths = [f["path"] for f in inv["files_written"]]
    assert f"{SCRATCH}/plan.md" in paths and "/work/repo/src/memory/history.py" in paths
    assert [m["path"] for m in inv["memory_writes"]] == ["/home/u/.claude/projects/p/memory/fact.md"]
    assert f"{SCRATCH}/plan.md" in inv["scratchpad_paths"]
    assert [m["command"] for m in inv["git_mutations"]] == ["git commit -m x", "gh pr create --title t --body b"]
    writes = {w["tool"]: w["summary"] for w in inv["external_writes"]}
    assert writes["RemoteTrigger"] == "update trigger_id=trig_1"  # the body is never copied
    assert writes["mcp__claude_ai_ClickUp__clickup_create_task"] == "list_id=9 name=Patient data"
    assert "mcp__claude_ai_ClickUp__clickup_get_task" not in writes
    assert "/work/other" in inv["directories"]  # a relative cd is resolved against the entry's cwd


def test_inventory_tracks_background_tasks_and_corrections(entries):
    inv = thread_inventory.inventory(entries)
    tasks = {task["id"]: task for task in inv["background_tasks"]}
    assert tasks["bg1"]["ended"]["status"] == "completed"
    assert tasks["bg2"]["ended"] is None and tasks["bg2"]["description"] == "never finishes"
    snippets = " ".join(c["snippet"] for c in inv["corrections"])
    assert "неверным" in snippets and "My mistake" in snippets


def test_monitor_async_agent_workflow_and_stop_are_tracked():
    entries = [
        *assistant([tool("Monitor", {"description": "watch CI", "command": "x"}, "m1"),
                    tool("Agent", {"description": "review", "prompt": "p"}, "a1"),
                    tool("Workflow", {"script": "x"}, "w1"),
                    tool("TaskStop", {"task_id": "brmon1"}, "s1")], "2026-10-02T09:00:00Z", "msg"),
        result("m1", "Monitor started (task brmon1, expires in 30m unless the source ends first)", "2026-10-02T09:00:01Z"),
        result("a1", "Async agent launched successfully.\nagentId: aagent1 (internal ID)", "2026-10-02T09:00:02Z"),
        result("w1", "Workflow launched in background. Task ID: wflow1\nSummary: x", "2026-10-02T09:00:03Z"),
        result("s1", "Successfully stopped task: brmon1 (command)", "2026-10-02T09:00:04Z"),
        user("<task-notification> <task-id>aagent1</task-id> <task-id>wflow1</task-id> <status>killed</status>"
             "</task-notification>", "2026-10-02T09:00:05Z", origin={"kind": "task-notification"}),
    ]
    tasks = {t["id"]: t for t in thread_inventory.inventory(entries)["background_tasks"]}
    assert tasks["brmon1"]["tool"] == "Monitor" and tasks["brmon1"]["ended"]["status"] == "stopped"
    assert tasks["aagent1"]["tool"] == "Agent" and tasks["aagent1"]["ended"]["status"] == "killed"
    assert tasks["wflow1"]["ended"]["status"] == "killed"  # every id of a block gets its status


def test_task_markers_count_only_in_results_of_the_tools_that_start_or_stop_tasks():
    entries = [
        *bash("sleep 600", "b1", run_in_background=True),
        result("b1", "Command running in background with ID: real1. Output is being written to: x", "t1"),
        *bash("python3 slow.py", "b2"),
        result("b2", "Command did not complete within its 120s timeout and was moved to the background "
                     "(ID: slow1). Output is being written to: y", "t2"),
        *assistant([tool("Read", {"file_path": "/w/old.log"}, "r1")], "2026-10-02T09:00:01Z", "mr"),
        result("r1", "Successfully stopped task: real1 (command)\nCommand running in background with ID: fake1", "t3"),
        *bash("cat old.log", "b3"),
        result("b3", "Monitor started (task fake2, expires in 30m)", "t4"),
    ]
    tasks = {t["id"]: t for t in thread_inventory.inventory(entries)["background_tasks"]}
    assert sorted(tasks) == ["real1", "slow1"]
    assert tasks["real1"]["ended"] is None  # a log that mentions a stop stops nothing


def test_prompt_sources():
    entries = [
        user("This session is being continued from a previous conversation...", "t1", isCompactSummary=True),
        human([{"type": "image", "source": {}}, {"type": "text", "text": "what is on the screenshot?"}], "t2"),
        human("<pasted_content id=x>log text</pasted_content> explain", "t3"),
        user("<task-notification> <task-id>z</task-id> <status>completed</status></task-notification>", "t4",
             origin={"kind": "task-notification"}),
        user("message from a peer agent", "t5", origin={"kind": "peer"}),
        user("<bash-input>cd /work/repo && git push origin eval/x</bash-input>", "t6"),
        human("close https://github.com/Owner/Repo/issues/62 please", "t7"),
    ]
    inv = thread_inventory.inventory(entries)
    assert inv["prompts"] == [{"ts": ts} for ts in ("t2", "t3", "t7")]
    assert inv["user_commands"][0]["command"].startswith("cd /work/repo && git push")
    assert inv["git_mutations"] == [{"ts": "t6", "command": "git push origin eval/x", "by": "user"}]
    assert ("issues", 62) in [(r["kind"], r["number"]) for r in inv["github_refs"]]


def test_inventory_output_omits_plain_user_prompt_text(tmp_path, capsys):
    prompt = "Private launch discussion with no credential-shaped text."
    transcript = tmp_path / f"{SESSION}.jsonl"
    transcript.write_text(json.dumps(human(prompt, "t1")) + "\n", encoding="utf-8")
    assert thread_inventory.main(["--transcript", str(transcript)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["prompts"] == [{"ts": "t1"}]
    assert prompt not in output
    assert "Private launch" not in output


def test_github_refs_from_gh_commands_keep_the_first_sighting():
    entries = [
        *bash("gh issue close 62 -R Owner/Repo --comment done", "g1", ts="2026-10-02T08:00:00Z"),
        {"type": "pr-link", "prNumber": 153, "prRepository": "Owner/Repo", "timestamp": "2026-10-02T08:17:54Z"},
        {"type": "pr-link", "prNumber": 153, "prRepository": "Owner/Repo", "timestamp": "2026-10-02T09:39:00Z"},
    ]
    refs = {(r["kind"], r["number"]): r for r in thread_inventory.inventory(entries)["github_refs"]}
    assert refs[("issues", 62)]["repository"] == "Owner/Repo"
    assert refs[("pull", 153)]["first_seen"] == "2026-10-02T08:17:54Z"


@pytest.mark.parametrize("command", [
    "git clean -fd", "git restore src/x.py", "git checkout -- .", "git rm old.py", "git branch --delete old",
    "git branch -f main HEAD~1", "git -C '/work/my repo' commit -m x", "git stash", "git tag v1.0",
    "LANG=C git push origin main", "gh api repos/o/r/issues/1/comments -X POST -f body=x",
    "gh api --method=PATCH repos/o/r/pulls/1", "gh api repos/o/r/issues -f title=x",
    "gh api graphql -f query='mutation { resolveReviewThread(input:{}) { clientMutationId } }'",
    "gh pr reopen 5", "gh issue delete 7 --yes", "gh api repos/o/r/issues/1/comments --raw-field=body=x",
    "gh api repos/o/r/issues/1/comments --field=body=x", "gh api repos/o/r/pulls --input=pr.json",
    "gh api repos/o/r/issues/1/comments -fbody=x", "gh api -X GET -X DELETE repos/o/r/git/refs/heads/x",
])
def test_git_and_gh_writes_are_detected(command):
    assert len(thread_inventory.inventory(bash(command, "c"))["git_mutations"]) == 1


@pytest.mark.parametrize("command", [
    "git stash list", "git tag -l", "git merge-base --is-ancestor a b", "git branch --show-current",
    "git status --short", "git log --oneline -3", "gh pr view 5 --json state", "gh api repos/o/r/pulls/1",
    "gh api graphql -f query='query { viewer { login } }'", "git config user.email", "gh api -X HEAD repos/o/r",
    "gh api -X DELETE -X GET repos/o/r",
])
def test_reads_are_not_mutations(command):
    assert thread_inventory.inventory(bash(command, "c"))["git_mutations"] == []


def test_long_commands_are_cut_to_their_first_line():
    command = "git commit -q -F - <<'EOF'\n" + "body line\n" * 500 + "EOF"
    mutation = thread_inventory.inventory(bash(command, "c"))["git_mutations"][0]
    assert mutation["command"] == "git commit -q -F - <<'EOF'"


def test_paths_from_chained_cd_git_c_and_worktree_add():
    inv = thread_inventory.inventory(bash(
        "cd .. && git -C Agents worktree add -q --no-track -b claude/x ../Agents-9 origin/main", "p"))
    assert "/work" in inv["directories"]
    assert "/work/Agents" in inv["directories"]  # resolved against the directory after `cd ..`
    assert "/work/Agents-9" in inv["directories"]


@pytest.mark.parametrize("name, expected", [
    ("mcp__claude_ai_ClickUp__clickup_execute_operator", "write"),
    ("mcp__atlassian__createJiraIssue", "write"),
    ("mcp__notion__notion-create-pages", "write"),
    ("mcp__github__push_files", "write"),
    ("mcp__github__request_copilot_review", "write"),
    ("mcp__claude_ai_ClickUp__clickup_start_time_tracking", "write"),
    ("mcp__github__pull_request_read", "read"),
    ("mcp__Agents-Core__log_interaction", "logging"),
    ("mcp__vendor__frobnicate", "unclassified"),
])
def test_mcp_tool_classification(name, expected):
    assert thread_inventory.classify_tool(name, {}) == expected


@pytest.mark.parametrize("name, expected", [
    ("mcp__ccd_turn__update_plan", "client_local"),
    ("mcp__ccd_session__mark_chapter", "client_local"),
    ("mcp__ccd_session_mgmt__set_session_title", "client_local"),
    ("mcp__ccd_turn__wrap_up", "client_local"),
    ("mcp__Agents-Core__run_flow", "read"),
    ("mcp__Agents-Core__validate_subagent_result", "read"),
    ("mcp__gitlab__get_merge_request_notes", "read"),
    ("mcp__gitlab__list_merge_requests", "read"),
    ("mcp__db__readonly_execute", "read"),
    ("mcp__db__execute_read_only_query", "read"),
    ("mcp__gitlab__list_merge_request_changed_files", "read"),
    ("mcp__gitlab__create_merge_request_note", "write"),
])
def test_read_verbs_and_client_local_tools_are_not_external_writes(name, expected):
    assert thread_inventory.classify_tool(name, {}) == expected


def test_client_local_calls_are_reported_apart_from_external_writes():
    inv = thread_inventory.inventory(assistant([
        tool("mcp__ccd_turn__update_plan", {"items": []}, "c1"),
        tool("mcp__ccd_turn__update_plan", {"items": []}, "c2"),
        tool("mcp__ccd_turn__wrap_up", {"recap": "x"}, "c3"),
        tool("mcp__gitlab__create_merge_request_note", {"id": 1}, "c4"),
    ], "2026-10-02T09:00:00Z", "msg"))
    assert [w["tool"] for w in inv["external_writes"]] == ["mcp__gitlab__create_merge_request_note"]
    assert inv["client_local_calls"] == {"mcp__ccd_turn__update_plan": 2, "mcp__ccd_turn__wrap_up": 1}
    assert inv["unclassified_tools"] == {}


@pytest.mark.parametrize("name", [
    "mcp__ccd_pr__set_auto_merge",        # arms the merge of a pull request on the forge
    "mcp__ccd_session_mgmt__delete_session",  # destroys a session
    "mcp__ccd_session_mgmt__send_message",    # reaches another session
    "mcp__ccd_session__move_to_cloud",        # starts a remote session
])
def test_host_server_tools_acting_outside_the_app_are_writes(name):
    assert thread_inventory.classify_tool(name, {}) == "write"


def test_an_exempt_host_tool_keeps_its_time_actor_and_target():
    """What client_local_calls cannot carry: a counter has no time, actor or target."""
    inv = thread_inventory.inventory(assistant([
        tool("mcp__ccd_pr__set_auto_merge", {"url": "https://github.com/Owner/Repo/pull/9"}, "a1"),
        tool("mcp__ccd_turn__update_plan", {"items": []}, "a2"),
    ], "2026-10-02T09:00:00Z", "msg"))
    assert [w["tool"] for w in inv["external_writes"]] == ["mcp__ccd_pr__set_auto_merge"]
    write = inv["external_writes"][0]
    assert write["ts"] == "2026-10-02T09:00:00Z" and write["by"] == "main"
    assert "url=https://github.com/Owner/Repo/pull/9" in write["summary"]
    assert inv["client_local_calls"] == {"mcp__ccd_turn__update_plan": 1}


@pytest.mark.parametrize("name", [
    "mcp__x__get_or_create_page",
    "mcp__x__find_and_replace",
    "mcp__x__list_add_task",
    "mcp__x__query_update_rows",
    "mcp__x__read_and_write_file",
    "mcp__x__show_and_delete",
])
def test_a_write_verb_keeps_its_verdict_after_a_leading_read_verb(name):
    assert thread_inventory.classify_tool(name, {}) == "write"


def test_artifact_tools_and_unclassified_tools_are_reported():
    inv = thread_inventory.inventory(assistant([
        tool("Artifact", {"file_path": "/tmp/page.html"}, "x1"),
        tool("Artifact", {"action": "read", "url": "u"}, "x2"),
        tool("ArtifactData", {"action": "set", "url": "u", "collection": "c"}, "x3"),
        tool("ArtifactComments", {"action": "reply", "url": "u"}, "x4"),
        tool("mcp__vendor__frobnicate", {"q": 1}, "x5"),
        tool("ScheduleWakeup", {"delaySeconds": 60, "reason": "check CI"}, "x6"),
    ], "2026-10-02T09:00:00Z", "msg"))
    tools = [w["tool"] for w in inv["external_writes"]]
    assert tools == ["Artifact", "ArtifactData", "ArtifactComments"]
    assert inv["unclassified_tools"] == {"mcp__vendor__frobnicate": 1}
    assert inv["scheduled"][0]["tool"] == "ScheduleWakeup"


def test_file_history_delta_paths():
    inv = thread_inventory.inventory([{"type": "file-history-delta", "trackingPath": "src/flow_persona.py",
                                       "backup": {"realParentDir": "/work/repo/src"}, "timestamp": "t"}])
    assert inv["files_written"][0]["path"] == "/work/repo/src/flow_persona.py"


def test_credentials_are_masked_in_printed_text():
    entries = [
        human("use token=abc123secret please", "2026-10-02T09:00:00Z"),
        {"type": "ai-title", "aiTitle": "Rotate sk-abcdefghijklmnop1234 key", "sessionId": SESSION},
        *bash("curl -H 'Authorization: Bearer xyz.secret' https://x && git push "
              "https://ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/o/r", "t1"),
        *bash("git clone https://bot:hunter2pass@git.example.com/o/r.git", "t2"),
        *bash("git clone https://bot:p@ssw0rd@git.example.com/o/s.git", "t3"),
        human('password="hunter two" TOKEN=\'alpha beta\' {"secret": "s p a c e"} api_key="unclosed', "t4"),
    ]
    inv = thread_inventory.inventory(entries)
    printed = json.dumps(inv)
    commands = [m["command"] for m in inv["git_mutations"]]
    assert {"git clone https://[masked]@git.example.com/o/r.git",
            "git clone https://[masked]@git.example.com/o/s.git"} <= set(commands)  # a password may contain @
    for secret in ("abc123secret", "xyz.secret", "ghp_abcdefghijklmnopqrstuvwxyz0123", "sk-abcdefghijklmnop1234",
                   "hunter2pass", "ssw0rd", "two", "beta", "a c e", "unclosed"):
        assert secret not in printed
    assert "[masked]" in printed


def test_paths_are_masked_in_output_but_searched_raw(tmp_path, capsys):
    secret_dir = tmp_path / "token=abc123secret"
    repo = secret_dir / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    entries = [
        *assistant([tool("Write", {"file_path": f"{repo}/out.md", "content": "x"}, "w1"),
                    tool("Edit", {"file_path": "/h/.claude/projects/p/memory/ghp_abcdefghijklmnopqrstuvwxyz0123.md",
                                  "old_string": "a", "new_string": "b"}, "w2")], "2026-10-02T09:00:00Z", "m1"),
        *bash(f"cd /tmp/sk-abcdefghijklmnop1234 && cat {SCRATCH}/xoxb-1234567890-abcdef.txt", "b1"),
    ]
    for entry in entries:
        entry["cwd"] = str(repo)
    transcript = secret_dir / f"{SESSION}.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    assert thread_inventory.main(["--transcript", str(transcript)]) == 0
    printed = capsys.readouterr().out
    for secret in ("abc123secret", "ghp_abcdefghijklmnopqrstuvwxyz0123", "sk-abcdefghijklmnop1234",
                   "xoxb-1234567890-abcdef"):
        assert secret not in printed
    roots = json.loads(printed)["git_roots"]
    assert len(roots) == 1 and roots[0].endswith("/token=[masked]")  # found through the real path


def test_heredocs_and_arithmetic_stay_linear():
    command = ("cat <<A\nbody\nA\n" * 5000 + "echo $((1<<2))\n" * 5000
               + "cat " + " ".join(f"<<B{i}" for i in range(2000)) + "\n"
               + "".join(f"x\nB{i}\n" for i in range(2000)) + "git push")
    start = time.monotonic()
    inv = thread_inventory.inventory(bash(command, "big"))
    assert time.monotonic() - start < 2
    assert [m["command"] for m in inv["git_mutations"]] == ["git push"]


def test_git_clone_and_init_destinations_are_directories():
    inv = thread_inventory.inventory(bash(
        "cd /w && git clone https://github.com/o/r.git && git clone -b dev git@github.com:o/s.git t && git init new"
        " && git -C /w commit -C HEAD", "cl"))
    assert {"/w", "/w/r", "/w/t", "/w/new"} <= set(inv["directories"])
    assert "/w/HEAD" not in inv["directories"]  # -C after the subcommand is not a directory
    wrapped = thread_inventory.inventory(bash(
        "env -C /other/repo git commit -m x && sudo -D /srv/x git status && env --chdir=/a/b git push"
        " && env -C/opt/x git status", "wr"))
    assert {"/other/repo", "/srv/x", "/a/b", "/opt/x"} <= set(wrapped["directories"])
    assert [m["command"] for m in wrapped["git_mutations"]] == [
        "env -C /other/repo git commit -m x", "env --chdir=/a/b git push"]
    unresolved = thread_inventory.inventory(bash("cd $SP && git init repo && git -C \"$HOME/x\" status", "v"))
    assert [d for d in unresolved["directories"] if "$" in d] == []  # a shell variable is not a path


def test_cd_in_a_subshell_or_child_shell_stays_there():
    inv = thread_inventory.inventory(bash(
        "(cd /tmp/other); git init next && cd /a && (cd b && git init x) && git init y"
        " && bash -c 'cd /z' && git init w && echo \"$(cd /q && pwd)\" `cd /r` && git init v", "sc"))
    dirs = set(inv["directories"])
    assert {"/work/repo/next", "/a/b/x", "/a/y", "/a/w", "/a/v"} <= dirs
    assert not {"/tmp/other/next", "/z/w", "/q/v", "/r/v"} & dirs


def test_cd_alone_goes_home_and_cd_dash_is_unknown():
    dirs = set(thread_inventory.inventory(bash(
        "cd /a && cd && git init h; cd -- /b && git init c; cd - && git init d", "cd"))["directories"])
    assert {os.path.expanduser("~/h"), "/b/c"} <= dirs and "/b/--" not in dirs
    dash = set(thread_inventory.inventory(bash("cd /a && cd -- -repo && git init x", "cd2"))["directories"])
    assert "/a/-repo/x" in dash
    assert not [d for d in dirs if d.endswith("/d")]  # `cd -` returns to a directory the helper cannot know


def test_shell_writes_list_redirection_and_tee_targets():
    inv = thread_inventory.inventory(bash(
        "printf x > ~/.config/app && echo y >> notes.md && git log > /tmp/l.txt 2>&1 && ls > /dev/null"
        f" && echo z | tee -a ~/.zshrc out.txt && echo s > {SCRATCH}/s.txt && > truncated.txt", "sw"))
    assert [w["files"] for w in inv["shell_writes"]] == [
        [os.path.expanduser("~/.config/app")], ["/work/repo/notes.md"], ["/tmp/l.txt"],
        [os.path.expanduser("~/.zshrc"), "/work/repo/out.txt"], ["/work/repo/truncated.txt"]]
    assert {"/tmp", os.path.expanduser("~/.config")} <= set(inv["directories"])  # git_roots candidates
    tilde = thread_inventory.inventory(bash(
        "echo x > '~/.config/app' && echo y > ~/.real && echo z | tee \"~/t\"", "tl"))
    assert [w["files"] for w in tilde["shell_writes"]] == [
        ["/work/repo/~/.config/app"], [os.path.expanduser("~/.real")], ["/work/repo/~/t"]]  # a quoted ~ is literal
    child = thread_inventory.inventory(bash("env -C /other bash -c 'echo x > out && git init sub'", "ch"))
    assert [w["files"] for w in child["shell_writes"]] == [["/other/out"]] and "/other/sub" in child["directories"]
    assert inv["shell_writes"][2]["command"] == "git log > /tmp/l.txt 2>&1"
    compared = thread_inventory.inventory(bash("[[ a > b ]] && echo $((1 > 0)) && (( x > 2 )) && [ a > c ]", "cmp"))
    assert [w["files"] for w in compared["shell_writes"]] == [["/work/repo/c"]]  # only [ ... ] redirects
    quoted = thread_inventory.inventory(bash(
        "printf '%s' '> not-written.txt' && echo \"a > b\" && git log --format='%h > %s' > out.txt"
        " && echo x >&2 && echo y 2>&1 && echo z >& log.txt && date > runs/$(date +%s).txt", "q"))
    assert [w["files"] for w in quoted["shell_writes"]] == [
        ["/work/repo/out.txt"], ["/work/repo/log.txt"], ["runs/$(…).txt"]]  # unresolved, kept as written


def test_pasted_notifications_end_no_task():
    entries = [
        *bash("sleep 600", "b1", run_in_background=True),
        result("b1", "Command running in background with ID: bg9. Output is being written to: x", "t1"),
        human("<task-notification> <task-id>bg9</task-id> <status>completed</status></task-notification>", "t2"),
        user("see <task-notification> <task-id>bg9</task-id> <status>killed</status></task-notification>", "t3"),
    ]
    tasks = thread_inventory.inventory(entries)["background_tasks"]
    assert [(t["id"], t["ended"]) for t in tasks] == [("bg9", None)]


def test_responses_without_an_id_are_counted_as_skipped():
    entries = [{"type": "assistant", "timestamp": "t1", "message": {"role": "assistant", "content": [
        tool("Bash", {"command": "git push"}, f"x{i}")], "usage": {"output_tokens": 5}}} for i in range(2)]
    inv = thread_inventory.inventory(entries)
    assert (inv["responses"], inv["skipped_entries"], len(inv["git_mutations"])) == (0, 2, 2)


def test_unexpected_entries_are_skipped_not_fatal():
    entries = [
        {"type": "file-history-delta", "trackingPath": 5},
        {"type": "pr-link", "prNumber": "x"}, {"type": "pr-link", "prNumber": True},
        {"type": "pr-link", "prNumber": 1.9},
        {"type": "assistant", "message": {"id": {"not": "hashable"}, "content": []}},
        {"type": "user", "timestamp": 5, "origin": {"kind": "human"}, "message": {"content": "hi"}},
        *assistant([tool("Bash", {"command": ["git", "push"]}, "l1"),
                    tool("Edit", {"file_path": ["a"], "old_string": "a", "new_string": "b"}, "l2")], "t", "ml"),
    ]
    inv = thread_inventory.inventory(entries)
    assert inv["skipped_entries"] == 5 and inv["github_refs"] == []
    assert inv["prompts"] == [{"ts": None}] and inv["files_written"] == []


def test_nesting_past_the_limit_is_not_read():
    def nested(levels, quoted=False):
        command = "echo " + "$(" * levels + "git push" + ")" * levels
        return f'echo "{command}"' if quoted else command
    assert [m["command"] for m in thread_inventory.inventory(bash(nested(15), "n1"))["git_mutations"]] == ["git push"]
    for command in (nested(25), nested(25, quoted=True), "(" * 25 + "git push" + ")" * 25):
        assert thread_inventory.inventory(bash(command, "n2"))["git_mutations"] == []


def test_deep_nesting_never_raises():
    nested = "".join(f"bash <<'E{i}'\n" for i in range(600)) + "git push\n" + "".join(
        f"E{i}\n" for i in reversed(range(600)))
    mixed = "".join(f"bash <<'E{i}'\necho {'$(' * 20}x{')' * 20}\n" for i in range(50)) + "".join(
        f"E{i}\n" for i in reversed(range(50)))
    for command in (nested, mixed):
        thread_inventory.inventory(bash(command, "deep"))


def test_long_parentheses_stay_linear():
    start = time.monotonic()
    for command in ("(" * 300_000, "((" + "x" * 300_000, "$((" * 100_000):
        thread_inventory.inventory(bash(command, "paren"))
    assert time.monotonic() - start < 3


def test_scratchpad_scan_is_linear_on_long_tokens():
    start = time.monotonic()
    thread_inventory.inventory(bash("echo " + "/a" * 80_000 + " " + "A" * 200_000, "big"))
    assert time.monotonic() - start < 2


def test_cli_reads_transcripts_subagents_and_git_roots(entries, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for entry in entries:
        if entry.get("cwd") == "/work/repo":
            entry["cwd"] = str(repo)
    transcript = tmp_path / f"{SESSION}.jsonl"
    # A transcript still being written can end in the middle of a multibyte character.
    transcript.write_bytes(("\n".join(json.dumps(e) for e in entries) + "\nnot json\n").encode() + "ы".encode()[:1])
    sub = tmp_path / SESSION / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text("\n".join(json.dumps(e) for e in [
        *bash("git commit -m from-agent && git push", "sa1", message_id="sub_msg")]) + "\n", encoding="utf-8")
    assert thread_inventory.main(["--transcript", str(transcript)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unreadable_lines"] == 2
    assert out["skipped_entries"] == 0
    assert out["git_roots"] in ([str(repo.resolve())], [str(repo)])
    assert out["subagent_transcripts"] == 1
    assert out["subagents_with_writes"][0]["agent"] == "agent-a1"
    assert {m["by"] for m in out["git_mutations"]} == {"main", "agent-a1"}
    assert out["responses"] == 5  # the subagent's response is counted too


def test_files_written_elsewhere_add_their_directory(tmp_path):
    repo = tmp_path / "other"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    main = tmp_path / f"{SESSION}.jsonl"
    main.write_text("\n".join(json.dumps(e) for e in [
        *assistant([tool("Edit", {"file_path": f"{repo}/a.py", "old_string": "a", "new_string": "b"}, "e1")],
                   "t1", "m1"),
        {"type": "file-history-delta", "trackingPath": "/elsewhere/b.py", "timestamp": "t2"},
    ]) + "\n", encoding="utf-8")
    out = thread_inventory.collect([main])
    assert {str(repo), "/elsewhere"} <= set(out["directories"])
    assert len(out["git_roots"]) == 1 and out["git_roots"][0].endswith("/other")


def test_cli_missing_transcript_and_latest_notes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(thread_inventory.Path, "home", staticmethod(lambda: tmp_path / "home"))
    project = tmp_path / "cfg" / "projects" / thread_inventory.project_dir_name(str(tmp_path / "work"))
    project.mkdir(parents=True)
    assert thread_inventory.main(["--project-dir", str(tmp_path / "work"), "--latest"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "transcript not found"}
    assert thread_inventory.main(["--transcript", str(tmp_path / "missing.jsonl")]) == 1
    assert json.loads(capsys.readouterr().out)["paths"] == [str(tmp_path / "missing.jsonl")]
    for name in ("old", "new"):
        (project / f"{name}.jsonl").write_text("{}\n", encoding="utf-8")
    os.utime(project / "old.jsonl", (time.time() - 60, time.time() - 60))
    assert thread_inventory.main(["--project-dir", str(tmp_path / "work"), "--latest"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["transcripts"] == [str(project / "new.jsonl")]
    assert "old.jsonl" in out["notes"][0]


def test_json_values_that_are_not_objects_are_skipped(tmp_path):
    transcript = tmp_path / f"{SESSION}.jsonl"
    transcript.write_text("[1, 2]\n\"text\"\nnull\nnot json\n" + json.dumps(human("hi", "t1")) + "\n",
                          encoding="utf-8")
    out = thread_inventory.collect([transcript])
    assert (out["skipped_entries"], out["unreadable_lines"]) == (3, 1)
    assert out["prompts"] == [{"ts": "t1"}]


def test_time_span_follows_time_not_file_order(tmp_path):
    early, late = tmp_path / "early.jsonl", tmp_path / "late.jsonl"
    early.write_text(json.dumps(human("a", "2026-10-02T08:00:00Z")) + "\n", encoding="utf-8")
    late.write_text(json.dumps(human("b", "2026-10-02T09:00:00Z")) + "\n", encoding="utf-8")
    out = thread_inventory.collect([late, early])
    assert (out["first"], out["last"]) == ("2026-10-02T08:00:00Z", "2026-10-02T09:00:00Z")


def test_session_lookup_uses_the_claude_config_dir(tmp_path, monkeypatch):
    project = tmp_path / "cfg" / "projects" / "-work-repo"
    project.mkdir(parents=True)
    (project / f"{SESSION}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(thread_inventory.Path, "home", staticmethod(lambda: tmp_path / "home"))
    assert thread_inventory.find_session(SESSION) == [project / f"{SESSION}.jsonl"]
    with pytest.raises(SystemExit):
        thread_inventory.find_session("../etc/passwd")
    assert thread_inventory.project_dir_name("/Users/a.b/Documents/Agents") == "-Users-a-b-Documents-Agents"


@pytest.mark.parametrize("command, kind", [
    ("git notes show HEAD", "read"), ("git notes add -m x", "write"), ("git update-ref refs/x HEAD", "write"),
    ("git sparse-checkout set src", "write"), ("git sparse-checkout list", "read"),
    ("git frobnicate --all", "unknown"), ("git add -A src", "write"), ("git archive HEAD", "read"),
    ("git bundle create x.bundle HEAD", "read"), ("git reflog expire --expire=now --all", "write"),
    ("git reflog delete HEAD@{1}", "write"), ("git reflog", "read"), ("git reflog show HEAD", "read"),
    ("git remote set-branches origin main", "write"), ("git replace -l", "read"), ("git replace a b", "write"),
])
def test_git_subcommands(command, kind):
    assert thread_inventory.git_kind(command.split()) == kind


@pytest.mark.parametrize("command, kind", [
    ("git tag", "read"), ("git tag -v v1", "read"), ("git tag --verify v1", "read"), ("git tag -n5", "read"),
    ("git tag --format %(refname)", "read"), ("git tag --sort -creatordate", "read"), ("git tag --column", "read"),
    ("git tag -a v2 -m x", "write"), ("git tag -d v1", "write"),
    ("git branch --no-track feat origin/main", "write"), ("git branch -q feat", "write"),
    ("git branch -v feat", "write"), ("git branch -m new", "write"), ("git branch --set-upstream-to=o/x", "write"),
    ("git branch --format %(refname)", "read"), ("git branch --sort -committerdate", "read"),
    ("git branch -vv", "read"), ("git branch -r", "read"), ("git branch --contains HEAD", "read"),
])
def test_git_tag_and_branch_list_unless_given_a_name(command, kind):
    assert thread_inventory.git_kind(command.split()) == kind


@pytest.mark.parametrize("command, kind", [
    ("git push --dry-run", "read"), ("git push -n origin x", "read"), ("git clean -n", "read"),
    ("git clean -fdn", "read"), ("git commit --dry-run", "read"), ("git add -n .", "read"),
    ("git apply --check x.patch", "read"), ("git worktree prune -n", "read"),
    ("git remote prune --dry-run origin", "read"), ("git reflog expire -n --all", "read"),
    ("git apply --stat --apply x.patch", "write"), ("git commit -n -m x", "write"),  # -n is --no-verify
    ("git clean -fdx", "write"), ("git add -N x", "write"), ("git notes prune -n", "read"),
    ("git notes prune --dry-run", "read"), ("git notes prune", "write"),
])
def test_dry_runs_and_checks_are_reads(command, kind):
    assert thread_inventory.git_kind(command.split()) == kind


@pytest.mark.parametrize("command, kind", [
    ("git fetch origin", "read"), ("git fetch --all --prune", "read"), ("git fetch --depth 1 origin main", "read"),
    ("git fetch origin refs/heads/*:refs/remotes/origin/*", "read"), ("git fetch --dry-run origin main:main", "read"),
    ("git fetch origin pull/155/head:pr-155", "write"), ("git fetch origin +main:main", "write"),
    ("git fetch origin tag v1", "write"), ("git fetch --tags", "write"),
    ("git fetch git@github.com:o/r.git x:y", "write"),
])
def test_fetch_writes_only_into_local_refs(command, kind):
    assert thread_inventory.git_kind(command.split()) == kind


def test_unknown_git_and_gh_commands_are_listed_not_dropped():
    inv = thread_inventory.inventory(bash("git frobnicate --all && gh extension install o/x && /usr/bin/git push", "u"))
    assert [c["command"] for c in inv["unclassified_commands"]] == ["git frobnicate --all", "gh extension install o/x"]
    assert [m["command"] for m in inv["git_mutations"]] == ["/usr/bin/git push"]


@pytest.mark.parametrize("command, expected", [
    ("gh pr merge --squash 12", ("pull", 12, None)),
    ("gh issue close -R o/r 62", ("issues", 62, "o/r")),
    ("gh issue close --repo=o/r 63", ("issues", 63, "o/r")), ("gh issue close -Ro/r 64", ("issues", 64, "o/r")),
    ("gh pr comment 5 --body 12", ("pull", 5, None)),
    ("gh pr view '#7' --json state", ("pull", 7, None)),
    ("gh pr checks --interval 30 153", ("pull", 153, None)), ("gh pr merge -s 12", ("pull", 12, None)),
    ("gh pr review -a 155", ("pull", 155, None)), ("gh pr view -c 7", ("pull", 7, None)),
    ("gh pr diff -e '*.md' 9", ("pull", 9, None)),
    ("gh api repos/o/r/issues/62/comments -f body=x", ("issues", 62, "o/r")),
    ("gh api -X POST 'repos/{owner}/{repo}/pulls/155/requested_reviewers' -f 'reviewers[]=x'", ("pull", 155, None)),
])
def test_gh_reference_parsing_skips_flags(command, expected):
    refs = thread_inventory.inventory(bash(command, "r"))["github_refs"]
    assert [(r["kind"], r["number"], r["repository"]) for r in refs] == [expected]


def test_gh_list_limit_is_not_a_reference():
    assert thread_inventory.inventory(bash("gh pr list --limit 30", "r"))["github_refs"] == []
    assert thread_inventory.inventory(bash("gh api repos/o/r/issues/comments/123", "r"))["github_refs"] == []
    assert thread_inventory.inventory(bash("gh pr view \u00b2", "r"))["github_refs"] == []  # no int() of a superscript


def test_structured_mcp_references():
    inv = thread_inventory.inventory(assistant([
        tool("mcp__github__pull_request_read", {"owner": "Owner", "repo": "Repo", "pullNumber": 153}, "s1"),
        tool("mcp__github__issue_read", {"owner": "Owner", "repo": "Repo", "issue_number": "9"}, "s2"),
    ], "2026-10-02T09:00:00Z", "msg"))
    refs = [(r["repository"], r["kind"], r["number"]) for r in inv["github_refs"]]
    assert refs == [("Owner/Repo", "issues", 9), ("Owner/Repo", "pull", 153)]


def test_image_prompts_retain_only_timestamp_metadata():
    inv = thread_inventory.inventory([
        human([{"type": "image", "source": {}}], "t1"),
        human([{"type": "image", "source": {}}, {"type": "text", "text": "what is this?"}], "t2"),
        user([{"type": "image", "source": {}}, {"type": "text", "text": "and this?"}], "t3"),  # no origin
        user([{"type": "text", "text": "<command-name>/clear</command-name>"}], "t4"),
        user([{"type": "text", "text": "[Request interrupted by user]"}], "t5"),  # written by the client
        user("<pasted_content id=1>log</pasted_content> why?", "t6"), user("<local-command-stdout>x", "t7"),
    ])
    assert inv["prompts"] == [{"ts": ts} for ts in ("t1", "t2", "t3", "t6")]


def test_every_writer_of_a_file_is_kept(tmp_path):
    main = tmp_path / f"{SESSION}.jsonl"
    main.write_text(json.dumps(assistant([tool("Edit", {"file_path": "/w/a.py", "old_string": "a",
                                                         "new_string": "b"}, "e1")], "t1", "m1")[0]) + "\n",
                    encoding="utf-8")
    sub = tmp_path / SESSION / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-b.jsonl").write_text(json.dumps(assistant([tool("Write", {"file_path": "/w/a.py", "content": "c"},
                                                                  "e2")], "t2", "m2")[0]) + "\n", encoding="utf-8")
    (sub / "agent-c.jsonl").write_text(json.dumps({"type": "file-history-delta", "trackingPath": "/w/c.py",
                                                   "timestamp": "t3"}) + "\n", encoding="utf-8")
    out = thread_inventory.collect([main])
    assert out["files_written"][0] == {"path": "/w/a.py", "ts": "t1", "by": ["main", "agent-b"]}
    assert [(s["agent"], s["files_written"]) for s in out["subagents_with_writes"]] == [("agent-b", 1), ("agent-c", 1)]
    (sub / "agent-d.jsonl").write_text(json.dumps(bash("echo x > /w/d.txt", "d1")[0]) + "\n", encoding="utf-8")
    shell_only = thread_inventory.collect([main])["subagents_with_writes"]
    assert [(s["agent"], s["shell_writes"]) for s in shell_only if s["agent"] == "agent-d"] == [("agent-d", 1)]


def test_quoted_config_survives_a_multiline_message():
    command = 'git -c user.name="Alexey Zhuchkov" -c user.email=a@b commit -q -m "Subject\n\nBody line"'
    inv = thread_inventory.inventory(bash(command, "q"))
    assert [m["command"] for m in inv["git_mutations"]] == [
        'git -c user.name="Alexey Zhuchkov" -c user.email=a@b commit -q -m "Subject']
    assert inv["unclassified_commands"] == []


def test_line_continuations_and_code_lines_in_heredocs():
    inv = thread_inventory.inventory(bash("git -c user.name=X \\\n  commit -m y && python3 - <<'EOF'\n"
                                          "c['git/gh write'] += 1\nEOF", "lc"))
    assert [m["command"] for m in inv["git_mutations"]] == ["git -c user.name=X commit -m y"]
    assert inv["unclassified_commands"] == []


@pytest.mark.parametrize("command, expected", [
    ("cat > notes.md <<'EOF'\ngit push origin main\nEOF\ngit status", []),
    ("cat > notes.md <<'EOF'\ngit push origin main", []),  # no delimiter line: the rest is the body
    ("bash <<'EOF'\ngit push origin main", ["git push origin main"]),
    ("git commit -F - <<-EOF\n\tgh pr merge 5\n\tEOF", ["git commit -F - <<-EOF"]),
    ("bash <<'EOF'\ngit push origin main\nEOF", ["git push origin main"]),
    ('gh pr comment 5 --body "fixed\ngit push origin main; git reset --hard"', ['gh pr comment 5 --body "fixed']),
    ("# don't forget\ngit push", ["git push"]),
    ("echo $((1<<2))\ngit push", ["git push"]),
    ("if git diff --quiet; then echo same; else git commit -am x; fi", ["git commit -am x"]),
    ("git push origin main 2>&1 | tail -1", ["git push origin main 2>&1"]),
    ("git branch --merged main | grep -v main | xargs git branch -d", ["xargs git branch -d"]),
    ("gh pr list --json number --jq '.[].number' | xargs -I {} gh pr close {}", ["xargs -I {} gh pr close {}"]),
    ("timeout 60 git push origin main && sudo -u bot git commit -m x", [
        "timeout 60 git push origin main", "sudo -u bot git commit -m x"]),
    ("env -i PATH=/bin git push; env -u X git tag v2; time -p gh issue close 5; nice -n 10 git gc", [
        "env -i PATH=/bin git push", "env -u X git tag v2", "time -p gh issue close 5", "nice -n 10 git gc"]),
    ("X+=1 git push && command -v git", ["X+=1 git push"]), ("env -a custom0 git push", ["env -a custom0 git push"]),
    ("command -v gh pr close 5; sudo -l git push; sudo -u bot git tag v8", ["sudo -u bot git tag v8"]),
    ("echo foo\\ #bar; git push", ["git push"]),  # an escaped blank keeps #bar in the word
    ("/usr/bin/time -o timing.txt git push", ["/usr/bin/time -o timing.txt git push"]),
    (">out git push; 2>/tmp/e gh issue close 1; < in.txt git apply", [
        ">out git push", "2>/tmp/e gh issue close 1", "< in.txt git apply"]),
    ('args=(git push origin main); declare -a more=(gh pr merge 5) && files=($(git ls-files) "$(git push)")', [
        "git push"]),  # array elements are data, their substitutions run
    ("trap -- 'git push origin x' EXIT; trap -p EXIT; trap - EXIT", ["git push origin x"]),
    ("<<'EOF' bash\ngit push\nEOF\nbash << 'EOF'\ngh pr merge 7\nEOF", ["git push", "gh pr merge 7"]),
    ("cat <<'EOF' | bash\ngit push\nEOF\ncat <<'EOF' | python3 -\ngit tag v1\nEOF\n"
     "cat <<'EOF' |& sh\ngh pr merge 3\nEOF",
     ["git push", "gh pr merge 3"]),  # a heredoc piped into a shell runs there
    ("cat <<'EOF' | cat | bash\ngit push\nEOF\ncat <<'EOF' | tee log.txt | sh\ngh pr merge 9\nEOF\n"
     "cat <<'EOF' | grep x | bash\ngit tag v5\nEOF", ["git push", "gh pr merge 9"]),  # cat and tee pass it on
    ("env -C/tmp git commit -m x && env -S'git tag v9'", ["env -C/tmp git commit -m x", "env -S'git tag v9'"]),
    ("cat <<'EOF' | bash < run.sh\ngit push\nEOF\nbash <<'EOF' < run.sh\ngit tag v1\nEOF\n"
     "bash < run.sh <<'EOF'\ngh pr merge 4\nEOF", ["gh pr merge 4"]),  # the last input redirection wins
    ("env -S 'git push origin main' && env --split-string='gh pr merge 5'", ["env -S 'git push origin main'",
                                                                         "env --split-string='gh pr merge 5'"]),
    ("bash 2>&1 <<'EOF'\ngit push origin main\nEOF\nbash > /tmp/o.log <<'EOF'\ngh pr merge 5\nEOF", [
        "git push origin main", "gh pr merge 5"]),
    ("bash -euo pipefail <<'EOF'\ngit push\nEOF\nbash -euo pipefail -c 'git tag v1'; bash <<< 'gh pr merge 6'", [
        "git push", "git tag v1", "gh pr merge 6"]),
    ("git commit -m \"$(cat <<'EOF'\nRevert\n\ngit push --force rewrote main\nEOF)\" && git push origin main", [
        'git commit -m "$(…)"', "git push origin main"]),  # bash ends the body at EOF) inside $(…)
    ("X=a#b git push; echo a#b && git tag v3", ["X=a#b git push", "git tag v3"]),
    ("function g { gh pr merge 5; }; eval 'git push origin x'; trap 'git stash' EXIT", [
        "function g { gh pr merge 5", "git push origin x", "git stash"]),
    ('GIT_AUTHOR_NAME="Alex Doe" env A="b c" git commit -m x',
     ['GIT_AUTHOR_NAME="Alex Doe" env A="b c" git commit -m x']),
    ("URL=$(gh pr create --fill) && echo `git tag v1`", ["gh pr create --fill", "git tag v1"]),
    ("echo 'git push' \"git push\" $'it\\'s; git push'", []),
])
def test_only_executed_commands_are_classified(command, expected):
    inv = thread_inventory.inventory(bash(command, "h"))
    assert [m["command"] for m in inv["git_mutations"]] == expected
    assert inv["unclassified_commands"] == []


def test_references_from_any_tool_input_but_not_file_contents():
    inv = thread_inventory.inventory(assistant([
        tool("Agent", {"description": "review", "prompt": "Review https://github.com/Owner/Repo/pull/155"}, "a1"),
        tool("WebFetch", {"url": "https://github.com/Owner/Repo/issues/62", "prompt": "state?"}, "a2"),
        tool("Write", {"file_path": "/w/doc.md", "content": "See https://github.com/Owner/Repo/issues/9"}, "a3"),
    ], "2026-10-02T09:00:00Z", "msg"))
    assert [(r["kind"], r["number"]) for r in inv["github_refs"]] == [("issues", 62), ("pull", 155)]


@pytest.mark.parametrize("command, expected", [
    ("gh api repos/o/r/issues/$(cat n)/comments -f body=x", ["gh api repos/o/r/issues/$(…)/comments -f body=x"]),
    ("git commit -q -m \"$(cat <<'EOF'\nIt's done; git push origin main\nEOF\n)\" && git push", [
        'git commit -q -m "$(…)"', "git push"]),
    ("bash -c 'cd /w && git push origin x' && sh -ec \"gh pr merge 5\"", ["git push origin x", "gh pr merge 5"]),
    ("bash run.sh <<'EOF'\ngit push\nEOF", []),
    ("cat > n.md <<EOF\nSee $(git push origin main) and `gh pr merge 5`\n\\$(git reset --hard)\nEOF", [
        "git push origin main", "gh pr merge 5"]),  # an unquoted body runs its substitutions
    ("cat > n.md <<'EOF'\n$(git push)\nEOF\ncat > m.md <<\\EOF\n`git push`\nEOF", []),
    ("cat > f <<E'OF'\n$(git push)\nEOF\ngit tag v1\nbash <<\"E\"OF\ngh pr merge 2\nEOF", [
        "git tag v1", "gh pr merge 2"]),  # a partly quoted delimiter is the whole word, quoted
    ("[[ a > b ]] && echo $((1 > 0)) && (( x > 2 )) && echo $(( $(git push) + 1 ))", ["git push"]),
    ('cat > n.md <<"EOF"\n$(git push)\nEOF', []),
    ("r(){ gh api -X POST repos/o/r/pulls/1/comments/$1/replies -f body=\"$2\"; }", [
        'gh api -X POST repos/o/r/pulls/1/comments/$1/replies -f body="$2"']),
    ("for f in a b; do git hash-object $f; done && git hash-object -w x", ["git hash-object -w x"]),
])
def test_substitutions_shell_scripts_and_functions(command, expected):
    inv = thread_inventory.inventory(bash(command, "s"))
    assert [m["command"] for m in inv["git_mutations"]] == expected
    assert inv["unclassified_commands"] == []


def test_references_come_from_executed_commands_not_heredoc_bodies():
    inv = thread_inventory.inventory(bash(
        "gh pr comment 5 -R o/r --body 'see https://github.com/o/r/issues/8' && cat > t.py <<'EOF'\n"
        "URL = 'https://github.com/Owner/Repo/pull/155'\nEOF\necho $(gh pr view https://github.com/o/r/pull/9)", "rf"))
    assert [(r["kind"], r["number"]) for r in inv["github_refs"]] == [("issues", 8), ("pull", 5), ("pull", 9)]


@pytest.mark.parametrize("command, kind", [
    ("git config --unset user.email", "write"), ("git config --global --remove-section alias", "write"),
    ("git config --edit", "write"), ("git config set user.name X", "write"), ("git config unset user.name", "write"),
    ("git config get user.name", "read"), ("git config list", "read"), ("git config --file f k v", "write"),
    ("git config -f .gitmodules submodule.x.path", "read"), ("git config --global user.email a@b", "write"),
    ("git config --get-urlmatch http https://x", "read"), ("git config -t bool core.filemode", "read"),
    ("git config --global get user.name", "read"), ("git config --file cfg get user.name", "read"),
    ("git config --global set user.name X", "write"),
])
def test_git_config_reads_and_writes(command, kind):
    assert thread_inventory.git_kind(command.split()) == kind


@pytest.mark.parametrize("command, kind", [
    ("gh auth login --web", "write"), ("gh auth setup-git", "write"), ("gh auth switch", "write"),
    ("gh auth status", "read"), ("gh auth token", "read"), ("gh config set editor vim", "write"),
    ("gh api graphql --input q.json", "unknown"),
    ("gh config get editor", "read"), ("gh $group --help", "read"), ("gh pr -h", "read"),
])
def test_gh_auth_and_config(command, kind):
    assert thread_inventory.gh_kind(command.split(), command) == kind


def test_subagent_items_keep_their_place_in_time(tmp_path):
    main = tmp_path / f"{SESSION}.jsonl"
    main.write_text("\n".join(json.dumps(e) for e in [
        *bash("git push origin x && gh pr view https://github.com/o/r/pull/5", "m1", ts="2026-10-02T09:05:00Z"),
        *assistant([tool("Write", {"file_path": "/w/a.py", "content": "m"}, "m2")], "2026-10-02T09:06:00Z", "mm2"),
    ]) + "\n", encoding="utf-8")
    sub = tmp_path / SESSION / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-c.jsonl").write_text("\n".join(json.dumps(e) for e in [
        *bash("git commit -m early && gh pr view https://github.com/o/r/pull/5", "c1", ts="2026-10-02T09:01:00Z"),
        *assistant([tool("Write", {"file_path": "/w/a.py", "content": "c"}, "c2")], "2026-10-02T09:02:00Z", "cm2"),
    ]) + "\n", encoding="utf-8")
    out = thread_inventory.collect([main])
    assert [m["command"] for m in out["git_mutations"]] == ["git commit -m early", "git push origin x"]
    assert out["github_refs"][0]["first_seen"] == "2026-10-02T09:01:00Z"
    assert out["files_written"] == [{"path": "/w/a.py", "ts": "2026-10-02T09:02:00Z", "by": ["agent-c", "main"]}]


GITLAB_MR = "https://git.example.org/grp/sub/proj/-/merge_requests/106"


def test_gitlab_merge_request_keeps_its_own_kind_and_real_url():
    entries = [{"type": "pr-link", "prNumber": 106, "prRepository": "grp/sub/proj", "prUrl": GITLAB_MR,
                "timestamp": "2026-11-05T14:20:01Z"}]
    refs = thread_inventory.inventory(entries)["github_refs"]
    assert refs == [{"repository": "grp/sub/proj", "kind": "merge_request", "number": 106, "url": GITLAB_MR,
                     "first_seen": "2026-11-05T14:20:01Z"}]


def test_github_pull_request_keeps_its_github_url():
    entries = [{"type": "pr-link", "prNumber": 7, "prRepository": "Owner/Repo",
                "prUrl": "https://github.com/Owner/Repo/pull/7", "timestamp": "2026-11-05T14:20:01Z"}]
    refs = thread_inventory.inventory(entries)["github_refs"]
    assert [(r["repository"], r["kind"], r["url"]) for r in refs] == [
        ("Owner/Repo", "pull", "https://github.com/Owner/Repo/pull/7")]


def test_ref_with_an_undeterminable_host_is_left_out_not_invented():
    entries = [{"type": "pr-link", "prNumber": 75, "prRepository": "grp/sub/subsub/proj",
                "timestamp": "2026-11-04T10:00:00Z"}]
    assert thread_inventory.inventory(entries)["github_refs"] == []


def test_gitlab_merge_request_link_in_text_is_not_a_github_pull_request():
    inv = thread_inventory.inventory([human(f"see {GITLAB_MR} please", "t1")])
    assert [(r["kind"], r["url"]) for r in inv["github_refs"]] == [("merge_request", GITLAB_MR)]


@pytest.mark.parametrize("command", [
    'gh pr view 5 --repo "$R"', "gh pr view 5 -R $R", 'gh api repos/$OWNER/$R/pulls/5',
])
def test_repository_starting_with_a_dollar_sign_is_not_recorded(command):
    refs = thread_inventory.inventory(bash(command, "r"))["github_refs"]
    assert refs and all(r["repository"] is None and r["url"] is None for r in refs)


WIN_MEMORY = "C:\\Users\\u\\.claude\\projects\\p\\memory\\"


@pytest.mark.parametrize("tool_name, data", [
    ("Write", {"file_path": WIN_MEMORY + "fact.md", "content": "x"}),
    ("Edit", {"file_path": WIN_MEMORY + "fact.md", "old_string": "a", "new_string": "b"}),
])
def test_memory_writes_accept_windows_backslash_paths(tool_name, data):
    inv = thread_inventory.inventory(assistant([tool(tool_name, data, "w1")], "2026-10-02T09:00:00Z", "m1"))
    assert [m["path"] for m in inv["memory_writes"]] == [WIN_MEMORY + "fact.md"]


def _session_with_memory(tmp_path, command_ts="2026-10-02T09:00:00Z"):
    project = tmp_path / ".claude" / "projects" / "p"
    (project / "memory").mkdir(parents=True)
    entries = [human("go", "2026-10-02T09:00:00Z"),
               *bash("python3 - <<'PY'\nopen('m','w').write('x')\nPY", "b1", ts="2026-10-02T09:05:00Z")]
    transcript = project / f"{SESSION}.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return project, transcript


def _mtime(stamp):
    from datetime import datetime
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def test_memory_files_written_by_a_script_are_detected_by_modification_time(tmp_path):
    project, transcript = _session_with_memory(tmp_path)
    inside, before, after = (project / "memory" / n for n in ("inside.md", "before.md", "after.md"))
    for path, stamp in ((inside, "2026-10-02T09:03:00Z"), (before, "2026-10-01T09:00:00Z"),
                        (after, "2026-10-03T09:00:00Z")):
        path.write_text("x")
        os.utime(path, (_mtime(stamp), _mtime(stamp)))
    result = thread_inventory.collect([transcript])
    assert result["memory_writes"] == []  # nothing was observed as a tool call
    assert [(m["path"], m["detected_by"]) for m in result["memory_files_detected"]] == [
        (str(inside), "modification time")]
    assert result["memory_scan_notes"] == []


def test_a_memory_file_seen_as_a_tool_call_is_not_listed_again_as_detected(tmp_path):
    project, transcript = _session_with_memory(tmp_path)
    known = project / "memory" / "known.md"
    known.write_text("x")
    os.utime(known, (_mtime("2026-10-02T09:03:00Z"),) * 2)
    result = thread_inventory.detect_memory_files([project / "memory"], "2026-10-02T09:00:00Z",
                                                  "2026-10-02T09:05:00Z", {str(known)})
    assert result["memory_files_detected"] == []


def test_a_missing_or_unreadable_memory_directory_is_a_note_not_a_failure(tmp_path):
    missing = thread_inventory.detect_memory_files([tmp_path / "nope"], "2026-10-02T09:00:00Z",
                                                   "2026-10-02T09:05:00Z", set())
    assert missing == {"memory_files_detected": [],
                       "memory_scan_notes": [{"directory": str(tmp_path / "nope"), "status": "missing"}]}
    plain_file = tmp_path / "file"
    plain_file.write_text("x")
    not_a_dir = thread_inventory.detect_memory_files([plain_file], "2026-10-02T09:00:00Z",
                                                     "2026-10-02T09:05:00Z", set())
    assert not_a_dir["memory_files_detected"] == [] and not_a_dir["memory_scan_notes"][0]["status"] == "missing"
    _, transcript = _session_with_memory(tmp_path / "other")
    (transcript.parent / "memory").rmdir()
    assert thread_inventory.collect([transcript])["memory_scan_notes"][0]["status"] == "missing"


def queued(prompt, ts, **attachment):
    return {"type": "attachment", "timestamp": ts, "sessionId": SESSION, "cwd": "/work/repo",
            "attachment": {"type": "queued_command", "prompt": prompt, **attachment}}


def test_origin_less_first_record_after_system_reminders_is_a_prompt():
    reminder = "<system-reminder>You were started from another session.</system-reminder>"
    inv = thread_inventory.inventory([
        user(f"{reminder}\n\n{reminder}\n\nClose https://github.com/Owner/Repo/issues/580", "t1"),
        user(f"{reminder}\n\n", "t2"),  # nothing but reminders
        user(f"{reminder}\n\n<local-command-stdout>x</local-command-stdout>", "t3"),  # a harness record
        user("<system-reminder>unclosed block", "t4"),
        user("This session is being continued...", "t5", isCompactSummary=True),
    ])
    assert inv["prompts"] == [{"ts": "t1"}]
    assert ("issues", 580) in [(r["kind"], r["number"]) for r in inv["github_refs"]]


def test_queued_command_attachments_count_only_when_origin_says_human():
    """Without `origin` the command mode says nothing: the app queues its own events as prompts."""
    inv = thread_inventory.inventory([
        human("start", "t1"),
        queued("also see https://github.com/Owner/Repo/pull/7", "t2", commandMode="prompt",
               origin={"kind": "human"}, humanTurn=True),
        queued('<ci-monitor-event>"Auto-fix pull requests" is watching '
               "https://github.com/Owner/Repo/pull/8</ci-monitor-event>",
               "t3", commandMode="prompt"),  # the app's own injection
        queued("no origin and no tag", "t4", commandMode="prompt"),
        queued("<task-notification><task-id>a1</task-id></task-notification>", "t5",
               commandMode="task-notification", origin={"kind": "task-notification"}),
        queued("<task-notification>no origin</task-notification>", "t6", commandMode="task-notification"),
        queued("from a peer", "t7", commandMode="prompt", origin={"kind": "peer"}),
        {"type": "attachment", "timestamp": "t8", "attachment": {"type": "edited_text_file"}},
    ])
    assert inv["prompts"] == [{"ts": "t1"}, {"ts": "t2", "queued": True}]
    assert ("pull", 7) in [(r["kind"], r["number"]) for r in inv["github_refs"]]
    assert 8 not in [r["number"] for r in inv["github_refs"]]  # an app event is not the user's text


WIN_CWD = "C:\\work\\repo"


def powershell(command, tool_id, ts="2026-10-02T09:00:00Z", cwd=WIN_CWD, **extra):
    """A call of the `PowerShell` tool that a Windows client offers beside `Bash`."""
    entries = assistant([tool("PowerShell", {"command": command, **extra}, tool_id)], ts, f"m_{tool_id}")
    return [{**entry, "cwd": cwd} for entry in entries]


def test_powershell_tool_commands_reach_the_same_fields():
    inv = thread_inventory.inventory([
        *powershell("git add .; git commit -m 'slice A5'\ngit push origin HEAD", "p1"),
        *powershell("gh pr create --title t --body b", "p2", ts="2026-10-02T09:01:00Z"),
        *powershell("Set-Content -Path notes.md -Value 'done'", "p3", ts="2026-10-02T09:02:00Z"),
        *powershell("git log --oneline > logs\\out.txt", "p4", ts="2026-10-02T09:03:00Z"),
    ])
    assert [m["command"] for m in inv["git_mutations"]] == [
        "git add .", "git commit -m 'slice A5'", "git push origin HEAD", "gh pr create --title t --body b"]
    assert [(w["command"], w["files"]) for w in inv["shell_writes"]] == [
        ("Set-Content -Path notes.md -Value 'done'", ["C:/work/repo/notes.md"]),
        ("git log --oneline > logs\\out.txt", ["C:/work/repo/logs/out.txt"])]
    assert inv["unclassified_commands"] == []
    # one spelling of the Windows working directory, and the directory the redirection wrote in
    assert "C:/work/repo/logs" in inv["directories"] and WIN_CWD not in inv["directories"]


def test_powershell_statements_separate_on_semicolons_newlines_pipes_and_braces():
    inv = thread_inventory.inventory(powershell(
        "git tag v1; git commit -m a\nforeach ($f in $files) { git add $f }\n"
        "git diff | Out-File -FilePath d.txt", "s1"))
    assert [m["command"] for m in inv["git_mutations"]] == ["git tag v1", "git commit -m a", "git add $f"]
    assert [w["files"] for w in inv["shell_writes"]] == [["C:/work/repo/d.txt"]]
    quoted = thread_inventory.inventory(powershell("git commit -m 'first; second | third'", "s2"))
    assert [m["command"] for m in quoted["git_mutations"]] == ["git commit -m 'first; second | third'"]


def test_powershell_here_strings_are_bodies_not_commands():
    inv = thread_inventory.inventory(powershell(
        "git commit -F - @'\ngit push origin main\nSet-Content C:\\evil.txt x\n'@", "h1"))
    assert [m["command"] for m in inv["git_mutations"]] == ["git commit -F - @'…'@"]
    assert inv["shell_writes"] == []  # the body is a commit message, not a cmdlet call
    expandable = thread_inventory.inventory(powershell('$body = @"\ngit push\n"@\ngit commit -m $body', "h2"))
    assert [m["command"] for m in expandable["git_mutations"]] == ["git commit -m $body"]
    refs = thread_inventory.inventory(powershell(
        "gh pr comment https://github.com/Owner/Repo/pull/7 --body @'\n"
        "see https://github.com/Owner/Repo/issues/9\n'@", "h3"))
    assert [(r["kind"], r["number"]) for r in refs["github_refs"]] == [("pull", 7)]


def test_powershell_writes_cmdlet_targets_and_redirections():
    inv = thread_inventory.inventory(powershell(
        "Set-Content -Path a.txt -Value 'x' -Encoding utf8\nAdd-Content b.txt 'more'\n"
        "git log | Out-File -FilePath C:\\logs\\log.txt\nWrite-Output x > c.txt\nWrite-Output y >> c.txt", "w1"))
    assert [(w["command"], w["files"]) for w in inv["shell_writes"]] == [
        ("Set-Content -Path a.txt -Value 'x' -Encoding utf8", ["C:/work/repo/a.txt"]),
        ("Add-Content b.txt 'more'", ["C:/work/repo/b.txt"]),
        ("Out-File -FilePath C:\\logs\\log.txt", ["C:/logs/log.txt"]),
        ("Write-Output x > c.txt", ["C:/work/repo/c.txt"]),
        ("Write-Output y >> c.txt", ["C:/work/repo/c.txt"])]
    plain = thread_inventory.inventory(powershell(
        "git log 2>&1; git log *>&1; git log > $null; Write-Output 'a > b.txt'; Get-Content a.txt", "w2"))
    assert plain["shell_writes"] == []  # a duplicated stream, $null and quoted text write no file


def test_powershell_backslash_is_a_path_separator_and_a_backtick_escapes():
    captured = thread_inventory.inventory(powershell(
        "$sha = & 'C:\\Program Files\\Git\\bin\\git.exe' commit -m \"fix: it's done\"", "q1"))
    assert [m["command"] for m in captured["git_mutations"]] == [
        "$sha = & 'C:\\Program Files\\Git\\bin\\git.exe' commit -m \"fix: it's done\""]
    literal = thread_inventory.inventory(powershell("Set-Content -LiteralPath C:\\repo\\a\\b.txt -Value x", "q2"))
    assert [w["files"] for w in literal["shell_writes"]] == [["C:/repo/a/b.txt"]]
    continued = thread_inventory.inventory(powershell("git commit `\n  -m 'wrapped'", "q3"))
    assert [m["command"] for m in continued["git_mutations"]] == ["git commit -m 'wrapped'"]
    commented = thread_inventory.inventory(powershell("# git push\n<# git tag v2 #>git fetch --dry-run", "q4"))
    assert commented["git_mutations"] == [] and commented["unclassified_commands"] == []


def test_powershell_commands_that_cannot_be_classified_are_listed_not_dropped():
    unknown = thread_inventory.inventory(powershell("git frobnicate --hard; gh wobble 3", "u1"))
    assert [c["command"] for c in unknown["unclassified_commands"]] == ["git frobnicate --hard", "gh wobble 3"]
    assert unknown["git_mutations"] == []
    chained = thread_inventory.inventory(powershell(
        "git status && git commit -m x\ngh pr view 7 || gh pr create", "u2"))
    assert [c["command"] for c in chained["unclassified_commands"]] == [
        "git status && git commit -m x", "gh pr view 7 || gh pr create"]
    assert chained["git_mutations"] == []  # 5.1 runs neither operator, so neither line ran
    other = thread_inventory.inventory(powershell("Remove-Item -Recurse -Force .\\build", "u3"))
    assert (other["git_mutations"], other["unclassified_commands"], other["shell_writes"]) == ([], [], [])


def test_powershell_background_start_is_a_task():
    inv = thread_inventory.inventory([
        *powershell("git push origin HEAD", "bg1", run_in_background=True),
        result("bg1", "Command running in background with ID: bg9. Output is being written to: x",
               "2026-10-02T09:00:05Z"),
    ])
    assert [(t["tool"], t["id"]) for t in inv["background_tasks"]] == [("PowerShell", "bg9")]
