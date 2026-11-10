"""One source for the separator rule the contract document states (#596).

``docs/subagent-contract.md`` used to restate the rule in prose of its own. Over four
review rounds on #595 three corrections each fixed one of those statements and left
another contradicting ``_split``, because nothing linked them. #597 left one source in the
engine: ``SEPARATORS`` and ``separator_rule``, whose sentences the rendered package states
and whose conditions ``tests/test_subagent_exchange.py`` executes through the labelled
examples.

This module writes those same sentences into the document, between its markers, so the
document quotes the statement instead of paraphrasing it. ``--write`` regenerates the
block, ``--check`` reports a stale one, and ``tests/test_subagent_contract_text.py`` fails
on a stale block.

What stays hand-written is deliberate. The conditions are never generated into English:
"spaced" and "outside a code span" live in control flow, not in a pattern, so generating
prose from the regexes would make the part that never drifted authoritative and leave the
part that did to hand-written text. The document's own sentence about what a code span is
also stays outside the markers, because the parser's masking rule is stated in prose in
exactly that one place and has nothing to drift from.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

from src.engine import subagent_text

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = ROOT / "docs/subagent-contract.md"
BEGIN_MARKER = "<!-- >>> separator rule (managed by src.engine.subagent_contract_text) >>> -->"
END_MARKER = "<!-- <<< separator rule <<< -->"
# The document's prose is wrapped by hand; its widest ordinary line is 92 characters.
WIDTH = 90
QUOTE = "> "
REGENERATE = ".venv/bin/python -m src.engine.subagent_contract_text --write"


class ContractTextError(Exception):
    """The document carries no complete marker pair, or its path is a symbolic link."""


def _unbreakable(text: str) -> tuple[str, list[str]]:
    """``text`` with every quoted spelling masked by a run of NULs of its own length.

    A spelling is quoted around its spaces (``' -- '``), so wrapping the sentence as it
    stands can end a line between the quote and the dash and leave the document showing a
    spelling nothing writes. NUL is not whitespace and appears in no source text, so the
    mask keeps each spelling inside one word while the line widths stay exactly those of
    the final text. The masks are recorded in the order they occur, left to right, because
    one spelling's mask can be a substring of a longer one's.
    """
    hidden: list[str] = []

    def mask(match: re.Match[str]) -> str:
        hidden.append(match.group(0))
        return "\0" * len(match.group(0))

    quoted = sorted((f"'{separator.spelling}'" for separator in subagent_text.SEPARATORS),
                    key=len, reverse=True)
    return re.compile("|".join(re.escape(spelling) for spelling in quoted)).sub(mask, text), hidden


def _restore(text: str, hidden: list[str]) -> str:
    """The masked spellings back in place, one per run of NULs, in the order they were hidden."""
    masks = iter(hidden)

    def put(match: re.Match[str]) -> str:
        quoted = next(masks, None)
        if quoted is None or len(quoted) != len(match.group(0)):
            raise ContractTextError(f"a mask of {len(match.group(0))} characters has no spelling")
        return quoted

    restored = re.sub(r"\0+", put, text)
    if next(masks, None) is not None or "\0" in restored:
        raise ContractTextError("a masked spelling outlived the render")
    return restored


def separator_block(width: int = WIDTH) -> str:
    """The rule's sentences as the document shows them: one blockquote, wrapped to the file.

    ``width`` is the document's; the tests pass others, because whether a wrap splits a
    spelling depends on it and the masking has to hold at every one.
    """
    masked, hidden = _unbreakable(" ".join(subagent_text.separator_rule()))
    wrapped = textwrap.fill(masked, width=width, initial_indent=QUOTE, subsequent_indent=QUOTE,
                            break_long_words=False, break_on_hyphens=False)
    return _restore(wrapped, hidden)


def managed_document(text: str) -> str:
    """``text`` with whatever sits between its markers replaced by the render."""
    try:
        before, rest = text.split(BEGIN_MARKER, 1)
        _, after = rest.split(END_MARKER, 1)
    except ValueError:
        raise ContractTextError(f"{DOCUMENT.name} has no complete separator marker pair") from None
    return before + BEGIN_MARKER + "\n" + separator_block() + "\n" + END_MARKER + after


def stale_files() -> list[Path]:
    """The generated documents whose committed text is not the render."""
    current = DOCUMENT.read_text(encoding="utf-8")
    return [] if managed_document(current) == current else [DOCUMENT]


def _replace(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temporary file in the same directory.

    A command whose job is to repair the document must not be able to leave it half
    written, so the new text is complete before it replaces the old, and the file keeps its
    mode. ``newline="\n"`` is load-bearing: a plain write translates newlines, so on
    Windows the committed document would gain CRLF while every check still passed, because
    reading it back translates them away again.

    Replacing rather than writing also settles the symbolic link: ``os.replace`` replaces
    the link itself instead of following it, so the refusal below is an explicit choice
    rather than the only thing standing between a link and its target.

    ``protocol_text._replace`` is the other generated-file writer. Each module in this tree
    keeps its own, as ``model_migration``, ``user_library`` and ``environments`` do.
    """
    if path.is_symlink():
        raise ContractTextError(f"refusing to replace the symbolic link {path}")
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        if path.exists() and os.name == "posix":
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_generated() -> list[Path]:
    """Regenerate the block; return the documents written, which is none when it was current."""
    current = DOCUMENT.read_text(encoding="utf-8")
    rendered = managed_document(current)
    if rendered == current:
        return []
    _replace(DOCUMENT, rendered)
    return [DOCUMENT]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.engine.subagent_contract_text",
                                     description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true",
                        help="regenerate the separator block of docs/subagent-contract.md")
    action.add_argument("--check", action="store_true",
                        help="report a stale block (the default)")
    args = parser.parse_args(argv)
    try:
        if args.write:
            for path in write_generated():
                print(f"updated {path.relative_to(ROOT)}")
            return 0
        stale = stale_files()
    except (ContractTextError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if stale:
        names = ", ".join(str(path.relative_to(ROOT)) for path in stale)
        print(f"stale {names}: run {REGENERATE}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
