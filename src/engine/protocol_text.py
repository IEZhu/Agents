"""One source for the persona protocol text (#431).

Each file in ``src/engine/protocol/`` holds one protocol section, with its wordings side by
side as named variants, so a rule is edited in one file:

- ``full``: the prose of the instruction files, the managed section that the installers write
  into ``CLAUDE.md`` and ``AGENTS.md``;
- ``compact`` (and ``compact-*`` when a surface needs it in parts): the short form that the MCP
  server instructions, the full ``instruction`` of a ``SUCCESS`` and the memory reminder all
  reuse unchanged;
- ``server``, ``bundle``, ``memory`` and their ``*-*`` parts: a surface's own wording where it
  needs one. The server instructions keep their opening within the windows that Claude Code
  shows of them, the ``SUCCESS`` instruction is sent with a conversation's first activation
  and a restore, and the memory reminder states the conversation steps in its own prose; a
  short rule that two of them state alike (``finish:turn-end``, ``finish:redaction``) is its
  own variant.

The ``pointer`` section holds what a later ``SUCCESS`` of a conversation and the server
instructions for a client whose own instruction file is current send instead of a text the
client already holds (#433).

The functions below compose each surface from these variants in a fixed order. The template
and the memory reminder stay committed files, because installers and shell scripts read them;
``python -m src.engine.protocol_text --write`` regenerates them together with the managed
section of this repository's ``CLAUDE.md``, and ``--check`` lists the stale ones.
``repeated_sentences`` finds a sentence that the source states in two places, so a rule is
reused instead of copied.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from functools import cache
from pathlib import Path
from typing import Callable, Mapping, Union

SOURCE_DIR = Path(__file__).resolve().parent / "protocol"
ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "scripts" / "templates" / "routing-protocol-core.md"
MEMORY_REMINDER = ROOT / "scripts" / "templates" / "memory-routing.md"
# Earlier generated reminders, which the migration helper and the uninstaller recognize by their bytes.
LEGACY_DIR = ROOT / "scripts" / "templates" / "legacy"
CLAUDE_MD = ROOT / "CLAUDE.md"
BEGIN_MARKER = "# >>> Agents-Core Routing Protocol (managed by init_repo) >>>"
END_MARKER = "# <<< Agents-Core Routing Protocol (managed by init_repo) <<<"
REGENERATE = "python -m src.engine.protocol_text --write"

_VARIANT_MARKER = re.compile(r"^<!-- variant: ([a-z][a-z0-9-]*) -->$")
# The section files the surfaces are built from. Only these are read, so another file in the
# directory (a README) cannot break the server's import; a test checks that the list is complete.
SECTION_NAMES = (
    "intro", "header", "worker", "delegation", "artifacts", "output-artifacts", "local-inference",
    "cooperation", "start", "decide", "apply", "flows", "first-work", "ownership", "progress", "finish",
    "history-errors", "compatibility", "tools", "pointer",
)
# Sentences shorter than this are too generic to count as a copied rule ("Then send the final answer.").
MIN_REPEATED_SENTENCE = 40

# A piece of a surface: a section variant, or text already composed from variants.
Piece = Union[tuple[str, str], str]


class ProtocolSourceError(ValueError):
    """A section file of the protocol source is malformed or a surface names a missing variant."""


def _parse(path: Path) -> dict[str, str]:
    variants: dict[str, list[str]] = {}
    current = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ProtocolSourceError(f"cannot read protocol section {path.name}: {error}") from None
    for number, line in enumerate(lines, 1):
        match = _VARIANT_MARKER.match(line)
        if match:
            current = match.group(1)
            if current in variants:
                raise ProtocolSourceError(f"{path.name}:{number}: variant {current!r} appears twice")
            variants[current] = []
        elif current is not None:
            variants[current].append(line)
        elif line.strip():
            raise ProtocolSourceError(f"{path.name}:{number}: text before the first variant marker")
    if not variants:
        raise ProtocolSourceError(f"{path.name}: no variant marker")
    bodies = {name: "\n".join(lines).strip("\n") for name, lines in variants.items()}
    for name, body in bodies.items():
        if not body.strip():
            raise ProtocolSourceError(f"{path.name}: variant {name!r} is empty")
    return bodies


@cache
def sections() -> dict[str, dict[str, str]]:
    """Every section of the source: {section: {variant: text}}."""
    return {name: _parse(SOURCE_DIR / f"{name}.md") for name in SECTION_NAMES}


def part(section: str, variant: str) -> str:
    try:
        return sections()[section][variant]
    except KeyError:
        raise ProtocolSourceError(f"the protocol source has no variant {section}:{variant}") from None


def _text(piece: Piece) -> str:
    return piece if isinstance(piece, str) else part(*piece)


def _paragraph(*pieces: Piece) -> str:
    """Pieces that continue one paragraph, one per line."""
    return "\n".join(_text(piece) for piece in pieces)


def _sentences_of(*pieces: Piece) -> str:
    """Pieces run together as sentences of one line."""
    return " ".join(" ".join(_text(piece).split()) for piece in pieces)


def template() -> str:
    """The managed routing section of CLAUDE.md and AGENTS.md (scripts/templates/routing-protocol-core.md)."""
    return "\n\n".join([
        part("intro", "full"), part("delegation", "full"), part("artifacts", "full"),
        part("output-artifacts", "full"), part("local-inference", "full"), part("delegation", "full-fallback"),
        part("worker", "full"), part("cooperation", "full"), part("start", "full"), part("decide", "full"),
        part("apply", "full"), part("flows", "full"), part("first-work", "full"), part("ownership", "full"),
        part("progress", "full"), _paragraph(("finish", "full"), ("history-errors", "full")),
        part("compatibility", "full"),
    ]) + "\n"


def server_instructions() -> str:
    """The instructions the MCP server sends at initialization.

    Claude Code shows their first 4,096 characters, and only 2,048 before v2.1.296 (#421, #508).
    The shorter window holds what every turn needs: the opening lines (protocol, server choice,
    worker exemption, first-answer activation, logging) and the delegation request through
    ``Require status: prepared.``. The first-work rule, which a session may need before its first
    activation, comes next, and the rest of the delegation exchange through its validation rule
    fits the longer window. The keep/switch rules also appear in the descriptions of the persona
    tools, and the describe_repo fallback, the owning-session rules and the definition of progress
    messages, which follow the windows, arrive with the full ``SUCCESS`` instruction.
    tests/test_log_interaction_contract.py checks both windows.
    """
    return "\n\n".join([
        _server_opening(),
        _paragraph(("delegation", "compact-request"), ("first-work", "compact"), ("delegation", "compact")),
        _paragraph(("decide", "server"), _sentences_of(("first-work", "compact-unavailable"), ("ownership", "compact"))),
        part("progress", "compact"),
        _sentences_of(("artifacts", "compact"), ("local-inference", "compact")),
        part("output-artifacts", "compact"),
        part("cooperation", "compact"),
        _paragraph(("apply", "server"), _sentences_of(("intro", "compact"), ("apply", "server-prompts"))),
        _server_tail(),
    ])


def _server_opening() -> str:
    """The per-turn lines that open the server instructions and their pointer."""
    return _paragraph(("header", "server"), ("worker", "server"), ("start", "server"),
                      _sentences_of(("finish", "server"), ("finish", "turn-end"), ("finish", "server-delivery")))


def _server_tail() -> str:
    """The closing rules: language and footer, the workspace header and history errors and the flow
    tools, which only the server instructions state, and the MCP-unavailable fallback."""
    return _paragraph(("tools", "server"), ("flows", "server"), ("compatibility", "server"))


def server_pointer() -> str:
    """The server instructions for a client whose own instruction file holds the current template (#433).

    It keeps the opening lines and the closing rules of ``server_instructions`` and points to the
    file for everything between them, which the template states in full.
    """
    pointer = " ".join(part("pointer", "server").replace("{revision}", template_revision()).split())
    return "\n\n".join([_server_opening(), pointer, _server_tail()])


# The variants of the ``SUCCESS`` instruction, in order. ``bundle_pointer`` names each rule they
# carry, and tests/test_instruction_pointers.py fails on a variant it does not name.
BUNDLE_PARTS: tuple[tuple[str, str], ...] = (
    ("worker", "bundle"), ("first-work", "compact"), ("first-work", "compact-unavailable"),
    ("ownership", "compact"), ("apply", "bundle"), ("apply", "bundle-pointer"), ("finish", "bundle"),
    ("finish", "redaction"), ("history-errors", "compact"), ("progress", "compact"),
    ("delegation", "compact-request"), ("delegation", "compact"), ("artifacts", "compact"),
    ("output-artifacts", "compact"), ("intro", "compact"), ("local-inference", "compact"),
    ("cooperation", "compact"),
)


def bundle_instruction() -> str:
    """The ``instruction`` of a conversation's first ``SUCCESS`` and of a restore: one paragraph.

    The worker exemption comes first, then the first-work and owning-session rules, which a
    client that never loaded the instruction files learns here, as it learns the definition of
    progress messages after the logging rules. A later ``SUCCESS`` sends ``bundle_pointer``.
    """
    return _sentences_of(*BUNDLE_PARTS)


def bundle_pointer() -> str:
    """The ``instruction`` of a ``SUCCESS`` after the first activation of a conversation (#433)."""
    return _sentences_of(("pointer", "bundle"))


def _revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def instruction_revision() -> str:
    """Names the full ``SUCCESS`` instruction, so a pointer can say which text it stands for."""
    return _revision(bundle_instruction())


def template_revision() -> str:
    """Names the managed routing section, so the server pointer and the warnings can say which one."""
    return _revision(template())


def managed_section(text: str) -> str | None:
    """The text between the routing markers of an instruction file, stripped; None without a pair."""
    try:
        _, rest = text.split(BEGIN_MARKER, 1)
        section, _ = rest.split(END_MARKER, 1)
    except ValueError:
        return None
    return section.strip()


def memory_reminder() -> str:
    """The routing reminder the installers write into a Claude profile's memory."""
    return "\n\n".join([
        _paragraph(("header", "memory"), ("intro", "compact")),
        _sentences_of(("delegation", "compact-request"), ("delegation", "compact")),
        part("worker", "memory"), part("artifacts", "compact"), part("output-artifacts", "compact"),
        part("local-inference", "compact"), part("cooperation", "compact"), part("ownership", "compact"),
        _sentences_of(("first-work", "compact"), ("first-work", "compact-unavailable")),
        part("start", "memory"), part("decide", "memory"), part("apply", "memory"), part("progress", "compact"),
        _paragraph(("finish", "memory"), ("finish", "turn-end"), ("finish", "redaction")),
        part("history-errors", "compact"), part("compatibility", "memory"),
    ]) + "\n"


def managed_claude_md(text: str) -> str:
    """``text`` (a CLAUDE.md) with its managed routing section replaced by the template."""
    try:
        before, rest = text.split(BEGIN_MARKER, 1)
        _, after = rest.split(END_MARKER, 1)
    except ValueError:
        raise ProtocolSourceError("CLAUDE.md has no complete routing marker pair") from None
    return before + BEGIN_MARKER + "\n\n" + template() + "\n" + END_MARKER + after


GENERATED: dict[Path, Callable[[str], str]] = {
    TEMPLATE: lambda _current: template(),
    MEMORY_REMINDER: lambda _current: memory_reminder(),
    CLAUDE_MD: managed_claude_md,
}


def _renders() -> dict[Path, tuple[str, str]]:
    """Each generated file's current text and its render, all computed before anything is written."""
    renders = {}
    for path, render in GENERATED.items():
        if path.is_symlink():
            raise ProtocolSourceError(f"refusing to replace the symbolic link {path}")
        current = path.read_text(encoding="utf-8")
        renders[path] = (current, render(current))
    return renders


def stale_files() -> list[Path]:
    """The committed generated files whose text differs from the render, and a missing legacy copy.

    The legacy directory is listed when the released reminder is neither current nor kept there.
    """
    renders = _renders()
    stale = [path for path, (current, rendered) in renders.items() if rendered != current]
    if MEMORY_REMINDER in renders and _missing_legacy(renders[MEMORY_REMINDER][1]) is not None:
        stale.append(LEGACY_DIR)
    return stale


def _replace(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temporary file in the same directory, keeping its mode."""
    if path.is_symlink():
        raise ProtocolSourceError(f"refusing to replace the symbolic link {path}")
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


def released_reminder() -> tuple[str, str] | None:
    """The memory reminder of the integration branch this checkout descends from, and where it came from.

    Installations run the released reminder, so that is the copy migration must recognize; the
    working tree may already hold an intermediate version that was never installed. None when
    git, a ``dev`` base or the file at that base is unavailable.
    """
    relative = MEMORY_REMINDER.relative_to(ROOT).as_posix()
    for base in ("origin/dev", "dev"):
        try:
            merge_base = subprocess.run(["git", "-C", str(ROOT), "merge-base", "HEAD", base], capture_output=True,
                                        text=True, timeout=30, check=True).stdout.strip()
            shown = subprocess.run(["git", "-C", str(ROOT), "show", f"{merge_base}:{relative}"], capture_output=True,
                                   timeout=30, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        return shown.decode("utf-8").replace("\r\n", "\n"), f"{merge_base[:12]} (merge base with {base})"
    return None


def _kept(previous: str) -> bool:
    data = previous.encode("utf-8")
    return any(copy.read_bytes().replace(b"\r\n", b"\n") == data for copy in LEGACY_DIR.glob("memory-routing-v*.md"))


def _missing_legacy(rendered: str) -> tuple[str, str] | None:
    """The released reminder and its source when migration would not recognize it, else None."""
    released = released_reminder()
    if released is None or released[0] == rendered or _kept(released[0]):
        return None
    return released


def _keep_previous_reminder(previous: str) -> Path | None:
    """Save ``previous`` as the next legacy reminder, unless a legacy copy already holds it."""
    if _kept(previous):
        return None
    copies = sorted(LEGACY_DIR.glob("memory-routing-v*.md"))
    numbers = [int(match.group(1)) for copy in copies if (match := re.fullmatch(r"memory-routing-v(\d+)\.md", copy.name))]
    target = LEGACY_DIR / f"memory-routing-v{max(numbers, default=0) + 1}.md"
    target.write_bytes(previous.encode("utf-8"))
    return target


def write_generated(on_write: Callable[[Path], None] = lambda path: None,
                    on_source: Callable[[str], None] = lambda source: None) -> list[Path]:
    """Regenerate the committed files; return the ones written, a new legacy reminder included.

    Everything is rendered, and every target checked, before the first write, so a source error
    or a symbolic link changes no file. On every run the released reminder (``released_reminder``)
    is kept as the next legacy copy when migration would not recognize it, even when the
    reminder itself is current; without a released one, a replaced working-tree reminder is
    kept. ``on_write`` hears of each file as soon as it is written, ``on_source`` where a kept
    reminder came from.
    """
    changed = []

    def written(path: Path) -> None:
        changed.append(path)
        on_write(path)
    renders = _renders()
    reminder = renders.get(MEMORY_REMINDER)
    if reminder is not None:
        current, rendered = reminder
        released = _missing_legacy(rendered)
        if released is not None:
            previous, source = released
        elif released_reminder() is None and rendered != current:
            previous, source = current, "the working tree (no dev base found)"
        else:
            previous = None
        if previous is not None and (legacy := _keep_previous_reminder(previous)) is not None:
            on_source(source)
            written(legacy)
    for path, (current, rendered) in renders.items():
        if rendered != current:
            _replace(path, rendered)
            written(path)
    return changed


def _sentences(text: str) -> set[str]:
    """Normalized sentences of ``text``: no Markdown emphasis or code marks, single spaces, lower case."""
    flat = " ".join(re.sub(r"[`*]", "", text).split()).lower()
    return {sentence.rstrip(".;:") for sentence in re.split(r"(?<=[.!?;:])\s+", flat) if sentence}


def repeated_sentences(blocks: Mapping[str, str], min_chars: int = MIN_REPEATED_SENTENCE) -> dict[str, list[str]]:
    """Sentences of at least ``min_chars`` characters that two or more of ``blocks`` contain.

    ``blocks`` maps a name to its text; the result maps each repeated sentence to the names
    that contain it, so a rule copied into a second place is reported with both places.
    """
    places: dict[str, list[str]] = defaultdict(list)
    for name, text in blocks.items():
        for sentence in _sentences(text):
            if len(sentence) >= min_chars:
                places[sentence].append(name)
    return {sentence: sorted(names) for sentence, names in places.items() if len(names) > 1}


def source_blocks() -> dict[str, str]:
    """Every variant of the source as ``section:variant`` → text, for ``repeated_sentences``."""
    return {f"{section}:{variant}": text for section, variants in sections().items()
            for variant, text in variants.items()}


def copied_sentences() -> dict[str, list[str]]:
    """Sentences the source states in two places, other than a section's full prose quoting one short form.

    The ``full`` variant of a section expands its short wording and may repeat a sentence of
    it. Any other repeat, across sections or among short variants, is a copied rule: move it
    into its own section and include that section where it is needed.
    """
    def quoted(names: list[str]) -> bool:
        sections_, variants = zip(*(name.split(":", 1) for name in names))
        return len(names) == 2 and len(set(sections_)) == 1 and "full" in variants
    return {sentence: names for sentence, names in repeated_sentences(source_blocks()).items()
            if not quoted(names)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.engine.protocol_text", description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true", help="regenerate the committed files from src/engine/protocol")
    action.add_argument("--check", action="store_true", help="list stale committed files (the default)")
    args = parser.parse_args(argv)
    try:
        if args.write:
            def report(path: Path) -> None:
                print(f"updated {path.relative_to(ROOT)}")
                if path.parent == LEGACY_DIR:
                    print(f"  add {path.name} where the tests list legacy reminders: grep -rn memory-routing-v tests/")
            write_generated(report, lambda source: print(f"  kept the released reminder of {source}"))
            return 0
        stale = stale_files()
    except (ProtocolSourceError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for path in stale:
        print(f"stale: {path.relative_to(ROOT)}; run {REGENERATE}", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
