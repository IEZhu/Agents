"""run_flow and get_flow declare a result size, so Claude Code keeps their results in the conversation
instead of saving one over 50,000 characters to a file, and they return their JSON text alone (#196)."""
import json

import pytest

import src.server as server
from src.flows import MAX_FLOW_BYTES

KEY = "anthropic/maxResultSizeChars"


@pytest.mark.asyncio
async def test_only_the_flow_tools_declare_a_result_size_and_they_return_text_alone():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    declared = {name: tool.meta[KEY] for name, tool in tools.items() if KEY in (tool.meta or {})}
    assert declared == {"run_flow": 500_000, "get_flow": 500_000}
    for name in declared:
        assert tools[name].model_dump(by_alias=True)["_meta"][KEY] == 500_000
        # With structured output Claude Code would show {"result": "<the JSON>"}: the flow escaped twice.
        assert tools[name].outputSchema is None


def largest_get_flow_result(max_flow_bytes: int) -> str:
    """A local copy and its built-in at the flow limit, in the characters that escape longest, with many versions."""
    text = "\x01" * max_flow_bytes  # json.dumps writes each as \u0001
    versions = [{"version": f"20261006T000000000000Z-{number:012x}", "revision_prefix": f"{number:012x}",
                 "deleted": False} for number in range(300)]
    return json.dumps({"status": "success", "flow": {"id": "user:review", "title": "T" * 200, "source": "user",
                                                     "revision": "a" * 64, "source_path": "/p" * 100},
                       "content": text, "history": versions, "upstream": {"content": text, "revision": "b" * 64}},
                      ensure_ascii=False)


@pytest.mark.parametrize("max_flow_bytes", [1, 4 * 1024, 16 * 1024, 30 * 1024])
def test_the_declared_size_covers_two_texts_at_the_flow_limit(max_flow_bytes):
    declared = server.flow_result_size(max_flow_bytes)
    assert len(largest_get_flow_result(max_flow_bytes)) <= declared
    assert server.UNDECLARED_RESULT_SIZE <= declared <= server.RESULT_SIZE_CEILING


def test_the_size_never_falls_below_claude_codes_default_or_rises_above_its_ceiling():
    # Declaring less than the default would lower Claude Code's threshold; it accepts no more than the ceiling.
    assert server.flow_result_size(1) > server.UNDECLARED_RESULT_SIZE == 50_000
    assert server.flow_result_size(MAX_FLOW_BYTES) == server.flow_result_size(10 ** 7) == 500_000
