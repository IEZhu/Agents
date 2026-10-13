"""Tell the installers whether to skip Claude Code's Agents-Core registration (#367).

Usage: python claude_code_plugin.py [--force-claude-code-mcp]

Exit 0 and print the reason when the agents-core Claude Code plugin is installed and enabled, so the
installer writes no Agents-Core entry for Claude Code; exit 1, printing nothing, to write it as
before: no plugin, a disabled one, unreadable plugin state, or a forced registration
(``--force-claude-code-mcp`` or ``AGENTS_FORCE_CLAUDE_CODE_MCP=1``). Reads Claude Code's plugin
state only (``src/claude_plugin.py``).
"""
from pathlib import Path
import sys


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from src.claude_plugin import FORCE_FLAG, skip_reason
        reason = skip_reason(force=FORCE_FLAG in arguments)
    except Exception:  # noqa: BLE001 - fail safe: register as before
        return 1
    if not reason:
        return 1
    print(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
