"""run_flow and get_flow declare a result size, so Claude Code keeps their results in the conversation
instead of saving one over 50,000 characters to a file (#196)."""
import pytest

import src.server as server
from src.flows import MAX_FLOW_BYTES

KEY = "anthropic/maxResultSizeChars"


@pytest.mark.asyncio
async def test_the_flow_tools_declare_their_result_size_in_tools_list_and_no_other_tool_does():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    for name in ("run_flow", "get_flow"):
        assert tools[name].model_dump(by_alias=True)["_meta"] == {KEY: 500_000}, name
    assert {name for name, tool in tools.items() if tool.meta} == {"run_flow", "get_flow"}


def test_the_size_is_derived_from_the_flow_limit_and_capped_at_claude_codes_ceiling():
    # Two texts (a flow and its built-in, or a flow and its persona bundle), each at most doubled by
    # JSON escaping.
    assert server.flow_result_size(16 * 1024) == 4 * 16 * 1024
    assert server.flow_result_size(MAX_FLOW_BYTES) == min(500_000, 4 * MAX_FLOW_BYTES) == 500_000
    assert server.flow_result_size(10 ** 7) == server.RESULT_SIZE_CEILING == 500_000
