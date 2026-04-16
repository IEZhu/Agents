"""Repository memory subsystem: describe + interaction history.

Provides three MCP tools (registered in src/server.py):
  - describe_repo:   one-shot bootstrap → managed section in CLAUDE.md
  - log_interaction: append-only intent/action/outcome log → history.md
  - read_history:    recent + lazy semantic recall over the log
"""
