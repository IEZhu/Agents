"""Idempotent, marker-bounded edits of CLAUDE.md (and friends).

Single source of truth for the marker-section editor previously inlined in
``scripts/init_repo.sh:636-672``. The bash installer can shell out to this
module via subprocess, and the MCP ``describe_repo`` tool calls it directly,
so both paths share identical behavior.

Contract for a managed section::

    {marker_begin}\n
    \n
    {section_content}\n
    \n
    {marker_end}\n

Behavior:
    - File missing            → create the file containing only the section.
    - File present, no markers → append section to the end (one blank line gap).
    - File present, both markers → replace content between markers in-place.
    - File present, only one marker → raise ``PartialMarkerError`` (do not guess).
    - File present, duplicate marker → raise ``DuplicateMarkerError``.

All writes are atomic (``tempfile`` in the same directory + ``os.replace``) so a
crash mid-write cannot leave a half-written CLAUDE.md.
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional


class ManagedSectionError(Exception):
    """Base class for managed-section editing errors."""


class PartialMarkerError(ManagedSectionError):
    """Only one of ``begin``/``end`` markers is present."""


class DuplicateMarkerError(ManagedSectionError):
    """One of the markers appears more than once."""


class InvertedMarkersError(ManagedSectionError):
    """The end marker appears before the begin marker."""


def atomic_write(path: str, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    The temporary file is created in the same directory so ``os.replace`` is
    a same-filesystem rename (atomic on POSIX). Writes use ``newline=""`` to
    disable translation and produce ``\\n`` endings. Reads use default newline
    mode, which normalizes ``\\r\\n`` → ``\\n``, so existing CRLF files are
    converted to LF on first edit. This is intentional — CLAUDE.md and
    history.md are always ``\\n``.
    """
    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(target_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=target_dir, prefix=".managed_section.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup if replace never happened.
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _validate_markers(content: str, marker_begin: str, marker_end: str) -> tuple[int, int]:
    """Return ``(begin_idx, end_idx)`` or raise on ambiguity.

    ``begin_idx`` points at the first character of ``marker_begin`` in the
    file; ``end_idx`` points one past the last character of ``marker_end``.
    Both are -1 if neither marker is present.
    """
    begin_count = content.count(marker_begin)
    end_count = content.count(marker_end)

    if begin_count == 0 and end_count == 0:
        return -1, -1

    if begin_count > 1 or end_count > 1:
        raise DuplicateMarkerError(
            f"Expected exactly one begin/end marker, "
            f"found {begin_count} begin and {end_count} end"
        )

    if begin_count != end_count:
        raise PartialMarkerError(
            f"Incomplete markers: begin={begin_count}, end={end_count}. "
            "Remove the orphaned marker manually before retrying."
        )

    begin_idx = content.find(marker_begin)
    end_anchor = content.find(marker_end, begin_idx)
    if end_anchor < 0:
        # end marker appears, but before begin
        raise InvertedMarkersError(
            "End marker appears before begin marker"
        )
    end_idx = end_anchor + len(marker_end)
    return begin_idx, end_idx


def _format_block(marker_begin: str, marker_end: str, section_content: str) -> str:
    """Compose a marker block with a single blank line padding either side."""
    body = section_content.strip("\n")
    return f"{marker_begin}\n\n{body}\n\n{marker_end}\n"


def upsert_section(
    file_path: str,
    marker_begin: str,
    marker_end: str,
    section_content: str,
) -> str:
    """Create, replace, or append a managed section.

    Returns the action taken: ``"created"``, ``"replaced"``, or ``"appended"``.
    """
    block = _format_block(marker_begin, marker_end, section_content)

    if not os.path.exists(file_path):
        atomic_write(file_path, block)
        return "created"

    with open(file_path, "r", encoding="utf-8") as f:
        original = f.read()

    begin_idx, end_idx = _validate_markers(original, marker_begin, marker_end)

    if begin_idx < 0:
        # No managed section yet — append, separated by exactly one blank line.
        prefix = original
        if not prefix.endswith("\n"):
            prefix += "\n"
        if not prefix.endswith("\n\n"):
            prefix += "\n"
        new_content = prefix + block
        atomic_write(file_path, new_content)
        return "appended"

    # Replace existing block. Consume trailing newlines so we don't accumulate
    # blank lines on every refresh.
    trailing = end_idx
    while trailing < len(original) and original[trailing] == "\n":
        trailing += 1
    new_content = original[:begin_idx] + block + original[trailing:]
    atomic_write(file_path, new_content)
    return "replaced"


def read_section(
    file_path: str,
    marker_begin: str,
    marker_end: str,
) -> Optional[str]:
    """Return the content between markers, or ``None`` if no managed section.

    Whitespace immediately inside the markers is trimmed so callers see exactly
    the body they passed in (modulo intentional inner blank lines).
    """
    if not os.path.exists(file_path):
        return None

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    begin_idx, end_idx = _validate_markers(content, marker_begin, marker_end)
    if begin_idx < 0:
        return None

    inner_start = begin_idx + len(marker_begin)
    inner_end = end_idx - len(marker_end)
    body = content[inner_start:inner_end]
    return body.strip("\n")


def remove_section(
    file_path: str,
    marker_begin: str,
    marker_end: str,
) -> bool:
    """Delete the managed section if present. Returns ``True`` if anything was
    removed; ``False`` if the file or the section did not exist."""
    if not os.path.exists(file_path):
        return False

    with open(file_path, "r", encoding="utf-8") as f:
        original = f.read()

    begin_idx, end_idx = _validate_markers(original, marker_begin, marker_end)
    if begin_idx < 0:
        return False

    # Consume trailing newlines after end marker.
    trailing = end_idx
    while trailing < len(original) and original[trailing] == "\n":
        trailing += 1

    # Also consume blank lines immediately preceding the begin marker so we
    # don't leave a widening gap each time a section is created and removed.
    leading = begin_idx
    while leading > 0 and original[leading - 1] == "\n":
        leading -= 1
    # Preserve the line break that ends the previous content (if any),
    # but only if we actually consumed at least one blank line.
    if leading > 0 and leading < begin_idx:
        leading += 1

    new_content = original[:leading] + original[trailing:]
    atomic_write(file_path, new_content)
    return True
