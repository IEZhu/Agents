"""run_flow and get_flow declare a result size, so Claude Code keeps their results in the conversation
instead of saving one over 50,000 characters to a file (#196)."""
import json

import pytest

import src.server as server
from src.flows import MAX_FLOW_BYTES, FlowCatalog
from src.result_size import INLINE_LIMIT
from src.user_flows import FlowLibrary

KEY = "anthropic/maxResultSizeChars"
UNDECLARED = INLINE_LIMIT  # Claude Code's threshold for a tool that declares nothing


@pytest.mark.asyncio
async def test_only_the_flow_tools_declare_a_result_size():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    declared = {name: tool.meta[KEY] for name, tool in tools.items() if KEY in (tool.meta or {})}
    assert declared == {"run_flow": 500_000, "get_flow": 500_000}
    for name in declared:
        assert tools[name].model_dump(by_alias=True)["_meta"][KEY] == 500_000
        # The output schema stays: a client that listed the tools before an update keeps validating results.
        assert tools[name].outputSchema is not None


@pytest.mark.parametrize("flow_bytes", [1024, 8 * 1024, 20 * 1024])
def test_the_size_covers_get_flow_of_a_local_copy_and_its_built_in_at_that_flow_size(tmp_path, flow_bytes):
    # The longest escape per byte, in a flow that is all heading: its title repeats the whole flow.
    flow = "# " + "\x01" * (flow_bytes - 3) + "\n"
    install = tmp_path / "flows"
    install.mkdir()
    (install / "review.md").write_text(flow, encoding="utf-8")
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "user")
    library.save("review", flow, override=True)
    result = json.dumps(library.get("user:review"), ensure_ascii=False)
    assert len(result) > 3 * 6 * (flow_bytes - 3)  # the title, the copy and the built-in
    assert len(result) <= server.flow_result_size(flow_bytes) < server.RESULT_SIZE_CEILING


def test_the_size_stays_above_claude_codes_default_and_at_most_its_ceiling():
    # Declaring less than the default would lower Claude Code's threshold; it accepts no more than the ceiling.
    assert server.flow_result_size(1) > UNDECLARED
    assert server.flow_result_size(MAX_FLOW_BYTES) == server.flow_result_size(10 ** 7) == server.RESULT_SIZE_CEILING
    assert server.RESULT_SIZE_CEILING == 500_000
