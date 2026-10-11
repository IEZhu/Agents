"""Persist complete, correlated persona tool diagnostics only when enabled."""

import asyncio
import json
import queue
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import src.server as server
from src.engine import persona
from src.schemas.protocol import PersonaDescriptor, RouterDecision
from src.utils import debug_logger


TOOLS = ("route_and_load", "get_agent_context", "refresh_persona_context")


def descriptor(revision="a" * 64, agent="software_engineer"):
    """Create a complete protocol descriptor for a previous activation."""
    return PersonaDescriptor(
        agent=agent, activation_id="existing-activation",
        bundle_revision=revision, scope="Implementation and debugging",
        skills_loaded=["skill-dev-clean-code"], implants_loaded=["IterBudget"],
        rules_loaded=["language-match"],
    )


@pytest.fixture
def diagnostics(tmp_path, monkeypatch):
    """Isolate debug files and persona assembly from the live installation."""
    root = tmp_path / "debug"
    monkeypatch.setattr(debug_logger, "AGENTS_DEBUG", True)
    monkeypatch.setattr(debug_logger, "_queue", None)
    monkeypatch.setattr(debug_logger, "get_debug_log_dir", lambda: root)
    monkeypatch.setattr(server, "_readiness_problem", AsyncMock(return_value=None))

    async def build(agent, query, history, tier=None, selection=None):
        """Return a full bundle large enough to reveal truncated snapshots."""
        return SimpleNamespace(
            agent=agent, bundle_revision="b" * 64, scope=f"Scope of {agent}",
            persona_block="Persona\n" + "Полная инструкция. " * 100,
            rules_block="Rules\n" + "Full rules. " * 100,
            skills_block="Skills\n" + "Full skills. " * 100,
            implants_block="Implants\n" + "Full implants. " * 100,
            skills_loaded=["skill-dev-clean-code"], implants_loaded=["IterBudget"],
            rules_loaded=["language-match"], tier="standard",
        )

    builder = AsyncMock(side_effect=build)
    monkeypatch.setattr(persona, "build_persona_bundle", builder)
    monkeypatch.setattr(server.router, "update_cache", AsyncMock())
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=RouterDecision(
        target_agent="software_engineer", confidence=1, reasoning="cached",
    )))
    monkeypatch.setattr(server.router, "keyword_veto", Mock(return_value=None))
    return root, builder


def arguments(tool, active=None, *, history=None, force_reload=False):
    """Build effective arguments with each tool's declared defaults."""
    args = {
        "query": "Проверь полную инструкцию\nwith a second line",
        "current_persona": active,
        "chat_history": history,
    }
    if tool != "refresh_persona_context":
        args["protocol_version"] = 2
    if tool == "get_agent_context":
        args.update(
            agent_name="software_engineer", reasoning="Selected by calling LLM",
            force_reload=force_reload,
        )
    return args


