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
  own variant;
- ``tool`` and ``tool-*``: a procedure that a tool's description carries instead of the
  always-on surfaces, which state only its trigger (``TOOL_PARTS``, #734).

The ``pointer`` section holds what a later ``SUCCESS`` of a conversation and the server
instructions for a client whose own instruction file is current send instead of a text the
client already holds (#433), and what a ``SUCCESS`` sends when the client's instructions hold
the current managed section, its first activation included (#734).

When the process-wide switches say that the client's Stop hook logs each turn
(``config.turn_log_hook()``: ``AGENTS_TURN_LOG_HOOK=1`` with the headless profile, #734), the server
instructions and the ``SUCCESS`` instruction finish with the ``finish`` variants ``hook``,
``hook-calls``, ``server-hook`` and ``bundle-hook`` instead of the log_interaction step. Each
function that renders one of them takes ``hook``, None for that process-wide decision. An
interactive session's own hook says it turn by turn instead (#701): ``turn_log_notice`` is what it
puts beside each prompt, and the model-logging texts defer to it (``finish:notice-rule``). The
committed files never depend on either.

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
import inspect
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

from src.engine import config

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
# Any line that looks like a variant marker; one that is not exactly a marker is refused.
_MARKER_LIKE = re.compile(r"^\s*<!--\s*variant", re.IGNORECASE)
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
        if match is None and _MARKER_LIKE.match(line):
            raise ProtocolSourceError(f"{path.name}:{number}: malformed variant marker {line!r}; "
                                      "write <!-- variant: name --> alone on its line")
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
    """The managed routing section of CLAUDE.md and AGENTS.md (scripts/templates/routing-protocol-core.md).

    Delegation, artifacts, local inference and cooperation appear here as triggers that name the
    tool whose description holds the procedure (``TOOL_PARTS``, #734).
    """
    return "\n\n".join([
        part("intro", "full"), part("delegation", "full"), part("worker", "full"),
        # A worker never loads validate_subagent_result, whose description holds the parent's side,
        # and its package carries the artifact rule only with an input manifest (#734 review).
        part("output-artifacts", "worker"), part("cooperation", "full"),
        part("start", "full"), part("decide", "full"), part("apply", "full"), part("flows", "full"), part("first-work", "full"), part("ownership", "full"),
        part("progress", "full"), _paragraph(("finish", "full"), ("history-errors", "full")),
        part("compatibility", "full"),
    ]) + "\n"


def _hook(hook: bool | None) -> bool:
    """``hook``, or the process-wide decision when the caller names none."""
    return config.turn_log_hook() if hook is None else hook


def server_instructions(hook: bool | None = None) -> str:
    """The instructions the MCP server sends at initialization.

    Claude Code shows their first 4,096 characters, and only 2,048 before v2.1.296 (#421, #508).
    The shorter window holds what every turn needs: the opening lines (protocol, server choice,
    worker exemption, first-answer activation, logging) and the delegation request through
    ``Require status: prepared.``. The longer window adds the first-work rule, which a session may
    need before its first activation, and the delegation trigger: validate every output, require
    ``valid: true``, read the descriptions of get_subagent_context and validate_subagent_result
    before the first delegation, the fallback when an endpoint is unavailable, and the exact-name
    selection that loads the persona tools when a client defers them (#734). The exchange
    itself, the bound on how much to delegate, artifacts, local inference and the cooperation
    mailbox live in tool descriptions (``TOOL_PARTS``, #734), which a deferring client loads only
    when it selects the tool. Both windows end within a few hundred characters of a rule they must
    hold, so text moved in front of one may push that rule out. The keep/switch rules also appear
    in the descriptions of the persona tools, and the owning-session rules and the definition of
    progress messages, which may follow the windows, arrive with the full ``SUCCESS`` instruction
    too. tests/test_log_interaction_contract.py checks both windows.
    """
    return "\n\n".join([
        _server_opening(hook),
        _paragraph(("delegation", "compact-request"), ("first-work", "compact"), ("delegation", "compact")),
        # After the older window (#508), inside the current one: the calls a hooked client still
        # makes, and for a client whose model logs, the notice rule, which only a session whose own
        # hook puts the turn-log notice beside its prompts needs, and that takes a recent client.
        *((part("start", "server-load-hook"), part("finish", "hook-calls")) if _hook(hook)
          else (part("start", "server-load"), part("finish", "notice-rule"))),
        _paragraph(("decide", "server"), _sentences_of(("first-work", "compact-unavailable"), ("ownership", "compact"))),
        part("progress", "compact"),
        _sentences_of(("artifacts", "compact"), ("local-inference", "compact")),
        part("cooperation", "compact"),
        _paragraph(("apply", "server"), _sentences_of(("intro", "compact"), ("apply", "server-prompts"))),
        _server_tail(),
    ])


def _server_opening(hook: bool | None = None) -> str:
    """The per-turn lines that open the server instructions and their pointer.

    Their finish line leaves logging to the client's Stop hook when ``hook`` (``_hook``); the calls
    such a client still makes (``finish:hook-calls``) follow the delegation request, so the older
    window keeps ``Require status: prepared.``.
    """
    finish = ((("finish", "hook"), ("finish", "server-hook")) if _hook(hook)
              else (("finish", "server"), ("finish", "turn-end"), ("finish", "server-delivery")))
    return _paragraph(("header", "server"), ("worker", "server"), ("start", "server"), _sentences_of(*finish))


def _server_tail() -> str:
    """The closing rules: language and footer, the workspace header and history errors and the flow
    tools, which only the server instructions state, and the MCP-unavailable fallback."""
    return _paragraph(("tools", "server"), ("flows", "server"), ("compatibility", "server"))


def server_pointer(hook: bool | None = None) -> str:
    """The server instructions for a client whose own instruction file holds the current template (#433).

    It keeps the opening lines and the closing rules of ``server_instructions`` and points to the
    file for everything between them, which the template states in full.
    """
    pointer = " ".join(part("pointer", "server").replace("{revision}", template_revision()).split())
    calls = [part("finish", "hook-calls")] if _hook(hook) else []
    return "\n\n".join([_server_opening(hook), *calls, pointer, _server_tail()])


# The variants of the ``SUCCESS`` instruction, in order. ``bundle_pointer`` names each rule they
# carry, and tests/test_instruction_pointers.py fails on a variant it does not name.
BUNDLE_PARTS: tuple[tuple[str, str], ...] = (
    ("worker", "bundle"), ("first-work", "compact"), ("first-work", "compact-unavailable"),
    ("ownership", "compact"), ("apply", "bundle"), ("apply", "bundle-pointer"), ("finish", "bundle"),
    ("finish", "redaction"), ("finish", "notice-rule"), ("history-errors", "compact"), ("progress", "compact"),
    ("delegation", "compact-request"), ("delegation", "compact"), ("artifacts", "compact"),
    ("intro", "compact"), ("local-inference", "compact"), ("cooperation", "compact"),
)
# The finish rule of the SUCCESS instruction when the client's Stop hook logs each turn: the model
# makes no per-turn log_interaction call, only the ones a flow or the user asks for, whose redaction
# the tool's description states; every turn is the hook's, so no notice rule either.
HOOK_FINISH: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    ("finish", "bundle"): (("finish", "bundle-hook"),),
    ("finish", "redaction"): (("finish", "hook"), ("finish", "hook-calls")),
    ("finish", "notice-rule"): (),
}


def bundle_instruction(hook: bool | None = None) -> str:
    """The ``instruction`` of a conversation's first ``SUCCESS`` and of a restore: one paragraph.

    The worker exemption comes first, then the first-work and owning-session rules, which a
    client that never loaded the instruction files learns here, as it learns the definition of
    progress messages after the logging rules. A later ``SUCCESS`` sends ``bundle_pointer``, and
    one whose client holds the managed section sends ``bundle_section_pointer``. With
    ``hook`` (``_hook``) the finish rule is ``HOOK_FINISH``'s.
    """
    if not _hook(hook):
        return _sentences_of(*BUNDLE_PARTS)
    return _sentences_of(*(piece for name in BUNDLE_PARTS for piece in HOOK_FINISH.get(name, (name,))))


def bundle_pointer(hook: bool | None = None) -> str:
    """The ``instruction`` of a ``SUCCESS`` after the first activation of a conversation (#433)."""
    return _sentences_of(("pointer", "bundle"), ("pointer", "bundle-rules"), _pointer_finish(hook),
                         ("pointer", "bundle-reload"), ("pointer", "bundle-restore"))


def bundle_section_pointer(hook: bool | None = None) -> str:
    """The ``instruction`` of a ``SUCCESS`` other than a restore when the client holds the managed section (#734).

    The section states every rule of ``bundle_instruction``, so this pointer names the same rules
    as ``bundle_pointer`` without claiming that an earlier activation sent the full instruction:
    it is true of a conversation's first activation as well as of a later one.
    """
    return _sentences_of(("pointer", "bundle-section"), ("pointer", "bundle-rules"), _pointer_finish(hook),
                         ("pointer", "bundle-section-reload"), ("pointer", "bundle-restore"))


def _pointer_finish(hook: bool | None = None) -> tuple[str, str]:
    """How both pointers end the answer; with the Stop hook logging each turn, without log_interaction (#734)."""
    return ("pointer", "bundle-finish-hook") if _hook(hook) else ("pointer", "bundle-finish")


def no_change_instruction(hook: bool | None = None) -> str:
    """The ``instruction`` of a ``NO_CHANGE``: keep the activation, and the current logging decision
    (#701 review), so a session learns of a change since its last ``SUCCESS`` from either."""
    return _sentences_of(("pointer", "no-change"), _pointer_finish(hook))


def turn_log_notice() -> str:
    """What an interactive session's turn-log hook puts beside each prompt (#701): the Stop hook
    records this turn, the calls a flow or the user asks for are still made, and the final answer
    of a turn closed by a call such as ``wrap_up`` is given as text first.

    Claude Code adds at most 10,000 characters of a hook's context in full; this stays far below.
    """
    return _sentences_of(("finish", "notice"), ("finish", "hook-calls"), ("finish", "notice-close"))


# The procedures that the descriptions of these tools carry instead of the always-on surfaces
# (#734). Every model call re-reads the managed section and the server instructions, while a
# deferring client (Claude Code) loads a tool's description only when it selects that tool. Each
# always-on surface keeps a trigger that names the tool to read before the first use. A rule that
# must hold while MCP is unavailable, or for a worker that never loads these tools, stays on the
# always-on surfaces.
TOOL_PARTS: dict[str, tuple[tuple[str, str], ...]] = {
    "get_subagent_context": (("delegation", "compact-request"), ("delegation", "tool-prepare"),
                             ("artifacts", "tool-prepare")),
    "validate_subagent_result": (("delegation", "tool-validate"), ("artifacts", "tool-validate"),
                                 ("output-artifacts", "tool")),
    "cooperation_register": (("cooperation", "tool"),),
    "local_context_create": (("local-inference", "tool"),),
}


def tool_procedure(name: str) -> str:
    """The protocol text that the description of tool ``name`` carries; empty for any other tool."""
    return "\n".join(part(*piece) for piece in TOOL_PARTS.get(name, ()))


def tool_description(name: str, docstring: str | None) -> str:
    """The description of tool ``name``: its docstring with the tool's procedure after the summary.

    The summary paragraph stays first, as a client lists it; the procedure follows, ahead of the
    tool's limits and errors, so a client that cuts a long description loses the mechanics before
    a rule. A tool without a procedure keeps its docstring unchanged.
    """
    procedure = tool_procedure(name)
    if not procedure:
        return docstring or ""
    summary, _, details = inspect.cleandoc(docstring or "").partition("\n\n")
    return "\n\n".join(text for text in (summary, procedure, details) if text)


def _revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def instruction_revision(hook: bool | None = None) -> str:
    """Names the full ``SUCCESS`` instruction, so a pointer can say which text it stands for."""
    return _revision(bundle_instruction(hook))


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
        part("worker", "memory"), part("artifacts", "compact"),
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

    The legacy directory is listed when the released reminder is neither current nor kept there;
    without a ``dev`` base the released reminder is unknown, and the directory is not listed.
    """
    renders = _renders()
    stale = [path for path, (current, rendered) in renders.items() if rendered != current]
    if MEMORY_REMINDER in renders:
        released = released_reminder()
        if released is not None and _needs_legacy(renders[MEMORY_REMINDER][1], released[0]):
            stale.append(LEGACY_DIR)
    return stale


def _replace(path: Path, text: str, mode_from: Path | None = None) -> None:
    """Write ``text`` to ``path`` through a temporary file in the same directory.

    An existing file keeps its mode; a new one takes the mode of ``mode_from`` when given.
    """
    if path.is_symlink():
        raise ProtocolSourceError(f"refusing to replace the symbolic link {path}")
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        reference = path if path.exists() else mode_from
        if reference is not None and reference.exists() and os.name == "posix":
            shutil.copymode(reference, temporary)
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


def _legacy_copies() -> list[Path]:
    """The kept reminders. A symbolic link in place of the legacy directory is refused."""
    if LEGACY_DIR.is_symlink():
        raise ProtocolSourceError(f"refusing the symbolic link {LEGACY_DIR}")
    return sorted(LEGACY_DIR.glob("memory-routing-v*.md"))


def _kept(previous: str) -> bool:
    data = previous.encode("utf-8")
    return any(copy.read_bytes().replace(b"\r\n", b"\n") == data for copy in _legacy_copies())


def _needs_legacy(rendered: str, released: str) -> bool:
    """Whether migration would not recognize the ``released`` reminder: neither rendered now nor kept."""
    return released != rendered and not _kept(released)


def _keep_legacy(previous: str) -> Path:
    """Save ``previous`` as the next legacy reminder, through a temporary file, with the reminder's mode."""
    numbers = [int(match.group(1)) for copy in _legacy_copies()
               if (match := re.fullmatch(r"memory-routing-v(\d+)\.md", copy.name))]
    target = LEGACY_DIR / f"memory-routing-v{max(numbers, default=0) + 1}.md"
    if target.is_symlink() or target.exists():
        raise ProtocolSourceError(f"refusing to replace {target}")
    _replace(target, previous, mode_from=MEMORY_REMINDER)
    return target


def write_generated(on_write: Callable[[Path], None] = lambda path: None,
                    on_source: Callable[[str], None] = lambda source: None,
                    on_unknown_release: Callable[[], None] = lambda: None) -> list[Path]:
    """Regenerate the committed files; return the ones written, a new legacy reminder included.

    Everything is rendered, and every target checked, before the first write, so a source error
    or a symbolic link changes no file. On every run the released reminder (``released_reminder``)
    is kept as the next legacy copy when migration would not recognize it, even when the
    reminder itself is current. Without a ``dev`` base the released reminder is unknown: no
    legacy copy is written, since the working-tree reminder may never have been installed, and
    ``on_unknown_release`` is called. ``on_write`` hears of each file as soon as it is written,
    ``on_source`` where a kept reminder came from.
    """
    changed = []

    def written(path: Path) -> None:
        changed.append(path)
        on_write(path)
    renders = _renders()
    if MEMORY_REMINDER in renders:
        released = released_reminder()
        if released is None:
            on_unknown_release()
        elif _needs_legacy(renders[MEMORY_REMINDER][1], released[0]):
            legacy = _keep_legacy(released[0])
            on_source(released[1])
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
            def unknown_release() -> None:
                print(f"warning: the released {MEMORY_REMINDER.name} could not be determined (no merge base with "
                      f"origin/dev or dev), so no legacy copy was kept; fetch dev and run {REGENERATE} again",
                      file=sys.stderr)
            write_generated(report, lambda source: print(f"  kept the released reminder of {source}"), unknown_release)
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
