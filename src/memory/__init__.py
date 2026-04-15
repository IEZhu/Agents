"""Repository memory subsystem: describe + history.

Provides three MCP tools (registered in src/server.py):
  - describe_repo:  one-shot bootstrap → managed section in CLAUDE.md
  - record_history: append-only intent/action/outcome log → history.md
  - read_history:   recent + lazy semantic recall over the log
"""
