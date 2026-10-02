"""The thread inventory helper reads a Claude Code transcript, not the model's memory."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("thread_inventory", ROOT / "scripts" / "dev" / "thread_inventory.py")
thread_inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(thread_inventory)

SESSION = "11111111-2222-3333-4444-555555555555"
SCRATCH = f"/tmp/claude-501/-proj/{SESSION}/scratchpad"


def user(text, ts, **extra):
    return {"type": "user", "timestamp": ts, "sessionId": SESSION, "cwd": "/work/repo",
            "message": {"role": "user", "content": text}, **extra}


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


@pytest.fixture
def entries():
    return [
        {"type": "bridge-session", "sessionId": SESSION, "bridgeSessionId": "cse_ABC"},
        user("<command-name>/clear</command-name>", "2026-10-02T08:00:00Z"),
        user("Сделай флоу закрытия треда", "2026-10-02T08:00:01Z"),
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
                    tool("RemoteTrigger", {"action": "list"}, "t4"),
                    tool("RemoteTrigger", {"action": "update", "trigger_id": "trig_1", "body": {}}, "t5"),
                    tool("mcp__claude_ai_ClickUp__clickup_create_task", {"name": "x"}, "t6"),
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
    assert [p["text"] for p in inv["prompts"]] == ["Сделай флоу закрытия треда"]
    assert inv["title"] == "Thread close" and inv["bridge_sessions"] == ["cse_ABC"]
    assert [(r["kind"], r["number"]) for r in inv["github_refs"]] == [("issues", 3), ("pull", 7)]
    assert f"{SCRATCH}/plan.md" in inv["files_written"]
    assert inv["memory_writes"] == ["/home/u/.claude/projects/p/memory/fact.md"]
    assert f"{SCRATCH}/plan.md" in inv["scratchpad_paths"]
    assert len(inv["git_mutations"]) == 1  # git status alone is not a mutation
    writes = [(w["tool"], w["summary"]) for w in inv["external_writes"]]
    assert ("RemoteTrigger", "update trig_1") in writes
    assert any(tool == "mcp__claude_ai_ClickUp__clickup_create_task" for tool, _ in writes)
    assert not any("clickup_get_task" in tool or summary == "list" for tool, summary in writes)
    assert "/work/other" in inv["directories"]  # a relative cd is resolved against the entry's cwd


def test_inventory_tracks_background_tasks_and_corrections(entries):
    inv = thread_inventory.inventory(entries)
    tasks = {task["id"]: task for task in inv["background_tasks"]}
    assert tasks["bg1"]["ended"]["status"] == "completed"
    assert tasks["bg2"]["ended"] is None and tasks["bg2"]["description"] == "never finishes"
    snippets = " ".join(c["snippet"] for c in inv["corrections"])
    assert "неверным" in snippets and "My mistake" in snippets


def test_cli_reads_transcripts_and_resolves_git_roots(entries, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for entry in entries:
        if entry.get("cwd") == "/work/repo":
            entry["cwd"] = str(repo)
    transcript = tmp_path / f"{SESSION}.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n", encoding="utf-8")
    assert thread_inventory.main(["--transcript", str(transcript)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unreadable_lines"] == 1
    assert out["git_roots"] == [str(repo.resolve())] or out["git_roots"] == [str(repo)]
    assert out["transcripts"] == [str(transcript)]


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


def test_credentials_are_masked_in_printed_text():
    entries = [
        user("use token=abc123secret please", "2026-10-02T09:00:00Z"),
        *assistant([tool("Bash", {"command": "curl -H 'Authorization: Bearer xyz.secret' https://x && git push "
                                             "https://ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/o/r"}, "t1")],
                   "2026-10-02T09:00:01Z", "msg_9"),
    ]
    printed = json.dumps(thread_inventory.inventory(entries))
    for secret in ("abc123secret", "xyz.secret", "ghp_abcdefghijklmnopqrstuvwxyz0123"):
        assert secret not in printed
    assert "[masked]" in printed
