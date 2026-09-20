"""The dialogue evaluator must fail closed; these do not simulate model quality."""
import json

import pytest

from evals.runners import run_persona_dialogues as runner
from evals.runners.run_persona_dialogues import assess_turn, client_output, payload_from, turn_input


def activation(agent="software_engineer", activation_id="a", replaces=None):
    persona = {"agent": agent, "activation_id": activation_id, "bundle_revision": "a" * 64,
               "scope": "Engineering", "skills_loaded": [], "implants_loaded": [], "rules_loaded": []}
    return {"tool": "get_agent_context", "arguments": {"agent_name": agent, "protocol_version": 2},
            "result": {"status": "SUCCESS", "protocol_version": 2, "persona": persona,
                       "replaces_activation_id": replaces, "persona_block": "persona", "rules_block": "rules",
                       "skills_block": "", "implants_block": "", "footer": f"**Agent**: {agent}"}, "result_bytes": 42}


def output(agent="software_engineer", calls=None):
    return {"answer": f"Cedar **Agent**: {agent}", "attempted_tools": calls or [], "client_errors": []}


def test_claude_final_does_not_inherit_facts_from_prior_text():
    events = [{"type": "assistant", "message": {"model": "some-model", "content": [{"type": "text", "text": "Cedar **Agent**: software_engineer"}]}},
              {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__Agents_Core__log_interaction"}]}},
              {"type": "result", "result": "Answer delivered above."}]
    actual = client_output(events)
    assert actual["answer"] == "Answer delivered above."
    assert "Cedar" in actual["visible_messages"][0]
    assert actual["models"] == ["some-model"]
    assert actual["attempted_tools"] == ["log_interaction"]


def test_codex_denied_calls_are_errors_and_attempts():
    actual = client_output([{"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "route_and_load", "error": {"message": "approval denied"}}}])
    assert actual["client_errors"]
    assert actual["attempted_tools"] == ["route_and_load"]


def test_nested_fastmcp_result_is_unwrapped():
    expected = {"status": "SUCCESS", "agent": "software_engineer"}
    assert payload_from([[{"type": "text", "text": json.dumps(expected)}], expected]) == expected


def test_role_name_in_answer_does_not_prove_activation():
    result, active = assess_turn({"expected": "load", "agent": "software_engineer", "direct": True}, [], output(), None, 2)
    assert not result["passed"]
    assert active is None
    assert "missing_successful_load" in result["failures"]


def logged(action="switch"):
    return {"tool": "log_interaction", "arguments": {"agent_name": "software_engineer", "persona": activation()["result"]["persona"], "persona_action": action, "response_content": "Cedar **Agent**: software_engineer"}, "result": {"request_id": "request", "history": {"status": "recorded"}}}


def test_successful_activation_and_keep():
    loaded, active = assess_turn({"expected": "load", "agent": "software_engineer", "direct": True}, [activation(), logged()], output(), None, 2)
    assert loaded["passed"]
    kept, same = assess_turn({"expected": "keep", "agent": "software_engineer", "facts": ["Cedar"]}, [logged("keep")], output(), active, 2)
    assert kept["passed"]
    assert same == active


def test_keep_cannot_pass_after_failed_initial_load():
    result, _ = assess_turn({"expected": "keep", "agent": "software_engineer"}, [], output(), None, 2)
    assert not result["passed"]
    assert "keep_without_same_active_persona" in result["failures"]


def test_catalog_or_denied_routing_invalidates_keep():
    active = activation()["result"]["persona"]
    for tool in ["list_agents", "load_implants", "route_and_load", "refresh_persona_context"]:
        result, _ = assess_turn({"expected": "keep", "agent": "software_engineer"}, [], output(calls=[tool]), active, 2)
        assert "unnecessary_selection_or_enrichment" in result["failures"]


def test_stale_activation_fails_even_when_correct_agent():
    active = activation()["result"]["persona"]
    result, retained = assess_turn({"expected": "switch", "agent": "lawyer"}, [activation("lawyer", "b", "stale")], output("lawyer"), active, 2)
    assert "activation_chain_mismatch" in result["failures"]
    assert "missing_successful_load" in result["failures"]
    assert retained is active
    assert not result["observed_switch"]


@pytest.mark.parametrize("field", ["persona_block", "rules_block", "skills_block", "implants_block", "footer"])
@pytest.mark.parametrize("invalid", ["missing", None, 7])
def test_incomplete_bundle_preserves_previous_activation(field, invalid):
    previous = activation()["result"]
    active = {**previous["persona"], "footer": previous["footer"]}
    call = activation("lawyer", "b", "a")
    if invalid == "missing":
        del call["result"][field]
    else:
        call["result"][field] = invalid
    result, retained = assess_turn({"expected": "switch", "agent": "lawyer"}, [call], output("lawyer"), active, 2)
    assert "incomplete_bundle" in result["failures"]
    assert "missing_successful_load" in result["failures"]
    assert not result["observed_switch"]
    assert retained is active
    kept, same = assess_turn({"expected": "keep", "agent": "software_engineer"}, [logged("keep")], output(), retained, 2)
    assert kept["passed"]
    assert same is active


@pytest.mark.parametrize("field", ["persona_block", "footer"])
@pytest.mark.parametrize("blank", ["", " \n\t"])
def test_empty_persona_or_footer_cannot_activate_initial_persona(field, blank):
    call = activation()
    call["result"][field] = blank
    verdict, active = assess_turn({"expected": "load", "agent": "software_engineer", "direct": True}, [call], output(), None, 2)
    assert "incomplete_bundle" in verdict["failures"]
    assert "missing_successful_load" in verdict["failures"]
    assert active is None


@pytest.mark.parametrize("field", list(activation()["result"]["persona"]))
def test_incomplete_descriptor_cannot_activate(field):
    call = activation()
    del call["result"]["persona"][field]
    verdict, active = assess_turn({"expected": "load", "agent": "software_engineer", "direct": True}, [call], output(), None, 2)
    assert "incomplete_descriptor" in verdict["failures"]
    assert "missing_successful_load" in verdict["failures"]
    assert active is None


@pytest.mark.parametrize("field,value", [
    ("agent", "invalid agent"), ("activation_id", ""), ("bundle_revision", "not-a-revision"),
    ("scope", ""), ("skills_loaded", None), ("implants_loaded", "implant"), ("rules_loaded", [7]),
])
def test_malformed_descriptor_cannot_activate(field, value):
    call = activation()
    call["result"]["persona"][field] = value
    verdict, active = assess_turn({"expected": "load", "agent": "software_engineer", "direct": True}, [call], output(), None, 2)
    assert "incomplete_descriptor" in verdict["failures"]
    assert "missing_successful_load" in verdict["failures"]
    assert active is None


def test_rejected_success_does_not_break_following_valid_activation_chain():
    active = activation()["result"]["persona"]
    rejected = activation("lawyer", "rejected", "stale")
    accepted = activation("lawyer", "accepted", "a")
    verdict, result = assess_turn({"expected": "switch", "agent": "lawyer"}, [rejected, accepted], output("lawyer"), active, 2)
    assert not verdict["passed"]  # The rejected response still fails this turn.
    assert "activation_chain_mismatch" in verdict["failures"]
    assert "missing_successful_load" not in verdict["failures"]
    assert result["activation_id"] == "accepted"
    assert verdict["observed_switch"]


def test_invalid_refresh_success_preserves_activation():
    active = activation()["result"]["persona"]
    call = activation(activation_id="b", replaces="a")
    call["tool"] = "refresh_persona_context"
    del call["result"]["footer"]
    verdict, retained = assess_turn({"expected": "refresh", "agent": "software_engineer"}, [call], output(), active, 2)
    assert "missing_successful_refresh" in verdict["failures"]
    assert "incomplete_bundle" in verdict["failures"]
    assert retained is active


def test_tool_error_cannot_install_success_payload():
    call = activation()
    call["error"] = "Transport failed"
    verdict, active = assess_turn({"expected": "load", "agent": "software_engineer", "direct": True}, [call], output(), None, 2)
    assert "server_tool_error" in verdict["failures"]
    assert "missing_successful_load" in verdict["failures"]
    assert active is None


def test_duplicate_history_write_counts_as_recorded():
    duplicate = logged()
    duplicate["result"]["history"]["status"] = "duplicate"
    verdict, _ = assess_turn({"expected": "load", "agent": "software_engineer", "direct": True}, [activation(), logged(), duplicate], output(), None, 2)
    assert verdict["passed"]


def test_no_change_cannot_count_as_switch():
    active = activation()["result"]["persona"]
    call = activation()
    call["result"]["status"] = "NO_CHANGE"
    result, _ = assess_turn({"expected": "switch", "agent": "lawyer"}, [call], output("lawyer"), active, 2)
    assert "missing_successful_load" in result["failures"]


def test_changed_footer_or_lost_fact_fails():
    actual = output()
    actual["answer"] = "**Agent**: software_engineer"
    call = activation()
    call["result"]["footer"] += " · **Skills**: real-skill"
    result, _ = assess_turn({"expected": "load", "agent": "software_engineer", "facts": ["Cedar"]}, [call], actual, None, 2)
    assert "missing_conversation_fact" in result["failures"]
    assert "footer_differs_from_bundle" in result["failures"]


def test_missing_or_wrong_attribution_fails():
    turn = {"expected": "load", "agent": "software_engineer", "direct": True}
    result, _ = assess_turn(turn, [activation()], output(), None, 2)
    assert "missing_attribution_log" in result["failures"]
    wrong = logged("keep")
    wrong["arguments"]["persona"] = {"agent": "wrong"}
    result, _ = assess_turn(turn, [activation(), wrong], output(), None, 2)
    assert "log_descriptor_mismatch" in result["failures"]
    assert "log_action_mismatch" in result["failures"]


def test_refresh_requires_valid_success_or_no_change():
    active = activation()["result"]["persona"]
    turn = {"expected": "refresh", "agent": "software_engineer"}
    for payload in ({}, {"status": "ROUTE_REQUIRED"}, {"status": "SUCCESS", "protocol_version": 1}):
        call = {"tool": "refresh_persona_context", "arguments": {}, "result": payload}
        result, _ = assess_turn(turn, [call, logged("refresh")], output(), active, 2)
        assert "missing_successful_refresh" in result["failures"]


def test_no_change_must_preserve_entire_descriptor():
    active = activation()["result"]["persona"]
    call = activation()
    call["tool"] = "refresh_persona_context"
    call["result"]["status"] = "NO_CHANGE"
    call["result"]["persona"]["bundle_revision"] = "silently-changed"
    result, _ = assess_turn({"expected": "refresh", "agent": "software_engineer"}, [call], output(), active, 2)
    assert "no_change_changed_activation" in result["failures"]


def test_codex_final_does_not_inherit_footer_from_progress():
    actual = client_output([
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Cedar **Agent**: software_engineer"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "I forgot the name."}},
    ])
    verdict, _ = assess_turn({"expected": "load", "agent": "software_engineer", "facts": ["Cedar"]}, [activation(), logged()], actual, None, 2)
    assert "missing_conversation_fact" in verdict["failures"]
    assert "wrong_footer_agent" in verdict["failures"]


def test_simulated_compaction_retains_only_requested_state():
    active = {**activation()["result"]["persona"], "footer": "footer"}
    base = {"query": "Continue.", "summary": "Cedar uses Python."}
    query, reset, state = turn_input({**base, "context_reset": "known_persona"}, active)
    assert reset and state == active
    assert '"activation_id": "a"' in query
    assert "footer" not in query and "get_agent_context" not in query
    query, reset, state = turn_input({**base, "context_reset": "unknown_persona"}, active)
    assert reset and state is None
    assert "software_engineer" not in query and "route_and_load" not in query


def test_failed_history_sink_and_unlogged_final_fail():
    call = logged()
    call["result"]["history"]["status"] = "error"
    call["arguments"]["response_content"] = "A different response."
    result, _ = assess_turn({"expected": "load", "agent": "software_engineer", "direct": True}, [activation(), call], output(), None, 2)
    assert "log_not_recorded" in result["failures"]
    assert "log_response_differs_from_final" in result["failures"]


def test_sampled_v2_response_never_counts_as_refresh():
    call = activation()
    call["tool"] = "refresh_persona_context"
    call["result"]["status"] = "SUCCESS_SAMPLED"
    result, _ = assess_turn({"expected": "refresh", "agent": "software_engineer"}, [call, logged("refresh")], output(), activation()["result"]["persona"], 2)
    assert "sampling_in_v2" in result["failures"]
    assert "missing_successful_refresh" in result["failures"]


def test_unknown_initial_role_requires_routing():
    result, _ = assess_turn({"expected": "load", "agent": "software_engineer"}, [activation(), logged()], output(), None, 2)
    assert "initial_selection_skipped_routing" in result["failures"]
    routed = activation()
    routed["tool"] = "route_and_load"
    result, _ = assess_turn({"expected": "load", "agent": "software_engineer"}, [routed, logged()], output(), None, 2)
    assert result["passed"]


@pytest.mark.parametrize("platform,relative", [
    ("win32", "Scripts/python.exe"), ("linux", "bin/python"), ("darwin", "bin/python"),
])
def test_project_interpreter_selects_platform_virtualenv(monkeypatch, tmp_path, platform, relative):
    monkeypatch.setattr(runner.sys, "platform", platform)
    executable = tmp_path / ".venv" / relative
    executable.parent.mkdir(parents=True)
    executable.touch(mode=0o700)
    assert runner.project_interpreter(tmp_path) == str(executable)


def test_missing_project_interpreter_fails_before_client_or_case_setup(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner.sys, "argv", [
        "run_persona_dialogues", "--client", "claude", "--out", str(tmp_path / "out"),
        "--seed-data", str(tmp_path / "seed"),
    ])

    def unexpected_start(*args, **kwargs):
        pytest.fail("A missing virtualenv must fail before starting clients or workers")

    monkeypatch.setattr(runner.subprocess, "run", unexpected_start)
    monkeypatch.setattr(runner, "ThreadPoolExecutor", unexpected_start)
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2
    assert "Project virtualenv interpreter not found" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_nonexecutable_project_interpreter_is_a_setup_error(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.sys, "platform", "linux")
    executable = tmp_path / ".venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(runner.os, "access", lambda path, mode: False)
    with pytest.raises(PermissionError, match="not executable"):
        runner.project_interpreter(tmp_path)
