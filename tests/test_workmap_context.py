"""The hand-back measurement reads worker transcripts."""
import json

from evals.scripts.workmap_context import handbacks, summary


def transcript(path, *inputs):
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "SubagentHandback", "input": value}]}} for value in inputs]
    lines.insert(0, {"type": "user", "message": {"content": "start"}})
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_each_handback_is_counted_and_one_line_receipts_are_recognised(tmp_path):
    agents = tmp_path / "subagents"
    agents.mkdir()
    transcript(agents / "agent-a.jsonl", {"report": "task: t\n## summary\n" + "x" * 500})
    transcript(agents / "agent-b.jsonl", {"report": "submitted t/map-code 0123456789ab completed"})
    rows = handbacks(tmp_path)
    assert [row["agent"] for row in rows] == ["agent-a", "agent-b"]
    assert rows[1]["line"] is True and rows[0]["line"] is False
    result = summary(rows)
    assert result["count"] == 2 and result["line_handbacks"] == 1
    assert result["total_chars"] == rows[0]["chars"] + rows[1]["chars"]


def assistant_text(path, *texts):
    content = [{"type": "text", "text": text} for text in texts]
    path.write_text(json.dumps({"type": "assistant", "message": {"content": content}}) + "\n", encoding="utf-8")


def test_workflow_transcripts_are_counted(tmp_path):
    workflow = tmp_path / "subagents" / "workflows" / "wf_1"
    workflow.mkdir(parents=True)
    transcript(workflow / "agent-w.jsonl", {"report": "submitted t/map-code 0123456789ab completed"})
    rows = handbacks(tmp_path)
    assert [row["agent"] for row in rows] == ["agent-w"]
    assert rows[0]["line"] is True


def test_plain_text_final_answer_is_counted_when_no_handback_was_called(tmp_path):
    agents = tmp_path / "subagents"
    agents.mkdir()
    text = "task: t\n## summary\nchecked the map"
    assistant_text(agents / "agent-p.jsonl", text)
    rows = handbacks(tmp_path)
    assert [(row["agent"], row["chars"], row["line"]) for row in rows] == [("agent-p", len(text), False)]


def test_chars_measure_the_delivered_string_not_the_escaped_json(tmp_path):
    agents = tmp_path / "subagents"
    agents.mkdir()
    text = 'say "hi"\nthere'
    transcript(agents / "agent-q.jsonl", {"message": text})
    rows = handbacks(tmp_path)
    assert rows[0]["chars"] == len(text)


def test_parent_transcript_tool_results_are_summed(tmp_path):
    from evals.scripts.workmap_context import parent_results

    project = tmp_path / "project"
    session = project / "session-1"
    session.mkdir(parents=True)
    result = '{"valid":true,"errors":[]}'
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__Agents-Core__validate_subagent_result", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": result},
            {"type": "tool_result", "tool_use_id": "t2", "content": "ignored output"}]}},
    ]
    lines.append(lines[1])  # the host repeats the result record; each call is counted once
    (project / "session-1.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    assert parent_results(session) == {"count": 1, "total_chars": len(result)}


def test_one_line_not_recorded_hand_back_is_a_work_map_receipt(tmp_path):
    agents = tmp_path / "subagents"
    agents.mkdir()
    transcript(agents / "agent-n.jsonl", {"report": "not recorded t/map-code 4003"})
    rows = handbacks(tmp_path)
    assert [(row["agent"], row["line"]) for row in rows] == [("agent-n", True)]
    assert summary(rows)["line_handbacks"] == 1
