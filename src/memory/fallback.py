"""The fallback journal: where a turn goes when no project resolves (#292).

`log_interaction` used to answer `workspace_required` and drop the turn whenever a session
named no registered project. It now appends the turn here instead, a `history.md` in the
installation's private state, with the directory the client named as unverified metadata.
Nothing is ever written to that directory: only a registered workspace receives memory.
"""
from dataclasses import dataclass
import os
from pathlib import Path

# Tests point this elsewhere so that they never write the installation's journal.
ROOT_OVERRIDE: Path | None = None
# A longer claim is no directory a client starts in; it is left out rather than cut.
MAX_CLAIM = 4096
# Names the journal's directory for user sync, which shares it under its own key (#292): it holds
# the directory's own path, so a copy elsewhere, or the same name in a repository, is no journal.
MARKER = ".agents-core-fallback"

REMEDY = ("To keep history in the project, start the session inside it (a directory with .git or "
          "CLAUDE.md) or register it once with `python -m src.daemon migrate --workspace "
          "/absolute/project`, then reconnect MCP; over stdio, set AGENTS_CLIENT_REPO_ROOT.")


@dataclass(frozen=True)
class Unresolved:
    """A call whose project did not resolve: why, and the directory its client named."""
    root: Path            # the fallback journal's directory
    reason: str           # the workspace error's code, such as workspace_required
    claimed: str | None   # unverified; metadata only
    detail: str = ""      # the whole error message: why the project did not resolve

    @property
    def history_path(self) -> Path:
        return self.root / "history.md"

    def report(self) -> dict:
        return {"root": None, "source": "fallback", "claimed": self.claimed, "reason": self.reason,
                "detail": self.detail, "journal": str(self.history_path)}

    def metadata(self) -> dict:
        metadata = {"unresolved": self.reason}
        if self.claimed:
            metadata["claimed_workspace"] = self.claimed
        return metadata


def journal_root() -> Path:
    """The fallback journal's directory, created private on first use.

    Next to user sync's state, which the daemon and stdio servers find the same way: the
    daemon's state directory, also one installed with `--state`, or XDG state on Linux.
    """
    if ROOT_OVERRIDE is not None:
        Path(ROOT_OVERRIDE).mkdir(parents=True, exist_ok=True)
        return mark(Path(ROOT_OVERRIDE))
    from src.daemon.state import private_dir
    from src.user_sync.engine import default_state_dir
    return mark(private_dir(default_state_dir().parent / "unresolved"))


def mark(root: Path) -> Path:
    """Write the marker that names ``root`` the fallback journal's directory; returns ``root``."""
    marker = Path(root) / MARKER
    own = str(Path(root).resolve())
    try:
        if marker.read_text(encoding="utf-8") == own:
            return Path(root)
    except OSError:
        pass
    from src.daemon.state import atomic_private
    atomic_private(marker, own)
    return Path(root)


def is_journal_root(root) -> bool:
    """Whether ``root`` is a fallback journal's directory, by its marker; never runs git."""
    try:
        return (Path(root) / MARKER).read_text(encoding="utf-8") == str(Path(root).resolve())
    except (OSError, UnicodeError):
        return False


def clean_claim(value) -> str | None:
    """A claimed directory as it is stored and matched, or None.

    Only a normalized absolute path of printable characters: a line separator in metadata would
    let a crafted claim forge fields of the entry it is stored with.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_CLAIM or not value.isprintable() or not os.path.isabs(value):
        return None
    return os.path.normpath(value)


def unresolved(error, claimed=None) -> Unresolved:
    """The fallback for a workspace error.

    Raises OSError, or ValueError for a symbolic link, when the journal's directory cannot be made.
    """
    detail = str(error)
    code = detail.split(":", 1)[0].strip() or "workspace_required"
    return Unresolved(journal_root(), code, clean_claim(claimed), detail)


def matches(entry_metadata, claimed) -> bool:
    """Whether a fallback entry belongs to the same claimed directory. Without a claim, every
    entry: the session named no directory, and all entries are this user's own."""
    return not claimed or (entry_metadata or {}).get("claimed_workspace") == claimed
