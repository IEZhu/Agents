"""Every MCP tool carries a human title and the hints Claude's plugin directory checks (#362)."""
import asyncio

import src.server as server

# Their changes reach the user library or the history, which user sync pushes to a remote.
SYNCED = {"save_flow", "delete_flow", "set_flow_persona", "log_interaction"}


def tools():
    return asyncio.run(server.mcp.list_tools())


def test_every_tool_has_a_title_and_a_read_only_or_destructive_hint():
    listed = tools()
    assert listed
    for tool in listed:
        annotations = tool.annotations
        assert tool.title and annotations is not None and annotations.title == tool.title, tool.name
        assert annotations.readOnlyHint is not None, tool.name
        if not annotations.readOnlyHint:
            assert annotations.destructiveHint is not None, tool.name
        assert annotations.openWorldHint is (tool.name in SYNCED), tool.name


def test_hints_cover_exactly_the_registered_tools():
    assert {tool.name for tool in tools()} == set(server.TOOL_HINTS)


def test_tools_that_delete_or_replace_are_destructive_and_readers_are_read_only():
    hints = {tool.name: tool.annotations for tool in tools()}
    for name in ("delete_flow", "clear_session_cache", "describe_repo", "write_repo_summary"):
        assert hints[name].readOnlyHint is False and hints[name].destructiveHint is True, name
    for name in ("list_flows", "get_flow", "list_agents", "read_history", "validate_subagent_result"):
        assert hints[name].readOnlyHint is True, name