def read_pair(root, tool, response, expected_args):
    """Verify correlated snapshots preserve the exact inputs and response."""
    snapshots = [json.loads(path.read_text(encoding="utf-8")) for path in root.rglob("*.json")]
    assert len(snapshots) == 2
    assert {snapshot["tool"] for snapshot in snapshots} == {tool}
    assert {snapshot["dir"] for snapshot in snapshots} == {"req", "res"}
    request = next(snapshot["data"] for snapshot in snapshots if snapshot["dir"] == "req")
    result = next(snapshot["data"] for snapshot in snapshots if snapshot["dir"] == "res")
    call_id = request["call_id"]
    uuid.UUID(call_id)
    assert request == {"call_id": call_id, **expected_args}
    assert result == {"call_id": call_id, "response": response}
    return json.loads(result["response"])


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,representation,history", [
    ("route_and_load", "dict", ["First line", "Вторая строка"]),
    ("get_agent_context", "model", "  Keep raw history\nwith whitespace  "),
    ("refresh_persona_context", "dict", None),
])
async def test_success_snapshots_preserve_all_inputs_and_complete_bundle(
    diagnostics, tool, representation, history,
):
    """Save raw inputs and all bundle blocks without changing the response."""
    root, builder = diagnostics
    active = descriptor(agent="lawyer" if tool == "route_and_load" else "software_engineer")
    supplied = {
        "dict": active.model_dump(), "model": active,
    }[representation]
    args = arguments(tool, supplied, history=history, force_reload=True)
    if tool == "get_agent_context":
        args["reasoning"] = "Restore lost instructions\nincluding every block"
    response = await getattr(server, tool)(**args)
    expected = {**args, "current_persona": active.model_dump() if representation == "model" else supplied}
    payload = read_pair(root, tool, response, expected)

    assert payload["status"] == "SUCCESS"
    assert payload["replaces_activation_id"] == active.activation_id
    assert payload["persona"]["agent"] == "software_engineer"
    assert payload["persona"]["bundle_revision"] == "b" * 64
    assert payload["footer"] == persona.persona_footer(PersonaDescriptor.model_validate(payload["persona"]))
    for block in ("persona", "rules", "skills", "implants"):
        assert len(payload[f"{block}_block"]) > 1000
    builder.assert_awaited_once()
    normalized = [] if history is None else ([history.strip()] if isinstance(history, str) else history)
    assert builder.call_args.args[2] == normalized


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["route_and_load", "get_agent_context"])
async def test_default_arguments_are_present_in_request_snapshot(diagnostics, tool):
    """Include default values even when callers omit optional arguments."""
    root, _ = diagnostics
    if tool == "get_agent_context":
        response = await server.get_agent_context("software_engineer", "default arguments")
    else:
        response = await server.route_and_load("default arguments")
    expected = arguments(tool)
    expected["query"] = "default arguments"
    assert read_pair(root, tool, response, expected)["status"] == "SUCCESS"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,status", [
    ("route_and_load", "ROUTE_REQUIRED"),
    ("route_and_load", "NO_CHANGE"),
    ("get_agent_context", "NO_CHANGE"),
    ("refresh_persona_context", "NO_CHANGE"),
])
async def test_non_success_results_are_saved_whole(diagnostics, monkeypatch, tool, status):
    """Preserve routing and unchanged results with their full attribution."""
    root, _ = diagnostics
    active = descriptor(revision="b" * 64 if tool == "refresh_persona_context" else "a" * 64)
    args = arguments(tool, active.model_dump())
    if status == "ROUTE_REQUIRED":
        monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=None))
    response = await getattr(server, tool)(**args)
    payload = read_pair(root, tool, response, args)
    assert payload["status"] == status
    assert payload["replaces_activation_id"] == active.activation_id
    if status == "NO_CHANGE":
        assert payload["persona"] == active.model_dump()
        assert payload["footer"] == persona.persona_footer(active)
    else:
        assert payload["candidates"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["route_and_load", "get_agent_context"])
async def test_unsupported_protocol_logs_request_and_error_before_readiness(diagnostics, tool):
    """Record an invalid protocol without waiting for persona readiness."""
    root, builder = diagnostics
    args = arguments(tool, descriptor().model_dump())
    args["protocol_version"] = 3
    response = await getattr(server, tool)(**args)
    payload = read_pair(root, tool, response, args)
    assert payload["status"] == "ERROR"
    assert "protocol_version=2" in payload["message"]
    server._readiness_problem.assert_not_awaited()
    builder.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("problem", ["warming_up", "store is corrupt"])
async def test_readiness_errors_include_both_snapshots(diagnostics, tool, problem):
    """Pair readiness errors with the request that could not be served."""
    root, builder = diagnostics
    server._readiness_problem.return_value = problem
    args = arguments(tool, descriptor().model_dump())
    response = await getattr(server, tool)(**args)
    payload = read_pair(root, tool, response, args)
    assert payload["status"] == "ERROR"
    assert problem in payload["message"]
    builder.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_malformed_descriptor_is_logged_without_hiding_raw_input(diagnostics, tool):
    """Save invalid descriptor fields together with the validation error."""
    root, builder = diagnostics
    malformed = {**descriptor().model_dump(), "bundle_revision": "not-a-sha256"}
    args = arguments(tool, malformed)
    response = await getattr(server, tool)(**args)
    payload = read_pair(root, tool, response, args)
    assert payload["status"] == "ERROR"
    assert "bundle_revision" in payload["message"]
    builder.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_invalid_string_descriptor_is_preserved_in_error_snapshot(diagnostics, tool):
    """Keep unsupported raw JSON strings intact in error diagnostics."""
    root, builder = diagnostics
    args = arguments(tool, descriptor().model_dump_json())
    response = await getattr(server, tool)(**args)
    payload = read_pair(root, tool, response, args)
    assert payload["status"] == "ERROR"
    assert "invalid: persona" in payload["message"]
    builder.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_bundle_is_logged_without_inventing_activation(diagnostics):
    """Log a bundle failure without creating a successful activation."""
    root, builder = diagnostics
    builder.side_effect = FileNotFoundError("Missing required rule")
    args = arguments("get_agent_context", descriptor().model_dump(), force_reload=True)
    response = await server.get_agent_context(**args)
    payload = read_pair(root, "get_agent_context", response, args)
    assert payload["status"] == "ERROR"
    assert "Missing required rule" in payload["message"]
    assert "persona" not in payload and "persona_block" not in payload


