"""Repository memory subsystem: describe + interaction history.

Provides four MCP tools (registered in src/server.py):
  - describe_repo:   read-only context and summary prompt
  - write_repo_summary: explicit write → managed section in CLAUDE.md
  - log_interaction: append-only intent/action/outcome log → history.md
  - read_history:    recent + lazy semantic recall over the log, as previews
                     that fit Claude Code's limit; one entry whole by its id
"""
