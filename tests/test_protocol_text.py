"""The protocol text has one source, src/engine/protocol/ (#431)."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.engine import persona, protocol_text
from src.engine.protocol_text import ProtocolSourceError

ROOT = Path(__file__).resolve().parents[1]
# Claude Code shows this many characters of the MCP server instructions before v2.1.296 (4,096
# since); tests/test_log_interaction_contract.py checks what each window must hold.
OLDER_CLAUDE_CODE_WINDOW = 2048


def test_committed_surfaces_match_the_render():
    stale = [str(path.relative_to(ROOT)) for path in protocol_text.stale_files()]
    assert not stale, f"stale protocol files {stale}: run {protocol_text.REGENERATE}"


def test_the_template_is_the_full_variants_in_reading_order():
    template = (ROOT / "scripts/templates/routing-protocol-core.md").read_text(encoding="utf-8")
    assert protocol_text.template() == template
    assert template.startswith("# Agents-Core Persona Protocol — version 2\n\n") and template.endswith(".\n")
    headings = [line for line in template.splitlines() if line.startswith("## ")]
    assert headings == ["## Delegated worker mode", "## Cooperating on intersecting work",
                        "## Start before the first answer", "## Decide locally before each answer",
                        "## Apply a complete response", "## Run a named flow",
                        "## Describe a repository on first work", "## Work that another session owns",
                        "## Finish each answer", "## Compatibility and unavailable servers"]


def test_server_and_persona_send_the_renders():
    from src import server
    assert server.mcp.instructions == protocol_text.server_instructions()
    assert persona.APPLY_INSTRUCTION == protocol_text.bundle_instruction()


def test_server_window_opens_with_the_one_line_rules():
    """#514: the per-turn lines and the delegation request fill the older window; the first-work
    rule follows the request, ahead of the rest of the exchange."""
    text = protocol_text.server_instructions()
    opening, delegation = text.split("\n\n", 2)[:2]
    assert [line.split()[0] for line in opening.split("\n")] == ["Persona", "If", "A", "Before", "Compose"]
    request, first_work, exchange = delegation.split("\n")
    assert request == protocol_text.part("delegation", "compact-request")
    assert request.startswith("Every AI delegation, including reviewers") and request.endswith("Require status: prepared.")
    assert len(opening) + 2 + len(request) < OLDER_CLAUDE_CODE_WINDOW
    assert first_work == protocol_text.part("first-work", "compact")
    assert exchange == protocol_text.part("delegation", "compact")


def test_bundle_instruction_is_one_paragraph_of_single_spaces():
    text = protocol_text.bundle_instruction()
    assert "\n" not in text and "  " not in text and text == text.strip()
    assert text.startswith("A worker already given a prepared agents-core/subagent/1 package")


SHORT_SURFACES = ("server_instructions", "bundle_instruction", "memory_reminder")


@pytest.mark.parametrize("section,variant,surfaces", [
    *((section, variant, SHORT_SURFACES) for section, variant in (
        ("delegation", "compact-request"), ("delegation", "compact"), ("artifacts", "compact"),
        ("output-artifacts", "compact"), ("local-inference", "compact"), ("cooperation", "compact"),
        ("intro", "compact"), ("first-work", "compact"), ("first-work", "compact-unavailable"),
        ("ownership", "compact"))),
    # The server instructions state the history errors in their tools paragraph, in their own words.
    ("history-errors", "compact", ("bundle_instruction", "memory_reminder")),
    ("finish", "progress", ("server_instructions", "memory_reminder")),
    ("finish", "redaction", ("bundle_instruction", "memory_reminder")),
])
def test_shared_rules_are_included_by_every_short_surface(section, variant, surfaces):
    flat = " ".join(protocol_text.part(section, variant).split())
    for surface in surfaces:
        assert flat in " ".join(getattr(protocol_text, surface)().split()), surface


def test_every_section_file_is_listed_and_only_listed_files_are_read():
    files = {path.stem for path in protocol_text.SOURCE_DIR.glob("*.md")} - {"README"}
    assert files == set(protocol_text.SECTION_NAMES)
    assert set(protocol_text.sections()) == set(protocol_text.SECTION_NAMES)


def test_the_source_states_no_rule_twice():
    copied = protocol_text.copied_sentences()
    assert not copied, "move each copied sentence into one section and include it: " + repr(copied)


@pytest.mark.parametrize("surface", ["template", "server_instructions", "bundle_instruction", "memory_reminder"])
def test_no_surface_repeats_a_sentence(surface):
    text = getattr(protocol_text, surface)()
    parts = re.split(r"\n\n|(?<=\.) (?=[A-Z])", text)
    assert protocol_text.repeated_sentences({str(i): chunk for i, chunk in enumerate(parts)}) == {}


def test_repeated_sentences_reports_every_place_and_ignores_short_and_cosmetic_differences():
    rule = "Require a valid result before accepting, integrating or marking the work complete."
    blocks = {"a": f"Intro. {rule}", "b": "`Require` a valid   result before accepting,\nintegrating or marking the work complete",
              "c": "Then send the final answer. Then send the final answer.", "d": "Unrelated text that is long enough."}
    assert protocol_text.repeated_sentences(blocks) == {
        "require a valid result before accepting, integrating or marking the work complete": ["a", "b"]}


def test_copied_sentences_allows_only_full_prose_quoting_one_short_form(monkeypatch):
    sentence = "Agree on scope, sequence, priority source, artifacts and one integrator."
    monkeypatch.setattr(protocol_text, "sections", lambda: {
        "cooperation": {"full": sentence + " More prose.", "compact": sentence},
        "worker": {"server": "A worker returns its JSON result only, within its scope.",
                   "compact": "A worker returns its JSON result only, within its scope."},
        "finish": {"full": "Mention it once in the answer and never retry logging in a loop."},
        "tools": {"server": "Mention it once in the answer and never retry logging in a loop."},
    })
    assert protocol_text.copied_sentences() == {
        "a worker returns its json result only, within its scope": ["worker:compact", "worker:server"],
        "mention it once in the answer and never retry logging in a loop": ["finish:full", "tools:server"],
    }


@pytest.mark.parametrize("text,message", [
    ("Loose line\n<!-- variant: full -->\nBody\n", "before the first variant marker"),
    ("<!-- variant: full -->\nBody\n<!-- variant: full -->\nAgain\n", "appears twice"),
    ("<!-- variant: full -->\n\n<!-- variant: compact -->\nBody\n", "is empty"),
    ("Just text\n", "before the first variant marker"),
    ("\n\n", "no variant marker"),
])
def test_malformed_section_files_are_refused(tmp_path, text, message):
    path = tmp_path / "broken.md"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ProtocolSourceError, match=message):
        protocol_text._parse(path)


def test_a_missing_variant_names_the_section():
    with pytest.raises(ProtocolSourceError, match="no variant tools:full"):
        protocol_text.part("tools", "full")


def test_managed_claude_md_keeps_repository_notes(tmp_path):
    begin, end = protocol_text.BEGIN_MARKER, protocol_text.END_MARKER
    text = f"Before\n{begin}\n\nold protocol\n\n{end}\n\n## Repository notes\nKeep me.\n"
    rendered = protocol_text.managed_claude_md(text)
    assert rendered == f"Before\n{begin}\n\n{protocol_text.template()}\n{end}\n\n## Repository notes\nKeep me.\n"
    assert protocol_text.managed_claude_md(rendered) == rendered
    with pytest.raises(ProtocolSourceError, match="marker pair"):
        protocol_text.managed_claude_md("no markers\n")


def test_write_regenerates_stale_files_and_check_reports_them(tmp_path, monkeypatch):
    template, reminder = tmp_path / "template.md", tmp_path / "reminder.md"
    template.write_text("stale\n", encoding="utf-8")
    reminder.write_text(protocol_text.memory_reminder(), encoding="utf-8")
    monkeypatch.setattr(protocol_text, "ROOT", tmp_path)
    monkeypatch.setattr(protocol_text, "GENERATED", {
        template: lambda _current: protocol_text.template(),
        reminder: lambda _current: protocol_text.memory_reminder(),
    })
    assert protocol_text.main(["--check"]) == 1
    assert protocol_text.stale_files() == [template]
    assert protocol_text.write_generated() == [template]
    assert template.read_bytes() == protocol_text.template().encode("utf-8")
    assert protocol_text.main([]) == 0


def test_the_command_line_check_passes_on_the_repository():
    result = subprocess.run([sys.executable, "-m", "src.engine.protocol_text", "--check"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_write_keeps_the_previous_reminder_as_the_next_legacy_copy(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "memory-routing-v1.md").write_bytes(b"first\n")
    (legacy / "memory-routing-v3.md").write_bytes(b"third\n")
    reminder = tmp_path / "memory-routing.md"
    reminder.write_bytes(b"installed\n")
    monkeypatch.setattr(protocol_text, "ROOT", tmp_path)
    monkeypatch.setattr(protocol_text, "LEGACY_DIR", legacy)
    monkeypatch.setattr(protocol_text, "MEMORY_REMINDER", reminder)
    monkeypatch.setattr(protocol_text, "GENERATED", {reminder: lambda _current: protocol_text.memory_reminder()})
    assert protocol_text.write_generated() == [legacy / "memory-routing-v4.md", reminder]
    assert (legacy / "memory-routing-v4.md").read_bytes() == b"installed\n"
    reminder.write_bytes(b"third\n")  # already kept: no second copy
    assert protocol_text.write_generated() == [reminder]
    assert sorted(path.name for path in legacy.iterdir()) == ["memory-routing-v1.md", "memory-routing-v3.md",
                                                               "memory-routing-v4.md"]


def test_a_render_error_writes_nothing_and_the_command_reports_it(tmp_path, monkeypatch, capsys):
    first, second = tmp_path / "first.md", tmp_path / "second.md"
    first.write_text("stale\n", encoding="utf-8")
    second.write_text("no markers\n", encoding="utf-8")
    monkeypatch.setattr(protocol_text, "ROOT", tmp_path)
    monkeypatch.setattr(protocol_text, "GENERATED", {
        first: lambda _current: protocol_text.template(), second: protocol_text.managed_claude_md})
    assert protocol_text.main(["--write"]) == 2
    assert "marker pair" in capsys.readouterr().err
    assert first.read_text(encoding="utf-8") == "stale\n"
    assert not list(tmp_path.glob(".*.tmp"))


def test_a_stray_file_in_the_source_directory_does_not_break_the_render(tmp_path, monkeypatch):
    source = tmp_path / "protocol"
    source.mkdir()
    for name in protocol_text.SECTION_NAMES:
        (source / f"{name}.md").write_bytes((protocol_text.SOURCE_DIR / f"{name}.md").read_bytes())
    (source / "README.md").write_text("How the variants work.\n", encoding="utf-8")
    (source / "NOTES.md").write_text("<!-- variant: full -->\n<!-- variant: full -->\n", encoding="utf-8")
    expected = protocol_text.server_instructions()
    monkeypatch.setattr(protocol_text, "SOURCE_DIR", source)
    protocol_text.sections.cache_clear()
    try:
        assert protocol_text.server_instructions() == expected
    finally:
        protocol_text.sections.cache_clear()


def test_replace_keeps_the_file_mode_and_refuses_a_symbolic_link(tmp_path):
    import os, stat
    target = tmp_path / "template.md"
    target.write_text("old\n", encoding="utf-8")
    os.chmod(target, 0o644)
    protocol_text._replace(target, "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    if os.name == "posix":  # Windows keeps only a read-only flag, which _replace leaves alone
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ProtocolSourceError, match="symbolic link"):
        protocol_text._replace(link, "other\n")
    assert target.read_text(encoding="utf-8") == "new\n"


def test_write_keeps_the_released_reminder_not_the_working_tree_one(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    reminder = tmp_path / "memory-routing.md"
    reminder.write_text("intermediate, never installed\n", encoding="utf-8")
    monkeypatch.setattr(protocol_text, "ROOT", tmp_path)
    monkeypatch.setattr(protocol_text, "LEGACY_DIR", legacy)
    monkeypatch.setattr(protocol_text, "MEMORY_REMINDER", reminder)
    monkeypatch.setattr(protocol_text, "released_reminder", lambda: ("released\n", "abc (merge base with dev)"))
    monkeypatch.setattr(protocol_text, "GENERATED", {reminder: lambda _current: protocol_text.memory_reminder()})
    seen, sources = [], []
    assert protocol_text.write_generated(seen.append, sources.append) == [legacy / "memory-routing-v1.md", reminder] == seen
    assert (legacy / "memory-routing-v1.md").read_text(encoding="utf-8") == "released\n"
    assert sources == ["abc (merge base with dev)"]
    # The reminder is current now, but a released one that is not kept is still saved and reported.
    (legacy / "memory-routing-v1.md").unlink()
    assert protocol_text.stale_files() == [legacy]
    assert protocol_text.write_generated() == [legacy / "memory-routing-v1.md"]
    assert protocol_text.stale_files() == []


def test_released_reminder_is_the_one_at_the_merge_base_with_dev():
    released = protocol_text.released_reminder()
    if released is None:
        pytest.skip("no git or no dev branch in this checkout")
    copies = [path.read_text(encoding="utf-8") for path in protocol_text.LEGACY_DIR.glob("memory-routing-v*.md")]
    assert released[0] == protocol_text.memory_reminder() or released[0] in copies


def test_released_reminder_reads_the_merge_base_with_dev_in_git(tmp_path, monkeypatch):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init", "-q", "-b", "dev")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    reminder = tmp_path / "memory-routing.md"
    reminder.write_text("released\n", encoding="utf-8")
    git("add", "memory-routing.md")
    git("commit", "-q", "-m", "release")
    git("switch", "-q", "-c", "feature")
    reminder.write_text("intermediate\n", encoding="utf-8")
    git("commit", "-q", "-am", "feature work")
    monkeypatch.setattr(protocol_text, "ROOT", tmp_path)
    monkeypatch.setattr(protocol_text, "MEMORY_REMINDER", reminder)
    text, source = protocol_text.released_reminder()
    assert text == "released\n" and "merge base with dev" in source


def test_a_symbolic_link_among_the_targets_is_refused_before_any_write(tmp_path, monkeypatch):
    first, real, link = tmp_path / "first.md", tmp_path / "real.md", tmp_path / "link.md"
    first.write_text("stale\n", encoding="utf-8")
    real.write_text("x\n", encoding="utf-8")
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    monkeypatch.setattr(protocol_text, "ROOT", tmp_path)
    monkeypatch.setattr(protocol_text, "GENERATED", {
        first: lambda _current: protocol_text.template(), link: lambda _current: protocol_text.template()})
    assert protocol_text.main(["--write"]) == 2
    assert first.read_text(encoding="utf-8") == "stale\n"
