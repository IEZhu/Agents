# Contributing

The contributor instructions of this repository live in [AGENTS.md](AGENTS.md),
which people and AI assistants read alike. Start there; this page is the short
index GitHub shows.

- **Read first**: [CLAUDE.md](CLAUDE.md). Its managed section is the persona
  protocol for this checkout, and its repository notes describe the code.
- **Where to work**: never edit, commit or switch branches in the installation
  checkout the daemon runs from. Cut a linked worktree from `origin/dev` and open
  the pull request against `dev` ([AGENTS.md](AGENTS.md#make-changes)).
- **Commits**: Conventional Commits (`type(scope): subject`), imperative subject,
  body wrapped at 72 characters, one change per commit.
- **Tests**: `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -q`;
  the [test guide](tests/README.md) covers the environment and focused selections.
- **Documentation**: every committed document is in English. The
  [documentation map](docs/README.md) says where a subject belongs, and
  `.venv/bin/python scripts/check_docs.py` checks links and the tool index.
- **Review and merge**: the [PR/MR review flow](flows/pr-review.md) states the
  description, bot review and merge conditions.
