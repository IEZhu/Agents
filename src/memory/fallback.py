"""The fallback journal: where a turn goes when no project resolves (#292).

`log_interaction` used to answer `workspace_required` and drop the turn whenever a session
named no registered project. It now appends the turn here instead, a `history.md` in the
installation's private state, with the directory the client named as unverified metadata.
Nothing is ever written to that directory: only a registered workspace receives memory.
"""
from dataclasses import dataclass
from pathlib import Path

# Tests point this elsewhere so that they never write the installation's journal.
ROOT_OVERRIDE: Path | None = None

REMEDY = ("To keep history in the project, start the session inside it (a directory with .git or "
          "CLAUDE.md) or register it once with `python -m src.daemon migrate --workspace "
          "/absolute/project`, then reconnect MCP; over stdio, set AGENTS_CLIENT_REPO_ROOT.")


@dataclass(frozen=True)
class Unresolved:
    """A call whose project did not resolve: why, and the directory its client named."""
    root: Path            # the fallback journal's directory
    reason: str           # the workspace error's code, such as workspace_required
    claimed: str | None   # unverified; metadata only

    @property
    def history_path(self) -> Path:
        return self.root / "history.md"

    def report(self) -> dict:
        return {"root": None, "source": "fallback", "claimed": self.claimed, "reason": self.reason,
                "journal": str(self.history_path)}

    def metadata(self) -> dict:
        metadata = {"unresolved": self.reason}
        if self.claimed:
            metadata["claimed_workspace"] = self.claimed
        return metadata


def journal_root() -> Path:
    """The fallback journal's directory, created private on first use."""
    if ROOT_OVERRIDE is not None:
        Path(ROOT_OVERRIDE).mkdir(parents=True, exist_ok=True)
        return Path(ROOT_OVERRIDE)
    from src.daemon.state import private_dir, state_dir
    return private_dir(state_dir() / "unresolved")


def unresolved(error, claimed=None) -> Unresolved:
    """The fallback for a workspace error; OSError when the journal's directory cannot be made."""
    code = str(error).split(":", 1)[0].strip() or "workspace_required"
    claimed = claimed.strip() if isinstance(claimed, str) and claimed.strip() else None
    return Unresolved(journal_root(), code, claimed)


def matches(entry_metadata, claimed) -> bool:
    """Whether a fallback entry belongs to the same claimed directory; every entry without a claim."""
    return not claimed or (entry_metadata or {}).get("claimed_workspace") == claimed