@pytest.mark.asyncio
async def test_concurrent_calls_have_distinct_matching_ids(diagnostics):
    """Match concurrent responses to requests using distinct diagnostic IDs."""
    root, _ = diagnostics
    requests = [arguments("get_agent_context", force_reload=True) for _ in range(2)]
    requests[0]["query"], requests[1]["query"] = "first conversation", "second conversation"
    responses = await asyncio.gather(*(server.get_agent_context(**args) for args in requests))
    snapshots = [json.loads(path.read_text(encoding="utf-8")) for path in root.rglob("*.json")]
    assert len(snapshots) == 4
    req = {snapshot["data"]["call_id"]: snapshot["data"] for snapshot in snapshots if snapshot["dir"] == "req"}
    res = {snapshot["data"]["call_id"]: snapshot["data"] for snapshot in snapshots if snapshot["dir"] == "res"}
    assert len(req) == 2 and req.keys() == res.keys()
    expected = dict(zip((args["query"] for args in requests), responses))
    for call_id, request in req.items():
        uuid.UUID(call_id)
        assert res[call_id]["response"] == expected[request["query"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_persona_snapshots_run_outside_event_loop_in_request_response_order(
    diagnostics, monkeypatch, tool,
):
    """Write both snapshots off the event-loop thread before returning the response."""
    root, _ = diagnostics
    event_loop_thread = threading.get_ident()
    original_debug_log = server.debug_log
    calls = []

    def record_debug_log(name, direction, data):
        """Record the writing thread while preserving the real JSON snapshots."""
        calls.append((threading.get_ident(), name, direction, data))
        original_debug_log(name, direction, data)

    monkeypatch.setattr(server, "debug_log", record_debug_log)
    active = None if tool == "route_and_load" else descriptor().model_dump()
    args = arguments(tool, active, force_reload=True)
    response = await getattr(server, tool)(**args)

    assert [(name, direction) for _, name, direction, _ in calls] == [(tool, "req"), (tool, "res")]
    assert all(thread_id != event_loop_thread for thread_id, _, _, _ in calls)
    assert calls[0][3]["call_id"] == calls[1][3]["call_id"]
    assert calls[1][3]["response"] == response
    assert read_pair(root, tool, response, args)["status"] == "SUCCESS"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_disabled_logging_does_not_write_or_change_tool_result(diagnostics, monkeypatch, tool):
    """Leave successful persona responses intact when diagnostics are off."""
    root, _ = diagnostics
    writer = Mock(side_effect=AssertionError("disabled debug logging must not write"))
    monkeypatch.setattr(debug_logger, "AGENTS_DEBUG", False)
    monkeypatch.setattr(debug_logger, "_write_debug", writer)
    active = None if tool == "route_and_load" else descriptor().model_dump()
    args = arguments(tool, active, force_reload=True)
    response = await getattr(server, tool)(**args)
    assert json.loads(response)["status"] == "SUCCESS"
    writer.assert_not_called()
    assert not root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_failed_debug_write_does_not_change_tool_result(diagnostics, monkeypatch, caplog, tool):
    """Retain persona results when the synchronous writer cannot write."""
    root, _ = diagnostics
    monkeypatch.setattr(debug_logger, "get_debug_log_dir", Mock(side_effect=OSError("unwritable")))
    active = None if tool == "route_and_load" else descriptor().model_dump()
    response = await getattr(server, tool)(**arguments(tool, active, force_reload=True))
    assert json.loads(response)["status"] == "SUCCESS"
    assert not root.exists()
    assert "failed to write" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
async def test_http_directory_errors_do_not_change_tool_result(
    diagnostics, monkeypatch, caplog, tool, error_type,
):
    """Keep HTTP request and response diagnostics from breaking persona calls."""
    root, _ = diagnostics
    pending = queue.Queue(maxsize=256)
    directory = Mock(side_effect=error_type("invalid service directory"))
    monkeypatch.setattr(debug_logger, "_queue", pending)
    monkeypatch.setattr(debug_logger, "queue", queue, raising=False)
    monkeypatch.setattr(debug_logger, "state_dir", directory, raising=False)
    active = None if tool == "route_and_load" else descriptor().model_dump()
    response = await getattr(server, tool)(**arguments(tool, active, force_reload=True))
    assert json.loads(response)["status"] == "SUCCESS"
    directory.assert_called()
    assert pending.empty() and not root.exists()
    assert "persona debug request snapshot failed" in caplog.text
    assert "persona debug response snapshot failed" in caplog.text
