"""Installer migration must not overwrite unrelated or user-edited instructions."""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def helpers(monkeypatch):
    helper_dir = Path(__file__).resolve().parents[1] / "scripts" / "_helpers"
    monkeypatch.syspath_prepend(str(helper_dir))
    injector = importlib.import_module("inject_claude_md")
    memory = importlib.import_module("migrate_routing_memory")
    return injector, memory


def test_managed_replacement_preserves_outside_bytes_and_backup(tmp_path, helpers):
    injector, _ = helpers
    target, source = tmp_path / "CLAUDE.md", tmp_path / "protocol.md"
    prefix, suffix = b"# Personal rules\r\nUse Spanish.\r\n\r\n", b"\r\n\r\nKeep this\r\n"
    old = prefix + injector.MARKER_BEGIN.encode() + b"\nold\n" + injector.MARKER_END.encode() + suffix
    target.write_bytes(old)
    source.write_text("new protocol\n", encoding="utf-8")

    assert injector.inject(target, source)
    result = target.read_bytes()
    assert result.startswith(prefix)
    assert result.endswith(suffix)
    assert b"new protocol" in result
    backups = list(tmp_path.glob("CLAUDE.md.backup.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == old
    assert not injector.inject(target, source)
    assert list(tmp_path.glob("CLAUDE.md.backup.*")) == backups


def test_legacy_markers_migrate_without_duplicate_section(tmp_path, helpers):
    injector, _ = helpers
    target, source = tmp_path / "CLAUDE.md", tmp_path / "protocol.md"
    target.write_text(f"Before\n{injector.LEGACY_MARKER_BEGIN}\nold\n{injector.LEGACY_MARKER_END}\nAfter")
    source.write_text("protocol 2")
    injector.inject(target, source)
    result = target.read_text()
    assert result.count(injector.MARKER_BEGIN) == 1
    assert injector.LEGACY_MARKER_BEGIN not in result
    assert result.startswith("Before\n") and result.endswith("\nAfter")


@pytest.mark.parametrize("shape", ["begin_only", "end_only", "reversed", "duplicate", "mixed"])
def test_invalid_markers_leave_entire_file_untouched(tmp_path, helpers, shape):
    injector, _ = helpers
    begin, end = injector.MARKER_BEGIN, injector.MARKER_END
    contents = {
        "begin_only": begin, "end_only": end, "reversed": end + "\n" + begin,
        "duplicate": begin + "\n" + begin + "\n" + end,
        "mixed": begin + "\n" + end + "\n" + injector.LEGACY_MARKER_BEGIN,
    }[shape]
    target, source = tmp_path / "CLAUDE.md", tmp_path / "protocol.md"
    target.write_text(contents)
    source.write_text("protocol 2")
    with pytest.raises(ValueError, match="marker"):
        injector.inject(target, source)
    assert target.read_text() == contents
    assert not list(tmp_path.glob("*.backup.*"))


def test_append_preserves_user_instructions_without_final_newline(tmp_path, helpers):
    injector, _ = helpers
    target, source = tmp_path / "CLAUDE.md", tmp_path / "protocol.md"
    target.write_text("User instructions")
    source.write_text("protocol 2\n")
    injector.inject(target, source)
    assert target.read_text().startswith("User instructions\n" + injector.MARKER_BEGIN)


def test_known_memory_and_index_migrate_with_backups(tmp_path, helpers):
    _, memory = helpers
    reminder, index = tmp_path / memory.FILENAME, tmp_path / "MEMORY.md"
    original = (memory.TEMPLATES / "memory-routing-v1.md").read_bytes()
    reminder.write_bytes(original)
    old_index = b"# My index\r\n" + memory.INDEX_ENTRIES[1].encode() + b"\r\nOther note\r\n"
    index.write_bytes(old_index)
    assert memory.migrate(tmp_path, 2)
    assert reminder.read_bytes() == (memory.TEMPLATES / "memory-routing-v2.md").read_bytes()
    assert index.read_bytes() == b"# My index\r\n" + memory.INDEX_ENTRIES[2].encode() + b"\r\nOther note\r\n"
    assert next(tmp_path.glob(memory.FILENAME + ".backup.*")).read_bytes() == original
    assert next(tmp_path.glob("MEMORY.md.backup.*")).read_bytes() == old_index
    backups = list(tmp_path.glob("*.backup.*"))
    assert not memory.migrate(tmp_path, 2)
    assert list(tmp_path.glob("*.backup.*")) == backups


def test_user_edited_memory_is_preserved_with_actionable_warning(tmp_path, helpers, capsys):
    _, memory = helpers
    reminder, index = tmp_path / memory.FILENAME, tmp_path / "MEMORY.md"
    custom = (memory.TEMPLATES / "memory-routing-v1.md").read_bytes() + b"\nMy exception\n"
    reminder.write_bytes(custom)
    index.write_text(memory.INDEX_ENTRIES[1] + "\n")
    assert not memory.migrate(tmp_path, 2)
    assert reminder.read_bytes() == custom
    assert index.read_text() == memory.INDEX_ENTRIES[1] + "\n"
    warning = capsys.readouterr().err
    assert str(reminder) in warning and "Manually" in warning and "keep/switch" in warning
    assert not list(tmp_path.glob("*.backup.*"))


@pytest.mark.parametrize("suffix", [" — my custom note", "\n" + "duplicate feedback_agents_core_routing.md"])
def test_user_edited_or_duplicate_index_entry_is_preserved(tmp_path, helpers, capsys, suffix):
    _, memory = helpers
    reminder, index = tmp_path / memory.FILENAME, tmp_path / "MEMORY.md"
    reminder.write_bytes((memory.TEMPLATES / "memory-routing-v1.md").read_bytes())
    custom_index = memory.INDEX_ENTRIES[1] + suffix + "\n"
    index.write_text(custom_index)
    assert memory.migrate(tmp_path, 2)
    assert index.read_text() == custom_index
    assert str(index) in capsys.readouterr().err
    assert not list(tmp_path.glob("MEMORY.md.backup.*"))


def test_windows_missing_memory_is_not_created(tmp_path, helpers):
    _, memory = helpers
    directory = tmp_path / "missing"
    assert not memory.migrate(directory, 2, existing_only=True)
    assert not directory.exists()


def test_unix_first_install_creates_memory_and_preserves_existing_index(tmp_path, helpers):
    _, memory = helpers
    index = tmp_path / "MEMORY.md"
    index.write_bytes(b"[My note](my-note.md)")
    assert memory.migrate(tmp_path, 2)
    assert index.read_text() == "[My note](my-note.md)\n" + memory.INDEX_ENTRIES[2] + "\n"
    assert (tmp_path / memory.FILENAME).exists()


def test_known_v2_reminder_can_roll_back_to_v1(tmp_path, helpers):
    _, memory = helpers
    memory.migrate(tmp_path, 2)
    assert memory.migrate(tmp_path, 1)
    assert (tmp_path / memory.FILENAME).read_bytes() == (memory.TEMPLATES / "memory-routing-v1.md").read_bytes()
    assert (tmp_path / "MEMORY.md").read_text() == memory.INDEX_ENTRIES[1] + "\n"


def test_symlink_target_is_not_replaced(tmp_path, helpers):
    injector, _ = helpers
    original, link, source = tmp_path / "personal.md", tmp_path / "CLAUDE.md", tmp_path / "protocol.md"
    original.write_text("My instructions")
    link.symlink_to(original)
    source.write_text("protocol 2")
    with pytest.raises(ValueError, match="symlink"):
        injector.inject(link, source)
    assert original.read_text() == "My instructions"
    assert link.is_symlink()
