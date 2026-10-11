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
        named = (Path(root) / MARKER).read_text(encoding="utf-8")
        return os.path.normcase(named) == os.path.normcase(str(Path(root).resolve()))  # case on Windows
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


# --- moving entries into a project (#292) ------------------------------------------------------

def _entries(root: Path):
    """This machine's fallback entries with the file each is in: ``(month or None, entry)``."""
    from src.memory.history import _archive_names, parsed_file
    files = [(None, root / "history.md")]
    files += [(name[:-3], root / "history" / name) for name in _archive_names(str(root / "history"))]
    for month, path in files:
        parsed = parsed_file(str(path))
        for entry in (parsed[1] if parsed else ()):
            yield month, entry


def unresolved_groups(root: Path | None = None) -> list[dict]:
    """The fallback entries of this machine by claimed directory: claimed, count, newest, ids."""
    root = Path(root) if root is not None else journal_root()
    groups: dict = {}
    for _, entry in _entries(root):
        claimed = (entry.metadata or {}).get("claimed_workspace")
        group = groups.setdefault(claimed, {"claimed": claimed, "count": 0, "newest": "", "ids": []})
        group["count"] += 1
        group["newest"] = max(group["newest"], entry.timestamp)
        group["ids"].append(entry.id)
    return sorted(groups.values(), key=lambda group: group["newest"], reverse=True)


def adopt(target: Path, *, claimed: str | None = None, entry_ids=(), root: Path | None = None,
          dry_run: bool = False) -> dict:
    """Move this machine's fallback entries into the project ``target``'s history.md.

    Selects the entries of the claimed directory ``claimed``, or the ids ``entry_ids``. Each keeps
    its id, time and metadata; a project that already holds an id keeps its copy, and the
    entry leaves the fallback journal either way. Only the caller decides that ``target`` is the
    right project: nothing moves by itself. ``dry_run`` reports what would move.
    """
    from src.memory.history import EntryChanged, HistoryWriter, delete_entry
    if not claimed and not entry_ids:
        raise ValueError("name the claimed directory or the entries to move")
    root = Path(root) if root is not None else journal_root()
    wanted = set(entry_ids)
    claimed = clean_claim(claimed) if claimed else None
    selected = [(month, entry) for month, entry in _entries(root)
                if entry.id in wanted or (claimed and (entry.metadata or {}).get("claimed_workspace") == claimed)]
    result = {"target": str(target), "moved": [], "already": [], "changed": [],
              "missing": sorted(wanted - {entry.id for _, entry in selected})}
    if dry_run:
        result["moved"] = [{"id": entry.id, "timestamp": entry.timestamp, "intent": entry.intent}
                           for _, entry in selected]
        return result
    writer = HistoryWriter(str(Path(target) / "history.md"), str(Path(target) / "history"))
    for month, entry in selected:
        written = writer.append_existing(entry)
        item = {"id": entry.id, "timestamp": entry.timestamp, "intent": entry.intent}
        try:
            delete_entry(str(root / "history.md"), month, entry.id, entry.timestamp, str(root / "history"))
        except EntryChanged:
            result["changed"].append(item)
            continue
        result["moved" if written["status"] == "recorded" else "already"].append(item)
    return result
